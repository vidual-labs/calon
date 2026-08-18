"""Unit tests for the cryptographic edge of external intake.

These are pure and live next to the domain tests for the same reason: ``signature.py``
imports nothing but the standard library, so nothing here needs a database, an app, or a
fixture at all. Time is always passed in, never read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from calon.intake.signature import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    IntakeAuthError,
    compute_signature,
    generate_secret,
    resolve_idempotency_key,
    verify_signature,
)

NOW = datetime(2026, 9, 1, 6, 0, 0, tzinfo=UTC)
NOW_SECONDS = int(NOW.timestamp())
SECRET = "test-secret-do-not-use-in-prod"
BODY = b'{"requester": {"name": "Ada Lovelace"}}'


def signed_headers(
    *,
    timestamp: int = NOW_SECONDS,
    secret: str = SECRET,
    body: bytes = BODY,
    algorithm_prefix: str = "sha256",
) -> dict[str, str]:
    """The header map a well-formed caller would send for this body."""
    digest = compute_signature(secret, str(timestamp), body).partition("=")[2]
    return {
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: f"{algorithm_prefix}={digest}",
        IDEMPOTENCY_HEADER: "req-001",
    }


class TestComputeSignature:
    def test_is_deterministic_and_prefixed(self) -> None:
        first = compute_signature(SECRET, "1788108000", BODY)
        second = compute_signature(SECRET, "1788108000", BODY)
        assert first == second
        assert first.startswith("sha256=")

    def test_changes_with_the_secret(self) -> None:
        a = compute_signature("secret-a", "1788108000", BODY)
        b = compute_signature("secret-b", "1788108000", BODY)
        assert a != b

    def test_changes_with_the_timestamp(self) -> None:
        a = compute_signature(SECRET, "1788108000", BODY)
        b = compute_signature(SECRET, "1788108001", BODY)
        assert a != b

    def test_changes_with_the_body(self) -> None:
        a = compute_signature(SECRET, "1788108000", b"one")
        b = compute_signature(SECRET, "1788108000", b"two")
        assert a != b

    def test_requires_a_non_empty_secret(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            compute_signature("", "1788108000", BODY)

    def test_requires_a_timestamp(self) -> None:
        with pytest.raises(ValueError, match="timestamp"):
            compute_signature(SECRET, "", BODY)


class TestVerifySignature:
    def test_a_correctly_signed_request_passes(self) -> None:
        verify_signature(signed_headers(), BODY, secret=SECRET, now=NOW)

    def test_the_digest_covers_the_raw_body_not_a_re_serialization(self) -> None:
        # Signing the re-serialized payload is a classic integration bug: the digest must
        # be over exactly the bytes that were on the wire.
        signed = signed_headers(body=b'{"name":"ada"}')
        verify_signature(signed, b'{"name":"ada"}', secret=SECRET, now=NOW)
        with pytest.raises(IntakeAuthError, match="signature"):
            verify_signature(signed, b'{"ada":{"name"}}', secret=SECRET, now=NOW)

    def test_a_wrong_secret_fails(self) -> None:
        headers = signed_headers()
        with pytest.raises(IntakeAuthError):
            verify_signature(headers, BODY, secret="some-other-secret", now=NOW)

    def test_a_tampered_signature_fails(self) -> None:
        headers = signed_headers()
        tampered = "sha256=" + "0" * 64
        with pytest.raises(IntakeAuthError, match="signature"):
            verify_signature({**headers, SIGNATURE_HEADER: tampered}, BODY, secret=SECRET, now=NOW)

    def test_missing_signature_header_fails(self) -> None:
        headers = dict(signed_headers())
        del headers[SIGNATURE_HEADER]
        with pytest.raises(IntakeAuthError, match="signature"):
            verify_signature(headers, BODY, secret=SECRET, now=NOW)

    def test_wrong_algorithm_prefix_fails(self) -> None:
        headers = signed_headers(algorithm_prefix="md5")
        with pytest.raises(IntakeAuthError, match="sha256="):
            verify_signature(headers, BODY, secret=SECRET, now=NOW)

    def test_a_fresh_timestamp_inside_the_window_passes(self) -> None:
        verify_signature(signed_headers(timestamp=NOW_SECONDS - 10), BODY, secret=SECRET, now=NOW)

    def test_a_timestamp_just_inside_the_window_edge_passes(self) -> None:
        window = timedelta(seconds=300)
        headers = signed_headers(timestamp=NOW_SECONDS - 299)
        verify_signature(headers, BODY, secret=SECRET, now=NOW, window=window)

    def test_a_timestamp_outside_the_window_fails(self) -> None:
        headers = signed_headers(timestamp=NOW_SECONDS - 301)
        with pytest.raises(IntakeAuthError, match="window"):
            verify_signature(headers, BODY, secret=SECRET, now=NOW)

    def test_a_future_timestamp_outside_the_window_fails(self) -> None:
        headers = signed_headers(timestamp=NOW_SECONDS + 400)
        with pytest.raises(IntakeAuthError, match="window"):
            verify_signature(headers, BODY, secret=SECRET, now=NOW)

    def test_a_missing_timestamp_header_fails(self) -> None:
        headers = dict(signed_headers())
        del headers[TIMESTAMP_HEADER]
        with pytest.raises(IntakeAuthError, match="timestamp"):
            verify_signature(headers, BODY, secret=SECRET, now=NOW)

    def test_a_non_integer_timestamp_fails(self) -> None:
        headers = signed_headers()
        headers[TIMESTAMP_HEADER] = "not-a-number"
        with pytest.raises(IntakeAuthError, match="integer"):
            verify_signature(headers, BODY, secret=SECRET, now=NOW)

    def test_an_unrepresentable_timestamp_fails(self) -> None:
        headers = signed_headers()
        headers[TIMESTAMP_HEADER] = "99999999999999999999"
        with pytest.raises(IntakeAuthError, match="representable"):
            verify_signature(headers, BODY, secret=SECRET, now=NOW)

    def test_an_unconfigured_source_fails_before_any_header_is_inspected(self) -> None:
        with pytest.raises(IntakeAuthError, match="no secret"):
            verify_signature(signed_headers(), BODY, secret="", now=NOW)

    def test_error_messages_do_not_disclose_which_check_failed_last(self) -> None:
        # The caller should be able to distinguish "not signed correctly" from "signed
        # but stale" without an oracle: a wrong digest and a stale timestamp both fail,
        # and neither message names the other's header.
        with pytest.raises(IntakeAuthError) as bad_digest:
            verify_signature(dict(signed_headers(), **{SIGNATURE_HEADER: "sha256=" + "0" * 64}), BODY, secret=SECRET, now=NOW)
        assert TIMESTAMP_HEADER not in str(bad_digest.value)

        with pytest.raises(IntakeAuthError) as stale:
            verify_signature(signed_headers(timestamp=NOW_SECONDS - 3600), BODY, secret=SECRET, now=NOW)
        assert "does not match" not in str(stale.value)


class TestResolveIdempotencyKey:
    def test_the_header_wins(self) -> None:
        assert resolve_idempotency_key("from-header", "from-ref") == "from-header"

    def test_the_source_ref_is_the_fallback(self) -> None:
        assert resolve_idempotency_key(None, "from-ref") == "from-ref"

    def test_whitespace_header_falls_through_to_the_ref(self) -> None:
        assert resolve_idempotency_key("   ", "from-ref") == "from-ref"

    def test_no_key_when_there_is_nothing(self) -> None:
        assert resolve_idempotency_key(None, None) is None
        assert resolve_idempotency_key("   ", "   ") is None


class TestGenerateSecret:
    def test_is_64_hex_chars(self) -> None:
        secret = generate_secret()
        assert len(secret) == 64
        int(secret, 16)  # raises if not valid hex

    def test_is_random(self) -> None:
        assert generate_secret() != generate_secret()
