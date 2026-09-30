"""Ranking of listings that pass the hard constraints.

Score = weighted mix of four signals a marketplace cares about:
  fit         how well soft preferences are met (quiet, whiteboard, fast wifi...)
  trust       review quality, Bayesian-adjusted so 5.0 from 3 reviews does not
              beat 4.7 from 300
  value       price relative to the user's budget (or to the market median)
  conversion  likelihood the booking completes: instant-book and right-sized room

Weights are explicit and documented; there is no paid boosting.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median

from app.core.models import Listing, ResolvedQuery, ScoreBreakdown
from app.core.vocab import FAST_WIFI_MBPS, amenity_phrase
from app.services.matcher import Evaluation, inr, per_person_hour

WEIGHTS = {"fit": 0.45, "trust": 0.25, "value": 0.15, "conversion": 0.15}
WEIGHTS_NO_PREFS = {"trust": 0.45, "value": 0.30, "conversion": 0.25}
BAYES_C = 10  # pseudo-reviews at the dataset mean


@dataclass
class MarketStats:
    mean_rating: float
    median_pp_hour: float

    @classmethod
    def from_listings(cls, listings: list[Listing]) -> "MarketStats":
        rated = [l.rating for l in listings if l.review_count > 0]
        pps = [l.price_per_hour if l.pricing == "per_seat" else l.price_per_hour / l.capacity for l in listings]
        return cls(mean_rating=mean(rated) if rated else 4.0, median_pp_hour=median(pps))


def bayes_rating(l: Listing, stats: MarketStats) -> float:
    return (BAYES_C * stats.mean_rating + l.rating * l.review_count) / (BAYES_C + l.review_count)


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def soft_signals(l: Listing, q: ResolvedQuery, stats: MarketStats) -> tuple[list[float], list[str], list[str]]:
    """Return (component scores, matched facts, missed facts) for soft preferences."""
    scores: list[float] = []
    hits: list[str] = []
    misses: list[str] = []
    if q.prefer_quiet:
        s = {"quiet": 1.0, "moderate": 0.4, "lively": 0.0}[l.noise_level]
        scores.append(s)
        if s == 1.0:
            hits.append("Quiet space")
        else:
            misses.append("Moderate noise level" if l.noise_level == "moderate" else "Lively, can get noisy")
    if q.prefer_fast_wifi:
        s = _clip((l.wifi_mbps - 40) / (FAST_WIFI_MBPS - 40))
        scores.append(s)
        if l.wifi_mbps >= FAST_WIFI_MBPS:
            hits.append(f"Fast Wi-Fi ({l.wifi_mbps} Mbps)")
        else:
            misses.append(f"Wi-Fi is only {l.wifi_mbps} Mbps")
    for a in q.preferred_amenities:
        has = a in l.amenities
        scores.append(1.0 if has else 0.0)
        (hits if has else misses).append(f"{'Has' if has else 'No'} {amenity_phrase(a)}")
    if q.min_rating:
        br = bayes_rating(l, stats)
        ok = l.review_count > 0 and l.rating >= q.min_rating
        scores.append(1.0 if ok else _clip(br / q.min_rating) * 0.5)
        if ok:
            hits.append(f"Rated {l.rating} ({l.review_count} reviews)")
        else:
            misses.append(f"Rated {l.rating} ({l.review_count} reviews), below {q.min_rating}")
    return scores, hits, misses


def score(ev: Evaluation, q: ResolvedQuery, stats: MarketStats) -> ScoreBreakdown:
    l = ev.listing
    comps, _, _ = soft_signals(l, q, stats)
    fit = mean(comps) if comps else None

    trust = _clip((bayes_rating(l, stats) - 3.5) / 1.4)

    if q.budget_amount and ev.cost_in_budget_unit is not None:
        value = _clip(1 - ev.cost_in_budget_unit / q.budget_amount)
    else:
        pp = per_person_hour(l, q.party_size) or (l.price_per_hour / l.capacity)
        value = _clip(1 - 0.5 * pp / stats.median_pp_hour)

    if q.party_size and l.space_type != "hot_desk":
        right_size = _clip((q.party_size / l.capacity) / 0.6)
    else:
        right_size = 1.0
    conversion = 0.5 * (1.0 if l.instant_book else 0.0) + 0.5 * right_size

    if fit is None:
        w = WEIGHTS_NO_PREFS
        total = w["trust"] * trust + w["value"] * value + w["conversion"] * conversion
    else:
        w = WEIGHTS
        total = w["fit"] * fit + w["trust"] * trust + w["value"] * value + w["conversion"] * conversion
    return ScoreBreakdown(
        fit=None if fit is None else round(fit, 3),
        trust=round(trust, 3),
        value=round(value, 3),
        conversion=round(conversion, 3),
        total=round(100 * total, 1),
    )


def general_tradeoffs(ev: Evaluation, q: ResolvedQuery) -> list[str]:
    """Trade-offs not tied to a stated preference but worth telling a booker."""
    l = ev.listing
    out: list[str] = []
    if q.party_size and l.space_type != "hot_desk" and q.party_size / l.capacity < 0.5:
        out.append(f"Seats {l.capacity}, much larger than your group of {q.party_size}")
    if q.party_size and q.party_size > 1 and l.space_type == "hot_desk":
        out.append("Open hot-desk area, not a private room for your group")
    if l.review_count == 0:
        out.append("New listing with no reviews yet")
    elif l.review_count < 10:
        out.append(f"Only {l.review_count} review{'s' if l.review_count > 1 else ''} so far")
    if q.budget_amount and ev.cost_in_budget_unit and 0.9 * q.budget_amount < ev.cost_in_budget_unit <= q.budget_amount:
        out.append("Close to the top of your budget")
    if not l.instant_book:
        out.append("Host must approve the booking")
    return out


def rank_exact(evals: list[Evaluation], q: ResolvedQuery, stats: MarketStats) -> list[tuple[Evaluation, ScoreBreakdown]]:
    scored = [(ev, score(ev, q, stats)) for ev in evals]
    scored.sort(key=lambda t: (-t[1].total, t[0].listing.id))
    return scored


def rank_alternatives(evals: list[Evaluation], q: ResolvedQuery, stats: MarketStats, max_cost: float = 3.0):
    """Closest non-matching listings: fewest/smallest violations first, quality second."""
    cands = [ev for ev in evals if not ev.is_exact and ev.violation_cost <= max_cost]
    scored = [(ev, score(ev, q, stats)) for ev in cands]
    scored.sort(key=lambda t: (round(t[0].violation_cost, 2), -t[1].total, t[0].listing.id))
    return scored


__all__ = ["MarketStats", "score", "soft_signals", "general_tradeoffs", "rank_exact", "rank_alternatives", "bayes_rating", "inr"]
