"""Lodging record roles — the check-in / check-out discriminator.

TripIt models one hotel stay as TWO records that are indistinguishable by
`type` — both are `Lodging` — and differ only in their summary prefix:

    Check-in:  Fairfield Inn & Suites by Marriott Gatlinburg Downtown
    Check-out: Fairfield Inn & Suites by Marriott Gatlinburg Downtown

Every consumer that needs "when does the stay start" has to read that prefix,
and reading it wrong is silent: a check-out record answers `start` just as
happily as a check-in does, so a stay looks like it begins on its last morning.
This module is the one place that prefix is spelled.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from lodging import CHECK_IN, CHECK_OUT, lodging_role, hotel_name

    lodging_role("Check-out: Fairfield Inn")   # → "out"
    hotel_name("Check-out: Fairfield Inn")     # → "Fairfield Inn"
"""

from __future__ import annotations

CHECK_IN = "in"
CHECK_OUT = "out"

CHECK_IN_PREFIX = "Check-in:"
CHECK_OUT_PREFIX = "Check-out:"

_PREFIX_ROLES = ((CHECK_IN_PREFIX, CHECK_IN), (CHECK_OUT_PREFIX, CHECK_OUT))


def lodging_role(summary: object) -> str | None:
    """`"in"` / `"out"` for a lodging summary, else None.

    None covers both a non-lodging summary and a lodging record TripIt wrote
    without the conventional prefix — callers treat it as "role unknown" rather
    than guessing a side.
    """
    if not isinstance(summary, str):
        return None
    for prefix, role in _PREFIX_ROLES:
        if summary.startswith(prefix):
            return role
    return None


def hotel_name(summary: object) -> str | None:
    """The hotel name with its `Check-in:` / `Check-out:` prefix stripped.

    None when the summary carries no recognized prefix, so a caller pairing
    stays by hotel never keys on the raw prefixed string for one record and the
    bare name for another.
    """
    if not isinstance(summary, str):
        return None
    for prefix in (CHECK_IN_PREFIX, CHECK_OUT_PREFIX):
        if summary.startswith(prefix):
            name = summary[len(prefix) :].strip()
            return name or None
    return None
