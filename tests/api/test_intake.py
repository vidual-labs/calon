"""End-to-end tests for the external-intake route ``POST /api/v1/{source_slug}``.

These run the real application over an in-process transport with a source that is
enabled in the operator config and backed by a synthetic adapter module registered in
``sys.modules`` before the app boots. The signing scheme and the wire format are the
ones ``docs/external-intake.md`` and ADR 0005 publish, so a real operator can copy the
shape out of these tests.

Covered (``docs/external-intake.md`` §Flow):

* a valid signed request is evaluated and a booking is written (``201``);
* a repeated request with the same ``Idempotency-Key`` returns the stored decision
  with ``Idempotent-Replay: true`` (``200``) and does not create a second booking;
* a replayed rejection returns the **stored** code — a retry cannot flip it;
* an unknown slug is a constant ``404`` (no probe oracle);
* a bad signature or a missing signature header is a constant ``401`` and records
  nothing;
* a timestamp outside the replay window is a ``401``.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from calon.clock import utcnow
from calon.db import Database
from calon.intake.external import HmacSourceAdapter
from calon.models import Booking, BookingIntent

SECRET = "test-source-secret"
RESOURCE = "default"
# A working slot, Tuesday 1 September 2026 10:00 Europe/Berlin (a valid start inside the
# default booking window relative to the frozen clock of 08:00 Berlin).
START = "2026-09-01T10:00:00+02:00"
TZ = "Europe/Berlin"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def demo_module():
    """A synthetic adapter module for slug ``demo``.

    Registered under ``calon.intake.external.demo`` in ``sys.modules`` so that the
    boot's ``SourceRegistry.from_config`` (which imports ``calon.intake.external``
    and looks for a submodule named after each enabled slug) finds it.
    """
    mod = types.ModuleType("calon.intake.external.demo")
    mod.demo = HmacSourceAdapter("demo", secret=SECRET, resource_slug=RESOURCE)  # type: ignore[attr-defined]
    sys.modules["calon.intake.external.demo"] = mod
    try:
        yield mod
    finally:
        sys.modules.pop("calon.intake.external.demo", None)


@pytest.fixture
def intake_client(boot, demo_module, tmp_path):
    """The real app booted against a config that enables the ``demo`` source.

    The ``boot`` helper returns a :class:`TestClient` but does not enter its context
    manager, so the lifespan (which runs migrations and builds the source registry)
    never fires. Enter it here so the app is actually started.
    """
    config_body = """
[resource]
slug = "default"
timezone = "Europe/Berlin"

[sources.demo]
secret = "test-source-secret"
enabled = true
"""
    with boot(config_body) as client:
        yield client


def _payload() -> dict[str, object]:
    return {
        "resource_slug": RESOURCE,
        "start": START,
        "timezone": TZ,
        "requester": {"name": "Ada Lovelace", "email": "ada@example.com"},
        "subject": "Intake demo",
    }


def _headers(
    body: bytes,
    *,
    secret: str = SECRET,
    timestamp: int | None = None,
    idempotency_key: str | None = None,
    include_signature: bool = True,
) -> dict[str, str]:
    """The exact wire format ``docs/external-intake.md`` publishes."""
    from calon.intake.signature import (
        SIGNATURE_ALGORITHM,
        SIGNATURE_HEADER,
        TIMESTAMP_HEADER,
        compute_signature,
    )

    now = utcnow()
    ts = str(timestamp if timestamp is not None else int(now.timestamp()))
    headers = {
        TIMESTAMP_HEADER: ts,
        "Content-Type": "application/json",
    }
    if include_signature:
        headers[SIGNATURE_HEADER] = (
            f"{SIGNATURE_ALGORITHM}=" + compute_signature(secret, ts, body).split("=", 1)[1]
        )
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_valid_signed_request_is_accepted(intake_client: TestClient) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    headers = _headers(body, idempotency_key="key-accept")
    r = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert data["decision"]["code"] == "ACCEPTED"
    assert data["booking"] is not None
    assert data["booking"]["status"] == "confirmed"


def test_accepted_booking_is_written_to_the_database(
    intake_client: TestClient, database: Database
) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    headers = _headers(body, idempotency_key="key-db")
    r = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r.status_code == 201, r.text

    intent_id = r.json()["intent_id"]
    with database.read() as session:
        intent = session.execute(
            select(BookingIntent).where(BookingIntent.id == intent_id)
        ).scalar_one()
        assert intent.source == "demo"
        assert intent.idempotency_key == "key-db"
        assert intent.status == "accepted"
        assert intent.decision_json is not None

        booking = session.execute(
            select(Booking).where(Booking.intent_id == intent_id)
        ).scalar_one()
        assert booking.status == "confirmed"


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_idempotent_replay_returns_stored_decision(intake_client: TestClient) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    headers = _headers(body, idempotency_key="key-replay")

    r1 = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r1.status_code == 201, r1.text
    first_intent_id = r1.json()["intent_id"]
    first_decision = r1.json()["decision"]

    # Same key, same instant: the stored response is returned, not re-evaluated.
    r2 = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("Idempotent-Replay") == "true"
    data = r2.json()
    assert data["intent_id"] == first_intent_id
    assert data["decision"] == first_decision


def test_replayed_rejection_returns_the_stored_code(intake_client: TestClient) -> None:
    # A start that is too early relative to the policy rejects on first evaluation.
    payload = _payload()
    payload["start"] = "2026-09-01T07:00:00+02:00"
    body = json.dumps(payload).encode("utf-8")
    headers = _headers(body, idempotency_key="key-reject")

    r1 = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r1.status_code == 201, r1.text
    assert r1.json()["status"] == "rejected"
    first_code = r1.json()["decision"]["code"]

    r2 = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("Idempotent-Replay") == "true"
    data = r2.json()
    assert data["status"] == "rejected"
    assert data["decision"]["code"] == first_code
    # The stored decision is returned verbatim — a retry cannot flip the outcome.
    assert data["decision"]["reason"] == r1.json()["decision"]["reason"]


# --------------------------------------------------------------------------
# Auth failures
# --------------------------------------------------------------------------


def test_unknown_source_slug_returns_404(intake_client: TestClient) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    r = intake_client.post("/api/v1/ghost", content=body, headers=_headers(body))
    assert r.status_code == 404
    assert r.json()["detail"] == "source not configured on this instance"


def test_bad_signature_returns_401_and_no_intent_is_recorded(
    intake_client: TestClient, database: Database
) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    headers = _headers(body, secret="wrong-secret", idempotency_key="key-bad")
    r = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    assert r.json()["detail"] == "unauthorized request"

    with database.read() as session:
        intents = session.execute(select(BookingIntent)).scalars().all()
    assert intents == []


def test_timestamp_outside_window_returns_401(intake_client: TestClient) -> None:
    # Far in the past: outside the 300s replay window relative to the frozen clock.
    stale_ts = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    body = json.dumps(_payload()).encode("utf-8")
    headers = _headers(body, timestamp=stale_ts, idempotency_key="key-stale")
    r = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "unauthorized request"


def test_missing_signature_header_returns_401(intake_client: TestClient) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    headers = _headers(body, include_signature=False, idempotency_key="key-none")
    r = intake_client.post("/api/v1/demo", content=body, headers=headers)
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "unauthorized request"
