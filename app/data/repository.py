"""Listing repository. JSON file for the prototype; swap for Postgres at scale."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.core.models import Listing

DATA_FILE = Path(__file__).with_name("listings.json")


@lru_cache
def load_listings(path: str | None = None) -> tuple[Listing, ...]:
    raw = json.loads(Path(path or DATA_FILE).read_text(encoding="utf-8"))
    listings = tuple(Listing.model_validate(r) for r in raw)
    ids = [l.id for l in listings]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate listing ids in dataset")
    return listings


def get_listing(listing_id: str) -> Listing | None:
    return next((l for l in load_listings() if l.id == listing_id), None)
