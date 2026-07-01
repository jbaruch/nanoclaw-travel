"""Typed narrowing helpers shared across the test suite."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def must(value: T | None) -> T:
    """Assert `value` is not None and return it narrowed to `T`.

    State readers (`read_config`, `read_flight_state`, ...) return
    `dict | None`; tests that read back what they just wrote use this
    to fail with a clear AssertionError instead of a TypeError when
    the read unexpectedly comes back empty.
    """
    assert value is not None, "expected a value, got None"
    return value
