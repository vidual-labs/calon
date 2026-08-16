"""calon's own intake, expressed as a source adapter.

Native requests already arrive in the canonical shape, so this adapter has almost nothing
to translate — and that is exactly why it exists. Making native intake *an* adapter rather
than the one path that skips the adapter layer is what guarantees there is a single
downstream code path (``CLAUDE.md`` §4.2). A native shortcut would be the obvious place for
scheduling logic to appear later, and there is no shortcut to put it in.

What it does do is capture the payload as received, so the audit trail records what was
actually sent rather than what calon made of it.
"""

from __future__ import annotations

from typing import Any

from calon.schemas import BookingIntentIn

__all__ = ["NATIVE_SOURCE", "adapt"]

#: The ``source`` recorded against a booking intent that calon itself accepted.
NATIVE_SOURCE = "native"


def adapt(payload: BookingIntentIn) -> tuple[BookingIntentIn, dict[str, Any]]:
    """Return the canonical intent and the raw payload to keep alongside it."""
    return payload, payload.model_dump(mode="json")
