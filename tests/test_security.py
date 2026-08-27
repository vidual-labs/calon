"""Unit tests for ``calon.security``'s session table.

The password-hashing primitives are exercised indirectly through the login/logout HTTP
tests in ``tests/test_calendar_handoff.py``; this file covers ``SessionTable`` in
isolation, where the eviction behaviour below is otherwise untested.
"""

from __future__ import annotations

from calon.security import SessionTable, new_oauth_state, verify_oauth_state

KEY = b"0" * 32


class TestSessionTable:
    def test_issue_returns_a_token_that_is_valid(self) -> None:
        table = SessionTable(KEY, ttl_seconds=3600)
        token = table.issue(created_at=1000.0)
        assert table.is_valid(token, now=1000.0)

    def test_a_token_is_invalid_once_its_ttl_has_passed(self) -> None:
        table = SessionTable(KEY, ttl_seconds=100)
        token = table.issue(created_at=1000.0)
        assert table.is_valid(token, now=1099.0)
        assert table.is_valid(token, now=1100.0)  # expires_at is inclusive
        assert not table.is_valid(token, now=1100.01)

    def test_an_unknown_token_is_invalid(self) -> None:
        table = SessionTable(KEY, ttl_seconds=3600)
        assert not table.is_valid("never-issued")

    def test_none_and_empty_tokens_are_invalid(self) -> None:
        table = SessionTable(KEY, ttl_seconds=3600)
        assert not table.is_valid(None)
        assert not table.is_valid("")

    def test_revoke_invalidates_immediately(self) -> None:
        table = SessionTable(KEY, ttl_seconds=3600)
        token = table.issue(created_at=1000.0)
        table.revoke(token)
        assert not table.is_valid(token, now=1000.0)

    def test_revoke_all_clears_every_session(self) -> None:
        table = SessionTable(KEY, ttl_seconds=3600)
        tokens = [table.issue(created_at=1000.0) for _ in range(3)]
        table.revoke_all()
        assert len(table) == 0
        assert not any(table.is_valid(t, now=1000.0) for t in tokens)

    def test_expired_sessions_are_evicted_rather_than_accumulating_forever(self) -> None:
        # Regression: is_valid() already treats an expired record as invalid, but
        # nothing ever removed the row itself, so a long-running instance's session
        # table grew by one entry per login for its entire uptime.
        table = SessionTable(KEY, ttl_seconds=100)
        stale = table.issue(created_at=1000.0)
        assert len(table) == 1

        # A login well after the stale token expired sweeps it out.
        fresh = table.issue(created_at=2000.0)
        assert len(table) == 1
        assert not table.is_valid(stale, now=2000.0)
        assert table.is_valid(fresh, now=2000.0)

    def test_a_still_valid_session_is_not_evicted_by_another_login(self) -> None:
        table = SessionTable(KEY, ttl_seconds=1000)
        first = table.issue(created_at=1000.0)
        second = table.issue(created_at=1001.0)
        assert len(table) == 2
        assert table.is_valid(first, now=1001.0)
        assert table.is_valid(second, now=1001.0)


class TestOAuthState:
    """The signed ``state`` value used by the calendar connect flow (ADR 0014)."""

    def test_a_freshly_issued_state_verifies_to_its_resource_slug(self) -> None:
        state = new_oauth_state(KEY, "default", now=1000.0)
        assert verify_oauth_state(KEY, state, now=1000.0) == "default"

    def test_a_state_signed_with_a_different_key_is_rejected(self) -> None:
        state = new_oauth_state(KEY, "default", now=1000.0)
        other_key = b"1" * 32
        assert verify_oauth_state(other_key, state, now=1000.0) is None

    def test_a_tampered_state_is_rejected(self) -> None:
        state = new_oauth_state(KEY, "default", now=1000.0)
        payload, signature = state.rsplit(".", 1)
        tampered = f"{payload}x.{signature}"
        assert verify_oauth_state(KEY, tampered, now=1000.0) is None

    def test_a_state_outside_the_ttl_window_is_rejected(self) -> None:
        state = new_oauth_state(KEY, "default", now=1000.0)
        assert verify_oauth_state(KEY, state, ttl_seconds=600, now=1600.0) == "default"
        assert verify_oauth_state(KEY, state, ttl_seconds=600, now=1601.0) is None

    def test_a_state_from_the_future_beyond_the_window_is_also_rejected(self) -> None:
        # Guards against a state minted with a clock far ahead of this process's clock.
        state = new_oauth_state(KEY, "default", now=2000.0)
        assert verify_oauth_state(KEY, state, ttl_seconds=600, now=1000.0) is None

    def test_a_malformed_state_is_rejected_rather_than_raising(self) -> None:
        assert verify_oauth_state(KEY, "not-a-real-state") is None
        assert verify_oauth_state(KEY, "") is None
        assert verify_oauth_state(KEY, "onlyonepart") is None

    def test_the_resource_slug_round_trips_even_with_a_colon_in_the_payload_split(self) -> None:
        # rsplit(":", 1) is used to separate the slug from the timestamp, so a slug is
        # safe even though the payload format itself uses ":" as a separator.
        state = new_oauth_state(KEY, "my-resource", now=1000.0)
        assert verify_oauth_state(KEY, state, now=1000.0) == "my-resource"
