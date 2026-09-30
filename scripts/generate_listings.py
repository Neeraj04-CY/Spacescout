"""Generate the synthetic listing dataset (app/data/listings.json).

Deterministic (fixed seed) so the dataset, tests and evaluation are reproducible.
All names and addresses are fictional.

Run:  python scripts/generate_listings.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "data" / "listings.json"

SEED = 7

# (area, space_type, capacity)
SPECS = [
    ("bandra", "meeting_room", 4), ("bandra", "meeting_room", 6), ("bandra", "meeting_room", 8),
    ("bandra", "private_cabin", 4), ("bandra", "private_cabin", 2), ("bandra", "hot_desk", 20),
    ("bandra", "meeting_room", 10),
    ("khar", "meeting_room", 4), ("khar", "private_cabin", 3), ("khar", "hot_desk", 15),
    ("bkc", "meeting_room", 6), ("bkc", "meeting_room", 12), ("bkc", "private_cabin", 6),
    ("bkc", "hot_desk", 40), ("bkc", "meeting_room", 20),
    ("andheri", "meeting_room", 4), ("andheri", "meeting_room", 8), ("andheri", "private_cabin", 4),
    ("andheri", "hot_desk", 30), ("andheri", "hot_desk", 25),
    ("lower_parel", "meeting_room", 6), ("lower_parel", "meeting_room", 10),
    ("lower_parel", "private_cabin", 5), ("lower_parel", "hot_desk", 35),
    ("powai", "meeting_room", 6), ("powai", "private_cabin", 4), ("powai", "hot_desk", 20),
    ("powai", "meeting_room", 16),
    ("fort", "meeting_room", 4), ("fort", "meeting_room", 8), ("fort", "hot_desk", 18),
    ("malad", "meeting_room", 6), ("malad", "hot_desk", 30), ("malad", "private_cabin", 8),
    ("goregaon", "meeting_room", 10), ("goregaon", "hot_desk", 25),
    ("vashi", "meeting_room", 8), ("vashi", "hot_desk", 20), ("vashi", "private_cabin", 4),
    ("vashi", "meeting_room", 30),
]

AREA_PRICE_MULT = {
    "bkc": 1.35, "lower_parel": 1.3, "bandra": 1.25, "fort": 1.2, "khar": 1.15,
    "powai": 1.05, "andheri": 1.0, "malad": 0.85, "goregaon": 0.85, "vashi": 0.75,
}
AREA_NAMES = {
    "bandra": "Bandra", "khar": "Khar", "bkc": "BKC", "andheri": "Andheri", "lower_parel": "Lower Parel",
    "powai": "Powai", "fort": "Fort", "malad": "Malad", "goregaon": "Goregaon", "vashi": "Vashi",
}
STREETS = {
    "bandra": ["Waterfield Lane", "Chapel Row"], "khar": ["16th Cross Road"], "bkc": ["G Block Avenue", "Tower Lane"],
    "andheri": ["Link Plaza Road", "Marol Gate Road"], "lower_parel": ["Mill Compound Road"], "powai": ["Lakeside Crescent"],
    "fort": ["Ledger Street"], "malad": ["Mindpark Road"], "goregaon": ["Studio Colony Road"], "vashi": ["Sector 30 Palm Road"],
}

PREFIXES = ["Kora", "Salt", "Monsoon", "Lantern", "Teak", "Harbour", "Quill", "Arcade", "Ferry", "Kiln",
            "Loom", "Tide", "Ember", "Juniper", "Basalt", "Chowk", "Verandah", "Coir", "Sandstone", "Anchor"]
SUFFIXES = ["Works", "Commons", "House", "Studio", "Loft", "Collective"]

AMENITY_PROBS = {
    "meeting_room": {"whiteboard": .7, "projector": .45, "tv_screen": .5, "video_conferencing": .45, "coffee": .7,
                     "pantry": .4, "parking": .3, "printer": .35, "power_backup": .8, "access_24x7": .15,
                     "wheelchair_accessible": .35, "natural_light": .5, "phone_booth": .15},
    "private_cabin": {"whiteboard": .5, "tv_screen": .35, "video_conferencing": .3, "lockers": .4, "coffee": .75,
                      "pantry": .5, "printer": .5, "power_backup": .85, "access_24x7": .4, "natural_light": .6,
                      "parking": .35, "wheelchair_accessible": .3},
    "hot_desk": {"phone_booth": .6, "coffee": .85, "pantry": .7, "lockers": .6, "standing_desk": .3, "printer": .6,
                 "access_24x7": .35, "natural_light": .5, "power_backup": .85, "parking": .3, "whiteboard": .2,
                 "wheelchair_accessible": .3},
}
NOISE_WEIGHTS = {
    "meeting_room": ([.6, .35, .05]), "private_cabin": ([.7, .3, 0.0]), "hot_desk": ([.15, .45, .4]),
}
WIFI_CHOICES = [40, 50, 75, 100, 150, 200, 300, 500]
WIFI_WEIGHTS = [1, 2, 2, 3, 3, 2, 2, 1]

TYPE_WORD = {"meeting_room": "meeting room", "private_cabin": "private cabin", "hot_desk": "hot-desk floor"}
NOISE_WORD = {"quiet": "Quiet", "moderate": "Moderately busy", "lively": "Lively, social"}

# Hand-tuned overrides so the assignment's example query and the evaluation set
# exercise interesting trade-offs. Documented rather than hidden.
OVERRIDES = {
    "L001": {"noise_level": "quiet", "amenities_add": ["whiteboard"], "wifi_mbps": 300, "rating": 4.6, "review_count": 212,
             "price_per_hour": 1800},
    "L002": {"noise_level": "quiet", "amenities_remove": ["whiteboard"], "wifi_mbps": 200, "rating": 4.8, "review_count": 96,
             "price_per_hour": 2100},
    "L004": {"noise_level": "quiet", "amenities_add": ["whiteboard"], "wifi_mbps": 50, "rating": 4.3, "review_count": 41,
             "price_per_hour": 1500},
    "L003": {"noise_level": "moderate", "amenities_add": ["whiteboard"], "wifi_mbps": 150, "rating": 4.1, "review_count": 58,
             "price_per_hour": 2300},
    "L008": {"rating": 5.0, "review_count": 3},  # few reviews: tests Bayesian trust
    "L012": {"rating": 0.0, "review_count": 0},  # brand-new listing, unrated
}


def main() -> None:
    rng = random.Random(SEED)
    names = [f"{p} {s}" for p in PREFIXES for s in SUFFIXES]
    rng.shuffle(names)
    listings = []
    for i, (area, stype, cap) in enumerate(SPECS, start=1):
        lid = f"L{i:03d}"
        mult = AREA_PRICE_MULT[area]
        if stype == "hot_desk":
            pricing = "per_seat"
            pph = round(rng.randint(110, 200) * mult / 10) * 10
            ppd = round(pph * rng.uniform(5.0, 6.5) / 50) * 50
        elif stype == "meeting_room":
            pricing = "per_room"
            pph = round((250 + 190 * cap) * mult * rng.uniform(0.85, 1.15) / 50) * 50
            ppd = round(pph * rng.uniform(6.0, 7.5) / 100) * 100
        else:
            pricing = "per_room"
            pph = round((300 + 160 * cap) * mult * rng.uniform(0.85, 1.15) / 50) * 50
            ppd = round(pph * rng.uniform(6.0, 7.0) / 100) * 100

        noise = rng.choices(["quiet", "moderate", "lively"], weights=NOISE_WEIGHTS[stype])[0]
        amenities = sorted(a for a, p in AMENITY_PROBS[stype].items() if rng.random() < p)
        wifi = rng.choices(WIFI_CHOICES, weights=WIFI_WEIGHTS)[0]

        if rng.random() < 0.15:
            reviews = rng.randint(1, 8)
        else:
            reviews = rng.randint(15, 420)
        rating = round(rng.uniform(3.5, 4.9), 1)
        instant = rng.random() < 0.7

        if "access_24x7" in amenities:
            open_days, open_t, close_t = list(range(7)), "00:00", "23:59"
        elif stype == "hot_desk":
            open_days, open_t, close_t = list(range(6)), "08:00", "22:00"
        else:
            open_days = rng.choice([list(range(5)), list(range(6)), list(range(7))])
            open_t, close_t = rng.choice([("09:00", "21:00"), ("08:00", "20:00"), ("10:00", "22:00")])

        bookings = []
        if stype != "hot_desk":
            for _ in range(rng.randint(0, 3)):
                start_h = rng.randint(10, 16)
                length = rng.choice([2, 3])
                days = sorted(rng.sample(range(5), rng.randint(1, 3)))
                bookings.append({"weekdays": days, "start": f"{start_h:02d}:00", "end": f"{start_h + length:02d}:00"})

        floor = rng.choice(["2nd", "3rd", "4th", "5th", "7th", "9th"])
        street = rng.choice(STREETS[area])
        rec = {
            "id": lid,
            "name": f"{names[i - 1]} {AREA_NAMES[area]}",
            "space_type": stype,
            "area": area,
            "address": f"{floor} floor, {rng.randint(3, 88)} {street}, {AREA_NAMES[area]}, Mumbai",
            "capacity": cap,
            "pricing": pricing,
            "price_per_hour": int(pph),
            "price_per_day": int(ppd),
            "noise_level": noise,
            "wifi_mbps": wifi,
            "amenities": amenities,
            "rating": rating,
            "review_count": reviews,
            "instant_book": instant,
            "open_days": open_days,
            "open_time": open_t,
            "close_time": close_t,
            "recurring_bookings": bookings,
            "description": f"{NOISE_WORD[noise]} {TYPE_WORD[stype]} for up to {cap} people on the {floor} floor in {AREA_NAMES[area]}.",
        }

        ov = OVERRIDES.get(lid, {})
        for k, v in ov.items():
            if k == "amenities_add":
                rec["amenities"] = sorted(set(rec["amenities"]) | set(v))
            elif k == "amenities_remove":
                rec["amenities"] = [a for a in rec["amenities"] if a not in v]
            else:
                rec[k] = v
        if ov.get("price_per_hour"):
            rec["price_per_day"] = int(round(rec["price_per_hour"] * 6.8 / 100) * 100)
        if "noise_level" in ov:
            rec["description"] = f"{NOISE_WORD[rec['noise_level']]} {TYPE_WORD[stype]} for up to {cap} people on the {floor} floor in {AREA_NAMES[area]}."
        listings.append(rec)

    OUT.write_text(json.dumps(listings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(listings)} listings to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
