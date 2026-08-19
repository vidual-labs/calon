"""Microsoft Graph provider (ADR 0009) — Batch 5 placeholder.

This module is a typed stub until Batch 5 lands the real client. It exists now so that
``calon.calendars`` imports cleanly, ``CalendarProviderRegistry._build_provider`` type-checks
against a concrete return type, and the package's public surface is stable before the
real HTTP work. The real implementation (Batch 5) replaces this file with an
:class:`httpx.Client`-based ``MicrosoftGraphProvider``.
"""

from __future__ import annotations

from datetime import datetime

from calon.calendars import CalendarEvent, CalendarProvider, FreeBusySpan

_NOT_IMPLEMENTED = (
    "the Microsoft Graph provider lands in Batch 5 of Phase 9; "
    "configure [calendars.<slug>] only once it ships"
)


class MicrosoftGraphProvider(CalendarProvider):
    """Placeholder for the Microsoft Graph adapter (Batch 5).

    A real ``free_busy`` / ``upsert_event`` land in Batch 5. Constructing this
    placeholder raises so the registry surfaces a clear boot error — a resource that
    configures ``[calendars.<slug>] provider = "microsoft"`` before Batch 5 ships cannot
    silently pretend it is connected (CLAUDE.md §3).
    """

    name = "microsoft"

    def __init__(
        self,
        *,
        resource_slug: str,
        calendar_id: str,
        refresh_token: str = "",
    ) -> None:
        self.resource_slug = resource_slug
        self.calendar_id = calendar_id
        self.refresh_token = refresh_token
        raise NotImplementedError(
            "MicrosoftGraphProvider lands in Batch 5 of Phase 9; "
            "see docs/adr/0013 and .hermes/plans/2026-08-19-phase-9-calendar-sync.md"
        )

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def upsert_event(self, resource_slug: str, event: CalendarEvent) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)
