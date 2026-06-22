"""Tests for the calendar event normalizer (`calendar_normalize.py`).

Fixtures are built programmatically with the real Google Calendar event
*structure* (Reclaim signature, Flighty summary format, start/end blocks)
but synthetic flight codes / airports — no real user data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from calendar_normalize import (  # noqa: E402
    NormalizeError,
    is_reclaim_travel,
    normalize_event,
)

PRIMARY_CAL = "user@example.com"
FLIGHTY_CAL = "c_synthetic@group.calendar.google.com"

RECLAIM_DESC = (
    "<i>This event was created by "
    '<a href="https://app.reclaim.ai/landing/about?name=Test">Reclaim</a>.</i>'
    "<p>Test is traveling to the airport for a flight at this time.</p>"
)
RECLAIM_DESC_NO_FLIGHT = (
    "<i>This event was created by "
    '<a href="https://app.reclaim.ai/landing">Reclaim</a>.</i>'
    "<p>Test is traveling at this time and may not be available.</p>"
)


def _timed(start_iso, end_iso, tz="America/Chicago"):
    return {
        "start": {"dateTime": start_iso, "timeZone": tz},
        "end": {"dateTime": end_iso, "timeZone": tz},
    }


def _reclaim_travel_event(summary="🚌 Travel", description=RECLAIM_DESC):
    return {
        "id": "reclaim-1",
        "summary": summary,
        "description": description,
        **_timed("2026-07-01T12:00:00-05:00", "2026-07-01T13:00:00-05:00"),
    }


# --- is_reclaim_travel ----------------------------------------------------


def test_reclaim_travel_block_is_classified_true():
    assert is_reclaim_travel(_reclaim_travel_event()) is True


def test_reclaim_travel_block_traveling_variant_true():
    # The "traveling at this time" buffer (no "flight" word) is still a
    # Reclaim travel block — classification keys off the summary, not the
    # description's "flight" mention.
    assert is_reclaim_travel(_reclaim_travel_event(description=RECLAIM_DESC_NO_FLIGHT)) is True


def test_reclaim_focus_block_is_false():
    # Reclaim-signed but not a travel block — habit/focus/task summaries
    # don't carry the travel marker.
    event = _reclaim_travel_event(summary="Focus Time")
    assert is_reclaim_travel(event) is False


def test_reclaim_habit_block_is_false():
    event = _reclaim_travel_event(summary="🥗 Lunch")
    assert is_reclaim_travel(event) is False


def test_user_event_titled_travel_without_signature_is_false():
    # A user's own "Travel to client site" with no Reclaim signature is
    # never a delete candidate.
    event = {
        "id": "user-1",
        "summary": "Travel to client site",
        "description": "Quarterly on-site visit",
        **_timed("2026-07-01T12:00:00-05:00", "2026-07-01T13:00:00-05:00"),
    }
    assert is_reclaim_travel(event) is False


def test_signature_present_but_summary_not_travel_is_false():
    assert is_reclaim_travel(_reclaim_travel_event(summary="Deep Work")) is False


def test_classification_is_case_insensitive():
    event = _reclaim_travel_event(summary="TRAVEL", description=RECLAIM_DESC.upper())
    assert is_reclaim_travel(event) is True


def test_missing_description_and_summary_is_false():
    assert is_reclaim_travel({"id": "x"}) is False


# --- normalize_event ------------------------------------------------------


def test_normalize_flight_event_shape():
    raw = {
        "id": "flight-1",
        "summary": "✈ BNA→YYZ • UA 8018",
        **_timed("2026-06-26T10:05:00-05:00", "2026-06-26T12:03:00-05:00"),
    }
    norm = normalize_event(raw, calendar_id=FLIGHTY_CAL)
    assert norm == {
        "event_id": "flight-1",
        "calendar_id": FLIGHTY_CAL,
        "summary": "✈ BNA→YYZ • UA 8018",
        "start": "2026-06-26T10:05:00-05:00",
        "end": "2026-06-26T12:03:00-05:00",
        "private_props": {},
        "is_reclaim_travel": False,
    }


def test_normalize_extracts_private_props():
    raw = {
        "id": "boarding-1",
        "summary": "Boarding UA8018",
        "extendedProperties": {"private": {"faFlightId": "100", "faKind": "boarding"}},
        **_timed("2026-06-26T09:35:00-05:00", "2026-06-26T10:05:00-05:00"),
    }
    norm = normalize_event(raw, calendar_id=FLIGHTY_CAL)
    assert norm["private_props"] == {"faFlightId": "100", "faKind": "boarding"}


def test_normalize_with_classify_reclaim_sets_travel_flag():
    norm = normalize_event(_reclaim_travel_event(), calendar_id=PRIMARY_CAL, classify_reclaim=True)
    assert norm["is_reclaim_travel"] is True
    assert norm["calendar_id"] == PRIMARY_CAL


def test_normalize_without_classify_reclaim_leaves_flag_false():
    # The same Reclaim event fetched without classify_reclaim (e.g. it
    # turned up on the byAir calendar query) is never flagged.
    norm = normalize_event(_reclaim_travel_event(), calendar_id=FLIGHTY_CAL, classify_reclaim=False)
    assert norm["is_reclaim_travel"] is False


def test_normalize_calendar_id_is_authoritative_not_from_body():
    # Even if the raw event carries an organizer email, calendar_id comes
    # from the fetch context, not the body.
    raw = {
        "id": "e1",
        "summary": "x",
        "organizer": {"email": "someone-else@group.calendar.google.com"},
        **_timed("2026-07-01T10:00:00-05:00", "2026-07-01T11:00:00-05:00"),
    }
    norm = normalize_event(raw, calendar_id=FLIGHTY_CAL)
    assert norm["calendar_id"] == FLIGHTY_CAL


def test_normalize_all_day_event_uses_date():
    raw = {
        "id": "allday-1",
        "summary": "Holiday",
        "start": {"date": "2026-07-04"},
        "end": {"date": "2026-07-05"},
    }
    norm = normalize_event(raw, calendar_id=PRIMARY_CAL)
    assert norm["start"] == "2026-07-04"
    assert norm["end"] == "2026-07-05"


def test_normalize_missing_id_raises():
    with pytest.raises(NormalizeError, match="missing required 'id'"):
        normalize_event({"summary": "x"}, calendar_id=FLIGHTY_CAL)


def test_normalize_missing_id_error_does_not_leak_values():
    # The error names the keys present but never their values, so calendar
    # content (summary/description/attendees) can't leak into logs.
    with pytest.raises(NormalizeError) as exc_info:
        normalize_event(
            {"summary": "Secret offsite location", "description": "private notes"},
            calendar_id=FLIGHTY_CAL,
        )
    message = str(exc_info.value)
    assert "Secret offsite location" not in message
    assert "private notes" not in message
    assert "summary" in message  # the key name is fine to surface


def test_normalize_missing_start_end_yields_none():
    raw = {"id": "e1", "summary": "x"}
    norm = normalize_event(raw, calendar_id=FLIGHTY_CAL)
    assert norm["start"] is None
    assert norm["end"] is None
