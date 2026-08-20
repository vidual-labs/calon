"""Render ``CalendarEvent`` to an RFC 5545 ``.ics`` byte string.

The shape of the output is fixed by ADR 0004 — the same file must import cleanly into
every major calendar — so the choices in here are load-bearing, not cosmetic:

* **UTC instants with the ``Z`` suffix.** ``DTSTART``/``DTEND``/``DTSTAMP`` are emitted
  as ``20260901T060000Z``: no ``VTIMEZONE`` block, nothing to get wrong across a DST
  transition, and no copy of a timezone database shipped inside every file. The requester's
  calendar renders the instants in their own local time.
* **``METHOD:PUBLISH``**, not ``METHOD:REQUEST``. ``REQUEST`` is an iTIP invitation that
  makes clients expect an RSVP and email workflow calon does not implement — that is where
  the puzzling "respond to this invitation" prompts come from. ``PUBLISH`` means "here is
  an event, add it to your calendar".
* **``UID`` stable, ``SEQUENCE`` incrementing.** The combination is what makes a second
  download (or a download after the booking is amended) update the existing entry in place
  instead of creating a duplicate.
* **``DTSTAMP`` set explicitly.** The ``icalendar`` package does not fill ``DTSTAMP`` in,
  and its absence is exactly the kind of thing that only shows up as "why did this import
  as a new event" in someone else's calendar.
* **Folding and escaping done by ``icalendar``.** Line folding at 75 octets and the
  escaping of commas, semicolons, and newlines are specified in RFC 5545 and easy to get
  subtly wrong by hand; they are the kind of thing that breaks the moment someone puts a
  comma in their name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from icalendar import Calendar, Event

from calon import __version__
from calon.calendarkit import CalendarEvent, event_uid


def _datetime(moment: datetime) -> datetime:
    """A UTC ``datetime`` with ``tzinfo=UTC`` — how ``icalendar`` serializes ``Z``.

    icalendar renders UTC-aware datetimes with the ``Z`` suffix by default; this makes the
    invariant explicit at the call site rather than depending on the input's exact
    ``tzinfo`` object.
    """
    return moment.astimezone(UTC)


def render(event: CalendarEvent, *, now: datetime) -> bytes:
    """One ``VCALENDAR`` containing one ``VEVENT``, ready to serve as ``.ics``.

    ``now`` is the moment the handoff is produced; it is what ``DTSTAMP`` takes, and the
    only field allowed to differ when the same event is rendered twice.
    """
    vcalendar = Calendar()  # type: ignore[no-untyped-call]
    vcalendar.add("version", "2.0")
    # The PRODID identifies this generator to clients that read the file, which is where
    # a calendar holding many feeds tells apart where an event came from.
    vcalendar.add("prodid", f"-//vidual-labs//calon {__version__}//EN")
    vcalendar.add("calscale", "GREGORIAN")
    # METHOD must be a top-level property, and icalendar 7.x has no convenience setter —
    # it must appear exactly once, so it is set once, here, at the top level.
    vcalendar.add("method", "PUBLISH")

    vevent = Event()  # type: ignore[no-untyped-call]
    # The stable identity of the booking (ADR 0004): booking IDs are never reused, so this
    # is fixed for the life of the booking. That is what lets a calendar client update the
    # existing entry instead of adding a duplicate on the next download.
    vevent.add("uid", event_uid(event.booking_id, event.instance_host))
    vevent.add("summary", event.title)
    vevent.add("dtstamp", _datetime(now))
    vevent.add("dtstart", _datetime(event.start_utc))
    vevent.add("dtend", _datetime(event.end_utc))
    vevent.add("description", event.description)
    if event.location is not None and event.location != "":
        vevent.add("location", event.location)
    if event.organizer_email is not None and event.organizer_email != "":
        # NAME is required wherever an ORGANIZER is present, so the name is mandatory
        # here even though the config treats it as optional.
        vevent.add(
            "organizer",
            f"{event.organizer_name or event.organizer_email}<{event.organizer_email}>",
        )
    # CONFIRMED rather than TENTATIVE: the requester holds a slot that passed the whole
    # rule chain, and calon has no RSVP surface to answer with.
    vevent.add("status", "CONFIRMED")
    vevent.add("sequence", event.sequence)

    vcalendar.add_component(vevent)
    # ``to_ical`` is untyped in icalendar (returns ``Any``); the bytes it actually emits.
    return cast("bytes", vcalendar.to_ical())
