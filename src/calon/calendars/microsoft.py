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

Free/busy: ``POST /users/{user}/calendar/getSchedule`` scoped to the requested UTC
window (Graph v1.0 has no ``getFreeBusy`` action on this path); a ``scheduleItems``
entry counts only when its ``status`` is exactly ``"busy"`` — ``free``, ``tentative``,
``oof``, and ``workingElsewhere`` are intentionally ignored, since only a definite
conflict should narrow what calon offers.

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
    FreeBusySpan,
)
from calon.calendars.oauth import (
    OAuthCredentials,
    ProviderTransport,
)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_ONE_DAY = timedelta(days=1)


class MicrosoftGraphProvider(ProviderTransport):
    """Microsoft Graph adapter implementing :class:`CalendarProvider` (ADR 0009).

    See the module docstring for the refresh-and-retry discipline and the re-runnable
    by-UID upsert design. A ``client`` may be injected for tests (typically wrapping
    ``httpx.MockTransport``); when none is given the provider owns and closes its own.
    The transport (client lifecycle, refresh-and-retry) is :class:`ProviderTransport`;
    this class owns only the endpoints and payload shapes that are Graph's own.
    """

    name = "microsoft"
    provider_name = "microsoft"
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    def __init__(
        self,
        *,
        resource_slug: str,
        calendar_id: str,
        refresh_token: str = "",
        credentials: OAuthCredentials | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(refresh_token=refresh_token, credentials=credentials, client=client)
        self.resource_slug = resource_slug
        # For the Graph API the per-resource handle is the mailbox *user*.
        self.user = calendar_id

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        """Fetch provider busy spans overlapping the window (ADR 0009).

        ``POST /users/{user}/calendar/getSchedule`` scoped to this resource's mailbox
        (Graph v1.0 has no ``getFreeBusy`` action on this path — ``getSchedule`` is the
        real one). Only a ``scheduleItems`` entry whose ``status`` is exactly
        ``"busy"`` reduces availability; ``free``, ``tentative``, ``oof``, and
        ``workingElsewhere`` are ignored on purpose — only a definite conflict should
        narrow what calon offers. The request is always made in the ``UTC`` zone (a
        valid identifier Graph accepts directly, unlike the Windows zone names it
        otherwise uses), so the response's ``start``/``end`` are read as UTC too. A
        provider that reports no busy time returns an empty tuple; an unreachable /
        dead provider raises :class:`CalendarProviderError` (the caller degrades to
        Calon-only data).
        """
        body = {
            "schedules": [self.user],
            "startTime": {"dateTime": _naive_rfc3339(window_start_utc), "timeZone": "UTC"},
            "endTime": {"dateTime": _naive_rfc3339(window_end_utc), "timeZone": "UTC"},
        }
        response = self._request(
            "POST", f"{_GRAPH_BASE}/users/{self.user}/calendar/getSchedule", json_body=body
        )
        data = response.json() if response.content else {}
        schedules = data.get("value") or []
        items = schedules[0].get("scheduleItems") if schedules else []
        spans: list[FreeBusySpan] = []
        for item in items or []:
            if not isinstance(item, dict) or item.get("status") != "busy":
                continue
            start = (item.get("start") or {}).get("dateTime")
            end = (item.get("end") or {}).get("dateTime")
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
            # Graph's dateTimeTimeZone shape requires both keys; a "dateTime" with no
            # "timeZone" is a malformed event, not an implicit-UTC shorthand.
            "start": {"dateTime": _naive_rfc3339(event.starts_at_utc), "timeZone": "UTC"},
            "end": {"dateTime": _naive_rfc3339(event.ends_at_utc), "timeZone": "UTC"},
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


def _naive_rfc3339(moment: datetime) -> str:
    """Render an aware datetime as the naive local string Graph's ``dateTimeTimeZone``
    shape wants — no offset, no ``Z``, paired with an explicit ``timeZone`` key.

    Every caller here always requests/sends ``"UTC"`` as that ``timeZone``, so the
    instant is first normalised to UTC before the offset is dropped.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_rfc3339(text: str) -> datetime:
    """Parse a Graph timestamp into an aware UTC datetime.

    Graph's free/busy responses carry ``dateTime`` as a *naive* string (no offset —
    e.g. ``"2026-08-20T09:00:00.0000000"``) alongside a separate ``timeZone`` field,
    which our caller has already resolved to UTC by requesting the schedule in UTC.
    A naive parse is therefore treated as already being UTC rather than reinterpreted
    via ``astimezone`` — that would instead convert it *from* the server process's own
    local zone, shifting every busy span by whatever offset the host happens to run
    in. Some Graph endpoints do emit a numeric offset; an already-aware value is
    honoured as written and only normalised to UTC.
    """
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
