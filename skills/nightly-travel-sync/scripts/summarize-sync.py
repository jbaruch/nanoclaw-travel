#!/usr/bin/env python3
"""Add a verified net report to sync JSON; preserve the original schedule.

Read one JSON object on stdin and emit it with timezoneDiff and reportLines.
Cancel equal delete/create tuples as a multiset. Reclaim endDate is exclusive;
expired rows are not new schedule changes. Pair a changed timezone only when
one removed and one added row share exact bounds. Never infer trip attribution
from a timezone row: OOO creation/deletion lines use the upstream name arrays.
Exit 2 with an actionable diagnostic if a report cannot be established.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def required_text(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone change needs timezone, from and to strings")
    return value


def calendar_date(home_timezone: str | None, instant: datetime) -> date:
    """Use the producer's home-calendar boundary, falling back to UTC."""
    try:
        zone = ZoneInfo(home_timezone) if home_timezone else UTC
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return instant.astimezone(zone).date()


def day(value: Any) -> date:
    """Parse a date-only contract field without echoing untrusted values."""
    if not isinstance(value, str):
        raise ValueError("date fields must use YYYY-MM-DD strings")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date fields must use valid YYYY-MM-DD strings") from None
    if parsed.isoformat() != value:
        raise ValueError("date fields must use YYYY-MM-DD strings")
    return parsed


def text_record(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) for key in fields):
        raise ValueError("sync output contains an incomplete or invalid string record")
    return value


def validate_success(result: dict[str, Any]) -> None:
    """Validate the complete public success shape before any host persistence."""
    required = {
        "mode",
        "noChanges",
        "homeTimezone",
        "timezoneChanges",
        "segments",
        "ooo",
        "conflicts",
    }
    if not required.issubset(result):
        raise ValueError("successful sync output is missing required fields")
    if (
        not isinstance(result["mode"], str)
        or not result["mode"]
        or type(result["noChanges"]) is not bool
        or not isinstance(result["homeTimezone"], (str, type(None)))
        or any(
            not isinstance(result[key], list)
            for key in ("segments", "conflicts", "timezoneChanges")
        )
    ):
        raise ValueError("successful sync output has invalid field types")
    for item in result["segments"]:
        row = text_record(item, ("timezone", "from", "to", "label"))
        if day(row["from"]) >= day(row["to"]):
            raise ValueError("segment date bounds must have a positive exclusive interval")
        for key in ("from_dt", "to_dt"):
            if key not in row or (row[key] is not None and not isinstance(row[key], str)):
                raise ValueError("segment instants must be ISO-8601 strings or null")
            if row[key] is not None:
                try:
                    parsed = datetime.fromisoformat(row[key])
                except ValueError:
                    raise ValueError("segment instants must be valid ISO-8601 strings") from None
                if parsed.tzinfo is None:
                    raise ValueError("segment instants must include a timezone offset")
    for item in result["conflicts"]:
        row = text_record(item, ("trip1", "trip2", "overlap"))
        day(row["overlap"])
    ooo = result["ooo"]
    if ooo is not None:
        counts = ("created", "deleted", "setToP2", "prioritySet")
        if not isinstance(ooo, dict) or any(
            type(ooo.get(key)) is not int or ooo[key] < 0 for key in counts
        ):
            raise ValueError("OOO counters must be nonnegative integers")
        if ooo["setToP2"] != ooo["prioritySet"]:
            raise ValueError("OOO priority aliases must agree")
        for key in ("createdNames", "deletedNames"):
            if not isinstance(ooo.get(key), list) or any(
                not isinstance(value, str) for value in ooo[key]
            ):
                raise ValueError("OOO names must be string arrays")


def summarize(result: dict[str, Any], today: date) -> dict[str, Any]:
    """Pure report transformation; today is injected for deterministic cutoffs."""
    changes = result.get("timezoneChanges", [])
    if not isinstance(changes, list):
        raise ValueError("timezoneChanges must be an array")
    created: Counter[tuple[str, str, str]] = Counter()
    deleted: Counter[tuple[str, str, str]] = Counter()
    for change in changes:
        if not isinstance(change, dict) or change.get("action") not in {"create", "delete"}:
            raise ValueError("unrecognized timezone change")

        zone, start, end = (required_text(change, key) for key in ("timezone", "from", "to"))
        start_date, end_date = day(start), day(end)
        if start_date >= end_date:
            raise ValueError("timezone change has invalid exclusive date bounds")
        if end_date <= today:
            continue
        target = created if change["action"] == "create" else deleted
        target[(zone, start, end)] += 1
    added = created - deleted
    removed = deleted - created
    modified = []
    bounds = sorted({(start, end) for _, start, end in added | removed})
    for start, end in bounds:
        before = [row for row in removed.elements() if row[1:] == (start, end)]
        after = [row for row in added.elements() if row[1:] == (start, end)]
        if len(before) == len(after) == 1:
            modified.append(
                {"from": start, "to": end, "oldTimezone": before[0][0], "newTimezone": after[0][0]}
            )
            removed[before[0]] -= 1
            added[after[0]] -= 1

    def rows(counter: Counter[tuple[str, str, str]]) -> list[dict[str, str]]:
        return [
            {"timezone": zone, "from": start, "to": end}
            for zone, start, end in sorted(counter.elements())
        ]

    diff = {"added": rows(added), "removed": rows(removed), "changed": modified}
    lines = []
    for key, verb in (("added", "добавлен"), ("removed", "удалён")):
        for row in diff[key]:
            lines.append(f"Часовой пояс: {verb} {row['timezone']} ({row['from']} → {row['to']}).")
    for row in modified:
        lines.append(
            f"Часовой пояс: {row['oldTimezone']} → {row['newTimezone']} "
            f"({row['from']} → {row['to']})."
        )
    ooo = result.get("ooo")
    if ooo is not None:
        if not isinstance(ooo, dict):
            raise ValueError("ooo must be an object or null")
        for action, verb in (("created", "создан"), ("deleted", "удалён")):
            count = ooo.get(action, 0)
            names = ooo.get(action + "Names", [])
            if (
                type(count) is not int
                or count < 0
                or not isinstance(names, list)
                or len(names) != count
                or any(not isinstance(name, str) or not name.strip() for name in names)
            ):
                raise ValueError(f"OOO {action} count does not match verified trip names")
            for name in names:
                lines.append(f"OOO: {verb} блок для поездки «{' '.join(name.split())}».")
        priority = ooo.get("prioritySet", 0)
        if type(priority) is not int or priority < 0:
            raise ValueError("OOO prioritySet must be a nonnegative integer")
        if priority:
            lines.append(f"OOO: приоритет P2 установлен для блоков: {priority}.")
    return {**result, "timezoneDiff": diff, "reportLines": lines}


def main() -> int:
    try:
        result = json.load(sys.stdin)
        if not isinstance(result, dict):
            raise ValueError("sync output must be a JSON object")
        errors = result.get("errors")
        if not isinstance(errors, list) or any(not isinstance(error, str) for error in errors):
            raise ValueError("sync output needs an errors array")
        if not errors:
            validate_success(result)
        output = summarize(result, calendar_date(result.get("homeTimezone"), datetime.now(UTC)))
    except (ValueError, TypeError) as error:
        print(
            f"sync-tripit: cannot verify the sync report ({error}). "
            "Inspect the sync JSON and upstream output contract before reporting changes.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
