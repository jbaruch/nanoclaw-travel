"""Build the two airport drive blocks' desired-state inputs — pure, no I/O.

Piece 4c of #90 (the integration), first slice. This is the deterministic seam
between the live world (byAir airport context + Maps routing + the resolved
origin) and the pure planner `airport_drive.plan_drive_block`:

    raw byAir airport payloads ─┐
    config clearance overrides ─┼─► airport_drive_inputs ─► DesiredDriveBlock ─► plan_drive_block
    routed drive (origin/dest/seconds) ─┘

The caller (the wake-cycle reconcile + the precheck re-anchor, both later 4c
slices) performs the I/O — `byair.get_airport(airport_id)`, `maps.travel_time(...)`,
the moving-origin ladder — then hands the resolved values here. This module does
ONLY the window math, summary text, and tz selection, mirroring how
`airport_drive.py` is a pure planner the precheck feeds. No network, no clock,
no state-record shape: the dep/arr instants arrive already resolved (byAir truth
when known, scheduled before the first poll; for the drive-home block, the live
ETA pre-create and the landed actual finalize — #90 §6 decision (c)).

Two blocks, computed by the two builders here (see `airport_drive.DesiredDriveBlock`):

  * departure (`to_airport`) — anchored to the be-at-the-airport DEADLINE
    `dep − clearance`; the block runs `[anchor − drive, anchor]`, so you must
    LEAVE BY `anchor − drive`. Summary: `Drive: → <CODE> (<flight>)`.
  * arrival (`from_airport`) — anchored to the earliest the drive home can START
    `actual_arr + post_arrival_delay`; the block runs `[anchor, anchor + drive]`.
    Summary: `Drive: <CODE> → home`.

Clearance / post-arrival minutes come from `airport_lead` (the operator policy
table + the byAir `delay.index` nudge); `config.json` overrides the base buffers
(schema v5, #90 4b) — this module reads those keys and passes them through, the
math stays in `airport_lead`.

stdlib-only (`datetime`) per `jbaruch/coding-policy: dependency-management`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from airport_drive import DesiredDriveBlock  # noqa: E402
from airport_lead import (  # noqa: E402
    resolve_departure_clearance_minutes,
    resolve_post_arrival_minutes,
)

# config.json keys that override `airport_lead`'s base policy table (#90 4b,
# schema v5). The delay-index nudge stays an `airport_lead` constant — not a
# config field — by the 4b decision, so it has no key here.
_CLEARANCE_DOMESTIC_KEY = "airport_clearance_domestic_minutes"
_CLEARANCE_INTERNATIONAL_KEY = "airport_clearance_international_minutes"
_POST_ARRIVAL_DOMESTIC_KEY = "airport_post_arrival_domestic_minutes"
_POST_ARRIVAL_INTL_US_KEY = "airport_post_arrival_intl_us_minutes"
_POST_ARRIVAL_INTL_ABROAD_KEY = "airport_post_arrival_intl_abroad_minutes"


@dataclass(frozen=True)
class AirportContext:
    """The slice of a byAir `get_airport` payload the drive blocks need.

    Extracted by `airport_context` so the builders depend on a stable shape, not
    byAir's raw dict. Every field is optional — byAir occasionally omits one, and
    the builders degrade safely (an undecodable/absent flag classifies
    international per `airport_lead.departure_class`; an absent tz omits the
    CREATE `timezone`; an absent delay index contributes no nudge).

    Fields:
        airport_id: byAir's airport id (for the caller's per-airport cache key).
        flag: `countryFlag` emoji — decoded to ISO for domestic/intl class.
        delay_index: `delay.index` ("low"/"medium"/"high") — departure nudge.
        timezone: the airport's IANA tz — the block's CREATE timezone (#83).
        code / name: airport code / name when the payload carries them (the
            builders take the code explicitly from the flight payload, so these
            are informational and may be None).
    """

    airport_id: int | None = None
    flag: str | None = None
    delay_index: str | None = None
    timezone: str | None = None
    code: str | None = None
    name: str | None = None


def _as_str(value: object) -> str | None:
    """A non-empty string, stripped of surrounding whitespace, or None.

    Whitespace-only counts as missing: a `timezone` of `"   "` is truthy but
    would survive into the CREATE args and be rejected by the calendar API, so
    it must read as absent (the block then omits the tz) rather than propagate.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def airport_context(payload: object) -> AirportContext:
    """Extract an `AirportContext` from a byAir `get_airport` payload, defensively.

    A non-dict payload yields an all-None context — the builders then over-buffer
    (international) and omit the tz, never raise. byAir nests congestion under
    `delay.index`; the country flag and IANA tz are top-level (#90 §3).
    """
    if not isinstance(payload, dict):
        return AirportContext()
    delay = payload.get("delay")
    delay_index = _as_str(delay.get("index")) if isinstance(delay, dict) else None
    return AirportContext(
        airport_id=_as_int(payload.get("id")),
        flag=_as_str(payload.get("countryFlag")),
        delay_index=delay_index,
        timezone=_as_str(payload.get("timezone")),
        code=_as_str(payload.get("code")),
        name=_as_str(payload.get("name")),
    )


def _override(config: dict | None, key: str) -> int | None:
    """Read a non-negative-int override from config, or None to use the default.

    `config.json` is hand-editable; `write_config` validates these keys, but a
    manual edit could slip a bad type past it. A non-int / negative / bool value
    is ignored (the `airport_lead` default applies) rather than propagated into
    the policy math — same defensive posture as `precheck._resolve_min_transfer_minutes`.
    """
    if not isinstance(config, dict):
        return None
    value = config.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _clearance_minutes(dep: AirportContext, arr: AirportContext, config: dict | None) -> int:
    kwargs: dict[str, int] = {}
    domestic = _override(config, _CLEARANCE_DOMESTIC_KEY)
    international = _override(config, _CLEARANCE_INTERNATIONAL_KEY)
    if domestic is not None:
        kwargs["domestic_minutes"] = domestic
    if international is not None:
        kwargs["international_minutes"] = international
    return resolve_departure_clearance_minutes(
        dep_flag=dep.flag,
        arr_flag=arr.flag,
        delay_index=dep.delay_index,
        **kwargs,
    )


def _post_arrival_minutes(dep: AirportContext, arr: AirportContext, config: dict | None) -> int:
    kwargs: dict[str, int] = {}
    for cfg_key, arg_name in (
        (_POST_ARRIVAL_DOMESTIC_KEY, "domestic_minutes"),
        (_POST_ARRIVAL_INTL_US_KEY, "intl_to_us_minutes"),
        (_POST_ARRIVAL_INTL_ABROAD_KEY, "intl_abroad_minutes"),
    ):
        value = _override(config, cfg_key)
        if value is not None:
            kwargs[arg_name] = value
    return resolve_post_arrival_minutes(dep_flag=dep.flag, arr_flag=arr.flag, **kwargs)


def departure_summary(dep_code: str, flight_code: str) -> str:
    """The drive-to-airport block title: `Drive: → BNA (DL123)` (#90 §10)."""
    return f"Drive: → {dep_code} ({flight_code})"


def arrival_summary(arr_code: str) -> str:
    """The drive-home block title: `Drive: BNA → home` (#90 §10)."""
    return f"Drive: {arr_code} → home"


def _require_aware(label: str, instant: datetime) -> None:
    if instant.tzinfo is None:
        raise ValueError(f"airport_drive_inputs: `{label}` must be timezone-aware")


def _require_seconds(baseline_seconds: int) -> None:
    if not isinstance(baseline_seconds, int) or isinstance(baseline_seconds, bool):
        raise ValueError("airport_drive_inputs: `baseline_seconds` must be an int")
    if baseline_seconds < 0:
        raise ValueError("airport_drive_inputs: `baseline_seconds` must be non-negative")


def _require_endpoints(origin: str, destination: str) -> None:
    # The seam fails fast here on an empty leg endpoint rather than deferring to
    # `airport_block.build_block_args` (which the planner calls downstream and
    # which rejects empty endpoints), so the error points at the builder call.
    if not origin or not destination:
        raise ValueError("airport_drive_inputs: `origin` and `destination` must be non-empty")


def departure_block(
    *,
    flight_code: str,
    dep_code: str,
    dep_ctx: AirportContext,
    arr_ctx: AirportContext,
    dep_instant: datetime,
    origin: str,
    destination: str,
    baseline_seconds: int,
    config: dict | None = None,
) -> DesiredDriveBlock:
    """Build the drive-TO-departure-airport block (`to_airport`).

    The anchor is the be-at-the-airport deadline `dep_instant − clearance`, where
    `clearance` is the route class buffer (domestic/international, decided from
    both airports' flags) plus the departure airport's `delay.index` nudge, with
    config overriding the base buffers. The block ends at the anchor and starts at
    `anchor − drive`, the leave-by; the CREATE timezone is the departure airport's.

    Args:
        flight_code: the flight's code, for the summary (`DL123`).
        dep_code: the departure airport code, for the summary (`BNA`).
        dep_ctx / arr_ctx: the two airports' contexts (flag, delay, tz).
        dep_instant: byAir-truth departure (actual when known, else scheduled),
            timezone-aware.
        origin: the routed leg origin (the resolved live location / home).
        destination: the routed leg destination (the departure airport, as the
            string Maps was queried with — stored so the recheck re-routes it).
        baseline_seconds: the routed drive seconds.
        config: optional `config.json` dict for the clearance overrides.

    Raises:
        ValueError: on a naive `dep_instant`, a bad `baseline_seconds`, or an
            empty `origin` / `destination`.
    """
    _require_aware("dep_instant", dep_instant)
    _require_seconds(baseline_seconds)
    _require_endpoints(origin, destination)
    anchor = dep_instant - timedelta(minutes=_clearance_minutes(dep_ctx, arr_ctx, config))
    leg_start = anchor - timedelta(seconds=baseline_seconds)
    return DesiredDriveBlock(
        direction="to_airport",
        summary=departure_summary(dep_code, flight_code),
        leg_start=leg_start,
        anchor=anchor,
        baseline_seconds=baseline_seconds,
        origin=origin,
        destination=destination,
        leg_end=None,  # defaults to the anchor — the drive ends at the deadline
        timezone=dep_ctx.timezone,
    )


def arrival_block(
    *,
    arr_code: str,
    dep_ctx: AirportContext,
    arr_ctx: AirportContext,
    arr_instant: datetime,
    origin: str,
    destination: str,
    baseline_seconds: int,
    config: dict | None = None,
) -> DesiredDriveBlock:
    """Build the drive-HOME-from-arrival-airport block (`from_airport`).

    The anchor is the earliest the drive home can start, `arr_instant +
    post_arrival_delay`, where the delay reflects what control awaits on landing
    (domestic / intl-into-US / intl-abroad, decided from both airports' flags),
    with config overriding. The block starts at the anchor and ends at `anchor +
    drive`; the CREATE timezone is the arrival airport's. No congestion nudge
    applies on the arrival side (`airport_lead`).

    Args:
        arr_code: the arrival airport code, for the summary (`BNA`).
        dep_ctx / arr_ctx: the two airports' contexts (flag, tz).
        arr_instant: the arrival instant — the live ETA pre-create, the landed
            actual on finalize (#90 §6 (c)), timezone-aware.
        origin: the routed leg origin (the arrival airport, as queried).
        destination: the routed leg destination (home).
        baseline_seconds: the routed drive seconds.
        config: optional `config.json` dict for the post-arrival overrides.

    Raises:
        ValueError: on a naive `arr_instant`, a bad `baseline_seconds`, or an
            empty `origin` / `destination`.
    """
    _require_aware("arr_instant", arr_instant)
    _require_seconds(baseline_seconds)
    _require_endpoints(origin, destination)
    anchor = arr_instant + timedelta(minutes=_post_arrival_minutes(dep_ctx, arr_ctx, config))
    leg_end = anchor + timedelta(seconds=baseline_seconds)
    return DesiredDriveBlock(
        direction="from_airport",
        summary=arrival_summary(arr_code),
        leg_start=anchor,  # the drive home starts at the post-arrival deadline
        anchor=anchor,
        baseline_seconds=baseline_seconds,
        origin=origin,
        destination=destination,
        leg_end=leg_end,
        timezone=arr_ctx.timezone,
    )
