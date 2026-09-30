"""Search orchestration: parse -> normalise -> decide -> match -> rank -> explain.

This is plain, explicit control flow rather than an agent loop: each step has one
job, runs once, and is observable in the response trace. The LLM is used in two
bounded places (understanding the request, phrasing explanations); every
decision about which listings to show is made by deterministic code.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from zoneinfo import ZoneInfo

from app.config import Settings
from app.core.models import (
    Chip,
    Listing,
    ParsedQuery,
    ResolvedQuery,
    ResultItem,
    SearchRequest,
    SearchResponse,
)
from app.core.vocab import AREAS, SPACE_TYPES, amenity_phrase
from app.services.explainer import FactSheet, explain
from app.services.llm_client import LLMClient, LLMError
from app.services.matcher import (
    UNIT_LABEL,
    Evaluation,
    active_constraints,
    evaluate_all,
    inr,
    per_person_hour,
)
from app.services.normalize import fmt_minutes, normalize
from app.services.parser_llm import LLMParser
from app.services.parser_rules import parse_rules
from app.services.reconcile import reconcile
from app.services.ranker import (
    MarketStats,
    general_tradeoffs,
    rank_alternatives,
    rank_exact,
    score,
    soft_signals,
)

log = logging.getLogger("spacescout.search")

EXAMPLE_QUERIES = [
    "Meeting room for 6 in BKC tomorrow morning",
    "Quiet desk in Andheri today, fast wifi, under ₹200 per hour",
    "Private cabin for 3 in Powai on Friday, must have a whiteboard",
]


class SearchService:
    def __init__(self, settings: Settings, listings: tuple[Listing, ...], client: LLMClient | None = None):
        self.s = settings
        self.listings = list(listings)
        self.stats = MarketStats.from_listings(self.listings)
        self.client = client or LLMClient(settings)
        self.llm_parser = LLMParser(self.client)

    # ------------------------------------------------------------------ parse
    async def _parse(self, query: str, mode: str, trace: dict) -> ParsedQuery:
        want_llm = mode == "llm" or (mode == "auto" and self.client.enabled)
        if want_llm:
            try:
                parsed, metas, hit = await self.llm_parser.parse(query)
                trace["parser"] = f"llm:{self.s.llm_model}" + (" (cached)" if hit else "")
                trace["llm_calls"].extend(m.as_dict() for m in metas)
                return parsed
            except LLMError as e:
                if e.meta is not None:
                    trace["llm_calls"].append(e.meta.as_dict())
                trace["parser"] = "rules (fallback)"
                trace["fallback_reason"] = f"{type(e).__name__}: {e}"
                log.warning("parser_fallback", extra={"reason": str(e)})
        else:
            trace["parser"] = "rules" + (" (no LLM key configured)" if not self.client.enabled else "")
        return parse_rules(query)

    # ------------------------------------------------------------------ main
    async def search(self, req: SearchRequest, request_id: str | None = None) -> SearchResponse:
        t0 = time.perf_counter()
        rid = request_id or uuid.uuid4().hex[:12]
        tz = ZoneInfo(self.s.app_timezone)
        now = dt.datetime.fromisoformat(req.reference_time) if req.reference_time else dt.datetime.now(tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)
        now = now.astimezone(tz)
        trace: dict = {"request_id": rid, "reference_time": now.isoformat(timespec="minutes"), "llm_calls": [], "timings_ms": {}}

        parsed = await self._parse(req.query, req.parser, trace)
        parsed, notes = reconcile(parsed, req.query)
        if notes:
            trace["reconciled"] = notes
        t_parse = time.perf_counter()
        q = normalize(parsed, now)
        q.assumptions = notes + q.assumptions
        chips = interpretation_chips(q)

        resp = dict(request_id=rid, interpretation=chips, parsed=parsed, results=[], total_exact_matches=0)

        # ---------------- decide whether we can search at all ----------------
        clarify = self._needs_clarification(q)
        if clarify:
            message, question, suggestions, show_popular = clarify
            items: list[ResultItem] = []
            if show_popular:
                popular = rank_exact(evaluate_all(self.listings, q), q, self.stats)[:3]
                items = await self._build_items(popular, q, "suggestion", trace)
            trace["timings_ms"].update(parse=_ms(t0, t_parse), total=_ms(t0))
            return SearchResponse(
                status="needs_clarification", message=message, clarifying_question=question,
                suggestions=suggestions, trace=trace, **{**resp, "results": items},
            )

        # ---------------- match + rank ----------------
        evals = evaluate_all(self.listings, q)
        exact = [e for e in evals if e.is_exact]
        ranked = rank_exact(exact, q, self.stats)
        alts = rank_alternatives(evals, q, self.stats)
        t_rank = time.perf_counter()

        limit = req.limit
        message = question = None
        suggestions: list[str] = []
        if ranked:
            chosen = [(ev, sc, "exact") for ev, sc in ranked[:limit]]
            status = "ok"
            if len(ranked) < 3 and alts:
                room = max(2, limit - len(chosen))
                chosen += [(ev, sc, "alternative") for ev, sc in alts[:room]]
                status = "partial"
                message = f"Only {len(ranked)} space{'s' if len(ranked) > 1 else ''} match{'es' if len(ranked) == 1 else ''} everything. Close alternatives are shown below, with what they miss."
            if not q.has_hard_constraints:
                question = "Want me to narrow this down? Tell me the area, group size or when you need it."
        else:
            status = "no_exact_match"
            message, question, suggestions = self._explain_no_match(q)
            chosen = [(ev, sc, "alternative") for ev, sc in alts[:limit]]
            if not chosen:
                message += " None of our listings come close enough to suggest."

        items = await self._build_items([(ev, sc) for ev, sc, _ in chosen], q, None, trace, kinds=[k for *_, k in chosen])
        t_end = time.perf_counter()
        trace["timings_ms"].update(parse=_ms(t0, t_parse), match_rank=_ms(t_parse, t_rank), explain=_ms(t_rank, t_end), total=_ms(t0, t_end))
        trace["counts"] = {"listings": len(self.listings), "exact": len(ranked), "alternatives_considered": len(alts)}
        log.info("search", extra={"request_id": rid, "status": status, "parser": trace["parser"], "exact": len(ranked), "total_ms": trace["timings_ms"]["total"]})
        return SearchResponse(
            status=status, message=message, clarifying_question=question, suggestions=suggestions, trace=trace,
            **{**resp, "results": items, "total_exact_matches": len(ranked)},
        )

    # ------------------------------------------------------------------ helpers
    def _needs_clarification(self, q: ResolvedQuery):
        """Return (message, question, suggestions, show_popular) or None."""
        covered = ", ".join(sorted({AREAS[l.area]["name"] for l in self.listings}))
        if q.off_topic:
            return (
                "I can only help find desks, meeting rooms and private cabins in Mumbai.",
                "What kind of space do you need, where, and for how many people?",
                EXAMPLE_QUERIES, False,
            )
        if q.location_outside_city and not q.areas:
            return (
                f"We only list spaces in Mumbai right now, so I can't search {q.location_unresolved.title()}.",
                f"Would a Mumbai area work? We cover {covered}.",
                EXAMPLE_QUERIES, False,
            )
        if q.location_unresolved and not q.areas:
            return (
                f"I couldn't match \"{q.location_unresolved}\" to an area we cover.",
                f"Which area should I search? We cover {covered}.",
                [], False,
            )
        if not q.has_hard_constraints and not q.has_soft_preferences:
            return (
                "That's a bit open-ended, so I haven't ranked anything for you yet.",
                "Where in Mumbai, for how many people, and when? Any must-haves (quiet, whiteboard, budget)?",
                EXAMPLE_QUERIES, True,
            )
        return None

    def _explain_no_match(self, q: ResolvedQuery) -> tuple[str, str, list[str]]:
        insights = []
        for c in active_constraints(q):
            evs = [e for e in evaluate_all(self.listings, q, skip={c}) if e.is_exact]
            insights.append((c, evs))
        insights.sort(key=lambda t: -len(t[1]))
        parts: list[str] = []
        suggestions: list[str] = []
        max_cap = max(l.capacity for l in self.listings)

        if q.party_size and q.party_size > max_cap:
            biggest = max(self.listings, key=lambda l: l.capacity)
            parts.append(f"No space in our listings fits {q.party_size} people; the largest ({biggest.name}) fits {max_cap}.")
            suggestions.append("Split the group across two rooms")

        for c, evs in insights:
            if not evs:
                continue
            n = len(evs)
            if c == "budget":
                cheapest = min(evs, key=lambda e: e.cost_in_budget_unit or 0)
                unit = UNIT_LABEL[q.budget_unit]
                if q.budget_amount > 0:
                    parts.append(f"Everything else fits {n} space{'s' if n > 1 else ''}, but the cheapest is {inr(cheapest.cost_in_budget_unit)}{unit} against your {inr(q.budget_amount)}{unit}.")
                else:
                    parts.append(f"No listing is free. The cheapest that fits everything else is {inr(cheapest.cost_in_budget_unit)}{unit}.")
                suggestions.append(f"Raise budget to {inr(cheapest.cost_in_budget_unit)}{unit}")
            elif c == "location":
                dist = {e.listing.area: (e.distance_km or 0.0) for e in evaluate_all([x.listing for x in evs], q)}
                areas_found = [AREAS[a]["name"] + f" ({d:.1f} km)" for a, d in sorted(dist.items(), key=lambda t: t[1])]
                parts.append(f"{n} space{'s' if n > 1 else ''} fit{'s' if n == 1 else ''} if you can go to {', '.join(areas_found[:3])}.")
                suggestions.append(f"Try {areas_found[0].split(' (')[0]}")
            elif c == "availability":
                parts.append(f"{n} space{'s' if n > 1 else ''} would fit at a different time.")
                suggestions.append("Try a different time or day")
            elif c == "capacity":
                parts.append(f"{n} space{'s' if n > 1 else ''} fit{'s' if n == 1 else ''} everything except group size.")
            elif c == "space_type":
                types = sorted({SPACE_TYPES[e.listing.space_type].lower() for e in evs})
                parts.append(f"{n} {' / '.join(types)} option{'s' if n > 1 else ''} fit if the space type is flexible.")
                suggestions.append("Consider a different space type")
            elif c == "amenity":
                parts.append(f"{n} space{'s' if n > 1 else ''} fit{'s' if n == 1 else ''} without the required amenities.")
                suggestions.append("Make the amenity optional")
            break  # the single most useful relaxation is enough

        if len(parts) == (1 if q.party_size and q.party_size > max_cap else 0):
            parts.append("Several of your requirements conflict, so relaxing just one isn't enough.")
        top = next((c for c, evs in insights if evs), None)
        question = {
            "budget": "Could you stretch the budget, or would a smaller or cheaper space work?",
            "location": "Would a nearby area work for you?",
            "availability": "Is your timing flexible?",
            "capacity": "Could you split the group, or is a larger budget possible?",
            "space_type": "Would a different kind of space work?",
            "amenity": "Is that amenity a must-have, or could it be optional?",
        }.get(top, "Which matters most to you: area, budget, group size or timing?")
        return "No space matches everything you asked for. " + " ".join(parts), question, suggestions

    async def _build_items(self, scored, q: ResolvedQuery, kind: str | None, trace: dict, kinds: list[str] | None = None) -> list[ResultItem]:
        sheets: list[FactSheet] = []
        prepared = []
        for i, (ev, sc) in enumerate(scored):
            k = kind or (kinds[i] if kinds else "exact")
            l = ev.listing
            _, hits, misses = soft_signals(l, q, self.stats)
            # Area and space type are already visible on the card; keep the reasons that discriminate.
            why = hits + [s for s in ev.satisfied if not s.startswith("In ") and s not in SPACE_TYPES.values()]
            if not why:
                why = [f"Rated {l.rating} from {l.review_count} reviews" if l.review_count else "Newly listed", f"{SPACE_TYPES[l.space_type]} for up to {l.capacity}"]
            tradeoffs = [v.message for v in ev.violations] + misses + general_tradeoffs(ev, q)
            sheets.append(FactSheet(l.id, l.name, why, tradeoffs))
            prepared.append((ev, sc, k, why, tradeoffs))

        texts, meta = await explain(sheets, self.client, self.s.explain_with_llm)
        if meta:
            trace["llm_calls"].append(meta.as_dict())

        items = []
        for ev, sc, k, why, tradeoffs in prepared:
            text, source = texts[ev.listing.id]
            pp = per_person_hour(ev.listing, q.party_size)
            items.append(ResultItem(
                listing=ev.listing, match=k, score=sc,
                price_per_person_hour=round(pp, 1) if pp is not None else None,
                cost_in_budget_unit=round(ev.cost_in_budget_unit, 1) if ev.cost_in_budget_unit is not None else None,
                why=why, tradeoffs=tradeoffs, violations=[v.message for v in ev.violations],
                available_slot=ev.slot_label, explanation=text, explanation_source=source,
            ))
        return items


def interpretation_chips(q: ResolvedQuery) -> list[Chip]:
    chips: list[Chip] = []
    if q.areas:
        names = [AREAS[a]["name"] for a in q.areas]
        chips.append(Chip(kind="hard", label=("Near " if q.location_mode == "near" else "Area: ") + ", ".join(names)))
    if q.party_size:
        chips.append(Chip(kind="hard", label=f"{q.party_size} {'person' if q.party_size == 1 else 'people'}"))
    if q.space_type:
        chips.append(Chip(kind="hard", label=SPACE_TYPES[q.space_type]))
    if q.budget_amount is not None and q.budget_unit:
        chips.append(Chip(kind="hard", label=f"≤ {inr(q.budget_amount)}{UNIT_LABEL[q.budget_unit]}"))
    if q.date:
        label = f"{q.date:%a %d %b}"
        if q.window_start is not None and not (q.window_start == 0 and q.window_end == 1440):
            label += f", {fmt_minutes(q.window_start)}–{fmt_minutes(q.window_end)}"
        if q.duration_min:
            label += f" ({q.duration_min / 60:g}h)"
        chips.append(Chip(kind="hard", label=label))
    for a in q.required_amenities:
        chips.append(Chip(kind="hard", label=f"Must have {amenity_phrase(a)}"))
    if q.prefer_quiet:
        chips.append(Chip(kind="soft", label="Prefer quiet"))
    if q.prefer_fast_wifi:
        chips.append(Chip(kind="soft", label="Prefer fast Wi-Fi"))
    for a in q.preferred_amenities:
        chips.append(Chip(kind="soft", label=f"Prefer {amenity_phrase(a)}"))
    if q.min_rating:
        chips.append(Chip(kind="soft", label=f"Prefer rating ≥ {q.min_rating:g}"))
    for a in q.assumptions:
        chips.append(Chip(kind="assumption", label=a))
    for u in q.unsupported_requests:
        chips.append(Chip(kind="unsupported", label=f"Can't filter on: {u}"))
    if q.location_unresolved and q.areas:
        chips.append(Chip(kind="unsupported", label=f"Unknown area ignored: {q.location_unresolved}"))
    return chips


def _ms(a: float, b: float | None = None) -> float:
    return round(((b or time.perf_counter()) - a) * 1000, 1)


__all__ = ["SearchService", "interpretation_chips", "Evaluation", "score"]
