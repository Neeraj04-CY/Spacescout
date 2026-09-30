"""Regression tests for problems found while using the app live with the LLM parser."""

import datetime as dt
from zoneinfo import ZoneInfo

from app.core.models import ParsedQuery
from app.core.vocab import amenity_phrase
from app.data.repository import get_listing
from app.services.matcher import evaluate
from app.services.normalize import normalize
from app.services.reconcile import reconcile

IST = ZoneInfo("Asia/Kolkata")
WED_NIGHT = dt.datetime(2026, 9, 30, 22, 26, tzinfo=IST)


# ------------------------------------------------------------------ reconcile
def test_invented_date_for_weekday_is_replaced():
    # Live: "on sunday" came back as day_kind=date, date=2026-10-01 (a Thursday).
    p, notes = reconcile(ParsedQuery(day_kind="date", date="2026-10-01"), "parking in Vashi on sunday for 3 of us")
    assert (p.day_kind, p.weekday, p.date) == ("weekday", "sunday", None)


def test_explicit_date_is_kept():
    p, _ = reconcile(ParsedQuery(day_kind="date", date="2026-10-12"), "room for 4 on 12 Oct")
    assert p.day_kind == "date" and p.date == "2026-10-12"


def test_day_budget_unit_needs_evidence():
    # Live: "max 1000 rupees total" and "1500 tak ka budget" came back as total_per_day.
    p, notes = reconcile(ParsedQuery(budget_amount=1000, budget_unit="total_per_day"), "max 1000 rupees total")
    assert p.budget_unit == "total_per_hour" and notes
    p, _ = reconcile(ParsedQuery(budget_amount=5000, budget_unit="total_per_day"), "12 people day after tomorrow, 5k for the day total")
    assert p.budget_unit == "total_per_day"


def test_per_person_unit_needs_evidence():
    # LLM eval S6: "under ₹1,200 an hour" for 2 people was read as per person, doubling the real budget.
    p, _ = reconcile(ParsedQuery(budget_amount=1200, budget_unit="per_person_per_hour", party_size=2), "Quiet cabin for 2, under ₹1,200 an hour")
    assert p.budget_unit == "total_per_hour"
    p, _ = reconcile(ParsedQuery(budget_amount=600, budget_unit="per_person_per_hour"), "under ₹600 per person per hour")
    assert p.budget_unit == "per_person_per_hour"


def test_required_amenity_is_kept_so_misses_are_explicit():
    # Live: demoting "anything with parking" to a preference reported a no-parking room as a match.
    p, notes = reconcile(ParsedQuery(required_amenities=["parking"]), "anything with parking in Vashi on sunday")
    assert p.required_amenities == ["parking"] and not notes


def test_inferred_space_type_is_dropped():
    p, notes = reconcile(ParsedQuery(space_type="meeting_room"), "spot for a client pitch in BKC for 6")
    assert p.space_type is None and notes
    p, _ = reconcile(ParsedQuery(space_type="hot_desk"), "a desk just for me in Vashi")
    assert p.space_type == "hot_desk"


# ------------------------------------------------------------------ time handling
def test_passed_window_today_moves_to_tomorrow():
    # Live: "this morning" at 22:26 produced a 22:30-12:00 window and nonsense reasons.
    q = normalize(ParsedQuery(day_kind="today", time_of_day="morning"), WED_NIGHT)
    assert q.date == dt.date(2026, 10, 1)
    assert (q.window_start, q.window_end) == (8 * 60, 12 * 60)
    assert any("already passed" in a for a in q.assumptions)


def test_day_without_time_searches_working_hours():
    # Live: 24x7 spaces were offered at 00:00-02:00.
    q = normalize(ParsedQuery(day_kind="tomorrow"), WED_NIGHT)
    assert (q.window_start, q.window_end) == (9 * 60, 19 * 60)


def test_slots_today_never_start_in_the_past():
    now = dt.datetime(2026, 10, 5, 15, 10, tzinfo=IST)  # Monday afternoon
    q = normalize(ParsedQuery(day_kind="today", time_of_day="afternoon", duration_hours=1), now)
    ev = evaluate(get_listing("L001"), q)
    assert ev.slot_label and "15:30" in ev.slot_label


def test_amenity_phrase_keeps_acronyms():
    assert amenity_phrase("tv_screen").startswith("TV")
    assert amenity_phrase("whiteboard") == "whiteboard"
    assert amenity_phrase("access_24x7") == "24x7 access"
