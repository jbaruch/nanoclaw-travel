"""Record the operator's drive-or-fly answer for a flight-less trip.

The operator triggers this by replying "drive" or "fly" to the question the
sweep asked about a trip booked with lodging and no flight, whose home→lodging
drive time landed in the ambiguous band (`lodging_source.classify_drive`).

The agent maps the reply to the trip by the NAME the question used — never an
internal key, since the message only ever showed the name. Resolution is by
trip summary against the live schedule, position-immune: a unique match is
recorded; several trips sharing a name come back as candidates for the agent to
disambiguate conversationally.

The answer outranks the drive-time band from here on (`drive_decision`
preserves an operator verdict across every later sweep), so the next sweep
plans the drive — or leaves it unplanned and lets the booking-gap check report
the missing flight — without asking again.

CLI: `python3 answer_drive_or_fly.py '<json-request>'` where the request is
`{"trip": "TN Tigers", "answer": "drive"}`. The answer is timestamped by the
process clock, so the request carries no time field. Always prints a JSON
result to stdout (never a bare traceback — the skill parses stdout):
  {"recorded": true, "trip": "TN Tigers", "answer": "drive"}
  {"recorded": false, "unmatched": "TN Tigers"}
  {"recorded": false, "ambiguous": "Team Weekend", "candidates": [{"when": ...}, ...]}
  {"recorded": false, "error": "<Type>: <message>"}   # operational failure

Exit codes: 0 = a result was produced (including unmatched / ambiguous — the
script ran fine, the trip just wasn't uniquely resolved); 1 = an operational
failure (an unreadable schedule, a verdict-store write failure); 2 = a
caller/usage error (missing argument, non-JSON, or an answer that is neither
`drive` nor `fly`).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from drive_decision import VERDICT_DRIVE, VERDICT_FLY, record_operator_answer  # noqa: E402
from lodging_source import DriveTrip, find_drive_trips  # noqa: E402
from trip_origin import load_travel_schedule  # noqa: E402

# How far ahead to look for the trip being answered about. Matches the sweep's
# own planning window, so any trip the sweep could have asked about is findable.
_LOOKAHEAD = timedelta(days=14)

_ANSWERS = {VERDICT_DRIVE, VERDICT_FLY}


def _normalize_answer(raw: object) -> str | None:
    """The canonical verdict for what the operator typed, else None."""
    if not isinstance(raw, str):
        return None
    answer = raw.strip().lower()
    return answer if answer in _ANSWERS else None


def resolve_trip(trips: list[DriveTrip], *, summary: str) -> tuple[DriveTrip | None, list[dict]]:
    """Find the trip being answered about, by summary.

    Returns `(trip, candidates)`: `trip` is set only when exactly ONE matches;
    several same-named trips return `(None, candidates)` for the agent to
    disambiguate. No match returns `(None, [])`. Matching is case-insensitive
    and whitespace-trimmed, since the operator retypes the name by hand.
    """
    wanted = summary.strip().casefold()
    matches = [trip for trip in trips if trip.summary.strip().casefold() == wanted]
    if not matches:
        return None, []
    if len(matches) > 1:
        return None, [
            {"trip": trip.summary, "when": trip.check_in.strftime("%a %b %d, %H:%M")}
            for trip in matches
        ]
    return matches[0], []


def record_answer(request: dict, *, schedule=None, now: datetime | None = None) -> dict:
    """Resolve + record an answer. See the module docstring for the shapes."""
    summary = request.get("trip")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("request needs a non-empty `trip` name")
    answer = _normalize_answer(request.get("answer"))
    if answer is None:
        raise ValueError(f"`answer` must be 'drive' or 'fly' (got {request.get('answer')!r})")

    now = now or datetime.now(timezone.utc)
    if schedule is None:
        schedule = load_travel_schedule()

    trips = find_drive_trips(schedule, now=now, window=_LOOKAHEAD)
    trip, candidates = resolve_trip(trips, summary=summary)
    if trip is None:
        if candidates:
            return {"recorded": False, "ambiguous": summary, "candidates": candidates}
        return {"recorded": False, "unmatched": summary}

    record_operator_answer(trip.key, answer, now=now, expires=trip.expires)
    return {"recorded": True, "trip": trip.summary, "answer": answer}


def _fail(message: str, code: int) -> int:
    """Emit the structured failure on stdout (skill contract) AND a concise
    diagnostic on stderr (standard script failure contract — an error stream for
    CI / operator logs to inspect), then return the exit code."""
    print(json.dumps({"recorded": False, "error": message}))
    print(f"answer_drive_or_fly: {message}", file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return _fail("usage: answer_drive_or_fly.py '<json-request>'", 2)
    try:
        request = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        return _fail(f"invalid JSON request: {exc}", 2)
    if not isinstance(request, dict):
        return _fail("request must be a JSON object", 2)
    try:
        result = record_answer(request)
    except ValueError as exc:
        # A malformed request is the caller's to fix, not an outage — exit 2 so
        # the skill corrects the call instead of telling the operator to retry.
        return _fail(str(exc), 2)
    except Exception as exc:  # noqa: BLE001 — outer-boundary-process-contract
        # The skill invokes this as a subprocess and parses ONLY stdout JSON. An
        # uncaught exception (an unreadable schedule, a corrupt verdict store, a
        # failed write) would emit a Python traceback the skill can't read — it
        # would look like "no result". Emit the documented
        # `{"recorded": false, "error": ...}` shape (plus a stderr diagnostic)
        # and a non-zero exit so the skill reports the failure instead of
        # hanging on unparseable output. KeyboardInterrupt / SystemExit still
        # propagate.
        return _fail(f"{type(exc).__name__}: {exc}", 1)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
