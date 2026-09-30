"""Rule-based fallback parser.

Used when no LLM key is configured, when the LLM call fails, or when forced via
`parser="rules"`. It handles common English phrasings only; the evaluation
compares it with the LLM parser to show where the LLM actually adds value.
"""

from __future__ import annotations

import re

from app.core.models import ParsedQuery
from app.core.vocab import AMENITIES, AREAS, OTHER_CITIES, WEEKDAYS

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
}
_NUM = r"(\d+|" + "|".join(NUM_WORDS) + r")"
_PEOPLE = r"(?:people|persons|person|pax|ppl|members|folks|of us|guys|colleagues|attendees|participants|seats|heads)"

_REQUIRED_CUES = re.compile(r"\b(must|need|needs|needed|required|require|mandatory|has to have|essential)\b")
_PREFERRED_CUES = re.compile(r"\b(ideally|preferably|prefer|nice to have|if possible|bonus|would be nice)\b")

UNSUPPORTED_TERMS = [
    "sea view", "pool", "swimming pool", "gym", "pet friendly", "pet-friendly", "pets", "rooftop", "terrace", "shower",
    "sleeping pod", "nap", "bar", "alcohol", "smoking", "sauna", "massage", "helipad", "free lunch", "free food",
]

_WORKSPACE_WORDS = re.compile(
    r"\b(desk|room|cabin|office|space|place|seat|work|working|meeting|cowork|co-work|study|call|team|people|book|spot)\b"
)


def _to_int(tok: str) -> int:
    return int(tok) if tok.isdigit() else NUM_WORDS[tok]


def _amount(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _hhmm(h: int, m: int, ampm: str | None, default_pm: bool) -> str | None:
    if ampm:
        ampm = ampm.lower()
        if ampm.startswith("p") and h < 12:
            h += 12
        if ampm.startswith("a") and h == 12:
            h = 0
    elif default_pm and 1 <= h <= 7:
        h += 12  # "from 2 to 5" in a workspace context means afternoon
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def parse_rules(query: str) -> ParsedQuery:
    t = " " + query.lower().replace("’", "'") + " "
    out: dict = {"required_amenities": [], "preferred_amenities": [], "unsupported_requests": []}

    # ---- party size --------------------------------------------------------
    m = (
        re.search(rf"\b{_NUM}\s*{_PEOPLE}\b", t)
        or re.search(rf"\b(?:team|group|party) of\s*{_NUM}\b", t)
        or re.search(rf"\bfor\s+{_NUM}\b(?!\s*(?:hours?|hrs?|h\b|pm|am|k\b|:))", t)
    )
    if m:
        out["party_size"] = _to_int(m.group(1))
    elif re.search(r"\b(just me|only me|myself|solo|for me|for one)\b", t):
        out["party_size"] = 1
    else:
        m = re.search(r"\bme and (\d+|" + "|".join(NUM_WORDS) + r") (?:friends|colleagues|others|people)\b", t)
        if m:
            out["party_size"] = _to_int(m.group(1)) + 1

    # ---- budget ------------------------------------------------------------
    m = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(k)?\b", t) or re.search(
        r"\b([\d,]+(?:\.\d+)?)\s*(k)?\s*(?:rupees|rs\b|inr\b|/-)", t
    ) or re.search(r"\b(?:under|below|less than|within|max|upto|up to|budget(?: of| is)?)\s*([\d,]+)\s*(k)?\b(?!\s*(?:people|pm|am|hours?))", t
    ) or re.search(r"\b(\d+(?:\.\d+)?)\s*(k)\b", t)
    if m:
        out["budget_amount"] = _amount(m.group(1), m.group(2))
        tail = t[m.end(): m.end() + 40]
        per_person = bool(re.search(r"(per person|per head|/person|/head|each|pp\b|per pax|a head)", tail))
        per_day = bool(re.search(r"(per day|/day|a day|for the day|daily|full day|whole day)", tail))
        out["budget_unit"] = ("per_person_" if per_person else "total_") + ("per_day" if per_day else "per_hour")
    elif re.search(r"\bfree\b(?! (?:wifi|coffee|tea|parking|slot|time))", t) and not re.search(r"\bfree (?:on|at|from|between)\b", t):
        out["budget_amount"] = 0.0
        out["budget_unit"] = "total_per_hour"

    # ---- day ----------------------------------------------------------------
    if re.search(r"\bday after tomorrow\b", t):
        out["day_kind"] = "day_after_tomorrow"
    elif re.search(r"\b(tomorrow|tmrw|tmr|tomorow)\b", t):
        out["day_kind"] = "tomorrow"
    elif re.search(r"\b(today|tonight|right now|now)\b", t):
        out["day_kind"] = "today"
    else:
        for wd in WEEKDAYS:
            if re.search(rf"\b{wd}\b|\b{wd[:3]}\b", t):
                out["day_kind"], out["weekday"] = "weekday", wd
                break

    # ---- time ---------------------------------------------------------------
    m = re.search(
        r"\b(?:from\s+|between\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|to|and|till|until)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t
    ) or re.search(r"\b(?:from|between)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|to|and|till|until)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m:
        end_ampm = m.group(6)
        s = _hhmm(int(m.group(1)), int(m.group(2) or 0), m.group(3) or end_ampm, default_pm=True)
        e = _hhmm(int(m.group(4)), int(m.group(5) or 0), end_ampm, default_pm=True)
        if s and e:
            out["start_time"], out["end_time"] = s, e
    else:
        m = re.search(r"\b(?:at|from|around)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t) or re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
        if m and (m.group(3) or m.group(2)):
            s = _hhmm(int(m.group(1)), int(m.group(2) or 0), m.group(3), default_pm=True)
            if s:
                out["start_time"] = s
    if "start_time" not in out:
        if re.search(r"\b(all day|full day|whole day|entire day)\b", t):
            out["time_of_day"] = "full_day"
        elif re.search(r"\b(morning|forenoon)\b", t):
            out["time_of_day"] = "morning"
        elif re.search(r"\b(afternoon|post lunch|after lunch)\b", t):
            out["time_of_day"] = "afternoon"
        elif re.search(r"\b(evening|tonight|after work)\b", t):
            out["time_of_day"] = "evening"
    m = re.search(r"\bfor\s+(\d+(?:\.\d+)?|an?|one|two|three|four|five|six)\s*(?:hours?|hrs?|h)\b", t)
    if m:
        tok = m.group(1)
        out["duration_hours"] = 1.0 if tok in ("a", "an") else float(tok) if tok[0].isdigit() else float(NUM_WORDS[tok])

    # ---- space type -------------------------------------------------------
    if re.search(r"\b(meeting room|conference|boardroom|board room|discussion room|meeting space|room for a meeting)\b", t):
        out["space_type"] = "meeting_room"
    elif re.search(r"\b(cabin|private office|private room|private space)\b", t):
        out["space_type"] = "private_cabin"
    elif re.search(r"\b(hot desk|hot-desk|desk|seat|workstation)\b", t):
        out["space_type"] = "hot_desk"

    # ---- location -----------------------------------------------------------
    loc_hits = []
    for key, meta in AREAS.items():
        for alias in sorted(meta["aliases"], key=len, reverse=True):
            mm = re.search(rf"\b{re.escape(alias)}\b", t)
            if mm:
                loc_hits.append((mm.start(), alias))
                break
    if loc_hits:
        loc_hits.sort()
        prefix = "near " if re.search(r"\b(near|around|close to|next to)\b", t) else ""
        out["location_text"] = prefix + " or ".join(a for _, a in loc_hits)
    else:
        for city in OTHER_CITIES:
            if re.search(rf"\b{re.escape(city)}\b", t):
                out["location_text"] = city
                break
        else:
            m = re.search(r"\b(?:in|at|near|around)\s+([a-z]+(?:\s[a-z]+)?)\b", t)
            stop = {"the", "a", "an", "morning", "afternoon", "evening", "mumbai", "bombay", "budget", "total", "person",
                    "cash", "advance", "total", "range", "time", "office", "town", "city", "area", "my", "our", "it"}
            if m and m.group(1).split()[0] not in stop and not re.match(r"\d", m.group(1)):
                cand = m.group(1).split()[0]
                if cand not in WEEKDAYS and cand not in {"tomorrow", "today"}:
                    out["location_text"] = cand

    # ---- preferences -------------------------------------------------------
    if re.search(r"\b(quiet|silent|calm|peaceful|focus|focused|distraction[- ]free|no noise|not noisy)\b", t):
        out["prefer_quiet"] = True
    if re.search(r"\b(fast|good|strong|high[- ]speed|reliable|great|stable)\s+(wifi|wi-fi|internet|connection)\b", t):
        out["prefer_fast_wifi"] = True
    m = re.search(r"\b([34](?:\.\d)?)\s*\+?\s*(?:stars?|rating|rated)\b", t) or re.search(r"\brated\s+(?:above|over)?\s*([34](?:\.\d)?)", t)
    if m:
        out["min_rating"] = float(m.group(1))

    for key, (label, syns) in AMENITIES.items():
        for syn in syns:
            if len(syn) <= 3 and syn not in ("tv", "ups"):
                continue
            mm = re.search(rf"\b{re.escape(syn)}\b", t)
            if not mm:
                continue
            if key == "wheelchair_accessible" and syn == "accessible" and not re.search(r"wheelchair|disab|step", t):
                break
            if key == "coffee" and re.search(r"\bcoffee (shop|cafe)\b", t):
                break
            # Decide required vs preferred from the clause the amenity appears in.
            cs = max(t.rfind(ch, 0, mm.start()) for ch in ",;.") + 1
            ends = [i for i in (t.find(ch, mm.end()) for ch in ",;.") if i != -1]
            clause = t[cs: min(ends) if ends else len(t)]
            if _REQUIRED_CUES.search(clause) and not _PREFERRED_CUES.search(clause):
                out["required_amenities"].append(key)
            else:
                out["preferred_amenities"].append(key)
            break

    for term in UNSUPPORTED_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", t) and not any(term in u for u in out["unsupported_requests"]):
            out["unsupported_requests"].append(term)

    has_signal = any(
        out.get(k) for k in ("party_size", "budget_amount", "day_kind", "time_of_day", "start_time", "space_type",
                             "location_text", "prefer_quiet", "prefer_fast_wifi", "min_rating")
    ) or out["required_amenities"] or out["preferred_amenities"] or out.get("budget_amount") == 0.0
    out["off_topic"] = not has_signal and not _WORKSPACE_WORDS.search(t)

    return ParsedQuery.model_validate(out)
