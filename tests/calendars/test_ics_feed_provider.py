"""The published-ICS-feed provider (ADR 0017): fetching, caching, and degrading.

The parsing itself is covered by ``tests/test_ics_busy.py``; what is pinned here is the
provider's side of the contract — that it fetches once per TTL, that every failure mode
becomes a :class:`CalendarProviderError` the registry can degrade on, that a feed never
leaks its (secret) URL into an error, and that it is read-only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from calon.calendars import CalendarEvent, CalendarProviderError, CalendarProviderRegistry
from calon.calendars.ics_feed import MAX_FEED_BYTES, IcsFeedProvider

FEED_URL = "https://calendar.example.com/secret-token/basic.ics"

_FEED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:a@example.com\r\n"
    "DTSTART:20260310T090000Z\r\nDTEND:20260310T100000Z\r\n"
    "SUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)

WINDOW_START = datetime(2026, 3, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 4, 1, tzinfo=UTC)


def _client(
    *,
    body: str | bytes = _FEED,
    status_code: int = 200,
    calls: list[httpx.Request] | None = None,
    error: type[httpx.HTTPError] | None = None,
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if error is not None:
            raise error("boom")
        content = body.encode("utf-8") if isinstance(body, str) else body
        return httpx.Response(status_code, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _provider(**kwargs: object) -> IcsFeedProvider:
    defaults: dict[str, object] = {
        "resource_slug": "default",
        "feed_url": FEED_URL,
        "timezone": "Europe/Berlin",
        "client": _client(),
    }
    defaults.update(kwargs)
    return IcsFeedProvider(**defaults)  # type: ignore[arg-type]


class TestFreeBusy:
    def test_it_reads_busy_time_from_the_feed(self) -> None:
        provider = _provider()
        spans = provider.free_busy("default", WINDOW_START, WINDOW_END)
        assert [(span.starts_at_utc, span.ends_at_utc) for span in spans] == [
            (datetime(2026, 3, 10, 9, 0, tzinfo=UTC), datetime(2026, 3, 10, 10, 0, tzinfo=UTC))
        ]

    def test_the_feed_is_fetched_once_per_cache_window(self) -> None:
        calls: list[httpx.Request] = []
        provider = _provider(client=_client(calls=calls))
        for _ in range(3):
            provider.free_busy("default", WINDOW_START, WINDOW_END)
        assert len(calls) == 1

    def test_a_zero_ttl_refetches_every_time(self) -> None:
        calls: list[httpx.Request] = []
        provider = _provider(client=_client(calls=calls), cache_ttl_seconds=0)
        for _ in range(3):
            provider.free_busy("default", WINDOW_START, WINDOW_END)
        assert len(calls) == 3


class TestFailures:
    def test_an_http_error_status_raises_without_echoing_the_url(self) -> None:
        """The feed URL is the credential — it must never reach a log or an exception."""
        provider = _provider(client=_client(status_code=404))
        with pytest.raises(CalendarProviderError) as caught:
            provider.free_busy("default", WINDOW_START, WINDOW_END)
        assert "404" in str(caught.value)
        assert "secret-token" not in str(caught.value)

    def test_a_transport_failure_raises(self) -> None:
        provider = _provider(client=_client(error=httpx.ConnectError))
        with pytest.raises(CalendarProviderError):
            provider.free_busy("default", WINDOW_START, WINDOW_END)

    def test_a_body_that_is_not_a_calendar_raises(self) -> None:
        provider = _provider(client=_client(body="<html>login page</html>"))
        with pytest.raises(CalendarProviderError):
            provider.free_busy("default", WINDOW_START, WINDOW_END)

    def test_an_oversized_feed_is_refused(self) -> None:
        provider = _provider(client=_client(body=b"x" * (MAX_FEED_BYTES + 1)))
        with pytest.raises(CalendarProviderError, match="larger than"):
            provider.free_busy("default", WINDOW_START, WINDOW_END)

    def test_a_non_http_url_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="http"):
            _provider(feed_url="file:///etc/passwd")

    def test_the_registry_degrades_instead_of_failing_a_booking(self) -> None:
        """CLAUDE.md §2: a broken feed narrows nothing, it never refuses."""
        registry = CalendarProviderRegistry({"default": _provider(client=_client(status_code=500))})
        assert registry.free_busy("default", WINDOW_START, WINDOW_END) == ()


class TestReadOnly:
    def test_the_provider_declares_itself_unwritable(self) -> None:
        assert _provider().writable is False

    def test_the_registry_reports_the_resource_as_not_written_back(self) -> None:
        registry = CalendarProviderRegistry({"default": _provider()})
        assert registry.writes_back("default") is False

    def test_an_upsert_through_the_registry_is_a_no_op(self) -> None:
        calls: list[httpx.Request] = []
        registry = CalendarProviderRegistry({"default": _provider(client=_client(calls=calls))})
        registry.upsert_event(
            "default",
            CalendarEvent(
                uid="booking@calon",
                summary="Consultation",
                starts_at_utc=datetime(2026, 3, 11, 9, 0, tzinfo=UTC),
                ends_at_utc=datetime(2026, 3, 11, 10, 0, tzinfo=UTC),
            ),
        )
        assert calls == []
