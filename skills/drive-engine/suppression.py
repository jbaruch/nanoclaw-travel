"""Trivial-leg suppression — pure, no I/O, no clock.

The suppression precedence for a planned leg is fixed (#156 G6):

    connection suppression → trivial-leg suppression → route/emit

Connection suppression is structural and happens upstream in `chain.py` — an
airside connection yields no leg at all, so it never reaches here. This module
owns the SECOND rung: a leg that survived to routing but whose routed drive is
trivially short (hotel literally at the terminal) produces no drive block, because
heading to the gate is already covered by the boarding / time-to-leave presence
block.

Per review R6 / V3 the suppression is CONDITIONAL on that presence block actually
existing for the flight (it lives on the byAir calendar, not primary): if it is
absent, do NOT suppress silently — the trivial drive block would be the only
"head to the gate" signal left. Pure: the caller resolves the routed drive and
checks the presence block upstream and passes both in.
"""

from __future__ import annotations

from datetime import timedelta

# Routed drives at or under this are trivial — the operator is effectively already
# at the terminal. Revisit-later default per #156 Decision 2.
TRIVIAL_LEG_THRESHOLD = timedelta(minutes=10)


def is_trivial_leg(
    routed_drive: timedelta,
    *,
    presence_block_present: bool,
    threshold: timedelta = TRIVIAL_LEG_THRESHOLD,
) -> bool:
    """Whether a routed leg should be suppressed as trivial (#156 R6 / G6).

    True only when the routed drive is at or under `threshold` AND a boarding /
    time-to-leave presence block exists for the flight. A trivial drive with no
    presence block is NOT suppressed — that block is what would otherwise tell the
    operator to head to the gate.
    """
    if routed_drive < timedelta(0):
        raise ValueError("is_trivial_leg: routed_drive must be non-negative")
    return routed_drive <= threshold and presence_block_present
