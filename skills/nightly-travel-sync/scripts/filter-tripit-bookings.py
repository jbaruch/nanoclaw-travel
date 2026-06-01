#!/usr/bin/env python3
"""Filter a Gmail message list (stdin JSON) for TripIt booking confirmations.

Input (stdin):
  JSON array of objects, each with at least a `subject` field. Optional
  fields (`id`, `from`, `date`) are preserved in the matches output so the
  alert can show the agent's choice of metadata.

Output (stdout):
  JSON object `{"matches": [...], "count": N}`.

Match rule:
  Subject starts with the unique TripIt forwarded-confirmation prefix.
  This is the ONLY subject pattern TripIt uses when it ingests a new
  booking from a forwarded confirmation email — Booking confirmed #N,
  Reservation confirmed at, Your Flight Receipt, Travel Reservation
  Center Trip ID, etc. Other tripit.com mail (Pro alerts, friend-shared
  trips, geofenced marketing, platform announcements) does NOT carry
  this prefix and is correctly excluded.
"""

import json
import sys

PREFIX = "Baruch, check out your TripIt itinerary for Fwd:"


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON on stdin: {e}"}), file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print(json.dumps({"error": "stdin must be a JSON array"}), file=sys.stderr)
        return 1

    matches = [
        m
        for m in data
        if isinstance(m, dict)
        and isinstance(m.get("subject"), str)
        and m["subject"].startswith(PREFIX)
    ]
    print(json.dumps({"matches": matches, "count": len(matches)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
