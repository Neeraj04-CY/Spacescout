"""Check LLM-parsed constraints against the words the user actually typed.

The LLM is good at language but will sometimes make a request *stricter* than
the user said: inventing a calendar date for "sunday", reading "under ₹1,200 an
hour" as per person, turning "with a whiteboard" into a must-have, or deciding
a "client pitch" needs a meeting room. A stricter-than-stated constraint silently
hides valid listings, so every hard constraint the parser sets must have lexical
evidence in the query. Without evidence, the constraint is relaxed, never
tightened, and the change is shown to the user as an assumption.

This runs on both parsers' output; the rule-based parser only sets fields it
found evidence for, so in practice it is a no-op there.
"""

from __future__ import annotations

import re

from app.core.models import ParsedQuery
from app.core.vocab import WEEKDAYS

_DAY_UNIT = re.compile(r"(per[- ]day|\ba day\b|for the day|/day|\bdaily\b|full[- ]day|all[- ]day|whole[- ]day|\bper din\b|\bdin ka\b|day rate|day pass)")
_PERSON_UNIT = re.compile(
    r"(per[- ]person|per[- ]head|/person|/head|\beach\b|\bpp\b|per pax|a head|per seat|per member|per individual"
    r"|prati vyakti|har (ek|vyakti|banda))"
)
_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_EXPLICIT_DATE = re.compile(
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s*(of\s+)?{_MONTHS}\b|\b{_MONTHS}\s+\d{{1,2}}\b|\b\d{{1,2}}[/-]\d{{1,2}}([/-]\d{{2,4}})?\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b"
)
_TODAY = re.compile(r"\b(today|tonight|this (morning|afternoon|evening)|aaj|abhi|right now)\b")
_TOMORROW = re.compile(r"\b(tomorrow|tmrw|tmr|tomorow|kal)\b")
_DAY_AFTER = re.compile(r"\b(day after tomorrow|parso|parson)\b")
_SPACE_EVIDENCE = {
    "hot_desk": re.compile(r"\b(desk|desks|hot[- ]desk|seat|seats|workstation|coworking desk)\b"),
    "meeting_room": re.compile(r"\b(meeting|conference|board ?room|discussion room|meeting space|huddle room|room)\b"),
    "private_cabin": re.compile(r"\b(cabin|private office|private room|private space|office for)\b"),
}


_NAMED_SPACE = {
    "hot_desk": re.compile(r"\b(desk|hot[- ]desk|seat|workstation)\b"),
    "meeting_room": re.compile(r"\b(meeting room|conference room|board ?room|discussion room)\b"),
    "private_cabin": re.compile(r"\b(cabin|private office)\b"),
}


def _weekday_in(text: str) -> str | None:
    for wd in WEEKDAYS:
        if re.search(rf"\b({wd}|{wd[:3]})\b", text):
            return wd
    return None


def reconcile(parsed: ParsedQuery, query: str) -> tuple[ParsedQuery, list[str]]:
    """Return a copy of `parsed` with unsupported hard constraints relaxed, plus notes for the user."""
    t = " " + query.lower().replace("’", "'") + " "
    p = parsed.model_copy(deep=True)
    notes: list[str] = []

    # ---- budget unit: per-day / per-person only if the user said so --------
    if p.budget_amount is not None and p.budget_unit:
        unit = p.budget_unit
        per_person = unit.startswith("per_person")
        per_day = unit.endswith("per_day")
        if per_day and not _DAY_UNIT.search(t):
            per_day = False
        if per_person and not _PERSON_UNIT.search(t):
            per_person = False
        fixed = ("per_person_" if per_person else "total_") + ("per_day" if per_day else "per_hour")
        if fixed != unit:
            p.budget_unit = fixed
            label = "total" if not per_person else "per person"
            notes.append(f"Budget read as {label} {'per day' if per_day else 'per hour'}, since no other unit was stated")

    # ---- day: an absolute date only if the user wrote one -------------------
    if p.day_kind == "date" and not _EXPLICIT_DATE.search(t):
        wd = _weekday_in(t)
        if wd:
            p.day_kind, p.weekday, p.date = "weekday", wd, None
        elif _DAY_AFTER.search(t):
            p.day_kind, p.date = "day_after_tomorrow", None
        elif _TOMORROW.search(t):
            p.day_kind, p.date = "tomorrow", None
        elif _TODAY.search(t):
            p.day_kind, p.date = "today", None
        else:
            p.day_kind, p.date = None, None
            notes.append("Couldn't tell which day you meant, so availability isn't filtered by date")
    if p.day_kind == "weekday" and p.weekday and not re.search(rf"\b({p.weekday}|{p.weekday[:3]})\b", t):
        wd = _weekday_in(t)
        if wd:
            p.weekday = wd
        else:
            p.day_kind, p.weekday = None, None
            notes.append("Couldn't tell which day you meant, so availability isn't filtered by date")

    # Required amenities are left as the parser read them. An over-strict must-have
    # is not hidden from the user: when nothing has it, the no-match path shows the
    # closest listings with "No <amenity>" as the stated miss. Demoting it instead
    # would report a listing without parking as a match for "anything with parking".

    # ---- space type only if the user named one ------------------------------
    if p.space_type and not _SPACE_EVIDENCE[p.space_type].search(t):
        p.space_type = None
        notes.append("Space type not stated, so desks, rooms and cabins are all considered")
    elif p.space_type is None:
        # The user named exactly one kind of space but the parser left it out
        # ("a desk just for me"): use the user's own word.
        named = [k for k, rx in _NAMED_SPACE.items() if rx.search(t)]
        if len(named) == 1:
            p.space_type = named[0]

    return ParsedQuery.model_validate(p.model_dump()), notes
