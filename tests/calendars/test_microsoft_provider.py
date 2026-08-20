"""Microsoft Graph provider: the real client against a scripted ``httpx.MockTransport``.

Pins the network behaviour of :class:`MicrosoftGraphProvider` *without touching the
network*: every test wires a ``httpx.Client`` whose transport is a scripted in-memory
handler that plays the v2.0 token endpoint and the Graph calendar API. This is the whole
point of the ADR 0009/0013 design — the provider is the only network edge, so mocking
one ``httpx.Client`` stands in for the entire provider surface (CLAUDE.md: no new runtime
dependency; ``httpx`` is already transitive, and ``httpx.MockTransport`` is part of it).

Covered:
- a clean free/busy (``getFreeBusy``) that parses busy spans correctly;
- the refresh-and-retry discipline — first call refreshes, a subsequent ``401`` triggers
  exactly one refresh and one retry, and a *second* consecutive ``401`` raises
  :class:`CalendarProviderError` (the caller degrades);
- the re-runnable-by-UID upsert: ``calendarView`` is consulted first; when an event
  carries the booking's ``iCalUID`` it is ``PATCH``ed, and when no such event exists a
  ``POST`` creates it (so a re-run never duplicates).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from calon.calendars import CalendarEvent, CalendarProviderError
from calon.calendars.microsoft import MicrosoftGraphProvider
from calon.calendars.oauth import OAuthCredentials
from calon.domain import FreeBusySpan

_TOKEN_HOST = "login.microsoftonline.com"
_GRAPH = "https://graph.microsoft.com/v1.0"
_USER = "casa-milo@vidual.org"


def at(hour: int, minute: int = 0) -> datetime:
    """A 2026-01-06 instant in UTC at the given hour/minute."""
    return datetime(2026, 1, 6, hour, minute, tzinfo=UTC)


def _busy_body(start: str, end: str) -> dict[str, object]:
    """A getFreeBusy response carrying one definite busy span."""
    return {"responses": [{"busy": [[start, end]]}]}


class Scripted:
    """An in-memory handler that plays both the token endpoint and the Graph API.

    ``token`` is the list of (access_token, expires_in, rotated_refresh) tuples the token
    endpoint returns, consumed in order (a ``None`` entry returns a 400 to model a dead
    grant). ``auth`` is the list of (status_code, json_body) responses returned for
    Graph calendar calls, consumed in order. ``seen`` records each request so a test can
    assert *how* the provider called the API.
    """

    def __init__(
        self,
        *,
        token: list[tuple[str, int, str]] | None = None,
        auth: list[tuple[int, dict[str, object]]] | None = None,
    ) -> None:
        self.token = list(token or [])
        self.auth = list(auth or [])
        self.seen: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.seen.append(
            {"method": request.method, "path": url.path, "body": request.content.decode()}
        )
        if url.host.endswith(_TOKEN_HOST):
            if not self.token:
                return httpx.Response(400, json={"error": "invalid_grant"})
            entry = self.token.pop(0)
            if entry is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            access, expires_in, rotated = entry
            return httpx.Response(
                200,
                json={
                    "access_token": access,
                    "expires_in": expires_in,
                    "refresh_token": rotated,
                    "token_type": "Bearer",
                },
            )
        if not self.auth:
            return httpx.Response(500, json={"error": "scripted: no auth response remaining"})
        status, body = self.auth.pop(0)
        return httpx.Response(status, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _provider(scripted: Scripted) -> MicrosoftGraphProvider:
    """A provider wired to ``scripted`` with the standard seed refresh token."""
    return MicrosoftGraphProvider(
        resource_slug="default",
        calendar_id=_USER,
        refresh_token="seed-refresh",
        credentials=OAuthCredentials(client_id="cid", client_secret="csecret"),
        client=scripted.client(),
    )


def _n_token(scripted: Scripted) -> int:
    return sum(1 for s in scripted.seen if s["path"].endswith("/token"))


def _n_api(scripted: Scripted) -> int:
    return sum(1 for s in scripted.seen if not s["path"].endswith("/token"))


class TestParseRfc3339:
    """Regression: a naive Graph timestamp must not be read as server-local time."""

    def test_a_naive_timestamp_is_treated_as_utc_regardless_of_the_hosts_tz(self):
        import os
        import time

        from calon.calendars.microsoft import _parse_rfc3339

        naive = "2026-08-20T09:00:00.0000000"  # Graph's own shape: no offset
        original = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
                os.environ["TZ"] = zone
                time.tzset()
                assert _parse_rfc3339(naive) == datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    def test_an_offset_timestamp_is_honoured_as_written(self):
        from calon.calendars.microsoft import _parse_rfc3339

        assert _parse_rfc3339("2026-08-20T09:00:00+02:00") == datetime(
            2026, 8, 20, 7, 0, tzinfo=UTC
        )


class TestFreeBusyGraph:
    def test_a_clean_freebusy_parses_the_busy_spans(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh")],
            auth=[(200, _busy_body("2026-01-06T10:30:00+00:00", "2026-01-06T11:30:00+00:00"))],
        )
        provider = _provider(scripted)
        spans = provider.free_busy("default", at(10, 0), at(12, 0))
        assert isinstance(spans, tuple) and len(spans) == 1
        span = spans[0]
        assert isinstance(span, FreeBusySpan)
        assert span.starts_at_utc == at(10, 30)
        assert span.ends_at_utc == at(11, 30)
        assert span.reason == "provider report"
        assert provider._store.access_token == "tok-1"
        provider.close()

    def test_the_first_401_triggers_one_refresh_and_one_retry(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh"), ("tok-2", 3600, "rotated-2")],
            auth=[
                (401, {}),  # first attempt: the just-refreshed token is rejected
                (200, _busy_body("2026-01-06T14:00:00+00:00", "2026-01-06T15:00:00+00:00")),
            ],
        )
        provider = _provider(scripted)
        spans = provider.free_busy("default", at(14, 0), at(15, 0))
        assert len(spans) == 1
        # Initial refresh (tok-1) + the retry refresh (tok-2) = two token posts; and
        # two calendar-API attempts (the rejected one + the retry that succeeds).
        assert _n_token(scripted) == 2
        assert _n_api(scripted) == 2
        assert provider._store.access_token == "tok-2"
        provider.close()

    def test_a_second_consecutive_401_raises_the_provider_error(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh"), ("tok-2", 3600, "rotated-2")],
            auth=[(401, {}), (401, {})],
        )
        provider = _provider(scripted)
        with pytest.raises(CalendarProviderError) as exc_info:
            provider.free_busy("default", at(14, 0), at(15, 0))
        assert "401" in str(exc_info.value)
        # The rotated refresh was adopted before the final 401 was surfaced.
        assert provider._store.refresh_token == "rotated-2"
        provider.close()


class TestUpsertGraph:
    def _event(self) -> CalendarEvent:
        return CalendarEvent(
            uid="bk-42",
            summary="Consultation",
            starts_at_utc=at(10, 0),
            ends_at_utc=at(11, 0),
        )

    def test_upsert_patches_an_existing_event_found_by_its_ical_uid(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh")],
            auth=[
                # calendarView finds the existing event carrying iCalUID "bk-42".
                (200, {"value": [{"iCalUID": "bk-42", "id": "g-event-1"}]}),
                (200, {}),  # the PATCH that updates it
            ],
        )
        provider = _provider(scripted)
        provider.upsert_event("default", self._event())
        api = [s for s in scripted.seen if not s["path"].endswith("/token")]
        assert [s["method"] for s in api] == ["GET", "PATCH"]
        patch = api[1]
        assert patch["path"].endswith(f"/users/{_USER}/events/g-event-1")
        assert "bk-42" in patch["body"]
        provider.close()

    def test_upsert_creates_when_no_existing_event_carries_the_uid(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh")],
            auth=[
                # calendarView finds nothing; the provider then POSTs to create it.
                (200, {"value": []}),
                (201, {"id": "g-event-new"}),
            ],
        )
        provider = _provider(scripted)
        provider.upsert_event("default", self._event())
        api = [s for s in scripted.seen if not s["path"].endswith("/token")]
        assert [s["method"] for s in api] == ["GET", "POST"]
        post = api[1]
        assert post["path"].endswith(f"/users/{_USER}/events")
        assert "bk-42" in post["body"]
        provider.close()
