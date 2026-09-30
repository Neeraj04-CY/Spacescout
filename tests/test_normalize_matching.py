import datetime as dt

from app.core.models import ParsedQuery
from app.data.repository import get_listing
from app.services.availability import check_availability, free_blocks
from app.services.matcher import cost_in_unit, evaluate
from app.services.normalize import normalize, resolve_location
from tests.helpers import NOW

TUE = dt.date(2026, 10, 6)


# ----------------------------------------------------------------- normalize
def test_tomorrow_afternoon_resolves_to_absolute_window():
    q = normalize(ParsedQuery(day_kind="tomorrow", time_of_day="afternoon"), NOW)
    assert q.date == TUE
    assert (q.window_start, q.window_end, q.duration_min) == (12 * 60, 17 * 60, 120)
    assert any("2-hour" in a for a in q.assumptions)


def test_weekday_resolves_to_next_occurrence():
    q = normalize(ParsedQuery(day_kind="weekday", weekday="friday"), NOW)
    assert q.date == dt.date(2026, 10, 9)


def test_past_date_is_ignored_with_assumption():
    q = normalize(ParsedQuery(day_kind="date", date="2026-01-01"), NOW)
    assert q.date is None
    assert any("past" in a for a in q.assumptions)


def test_explicit_range_sets_duration():
    q = normalize(ParsedQuery(day_kind="today", start_time="14:00", end_time="17:00"), NOW)
    assert (q.window_start, q.window_end, q.duration_min) == (840, 1020, 180)


def test_today_window_clipped_to_now():
    q = normalize(ParsedQuery(day_kind="today", time_of_day="morning"), NOW)
    assert q.window_start == 10 * 60  # 10:00 is "now"


def test_location_aliases_typos_and_near():
    assert resolve_location("Bandra Kurla Complex")[0] == ["bkc"]
    assert resolve_location("Bandraa")[0] == ["bandra"]
    areas, mode, _, _ = resolve_location("near Bandra")
    assert mode == "near" and "khar" in areas and "bandra" in areas
    assert resolve_location("Andheri or Powai")[0] == ["andheri", "powai"]


def test_other_city_and_unknown_place():
    areas, _, unresolved, outside = resolve_location("Pune")
    assert areas == [] and outside and unresolved == "pune"
    areas, _, unresolved, outside = resolve_location("Atlantis")
    assert areas == [] and not outside and unresolved == "atlantis"
    assert resolve_location("anywhere in Mumbai") == ([], None, None, False)


def test_budget_unit_assumed_when_missing():
    q = normalize(ParsedQuery(budget_amount=500), NOW)
    assert q.budget_unit == "total_per_hour"
    assert any("Budget unit" in a for a in q.assumptions)


# -------------------------------------------------------------- availability
def test_free_blocks_subtract_recurring_bookings():
    l1 = get_listing("L001")  # 24x7, booked Mon/Tue/Fri 11-13
    assert free_blocks(l1, TUE) == [(0, 660), (780, 1440)]


def test_booked_window_reports_alternative_slot():
    l = get_listing("L002")  # booked Mon/Thu/Fri 10:00-13:00, open 08:00-20:00
    r = check_availability(l, dt.date(2026, 10, 12), 10 * 60, 13 * 60, 120)
    assert not r.ok and r.reason == "Already booked during your time"
    assert r.other_slots


def test_closed_day():
    l = get_listing("L002")  # Mon-Fri only
    r = check_availability(l, dt.date(2026, 10, 11), 600, 1000, 60)  # Sunday
    assert not r.ok and r.reason.startswith("Closed")


# ------------------------------------------------------------------- matcher
def test_per_person_cost_for_rooms_vs_seats():
    room, desk = get_listing("L001"), get_listing("L006")
    assert cost_in_unit(room, "per_person_per_hour", 4) == 450
    assert cost_in_unit(desk, "per_person_per_hour", 4) == desk.price_per_hour
    assert cost_in_unit(desk, "total_per_hour", 4) == desk.price_per_hour * 4


def test_violations_are_specific():
    q = normalize(ParsedQuery(location_text="Bandra", party_size=12, budget_amount=100, budget_unit="per_person_per_hour"), NOW)
    ev = evaluate(get_listing("L001"), q)
    codes = {v.code for v in ev.violations}
    assert codes == {"capacity", "budget"}
    assert not ev.is_exact
