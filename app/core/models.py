"""Pydantic models for listings, parsed queries and search responses."""

from __future__ import annotations

import re
import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.vocab import AMENITY_KEYS, AREAS, NOISE_LEVELS, SPACE_TYPES, WEEKDAYS

SpaceType = Literal["hot_desk", "meeting_room", "private_cabin"]
BudgetUnit = Literal["per_person_per_hour", "total_per_hour", "per_person_per_day", "total_per_day"]
DayKind = Literal["today", "tomorrow", "day_after_tomorrow", "weekday", "date"]
TimeOfDay = Literal["morning", "afternoon", "evening", "full_day"]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# --------------------------------------------------------------------------- #
# Listing data
# --------------------------------------------------------------------------- #
class RecurringBooking(BaseModel):
    weekdays: list[int] = Field(description="0=Monday .. 6=Sunday")
    start: str
    end: str


class Listing(BaseModel):
    id: str
    name: str
    space_type: SpaceType
    area: str
    address: str
    capacity: int = Field(ge=1)
    pricing: Literal["per_seat", "per_room"]
    price_per_hour: int = Field(gt=0, description="INR; per seat for hot desks, per room otherwise")
    price_per_day: int = Field(gt=0)
    noise_level: Literal["quiet", "moderate", "lively"]
    wifi_mbps: int = Field(ge=0)
    amenities: list[str]
    rating: float = Field(ge=0, le=5)
    review_count: int = Field(ge=0)
    instant_book: bool
    open_days: list[int]
    open_time: str
    close_time: str
    recurring_bookings: list[RecurringBooking] = []
    description: str

    @field_validator("area")
    @classmethod
    def _known_area(cls, v: str) -> str:
        if v not in AREAS:
            raise ValueError(f"unknown area {v!r}")
        return v

    @field_validator("amenities")
    @classmethod
    def _known_amenities(cls, v: list[str]) -> list[str]:
        unknown = [a for a in v if a not in AMENITY_KEYS]
        if unknown:
            raise ValueError(f"unknown amenities {unknown}")
        return v


# --------------------------------------------------------------------------- #
# Parser output (LLM or rules) - "what the user said", not yet resolved
# --------------------------------------------------------------------------- #
class ParsedQuery(BaseModel):
    """Structured intent extracted from free text.

    Deliberately contains no computed values (no absolute dates, no per-person
    arithmetic): the LLM only maps language to fields, code does the maths.
    """

    location_text: Optional[str] = None
    party_size: Optional[int] = None
    space_type: Optional[SpaceType] = None
    budget_amount: Optional[float] = None
    budget_unit: Optional[BudgetUnit] = None
    day_kind: Optional[DayKind] = None
    weekday: Optional[str] = None
    date: Optional[str] = None
    time_of_day: Optional[TimeOfDay] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_hours: Optional[float] = None
    required_amenities: list[str] = []
    preferred_amenities: list[str] = []
    prefer_quiet: bool = False
    prefer_fast_wifi: bool = False
    min_rating: Optional[float] = None
    unsupported_requests: list[str] = []
    off_topic: bool = False

    @field_validator("party_size")
    @classmethod
    def _party(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if v < 1 or v > 1000:
            raise ValueError("party_size out of range")
        return v

    @field_validator("budget_amount")
    @classmethod
    def _budget(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if v < 0:
            raise ValueError("budget must be >= 0")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def _hhmm(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not _HHMM.match(v):
            raise ValueError(f"time must be HH:MM, got {v!r}")
        return v

    @field_validator("weekday")
    @classmethod
    def _weekday(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in WEEKDAYS:
            raise ValueError(f"unknown weekday {v!r}")
        return v

    @field_validator("date")
    @classmethod
    def _date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        dt.date.fromisoformat(v)  # raises on bad input
        return v

    @field_validator("duration_hours")
    @classmethod
    def _duration(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if v <= 0 or v > 24:
            raise ValueError("duration_hours out of range")
        return v

    @field_validator("min_rating")
    @classmethod
    def _rating(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return max(0.0, min(5.0, v))

    @model_validator(mode="after")
    def _split_unknown_amenities(self) -> "ParsedQuery":
        """Anything outside the amenity vocabulary becomes an explicit 'unsupported'
        request, so it is shown to the user rather than silently ignored."""
        unsupported = list(self.unsupported_requests)
        for field in ("required_amenities", "preferred_amenities"):
            kept = []
            for a in getattr(self, field):
                key = a.strip().lower().replace(" ", "_")
                if key in AMENITY_KEYS:
                    if key not in kept:
                        kept.append(key)
                elif a not in unsupported:
                    unsupported.append(a)
            setattr(self, field, kept)
        # a required amenity is not also "preferred"
        self.preferred_amenities = [a for a in self.preferred_amenities if a not in self.required_amenities]
        self.unsupported_requests = unsupported
        return self


# --------------------------------------------------------------------------- #
# Resolved query - after deterministic normalisation
# --------------------------------------------------------------------------- #
class ResolvedQuery(BaseModel):
    areas: list[str] = []
    location_mode: Optional[Literal["exact", "near"]] = None
    location_unresolved: Optional[str] = None
    location_outside_city: bool = False
    party_size: Optional[int] = None
    space_type: Optional[SpaceType] = None
    budget_amount: Optional[float] = None
    budget_unit: Optional[BudgetUnit] = None
    date: Optional[dt.date] = None
    window_start: Optional[int] = None  # minutes from midnight
    window_end: Optional[int] = None
    duration_min: Optional[int] = None
    required_amenities: list[str] = []
    preferred_amenities: list[str] = []
    prefer_quiet: bool = False
    prefer_fast_wifi: bool = False
    min_rating: Optional[float] = None
    unsupported_requests: list[str] = []
    off_topic: bool = False
    assumptions: list[str] = []

    @property
    def has_hard_constraints(self) -> bool:
        return bool(
            self.areas
            or self.party_size
            or self.space_type
            or self.budget_amount is not None
            or self.date
            or self.required_amenities
        )

    @property
    def has_soft_preferences(self) -> bool:
        return bool(self.preferred_amenities or self.prefer_quiet or self.prefer_fast_wifi or self.min_rating)


# --------------------------------------------------------------------------- #
# API request / response
# --------------------------------------------------------------------------- #
class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    reference_time: Optional[str] = Field(
        default=None, description="ISO datetime used as 'now' (for reproducible evaluation). Defaults to current time."
    )
    parser: Literal["auto", "llm", "rules"] = "auto"
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be blank")
        return v

    @field_validator("reference_time")
    @classmethod
    def _iso(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            dt.datetime.fromisoformat(v)  # raises ValueError -> 422
        return v


class Chip(BaseModel):
    kind: Literal["hard", "soft", "assumption", "unsupported"]
    label: str


class ScoreBreakdown(BaseModel):
    fit: Optional[float]
    trust: float
    value: float
    conversion: float
    total: float


class ResultItem(BaseModel):
    listing: Listing
    match: Literal["exact", "alternative", "suggestion"]
    score: ScoreBreakdown
    price_per_person_hour: Optional[float]
    cost_in_budget_unit: Optional[float]
    why: list[str]
    tradeoffs: list[str]
    violations: list[str]
    available_slot: Optional[str]
    explanation: str
    explanation_source: str


class SearchResponse(BaseModel):
    request_id: str
    status: Literal["ok", "partial", "no_exact_match", "needs_clarification"]
    message: Optional[str] = None
    clarifying_question: Optional[str] = None
    suggestions: list[str] = []
    interpretation: list[Chip]
    parsed: ParsedQuery
    results: list[ResultItem]
    total_exact_matches: int
    trace: dict


__all__ = [
    "Listing",
    "ParsedQuery",
    "ResolvedQuery",
    "SearchRequest",
    "SearchResponse",
    "ResultItem",
    "ScoreBreakdown",
    "Chip",
    "SPACE_TYPES",
    "NOISE_LEVELS",
]
