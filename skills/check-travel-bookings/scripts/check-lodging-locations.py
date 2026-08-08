#!/usr/bin/env python3
"""Flag upcoming hotel stays whose TripIt `location` is not a usable address.

TripIt sometimes drops a non-address into a Lodging's `location` — most often a
resort-fee / nightly-rate note (`"Stay resort fee: $72.03"`) or a blank. A garbage
location can't anchor a drive (the drive engine resolves the wrong place or
suppresses the block) and reads as nonsense on the calendar. The ONLY fix is a
manual TripIt edit, so this script only DETECTS; the `check-travel-bookings` skill
ALERTS the operator. Nothing is auto-corrected.

Source is `/workspace/group/travel-schedule.json` — the raw TripIt-derived feed,
the only artifact that carries each Lodging's `location` (the day-keyed
`travel-db.json` drops it). One warning per upcoming stay, keyed on the
`Check-in:` record, naming the hotel, the bad location text, and the check-in
date. Silent when every upcoming stay has a plausible address.

Detection is deliberately conservative and fully enumerable per
`coding-policy: script-delegation` (the Regex Trap): it flags KNOWN non-address
shapes — a blank, a currency amount, or a rate/fee keyword — never a general
"does this look like an address?" judgment (that is reasoning, not scripting).
Real street addresses carry none of these, so false positives are near zero;
novel garbage shapes (a phone number, a booking code) are out of scope and pass
through silently rather than risk flagging a real address.

Scheduled-task-adjacent contract: single-line-free JSON on stdout, exit 0 on a
clean read (including when the schedule is missing/unreadable — schedule
freshness is `nightly-travel-sync`'s own alert surface, not this check's, so a
missing file yields an empty warning list, never a hard failure). A stderr
diagnostic records a degraded read.

stdlib-only per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# travel-core owns the `Check-in:` / `Check-out:` discriminator. Runtime mount
# first, dev-clone sibling fallback for CI (travel-core's SKILL.md pattern; this
# script sits one level deeper, under `scripts/`).
_BUNDLE_DIR = Path(__file__).resolve().parent.parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from lodging import CHECK_IN, hotel_name, lodging_role  # noqa: E402

SCHEDULE_PATH = "/workspace/group/travel-schedule.json"

# --- garbage signals (enumerable non-address shapes only) -------------------

# A currency symbol next to a number — a price/fee, never part of a street
# address. Covers `$72.03`, `€25`, `₪500`, etc.
_CURRENCY_RE = re.compile(r"[$€£₪¥₹]\s*\d")

# Rate / fee wording TripIt drops into the address field. Word-bounded so it
# can't fire on an address that merely contains one of the substrings. A bare
# "deposit" is deliberately excluded — it is a real place name ("Deposit, NY"),
# and a monetary deposit is already caught by `_CURRENCY_RE`.
_FEE_KEYWORDS_RE = re.compile(
    r"\b(?:resort fee|nightly rate|room rate|per night)\b|/\s*night",
    re.IGNORECASE,
)


def garbage_reason(location: object) -> str | None:
    """A short reason when `location` is not a usable address, else None.

    Blank / non-string → "no address in TripIt". A currency amount or a
    rate/fee keyword → the field holds a fee note, not an address. Anything else
    is treated as a plausible address and passes.
    """
    if not isinstance(location, str) or not location.strip():
        return "no address in TripIt"
    if _CURRENCY_RE.search(location) or _FEE_KEYWORDS_RE.search(location):
        return "location looks like a fee/rate note, not an address"
    return None


def _parse_day(value: object) -> date | None:
    """A schedule `start` string as a calendar date, else None.

    Tolerates both the date-only `YYYY-MM-DD` and the timed `YYYY-MM-DDTHH:MM:SSZ`
    shapes `refresh-travel-schedule.py` emits — the `[:10]` slice keeps only the
    day, matching `check-travel-bookings.py`.
    """
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def find_garbage_lodging(schedule: list | None, today: date) -> list[dict]:
    """Upcoming stays whose location is garbage — one entry per Check-in record.

    `today` is injected (not read from the clock) so the scan stays pure and
    testable. Only stays checking in on or after `today` are considered — a past
    or in-progress stay is not worth nagging about. Entries are sorted by
    check-in date.
    """
    warnings: list[dict] = []
    for record in schedule or []:
        if not isinstance(record, dict) or record.get("type") != "Lodging":
            continue
        summary = record.get("summary")
        if lodging_role(summary) != CHECK_IN:
            continue
        hotel = hotel_name(summary)
        if hotel is None:
            continue
        checkin = _parse_day(record.get("start"))
        if checkin is None or checkin < today:
            continue
        reason = garbage_reason(record.get("location"))
        if reason is None:
            continue
        warnings.append(
            {
                "hotel": hotel,
                "location": record.get("location")
                if isinstance(record.get("location"), str)
                else "",
                "checkin": checkin.isoformat(),
                "reason": reason,
            }
        )
    warnings.sort(key=lambda w: w["checkin"])
    return warnings


def load_schedule(path: str) -> list | None:
    """Read travel-schedule.json's record list, or None on any degraded read.

    Missing / unreadable / non-UTF-8 / malformed / non-list root all resolve to
    None — the caller emits no warnings and exits 0, because schedule freshness
    is `nightly-travel-sync`'s alert surface, not this location check's. A stderr
    diagnostic records the cause.
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(
            f"check-lodging-locations: no usable travel schedule at {path} "
            f"({type(exc).__name__}) — no location check this run",
            file=sys.stderr,
        )
        return None
    if not isinstance(payload, list):
        print(
            f"check-lodging-locations: travel schedule at {path} has a non-list "
            "root — no location check this run",
            file=sys.stderr,
        )
        return None
    return payload


def main() -> None:
    today = date.today()
    schedule = load_schedule(SCHEDULE_PATH)
    warnings = find_garbage_lodging(schedule, today)
    output = {
        "garbage_lodging": warnings,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
