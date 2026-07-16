"""Tests for nightly-travel-sync/scripts/fetch-tripit-emails.py.

Where the test boundary sits, and why
-------------------------------------
The four Gmail helpers this script composes (`google-rest`, `gmail-ops`,
`gmail-message`, `sanitize-email-body`) are owned and tested by
`nanoclaw-admin`'s heartbeat skill. This repo's CI checks out only this repo, so
they cannot be imported here — and vendoring them is forbidden
(`coding-policy: dependency-management`, No Vendoring). So these tests write
test doubles into a tmp dir and point `NANOCLAW_HEARTBEAT_SCRIPTS` at it, the
same env var the script uses for a dev clone.

What that leaves genuinely tested here is this script's own contract: the query
it sends, the fetch bound, the projection, the fail-closed load, and the exit
splits. The doubles are deliberately NOT re-implementations — the
`gmail_message` double asserts it was handed a real native Gmail resource
(RFC822 header LIST, base64url body in a NESTED MIME tree, `internalDate` as an
epoch-ms STRING) and fails the test if the script mangled it on the way through.
Parsing that shape is admin's contract, tested in admin.

Transport is exercised for real: the `google_rest` double issues an actual
`urllib` request against a local `http.server` bound to 127.0.0.1 on an
ephemeral port. No outbound network, no wall clock, no random input.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "nightly-travel-sync" / "scripts" / "fetch-tripit-emails.py"
FILTER = REPO_ROOT / "skills" / "nightly-travel-sync" / "scripts" / "filter-tripit-bookings.py"

# The prefix `filter-tripit-bookings.py` matches on, and `check-travel-freshness.py`
# emits as `subject_prefix`. Duplicated here as a fixture value, not imported —
# a test that reads the constant it is checking proves nothing.
TRIPIT_PREFIX = "Baruch, check out your TripIt itinerary for Fwd:"

SYNTH_QUERY = "from:tripit.com after:2026/07/01"


def _load_script():
    spec = importlib.util.spec_from_file_location("fetch_tripit_emails_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_module = _load_script()


# --- native Gmail fixture shapes -----------------------------------------


def _b64url(text: str) -> str:
    """Gmail base64url, unpadded — exactly how Gmail sends body parts."""
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _native_message(message_id: str, subject: str, sender: str = "TripIt <no-reply@tripit.com>"):
    """A Gmail `users.messages.get` resource in its REAL native shape.

    The details that matter, and that the Composio path hid: headers are the
    FULL raw RFC822 list (not a curated set), the body is base64url inside a
    NESTED MIME tree (multipart/mixed -> multipart/related -> text/html, with no
    text/plain part — the live-observed shape), and `internalDate` is epoch
    MILLISECONDS as a STRING, not an int and not an ISO stamp.
    """
    return {
        "id": message_id,
        "threadId": f"thread_{message_id}",
        "labelIds": ["INBOX"],
        "internalDate": "1783036800000",
        "snippet": "TripIt itinerary preview",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Delivered-To", "value": "synthetic@example.invalid"},
                {"name": "Received", "value": "by 2002:a05 with SMTP id x"},
                {"name": "ARC-Seal", "value": "i=1; a=rsa-sha256; t=1783036800"},
                {"name": "From", "value": sender},
                {"name": "To", "value": "synthetic@example.invalid"},
                {"name": "Subject", "value": subject},
            ],
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": _b64url("<p>itinerary</p>")},
                        }
                    ],
                }
            ],
        },
    }


# --- helper doubles -------------------------------------------------------

_GOOGLE_REST_DOUBLE = '''
"""Double for heartbeat's google-rest.py: a real urllib call to the fixture
server, with the same error taxonomy the script catches by attribute."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class GatewayNotInjecting(RuntimeError):
    pass


class TierAccessRestricted(RuntimeError):
    pass


def surface_url(surface, path):
    return f"{os.environ['FIXTURE_BASE_URL']}/{surface}/v1/{path.lstrip('/')}"


def google_request(method, url, *, params=None, body=None, timeout=30.0):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    request = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 401:
            raise GatewayNotInjecting(f"gateway not injecting: {detail}") from e
        if e.code == 403 and "access_restricted" in detail:
            raise TierAccessRestricted(f"tier restricted: {detail}") from e
        raise
    return json.loads(raw.decode("utf-8")) if raw else {}
'''

_GMAIL_OPS_DOUBLE = '''
"""Double for heartbeat's gmail-ops.py — the same list/get split and the same
keyword contract the script binds against."""


def list_messages(google_request, *, limit, label_ids=None, query=None,
                  include_spam_trash=False, surface_url):
    params = {"maxResults": limit, "includeSpamTrash": str(bool(include_spam_trash)).lower()}
    if query:
        params["q"] = query
    resp = google_request("GET", surface_url("gmail", "users/me/messages"), params=params)
    messages = resp.get("messages")
    return messages if isinstance(messages, list) else []


def get_message(google_request, message_id, *, fmt="full", surface_url):
    return google_request(
        "GET", surface_url("gmail", f"users/me/messages/{message_id}"), params={"format": fmt}
    )
'''

# The gmail_message double does NOT re-implement admin's parser. It asserts the
# script handed it an untouched NATIVE resource, then returns a parsed row built
# only from fields it verified are present in that native shape. If the script
# ever pre-digests the resource, or hands over something else, this fails loudly
# rather than passing on a shape admin's real parser would reject.
_GMAIL_MESSAGE_DOUBLE = '''
"""Double for heartbeat's gmail-message.py. Verifies the native shape it was
handed, then returns a row whose text fields went through `sanitize`."""


def _header(payload, name):
    for h in payload.get("headers") or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def parse_message(raw, sanitize):
    if not isinstance(raw, dict):
        return {}
    # Native-shape assertions: these are what the Composio envelope used to
    # hide, so a regression that reintroduces pre-flattening trips here.
    assert isinstance(raw.get("internalDate"), str), "internalDate must be an epoch-ms STRING"
    payload = raw.get("payload") or {}
    assert isinstance(payload.get("headers"), list), "headers must be the raw RFC822 LIST"
    assert "parts" in payload, "body must arrive as a nested MIME tree"
    return {
        "messageId": raw.get("id") or "",
        "threadId": raw.get("threadId") or "",
        "internalDate": "2026-07-01T12:00:00+00:00",
        "from": sanitize(_header(payload, "From"), max_len=300),
        "subject": sanitize(_header(payload, "Subject"), max_len=300),
        "snippet": sanitize(raw.get("snippet") or ""),
        "body": sanitize(""),
    }
'''

# A real-enough sanitizer double: it records what it was called with (so a test
# can prove every emitted field went through it) and drops the invisible padding
# from the 2026-04-24 incident, so the poison-defense test exercises a genuine
# transformation rather than an identity function.
_SANITIZE_DOUBLE = '''
"""Double for heartbeat's sanitize-email-body.py."""
import re
import unicodedata

CALLS = []

_KEEP = frozenset("\\t\\n\\r")
_DROP_CATEGORIES = frozenset(("Cf", "Cc", "Cs"))
_NONASCII_RUN = re.compile(r"([^\\x00-\\x7f])(?:\\s*\\1){3,}")


def sanitize(s, max_len=2000):
    if not isinstance(s, str):
        return s
    CALLS.append(s)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(
        c for c in s
        if c in _KEEP or unicodedata.category(c) not in _DROP_CATEGORIES
    )
    s = _NONASCII_RUN.sub(r"\\1", s)
    s = re.sub(r"\\s+", " ", s).strip()
    return s if len(s) <= max_len else s[:max_len] + " [...]"
'''

_DOUBLES = {
    "google-rest.py": _GOOGLE_REST_DOUBLE,
    "gmail-ops.py": _GMAIL_OPS_DOUBLE,
    "gmail-message.py": _GMAIL_MESSAGE_DOUBLE,
    "sanitize-email-body.py": _SANITIZE_DOUBLE,
}


@pytest.fixture
def heartbeat_scripts(tmp_path, monkeypatch):
    """A dir of helper doubles, wired in via NANOCLAW_HEARTBEAT_SCRIPTS.

    Returns the Path so a test can delete one file and exercise fail-closed.
    """
    scripts = tmp_path / "heartbeat_scripts"
    scripts.mkdir()
    for filename, source in _DOUBLES.items():
        (scripts / filename).write_text(source)
    monkeypatch.setenv("NANOCLAW_HEARTBEAT_SCRIPTS", str(scripts))
    return scripts


@pytest.fixture
def gmail_server(monkeypatch):
    """A local Gmail-shaped HTTP server. `state` drives what it answers."""
    state: dict = {"messages": [], "status": 200, "body": None, "requests": []}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's API
            split = urlsplit(self.path)
            state["requests"].append((split.path, dict(parse_qsl(split.query))))
            if state["status"] != 200:
                self.send_response(state["status"])
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(state["body"] or b"{}")
                return
            if split.path.endswith("/messages"):
                # Honour maxResults the way Gmail does — cap the page and say
                # nothing about the remainder. That is what makes the
                # truncation tests real rather than staged: the script sees
                # exactly what Gmail would hand it, a full page and no hint
                # that more exist.
                params = dict(parse_qsl(split.query))
                limit = int(params.get("maxResults", len(state["messages"])))
                payload = {
                    "messages": [
                        {"id": m["id"], "threadId": m["threadId"]}
                        for m in state["messages"][:limit]
                    ]
                }
            else:
                message_id = split.path.rsplit("/", 1)[-1]
                found = [m for m in state["messages"] if m["id"] == message_id]
                payload = found[0] if found else {}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, format, *args):  # noqa: A002 - matches the base signature
            pass  # keep the test output clean

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    # poll_interval bounds how long shutdown() blocks; the 0.5s default
    # would add half a second of teardown to every test using this fixture.
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    monkeypatch.setenv("FIXTURE_BASE_URL", f"http://{host}:{port}")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run(query: str = SYNTH_QUERY) -> tuple[int, str, str]:
    """Run main() in-process, capturing stdout/stderr via a subprocess-free path."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fetch_module.main(["fetch-tripit-emails.py", query])
    return code, out.getvalue(), err.getvalue()


# --- request shaping ------------------------------------------------------


def test_query_is_passed_through_verbatim(heartbeat_scripts, gmail_server):
    """The query comes from check-travel-freshness.py already `after:`-buffered.
    This script must not rebuild or widen it."""
    gmail_server["messages"] = []
    code, _, _ = _run()
    assert code == 0
    list_path, params = gmail_server["requests"][0]
    assert list_path.endswith("/users/me/messages")
    assert params["q"] == SYNTH_QUERY


def test_fetch_is_bounded_and_does_not_paginate(heartbeat_scripts, gmail_server):
    """The bound is what keeps the per-message `get` count flat (nanoclaw#656)."""
    gmail_server["messages"] = []
    _run()
    _, params = gmail_server["requests"][0]
    assert params["maxResults"] == str(fetch_module.MAX_RESULTS)
    assert "pageToken" not in params


def test_messages_are_fetched_as_metadata_not_full(heartbeat_scripts, gmail_server):
    """This probe reads only the subject, so pulling bodies would be wasted
    bandwidth AND untrusted text with no reader."""
    gmail_server["messages"] = [_native_message("m1", f"{TRIPIT_PREFIX} DL123")]
    _run()
    get_requests = [r for r in gmail_server["requests"] if "/messages/" in r[0]]
    assert [params["format"] for _, params in get_requests] == ["metadata"]


def test_list_then_get_is_n_plus_one(heartbeat_scripts, gmail_server):
    """Native Gmail lists stubs only; each message costs its own get."""
    gmail_server["messages"] = [
        _native_message("m1", f"{TRIPIT_PREFIX} DL123"),
        _native_message("m2", "TripIt Pro alert"),
    ]
    _run()
    assert len(gmail_server["requests"]) == 3  # 1 list + 2 gets


# --- projection -----------------------------------------------------------


def test_projects_the_fields_the_filter_consumes(heartbeat_scripts, gmail_server):
    gmail_server["messages"] = [_native_message("m1", f"{TRIPIT_PREFIX} DL123")]
    code, out, _ = _run()
    assert code == 0
    rows = json.loads(out)
    assert rows == [
        {
            "id": "m1",
            "from": "TripIt <no-reply@tripit.com>",
            "subject": f"{TRIPIT_PREFIX} DL123",
            "date": "2026-07-01T12:00:00+00:00",
        }
    ]


def test_bodies_and_snippets_are_not_projected(heartbeat_scripts, gmail_server):
    """Every field omitted is untrusted text that never reaches the agent.
    Nothing downstream reads a body, so none is emitted."""
    gmail_server["messages"] = [_native_message("m1", f"{TRIPIT_PREFIX} DL123")]
    _, out, _ = _run()
    [row] = json.loads(out)
    assert "body" not in row and "snippet" not in row


def test_empty_window_emits_an_empty_array(heartbeat_scripts, gmail_server):
    gmail_server["messages"] = []
    code, out, _ = _run()
    assert code == 0
    assert json.loads(out) == []


def test_output_is_a_single_line_json_array(heartbeat_scripts, gmail_server):
    """The filter reads a JSON array on stdin — not an object, not multi-line."""
    gmail_server["messages"] = [_native_message("m1", f"{TRIPIT_PREFIX} DL123")]
    _, out, _ = _run()
    assert "\n" not in out.strip()
    assert isinstance(json.loads(out), list)


# --- poison defense -------------------------------------------------------


def test_subject_is_sanitized_before_it_leaves_the_container(heartbeat_scripts, gmail_server):
    """The 2026-04-24 incident shape: invisible-Unicode padding in third-party
    text. The agent reads this script's stdout, so the padding must be gone
    BEFORE it is printed — that is the whole reason the fetch moved in-container
    instead of staying an agent-driven tool call.
    """
    poisoned = f"{TRIPIT_PREFIX} DL123ϏϏϏϏϏ‌‌‌"
    gmail_server["messages"] = [_native_message("m1", poisoned)]
    code, out, _ = _run()
    assert code == 0
    [row] = json.loads(out)
    assert "‌" not in row["subject"]  # ZWNJ dropped
    assert row["subject"].count("Ϗ") <= 1  # padding run collapsed
    # and the real prefix survived, so the filter still matches it
    assert row["subject"].startswith(TRIPIT_PREFIX)


def test_missing_sanitizer_fails_closed(heartbeat_scripts, gmail_server):
    """No sanitizer means no fetch. Emitting unsanitized output would defeat
    the entire point of the script, so this must never degrade to a fetch."""
    (heartbeat_scripts / "sanitize-email-body.py").unlink()
    gmail_server["messages"] = [_native_message("m1", f"{TRIPIT_PREFIX} DL123")]
    code, out, err = _run()
    assert code == 2
    assert out == ""  # nothing emitted at all
    assert not gmail_server["requests"]  # and nothing was even fetched
    assert "sanitize-email-body.py" in err
    assert "Refusing to fetch" in err


@pytest.mark.parametrize(
    "missing",
    ["google-rest.py", "gmail-ops.py", "gmail-message.py", "sanitize-email-body.py"],
)
def test_any_missing_helper_fails_closed(heartbeat_scripts, gmail_server, missing):
    (heartbeat_scripts / missing).unlink()
    code, out, err = _run()
    assert code == 2
    assert out == ""
    assert missing in err


def test_missing_mount_entirely_fails_closed(monkeypatch, gmail_server):
    """No co-loaded heartbeat tile at all — the message must name the fix."""
    monkeypatch.setenv("NANOCLAW_HEARTBEAT_SCRIPTS", "/nonexistent/synthetic/path")
    code, out, err = _run()
    assert code == 2
    assert out == ""
    assert "additionalTiles" in err


# --- config failures ------------------------------------------------------


def test_gateway_not_injecting_exits_two(heartbeat_scripts, gmail_server):
    """401: the gateway is not authenticating us. Operator-actionable config,
    not a per-message error — so it exits rather than emitting a partial list."""
    gmail_server["status"] = 401
    gmail_server["body"] = b'{"error": "invalid_credentials"}'
    code, out, err = _run()
    assert code == 2
    assert out == ""
    assert "unauthenticated" in err


def test_tier_restricted_exits_two(heartbeat_scripts, gmail_server):
    """403 + access_restricted: this tier is gated from Google by design."""
    gmail_server["status"] = 403
    gmail_server["body"] = b'{"error": "access_restricted"}'
    code, out, err = _run()
    assert code == 2
    assert out == ""
    assert "tier" in err


def test_call_failure_exits_nonzero_without_partial_output(heartbeat_scripts, gmail_server):
    """A 5xx means the list is not a sound basis for "no booking found". The
    probe reports no-match by emitting an empty array, so a partial list would
    read as a false silence — the one outcome this step exists to prevent.
    """
    gmail_server["status"] = 500
    gmail_server["body"] = b'{"error": "backend error"}'
    code, out, err = _run()
    assert code == 1
    assert out == ""
    assert "no list emitted" in err


def test_usage_error_exits_two(heartbeat_scripts):
    for argv in ([], ["fetch-tripit-emails.py"], ["fetch-tripit-emails.py", "   "]):
        assert fetch_module.main(argv) == 2


# --- truncation: a bound this probe hit must never look like a clean zero ---


def test_full_page_signals_truncation(heartbeat_scripts, gmail_server, monkeypatch):
    """A list that comes back AT the cap means older mail went unexamined.

    Gmail says nothing about the remainder (`gmail-ops.list_messages` drops
    `nextPageToken`), so a full page is the only evidence available — and it is
    enough to know the answer is not trustworthy.
    """
    monkeypatch.setattr(fetch_module, "MAX_RESULTS", 2)
    gmail_server["messages"] = [
        _native_message("m1", "TripIt Pro: your flight is delayed"),
        _native_message("m2", "Check out these travel deals"),
        _native_message("m3", f"{TRIPIT_PREFIX} DL123"),  # behind the cap, unseen
    ]
    code, out, err = _run()

    assert code == fetch_module.EXIT_TRUNCATED
    assert fetch_module.TRUNCATION_MARKER in err
    # stdout is still real, and still the filter's contract — it is just partial
    rows = json.loads(out)
    assert [r["id"] for r in rows] == ["m1", "m2"]


def test_truncated_empty_result_is_distinguishable_from_a_clean_one(
    heartbeat_scripts, gmail_server, monkeypatch
):
    """THE invariant. Both runs print `[]`; only one of them means
    "no booking confirmation arrived". If these two were indistinguishable, a
    truncated window would read as silence — and because the window is
    `after:<schedule mtime>`, that silence keeps the schedule stale, which
    widens the window, which makes the next truncation likelier. Stale ->
    wider -> more truncation -> staler, forever, in silence.
    """
    monkeypatch.setattr(fetch_module, "MAX_RESULTS", 2)

    def _count(stdout: str) -> int:
        """What Step 3 actually reads: the real filter's verdict."""
        result = subprocess.run(
            [sys.executable, str(FILTER)], input=stdout, capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)["count"]

    # Truncated: two non-matching messages fill the page, and the confirmation
    # sits behind it — unseen.
    gmail_server["messages"] = [
        _native_message("m1", "TripIt Pro alert"),
        _native_message("m2", "Travel deals"),
        _native_message("m3", f"{TRIPIT_PREFIX} DL123"),
    ]
    truncated_code, truncated_out, truncated_err = _run()

    # Clean: non-matching mail only, window fully seen. There is genuinely no
    # confirmation to find.
    gmail_server["messages"] = [_native_message("m1", "TripIt Pro alert")]
    clean_code, clean_out, clean_err = _run()

    # The filter — Step 3's only view of the data — cannot tell these apart:
    # both are a zero match count. One is "no booking arrived"; the other is
    # "a booking arrived and we didn't look at it".
    assert _count(truncated_out) == 0
    assert _count(clean_out) == 0

    # So the distinction MUST live outside the data, or the alert is lost:
    assert truncated_code == fetch_module.EXIT_TRUNCATED
    assert clean_code == fetch_module.EXIT_OK
    assert truncated_code != clean_code
    assert fetch_module.TRUNCATION_MARKER in truncated_err
    assert fetch_module.TRUNCATION_MARKER not in clean_err


def test_under_cap_is_not_truncated(heartbeat_scripts, gmail_server, monkeypatch):
    """One short of the cap proves the window was fully drained."""
    monkeypatch.setattr(fetch_module, "MAX_RESULTS", 3)
    gmail_server["messages"] = [
        _native_message("m1", "TripIt Pro alert"),
        _native_message("m2", "Travel deals"),
    ]
    code, _, err = _run()
    assert code == fetch_module.EXIT_OK
    assert fetch_module.TRUNCATION_MARKER not in err


def test_truncation_still_reports_the_matches_it_did_see(
    heartbeat_scripts, gmail_server, monkeypatch
):
    """A match found inside a partial window is still a real match — the blind
    spot is reported ALONGSIDE it, not instead of it."""
    monkeypatch.setattr(fetch_module, "MAX_RESULTS", 2)
    gmail_server["messages"] = [
        _native_message("m1", f"{TRIPIT_PREFIX} DL123"),
        _native_message("m2", "TripIt Pro alert"),
        _native_message("m3", "Older mail behind the cap"),
    ]
    code, out, _ = _run()
    assert code == fetch_module.EXIT_TRUNCATED

    result = subprocess.run(
        [sys.executable, str(FILTER)], input=out, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout)["count"] == 1


def test_truncation_diagnostic_is_actionable(heartbeat_scripts, gmail_server, monkeypatch):
    """The operator note has to say what was NOT looked at, not just that
    something went wrong."""
    monkeypatch.setattr(fetch_module, "MAX_RESULTS", 2)
    gmail_server["messages"] = [_native_message(f"m{i}", "TripIt Pro alert") for i in range(3)]
    _, _, err = _run()
    assert "not the whole window" in err
    assert "may be sitting behind the cap" in err


def test_empty_window_is_never_truncated(heartbeat_scripts, gmail_server):
    """The genuine no-mail case: zero is the whole window, so it stays silent."""
    gmail_server["messages"] = []
    code, out, err = _run()
    assert code == fetch_module.EXIT_OK
    assert json.loads(out) == []
    assert fetch_module.TRUNCATION_MARKER not in err


# --- end-to-end with the real filter --------------------------------------


def test_output_feeds_the_real_filter(heartbeat_scripts, gmail_server):
    """The contract that matters: this script's stdout is the filter's stdin.
    Runs the REAL filter, unchanged, over the real output."""
    gmail_server["messages"] = [
        _native_message("m1", f"{TRIPIT_PREFIX} DL123 BNA-JFK"),
        _native_message("m2", "TripIt Pro: your flight is delayed"),  # not a confirmation
    ]
    _, out, _ = _run()

    result = subprocess.run(
        [sys.executable, str(FILTER)], input=out, capture_output=True, text=True, check=True
    )
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["matches"][0]["subject"] == f"{TRIPIT_PREFIX} DL123 BNA-JFK"
    assert payload["matches"][0]["id"] == "m1"


def test_no_confirmation_in_window_yields_a_silent_zero(heartbeat_scripts, gmail_server):
    """Step 3 alerts only on count >= 1; other tripit.com mail must not fire it."""
    gmail_server["messages"] = [
        _native_message("m1", "TripIt Pro: your flight is delayed"),
        _native_message("m2", "Check out these travel deals"),
    ]
    _, out, _ = _run()
    result = subprocess.run(
        [sys.executable, str(FILTER)], input=out, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout)["count"] == 0
