"""Tests for the airport-clearance resolver (`airport_lead.py`).

Deterministic fixtures only — real `countryFlag` emoji and `delay.index`
values sampled from live byAir airport payloads, no generated inputs.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from airport_lead import (  # noqa: E402
    ARRIVAL_DOMESTIC,
    ARRIVAL_INTL_ABROAD,
    ARRIVAL_INTL_TO_US,
    BASE_CLEARANCE_DOMESTIC_MINUTES,
    BASE_CLEARANCE_INTERNATIONAL_MINUTES,
    CLASS_DOMESTIC,
    CLASS_INTERNATIONAL,
    POST_ARRIVAL_DOMESTIC_MINUTES,
    POST_ARRIVAL_INTL_ABROAD_MINUTES,
    POST_ARRIVAL_INTL_TO_US_MINUTES,
    arrival_class,
    departure_class,
    flag_to_iso,
    resolve_departure_clearance_minutes,
    resolve_post_arrival_minutes,
)

# Real countryFlag emoji sampled from byAir search_airports payloads.
US = "🇺🇸"  # United States
FR = "🇫🇷"  # France        (Schengen)
DE = "🇩🇪"  # Germany       (Schengen)
CZ = "🇨🇿"  # Czechia       (Schengen; byAir spells it "Czechia")
HR = "🇭🇷"  # Croatia       (Schengen since 2023)
RO = "🇷🇴"  # Romania       (Schengen full since 2025-01)
BG = "🇧🇬"  # Bulgaria      (Schengen full since 2025-01)
CH = "🇨🇭"  # Switzerland   (Schengen, non-EU)
IE = "🇮🇪"  # Ireland       (EU, NOT Schengen — Common Travel Area)
TR = "🇹🇷"  # Türkiye       (byAir native spelling; not US, not Schengen)


# --- flag_to_iso -------------------------------------------------------


def test_flag_decodes_to_iso():
    assert flag_to_iso(US) == "US"
    assert flag_to_iso(CZ) == "CZ"  # native "Czechia" name, canonical CZ code
    assert flag_to_iso(TR) == "TR"  # native "Türkiye" name, canonical TR code
    assert flag_to_iso(HR) == "HR"


def test_flag_none_or_empty_returns_none():
    assert flag_to_iso(None) is None
    assert flag_to_iso("") is None


def test_flag_non_regional_indicator_returns_none():
    # Plain ASCII letters are not regional indicators.
    assert flag_to_iso("US") is None
    # A single regional indicator is not a country flag.
    assert flag_to_iso("🇺") is None


def test_flag_rejects_extra_characters():
    # Strict: exactly two regional indicators and nothing else. A valid flag
    # with surrounding junk must reject, not silently decode to the country.
    assert flag_to_iso("🇺🇸 ") is None  # trailing whitespace
    assert flag_to_iso("x🇺🇸y") is None  # surrounding characters


# --- departure_class ---------------------------------------------------


def test_departure_same_country_domestic():
    assert departure_class("US", "US") == CLASS_DOMESTIC


def test_departure_intra_schengen_domestic():
    # Crosses a border but no passport/customs control -> domestic.
    assert departure_class("FR", "DE") == CLASS_DOMESTIC
    # Recent joiners and non-EU Schengen members count too.
    assert departure_class("HR", "RO") == CLASS_DOMESTIC
    assert departure_class("BG", "CH") == CLASS_DOMESTIC


def test_departure_crossing_schengen_boundary_international():
    assert departure_class("US", "FR") == CLASS_INTERNATIONAL
    assert departure_class("FR", "US") == CLASS_INTERNATIONAL


def test_departure_ireland_not_schengen_international():
    # Common Travel Area is NOT Schengen — classifies international by design.
    assert departure_class("IE", "FR") == CLASS_INTERNATIONAL


def test_departure_undecodable_endpoint_is_international():
    # Conservative: a missing/undecodable side over-buffers, never under.
    assert departure_class(None, "US") == CLASS_INTERNATIONAL
    assert departure_class("US", None) == CLASS_INTERNATIONAL
    assert departure_class(None, None) == CLASS_INTERNATIONAL


# --- arrival_class -----------------------------------------------------


def test_arrival_domestic():
    assert arrival_class("US", "US") == ARRIVAL_DOMESTIC


def test_arrival_intra_schengen_is_domestic():
    assert arrival_class("FR", "DE") == ARRIVAL_DOMESTIC


def test_arrival_into_us_is_intl_to_us():
    assert arrival_class("FR", "US") == ARRIVAL_INTL_TO_US


def test_arrival_abroad_is_intl_abroad():
    assert arrival_class("US", "FR") == ARRIVAL_INTL_ABROAD
    assert arrival_class("FR", "TR") == ARRIVAL_INTL_ABROAD


def test_arrival_undecodable_defaults_to_abroad():
    # international (undecodable) + arr not decodable as US -> abroad (60, the
    # most conservative post-arrival delay).
    assert arrival_class(None, None) == ARRIVAL_INTL_ABROAD


# --- resolve_departure_clearance_minutes -------------------------------


def test_departure_clearance_domestic_low_delay():
    assert (
        resolve_departure_clearance_minutes(dep_flag=US, arr_flag=US, delay_index="low")
        == BASE_CLEARANCE_DOMESTIC_MINUTES  # 60 + 0
    )


def test_departure_clearance_domestic_high_delay_nudges():
    assert (
        resolve_departure_clearance_minutes(dep_flag=US, arr_flag=US, delay_index="high")
        == BASE_CLEARANCE_DOMESTIC_MINUTES + 30  # 90
    )


def test_departure_clearance_international_medium_delay():
    assert (
        resolve_departure_clearance_minutes(dep_flag=US, arr_flag=FR, delay_index="medium")
        == BASE_CLEARANCE_INTERNATIONAL_MINUTES + 15  # 135
    )


def test_departure_clearance_intra_schengen_is_domestic_buffer():
    assert (
        resolve_departure_clearance_minutes(dep_flag=FR, arr_flag=DE, delay_index="low")
        == BASE_CLEARANCE_DOMESTIC_MINUTES  # 60
    )


def test_departure_clearance_unknown_delay_index_no_nudge():
    assert (
        resolve_departure_clearance_minutes(dep_flag=US, arr_flag=US, delay_index=None)
        == BASE_CLEARANCE_DOMESTIC_MINUTES
    )
    assert (
        resolve_departure_clearance_minutes(dep_flag=US, arr_flag=US, delay_index="severe")
        == BASE_CLEARANCE_DOMESTIC_MINUTES
    )


def test_departure_clearance_undecodable_flag_buffers_international():
    # An undecodable departure flag must not collapse to the domestic buffer.
    assert (
        resolve_departure_clearance_minutes(dep_flag=None, arr_flag=US, delay_index="low")
        == BASE_CLEARANCE_INTERNATIONAL_MINUTES  # 120
    )


def test_departure_clearance_config_override():
    assert (
        resolve_departure_clearance_minutes(
            dep_flag=US,
            arr_flag=US,
            delay_index="high",
            domestic_minutes=45,
        )
        == 45 + 30
    )


# --- resolve_post_arrival_minutes --------------------------------------


def test_post_arrival_domestic():
    assert (
        resolve_post_arrival_minutes(dep_flag=US, arr_flag=US)
        == POST_ARRIVAL_DOMESTIC_MINUTES  # 20
    )


def test_post_arrival_intra_schengen_domestic():
    assert (
        resolve_post_arrival_minutes(dep_flag=FR, arr_flag=DE)
        == POST_ARRIVAL_DOMESTIC_MINUTES  # 20
    )


def test_post_arrival_into_us():
    assert (
        resolve_post_arrival_minutes(dep_flag=FR, arr_flag=US)
        == POST_ARRIVAL_INTL_TO_US_MINUTES  # 40
    )


def test_post_arrival_abroad():
    assert (
        resolve_post_arrival_minutes(dep_flag=US, arr_flag=FR)
        == POST_ARRIVAL_INTL_ABROAD_MINUTES  # 60
    )


def test_post_arrival_config_override():
    assert resolve_post_arrival_minutes(dep_flag=FR, arr_flag=US, intl_to_us_minutes=35) == 35
