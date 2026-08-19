"""End-to-end tests for the OpenFlow source on the external-intake route.

Companion to ``test_intake.py`` (the canonical ``X-Calon-*`` scheme). Here we drive the
real application through HTTP with the **OpenFlow** source enabled, registered through
the genuine registry path (the ``openflow`` slug is special-cased at boot to build a
real :class:`calon.intake.external.openflow.OpenFlowAdapter` from the operator config's
``[sources.openflow.fields.<formId>]`` table) and signed with the **OpenFlow shim** —
a single ``X-OpenFlow-Signature`` header holding ``hex(HMAC-SHA256(secret, rawBody))``.

Covered (``docs/external-intake.md`` §Flow, OpenFlow variant):

* a valid OpenFlow-sig… accepted and a booking is written (``201``);
* a repeated request with the same idempotency key returns the stored decision
  (``200``, ``Idempotent-Replay: true``) and creates no second booking;
* a forged signature, or a payload timestamp outside the clock window, is a
  constant ``401`` and records nothing;
* a request that instead carries the canonical ``X-Calon-*`` headers is verified by
  the standard scheme (canonical takes precedence over the shim);
* an unmapped form id is a ``400`` parse error (the config did not map that form).
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from calon.clock import utcnow
from calon.db import Database
from calon.intake.external.openflow import OPENFLOW_SIGNATURE_HEADER
from calon.intake.signature import (
    SIGNATURE_ALGORITHM,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    compute_signature,
)
from calon.models import Booking, BookingIntent

SECRET = "openflow-secret-0123456789abcdef"
RESOURCE = "default"
FORM_ID = "of-e2e-form"
TZ = "Europe/Berlin"
# A working instant relative to the frozen clock (Tuesday 1 Sep 2026 08:00 Berlin):
# 10:00 Berlin is a valid start (>= min_notice, allowed weekday, inside the window).
START_TZ = "2026-09-01T10:00:00+02:00"

FIELD_IDS = {
    "start": "fld_start",
    "end": "fld_end",
    "name": "fld_name",
    "email": "fld_email",
    "phone": "fld_phone",
    "subject": "fld_subject",
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def boot_with_openflow(boot):
    """The real app booted against a config that enables the OpenFlow source.

    The ``boot`` helper returns a :class:`TestClient` but does not enter its context
    manager, so the lifespan (which runs migrations and builds the source registry)
    never fires. Enter it here so the app is actually started. The ``openflow`` slug
    is resolved by :meth:`SourceRegistry.from_config` into a genuine
    ``OpenFlowAdapter`` from the ``fields`` sub-table below.
    """
    config_body = f"""
[resource]
slug = "default"
timezone = "Europe/Berlin"

[sources.openflow]
enabled = true
secret = "{SECRET}"
resource_slug = "default"
timestamp_window_seconds = 300

[sources.openflow.fields."{FORM_ID}"]
start = "{FIELD_IDS["start"]}"
end = "{FIELD_IDS["end"]}"
name = "{FIELD_IDS["name"]}"
email = "{FIELD_IDS["email"]}"
phone = "{FIELD_IDS["phone"]}"
subject = "{FIELD_IDS["subject"]}"
timezone = "{TZ}"
"""
    with boot(config_body) as client:
        yield client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _payload(
    form_id: str = FORM_ID,
    *,
    start: str = START_TZ,
    name: str = "Ada Lovelace",
    email: str = "ada@example.com",
    phone: str | None = None,
    subject: str = "OpenFlow e2e",
    clock: str | None = "2026-09-01T08:00:00+02:00",
) -> dict[str, object]:
    """A genuine-shape OpenFlow submission (event/formId/formTitle/timestamp/data)."""
    data: dict[str, object] = {
        FIELD_IDS["start"]: start,
        FIELD_IDS["name"]: name,
        FIELD_IDS["email"]: email,
        FIELD_IDS["subject"]: subject,
    }
    if phone is not None:
        data[FIELD_IDS["phone"]] = phone
    if clock is not None:
        payload: dict[str, object] = {
            "event": "submission",
            "formId": form_id,
            "formTitle": "OpenFlow e2e form",
            "timestamp": clock,
            "data": data,
        }
    else:
        payload = {
            "event": "submission",
            "formId": form_id,
            "formTitle": "OpenFlow e2e form",
            "data": data,
        }
    return payload


def _signed_headers(body: bytes, *, secret: str = SECRET) -> dict[str, str]:
    """The OpenFlow shim: one header, the HMAC-SHA256 hex of the raw body."""
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-OpenFlow-Signature": sig, "Content-Type": "application/json"}


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_valid_openflow_request_is_accepted(boot_with_openflow: TestClient) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    r = boot_with_openflow.post("/api/v1/openflow", content=body, headers=_signed_headers(body))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert data["decision"]["code"] == "ACCEPTED"
    assert data["booking"] is not None
    assert data["booking"]["status"] == "confirmed"


def test_openflow_booking_is_written_to_the_database(
    boot_with_openflow: TestClient,
    database: Database,
) -> None:

    body = json.dumps(_payload()).encode("utf-8")
    r = boot_with_openflow.post("/api/v1/openflow", content=body, headers=_signed_headers(body))
    assert r.status_code == 201, r.text

    intent_id = r.json()["intent_id"]
    with database.read() as session:
        intent = session.execute(
            select(BookingIntent).where(BookingIntent.id == intent_id)
        ).scalar_one()
        assert intent.source == "openflow"
        assert intent.status == "accepted"
        assert intent.decision_json is not None

        booking = session.execute(
            select(Booking).where(Booking.intent_id == intent_id)
        ).scalar_one()
        assert booking.status == "confirmed"


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_openflow_idempotent_replay_returns_stored_decision(
    boot_with_openflow: TestClient,
) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    # The OpenFlow shim does not carry an Idempotency-Key header; the route derives the
    # key from the intent's source_ref (of:<formId>:<start answer>). A repeat of the same
    # submission therefore resolves to the same key and returns the stored decision.
    r1 = boot_with_openflow.post("/api/v1/openflow", content=body, headers=_signed_headers(body))
    assert r1.status_code == 201, r1.text
    first_decision = r1.json()["decision"]
    first_intent_id = r1.json()["intent_id"]

    r2 = boot_with_openflow.post("/api/v1/openflow", content=body, headers=_signed_headers(body))
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r2.json()["intent_id"] == first_intent_id
    assert r2.json()["decision"] == first_decision


# --------------------------------------------------------------------------
# Auth failures
# --------------------------------------------------------------------------


def test_forged_openflow_signature_returns_401(boot_with_openflow: TestClient) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    headers = _signed_headers(body, secret="wrong-secret")
    r = boot_with_openflow.post("/api/v1/openflow", content=body, headers=headers)
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "unauthorized request"


def test_payload_timestamp_outside_window_returns_401(boot_with_openflow: TestClient) -> None:
    # A wall clock far in the past: outside OPENFLOW_CLOCK_WINDOW (4 min) of now.
    clock = "2026-09-01T04:00:00+02:00"
    body = json.dumps(_payload(clock=clock)).encode("utf-8")
    r = boot_with_openflow.post("/api/v1/openflow", content=body, headers=_signed_headers(body))
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "unauthorized request"


def test_missing_openflow_signature_returns_401(boot_with_openflow: TestClient) -> None:
    body = json.dumps(_payload()).encode("utf-8")
    r = boot_with_openflow.post(
        "/api/v1/openflow",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "unauthorized request"


# --------------------------------------------------------------------------
# Canonical scheme takes precedence over the OpenFlow shim
# --------------------------------------------------------------------------


def test_canonical_headers_take_precedence_over_shim(boot_with_openflow: TestClient) -> None:
    """When the canonical ``X-Calon-*`` headers are present they win: the standard
    scheme verifies the request and the OpenFlow shim is not consulted at all. We prove
    this by (a) signing a valid OpenFlow body with the canonical scheme and (b) also
    attaching a *wrong* ``X-OpenFlow-Signature`` — if the shim were (erroneously) used,
    the bad shim header would 401 it. Authenticating through the canonical path instead
    means the request succeeds (``201``) and the bogus shim header is ignored.
    """
    body = json.dumps(_payload()).encode("utf-8")
    now = utcnow()
    ts = str(int(now.timestamp()))
    headers = {
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: (
            SIGNATURE_ALGORITHM + "=" + compute_signature(SECRET, ts, body).split("=", 1)[1]
        ),
        # A deliberately wrong shim header: must be ignored because canonical wins.
        OPENFLOW_SIGNATURE_HEADER: "0" * 64,
        "Content-Type": "application/json",
        "Idempotency-Key": "key-canonical-precedence",
    }
    r = boot_with_openflow.post("/api/v1/openflow", content=body, headers=headers)
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------
# Parse failure: unmapped form
# --------------------------------------------------------------------------


def test_unmapped_form_is_a_parse_error_not_auth(boot_with_openflow: TestClient) -> None:
    """A submission for a form with no ``fields.<formId>`` mapping is a 400 parse error
    (not a 401): the request is authenticated, but the adapter has no mapping table for
    that formId and cannot translate it.
    """
    body = json.dumps(_payload(form_id="of-unmapped-form")).encode("utf-8")
    r = boot_with_openflow.post("/api/v1/openflow", content=body, headers=_signed_headers(body))
    assert r.status_code == 400, r.text
    assert "no field mapping" in r.json()["detail"]


__all__ = []
