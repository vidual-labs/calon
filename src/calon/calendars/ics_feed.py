"""Published-ICS-feed provider (ADR 0017) — free/busy without an OAuth app.

Google Calendar and Outlook / Microsoft 365 both let a *user* publish a secret ICS URL
from the calendar's own settings, with no developer console, no app registration, and no
admin rights. That is the whole point of this provider: an operator who cannot register
an OAuth client — the common case on a managed tenant, or for anyone who simply does not
want to — pastes one URL and gets their real commitments respected by the rule chain.

What it is, precisely:

* **Read-only.** ``writable`` is ``False`` and :meth:`upsert_event` never writes. Feed
  publishing is one-way; the requester's ``.ics`` handoff and the Google/Outlook
  deeplinks remain how a booking reaches a calendar on this path.
* **Eventually consistent, at the publisher's pace.** Providers cache these feeds hard;
  an edit can take hours to appear in the published copy. calon adds its own short cache
  on top (:data:`DEFAULT_CACHE_TTL_SECONDS`) so an availability check does not re-fetch
  the whole calendar on every request. Both delays are the operator's to accept — the
  panel says so.

Failures degrade, they never refuse a booking (``CLAUDE.md`` §2): any transport error,
oversized body, or unparseable document raises :class:`CalendarProviderError`, which the
registry turns into an empty free/busy answer.

The URL is operator-supplied and fetched server-side. calon deliberately does **not**
blocklist private address ranges: a self-hoster pointing at a Nextcloud or Radicale feed
on their own LAN is a first-class use of this, and the operator already controls what the
process reads. See ADR 0017 for that call.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from calon.calendarkit._ics_busy import IcsFeedError, busy_spans_from_ics
from calon.calendars import CalendarEvent, CalendarProviderError, FreeBusySpan

__all__ = ["DEFAULT_CACHE_TTL_SECONDS", "IcsFeedProvider"]

logger = logging.getLogger("calon.calendars.ics_feed")

#: How long a fetched feed is reused before calon asks for it again. Short enough that an
#: operator's change shows up in minutes once the publisher has caught up, long enough
#: that a burst of availability checks is one request rather than dozens.
DEFAULT_CACHE_TTL_SECONDS = 300

#: Refuse to buffer more than this from a feed. A calendar of any plausible size is far
#: below it; the cap is what stops a hostile or broken URL from exhausting memory.
MAX_FEED_BYTES = 5 * 1024 * 1024

_TIMEOUT_SECONDS = 10.0


class IcsFeedProvider:
    """A published ICS URL adapted to the :class:`~calon.calendars.CalendarProvider` contract.

    ``timezone`` is the resource's own IANA zone, used to read all-day and floating
    events — the values an ICS feed leaves for the reader to interpret.
    """

    name = "ics"
    #: Read-only: the write-back skips this provider entirely rather than auditing a
    #: failure per booking (a feed cannot be written to, so a failure would be noise).
    writable = False

    def __init__(
        self,
        *,
        resource_slug: str,
        feed_url: str,
        timezone: str = "UTC",
        client: httpx.Client | None = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        scheme = urlsplit(feed_url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError("a calendar feed URL must be http:// or https://")
        self.resource_slug = resource_slug
        self.feed_url = feed_url
        self.timezone = timezone
        self._client = client
        self._owns_client = client is None
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_text: str | None = None
        self._cached_at: float | None = None

    # -- the contract ------------------------------------------------------

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        """Busy spans from the feed, clipped to the window."""
        text = self._feed_text()
        try:
            return busy_spans_from_ics(
                text,
                window_start_utc=window_start_utc,
                window_end_utc=window_end_utc,
                default_timezone=self.timezone,
                reason="calendar feed",
            )
        except IcsFeedError as exc:
            raise CalendarProviderError(f"ics feed: {exc}") from exc

    def upsert_event(self, resource_slug: str, event: CalendarEvent) -> None:
        """Nothing to do: a published feed is read-only (see :attr:`writable`).

        Deliberately a no-op rather than an error. The caller already skips a
        non-writable provider; this keeps the contract total if some future caller does
        not, and a silent no-op is the honest outcome — no write was attempted, so
        nothing failed.
        """
        return None

    # -- fetching ----------------------------------------------------------

    def _feed_text(self) -> str:
        """The feed body, from the short-lived cache when it is still fresh."""
        now = time.monotonic()
        if (
            self._cached_text is not None
            and self._cached_at is not None
            and now - self._cached_at < self._cache_ttl_seconds
        ):
            return self._cached_text

        text = self._fetch()
        self._cached_text = text
        self._cached_at = now
        return text

    def _fetch(self) -> str:
        client = self._client or httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        try:
            response = client.get(self.feed_url, headers={"accept": "text/calendar, */*"})
            if response.status_code >= 400:
                # The URL itself is a secret (it is what authorizes the read), so it is
                # never echoed into the message — the resource slug identifies which feed.
                raise CalendarProviderError(
                    f"ics feed for {self.resource_slug!r} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            content = response.content
        except httpx.HTTPError as exc:
            raise CalendarProviderError(
                f"ics feed for {self.resource_slug!r} could not be fetched: {type(exc).__name__}"
            ) from exc
        finally:
            if self._owns_client:
                client.close()

        if len(content) > MAX_FEED_BYTES:
            raise CalendarProviderError(
                f"ics feed for {self.resource_slug!r} is larger than "
                f"{MAX_FEED_BYTES // (1024 * 1024)} MB and was not read"
            )
        return content.decode("utf-8", errors="replace")
