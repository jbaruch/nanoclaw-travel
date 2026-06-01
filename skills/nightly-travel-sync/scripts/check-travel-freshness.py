#!/usr/bin/env python3
"""Check freshness of travel-schedule.json. Output JSON status to stdout.

Statuses:
  - "missing" — file does not exist (sync pipeline never wrote it or it was deleted)
  - "fresh"   — file age (now - mtime) < STALE_THRESHOLD_DAYS (steady state, no alert)
  - "stale"   — file age >= threshold; agent must consult Gmail for missed bookings.
                Output includes the Gmail query string (with one-day pre-buffer
                for Gmail `after:` boundary semantics) and the subject prefix
                that signals a TripIt forwarded-confirmation notification.

Exit code is always 0 — the agent decides what to do based on the JSON status.
"""

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

SCHEDULE_PATH = pathlib.Path("/workspace/group/travel-schedule.json")
STALE_THRESHOLD_DAYS = 7
SUBJECT_PREFIX = "Baruch, check out your TripIt itinerary for Fwd:"


def main():
    if not SCHEDULE_PATH.exists():
        print(
            json.dumps(
                {
                    "status": "missing",
                    "path": str(SCHEDULE_PATH),
                }
            )
        )
        return 0

    mtime = datetime.fromtimestamp(SCHEDULE_PATH.stat().st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - mtime
    age_days = round(age.total_seconds() / 86400, 2)

    if age < timedelta(days=STALE_THRESHOLD_DAYS):
        print(
            json.dumps(
                {
                    "status": "fresh",
                    "mtime": mtime.isoformat(),
                    "age_days": age_days,
                }
            )
        )
        return 0

    # Gmail's `after:` filter is date-only and inclusive of the named day in
    # the user's timezone. To avoid losing a booking confirmation that landed
    # on the same day as the mtime due to TZ skew or boundary handling,
    # subtract one day.
    gmail_after = (mtime - timedelta(days=1)).strftime("%Y/%m/%d")
    print(
        json.dumps(
            {
                "status": "stale",
                "mtime": mtime.isoformat(),
                "age_days": age_days,
                "gmail_query": f"from:tripit.com after:{gmail_after}",
                "subject_prefix": SUBJECT_PREFIX,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
