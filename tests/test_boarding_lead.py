"""Tests for the boarding-lead resolver (`boarding_lead.py`).

Deterministic fixtures only — real airport coordinates and aircraft model
strings (sampled from live byAir payloads), no generated inputs.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from boarding_lead import (  # noqa: E402
    DEFAULT_LEAD_MINUTES,
    LEAD_NARROWBODY_MINUTES,
    LEAD_TRANSOCEANIC_MINUTES,
    LEAD_WIDEBODY_MINUTES,
    SIZE_NARROWBODY,
    SIZE_UNKNOWN,
    SIZE_WIDEBODY,
    classify_aircraft,
    is_transoceanic,
    resolve_boarding_lead_minutes,
)

# Real airport coordinates (lat, lon) sampled from byAir.
BNA = (36.1245, -86.6782)  # Nashville
LHR = (51.4706, -0.461941)  # London Heathrow
JFK = (40.640655, -73.781937)  # New York
SVO = (55.970288, 37.415021)  # Moscow
LED = (59.800146, 30.265169)  # Saint Petersburg
RSW = (26.533799, -81.756812)  # Fort Myers
BWI = (39.176772, -76.668419)  # Baltimore
SFO = (37.618805, -122.375416)  # San Francisco
NRT = (35.764722, 140.386389)  # Tokyo Narita
SIN = (1.35019, 103.994003)  # Singapore
ALA = (43.350275, 77.03082)  # Almaty


# --- classify_aircraft -------------------------------------------------


def test_classify_narrowbody_a320_family():
    assert classify_aircraft("Airbus A320") == SIZE_NARROWBODY
    assert classify_aircraft("Airbus A321") == SIZE_NARROWBODY
    assert classify_aircraft("Airbus A319") == SIZE_NARROWBODY


def test_classify_narrowbody_737_and_757():
    assert classify_aircraft("Boeing 737-800") == SIZE_NARROWBODY
    assert classify_aircraft("Boeing 737 MAX 9") == SIZE_NARROWBODY
    assert classify_aircraft("Boeing 757-200") == SIZE_NARROWBODY


def test_classify_narrowbody_regional_and_turboprop():
    assert classify_aircraft("Embraer E175") == SIZE_NARROWBODY
    assert classify_aircraft("ATR 72-600") == SIZE_NARROWBODY
    assert classify_aircraft("Airbus A220-300") == SIZE_NARROWBODY


def test_classify_widebody_twin_aisle():
    assert classify_aircraft("Boeing 787-9") == SIZE_WIDEBODY
    assert classify_aircraft("Boeing 777-300ER") == SIZE_WIDEBODY
    assert classify_aircraft("Boeing 767-300") == SIZE_WIDEBODY
    assert classify_aircraft("Boeing 747-8") == SIZE_WIDEBODY
    assert classify_aircraft("Airbus A330-300") == SIZE_WIDEBODY
    assert classify_aircraft("Airbus A350-900") == SIZE_WIDEBODY
    assert classify_aircraft("Airbus A380-800") == SIZE_WIDEBODY


def test_classify_unknown_when_blank():
    assert classify_aircraft("") == SIZE_UNKNOWN
    assert classify_aircraft(None) == SIZE_UNKNOWN


# --- is_transoceanic ---------------------------------------------------


def test_transatlantic_true():
    assert is_transoceanic(*BNA, *LHR) is True
    assert is_transoceanic(*JFK, *SVO) is True


def test_transpacific_true():
    assert is_transoceanic(*SFO, *NRT) is True


def test_domestic_same_block_false():
    assert is_transoceanic(*RSW, *BWI) is False  # both Americas
    assert is_transoceanic(*SVO, *LED) is False  # both Eur/Africa


def test_europe_asia_overland_not_transoceanic():
    # London->Singapore and Almaty->Moscow cross land, not an ocean.
    assert is_transoceanic(*LHR, *SIN) is False
    assert is_transoceanic(*ALA, *SVO) is False


def test_short_boundary_straddle_below_distance_floor_false():
    # Two points just across the -30 block boundary, ~340 km apart: a
    # block pair match, but under the transoceanic distance floor.
    assert is_transoceanic(40.0, -32.0, 40.0, -28.0) is False


# --- resolve_boarding_lead_minutes -------------------------------------


def test_resolve_transoceanic_overrides_narrowbody():
    # A narrowbody flying transatlantic still gets the ocean lead.
    assert (
        resolve_boarding_lead_minutes(
            aircraft_model="Boeing 757-200",
            inbound_aircraft_model=None,
            dep_lat=BNA[0],
            dep_lon=BNA[1],
            arr_lat=LHR[0],
            arr_lon=LHR[1],
        )
        == LEAD_TRANSOCEANIC_MINUTES
    )


def test_resolve_widebody_domestic():
    assert (
        resolve_boarding_lead_minutes(
            aircraft_model="Boeing 777-300ER",
            inbound_aircraft_model=None,
            dep_lat=RSW[0],
            dep_lon=RSW[1],
            arr_lat=BWI[0],
            arr_lon=BWI[1],
        )
        == LEAD_WIDEBODY_MINUTES
    )


def test_resolve_narrowbody_domestic():
    assert (
        resolve_boarding_lead_minutes(
            aircraft_model="Airbus A320",
            inbound_aircraft_model=None,
            dep_lat=RSW[0],
            dep_lon=RSW[1],
            arr_lat=BWI[0],
            arr_lon=BWI[1],
        )
        == LEAD_NARROWBODY_MINUTES
    )


def test_resolve_falls_back_to_inbound_model_when_blank():
    # byAir top-level model is sometimes "" — fall back to the inbound
    # aircraft model (Find My Plane) before defaulting.
    assert (
        resolve_boarding_lead_minutes(
            aircraft_model="",
            inbound_aircraft_model="Airbus A350-900",
            dep_lat=RSW[0],
            dep_lon=RSW[1],
            arr_lat=BWI[0],
            arr_lon=BWI[1],
        )
        == LEAD_WIDEBODY_MINUTES
    )


def test_resolve_default_when_nothing_known():
    # No aircraft and same-block (domestic) coords -> default narrowbody lead.
    assert (
        resolve_boarding_lead_minutes(
            aircraft_model="",
            inbound_aircraft_model=None,
            dep_lat=RSW[0],
            dep_lon=RSW[1],
            arr_lat=BWI[0],
            arr_lon=BWI[1],
        )
        == DEFAULT_LEAD_MINUTES
    )


def test_resolve_unknown_aircraft_but_transoceanic():
    # No aircraft info, but the route is transatlantic -> ocean lead.
    assert (
        resolve_boarding_lead_minutes(
            aircraft_model=None,
            inbound_aircraft_model=None,
            dep_lat=BNA[0],
            dep_lon=BNA[1],
            arr_lat=LHR[0],
            arr_lon=LHR[1],
        )
        == LEAD_TRANSOCEANIC_MINUTES
    )


def test_resolve_skips_transoceanic_when_coords_missing():
    # Missing coordinates -> transoceanic check skipped, falls to size.
    assert (
        resolve_boarding_lead_minutes(
            aircraft_model="Airbus A350-900",
            inbound_aircraft_model=None,
            dep_lat=None,
            dep_lon=None,
            arr_lat=None,
            arr_lon=None,
        )
        == LEAD_WIDEBODY_MINUTES
    )
