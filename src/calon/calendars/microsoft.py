"""Microsoft Graph provider (ADR 0009), the real client.

Adapts one resource's Microsoft 365 calendar to the two-method :class:`CalendarProvider`
contract over the Microsoft Graph API (``https://graph.microsoft.com/v1.0/``) using
``httpx`` (already a transitive dependency — no new runtime dependency is introduced;
ADR 0013).

Authentication is a self-contained refresh cycle against the v2.0 common tenant:
``POST https://login.microsoftonline.com/common/oauth2/v2.0/token`` with
``grant_type=refresh_token``. The constructor takes the resource's seed *refresh token*
(from the TOML) plus the operator's app-level :class:`OAuthCredentials`; the provider
keeps a :class:`TokenStore`, refreshes on demand, and refreshes **at most once per
call** on a ``401`` (the common "expired access token" case). A second ``401`` means
the grant is dead, so the call raises :class:`CalendarProviderError` and the caller
degrades to Calon-only data (CLAUDE.md §2, ADR 0009 / 0013).

Per-resource handle: the ``calendar_id`` config value names the *user* (the mailbox
owner, e.g. a UPN or mail-alias) whose calendar is synced — the Graph API addresses
calendar data as ``/users/{user}/``. There is no separate calendar id in the common
case (the mailbox's default calendar).

Free/busy: ``POST /users/{user}/calendar/getFreeBusy`` scoped to the requested UTC
window; the response's ``busy`` list is the source of truth (``available``/``tentative``
are intentionally ignored — only definite busy time reduces Calon availability).

Upsert: Microsoft Graph does **not** allow a caller-chosen event id, so
``upsert_event`` is a *re-runnable by UID* flow: it first locates an existing event
whose ``iCalUID`` matches the booking's UID (list the resource's ``calendarView`` for
the event's own day and match); if one is found it ``PATCH /users/{user}/events/{id}``
it, otherwise it ``POST /users/{user}/events`` to create it (carrying ``iCalUID`` so a
later re-run can find it). Either path leaves the booking as exactly one event keyed by
that UID; re-running the write-back never creates a duplicate (ADR 0009 Consequences).

No secret is ever echoed into a log line or an exception string (CLAUDE.md §8).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from calon.calendars import (
    CalendarEvent,
    CalendarProviderError,
    FreeBusySpan,
)
from calon.calendars.oauth import (
    OAuthCredentials,
    TokenStore,
    calendar_error,
    refresh_access_token,
)

_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_ERROR_PROVIDER = "microsoft"
_ONE_DAY = timedelta(days=1)


class MicrosoftGraphProvider:
    """Microsoft Graph adapter implementing :class:`CalendarProvider` (ADR 0009).

    See the module docstring for the refresh-and-retry discipline and the re-runnable
    by-UID upsert design. A ``client`` may be injected for tests (typically wrapping
    ``httpx.MockTransport``); when none is given the provider owns and closes its own.
    """

    name = "microsoft"

    def __init__(
        self,
        *,
        resource_slug: str,
        calendar_id: str,
        refresh_token: str = "",
        credentials: OAuthCredentials | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.resource_slug = resource_slug
        # For the Graph API the per-resource handle is the mailbox *user*.
        self.user = calendar_id
        self._credentials = credentials or OAuthCredentials()
        self._store = TokenStore(refresh_token=refresh_token)
        self._client = client
        self._owns_client = client is None

    def _client_instance(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)
            self._owns_client = True
        return self._client

    def close(self) -> None:
        """Close the provider's own HTTP client (a no-op if one was injected)."""
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _refresh(self) -> None:
        """Refresh the access token once, or raise if the grant cannot be refreshed."""
        try:
            access, expires_in, refresh = refresh_access_token(
                self._client_instance(),
                token_url=_TOKEN_URL,
                credentials=self._credentials,
                refresh_token=self._store.refresh_token,
            )
        except CalendarProviderError:
            raise
        self._store.adopt(access, expires_in, refresh)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one HTTP request, refreshing the token once on a ``401`` and retrying.

        The first failure after a refresh re-raises the :class:`CalendarProviderError` as
        a dead grant; the caller (free_busy / upsert_event) lets it propagate, and the
        registry degrades. A transport error is wrapped the same way.
        """
        if not self._store.is_fresh():
            self._refresh()
        headers = {"Authorization": f"Bearer {self._store.access_token}"}
        client = self._client_instance()
        try:
            response = client.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise calendar_error(
                _ERROR_PROVIDER, f"transport error ({type(exc).__name__})"
            ) from exc
        if response.status_code == 401:
            # One and only refresh-and-retry: a dead grant must not loop forever.
            self._refresh()
            headers = {"Authorization": f"Bearer {self._store.access_token}"}
            try:
                response = client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                raise calendar_error(
                    _ERROR_PROVIDER, f"transport error after refresh ({type(exc).__name__})"
                ) from exc
            if response.status_code == 401:
                raise calendar_error(
                    _ERROR_PROVIDER, "refreshed token still rejected (401); grant is dead"
                )
        if response.status_code >= 400:
            raise calendar_error(
                _ERROR_PROVIDER,
                f"{method} {url} returned {response.status_code}",
            )
        return response

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        """Fetch provider busy spans overlapping the window (ADR 0009).

        ``POST /users/{user}/calendar/getFreeBusy`` scoped to this resource's mailbox.
        Only definite ``busy`` time reduces availability; ``available``/``tentative``
        are ignored on purpose. A provider that reports no busy time returns an empty
        tuple; an unreachable / dead provider raises :class:`CalendarProviderError`
        (the caller degrades to Calon-only data).
        """
        body = {
            "startDateTime": _rfc3339(window_start_utc),
            "endDateTime": _rfc3339(window_end_utc),
            "includeOnlineMeetings": False,
        }
        response = self._request(
            "POST", f"{_GRAPH_BASE}/users/{self.user}/calendar/getFreeBusy", json_body=body
        )
        data = response.json() if response.content else {}
        busy_list = ((data.get("responses") or [{}])[0].get("busy")) or []
        spans: list[FreeBusySpan] = []
        for entry in busy_list:
            if not (isinstance(entry, list) and len(entry) >= 2):
                continue
            start, end = entry[0], entry[1]
            if not start or not end:
                continue
            spans.append(
                FreeBusySpan(
                    starts_at_utc=_parse_rfc3339(start),
                    ends_at_utc=_parse_rfc3339(end),
                    reason="provider report",
                )
            )
        return tuple(spans)

    def upsert_event(self, resource_slug: str, event: CalendarEvent) -> None:
        """Create or update the event keyed by ``event.uid`` (re-runnable, ADR 0009).

        The Graph API does not accept a caller-chosen event id, so this is a read-first
        flow: list the resource's ``calendarView`` for the event's own day and match any
        existing event by ``iCalUID``. If one is found, ``PATCH`` it; otherwise ``POST``
        a new event carrying ``iCalUID``. Either way exactly one event carries that UID,
        so a re-run of the write-back is idempotent.
        """
        payload: dict[str, Any] = {
            "subject": event.summary,
            "iCalUID": event.uid,
            "start": {"dateTime": _rfc3339(event.starts_at_utc)},
            "end": {"dateTime": _rfc3339(event.ends_at_utc)},
        }
        if event.description:
            payload["body"] = {"contentType": "text", "content": event.description}
        existing_id = self._find_event_id_by_uid(event.uid, event.starts_at_utc)
        if existing_id is not None:
            patch_url = f"{_GRAPH_BASE}/users/{self.user}/events/{existing_id}"
            self._request("PATCH", patch_url, json_body=payload)
        else:
            self._request("POST", f"{_GRAPH_BASE}/users/{self.user}/events", json_body=payload)

    def _find_event_id_by_uid(self, uid: str, when_utc: datetime) -> str | None:
        """The id of the event carrying ``iCalUID == uid`` on ``when_utc``'s day, else None.

        The lookup is scoped to the event's own day (a calendarView with a start/end of
        that day) so it is cheap and stable; a match is an ``iCalUID`` equality.
        """
        day_start = when_utc.astimezone(UTC).date()
        start_dt = datetime(day_start.year, day_start.month, day_start.day, tzinfo=UTC)
        end_dt = start_dt + _ONE_DAY
        response = self._request(
            "GET",
            f"{_GRAPH_BASE}/users/{self.user}/calendarView",
            params={
                "startDateTime": _rfc3339(start_dt),
                "endDateTime": _rfc3339(end_dt),
            },
        )
        data = response.json() if response.content else {}
        for item in data.get("value") or []:
            if item.get("iCalUID") == uid and item.get("id"):
                return str(item["id"])
        return None


def _rfc3339(moment: datetime) -> str:
    """Render an aware datetime as an ISO 8601 instant with a ``Z`` suffix."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339(text: str) -> datetime:
    """Parse an ISO 8601 instant (Graph emits a numeric offset) into an aware UTC datetime."""
    return datetime.fromisoformat(text).astimezone(UTC)
