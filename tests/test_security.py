"""Unit tests for ``calon.security``'s session table.

The password-hashing primitives are exercised indirectly through the login/logout HTTP
tests in ``tests/test_calendar_handoff.py``; this file covers ``SessionTable`` in
isolation, where the eviction behaviour below is otherwise untested.
"""

from __future__ import annotations

from calon.security import SessionTable

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
