"""The calendar handoff: an RFC 5545 file and provider deeplinks.

calon never authenticates against a calendar provider (ADR 0004). When a booking is
accepted, it hands the requester two things that get the event onto their calendar:

* **The ``.ics`` file** — the baseline. ``render`` produces a ``VCALENDAR`` containing a
  single ``VEVENT``: UTC instants with the ``Z`` suffix, ``METHOD:PUBLISH``, a stable
  ``UID``, and an incrementing ``SEQUENCE``. Any calendar that has ever read RFC 5545 —
  Google Calendar, Outlook, Microsoft 365, Apple, Thunderbird, Fastmail — can add it,
  and a second download of the same event **updates** the entry in place instead of
  duplicating it.
* **Provider deeplinks** — the convenience layer. One-click "add to Google Calendar" /
  "add to Outlook" buttons built from the same event. They carry no ``UID`` and cannot
  deduplicate; that is why the ICS file is the contract and the links are the shortcut.

The two layers are described in [docs/calendar-handoff.md](../docs/calendar-handoff.md)
and justified in [ADR 0004](../docs/adr/0004-ics-first-calendar-handoff.md).

The module lives in ``calendarkit`` rather than ``calendar`` because ``import calendar``
resolves to the standard library (``CLAUDE.md`` §5: never shadow a stdlib module name).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "CalendarEvent",
    "build_deeplinks",
    "build_ics",
    "event_uid",
    "ics_filename",
]


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """The data the handoff is built from.

    One instance per booking. ``start_utc`` and ``end_utc`` are the booking's own span —
    deliberately **not** the buffered span: buffers widen conflict detection and are none
    of the requester's business (``docs/domain-model.md``). ``timezone`` is the
    requester's IANA zone, carried for display only; the emitted file itself is pure UTC.
    """

    booking_id: str
    instance_host: str
    sequence: int
    title: str
    description: str
    location: str | None
    start_utc: datetime
    end_utc: datetime
    timezone: str
    organizer_name: str | None = None
    organizer_email: str | None = None


def event_uid(booking_id: str, instance_host: str) -> str:
    """The ``UID`` property: stable for the life of the booking.

    ``<booking-id>@<instance-host>``. The booking ID is never reused, so this identifies
    one event across the entire lifetime of the instance — which is what lets a calendar
    client update an existing entry instead of adding a duplicate when the file is
    downloaded again (ADR 0004).

    ``instance_host`` is long-lived state: it forms the domain of every ``UID`` calon
    issues, and changing it later changes every future ``UID`` (``CLAUDE.md`` §10).
    """
    return f"{booking_id}@{instance_host}"


def ics_filename(booking_id: str) -> str:
    """The ``Content-Disposition`` filename: ``calon-<booking-id>.ics``."""
    return f"calon-{booking_id}.ics"


def build_ics(event: CalendarEvent, *, now: datetime) -> bytes:
    """Render ``event`` as a ``VCALENDAR``/``VEVENT`` byte string.

    ``now`` is the moment the file is produced, injected rather than read: the domain
    rule (``CLAUDE.md`` §4.1) is that the wall clock is always a parameter. It becomes
    ``DTSTAMP`` — the one field whose value is allowed to differ on every download while
    the ``UID`` stays fixed.
    """
    from calon.calendarkit._ics import render

    return render(event, now=now)


def build_deeplinks(event: CalendarEvent) -> dict[str, str]:
    """One-click add-to-calendar URLs, keyed by provider.

    Always three entries: ``google`` (calendar.google.com), ``outlook_office``
    (outlook.office.com — work and school accounts), and ``outlook_live``
    (outlook.live.com — personal accounts). ``location`` is omitted from a link's query
    string when the event has none. The query strings are exact and covered by
    golden-file tests, so a silent re-encoding fails the build rather than quietly
    producing broken buttons.
    """
    from calon.calendarkit._deeplinks import render_all

    return render_all(event)
