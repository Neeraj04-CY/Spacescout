"""Deterministic normalisation: ParsedQuery (language) -> ResolvedQuery (facts).

This is where dates, time windows, locations and budget units are resolved.
Keeping this out of the LLM means relative dates ("tomorrow afternoon") and
unit arithmetic are always computed correctly and are unit-testable.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re

from app.core.models import ParsedQuery, ResolvedQuery
from app.core.vocab import (
    AREAS,
    DEFAULT_DURATION_HOURS,
    NEARBY_KM,
    OTHER_CITIES,
    TIME_OF_DAY_WINDOWS,
    WEEKDAYS,
    area_distance_km,
)

_CITY_WIDE = {"mumbai", "bombay", "anywhere", "any", "anywhere in mumbai", "mumbai city"}
_NEAR_RE = re.compile(r"\b(near|around|close to|next to|nearby|near to|walking distance)\b")
_FILLER_RE = re.compile(r"\b(near|around|close to|next to|nearby|near to|in|at|the|area|side|station|somewhere)\b")

_ALIAS_TO_AREA: list[tuple[str, str]] = sorted(
    ((alias, key) for key, meta in AREAS.items() for alias in meta["aliases"]),
    key=lambda t: -len(t[0]),  # longest alias first: "bandra kurla complex" before "bandra"
)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def fmt_minutes(m: int) -> str:
    m = min(m, 24 * 60 - 1) if m >= 24 * 60 else m
    return f"{m // 60:02d}:{m % 60:02d}"


def resolve_location(text: str | None) -> tuple[list[str], str | None, str | None, bool]:
    """Return (area_keys, mode, unresolved_text, outside_city)."""
    if not text or not text.strip():
        return [], None, None, False
    t = text.lower().strip()
    if t in _CITY_WIDE:
        return [], None, None, False
    near = bool(_NEAR_RE.search(t))
    parts = [p.strip() for p in re.split(r",|/|&|\bor\b|\band\b", t) if p.strip()]
    areas: list[str] = []
    unresolved: list[str] = []
    outside = False
    for part in parts:
        found = None
        for alias, key in _ALIAS_TO_AREA:
            if re.search(rf"\b{re.escape(alias)}\b", part):
                found = key
                break
        if not found:
            cleaned = _FILLER_RE.sub(" ", part)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if not cleaned or cleaned in _CITY_WIDE:
                continue
            match = difflib.get_close_matches(cleaned, [a for a, _ in _ALIAS_TO_AREA], n=1, cutoff=0.8)
            if match:
                found = dict(_ALIAS_TO_AREA)[match[0]]
            elif cleaned in OTHER_CITIES:
                outside = True
                unresolved.append(cleaned)
            else:
                unresolved.append(cleaned)
        if found and found not in areas:
            areas.append(found)
    if near and areas:
        for key in list(areas):
            for other in AREAS:
                if other not in areas and area_distance_km(key, other) <= NEARBY_KM:
                    areas.append(other)
    mode = ("near" if near else "exact") if areas else None
    return areas, mode, (", ".join(unresolved) if unresolved else None), outside


def _resolve_date(p: ParsedQuery, now: dt.datetime, assumptions: list[str]) -> dt.date | None:
    today = now.date()
    if p.day_kind == "today":
        return today
    if p.day_kind == "tomorrow":
        return today + dt.timedelta(days=1)
    if p.day_kind == "day_after_tomorrow":
        return today + dt.timedelta(days=2)
    if p.day_kind == "weekday" and p.weekday:
        target = WEEKDAYS.index(p.weekday)
        delta = (target - today.weekday()) % 7
        return today + dt.timedelta(days=delta)
    if p.day_kind == "date" and p.date:
        d = dt.date.fromisoformat(p.date)
        if d < today:
            assumptions.append(f"{d:%d %b %Y} is in the past, so the date was ignored")
            return None
        return d
    if p.date:  # date given without day_kind
        d = dt.date.fromisoformat(p.date)
        return d if d >= today else None
    return None


def normalize(p: ParsedQuery, now: dt.datetime) -> ResolvedQuery:
    assumptions: list[str] = []
    areas, mode, unresolved, outside = resolve_location(p.location_text)

    day = _resolve_date(p, now, assumptions)

    # ---- time window -------------------------------------------------------
    ws = we = None
    duration = int(p.duration_hours * 60) if p.duration_hours else None
    if p.start_time and p.end_time and _minutes(p.end_time) > _minutes(p.start_time):
        ws, we = _minutes(p.start_time), _minutes(p.end_time)
        duration = duration or (we - ws)
    elif p.start_time:
        ws = _minutes(p.start_time)
        duration = duration or int(DEFAULT_DURATION_HOURS * 60)
        we = min(ws + duration, 24 * 60)
        if not p.duration_hours:
            assumptions.append(f"Assumed a {DEFAULT_DURATION_HOURS:g}-hour booking")
    elif p.time_of_day:
        s, e = TIME_OF_DAY_WINDOWS[p.time_of_day]
        ws, we = _minutes(s), _minutes(e)
        if p.time_of_day == "full_day":
            duration = duration or (we - ws)
        elif not duration:
            duration = int(DEFAULT_DURATION_HOURS * 60)
            assumptions.append(f"Assumed a {DEFAULT_DURATION_HOURS:g}-hour slot within the {p.time_of_day}")

    if (ws is not None) and day is None:
        # A time was given without a day: today if still possible, else tomorrow.
        now_min = now.hour * 60 + now.minute
        still_possible_today = we - max(ws, now_min) >= (duration or 0)
        day = now.date() if still_possible_today else now.date() + dt.timedelta(days=1)
        assumptions.append(f"No day given, assumed {'today' if day == now.date() else 'tomorrow'}")

    if day is not None and ws is None:
        ws, we = 0, 24 * 60
        if not duration:
            duration = int(DEFAULT_DURATION_HOURS * 60)
            assumptions.append(f"No time given, looking for any {DEFAULT_DURATION_HOURS:g}-hour slot that day")

    if day is not None and day == now.date() and ws is not None:
        now_min = now.hour * 60 + now.minute
        rounded = ((now_min + 29) // 30) * 30
        if ws < rounded:
            ws = rounded
            assumptions.append(f"Only considering slots from {fmt_minutes(ws)} today")

    # ---- budget ------------------------------------------------------------
    unit = p.budget_unit
    if p.budget_amount is not None and unit is None:
        unit = "total_per_hour"
        assumptions.append("Budget unit unclear, treated as total per hour")
    if unit in ("per_person_per_hour", "per_person_per_day") and not p.party_size and p.budget_amount is not None:
        assumptions.append("Group size not given, per-person budget computed for 1 person")

    return ResolvedQuery(
        areas=areas,
        location_mode=mode,
        location_unresolved=unresolved,
        location_outside_city=outside,
        party_size=p.party_size,
        space_type=p.space_type,
        budget_amount=p.budget_amount,
        budget_unit=unit if p.budget_amount is not None else None,
        date=day,
        window_start=ws,
        window_end=we,
        duration_min=duration if day is not None else None,
        required_amenities=p.required_amenities,
        preferred_amenities=p.preferred_amenities,
        prefer_quiet=p.prefer_quiet,
        prefer_fast_wifi=p.prefer_fast_wifi,
        min_rating=p.min_rating,
        unsupported_requests=p.unsupported_requests,
        off_topic=p.off_topic,
        assumptions=assumptions,
    )
