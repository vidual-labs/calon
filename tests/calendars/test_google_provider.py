"""Google Calendar provider: the real client against a scripted ``httpx.MockTransport``.

Pins the network behaviour of :class:`GoogleCalendarProvider` *without touching the
network*: every test wires a ``httpx.Client`` whose transport is a scripted in-memory
handler that plays the OAuth token endpoint and the Calendar v3 API. This is the whole
point of the ADR 0009/0013 design — the provider is the only network edge, so mocking
one ``httpx.Client`` stands in for the entire provider surface (CLAUDE.md: no new runtime
dependency; ``httpx`` is already transitive, and ``httpx.MockTransport`` is part of it).

Covered:
- a clean free/busy that parses busy spans correctly;
- the refresh-and-retry discipline — first call refreshes, a subsequent ``401`` triggers
  exactly one refresh and one retry, and a *second* consecutive ``401`` raises
  :class:`CalendarProviderError` (the caller degrades);
- an upsert that PATCHes by the booking's UID and falls back to POST on ``404``;
- the token endpoint returning a rotated refresh token that the provider adopts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from calon.calendars import CalendarEvent, CalendarProviderError
from calon.calendars.google import GoogleCalendarProvider
from calon.calendars.oauth import OAuthCredentials
from calon.domain import FreeBusySpan

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API = "https://www.googleapis.com/calendar/v3"


def at(hour: int, minute: int = 0) -> datetime:
    """A 2026-01-06 instant in UTC at the given hour/minute."""
    return datetime(2026, 1, 6, hour, minute, tzinfo=UTC)


class Scripted:
    """An in-memory handler that plays both the token endpoint and the Calendar API.

    ``token`` is the list of (access_token, expires_in, rotated_refresh) tuples the token
    endpoint returns, consumed in order (a ``None`` entry returns a 400 to model a dead
    grant). ``auth`` is the list of (status_code, json_body) responses returned for
    calendar API calls, consumed in order. ``seen`` records each request so a test can
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
        url = request.url.path
        self.seen.append({"method": request.method, "path": url, "body": request.content.decode()})
        if url.rsplit("/", 1)[-1] == "token" or "/oauth2.googleapis.com/token" in request.url.host:
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


def _provider(scripted: Scripted) -> GoogleCalendarProvider:
    """A provider wired to ``scripted`` with the standard seed refresh token."""
    return GoogleCalendarProvider(
        resource_slug="default",
        calendar_id="casa-milo@group.calendar.google.com",
        refresh_token="seed-refresh",
        credentials=OAuthCredentials(client_id="cid", client_secret="csecret"),
        client=scripted.client(),
    )


class TestFreeBusyHttp:
    def _busy(self, start: str, end: str) -> dict[str, object]:
        return {
            "calendars": {
                "casa-milo@group.calendar.google.com": {
                    "busy": [
                        {"start": start, "end": end},
                    ]
                }
            }
        }

    def test_a_clean_freebusy_parses_the_busy_spans(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh")],
            auth=[(200, self._busy("2026-01-06T10:30:00Z", "2026-01-06T11:30:00Z"))],
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

    def test_the_first_401_triggers_one_refresh_and_one_retry(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh"), ("tok-2", 3600, "rotated-2")],
            auth=[
                (401, {}),  # first attempt: the just-refreshed token is rejected
                (200, self._busy("2026-01-06T14:00:00Z", "2026-01-06T15:00:00Z")),
            ],
        )
        provider = _provider(scripted)
        spans = provider.free_busy("default", at(14, 0), at(15, 0))
        assert len(spans) == 1
        # Initial refresh (tok-1) + the retry refresh (tok-2) = two token posts; and
        # two calendar-API attempts (the rejected one + the retry that succeeds).
        n_token = sum(1 for s in scripted.seen if s["path"].endswith("/token"))
        n_api = sum(1 for s in scripted.seen if not s["path"].endswith("/token"))
        assert n_token == 2
        assert n_api == 2
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


class TestUpsertHttp:
    def _event(self) -> CalendarEvent:
        return CalendarEvent(
            uid="bk-42",
            summary="Consultation",
            starts_at_utc=at(10, 0),
            ends_at_utc=at(11, 0),
        )

    def test_upsert_patches_by_the_booking_uid(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh")],
            auth=[(200, {"id": "bk-42", "status": "confirmed"})],
        )
        provider = _provider(scripted)
        provider.upsert_event("default", self._event())
        # One token refresh, one PATCH to the caller-chosen id.
        patch_calls = [
            s
            for s in scripted.seen
            if s["method"] == "PATCH" and s["path"].endswith("/events/bk-42")
        ]
        assert len(patch_calls) == 1
        assert '"id": "bk-42"' in patch_calls[0]["body"] or '"id":"bk-42"' in patch_calls[0]["body"]
        assert provider._store.access_token == "tok-1"
        provider.close()

    def test_upsert_falls_back_to_post_on_404(self):
        scripted = Scripted(
            token=[("tok-1", 3600, "seed-refresh")],
            auth=[(404, {"error": {"code": 404, "message": "notFound"}}), (200, {"id": "bk-42"})],
        )
        provider = _provider(scripted)
        provider.upsert_event("default", self._event())
        methods = [s["method"] for s in scripted.seen if not s["path"].endswith("/token")]
        # A 404 on the PATCH followed by a POST to create it.
        assert methods == ["PATCH", "POST"]
        provider.close()
