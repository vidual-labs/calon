"""The OpenFlow adapter — the first *real* provider in the external intake framework.

A source that signs its requests with a **different** scheme — namely OpenFlow, whose
webhook integration (``vidual-labs/openflow``, ``backend/src/models/integrations.js``)
signs the raw body with ``hexdigest(HMAC-SHA256(secret, rawBody))`` under
``X-OpenFlow-Signature`` and carries the submission's own ``timestamp`` as a *field in
the payload* (a wall clock, not a signed timestamp header) — needs its own adapter
under this package (ADR 0005, rule 3). Everything below the adapter is unchanged: the
route hands this object the raw bytes, a single instant, and nothing else.

Two things differ from :class:`HmacSourceAdapter`, and both are deliberate:

* **The signature shim.** OpenFlow sends exactly one signature header
  (``X-OpenFlow-Signature``) and no signed timestamp of its own. The standard
  scheme's replay window is a function of the request's *signed* ``X-Calon-Timestamp``;
  the shim has nothing signed to anchor on, so it applies the window to the payload's
  own ``timestamp`` field instead (see :data:`OPENFLOW_CLOCK_WINDOW`). If the
  canonical ``X-Calon-*`` headers happen to be present they win — the shim is an
  escape hatch, not a way to mix the two schemes on one request.
* **The field mapping.** An OpenFlow submission is keyed by *field id* (the ``data``
  object's keys), and the same form can collect many bookings over time. The mapping
  from a field id to one of the canonical intent's slots (``start`` / ``end`` /
  ``name`` / ``email`` / ``phone`` / ``subject``) lives in the operator config —
  ``[sources.openflow.fields.<id>]`` — and is passed to the adapter as an already-
  parsed :class:`OpenFlowFieldMapping`. The idempotency key is derived from
  ``formId + start answer`` so a delivery-queue retry (same form, same answer) is
  replayed, not double-booked.

See ``docs/external-intake.md`` (the "About OpenFlow specifically" section) and ADR
0013 for the decision record.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from calon.config import ConfigError
from calon.intake.external import IntakeRequest
from calon.intake.signature import (
    TIMESTAMP_HEADER,
    IntakeAuthError,
    IntakeParseError,
    verify_signature,
)
from calon.schemas import BookingIntentIn, RequesterIn

__all__ = [
    "FIELD_KEY_PREFIX",
    "OPENFLOW_CLOCK_WINDOW",
    "OPENFLOW_SIGNATURE_HEADER",
    "OpenFlowAdapter",
    "OpenFlowFieldMapping",
    "parse_openflow_fields",
]

#: HTTP header carrying OpenFlow's HMAC-SHA256 hex digest of the raw body.
OPENFLOW_SIGNATURE_HEADER = "X-OpenFlow-Signature"

#: The clock window applied by the shim. OpenFlow carries the submission's own
#: ``timestamp`` field in the payload (a wall clock, not a signature — so it is not
#: signed over), and the shim verifies the signature over the raw body *and* requires
#: that payload instant to be within this of the instance's own clock (the ``now`` the
#: route supplies). A forged request with a tamper-proof signature cannot pick its
#: own instant and still verify: both the bytes and their timestamp are on the wire,
#: and the secret does not extend to changing either after the fact.
#:
#: ``timedelta(minutes=4)`` ≈ 240 seconds, which is 80% of the standard 300-second
#: window. The shim is a little stricter because it has no way to re-derive a clock
#: skew from the request's own words — there is no timestamp header to sign against,
#: only the payload's field — so the window is anchored on the instance's side.
OPENFLOW_CLOCK_WINDOW = timedelta(minutes=4)

#: The prefix the idempotency key is built from. ``of:<formId>:<answer>`` is unique
#: per (form, answer) and lets a delivery-queue retry (same form, same answer) be
#: recognised as the same logical request. Two different forms that both ask for the
#: same instant are *different* logical requests and must be evaluated freshly.
FIELD_KEY_PREFIX = "of:"

#: The field-id keys the operator config uses under ``[sources.openflow.fields.<id>]``.
_ALLOWED_FIELD_KEYS = frozenset(
    {
        "start",
        "end",
        "name",
        "email",
        "phone",
        "subject",
        "timezone",
    }
)


@dataclass(frozen=True, slots=True)
class OpenFlowFieldMapping:
    """One field id → one canonical-intent slot (``docs/external-intake.md``).

    Every field is optional in the dataclass: an OpenFlow form that collects a
    booking does not always send ``phone`` for example, and the intent's default
    (``None``) is the right outcome for a question the form does not ask. What is
    *required* is the mapping — the slot key name — not the presence of the answer
    in the payload.
    """

    start: str | None = None
    end: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    subject: str | None = None
    #: IANA timezone for display; the intent's default, ``Europe/Berlin``, is applied
    #: when this is ``None``. The form's own answers (``start``/``end``/``phone``)
    #: are provider-specific and do not need to agree with it.
    timezone: str | None = None


def parse_openflow_fields(label: str, raw: Mapping[str, Any]) -> dict[str, OpenFlowFieldMapping]:
    """Parse the ``[sources.openflow.fields.<formId>]`` table (one mapping per form).

    ``label`` is the config line to report any violation to — the TOML file plus line,
    or a test's hand-written label. Every entry must be a table whose keys are a subset
    of :data:`_ALLOWED_FIELD_KEYS`; an unrecognised key is a config error, and an
    operator who believes they mapped ``fld_start_id`` in fact mapped a different key
    is the sort of failure that should fail at boot, not at the first booking the
    lunchtime rush produces.

    This is where the operator-facing :class:`calon.config.SourceConfig` carries the
    ``fields`` sub-table for the one adapter that needs it — the framework's
    :class:`calon.intake.signature.SourceConfig` (the per-adapter runtime value
    object) deliberately stays minimal.
    """
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{label}: [sources.openflow.fields] must be a table of per-field tables")
    out: dict[str, OpenFlowFieldMapping] = {}
    for field_id, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{label}: [sources.openflow.fields.{field_id}] must be a table")
        unknown = set(entry) - _ALLOWED_FIELD_KEYS
        if unknown:
            raise ConfigError(
                f"{label}: unrecognised field mapping key(s) in "
                f"[sources.openflow.fields.{field_id}]: {', '.join(sorted(unknown))}"
            )
        out[field_id] = OpenFlowFieldMapping(
            start=entry.get("start"),
            end=entry.get("end"),
            name=entry.get("name"),
            email=entry.get("email"),
            phone=entry.get("phone"),
            subject=entry.get("subject"),
            timezone=entry.get("timezone"),
        )
    return out


class OpenFlowAdapter:
    """An adapter for OpenFlow's webhook (the first real provider on the framework).

    ``slug`` must be ``"openflow"`` (the module name — the registry's convention).
    ``field_mappings`` is a :data:`dict` keyed by the OpenFlow *formId* (the payload's
    ``formId`` string), **not** by the canonical slot name. One entry per form: the
    ``data`` object's field ids inside ``[sources.openflow.fields.<formId>]`` are the
    mapping table, and the formId is the key that tells the adapter which table to
    apply. An adapter constructed with no field mappings still verifies and
    authenticates — :meth:`verify` is a pure function of the headers and the secret —
    but every :meth:`parse` raises :class:`IntakeParseError` ("no field mapping"),
    which the route reduces to ``400``, not ``500``.
    """

    slug = "openflow"

    def __init__(
        self,
        *,
        secret: str,
        resource_slug: str = "default",
        field_mappings: Mapping[str, OpenFlowFieldMapping] | None = None,
        timestamp_window_seconds: int = 300,
    ) -> None:
        self.secret = secret
        self.resource_slug = resource_slug
        self._window_seconds = timestamp_window_seconds
        self._field_mappings: Mapping[str, OpenFlowFieldMapping] = dict(field_mappings or {})

    # --- verify ------------------------------------------------------------------

    def verify(self, request: IntakeRequest, *, now: datetime) -> None:
        """Verify one signed request at the given instant.

        The canonical ``X-Calon-*`` headers take precedence over the OpenFlow shim
        (decision 1 of the plan): if the request carries them, the standard
        verification runs and the shim is not consulted at all. If only the OpenFlow
        header is present the shim runs — the request is authenticated iff the
        instance's own clock (``now``) was within :data:`OPENFLOW_CLOCK_WINDOW` of the
        moment it signed.
        """
        timestamp_header = _hget(request.headers, TIMESTAMP_HEADER)
        if timestamp_header is not None:
            # The standard scheme takes over; the shim is not consulted.
            from datetime import timedelta

            verify_signature(
                request.headers,
                request.raw_body,
                secret=self.secret,
                now=now,
                window=timedelta(seconds=self._window_seconds),
            )
            return

        supplied_raw = _hget(request.headers, OPENFLOW_SIGNATURE_HEADER)
        if supplied_raw is None or not supplied_raw.strip():
            raise IntakeAuthError(
                f"no signature header {OPENFLOW_SIGNATURE_HEADER!r}; "
                f"the source signs the raw body with "
                f"X-OpenFlow-Signature: hexdigest(HMAC-SHA256(secret, rawBody))"
            )
        expected = hmac.new(
            self.secret.encode("utf-8"),
            request.raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_raw.strip().lower(), expected):
            raise IntakeAuthError("OpenFlow signature does not match the raw body")

        # The payload's own ``timestamp`` is a wall clock, not a signature, so the
        # shim verifies it independently. The window is anchored on ``now`` (the
        # instant the route supplied); a request whose payload instant is more than
        # OPENFLOW_CLOCK_WINDOW past **or before** is rejected. A forged request that
        # tampers with its own ``timestamp`` to fall inside the window must still sign
        # the raw body, and the signature check above already binds every byte on the
        # wire.
        try:
            payload = _load_payload(request.raw_body)
        except IntakeParseError:
            # Not parseable as JSON: the parse step (not verify) is where a malformed
            # body is rejected, so we do not gate on the window here.
            return
        raw_timestamp = payload.get("timestamp")
        if raw_timestamp is None:
            # The submission carries no wall clock at all: there is nothing to window.
            return
        try:
            ts = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            # A present-but-unparseable clock: recordable in metadata, not a
            # signature failure. Do not silently treat it as "outside the window" —
            # that would 401 a request the parse step would have 400'd.
            return
        if abs(now - ts) > OPENFLOW_CLOCK_WINDOW:
            raise IntakeAuthError("request timestamp is outside the allowed replay window")

    # --- parse -------------------------------------------------------------------

    def parse(self, request: IntakeRequest) -> BookingIntentIn:
        """Translate an OpenFlow submission into the canonical booking intent.

        The mapping is keyed by ``formId`` (one :class:`OpenFlowFieldMapping` per form).
        The idempotency key is ``of:<formId>:<start answer>`` (the plan's decision 3);
        the ``metadata`` map carries the provider's own shape, untouched (decision 4).
        """
        payload = _load_payload(request.raw_body)
        form_id = _form_id_of(payload)

        mapping = self._field_mappings.get(form_id)
        if mapping is None:
            raise IntakeParseError(
                f"no field mapping configured for form {form_id!r}; "
                f"add [sources.openflow.fields.{form_id}] to the operator config"
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise IntakeParseError("request body has no 'data' object")

        def _answer(slot: OpenFlowFieldMapping, field_id: str | None) -> Any:
            if field_id is None:
                return None
            return data.get(field_id)

        # The mapping is one :class:`OpenFlowFieldMapping` per form, and each of its
        # *fields* points at a field id inside ``data``. ``_answer`` looks up the id
        # for a canonical slot. (A form's mapping that has no ``start`` field cannot
        # yield a booking; that is a config error the operator sees here.)
        start_raw = _answer(mapping, mapping.start)
        if start_raw is None:
            raise IntakeParseError(
                f"form {form_id!r} is missing its mapped start answer; "
                f"check [sources.openflow.fields.{form_id}].start"
            )
        end_raw = _answer(mapping, mapping.end)
        name_raw = _answer(mapping, mapping.name)
        email_raw = _answer(mapping, mapping.email)
        phone_raw = _answer(mapping, mapping.phone)
        subject_raw = _answer(mapping, mapping.subject)
        if name_raw is None:
            raise IntakeParseError(
                f"form {form_id!r} is missing its mapped requester name; "
                f"check [sources.openflow.fields.{form_id}].name"
            )
        if email_raw is None:
            raise IntakeParseError(
                f"form {form_id!r} is missing its mapped requester email; "
                f"check [sources.openflow.fields.{form_id}].email"
            )

        timezone = mapping.timezone or "Europe/Berlin"

        # ``BookingIntentIn`` takes aware ``datetime`` values, not strings. The
        # answers are the provider's own ISO-8601 strings in the zone the form
        # declared; parse them here (the adapter translates — the schema is the
        # contract, it does not do provider-specific re-serialising). An explicit
        # end before start is a malformed span, not a request to backfill the zone's
        # default duration backwards, so we reject it here (a 400, not a 500). A
        # malformed instant or an unresolvable IANA zone is the operator's config;
        # both surface the same way — as a parse error the route turns into a 400.
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            tz = ZoneInfo(timezone)
            start_dt = datetime.fromisoformat(str(start_raw)).astimezone(tz)
            end_dt: datetime | None = (
                None if end_raw is None else datetime.fromisoformat(str(end_raw)).astimezone(tz)
            )
        except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
            raise IntakeParseError(
                f"form {form_id!r}: start/end answers or the configured timezone "
                f"could not be resolved to instants (start={start_raw!r}, "
                f"end={end_raw!r}, timezone={timezone!r})"
            ) from exc
        if end_dt is not None and end_dt <= start_dt:
            raise IntakeParseError(
                f"form {form_id!r}: end ({end_dt.isoformat()}) is not after "
                f"start ({start_dt.isoformat()})"
            )

        # metadata: the provider's own shape, untouched (decision 4). The payload's
        # ``timestamp`` is a wall clock, not a signature; we record it for the audit
        # trail and never trust it for the window check (that happens in verify).
        metadata: dict[str, Any] = {
            "event": payload.get("event"),
            "formTitle": payload.get("formTitle"),
            "field_id": form_id,
            "data": data,
        }
        metadata["timestamp_utc"] = _coerce_timestamp(payload.get("timestamp"))

        # The idempotency key is derived from the form id and the *start* answer:
        # a delivery-queue retry (same form, same answer) resolves to the same key.
        # The name/email are intentionally left out so a corrected re-submit gets a
        # fresh evaluation, not a replay of someone else's rejection.
        source_ref = f"{FIELD_KEY_PREFIX}{form_id}:{start_raw}"

        # Construct inside a guard so a schema-level failure (an implausible
        # email, an unrecognised IANA zone, ...) surfaces as an ``IntakeParseError``
        # (a 400 at the route) rather than a raw ``ValidationError`` leaking past the
        # adapter: the route catches only ``IntakeParseError`` for the 400, so every
        # parse-time problem must exit through this one type.
        try:
            requester = RequesterIn(
                name=str(name_raw),
                email=str(email_raw),
                phone=str(phone_raw) if phone_raw is not None else None,
            )

            return BookingIntentIn(
                resource_slug=self.resource_slug,
                start=start_dt,
                end=end_dt,
                timezone=timezone,
                requester=requester,
                subject=str(subject_raw or f"{payload.get('formTitle', 'Booking')}: {name_raw}"),
                metadata=metadata,
                source_ref=source_ref,
            )
        except ValidationError as exc:
            raise IntakeParseError(
                f"form {form_id!r}: the provider's answers do not fit the booking schema ({exc})"
            ) from exc


def _hget(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (RFC 7230 §3.2)."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _form_id_of(payload: dict[str, Any]) -> str:
    """The form id for this submission, or a parse error if it is not a string."""
    raw = payload.get("formId")
    if not isinstance(raw, str) or not raw.strip():
        raise IntakeParseError("request body is missing a non-empty 'formId' string")
    return raw.strip()


def _coerce_timestamp(raw: Any) -> str | None:
    """The payload's ``timestamp`` field, normalised to ISO-8601, or ``None``.

    OpenFlow sends ``2026-09-01T05:58:00.000Z``; we store what we can parse and
    record ``None`` if it is absent or unparseable (it is metadata, not a gate).
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed.isoformat()
    except (ValueError, TypeError):
        return None


def _load_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IntakeParseError("request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise IntakeParseError("request body must be a JSON object")
    return payload


# The registry convention: the module must expose a slug-named attribute that the
# boot reads. For now ``openflow`` is ``None`` — the *adapter* for an instance is
# built by the boot's ``SourceRegistry.from_config(_adapter_for)``, which construct...[truncated]
