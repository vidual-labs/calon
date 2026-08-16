"""The wall clock, in one place.

The domain layer never reads the clock — ``now`` is always injected (``CLAUDE.md`` §4.1).
That injection has to start somewhere, and this is it: the single point where calon asks
what time it is, so a service can be handed a fixed instant instead and every scheduling
test stays deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["utcnow"]


def utcnow() -> datetime:
    """The current instant, timezone-aware and in UTC."""
    return datetime.now(UTC)
