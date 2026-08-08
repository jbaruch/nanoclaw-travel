"""Canonical trip key — the stable per-trip identifier shared across skills.

One trip is named by several artifacts: `travel-schedule.json` holds the TripIt
`Trip` wrapper, `travel-db.json` buckets that trip's items under a slug, and the
drive engine records a per-trip drive-or-fly verdict. Those artifacts have to
agree on ONE key or a cross-skill read silently misses (a verdict written under
`tn-tigers-2026-08` never found by a reader computing `tn-tigers-vs-2026-8`).

The key is derived from facts that do not drift: the trip summary and its start
month. A TripIt trip id would be stabler still, but `travel-schedule.json` does
not carry one as a field — it appears only inside the free-text description URL —
so the summary+month slug is the identifier both sides can compute.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from trip_key import trip_key

    trip_key("TN TIGERS VS Faith Christian School", "2026-08-14")
    # → "tn-tigers-vs-faith-christian-school-2026-08"
"""

from __future__ import annotations

import re
from datetime import date, datetime

# A trailing 4-digit year in the summary ("JNation 2026") is dropped: the key
# already carries the start year, and TripIt re-titles a recurring trip's year
# without it being a different trip.
_TRAILING_YEAR_RE = re.compile(r"\s+\d{4}$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _start_date(value: str | date | datetime) -> date:
    """The trip's start as a calendar date.

    Accepts the schedule's ISO string (date-only `2026-08-14` or timed
    `2026-08-14T20:00:00Z` — only the date part is read) as well as an already
    parsed `date`/`datetime`, so both the schedule reader and the DB builder
    pass what they already hold.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"trip_key: unusable trip start {value!r}")
    return date.fromisoformat(value.strip()[:10])


def trip_key(summary: str, start: str | date | datetime) -> str:
    """The canonical key for one trip — `<slugified-summary>-<YYYY>-<MM>`.

    Args:
        summary: the trip's TripIt summary.
        start: the trip's start, as the schedule's ISO string or a date.

    Returns:
        The slug both `travel-db.json` and the drive-engine verdict store key on.

    Raises:
        ValueError: when `start` is not a parseable date.
    """
    parsed = _start_date(start)
    clean = _TRAILING_YEAR_RE.sub("", summary.strip())
    slug_base = _NON_SLUG_RE.sub("-", clean.lower()).strip("-")
    return f"{slug_base}-{parsed.year}-{parsed.month:02d}"
