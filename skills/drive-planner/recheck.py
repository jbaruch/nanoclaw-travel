"""Decide whether a scheduled drive recheck should ping the user — the gate.

After drive-planner creates a drive block it schedules T-45 / T-30 / T-15
rechecks (Epic #59 §3). Each recheck re-routes the leg with live traffic
and then has to answer one question: *did traffic grow enough since the
baseline that the user needs to leave earlier — or is it already time to
go?* Most rechecks are no-ops; pinging on every trivial fluctuation is the
trust-eroding failure mode (lombot #49 in spirit). So the recheck is a
gate: alert only when the growth crosses a threshold, or when the freshly
recomputed leave-by has already arrived.

This module is the deterministic core (per `coding-policy: script-
delegation` — a pure function of baseline seconds, current seconds, and the
deadline). It does NOT route: getting `current_seconds` needs live traffic
(`maps_client`) and is the caller's job. `evaluate_recheck()` takes the two
durations plus the arrive-by deadline and returns a `RecheckDecision`.

Alert when EITHER:
    - the drive grew at least `threshold_seconds` over the baseline
      (DEFAULT_ALERT_THRESHOLD_SECONDS), or
    - the recomputed leave-by (`arrive_by − current − buffer`) is at or
      before `now` — you must leave now, regardless of growth.

The CLI follows the precheck-gating contract (per `coding-policy:
script-delegation` Precheck Gating): it emits `{"wake_agent": <alert>,
"data": {<decision>}}` so a scheduler can run it and only wake the agent
when `wake_agent` is true, with `data` carrying what the agent needs to
compose the ping.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from recheck import evaluate_recheck, RecheckDecision, RecheckError

    decision = evaluate_recheck(
        baseline_seconds=1500,        # drive time when the block was created
        current_seconds=2300,         # drive time just re-routed, live
        arrive_by=datetime(...),      # the meeting start (tz-aware)
        now=datetime.now(tz=...),     # tz-aware
    )
    if decision.alert:
        ...  # ping: leave earlier / leave now

CLI (precheck-gating contract):
    echo '{"baseline_seconds": 1500, "current_seconds": 2300,
           "arrive_by": "...", "now": "..."}' | python recheck.py
    # stdout: {"wake_agent": true, "data": {<RecheckDecision dict>}}; exit 0
    # stderr: {"error": "..."} + non-zero exit on bad input
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

# Minimum growth over the baseline drive that is worth a ping. Below this,
# the recheck stays silent — a couple of minutes of jitter is noise, not a
# reason to interrupt. A black-box constant (per `coding-policy:
# script-as-black-box`); callers override via `threshold_seconds=`.
DEFAULT_ALERT_THRESHOLD_SECONDS = 10 * 60

# Slack subtracted from the deadline so the user aims to arrive a little
# early, not exactly at the meeting start. Folded into the leave-by:
# leave_by = arrive_by − current_drive − buffer.
DEFAULT_ARRIVAL_BUFFER_SECONDS = 5 * 60


class RecheckError(ValueError):
    """Raised on a malformed recheck input the caller must fix.

    A ValueError subclass — the fix is "pass well-formed inputs" (non-
    negative integer durations, tz-aware datetimes), not "retry".
    """


@dataclass(frozen=True)
class RecheckDecision:
    """The outcome of one recheck gate evaluation.

    Fields:
        alert: ping the user — traffic grew past the threshold OR the
            recomputed leave-by is at/after now
        grew_past_threshold: the drive grew by at least threshold_seconds
        leave_by_passed: now is at or after the recomputed leave-by
        delta_seconds: current_seconds − baseline_seconds (negative when
            traffic improved)
        baseline_seconds: the drive time captured when the block was created
        current_seconds: the freshly re-routed drive time
        new_leave_by: arrive_by − current_seconds − buffer_seconds
        seconds_until_leave_by: whole seconds from now to new_leave_by
            (negative once leave-by has passed)
        reason: short, audit-friendly explanation of the alert decision
    """

    alert: bool
    grew_past_threshold: bool
    leave_by_passed: bool
    delta_seconds: int
    baseline_seconds: int
    current_seconds: int
    new_leave_by: datetime
    seconds_until_leave_by: int
    reason: str


def _require_non_negative_int(value: object, name: str) -> int:
    """Return value as an int, or raise RecheckError. bool is rejected."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecheckError(f"`{name}` must be a non-negative integer (got {value!r})")
    return value


def _require_aware(value: object, name: str) -> datetime:
    """Return a tz-aware datetime, or raise RecheckError.

    A naive datetime can't be compared to the other tz-aware datetimes
    without raising, so it is rejected at the boundary with an actionable
    message rather than allowed to surface as a TypeError later.
    """
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecheckError(f"`{name}` must be a timezone-aware datetime (got {value!r})")
    return value


def evaluate_recheck(
    *,
    baseline_seconds: int,
    current_seconds: int,
    arrive_by: datetime,
    now: datetime,
    buffer_seconds: int = DEFAULT_ARRIVAL_BUFFER_SECONDS,
    threshold_seconds: int = DEFAULT_ALERT_THRESHOLD_SECONDS,
) -> RecheckDecision:
    """Decide whether this recheck should ping. Pure; no I/O.

    Args:
        baseline_seconds: drive time captured when the block was created.
        current_seconds: drive time just re-routed with live traffic.
        arrive_by: the meeting start / hard arrival deadline (tz-aware).
        now: tz-aware current time.
        buffer_seconds: arrive-early slack folded into the leave-by.
        threshold_seconds: minimum growth over baseline that warrants a ping.

    Returns:
        RecheckDecision.

    Raises:
        RecheckError: on a negative / non-integer duration or a naive /
            non-datetime arrive_by or now.
    """
    baseline_seconds = _require_non_negative_int(baseline_seconds, "baseline_seconds")
    current_seconds = _require_non_negative_int(current_seconds, "current_seconds")
    buffer_seconds = _require_non_negative_int(buffer_seconds, "buffer_seconds")
    threshold_seconds = _require_non_negative_int(threshold_seconds, "threshold_seconds")
    arrive_by = _require_aware(arrive_by, "arrive_by")
    now = _require_aware(now, "now")

    delta_seconds = current_seconds - baseline_seconds
    grew_past_threshold = delta_seconds >= threshold_seconds

    new_leave_by = arrive_by - timedelta(seconds=current_seconds + buffer_seconds)
    seconds_until_leave_by = int((new_leave_by - now).total_seconds())
    leave_by_passed = seconds_until_leave_by <= 0

    alert = grew_past_threshold or leave_by_passed
    if leave_by_passed and grew_past_threshold:
        reason = "traffic grew past threshold and the leave-by has arrived"
    elif leave_by_passed:
        reason = "leave-by has arrived"
    elif grew_past_threshold:
        reason = "traffic grew past threshold"
    else:
        reason = "no significant change"

    return RecheckDecision(
        alert=alert,
        grew_past_threshold=grew_past_threshold,
        leave_by_passed=leave_by_passed,
        delta_seconds=delta_seconds,
        baseline_seconds=baseline_seconds,
        current_seconds=current_seconds,
        new_leave_by=new_leave_by,
        seconds_until_leave_by=seconds_until_leave_by,
        reason=reason,
    )


def _decision_to_dict(decision: RecheckDecision) -> dict:
    """JSON-serializable view of a RecheckDecision (datetime → ISO-8601)."""
    return {
        "alert": decision.alert,
        "grew_past_threshold": decision.grew_past_threshold,
        "leave_by_passed": decision.leave_by_passed,
        "delta_seconds": decision.delta_seconds,
        "baseline_seconds": decision.baseline_seconds,
        "current_seconds": decision.current_seconds,
        "new_leave_by": decision.new_leave_by.isoformat(),
        "seconds_until_leave_by": decision.seconds_until_leave_by,
        "reason": decision.reason,
    }


def _parse_iso(raw: object) -> datetime | None:
    """Parse an ISO-8601 / RFC3339 string into a tz-aware datetime, or None.

    Normalizes a trailing `Z` to `+00:00` and rejects a naive result (which
    can't be compared to the other tz-aware times) — matching scan.py's
    boundary parsing.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def main() -> int:
    """CLI wrapper around `evaluate_recheck` — the precheck-gating contract.

    stdin: a JSON object
        {"baseline_seconds": <int>, "current_seconds": <int>,
         "arrive_by": "<tz-aware ISO-8601>", "now": "<tz-aware ISO-8601>",
         "buffer_seconds": <int, optional>, "threshold_seconds": <int, optional>}
    stdout: {"wake_agent": <alert>, "data": {<RecheckDecision dict>}} (exit 0)
    stderr: {"error": "..."} with a non-zero exit on invalid JSON, a missing
        / naive datetime, or any non-integer / negative duration.
    """
    try:
        request = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON on stdin: {exc}"}), file=sys.stderr)
        return 1
    if not isinstance(request, dict):
        print(json.dumps({"error": "stdin must be a JSON object"}), file=sys.stderr)
        return 1

    arrive_by = _parse_iso(request.get("arrive_by"))
    now = _parse_iso(request.get("now"))
    for label, value in (("arrive_by", arrive_by), ("now", now)):
        if value is None:
            print(
                json.dumps({"error": f"`{label}` must be a timezone-aware ISO-8601 string"}),
                file=sys.stderr,
            )
            return 1

    kwargs = {
        "baseline_seconds": request.get("baseline_seconds"),
        "current_seconds": request.get("current_seconds"),
        "arrive_by": arrive_by,
        "now": now,
    }
    if request.get("buffer_seconds") is not None:
        kwargs["buffer_seconds"] = request.get("buffer_seconds")
    if request.get("threshold_seconds") is not None:
        kwargs["threshold_seconds"] = request.get("threshold_seconds")

    try:
        decision = evaluate_recheck(**kwargs)
    except RecheckError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    payload = {"wake_agent": decision.alert, "data": _decision_to_dict(decision)}
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
