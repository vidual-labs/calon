"""The repository functions the external-intake route needs to read stored outcomes.

ADR 0005: a replay returns the stored original response rather than re-evaluating.
This means the route needs two reads from the intent table — the stored structured
decision (``decision_json``) and the stored booking (if the original was accepted).
Both are plain lookups, so both are placed next to the other readers in
:mod:`calon.services.repository` rather than duplicated inside the route module.

This file exists to make that placement explicit: the route module is where
HTTP-level decisions live, and the *storage* side of the idempotency contract
belongs with the storage.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from calon.models import BookingIntent

__all__ = [
    "load_intent_by_source_idempotency",
]


def load_intent_by_source_idempotency(
    session: Session,
    *,
    source: str,
    idempotency_key: str,
) -> BookingIntent | None:
    """The intent row this (source, key) pair was recorded against, if any.

    ``None`` is the caller's signal that this pair has never been seen on this source,
    i.e. it must run a fresh evaluation. The unique index on
    ``(source, idempotency_key)`` (migration 0001) is what makes this lookup
    deterministic under concurrent retries: two requests racing on the same key
    resolve to at most one intent row, regardless of which writer committed first.
    """
    return session.scalar(
        select(BookingIntent).where(
            BookingIntent.source == source,
            BookingIntent.idempotency_key == idempotency_key,
        )
    )
