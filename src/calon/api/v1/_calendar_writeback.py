"""Post-acceptance calendar write-back shared by the native and external intake routes.

ADR 0009: after a booking is accepted *and the write transaction commits*, each route
calls :func:`perform_write_back` to push the event to the resource's connected calendar
provider. Both the native booking route (``api/v1/bookings.py``) and the external intake
route (``api/v1/intake.py``) create bookings, so the shared module keeps the write-back,
the audit append, and the degrade-on-failure logic in one place (CLAUDE.md §10).

The write-back runs **outside** the acceptance transaction on purpose: the provider is a
network hop, and an unreachable provider must never hold the DB lock or roll a booking
back. The failure is audited as ``booking.calendar_sync_failed`` and logged; the booking
stays booked and the response still reflects the 201.

A resource with no configured provider is a silent no-op (degrade-not-fail, CLAUDE.md §2);
:func:`perform_write_back` returns ``None`` for that case so the caller can tell a real
sync outcome (``True``/``False``) apart from "nothing to sync".
"""

from __future__ import annotations

import logging
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from calon.calendars import CalendarEvent as CalendarProviderEvent
from calon.calendars import CalendarProviderError, CalendarProviderRegistry
from calon.db import Database
from calon.models import BookingIntent, ResourceRow
from calon.services import booking_service, repository

__all__ = ["perform_write_back", "resolve_resource_slug"]

log = logging.getLogger("calon.api.writeback")


def resolve_resource_slug(session: Session, *, intent: BookingIntent) -> str:
    """Resolve the resource slug for a stored intent via its ``resource_id``.

    The ``BookingIntent`` model stores ``resource_id`` (a foreign key), not the slug, so a
    write-back that needs the slug for a provider lookup does this small join. Returns an
    empty string when the intent has no ``resource_id`` or the resource row is missing —
    a caller with an empty slug simply finds no provider and degrades to no sync.
    """
    if intent.resource_id is None:
        return ""

    resource = session.execute(
        sa.select(ResourceRow).where(ResourceRow.id == intent.resource_id)
    ).scalar_one_or_none()

    return resource.slug if resource is not None else ""


def perform_write_back(
    database: Database,
    calendar_registry: CalendarProviderRegistry,
    *,
    booking: booking_service.AcceptedBooking,
    intent: BookingIntent,
    now: datetime,
) -> bool | None:
    """Trigger the post-commit calendar write-back for one accepted booking.

    Builds a :class:`calon.calendars.CalendarEvent` from the committed booking + intent,
    resolves the resource slug, and pushes the event to the resource's provider (if one is
    configured). The provider failure is caught, logged, and audited as
    ``booking.calendar_sync_failed``; the booking is never rolled back (ADR 0009,
    CLAUDE.md §2). Returns ``None`` when no provider is configured for the resource
    (nothing to sync), ``True`` on a successful upsert, ``False`` on a degraded one.

    ``session`` is the *read-side* lookup session used to resolve the slug; the audit
    append then opens its own short write session.
    """
    with database.read() as session:
        resource_slug = resolve_resource_slug(session, intent=intent)

    # No provider for this resource: nothing to sync and nothing to audit. Returning
    # ``None`` (rather than ``True``) keeps ``calendar_synced`` honest. A read-only
    # provider — a published ICS feed (ADR 0017) — is the same case: it reports busy
    # time but has nowhere to put an event, so there is no failure to audit either.
    if not calendar_registry.writes_back(resource_slug):
        return None

    provider_event = CalendarProviderEvent(
        uid=booking.ics_uid,
        summary=intent.subject,
        starts_at_utc=booking.start_utc,
        ends_at_utc=booking.end_utc,
        description=intent.notes or "",
    )

    synced = True
    try:
        calendar_registry.upsert_event(resource_slug, provider_event)
    except CalendarProviderError as exc:
        log.warning(
            "calendar write-back degraded for booking %r: %s",
            booking.id,
            exc,
            exc_info=exc,
        )
        synced = False

    # Audit the write-back outcome in its own short write session.
    with database.write() as session:
        repository.append_audit(
            session,
            at=now,
            actor="system",
            event_type="booking.calendar_synced" if synced else "booking.calendar_sync_failed",
            intent_id=intent.id,
            booking_id=booking.id,
            payload={
                "uid": booking.ics_uid,
                "provider_error": None if synced else "degraded",
            },
        )

    return synced
