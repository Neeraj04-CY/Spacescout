"""Availability against weekly opening hours and recurring bookings.

The prototype models availability as a static weekly pattern so the dataset
stays valid whatever day it is run. In production this module would call the
booking system at query time (see DESIGN_NOTE.md, "stale availability").
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.core.models import Listing
from app.services.normalize import fmt_minutes

DAY_NAMES = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]


def _m(hhmm: str) -> int:
    h, m = hhmm.split(":")
    v = int(h) * 60 + int(m)
    return 24 * 60 if v == 23 * 60 + 59 else v  # treat 23:59 as end of day


def free_blocks(listing: Listing, day: dt.date) -> list[tuple[int, int]]:
    """Free intervals (minutes from midnight) for the listing on `day`."""
    if day.weekday() not in listing.open_days:
        return []
    blocks = [(_m(listing.open_time), _m(listing.close_time))]
    for b in listing.recurring_bookings:
        if day.weekday() not in b.weekdays:
            continue
        bs, be = _m(b.start), _m(b.end)
        nxt = []
        for s, e in blocks:
            if be <= s or bs >= e:
                nxt.append((s, e))
                continue
            if s < bs:
                nxt.append((s, bs))
            if be < e:
                nxt.append((be, e))
        blocks = nxt
    return [(s, e) for s, e in blocks if e > s]


@dataclass
class AvailabilityResult:
    ok: bool
    slot: tuple[int, int] | None
    reason: str | None  # why not available, human readable
    other_slots: list[tuple[int, int]]  # free blocks outside the requested window (for alternatives)


def check_availability(listing: Listing, day: dt.date, ws: int, we: int, duration: int, not_before: int = 0) -> AvailabilityResult:
    blocks = [(max(s, not_before), e) for s, e in free_blocks(listing, day) if e > not_before]
    if not blocks:
        if day.weekday() not in listing.open_days:
            return AvailabilityResult(False, None, f"Closed on {DAY_NAMES[day.weekday()]}", [])
        return AvailabilityResult(False, None, "No free time left that day", [])
    for s, e in blocks:
        start, end = max(s, ws), min(e, we)
        if end - start >= duration:
            return AvailabilityResult(True, (start, start + duration), None, [])
    others = [(s, e) for s, e in blocks if e - s >= duration]
    open_s, open_e = _m(listing.open_time), _m(listing.close_time)
    open_overlap = min(we, open_e) - max(ws, open_s)
    if open_overlap < duration:
        reason = f"Open {listing.open_time}–{fmt_minutes(open_e)}, so a {duration / 60:g}h slot doesn't fit your time"
    else:
        reason = "Already booked during your time"
    return AvailabilityResult(False, None, reason, others)


def fmt_slot(day: dt.date, slot: tuple[int, int]) -> str:
    return f"{day:%a %d %b}, {fmt_minutes(slot[0])}–{fmt_minutes(slot[1])}"
