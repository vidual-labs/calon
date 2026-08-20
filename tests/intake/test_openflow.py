"""Unit tests for the OpenFlow adapter (phase 8 — first real provider adapter).

The adapter translates OpenFlow's webhook — the *genuine* payload shape shipped by
``vidual-labs/openflow`` (``backend/src/models/integrations.js``, ``runWebhook``) — onto
calon's canonical booking intent. The fixture in ``tests/fixtures/openflow.json`` is the
sample that shape is built against; these tests are the parse/verify contract batch of
the TDD sequence (plan: ``.hermes/plans/phase-8-openflow-adapter.md``, batch 1).

Three things are tested that the generic framework already covers elsewhere:

* **The signature shim.** OpenFlow signs the raw body with ``X-OpenFlow-Signature:
  hexdigest(HMAC-SHA256(secret, rawBody))`` and carries the submission's own ``timestamp``
  as a *field in the payload* (a wall clock, not a signed timestamp header) — it has no
  ``X-Calon-Timestamp`` to sign against. The :class:`OpenFlowVerifier` verifies that one
  header over the raw body **and** requires the payload's ``timestamp`` to be within
  :data:`OPENFLOW_CLOCK_WINDOW` of the instance's own clock (the ``now`` the route
  supplied). The precedence rule is the security guarantee: if the standard
  ``X-Calon-*`` headers are present they take over and the shim is not consulted at all.
* **The field mapping.** OpenFlow submissions are keyed by field id inside the payload's
  ``data`` object, and the same form can collect a hundred bookings. The mapping that a
  field id is ``start`` or ``email`` lives in the operator config
  (``[sources.openflow.fields.<formId>]``), one :class:`OpenFlowFieldMapping` per form.
  The adapter is handed the already-parsed mapping keyed by *form id*.
* **The idempotency key.** It is ``of:<formId>:<start answer>``, so an OpenFlow
  delivery-queue retry (same form, same answer) is replayed as one logical request.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from calon.intake.external import IntakeRequest
from calon.intake.external.openflow import (
    FIELD_KEY_PREFIX,
    OPENFLOW_CLOCK_WINDOW,
    OPENFLOW_SIGNATURE_HEADER,
    OpenFlowAdapter,
    OpenFlowFieldMapping,
    parse_openflow_fields,
)
from calon.intake.signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    IntakeAuthError,
    IntakeParseError,
    compute_signature,
)

#: The instant the "canonical" path's timestamp is signed at (a frozen clock).
NOW = datetime(2026, 9, 1, 7, 0, 0, tzinfo=UTC)
NOW_SECONDS = int(NOW.timestamp())

#: The instant the shim's payload ``timestamp`` should sit around (a frozen clock).
SIM_NOW = datetime(2026, 9, 1, 5, 0, 0, tzinfo=UTC)
SIM_NOW_ISO = SIM_NOW.isoformat()

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "openflow.json"

OPENFLOW_SECRET = "test-openflow-secret"
TZ = "Europe/Berlin"
FORM_ID = "af-test-form"

# The field ids this test form's ``data`` uses. A mapping entry points at one of these.
FIELD_IDS = {
    "start": "af_start",
    "end": "af_end",
    "name": "af_name",
    "email": "af_email",
    "phone": "af_phone",
    "subject": "af_subject",
}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(FIXTURE.read_text(encoding="utf-8")))


def _payload_bytes(fixture: dict[str, Any], variant: str) -> bytes:
    return json.dumps(fixture["variants"][variant]).encode("utf-8")


def _sign_shim(body: bytes, secret: str = OPENFLOW_SECRET) -> str:
    import hashlib
    import hmac

    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _shim_headers(body: bytes, *, secret: str = OPENFLOW_SECRET) -> dict[str, str]:
    """OpenFlow's wire format: one header, ``X-OpenFlow-Signature``, no timestamp header."""
    return {"Content-Type": "application/json", OPENFLOW_SIGNATURE_HEADER: _sign_shim(body, secret)}


def _canonical_headers(
    body: bytes, *, secret: str = OPENFLOW_SECRET, timestamp: int = NOW_SECONDS
) -> dict[str, str]:
    """The standard calon wire format — the shape ``docs/external-intake.md`` publishes."""
    ts = str(timestamp)
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: compute_signature(secret, ts, body)}


def _form_payload(
    form_id: str = FORM_ID,
    *,
    start: str = "2026-09-01T11:00:00+02:00",
    end: str | None = None,
    name: str = "Ada Lovelace",
    email: str = "ada@example.com",
    phone: str | None = None,
    subject: str | None = None,
    timestamp: str = SIM_NOW_ISO,
    **extra: Any,
) -> dict[str, Any]:
    """An OpenFlow submission whose field ids match :data:`FIELD_IDS`."""
    data: dict[str, Any] = {
        FIELD_IDS["start"]: start,
        FIELD_IDS["name"]: name,
        FIELD_IDS["email"]: email,
    }
    if end is not None:
        data[FIELD_IDS["end"]] = end
    if phone is not None:
        data[FIELD_IDS["phone"]] = phone
    if subject is not None:
        data[FIELD_IDS["subject"]] = subject
    data.update(extra)
    payload: dict[str, Any] = {
        "event": "submission",
        "formId": form_id,
        "formTitle": "Consultation booking",
        "data": data,
        "timestamp": timestamp,
    }
    return payload


def _signed_shim_request(form_id: str = FORM_ID, **kwargs: Any) -> IntakeRequest:
    body: bytes = json.dumps(_form_payload(form_id, **kwargs)).encode("utf-8")
    return IntakeRequest(source_slug="openflow", raw_body=body, headers=_shim_headers(body))


def _canonical_request(form_id: str = FORM_ID, **kwargs: Any) -> IntakeRequest:
    """A signed request in calon's canonical wire format (the precedence test)."""
    body: bytes = json.dumps(_form_payload(form_id, **kwargs)).encode("utf-8")
    return IntakeRequest(source_slug="openflow", raw_body=body, headers=_canonical_headers(body))


def _mapping() -> dict[str, OpenFlowFieldMapping]:
    """The per-form mapping for :data:`FORM_ID`, keyed by form id."""
    return {
        FORM_ID: OpenFlowFieldMapping(
            start=FIELD_IDS["start"],
            end=FIELD_IDS["end"],
            name=FIELD_IDS["name"],
            email=FIELD_IDS["email"],
            phone=FIELD_IDS["phone"],
            subject=FIELD_IDS["subject"],
            timezone=TZ,
        )
    }


def _adapter(**overrides: Any) -> OpenFlowAdapter:
    kwargs: dict[str, Any] = {
        "secret": OPENFLOW_SECRET,
        "resource_slug": "default",
        "field_mappings": _mapping(),
    }
    kwargs.update(overrides)
    return OpenFlowAdapter(**kwargs)


# --------------------------------------------------------------------------
# Construction + config parsing
# --------------------------------------------------------------------------


class TestConstruction:
    def test_adapter_builds_with_a_per_form_mapping(self, fixture: dict[str, Any]) -> None:
        field_mapping = fixture["mapping"]["of_form_001"]
        adapter = OpenFlowAdapter(
            secret=fixture["secret"],
            resource_slug="default",
            field_mappings={
                "of_form_001": OpenFlowFieldMapping(
                    start=field_mapping["start"],
                    end=field_mapping["end"],
                    name=field_mapping["name"],
                    email=field_mapping["email"],
                    phone=field_mapping["phone"],
                ),
            },
        )
        assert adapter.slug == "openflow"
        assert adapter.verify is not None
        assert adapter.parse is not None

    def test_an_adapter_without_fields_verifies_but_does_not_parse(self) -> None:
        adapter = OpenFlowAdapter(secret=OPENFLOW_SECRET)
        # Authentication still works (a pure function of the header and the secret)…
        adapter.verify(_signed_shim_request(), now=SIM_NOW)
        # …but every parse is rejected, because nothing is mapped (400, not 500).
        with pytest.raises(IntakeParseError, match="no field mapping configured"):
            adapter.parse(_signed_shim_request())

    def test_parse_openflow_fields_reads_the_config_table(self) -> None:
        raw = {
            "fld_start_id": {"start": "fld_start_id", "timezone": TZ},
            "fld_name_id": {"name": "fld_name_id"},
        }
        parsed = parse_openflow_fields("[sources.openflow.fields]", raw)
        assert set(parsed) == {"fld_start_id", "fld_name_id"}
        assert parsed["fld_start_id"].start == "fld_start_id"
        assert parsed["fld_name_id"].name == "fld_name_id"
        assert parsed["fld_start_id"].timezone == TZ

    def test_parse_openflow_fields_rejects_an_unknown_key(self) -> None:
        with pytest.raises(Exception, match="unrecognised field mapping key"):
            parse_openflow_fields("cfg", {"fld_x": {"start": "fld_x", "bogus": "nope"}})

    def test_parse_openflow_fields_rejects_a_non_table(self) -> None:
        with pytest.raises(Exception, match="must be a table"):
            parse_openflow_fields("cfg", cast("Mapping[str, Any]", ["not", "a", "table"]))


# --------------------------------------------------------------------------
# verify: the standard scheme
# --------------------------------------------------------------------------


class TestCanonicalVerify:
    """The standard ``X-Calon-*`` headers take precedence; the shim is not consulted."""

    def test_a_correctly_signed_canonical_request_verifies(self) -> None:
        _adapter().verify(_canonical_request(), now=NOW)  # must not raise

    def test_a_wrong_secret_fails(self) -> None:
        adapter = _adapter(secret="other-secret")
        with pytest.raises(IntakeAuthError):
            adapter.verify(_canonical_request(), now=NOW)

    def test_a_stale_timestamp_fails(self) -> None:
        adapter = _adapter()
        body: bytes = json.dumps(_form_payload()).encode("utf-8")
        headers = _canonical_headers(body, timestamp=NOW_SECONDS - 600)
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers=headers)
        with pytest.raises(IntakeAuthError, match="window"):
            adapter.verify(request, now=NOW)

    def test_a_missing_signature_header_fails(self) -> None:
        adapter = _adapter()
        body: bytes = json.dumps(_form_payload()).encode("utf-8")
        request = IntakeRequest(
            source_slug="openflow",
            raw_body=body,
            headers={TIMESTAMP_HEADER: str(NOW_SECONDS)},  # no X-Calon-Signature
        )
        with pytest.raises(IntakeAuthError, match="signature"):
            adapter.verify(request, now=NOW)


# --------------------------------------------------------------------------
# verify: the OpenFlow shim
# --------------------------------------------------------------------------


class TestShimVerify:
    """``X-OpenFlow-Signature`` over the raw body, plus a clock-windowed payload timestamp."""

    def test_a_shim_signed_request_verifies(self) -> None:
        _adapter().verify(_signed_shim_request(), now=SIM_NOW)  # must not raise

    def test_a_wrong_secret_fails_the_shim(self) -> None:
        adapter = _adapter(secret="other-secret")
        with pytest.raises(IntakeAuthError, match="does not match"):
            adapter.verify(_signed_shim_request(), now=SIM_NOW)

    def test_a_missing_shim_header_fails(self) -> None:
        adapter = _adapter()
        body: bytes = json.dumps(_form_payload()).encode("utf-8")
        request = IntakeRequest(
            source_slug="openflow", raw_body=body, headers={"Content-Type": "application/json"}
        )
        with pytest.raises(IntakeAuthError, match="no signature header"):
            adapter.verify(request, now=SIM_NOW)

    def test_a_payload_timestamp_outside_the_window_fails(self) -> None:
        adapter = _adapter()
        old = (SIM_NOW - OPENFLOW_CLOCK_WINDOW - timedelta(seconds=1)).isoformat()
        with pytest.raises(IntakeAuthError, match="window"):
            adapter.verify(_signed_shim_request(timestamp=old), now=SIM_NOW)

    def test_a_payload_timestamp_inside_the_window_verifies(self) -> None:
        adapter = _adapter()
        within = (SIM_NOW - timedelta(seconds=100)).isoformat()
        adapter.verify(_signed_shim_request(timestamp=within), now=SIM_NOW)  # not raise

    def test_a_payload_timestamp_far_before_fails(self) -> None:
        adapter = _adapter()
        early = (SIM_NOW - OPENFLOW_CLOCK_WINDOW - timedelta(hours=1)).isoformat()
        with pytest.raises(IntakeAuthError, match="window"):
            adapter.verify(_signed_shim_request(timestamp=early), now=SIM_NOW)

    def test_a_payload_without_a_timestamp_verifies(self) -> None:
        # A submission that carries no ``timestamp`` field at all: there is nothing to
        # window, so the signature alone is enough. (OpenFlow always sends one, but the
        # shim must not 401 the absent case.)
        adapter = _adapter()
        payload = _form_payload()
        del payload["timestamp"]
        body: bytes = json.dumps(payload).encode("utf-8")
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers=_shim_headers(body))
        adapter.verify(request, now=SIM_NOW)  # must not raise

    def test_a_naive_payload_timestamp_does_not_crash_verify(self) -> None:
        # Regression: comparing ``now - ts`` when ``ts`` parsed as naive (no offset,
        # and no "Z" for the ``.replace`` to turn into one) raised TypeError, which
        # no caller catches — the route answered 500 to an authenticated source
        # instead of treating the ambiguous clock the same as an unparseable one.
        adapter = _adapter()
        naive = "2026-09-01T05:00:00"  # no offset, no trailing Z
        adapter.verify(_signed_shim_request(timestamp=naive), now=SIM_NOW)  # must not raise

    def test_canonical_headers_take_precedence_over_the_shim(self) -> None:
        # A request carrying BOTH schemes verifies through the canonical path: the
        # canonical signature is computed over "<ts>.<body>", so a correct canonical
        # auth with a *wrong* shim header must still pass — proof the canonical path
        # was the one taken (the shim is not consulted).
        adapter = _adapter()
        body: bytes = json.dumps(_form_payload()).encode("utf-8")
        headers = {**_canonical_headers(body), OPENFLOW_SIGNATURE_HEADER: "bad-signature"}
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers=headers)
        adapter.verify(request, now=NOW)  # must not raise
        # …and the mirror image: a valid shim plus a *wrong* canonical signature
        # must fail through the canonical path (the shim is not consulted).
        headers2 = {**_shim_headers(body), **_canonical_headers(body, secret="other-secret")}
        request2 = IntakeRequest(source_slug="openflow", raw_body=body, headers=headers2)
        with pytest.raises(IntakeAuthError, match="signature"):
            adapter.verify(request2, now=NOW)


# --------------------------------------------------------------------------
# parse: the genuine payload → the canonical intent
# --------------------------------------------------------------------------


class TestParseFixture:
    """The real fixture, mapped, through the adapter."""

    def test_the_accept_variant_maps_to_the_canonical_intent(self, fixture: dict[str, Any]) -> None:
        field_mapping = fixture["mapping"]["of_form_001"]
        adapter = OpenFlowAdapter(
            secret=fixture["secret"],
            field_mappings={
                "of_form_001": OpenFlowFieldMapping(
                    start=field_mapping["start"],
                    end=field_mapping["end"],
                    name=field_mapping["name"],
                    email=field_mapping["email"],
                    phone=field_mapping["phone"],
                ),
            },
        )
        body = _payload_bytes(fixture, "accept")
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        intent = adapter.parse(request)

        assert intent.resource_slug == "default"
        assert intent.timezone == TZ
        assert intent.requester.name == "Ada Lovelace"
        assert intent.requester.email == "ada@example.com"
        assert intent.requester.phone == "+49 151 2345678"
        assert intent.subject == "Consultation booking: Ada Lovelace"  # formTitle: name
        # source_ref = "of:<formId>:<start answer>"
        assert intent.source_ref == "of:of_form_001:2026-09-01T10:00:00+02:00"
        # metadata: the provider's shape, untouched.
        assert set(intent.metadata) == {
            "event",
            "formTitle",
            "field_id",
            "data",
            "timestamp_utc",
        }
        assert intent.metadata["formTitle"] == "Consultation booking"
        assert intent.metadata["field_id"] == "of_form_001"
        assert intent.metadata["data"] == fixture["variants"]["accept"]["data"]
        assert intent.metadata["event"] == "submission"

    def test_a_variant_without_phone_or_end_maps_the_same(self, fixture: dict[str, Any]) -> None:
        # ``reject-early`` has no phone and no end in its ``data`` — both optional.
        field_mapping = fixture["mapping"]["of_form_001"]
        adapter = OpenFlowAdapter(
            secret=fixture["secret"],
            field_mappings={
                "of_form_001": OpenFlowFieldMapping(
                    start=field_mapping["start"],
                    end=field_mapping["end"],
                    name=field_mapping["name"],
                    email=field_mapping["email"],
                    phone=field_mapping["phone"],
                ),
            },
        )
        body = _payload_bytes(fixture, "reject-early")
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        intent = adapter.parse(request)

        assert intent.requester.phone is None
        assert intent.end is None  # the resource's default duration applies downstream
        assert intent.source_ref == "of:of_form_001:2026-09-01T07:00:00+02:00"

    def test_an_unmapped_form_is_a_parse_error(self, fixture: dict[str, Any]) -> None:
        field_mapping = fixture["mapping"]["of_form_001"]
        adapter = OpenFlowAdapter(
            secret=fixture["secret"],
            field_mappings={
                "of_form_001": OpenFlowFieldMapping(
                    start=field_mapping["start"],
                    end=field_mapping["end"],
                    name=field_mapping["name"],
                    email=field_mapping["email"],
                    phone=field_mapping["phone"],
                ),
            },
        )
        body = _payload_bytes(fixture, "no-mapping")
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        with pytest.raises(IntakeParseError, match="no field mapping configured"):
            adapter.parse(request)

    def test_a_replayed_variant_is_replayable_by_its_own_key(self, fixture: dict[str, Any]) -> None:
        # ``replay`` reuses of_form_002 (a *different* form), so its idempotency key
        # differs from ``accept``'s — a replay is per (formId, start answer).
        adapter = OpenFlowAdapter(
            secret=fixture["secret"],
            field_mappings={
                "of_form_002": OpenFlowFieldMapping(
                    start="fld_start",
                    end="fld_end",
                    name="fld_name",
                    email="fld_email",
                    phone=None,
                ),
            },
        )
        body = _payload_bytes(fixture, "replay")
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        intent = adapter.parse(request)
        assert intent.source_ref == "of:of_form_002:2026-09-01T10:00:00+02:00"


class TestParseEdgeCases:
    """The awkward fields the plan calls out (batch 1)."""

    def test_a_missing_data_object_is_a_parse_error(self) -> None:
        adapter = _adapter()
        body = (
            b'{"event":"submission","formId":"x","formTitle":"t",'
            b'"timestamp":"2026-09-01T00:00:00Z"}'
        )
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        with pytest.raises(IntakeParseError, match="no field mapping configured"):
            adapter.parse(request)

    def test_a_data_list_is_a_parse_error(self) -> None:
        adapter = OpenFlowAdapter(
            secret=OPENFLOW_SECRET,
            field_mappings={"x": OpenFlowFieldMapping(start="s", name="n", email="e")},
        )
        body = b'{"event":"submission","formId":"x","formTitle":"t","data":[1,2]}'
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        with pytest.raises(IntakeParseError, match="'data' object"):
            adapter.parse(request)

    def test_a_non_json_body_is_a_parse_error(self) -> None:
        adapter = _adapter()
        request = IntakeRequest(source_slug="openflow", raw_body=b"not json", headers={})
        with pytest.raises(IntakeParseError, match="not valid JSON"):
            adapter.parse(request)

    def test_a_missing_name_is_a_parse_error(self) -> None:
        adapter = _adapter()
        body = (
            json.dumps(
                {
                    "event": "submission",
                    "formId": FORM_ID,
                    "formTitle": "t",
                    "data": {
                        FIELD_IDS["email"]: "ada@example.com",
                        FIELD_IDS["start"]: "2026-09-01T11:00:00+02:00",
                    },
                    "timestamp": SIM_NOW_ISO,
                }
            )
        ).encode()
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        with pytest.raises(IntakeParseError, match="requester name"):
            adapter.parse(request)

    def test_an_unplausible_email_is_a_parse_error(self) -> None:
        adapter = _adapter()
        request = _signed_shim_request(email="not-an-email")
        with pytest.raises(IntakeParseError):
            adapter.parse(request)

    def test_an_unknown_timezone_is_a_parse_error(self) -> None:
        # The adapter resolves the configured IANA zone itself (it must localise the
        # start/end answers into aware instants), so an unresolvable zone is a
        # parse-time problem the route turns into a 400. We therefore assert
        # ``IntakeParseError`` (the adapter's one parse-time type) rather than the
        # schema's ``ValidationError`` — the adapter does not delegate the IANA lookup
        # to the schema because it needs the zone *before* the intent is built.

        adapter = OpenFlowAdapter(
            secret=OPENFLOW_SECRET,
            field_mappings={
                FORM_ID: OpenFlowFieldMapping(
                    start=FIELD_IDS["start"],
                    name=FIELD_IDS["name"],
                    email=FIELD_IDS["email"],
                    timezone="Not/AZone",
                ),
            },
        )
        with pytest.raises(IntakeParseError):
            adapter.parse(_signed_shim_request())

    def test_a_naive_start_answer_is_interpreted_in_the_forms_own_zone(self) -> None:
        # Regression: the adapter used to convert a naive start answer with
        # ``.astimezone(tz)``, which reinterprets a naive value from the *server
        # process's* local zone rather than the form's declared zone — the same
        # booking request would land at a different instant depending on the
        # host's ``TZ``. A naive answer must be read as already being in ``tz``.
        adapter = _adapter()
        request = _signed_shim_request(start="2026-09-01T11:00:00")  # no offset
        intent = adapter.parse(request)
        assert intent.start == datetime(2026, 9, 1, 11, 0, 0, tzinfo=ZoneInfo(TZ))

    def test_an_offset_start_answer_is_honoured_as_written(self) -> None:
        # An answer that already carries its own offset is not reinterpreted — only
        # re-expressed in the form's zone, exactly as the pre-naive-answer behaviour
        # already worked.
        adapter = _adapter()
        request = _signed_shim_request(start="2026-09-01T11:00:00+05:00")
        intent = adapter.parse(request)
        assert intent.start == datetime(2026, 9, 1, 11, 0, 0, tzinfo=UTC) + timedelta(hours=-5)

    def test_the_payload_timestamp_is_not_required(self) -> None:
        adapter = _adapter()
        payload = _form_payload()
        del payload["timestamp"]
        body: bytes = json.dumps(payload).encode("utf-8")
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        intent = adapter.parse(request)
        assert intent.metadata["timestamp_utc"] is None  # not an error, just recorded

    def test_an_unparseable_timestamp_is_none_not_a_error(self) -> None:
        adapter = _adapter()
        body: bytes = json.dumps(_form_payload(timestamp="not-a-clock")).encode("utf-8")
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        intent = adapter.parse(request)
        assert intent.metadata["timestamp_utc"] is None

    def test_a_missing_form_id_is_a_parse_error(self) -> None:
        adapter = _adapter()
        body = (json.dumps({"event": "submission", "data": {}, "timestamp": SIM_NOW_ISO})).encode()
        request = IntakeRequest(source_slug="openflow", raw_body=body, headers={})
        with pytest.raises(IntakeParseError, match="formId"):
            adapter.parse(request)

    def test_end_before_start_is_a_parse_error(self) -> None:
        adapter = _adapter()
        # end strictly before start: the resource's default duration would backfill,
        # but an explicit end-before-start is malformed as a span.
        request = _signed_shim_request(
            start="2026-09-01T12:00:00+02:00",
            end="2026-09-01T11:00:00+02:00",
        )
        with pytest.raises(IntakeParseError, match="not after"):
            adapter.parse(request)

    def test_the_idempotency_key_uses_the_form_id_and_the_start_answer(self) -> None:
        adapter = _adapter()
        r1 = _signed_shim_request(
            start="2026-09-01T11:00:00+02:00", name="Ada", email="a@example.com"
        )
        r2 = _signed_shim_request(
            start="2026-09-01T12:00:00+02:00", name="Ada", email="a@example.com"
        )
        i1 = adapter.parse(r1)
        i2 = adapter.parse(r2)
        assert i1.source_ref is not None and i2.source_ref is not None
        assert i1.source_ref == f"{FIELD_KEY_PREFIX}{FORM_ID}:2026-09-01T11:00:00+02:00"
        assert i1.source_ref == "of:af-test-form:2026-09-01T11:00:00+02:00"
        assert i2.source_ref.endswith("12:00:00+02:00")
        assert i1.source_ref != i2.source_ref  # different answers → different keys


__all__ = [
    "FIELD_IDS",
    "FORM_ID",
    "NOW",
    "NOW_SECONDS",
    "OPENFLOW_SECRET",
    "SIM_NOW",
    "SIM_NOW_ISO",
    "TZ",
    "TestCanonicalVerify",
    "TestConstruction",
    "TestParseEdgeCases",
    "TestParseFixture",
    "TestShimVerify",
]
