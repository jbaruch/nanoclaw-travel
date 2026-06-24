"""Read the operator's canonical home address — the drive origin.

Every home-anchored drive leg (outbound from home, return to home) starts or
ends at the operator's current residence. That address has ONE canonical home
(Epic #59 §4): the machine-readable `## Addresses` block in the owner profile
`/workspace/trusted/user_profile.md`, owned by the `trusted-memory` skill in
the `nanoclaw-admin` tile. drive-planner is a READER of that block, never a
writer — the admin tile owns its shape and migration.

The block the admin tile writes (Epic #59 §4):

    ## Addresses
    <!-- canonical, machine-read by travel tile -->
    - current_home: 12 Example St, Sampleton, TN 37000
    - home_airport: BNA
    - new_home_wip: 99 Placeholder Rd, Testburg, TN 37100

`current_home` is the drive origin. `new_home_wip` (a house under
construction) is deliberately NOT read — switching origins is a later,
explicit change, not an automatic pickup of whichever address appears first.

This is the deterministic reader (per `coding-policy: script-delegation` — a
fixed parse of a fixed block). It does NOT fall back to a guessed address: a
silent wrong origin would route every drive from the wrong place and quietly
mis-time every leave-by. A missing block raises with an actionable message
pointing at the admin tile.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from home_address import read_current_home, HomeAddressError

    home = read_current_home()   # "12 Example St, Sampleton, TN 37000"
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_DEFAULT_PROFILE_PATH = "/workspace/trusted/user_profile.md"
_PROFILE_PATH_ENV = "USER_PROFILE_PATH"

# The `## Addresses` section heading. `current_home` is read ONLY from inside
# this canonical block — a `current_home:` mention elsewhere in the profile
# (prose, an example, a stale note) must never set the drive origin.
_ADDRESSES_HEADING_RE = re.compile(r"^[ \t]*##[ \t]+Addresses[ \t]*$", re.MULTILINE)
# The next `## ` heading that closes the Addresses section.
_NEXT_H2_RE = re.compile(r"^[ \t]*##[ \t]+\S", re.MULTILINE)
# The canonical drive-origin key inside the `## Addresses` block. Matched as a
# `- current_home: <value>` list item, tolerant of surrounding whitespace.
_CURRENT_HOME_RE = re.compile(r"^\s*-\s*current_home\s*:\s*(?P<value>\S.*?)\s*$", re.MULTILINE)


def _addresses_section(text: str) -> str | None:
    """The body of the `## Addresses` block, or None when the heading is absent.

    Runs from just after the `## Addresses` heading to the next `## ` heading
    (or end of file). Scoping the `current_home` read to this block is what
    keeps a stale or example `current_home:` elsewhere in the profile from
    silently becoming the drive origin.
    """
    heading = _ADDRESSES_HEADING_RE.search(text)
    if heading is None:
        return None
    body = text[heading.end() :]
    nxt = _NEXT_H2_RE.search(body)
    return body[: nxt.start()] if nxt else body


class HomeAddressError(Exception):
    """Raised when the canonical home address cannot be read.

    The fix is always "make the admin tile's `## Addresses` block present and
    well-formed", not "retry" — the message says so. drive-planner refuses to
    guess an origin rather than route every drive from the wrong place.
    """


def profile_path() -> Path:
    """The owner-profile path; overridable via `USER_PROFILE_PATH` for tests."""
    return Path(os.environ.get(_PROFILE_PATH_ENV, _DEFAULT_PROFILE_PATH))


def read_current_home(*, path: Path | None = None) -> str:
    """Return the `current_home` address from the canonical Addresses block.

    Args:
        path: override the profile path (defaults to `profile_path()`).

    Returns:
        The `current_home` value, whitespace-trimmed.

    Raises:
        HomeAddressError: when the profile file is missing, carries no
            `## Addresses` block, or that block carries no non-empty
            `current_home:` entry — each with a message pointing at the
            `nanoclaw-admin` trusted-memory Addresses block to fix. A
            `current_home:` outside the block is deliberately not read.
    """
    target = path if path is not None else profile_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HomeAddressError(
            f"owner profile not found at {target} — the canonical home address lives in the "
            "`## Addresses` block of user_profile.md, owned by the nanoclaw-admin trusted-memory "
            "skill; add the block (current_home: <address>) and redeploy"
        ) from exc
    except OSError as exc:
        raise HomeAddressError(f"owner profile at {target} is unreadable ({exc})") from exc

    section = _addresses_section(text)
    if section is None:
        raise HomeAddressError(
            f"no `## Addresses` block in {target} — the canonical home address lives in that "
            "block of user_profile.md (nanoclaw-admin trusted-memory); add it with a "
            "`- current_home: <address>` line and redeploy"
        )
    match = _CURRENT_HOME_RE.search(section)
    if match is None:
        raise HomeAddressError(
            f"no `current_home:` entry in the `## Addresses` block of {target} — add "
            "`- current_home: <address>` to the canonical block (nanoclaw-admin trusted-memory)"
        )
    return match["value"].strip()
