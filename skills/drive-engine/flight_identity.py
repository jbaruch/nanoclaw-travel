"""Canonical flight identity and the byAir ∪ TripIt union — pure, no I/O, no clock.

The unified drive engine (issue #156) derives airport legs from the union of two
flight sources: byAir's tracked flights and TripIt's itinerary segments. A single
physical flight routinely appears in BOTH — and, worse, appears in byAir under
more than one record: the live STN→CPH leg is tracked as `flight=6277117` (code
`FR7382`) AND `flight=7166978` (code `MW7382`), a codeshare the operator caught on
his own calendar (#156 review V2). Any identity that keys on the flight designator
hashes those apart, the connection between the two is missed, and the duplicate
`Drive:` blocks storm the calendar — exactly the defect this engine exists to kill.

So the canonical identity deliberately EXCLUDES the designator (#156 review W3):

    canonical identity = (dep_airport, arr_airport, scheduled_dep_instant ± tolerance)

Both airports are IATA codes — the only airport identifier the two sources share
(byAir's internal integer `airport_id` is meaningless to TripIt). The scheduled
departure is normalized to a true UTC instant, so the "TripIt says 23:50 on the
12th, byAir says 00:10Z on the 13th" midnight/TZ skew collapses to a small instant
gap rather than a date mismatch. Tolerance absorbs residual source disagreement on
the scheduled time while staying well under the 24 h daily-frequency floor, so
consecutive daily operations of the same route never over-merge. The marketing
`code` is retained for display and as a WEAK corroborator only — never a required
match component, because codeshares renumber.

This module is pure over an already-normalized `Flight` shape. The source-specific
normalization (byAir record + `get_airport` lookups → `Flight`; TripIt segment →
`Flight`) performs the I/O and hands normalized instants here, mirroring the
`airport_drive_inputs` split (resolved values in, pure planning here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Default identity tolerance on the scheduled-departure instant. Absorbs TZ /
# midnight-boundary skew and genuine byAir-vs-TripIt disagreement on the scheduled
# time (a few hours at most for one physical flight) while staying well under 24 h
# so two daily operations of the same route stay distinct. Revisit-later default
# per #156 ("well under 24 h"); a single operator is not on two same-route flights
# inside this window, so over-merge is not a practical risk. Tunable via the
# `tolerance` parameter on `merge_flights`.
DEFAULT_IDENTITY_TOLERANCE = timedelta(hours=6)

BYAIR = "byair"
TRIPIT = "tripit"
_VALID_SOURCES = frozenset({BYAIR, TRIPIT})


def _as_utc(when: datetime, *, field_name: str) -> datetime:
    """Normalize a tz-aware datetime to UTC. Reject naive datetimes.

    A naive datetime here means the caller lost the source offset — the exact
    corruption that makes a midnight-boundary flight hash to the wrong instant —
    so it is a hard error, not a silent local-time assumption.
    """
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be timezone-aware (carry its source UTC offset); "
            f"got naive {when!r} — resolve the offset before building a Flight"
        )
    return when.astimezone(timezone.utc)


@dataclass(frozen=True)
class Flight:
    """One flight as seen by a SINGLE source, normalized for identity.

    Airports are IATA codes (the shared identifier); `scheduled_dep`/`scheduled_arr`
    are tz-aware. `code` is the marketing designator — display + weak corroboration
    only, never part of identity. `source` is `byair` or `tripit`. Live times ride
    only on byAir records (`live_dep`/`live_arr`); TripIt carries none.
    """

    dep_airport: str
    arr_airport: str
    scheduled_dep: datetime
    scheduled_arr: datetime | None = None
    code: str | None = None
    source: str = BYAIR
    live_dep: datetime | None = None
    live_arr: datetime | None = None
    byair_flight_id: int | None = None
    tripit_segment_id: str | None = None
    trip_id: int | None = None

    def __post_init__(self) -> None:
        if self.source not in _VALID_SOURCES:
            raise ValueError(
                f"Flight.source must be one of {sorted(_VALID_SOURCES)}; got {self.source!r}"
            )
        if not self.dep_airport or not self.arr_airport:
            raise ValueError("Flight requires non-empty dep_airport and arr_airport IATA codes")
        # Freeze-safe normalization: uppercase IATA, UTC instants.
        set_ = object.__setattr__
        set_(self, "dep_airport", self.dep_airport.strip().upper())
        set_(self, "arr_airport", self.arr_airport.strip().upper())
        set_(self, "scheduled_dep", _as_utc(self.scheduled_dep, field_name="scheduled_dep"))
        if self.scheduled_arr is not None:
            set_(self, "scheduled_arr", _as_utc(self.scheduled_arr, field_name="scheduled_arr"))
        if self.live_dep is not None:
            set_(self, "live_dep", _as_utc(self.live_dep, field_name="live_dep"))
        if self.live_arr is not None:
            set_(self, "live_arr", _as_utc(self.live_arr, field_name="live_arr"))
        if self.source == BYAIR and self.byair_flight_id is None:
            raise ValueError("a byair-source Flight must carry byair_flight_id")
        if self.source == TRIPIT and self.tripit_segment_id is None:
            raise ValueError("a tripit-source Flight must carry tripit_segment_id")


@dataclass(frozen=True)
class MergedFlight:
    """One physical flight after collapsing all source records that share identity.

    Carries EVERY contributing source id so reconcile can map a legacy calendar
    block stamped with any one of them (`[flight-assist:flight=6277117]` or
    `=7166978`) back to this single flight during cutover. Times follow the
    byAir-wins rule: `scheduled_dep`/`scheduled_arr` and any live overlay come from
    a byAir member when one exists, else from TripIt.
    """

    dep_airport: str
    arr_airport: str
    scheduled_dep: datetime
    scheduled_arr: datetime | None
    live_dep: datetime | None
    live_arr: datetime | None
    code: str | None
    byair_flight_ids: frozenset[int] = field(default_factory=frozenset)
    tripit_segment_ids: frozenset[str] = field(default_factory=frozenset)
    trip_id: int | None = None

    @property
    def has_byair(self) -> bool:
        return bool(self.byair_flight_ids)

    @property
    def has_tripit(self) -> bool:
        return bool(self.tripit_segment_ids)

    @property
    def effective_dep(self) -> datetime:
        """Best-known departure instant: live byAir time when present, else scheduled."""
        return self.live_dep or self.scheduled_dep

    @property
    def effective_arr(self) -> datetime | None:
        return self.live_arr or self.scheduled_arr


def _merge_cluster(cluster: list[Flight]) -> MergedFlight:
    """Collapse a cluster of same-identity Flights into one MergedFlight.

    byAir wins on times (#156 Decision 4 / R2): scheduled and live instants come
    from a byAir member when the cluster has one, else from TripIt. When several
    byAir members exist (the dual-id codeshare case), pick deterministically by
    lowest `byair_flight_id` so the merge is order-independent, and retain every
    member id.
    """
    byair_members = sorted(
        (f for f in cluster if f.source == BYAIR), key=lambda f: f.byair_flight_id or 0
    )
    tripit_members = [f for f in cluster if f.source == TRIPIT]
    time_source = byair_members[0] if byair_members else tripit_members[0]
    display = next((f for f in cluster if f.code), None)

    return MergedFlight(
        dep_airport=time_source.dep_airport,
        arr_airport=time_source.arr_airport,
        scheduled_dep=time_source.scheduled_dep,
        scheduled_arr=time_source.scheduled_arr,
        live_dep=time_source.live_dep,
        live_arr=time_source.live_arr,
        code=display.code if display else None,
        byair_flight_ids=frozenset(
            f.byair_flight_id for f in byair_members if f.byair_flight_id is not None
        ),
        tripit_segment_ids=frozenset(
            f.tripit_segment_id for f in tripit_members if f.tripit_segment_id is not None
        ),
        trip_id=next((f.trip_id for f in cluster if f.trip_id is not None), None),
    )


def merge_flights(
    flights: list[Flight], *, tolerance: timedelta = DEFAULT_IDENTITY_TOLERANCE
) -> list[MergedFlight]:
    """Union byAir and TripIt flights into distinct physical flights (#156 W3 / R2).

    Two source records are the same physical flight iff they share (dep_airport,
    arr_airport) AND their scheduled departures fall within `tolerance`. The
    designator is NOT consulted, so codeshares (FR7382 / MW7382) and byAir's own
    dual ids collapse to one flight. A flight tracked by only one source still
    produces a MergedFlight (union, not intersection) so a byAir-only or
    TripIt-only flight is never dropped.

    Deterministic: output is ordered by (dep_airport, arr_airport, scheduled_dep);
    no clock, no randomness, no source id in the ordering key.
    """
    if tolerance < timedelta(0):
        raise ValueError(f"tolerance must be non-negative; got {tolerance}")

    by_route: dict[tuple[str, str], list[Flight]] = {}
    for f in flights:
        by_route.setdefault((f.dep_airport, f.arr_airport), []).append(f)

    merged: list[MergedFlight] = []
    for route in sorted(by_route):
        # Greedy clustering by scheduled-departure proximity. Sorted by instant, a
        # new cluster starts whenever the gap from the current cluster's ANCHOR
        # exceeds tolerance. Anchoring on the cluster's first instant (not the
        # previous one) prevents a chain of sub-tolerance steps from transitively
        # swallowing flights hours apart.
        route_flights = sorted(by_route[route], key=lambda f: f.scheduled_dep)
        cluster: list[Flight] = []
        anchor: datetime | None = None
        for f in route_flights:
            if anchor is not None and f.scheduled_dep - anchor > tolerance:
                merged.append(_merge_cluster(cluster))
                cluster = []
                anchor = None
            if anchor is None:
                anchor = f.scheduled_dep
            cluster.append(f)
        if cluster:
            merged.append(_merge_cluster(cluster))

    merged.sort(key=lambda m: (m.dep_airport, m.arr_airport, m.scheduled_dep))
    return merged
