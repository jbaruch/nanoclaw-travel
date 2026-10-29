"""Validate flight-assist-owned EV planner preferences; no access authority.

Literal mappings select a request to the protected Tesla provider. They cannot
grant a vehicle, role, enrollment or operation. Destination energy requires an
explicit source reference; lodging names and multi-day stays supply none.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import datetime

FIELDS = frozenset(
    {
        "schema_version",
        "stop_minutes",
        "reserve_percent",
        "cold_allowance_percent",
        "highway_allowance_percent",
        "default_vehicle",
        "event_vehicles",
        "person_vehicles",
        "event_drivers",
        "destination_charges",
    }
)


def text(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or value != value.strip()
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)
    ):
        raise ValueError("EV planning needs bounded nonempty literal identifiers.")
    return value


def number(value: object, low: float, high: float) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError("EV planning needs finite bounded numeric preferences.")


def mapping(value: object, maximum: int, key_size: int, value_size: int) -> None:
    if not isinstance(value, dict) or len(value) > maximum:
        raise ValueError("EV planning mapping is missing or too large.")
    for key, item in value.items():
        text(key, key_size)
        text(item, value_size)


def validate_ev_preferences(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError(
            "Update EV planning preferences to the documented schema 1; preserve input."
        )
    for field, low, high in (
        ("stop_minutes", 1, 240),
        ("reserve_percent", 0, 50),
        ("cold_allowance_percent", 0, 200),
        ("highway_allowance_percent", 0, 200),
    ):
        number(value[field], low, high)
    if type(value["stop_minutes"]) is not int or type(value["reserve_percent"]) is not int:
        raise ValueError("EV stop duration and reserve must be integers.")
    if value["default_vehicle"] is not None:
        text(value["default_vehicle"], 100)
    mapping(value["event_vehicles"], 1000, 512, 100)
    mapping(value["person_vehicles"], 100, 254, 100)
    mapping(value["event_drivers"], 1000, 512, 254)
    charges = value["destination_charges"]
    if not isinstance(charges, list) or len(charges) > 100:
        raise ValueError("EV destination charging needs a bounded evidence list.")
    seen = set()
    for charge in charges:
        fields = {
            "schema_version",
            "vehicle",
            "destination",
            "available_at",
            "energy_kwh",
            "source",
        }
        if (
            not isinstance(charge, dict)
            or set(charge) != fields
            or type(charge["schema_version"]) is not int
            or charge["schema_version"] != 1
        ):
            raise ValueError("Use complete schema-1 destination charging evidence.")
        text(charge["vehicle"], 100)
        text(charge["destination"], 2000)
        instant = datetime.fromisoformat(text(charge["available_at"], 50).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("Destination charging needs a zoned availability instant.")
        number(charge["energy_kwh"], 0.001, 1000)
        text(charge["source"], 1000)
        identity = (charge["vehicle"], " ".join(charge["destination"].split()).casefold(), instant)
        if identity in seen:
            raise ValueError("Duplicate destination charging evidence must not add energy twice.")
        seen.add(identity)
    return value
