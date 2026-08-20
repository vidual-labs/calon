"""POST /api/v1/intake/{source_slug} — the one endpoint every external source submits to.

The flow (``docs/external-intake.md``, ADR 0005):

1. Look up the source by slug. A source the operator did not enable receives ``404``
   with a constant body — probing which slugs are alive is not an oracle (ADR 0012).
2. ``adapter.verify`` — HMAC-SHA256 over the raw body plus a replay window checked
   against the instant the route supplies (``CLAUDE.md`` §4.1 — the route reads the
   clock once and passes it down). A failure is ``401`` and is logged as
   ``intake.rejected_signature``.
3. ``adapter.parse`` — the provider shape becomes the canonical booking intent.
   A failure is ``400`` and is logged as ``intake.rejected_parse``.
4. Idempotency (ADR 0005, §Idempotency): when the request resolves to an idempotency
   key that is already recorded for this source, the **stored original response** is
   returned with ``200`` and ``Idempotent-Replay: true`` — the rules are not
   re-evaluated, and no second booking is created. A retry cannot turn yesterday's
   rejection into today's acceptance.
5. Otherwise the request runs through ``booking_service.submit_intent`` — the single
   downstream path (``CLAUDE.md`` §4.2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders

from calon.api.deps import CalendarRegistryDep, DatabaseDep, SettingsDep, get_source_registry
from calon.calendars import CalendarProviderRegistry
from calon.clock import utcnow
from calon.db import Database
from calon.intake.external import IntakeRequest, SourceRegistry
from calon.intake.signature import IntakeAuthError, IntakeParseError, resolve_idempotency_key
from calon.models import Booking, BookingIntent
from calon.schemas import BookingIntentIn, BookingOut, BookingResponse, DecisionOut
from calon.services import booking_service, intake_read

from . import _calendar_writeback

__all__ = ["router"]

log = logging.getLogger("calon.api.intake")

router = APIRouter(tags=["intake"])

#: The boot-built source registry, injected per request.
RegistryDep = Annotated[SourceRegistry, Depends(get_source_registry)]


@router.post("/{source_slug}", status_code=200, include_in_schema=True)
async def intake(
    request: Request,
    source_slug: str,
    response: Response,
    registry: RegistryDep,
    database: DatabaseDep,
    calendar_registry: CalendarRegistryDep,
    settings: SettingsDep,
) -> Any:
    """One endpoint serves every registered source (``docs/external-intake.md``).

    The raw body is read **before** any adapter is consulted, because the signature
    covers the exact bytes on the wire (ADR 0005) — re-serializing first would change
    them. The instant is read **once** and handed to both the signature check and the
    rule evaluation, which is what keeps the replay-window decision and the scheduling
    decision consistent for a request that sits inside the window.
    """
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    now = utcnow()
    intent_request = IntakeRequest(source_slug=source_slug, raw_body=body, headers=headers)

    adapter = registry.get(source_slug)
    if adapter is None:
        # Constant for every unenabled slug — the same body for a typo and for a
        # slug the operator configured but left disabled, so the response is not a
        # probe oracle (ADR 0012).
        log.info("intake.404 source=%s (not enabled here)", source_slug)
        return JSONResponse({"detail": "source not configured on this instance"}, status_code=404)

    # --- verify ---------------------------------------------------------------
    try:
        adapter.verify(intent_request, now=now)
    except IntakeAuthError as exc:
        log.warning("intake.rejected_signature source=%s detail=%s", source_slug, exc)
        return JSONResponse({"detail": "unauthorized request"}, status_code=401)

    # --- parse ----------------------------------------------------------------
    try:
        intent = adapter.parse(intent_request)
    except IntakeParseError as exc:
        log.warning("intake.rejected_parse source=%s detail=%s", source_slug, exc)
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # --- idempotent replay ----------------------------------------------------
    key = resolve_idempotency_key(headers.get("idempotency-key"), intent.source_ref)
    stored: BookingIntent | None = None
    if key is not None:
        with database.read() as session:
            stored = _find_stored_outcome(session, source=source_slug, idempotency_key=key)
    if stored is not None:
        log.info("intake.replayed source=%s key=%s intent_id=%s", source_slug, key, stored.id)
        response.headers["Idempotent-Replay"] = "true"
        return _stored_outcome_response(stored, database)

    # --- fresh evaluation -----------------------------------------------------
    raw_payload = _decode_json(body)
    result = _evaluate_with_write(
        database,
        intent=intent,
        source=source_slug,
        now=now,
        raw_payload=raw_payload,
        idempotency_key=key,
        response_headers=response.headers,
        source_slug=source_slug,
        calendar_registry=calendar_registry,
        instance_host=settings.instance_host,
    )
    # _evaluate_with_write returns either a Submission (fresh) or a BookingResponse
    # (race-path replay). Distinguish by type.
    if isinstance(result, booking_service.Submission):
        return _fresh_response(result, intent.timezone)
    return result


# --------------------------------------------------------------------------------------
# Write path
# --------------------------------------------------------------------------------------


def _evaluate_with_write(
    database: Database,
    *,
    intent: BookingIntentIn,
    source: str,
    now: datetime,
    raw_payload: dict[str, Any] | None,
    idempotency_key: str | None,
    response_headers: MutableHeaders,
    source_slug: str,
    calendar_registry: CalendarProviderRegistry,
    instance_host: str,
) -> booking_service.Submission | BookingResponse:
    """Run the evaluation path inside the write transaction.

    Returns either a :class:`booking_service.Submission` (fresh evaluation) or a
    :class:`BookingResponse` (integrity-race replay). The caller checks the type
    and assembles the final response accordingly.

    On acceptance, after the write transaction commits, the post-commit calendar
    write-back runs (ADR 0009). A provider failure is audited as
    ``booking.calendar_sync_failed`` and the response is unchanged — the write-back
    never fails the acceptance (the booking is already committed).

    On an :class:`IntegrityError` (concurrent request with the same idempotency
    key committed first) the winner's row is re-read from the *same* session and
    the replay header is set before returning.
    """
    submission: booking_service.Submission | None = None
    with database.write() as session:
        try:
            submission = booking_service.submit_intent(
                session,
                intent=intent,
                source=source,
                now=now,
                raw_payload=raw_payload,
                idempotency_key=idempotency_key,
                instance_host=instance_host,
                calendar_registry=calendar_registry,
            )
        except IntegrityError:
            stored = _find_stored_outcome(
                session, source=source_slug, idempotency_key=idempotency_key or ""
            )
            if stored is None:
                # The winner's row has been committed, but the current session's snapshot
                # has not advanced yet - re-raise so the caller sees a 500 and can
                # retry. The next attempt's read transaction will find the row.
                raise
            response_headers["Idempotent-Replay"] = "true"
            # Build the replay response directly so the caller's final response is
            # the same shape it would get from a normal replay hit.
            return _assembled_replay_response(session, stored)

    # Post-commit calendar write-back (ADR 0009). Runs *after* the write session has
    # closed so an unreachable provider never holds the DB lock. Skipped entirely when
    # the submission has no accepted booking (rejected outcomes, race-path replays).
    if submission is not None and submission.booking is not None:
        with database.read() as session:
            intent_row = (
                session.get(BookingIntent, submission.intent_id)
                if submission.intent_id is not None
                else None
            )
        if intent_row is not None:
            synced = _calendar_writeback.perform_write_back(
                database,
                calendar_registry,
                booking=submission.booking,
                intent=intent_row,
                now=now,
            )
            if synced is not None:
                submission = replace(
                    submission,
                    decision=submission.decision.with_calendar_synced(synced),
                )

    return submission


def _assembled_replay_response(session: Session, stored: BookingIntent) -> BookingResponse:
    """Build a :class:`BookingResponse` from a stored row (race-path replay)."""
    if stored.decision_json is None:
        raise RuntimeError("stored intent has no serialized decision; re-send with a fresh key")
    booking_row: Booking | None = (
        session.query(Booking).filter_by(intent_id=stored.id).first()
        if stored.status == "accepted"
        else None
    )
    booking: BookingOut | None = None
    if booking_row is not None:
        booking = BookingOut.of(
            id=booking_row.id,
            start_utc=booking_row.start_utc,
            end_utc=booking_row.end_utc,
            status=booking_row.status,
            timezone=stored.requester_timezone,
        )
    return BookingResponse(
        intent_id=stored.id,
        status=stored.status,
        decision=DecisionOut.model_validate(stored.decision_json),
        booking=booking,
    )


# --------------------------------------------------------------------------------------
# Response assembly
# --------------------------------------------------------------------------------------


def _fresh_response(submission: booking_service.Submission, timezone: str) -> JSONResponse:
    """Assemble the response for a request the route just evaluated (HTTP 201 — new resource)."""
    booking: BookingOut | None = None
    if submission.booking is not None:
        booking = BookingOut.of(
            id=submission.booking.id,
            start_utc=submission.booking.start_utc,
            end_utc=submission.booking.end_utc,
            status="confirmed",
            timezone=timezone,
        )
    body = BookingResponse(
        intent_id=submission.intent_id,
        status=submission.status,
        decision=DecisionOut.of(submission.decision),
        booking=booking,
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=201)


def _stored_outcome_response(intent_row: BookingIntent, database: Database) -> BookingResponse:
    """Assemble the ``Idempotent-Replay`` response from the stored intent row.

    The structured decision was serialized to ``decision_json`` at evaluation time
    (``booking_service._record_outcome``), and the replay returns exactly that
    value — including suggestions — without re-deriving it.
    """
    with database.read() as session:
        if intent_row.decision_json is None:
            raise RuntimeError(
                "stored intent has no serialized decision; re-send the request with a "
                "fresh idempotency key, or apply migration 0002"
            )
        booking_row: Booking | None = (
            session.query(Booking).filter_by(intent_id=intent_row.id).first()
            if intent_row.status == "accepted"
            else None
        )
        booking: BookingOut | None = None
        if booking_row is not None:
            booking = BookingOut.of(
                id=booking_row.id,
                start_utc=booking_row.start_utc,
                end_utc=booking_row.end_utc,
                status=booking_row.status,
                timezone=intent_row.requester_timezone,
            )
        return BookingResponse(
            intent_id=intent_row.id,
            status=intent_row.status,
            decision=DecisionOut.model_validate(intent_row.decision_json),
            booking=booking,
        )


# --------------------------------------------------------------------------------------
# Reads / decodes
# --------------------------------------------------------------------------------------


def _find_stored_outcome(
    session: Session, *, source: str, idempotency_key: str
) -> BookingIntent | None:
    """The recorded intent for this (source, key) pair, if one exists."""
    return intake_read.load_intent_by_source_idempotency(
        session, source=source, idempotency_key=idempotency_key
    )


def _decode_json(body: bytes) -> dict[str, Any] | None:
    """The raw payload to keep alongside the intent, or ``None`` when it is not JSON."""
    try:
        decoded: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None
