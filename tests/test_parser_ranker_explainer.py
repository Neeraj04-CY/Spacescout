from app.core.models import ParsedQuery
from app.data.repository import get_listing, load_listings
from app.services.explainer import FactSheet, check_grounding, template_explanation
from app.services.normalize import normalize
from app.services.parser_rules import parse_rules
from app.services.ranker import MarketStats, bayes_rating
from tests.helpers import NOW


# --------------------------------------------------------------- rules parser
def test_rules_parser_on_assignment_example():
    p = parse_rules("Quiet place for 4 people in Bandra tomorrow afternoon, fast wifi, under ₹600 per person per hour, ideally with a whiteboard")
    assert p.location_text == "bandra"
    assert p.party_size == 4
    assert (p.budget_amount, p.budget_unit) == (600, "per_person_per_hour")
    assert (p.day_kind, p.time_of_day) == ("tomorrow", "afternoon")
    assert p.prefer_quiet and p.prefer_fast_wifi
    assert p.preferred_amenities == ["whiteboard"] and p.required_amenities == []


def test_rules_parser_required_vs_preferred():
    p = parse_rules("meeting room for 8 in BKC, must have a projector, whiteboard would be nice")
    assert p.required_amenities == ["projector"]
    assert p.preferred_amenities == ["whiteboard"]
    assert p.space_type == "meeting_room"


def test_rules_parser_time_range_and_day_budget():
    p = parse_rules("cabin for 2 in Powai on friday from 2 to 5, 5k for the day")
    assert (p.start_time, p.end_time) == ("14:00", "17:00")
    assert (p.day_kind, p.weekday) == ("weekday", "friday")
    assert (p.budget_amount, p.budget_unit) == (5000, "total_per_day")


def test_rules_parser_flags_unsupported_and_off_topic():
    p = parse_rules("desk in Khar with a sea view and a gym")
    assert "sea view" in p.unsupported_requests and "gym" in p.unsupported_requests
    assert parse_rules("write me a poem about cats").off_topic
    assert not parse_rules("somewhere nice to work").off_topic


# -------------------------------------------------------------------- ranker
def test_bayesian_trust_discounts_few_reviews():
    stats = MarketStats.from_listings(list(load_listings()))
    few = get_listing("L008")   # 5.0 from 3 reviews
    many = get_listing("L002")  # 4.8 from 96 reviews
    assert bayes_rating(few, stats) < bayes_rating(many, stats)


# ----------------------------------------------------------------- explainer
SHEET = FactSheet("L001", "Tide Commons Bandra", ["Quiet space", "Has whiteboard", "₹450/person/hr, within your ₹600/person/hr"],
                  ["Host must approve the booking"])


def test_template_is_one_or_two_sentences():
    t = template_explanation(SHEET)
    assert t.count(".") <= 3 and "Trade-off" in t


def test_grounding_accepts_faithful_text():
    assert check_grounding("A quiet room with a whiteboard at ₹450 per person per hour; the host must approve the booking.", SHEET) is None


def test_grounding_rejects_invented_amenity_number_and_noise():
    assert "projector" in check_grounding("Quiet, with a whiteboard and a projector.", SHEET)
    assert "number" in check_grounding("Quiet room at ₹399 per person.", SHEET)
    sheet2 = FactSheet("X", "Y", ["Has whiteboard"], [])
    assert "noise" in check_grounding("A peaceful room with a whiteboard.", sheet2)


def test_parsed_query_rejects_bad_values():
    for bad in ({"party_size": -3}, {"start_time": "25:00"}, {"date": "tomorrow"}, {"weekday": "funday"}):
        try:
            ParsedQuery.model_validate(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_normalize_keeps_unsupported_visible():
    q = normalize(ParsedQuery(preferred_amenities=["whiteboard", "swimming pool"]), NOW)
    assert q.preferred_amenities == ["whiteboard"]
    assert q.unsupported_requests == ["swimming pool"]
