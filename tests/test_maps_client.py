"""Tests for skills/flight-assist/maps_client.py.

Mocks `urllib.request.urlopen` so the tests exercise URL building,
response parsing, and error branching without touching the live Google
Maps Distance Matrix API. Synthetic fixtures only (no real API keys,
fixture address strings).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from maps_client import MapsClient, MapsError, TravelTime  # noqa: E402

SYNTH_KEY = "AIzaSy_synthetic_test_key"


class _FakeResponse:
    """Stand-in for the urlopen context manager: yields .read()."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _ok_response(
    *,
    duration_value: int = 1800,
    in_traffic_value: int | None = 2400,
    distance_value: int = 60000,
    origin_resolved: str = "1 Fixture Loop, Cupertino, CA 95014, USA",
    destination_resolved: str = "San Francisco International Airport, San Francisco, CA, USA",
) -> _FakeResponse:
    element: dict = {
        "status": "OK",
        "duration": {"text": "30 mins", "value": duration_value},
        "distance": {"text": "60 km", "value": distance_value},
    }
    if in_traffic_value is not None:
        element["duration_in_traffic"] = {"text": "40 mins", "value": in_traffic_value}
    body = json.dumps(
        {
            "status": "OK",
            "origin_addresses": [origin_resolved],
            "destination_addresses": [destination_resolved],
            "rows": [{"elements": [element]}],
        }
    ).encode()
    return _FakeResponse(body)


def _error_response(status: str, error_message: str = "synthetic error") -> _FakeResponse:
    body = json.dumps({"status": status, "error_message": error_message}).encode()
    return _FakeResponse(body)


@pytest.fixture
def client() -> MapsClient:
    return MapsClient(SYNTH_KEY, timeout=5.0)


def test_from_env_raises_when_key_unset(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GOOGLE_MAPS_API_KEY"):
        MapsClient.from_env()


def test_from_env_uses_key_from_env_var(monkeypatch):
    """A from_env-constructed client sends the env-var key in the URL."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", SYNTH_KEY)
    captured_urls = []

    def fake_urlopen(request, **kwargs):
        captured_urls.append(request.full_url)
        return _ok_response()

    c = MapsClient.from_env()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        c.travel_time(origin="home", destination="SFO")

    assert len(captured_urls) == 1
    parsed = urllib.parse.urlparse(captured_urls[0])
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params["key"] == SYNTH_KEY


def test_constructor_rejects_empty_key():
    with pytest.raises(ValueError, match="empty"):
        MapsClient("")


def test_travel_time_rejects_empty_origin(client):
    with pytest.raises(ValueError, match="origin and destination"):
        client.travel_time(origin="", destination="SFO")


def test_travel_time_rejects_empty_destination(client):
    with pytest.raises(ValueError, match="origin and destination"):
        client.travel_time(origin="home", destination="")


def test_travel_time_success_with_traffic(client):
    with patch("urllib.request.urlopen", return_value=_ok_response()):
        result = client.travel_time(
            origin="1 Fixture Loop, Cupertino, CA", destination="SFO airport"
        )

    assert isinstance(result, TravelTime)
    assert result.duration_seconds == 1800
    assert result.in_traffic_seconds == 2400
    assert result.traffic_factor == pytest.approx(2400 / 1800)
    assert result.distance_meters == 60000
    assert "Cupertino" in result.origin_resolved
    assert "Francisco" in result.destination_resolved


def test_travel_time_without_traffic_block(client):
    """When the API omits duration_in_traffic, fall back to free-flow values."""
    with patch("urllib.request.urlopen", return_value=_ok_response(in_traffic_value=None)):
        result = client.travel_time(origin="home", destination="SFO")

    assert result.duration_seconds == 1800
    assert result.in_traffic_seconds is None
    assert result.traffic_factor == 1.0


def test_travel_time_url_carries_traffic_args(client):
    """departure_time=now and traffic_model=best_guess must be on every request."""
    captured_urls = []

    def fake_urlopen(request, **kwargs):
        captured_urls.append(request.full_url)
        return _ok_response()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.travel_time(origin="home", destination="SFO airport")

    parsed = urllib.parse.urlparse(captured_urls[0])
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params["origins"] == "home"
    assert params["destinations"] == "SFO airport"
    assert params["departure_time"] == "now"
    assert params["traffic_model"] == "best_guess"
    assert params["key"] == SYNTH_KEY


def test_top_level_error_raises_maps_error(client):
    with patch("urllib.request.urlopen", return_value=_error_response("REQUEST_DENIED")):
        with pytest.raises(MapsError) as exc_info:
            client.travel_time(origin="home", destination="SFO")
    assert exc_info.value.status == "REQUEST_DENIED"


def test_element_level_error_raises_maps_error(client):
    """An OK top-level status with a per-element NOT_FOUND surfaces as MapsError."""
    body = json.dumps(
        {
            "status": "OK",
            "origin_addresses": [""],
            "destination_addresses": [""],
            "rows": [{"elements": [{"status": "NOT_FOUND"}]}],
        }
    ).encode()
    with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
        with pytest.raises(MapsError) as exc_info:
            client.travel_time(origin="nowhere", destination="SFO")
    assert exc_info.value.status == "NOT_FOUND"


def test_zero_results_element_raises_maps_error(client):
    """ZERO_RESULTS at the element level (e.g., no driving route) surfaces as MapsError."""
    body = json.dumps(
        {
            "status": "OK",
            "origin_addresses": ["origin"],
            "destination_addresses": ["destination"],
            "rows": [{"elements": [{"status": "ZERO_RESULTS"}]}],
        }
    ).encode()
    with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
        with pytest.raises(MapsError) as exc_info:
            client.travel_time(origin="origin", destination="destination")
    assert exc_info.value.status == "ZERO_RESULTS"


def test_malformed_response_no_rows_raises_maps_error(client):
    body = json.dumps(
        {"status": "OK", "origin_addresses": [], "destination_addresses": [], "rows": []}
    ).encode()
    with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
        with pytest.raises(MapsError) as exc_info:
            client.travel_time(origin="home", destination="SFO")
    assert exc_info.value.status == "MALFORMED_RESPONSE"


def test_malformed_response_missing_duration_raises_maps_error(client):
    """An OK element without duration is structurally invalid."""
    body = json.dumps(
        {
            "status": "OK",
            "origin_addresses": ["origin"],
            "destination_addresses": ["destination"],
            "rows": [{"elements": [{"status": "OK", "distance": {"value": 100}}]}],
        }
    ).encode()
    with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
        with pytest.raises(MapsError) as exc_info:
            client.travel_time(origin="origin", destination="destination")
    assert exc_info.value.status == "MALFORMED_RESPONSE"
