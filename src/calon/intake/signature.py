"""The cryptographic edge of external intake.

Pure module: no I/O, no database, no framework import, no wall clock — ``now`` is always
a parameter, the same rule that ``calon.domain`` obeys (``CLAUDE.md`` §4.1). A caller at
the I/O edge (the ``/api/v1/intake/{source_slug}`` route) supplies the raw request body,
the request headers, the per-source secret, and the current instant, and gets either a
canonical booking intent or a raised :class:`IntakeAuthError`.

``verify`` is the only thing an external source has to satisfy before its payload is
parsed: a signature over the raw body with a per-source shared secret, and a timestamp
inside a replay window. A failed check raises :class:`IntakeAuthError` with the class
name of what failed — the message is logged, not returned, so the error text is not a
hint oracle for an attacker (see ADR 0005 and 0012).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from typing import Any

__all__ = [
    "DEFAULT_TIMESTAMP_WINDOW",
    "IDEMPOTENCY_HEADER",
    "SIGNATURE_ALGORITHM",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "IntakeAuthError",
    "IntakeError",
    "IntakeIntegrityError",
    "IntakeParseError",
    "SourceConfig",
    "SourceContext",
    "SourceSpec",
    "compute_signature",
    "generate_secret",
    "resolve_idempotency_key",
    "verify_signature",
]

#: The HTTP header carrying the request's timestamp (seconds since the Unix epoch).
TIMESTAMP_HEADER = "X-Calon-Timestamp"
#: The HTTP header carrying the HMAC-SHA256 hex digest of ``"<timestamp>.<raw body>"``.
SIGNATURE_HEADER = "X-Calon-Signature"
#: The HTTP header carrying the idempotency key for the request.
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: The digest algorithm prefix the signature header must carry.
SIGNATURE_ALGORITHM = "sha256"
#: The default replay window: a request's timestamp must be within this of ``now``.
DEFAULT_TIMESTAMP_WINDOW = timedelta(seconds=300)


class IntakeError(ValueError):
    """Base for external-intake failures that the caller should translate to an HTTP response."""


class IntakeAuthError(IntakeError):
    """The signature or timestamp is not acceptable. Maps to ``HTTP 401 Unauthorized``."""


class IntakeParseError(IntakeError):
    """The payload did not map to the canonical booking intent. Maps to ``HTTP 400 Bad Request``."""


class IntakeIntegrityError(IntakeError):
    """An integrity check inside the intake route failed (e.g. idempotency race). Maps to 500-ish."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One configured external source, read from ``config/calon.toml``.

    ``enabled`` is the operator's explicit opt-in. A source with no entry in the config
    file, or one with ``enabled = false``, is invisible to the intake endpoint and
    receives ``404`` rather than ``401`` (so an unauthenticated caller cannot probe
    which slugs are actually enabled — see ADR 0012).
    """

    slug: str
    secret: str
    resource_slug: str = "default"
    timestamp_window: timedelta = field(default=DEFAULT_TIMESTAMP_WINDOW)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """A source in its resolved form: config plus the adapter bound to it.

    The adapter is a protocol (see :mod:`calon.intake.registry`); the spec is what the
    ``/intake/{source_slug}`` route pulls out of the registry and passes to
    ``verify`` and ``parse``.
    """

    slug: str
    adapter: Any  # SourceAdapter (typed at the registry level; Any here to keep this module pure)
    config: SourceConfig


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Per-request source context: the spec plus the raw request, for the route to verify/parse."""

    spec: SourceSpec
    headers: Mapping[str, str]
    body: bytes


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Return the ``sha256=<hex digest>`` value for the given raw request.

    The digest covers exactly the bytes ``<timestamp>.<raw body>`` — the ASCII digits of
    the timestamp, a single byte of ``.`` (0x2E, no space), then the body exactly as it
    arrived on the wire. The ``.`` is a length separator, the same trick ``AWS SigV4``
    and ``Stripe``-style webhooks use: without it, ``("17881080001", b"")`` and
    ``("1788108000", b"1")`` would hash to the same value, because both concatenate to
    ``17881080001``. Non-ASCII characters in the timestamp are a programming error, not
    input to worry about — a timestamp is integer seconds — and are rejected here rather
    than silently encoded as a different string.

    Callers sign with the same preimage — in Python::

        hmac.new(secret.encode(), timestamp.encode("ascii") + b"." + body, hashlib.sha256)

    — or the equivalent in their language of choice. Re-serializing the payload first
    would produce a different digest, so the raw body is the contract; do not JSON-parse
    before signing (ADR 0005).

    The body is not interpreted: it is the bytes of the request as they arrived. This
    matters for the raw-byte contract callers depend on, and is why a non-UTF-8 body
    must still sign, not raise, here — the parse step, not the signature, is where a
    bad body gets rejected, and only *after* the caller's identity has been established.
    """
    if not secret:
        raise ValueError("secret must be a non-empty string")
    if not timestamp:
        raise ValueError("timestamp must be a non-empty string")
    try:
        ts_bytes = timestamp.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("timestamp must be ASCII (integer seconds)") from exc
    data = ts_bytes + b"." + body
    digest = hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _parse_timestamp(raw: str | None) -> datetime:
    if raw is None or not raw.strip():
        raise IntakeAuthError(f"missing timestamp header {TIMESTAMP_HEADER!r}")
    try:
        ts = int(raw)
    except ValueError:
        raise IntakeAuthError(f"timestamp header {TIMESTAMP_HEADER!r} must be integer seconds") from None
    try:
        return datetime.fromtimestamp(ts, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise IntakeAuthError(f"timestamp header {TIMESTAMP_HEADER!r} is not a representable instant") from None


def verify_signature(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secret: str,
    now: datetime,
    window: timedelta = DEFAULT_TIMESTAMP_WINDOW,
) -> None:
    """Verify one signed request, or raise :class:`IntakeAuthError`.

    The check is constant-time: the digests are compared with ``hmac.compare_digest``,
    and the timestamp is parsed as an integer before any arithmetic on the window so a
    malformed timestamp raises rather than being silently treated as zero.
    """
    if not secret:
        raise IntakeAuthError("the source has no secret configured; check [sources.<slug>] in calon.toml")

    # --- timestamp: presence, format, and freshness are checked together ---------------
    ts_raw = headers.get(TIMESTAMP_HEADER)
    moment = _parse_timestamp(ts_raw)
    drift = now - moment
    if drift > window or -drift > window:
        raise IntakeAuthError("timestamp is outside the allowed replay window")

    # --- signature: prefix + digest ----------------------------------------------------
    supplied_raw = headers.get(SIGNATURE_HEADER)
    if supplied_raw is None or not supplied_raw.strip():
        raise IntakeAuthError(f"missing signature header {SIGNATURE_HEADER!r}")
    prefix, sep, supplied_digest = supplied_raw.partition("=")
    if not sep or prefix.strip().lower() != SIGNATURE_ALGORITHM:
        raise IntakeAuthError(f"signature header must be {SIGNATURE_ALGORITHM}=<hex digest>")
    expected = compute_signature(secret, ts_raw, body)
    expected_digest = expected.partition("=")[2]
    if not hmac.compare_digest(supplied_digest.lower(), expected_digest):
        raise IntakeAuthError("signature does not match")


def resolve_idempotency_key(
    header_value: str | None,
    source_ref: str | None,
) -> str | None:
    """The idempotency key for the request, or ``None`` when the source gave none.

    The :data:`IDEMPOTENCY_HEADER` header wins over the source's own ``source_ref``; a
    whitespace-only header value is treated as absent. The key is stored on the intent
    row and unique per source — a replay with the same key returns the outcome the
    first request produced, not a fresh evaluation (ADR 0005).
    """
    header = (header_value or "").strip()
    if header:
        return header
    ref = (source_ref or "").strip()
    return ref or None


def generate_secret() -> str:
    """A fresh 256-bit random secret, for operators generating ``[sources.<slug>]`` entries."""
    return secrets.token_hex(32)

