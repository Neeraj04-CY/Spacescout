"""Hard-constraint matching.

Every listing is evaluated against every hard constraint. A listing with zero
violations is an exact match. Violations carry a numeric cost so that, when
nothing matches exactly, we can rank the *closest* alternatives by how far they
miss rather than inventing something that fits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.models import Listing, ResolvedQuery
from app.core.vocab import AREAS, SPACE_TYPES, amenity_phrase, area_distance_km
from app.services.availability import check_availability, fmt_slot

UNIT_LABEL = {
    "per_person_per_hour": "/person/hr",
    "total_per_hour": "/hr",
    "per_person_per_day": "/person/day",
    "total_per_day": "/day",
}


def inr(x: float) -> str:
    return f"₹{x:,.0f}"


def cost_in_unit(listing: Listing, unit: str, party: int | None) -> float:
    """Price of the listing expressed in the user's budget unit."""
    n = party or 1
    per_seat = listing.pricing == "per_seat"
    if unit == "per_person_per_hour":
        return listing.price_per_hour if per_seat else listing.price_per_hour / n
    if unit == "total_per_hour":
        return listing.price_per_hour * n if per_seat else listing.price_per_hour
    if unit == "per_person_per_day":
        return listing.price_per_day if per_seat else listing.price_per_day / n
    if unit == "total_per_day":
        return listing.price_per_day * n if per_seat else listing.price_per_day
    raise ValueError(unit)


def per_person_hour(listing: Listing, party: int | None) -> float | None:
    if listing.pricing == "per_seat":
        return float(listing.price_per_hour)
    if party:
        return listing.price_per_hour / party
    return None


@dataclass
class Violation:
    code: str  # location | capacity | space_type | budget | availability | amenity
    cost: float
    message: str


@dataclass
class Evaluation:
    listing: Listing
    violations: list[Violation] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)  # hard constraints met, as facts
    cost_in_budget_unit: float | None = None
    slot_label: str | None = None
    distance_km: float | None = None

    @property
    def is_exact(self) -> bool:
        return not self.violations

    @property
    def violation_cost(self) -> float:
        return sum(v.cost for v in self.violations)


def evaluate(listing: Listing, q: ResolvedQuery, skip: set[str] | None = None) -> Evaluation:
    """Check `listing` against the hard constraints in `q`.

    `skip` lets the relaxation analysis ask "what if this constraint were dropped?".
    """
    skip = skip or set()
    ev = Evaluation(listing=listing)
    area_name = AREAS[listing.area]["name"]

    # Location
    if q.areas and "location" not in skip:
        if listing.area in q.areas:
            ev.satisfied.append(f"In {area_name}")
        else:
            nearest = min(q.areas, key=lambda a: area_distance_km(listing.area, a))
            d = area_distance_km(listing.area, nearest)
            ev.distance_km = d
            ev.violations.append(
                Violation("location", 0.25 + d / 4, f"In {area_name}, {d:.1f} km from {AREAS[nearest]['name']}")
            )

    # Space type
    if q.space_type and "space_type" not in skip:
        if listing.space_type == q.space_type:
            ev.satisfied.append(SPACE_TYPES[listing.space_type])
        else:
            ev.violations.append(
                Violation("space_type", 0.8, f"{SPACE_TYPES[listing.space_type]}, not a {SPACE_TYPES[q.space_type].lower()}")
            )

    # Capacity
    if q.party_size and "capacity" not in skip:
        if listing.capacity >= q.party_size:
            ev.satisfied.append(
                f"{listing.capacity} seats available" if q.party_size == 1 else f"Seats {listing.capacity}, enough for your {q.party_size}"
            )
        else:
            short = q.party_size - listing.capacity
            ev.violations.append(
                Violation("capacity", 1.0 + 2 * short / q.party_size, f"Seats only {listing.capacity}, you need {q.party_size}")
            )

    # Budget
    if q.budget_amount is not None and q.budget_unit:
        c = cost_in_unit(listing, q.budget_unit, q.party_size)
        ev.cost_in_budget_unit = c
        if "budget" not in skip:
            unit = UNIT_LABEL[q.budget_unit]
            if c <= q.budget_amount:
                ev.satisfied.append(f"{inr(c)}{unit}, within your {inr(q.budget_amount)}{unit}")
            else:
                over = c - q.budget_amount
                if q.budget_amount > 0:
                    msg = f"{inr(c)}{unit}: {inr(over)} over your {inr(q.budget_amount)}{unit}"
                    cost = min(3.0, 2.5 * over / q.budget_amount)
                else:
                    msg, cost = f"Costs {inr(c)}{unit}, not free", 3.0
                ev.violations.append(Violation("budget", cost, msg))

    # Availability
    if q.date and q.window_start is not None and q.duration_min and "availability" not in skip:
        av = check_availability(listing, q.date, q.window_start, q.window_end, q.duration_min, q.not_before)
        if av.ok:
            ev.slot_label = fmt_slot(q.date, av.slot)
            ev.satisfied.append(f"Free {ev.slot_label}")
        else:
            msg = av.reason or "Not available"
            if av.other_slots:
                s, e = av.other_slots[0]
                msg += f"; free {fmt_slot(q.date, (s, e))} instead"
            ev.violations.append(Violation("availability", 1.5 if msg.startswith("Closed") else 1.2, msg))

    # Required amenities
    if "amenity" not in skip:
        for a in q.required_amenities:
            if a in listing.amenities:
                ev.satisfied.append(f"Has {amenity_phrase(a)}")
            else:
                ev.violations.append(Violation("amenity", 0.8, f"No {amenity_phrase(a)}"))

    return ev


def evaluate_all(listings: list[Listing], q: ResolvedQuery, skip: set[str] | None = None) -> list[Evaluation]:
    return [evaluate(l, q, skip) for l in listings]


def active_constraints(q: ResolvedQuery) -> list[str]:
    out = []
    if q.areas:
        out.append("location")
    if q.space_type:
        out.append("space_type")
    if q.party_size:
        out.append("capacity")
    if q.budget_amount is not None:
        out.append("budget")
    if q.date and q.window_start is not None:
        out.append("availability")
    if q.required_amenities:
        out.append("amenity")
    return out
