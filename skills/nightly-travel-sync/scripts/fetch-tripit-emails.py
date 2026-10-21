#!/usr/bin/env python3
"""Fetch + sanitize TripIt confirmation emails natively, for the freshness probe.

Native Gmail REST via the OneCLI gateway (nanoclaw#638), replacing the
`COMPOSIO_SEARCH_TOOLS` -> `GMAIL_FETCH_EMAILS` path SKILL.md Step 3 used to
drive from the agent. This container holds NO Google credential: the gateway
injects the `Authorization: Bearer` on the wire to Google and refreshes it (see
heartbeat's `google-rest.py`). Nothing here reads a key from the environment.

Why this script exists at all (the security reason)
---------------------------------------------------
The step it replaces had the AGENT call the Gmail tool and hand-build the JSON
array. That put raw Gmail subjects — untrusted third-party text — straight into
the session transcript, which is exactly the hole
`/workspace/group/nanoclaw-poison-defense.md` exists to close (2026-04-24: a
maintenance session was killed by invisible-Unicode padding in a marketing
preview). Fetching AND sanitizing in-container means only this script's
sanitized stdout reaches the agent. That invariant is why a missing sanitizer
is fatal here rather than degraded: emitting unsanitized output would defeat
the entire point of the script.

The Composio path was also latently broken. `COMPOSIO_SEARCH_TOOLS` returns a
recommended PLAN, not a callable slug (nanoclaw#649), so the tool-discovery
line could not have worked — it just never screamed, because this branch only
fires when the schedule goes stale (>7 days).

Cross-tile dependency
---------------------
The four helpers below are owned and tested by `nanoclaw-admin`'s heartbeat
skill and consumed over the co-loaded `tessl__heartbeat` mount — the same way
`nanoclaw-orders`' `fetch-order-emails.py` consumes them. Unlike the calendar
client (which stays self-contained in this plugin, because travel owns its
per-service clients), Gmail is not travel's domain: re-implementing RFC822 MIME
parsing and the poison sanitizer here would be strictly worse than depending on
the one tested copy. admin co-loads in the owner's chat, where this runs.

The list/get split (N+1)
------------------------
Composio's `GMAIL_FETCH_EMAILS` returned full messages for a query in one call.
Native `users.messages.list` answers a query with `{id, threadId}` stubs only,
so every message costs a second `get`. The fetch is bounded by MAX_RESULTS and
does NOT paginate — `gmail-ops.list_messages` deliberately refuses to (the
unbounded-crawl shape that drained a container in nanoclaw#656).

Messages are fetched with `format=metadata`: this probe reads only the subject
line, so there is no reason to pull message bodies over the wire, and not
pulling them keeps that much untrusted text out of the container entirely.

A bounded fetch can only be honest if it admits its bound
---------------------------------------------------------
The query is sender-scoped (`from:tripit.com after:<schedule mtime>`), never
subject-scoped: the confirmation prefix is `filter-tripit-bookings.py`'s
authority, and pushing it into the Gmail query would hand the match to Gmail's
fuzzy `subject:` tokenizer, where a miss becomes a false silence nothing
downstream can see.

So the window holds all of TripIt's mail, and the cap can hide a confirmation
behind a burst of Pro alerts or marketing. That is not merely a missed alert —
it feeds back. The window is `after:<mtime>`, so a missed confirmation leaves
the schedule stale, which WIDENS the window, which makes the next truncation
likelier: stale -> wider -> more truncation -> staler, converging on
permanently broken with nothing ever surfaced. It is nanoclaw#171's truncation
shape (a partial view read as the whole one) wearing different clothes.

The fix is not a bigger cap — any cap can be exceeded. It is that this script
must never report a bound it hit as if it had seen everything. When the list
comes back full, `EXIT_TRUNCATED` + the `WINDOW_TRUNCATED` marker say so, and
SKILL.md Step 3 reports the blind spot instead of applying its silent-on-zero
rule. Silence is only ever correct when the whole window was actually seen.

Usage
-----
    fetch-tripit-emails.py '<gmail_query>'

`gmail_query` is the one `check-travel-freshness.py` emits on a `stale` status,
already buffered for Gmail's `after:` boundary semantics. Passed through
verbatim — this script does not build or widen it.

Output
------
A single-line JSON ARRAY on stdout — the shape `filter-tripit-bookings.py`
consumes on stdin, unchanged by this migration:

    [{"id", "from", "subject", "date"}, ...]

`subject` is the field the filter matches; `id` / `from` / `date` are carried
so the alert can show metadata. Bodies and snippets are deliberately NOT
projected: nothing downstream reads them, and every field omitted is untrusted
text that never reaches the agent.

The array says what was FOUND; the exit code says whether that is the whole
story. Both matter — see below.

Exit codes
----------
  0 — the whole window was examined; the array is complete. Only on this path
      may a zero match count be read as "no booking confirmation arrived".
  3 — EXIT_TRUNCATED. The array on stdout is REAL but PARTIAL: the list came
      back at its cap, so older mail in the window was never examined. stdout
      is still emitted (the rows found are genuine, and a match found in them
      is still a match) — what changes is that a zero count proves nothing.
      Step 3 must report the blind spot. See the section above.
  2 — a shared helper could not be loaded (fail closed), a usage error, or an
      operator-actionable config failure: the gateway is not injecting (401) or
      this agent's tier is gated from Google (403). Same split as
      `flight-assist/scripts/reconcile.py`'s {"error": "gateway"|"tier"}.
      No stdout.
  1 — a Gmail call failed. Deliberately fatal rather than partial: this probe's
      whole job is to decide whether a booking was missed, and it reports "no
      match" by returning an empty array. A list or get we could not complete
      means the array is not a sound basis for that conclusion. The surviving
      messages are discarded and the next nightly fire retries. No stdout.

Every non-zero path writes an actionable diagnostic to stderr.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
from typing import NamedTuple

# Shared helpers owned by nanoclaw-admin's heartbeat skill, consumed over the
# co-loaded tile mount. `NANOCLAW_HEARTBEAT_SCRIPTS` overrides the location for
# tests and dev clones — heartbeat lives in a different repo, so there is no
# in-repo sibling to fall back to the way nanoclaw-orders has. If the directory
# resolves to nothing loadable, main() fails closed rather than fetching.
_HEARTBEAT_MOUNT = "/home/node/.claude/skills/tessl__heartbeat/scripts"
_HEARTBEAT_SCRIPTS_VAR = "NANOCLAW_HEARTBEAT_SCRIPTS"

_SHARED_MODULES = {
    "sanitize_email_body": "sanitize-email-body.py",
    "google_rest": "google-rest.py",
    "gmail_ops": "gmail-ops.py",
    "gmail_message": "gmail-message.py",
}

# Cap on the single Gmail list. The bounded fetch is what keeps the per-message
# `get` count flat; nothing here paginates past it (nanoclaw#656).
#
# The cap is a MITIGATION, not the safety property — `EXIT_TRUNCATED` is (see
# the module docstring). It is set generously rather than tightly because the
# cost of raising it is one metadata `get` per extra message on a nightly job
# that only runs when the schedule has already gone stale, whereas the cost of
# hitting it is an operator alert about a window we could not fully see. 100
# covers any realistic TripIt volume inside a normal stale window, so the
# truncation signal stays the rare exception it is meant to be.
MAX_RESULTS = 100

# Gmail message format. `metadata` returns headers without bodies — this probe
# reads only the subject, so a body is both wasted bandwidth and untrusted text
# with no reader.
_MESSAGE_FORMAT = "metadata"

EXIT_OK = 0
EXIT_CALL_FAILED = 1
EXIT_CONFIG = 2
# Distinct from EXIT_CALL_FAILED because stdout IS valid on this path — the
# array is real, it is just not the whole window. Step 3 branches on it to
# report rather than fall through to its silent-on-zero rule.
EXIT_TRUNCATED = 3

# Stable, greppable marker on the stderr line for a truncated window. Belt and
# braces with EXIT_TRUNCATED: a pipeline missing `set -o pipefail` swallows the
# fetch's exit code (the filter's 0 wins), and this probe must not go quiet just
# because a shell option was dropped. Either signal is enough for Step 3.
TRUNCATION_MARKER = "WINDOW_TRUNCATED"

# Transport failures that mean "this call did not complete". HTTPError
# subclasses OSError, so a Google 5xx lands here. GatewayNotInjecting /
# TierAccessRestricted are RuntimeErrors and deliberately do NOT — they are
# config failures and propagate to main() for their own exit.
_CALL_ERRORS = (urllib.error.URLError, OSError, json.JSONDecodeError)


def _heartbeat_scripts_dir() -> str:
    """Where the shared heartbeat helpers live: the tile mount in the
    container, or the `NANOCLAW_HEARTBEAT_SCRIPTS` override."""
    return os.environ.get(_HEARTBEAT_SCRIPTS_VAR) or _HEARTBEAT_MOUNT


def _load_module(modname: str, filename: str):
    path = os.path.join(_heartbeat_scripts_dir(), filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load {modname} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_shared_modules() -> dict:
    """Load every shared helper, or raise. All-or-nothing on purpose: a
    partial load could otherwise fetch with no sanitizer."""
    return {name: _load_module(name, filename) for name, filename in _SHARED_MODULES.items()}


class FetchResult(NamedTuple):
    """`rows` is the projected array; `truncated` says whether the window was
    seen in full. They are separate because a truncated fetch still returns
    real rows — the caller reports them AND the blind spot, rather than
    choosing between the two."""

    rows: list
    truncated: bool


def fetch_tripit_emails(gmail, sanitize, gmail_message, query: str) -> FetchResult:
    """List `query`, fetch each stub's metadata, sanitize, project compact rows.

    `gmail` is a collaborator with `.list(limit, query)` / `.get(message_id)`;
    `sanitize` is heartbeat's `sanitize()`; `gmail_message` is heartbeat's
    parser module. Every text field is sanitized by `parse_message` before it
    is projected — this function never touches raw header text itself.

    Raises whatever the collaborators raise: `_CALL_ERRORS` for a failed call,
    `GatewayNotInjecting` / `TierAccessRestricted` for a config failure. Both
    are fatal at main(); see the module docstring on why partial output is not
    an option for this probe.
    """
    stubs = gmail.list(limit=MAX_RESULTS, query=query)
    # A full page is the only evidence of truncation available here: Gmail
    # signals "more exist" with a `nextPageToken`, but `gmail-ops.list_messages`
    # returns the stub list alone and drops it, and that helper is admin's to
    # change. `len == limit` over-reports by exactly one case — a window holding
    # precisely MAX_RESULTS messages and no more — which is the safe direction:
    # a spurious "couldn't see the whole window" costs the operator one note; a
    # missed one costs a booking.
    truncated = len(stubs) >= MAX_RESULTS
    rows: list = []
    for stub in stubs:
        message_id = stub.get("id") or ""
        if not message_id:
            continue
        msg = gmail_message.parse_message(gmail.get(message_id), sanitize)
        if not msg:
            # parse_message returns {} only for a non-dict resource — a shape
            # Gmail should never send. Same reasoning as a failed get: a
            # message this probe cannot read is a booking it cannot rule out,
            # so it must not pass silently.
            raise ValueError(f"unrecognised message resource for id {message_id}")
        rows.append(
            {
                "id": msg.get("messageId"),
                "from": msg.get("from"),
                "subject": msg.get("subject"),
                "date": msg.get("internalDate"),
            }
        )
    return FetchResult(rows=rows, truncated=truncated)


def _bind_gmail(google_rest, gmail_ops):
    """Bind the gmail-ops functions to the transport, so `fetch_tripit_emails`
    takes one collaborator instead of threading `google_request` +
    `surface_url` through every call site (and so tests can hand it a fake)."""
    request, surface_url = google_rest.google_request, google_rest.surface_url

    class _Gmail:
        def list(self, *, limit, query):
            return gmail_ops.list_messages(
                request,
                limit=limit,
                query=query,
                include_spam_trash=False,
                surface_url=surface_url,
            )

        def get(self, message_id):
            return gmail_ops.get_message(
                request, message_id, fmt=_MESSAGE_FORMAT, surface_url=surface_url
            )

    return _Gmail()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        sys.stderr.write(
            "fetch-tripit-emails: usage: fetch-tripit-emails.py '<gmail_query>' — pass the "
            "`gmail_query` from check-travel-freshness.py's stale output verbatim.\n"
        )
        return 2

    try:
        mods = load_shared_modules()
    except (FileNotFoundError, PermissionError, ImportError, OSError, SyntaxError) as exc:
        sys.stderr.write(
            f"fetch-tripit-emails: shared Gmail helper unavailable ({exc}) — expected under "
            f"{_heartbeat_scripts_dir()} (the co-loaded tessl__heartbeat mount, or "
            f"${_HEARTBEAT_SCRIPTS_VAR}). Refusing to fetch without the sanitizer: raw email "
            f"text must never reach the agent. Check that nanoclaw-admin is co-loaded in this "
            f"chat's additionalTiles.\n"
        )
        return 2

    google_rest = mods["google_rest"]
    try:
        result = fetch_tripit_emails(
            _bind_gmail(google_rest, mods["gmail_ops"]),
            mods["sanitize_email_body"].sanitize,
            mods["gmail_message"],
            argv[1],
        )
    except google_rest.GatewayNotInjecting as exc:
        sys.stderr.write(f"fetch-tripit-emails: Gmail is unauthenticated — {exc}\n")
        return EXIT_CONFIG
    except google_rest.TierAccessRestricted as exc:
        sys.stderr.write(f"fetch-tripit-emails: Gmail unavailable at this tier — {exc}\n")
        return EXIT_CONFIG
    except _CALL_ERRORS as exc:
        sys.stderr.write(
            f"fetch-tripit-emails: Gmail call failed ({type(exc).__name__}: {exc}) — no list "
            f"emitted. A partial list could report 'no booking found' when one exists, so the "
            f"probe fails rather than guess; the next nightly fire retries.\n"
        )
        return EXIT_CALL_FAILED
    except ValueError as exc:
        sys.stderr.write(f"fetch-tripit-emails: {exc} — no list emitted.\n")
        return EXIT_CALL_FAILED

    # stdout FIRST, and on every path below: the rows are real either way, so
    # the filter gets its input whether or not the window was complete.
    print(json.dumps(result.rows))
    if result.truncated:
        sys.stderr.write(
            f"fetch-tripit-emails: {TRUNCATION_MARKER} — the query returned its full "
            f"{MAX_RESULTS}-message cap, so older mail in this window was NOT examined and a "
            f"booking confirmation may be sitting behind the cap. The {len(result.rows)} "
            f"message(s) emitted are real but are not the whole window: do NOT read a zero "
            f"match count as 'no booking found'. Report the blind spot to the operator. The "
            f"window widens the longer travel-schedule.json stays stale, so this will keep "
            f"firing until the schedule refreshes.\n"
        )
        return EXIT_TRUNCATED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
