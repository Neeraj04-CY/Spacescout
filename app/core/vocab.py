"""Controlled vocabularies shared by the parser, matcher and explainer.

Everything the system is allowed to say about a listing is expressed in these
vocabularies. The LLM parser must map user language onto them, and anything it
cannot map is reported back to the user as "unsupported" instead of being
silently dropped or invented.
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------- #
# Amenities
# --------------------------------------------------------------------------- #
# key -> (display label, synonyms used by the rule-based parser and by the
# grounding validator that checks LLM-written explanations)
AMENITIES: dict[str, tuple[str, list[str]]] = {
    "whiteboard": ("Whiteboard", ["whiteboard", "white board", "writing wall"]),
    "projector": ("Projector", ["projector"]),
    "tv_screen": ("TV / display screen", ["tv screen", "display screen", "tv", "big screen", "monitor for presenting"]),
    "video_conferencing": ("Video-conferencing setup", ["video conferencing", "video-conferencing", "vc setup", "conference camera", "zoom room"]),
    "phone_booth": ("Phone booth", ["phone booth", "call booth", "phone booths", "call booths"]),
    "coffee": ("Free coffee / tea", ["coffee", "tea", "chai"]),
    "pantry": ("Pantry", ["pantry", "kitchen"]),
    "parking": ("Parking", ["parking", "car park"]),
    "printer": ("Printer", ["printer", "printing"]),
    "power_backup": ("Power backup", ["power backup", "generator", "ups", "inverter"]),
    "access_24x7": ("24x7 access", ["24x7", "24/7", "24 x 7", "round the clock", "late night access"]),
    "wheelchair_accessible": ("Wheelchair accessible", ["wheelchair", "accessible", "step-free"]),
    "standing_desk": ("Standing desks", ["standing desk", "standing desks", "sit-stand"]),
    "lockers": ("Lockers", ["locker", "lockers"]),
    "natural_light": ("Natural light", ["natural light", "sunlight", "daylight", "windows"]),
}

AMENITY_KEYS: list[str] = list(AMENITIES)


def amenity_label(key: str) -> str:
    return AMENITIES[key][0]


def amenity_phrase(key: str) -> str:
    """Label for use mid-sentence: lower-cased unless it starts with an acronym (TV, 24x7)."""
    label = AMENITIES[key][0]
    if label[:2].isupper() or label[0].isdigit():
        return label
    return label[0].lower() + label[1:]


# --------------------------------------------------------------------------- #
# Space types and noise levels
# --------------------------------------------------------------------------- #
SPACE_TYPES: dict[str, str] = {
    "hot_desk": "Hot desk",
    "meeting_room": "Meeting room",
    "private_cabin": "Private cabin",
}

NOISE_LEVELS = ("quiet", "moderate", "lively")

FAST_WIFI_MBPS = 100  # "fast wifi" threshold used for soft-preference scoring

# --------------------------------------------------------------------------- #
# Locations (Mumbai)
# --------------------------------------------------------------------------- #
# Areas we have listings in, plus a few known areas with no listings, so that
# "Juhu" resolves to a real place and we can suggest the nearest areas instead
# of pretending we have inventory there.
AREAS: dict[str, dict] = {
    "bandra": {"name": "Bandra", "lat": 19.0596, "lon": 72.8295, "aliases": ["bandra", "bandra west", "bandra w", "bandra east", "bandra e"]},
    "khar": {"name": "Khar", "lat": 19.0728, "lon": 72.8360, "aliases": ["khar", "khar west"]},
    "bkc": {"name": "BKC", "lat": 19.0660, "lon": 72.8656, "aliases": ["bkc", "bandra kurla complex", "bandra-kurla complex"]},
    "andheri": {"name": "Andheri", "lat": 19.1197, "lon": 72.8468, "aliases": ["andheri", "andheri east", "andheri west", "andheri e", "andheri w", "marol"]},
    "lower_parel": {"name": "Lower Parel", "lat": 18.9966, "lon": 72.8258, "aliases": ["lower parel", "parel", "kamala mills"]},
    "powai": {"name": "Powai", "lat": 19.1176, "lon": 72.9060, "aliases": ["powai", "hiranandani"]},
    "fort": {"name": "Fort", "lat": 18.9338, "lon": 72.8354, "aliases": ["fort", "colaba", "churchgate", "nariman point", "south mumbai", "sobo"]},
    "malad": {"name": "Malad", "lat": 19.1864, "lon": 72.8485, "aliases": ["malad", "malad west", "mindspace"]},
    "goregaon": {"name": "Goregaon", "lat": 19.1663, "lon": 72.8526, "aliases": ["goregaon", "goregaon east", "goregaon west"]},
    "vashi": {"name": "Vashi", "lat": 19.0771, "lon": 72.9986, "aliases": ["vashi", "navi mumbai"]},
    # Known areas without inventory (used for honest "we have nothing there" answers)
    "juhu": {"name": "Juhu", "lat": 19.1075, "lon": 72.8263, "aliases": ["juhu"]},
    "santacruz": {"name": "Santacruz", "lat": 19.0825, "lon": 72.8410, "aliases": ["santacruz", "santa cruz"]},
    "worli": {"name": "Worli", "lat": 19.0176, "lon": 72.8162, "aliases": ["worli"]},
    "dadar": {"name": "Dadar", "lat": 19.0178, "lon": 72.8478, "aliases": ["dadar"]},
    "chembur": {"name": "Chembur", "lat": 19.0522, "lon": 72.9005, "aliases": ["chembur"]},
    "mulund": {"name": "Mulund", "lat": 19.1726, "lon": 72.9425, "aliases": ["mulund", "nahur"]},
}

# Cities we explicitly know we do NOT serve (clearer message than "unknown place").
OTHER_CITIES = {"pune", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad", "chennai", "kolkata", "nashik", "gurgaon", "gurugram", "noida", "ahmedabad", "goa"}

NEARBY_KM = 5.0  # areas within this distance count as "nearby"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def area_distance_km(a: str, b: str) -> float:
    return haversine_km(AREAS[a]["lat"], AREAS[a]["lon"], AREAS[b]["lat"], AREAS[b]["lon"])


# --------------------------------------------------------------------------- #
# Time of day
# --------------------------------------------------------------------------- #
TIME_OF_DAY_WINDOWS: dict[str, tuple[str, str]] = {
    "morning": ("08:00", "12:00"),
    "afternoon": ("12:00", "17:00"),
    "evening": ("17:00", "21:00"),
    "full_day": ("09:00", "18:00"),
}

DEFAULT_DURATION_HOURS = 2.0
WORKING_HOURS = ("09:00", "19:00")  # searched when a day is given without a time
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
