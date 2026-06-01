"""Baseline tests for nightly-travel-sync/scripts/filter-tripit-bookings.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - stdin: JSON array of message objects (each with at least a `subject`
    field; optional `id`/`from`/`date` preserved on matches)
  - match rule: `subject.startswith(PREFIX)` where PREFIX is the script's
    forwarded-confirmation marker
  - stdout: JSON `{"matches": [...], "count": N}`
  - exit 0 on success
  - exit 1 with stderr diagnostic on invalid-JSON stdin or non-list root

Tests intercept stdin via a `_FakeStdin` wrapper so behaviour is
deterministic across pytest's stdin capture.
"""

import json


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _run(module, monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", _FakeStdin(stdin_text))
    monkeypatch.setattr("sys.argv", ["filter-tripit-bookings.py"])
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _msg(mid, subject, **extra):
    return {"id": mid, "subject": subject, **extra}


def test_empty_input_yields_zero_count(filter_tripit_bookings, monkeypatch, capsys):
    code, out, err = _run(filter_tripit_bookings, monkeypatch, capsys, "[]")
    assert code == 0
    assert err == ""
    assert json.loads(out) == {"matches": [], "count": 0}


def test_subject_prefix_match_includes_metadata(filter_tripit_bookings, monkeypatch, capsys):
    """Matching messages keep their full payload (id, from, date)."""
    prefix = filter_tripit_bookings.PREFIX
    msgs = [
        _msg(
            "m1",
            f"{prefix} ATL→SJO trip",
            **{"from": "noreply@tripit.com", "date": "2026-04-30"},
        ),
        _msg("m2", "Tripit Pro alert: Flight delay"),  # tripit-related, no prefix
        _msg("m3", "Some marketing note"),
    ]
    code, out, _ = _run(filter_tripit_bookings, monkeypatch, capsys, json.dumps(msgs))
    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["matches"][0]["id"] == "m1"
    assert payload["matches"][0]["from"] == "noreply@tripit.com"
    assert payload["matches"][0]["date"] == "2026-04-30"


def test_non_prefix_excluded_even_when_tripit_sender(filter_tripit_bookings, monkeypatch, capsys):
    """The script filters by SUBJECT prefix, not by sender. Tripit-Pro
    alerts and friend-shared trips MUST be excluded."""
    msgs = [
        _msg("a", "Tripit Pro: Your flight is delayed"),
        _msg("b", "Friend shared a trip with you"),
        _msg("c", "Geofenced check-in offer"),
    ]
    _, out, _ = _run(filter_tripit_bookings, monkeypatch, capsys, json.dumps(msgs))
    assert json.loads(out) == {"matches": [], "count": 0}


def test_non_dict_items_skipped(filter_tripit_bookings, monkeypatch, capsys):
    """`isinstance(m, dict)` guard protects against malformed entries."""
    prefix = filter_tripit_bookings.PREFIX
    msgs = [
        "not-a-dict",
        42,
        None,
        _msg("good", f"{prefix} a real booking"),
    ]
    code, out, _ = _run(filter_tripit_bookings, monkeypatch, capsys, json.dumps(msgs))
    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["matches"][0]["id"] == "good"


def test_non_string_subject_skipped(filter_tripit_bookings, monkeypatch, capsys):
    """`isinstance(subject, str)` guard — a numeric/null subject is skipped."""
    msgs = [{"id": "x", "subject": 42}, {"id": "y"}]
    code, out, _ = _run(filter_tripit_bookings, monkeypatch, capsys, json.dumps(msgs))
    assert code == 0
    assert json.loads(out)["count"] == 0


def test_invalid_json_exits_1(filter_tripit_bookings, monkeypatch, capsys):
    code, out, err = _run(filter_tripit_bookings, monkeypatch, capsys, "{ broken")
    assert code == 1
    assert out == ""
    assert "invalid JSON" in err


def test_non_list_root_exits_1(filter_tripit_bookings, monkeypatch, capsys):
    """Top-level object instead of array — must exit 1."""
    code, out, err = _run(filter_tripit_bookings, monkeypatch, capsys, '{"a": 1}')
    assert code == 1
    assert out == ""
    assert "must be a JSON array" in err
