# Calendar handoff

> Status: implemented (phase 3). Accepts now carry the `CalendarHandoff` schema; the
> `.ics` endpoint is live. The endpoint is **login-gated** — it carries a requester's
> name and subject, so it is behind the operator login (see
> [self-hosting § Security](self-hosting.md#security)), not a public route.

## The principle

**Zero credentials, universal reach.** calon never authenticates against a calendar
provider. When a booking is accepted, calon hands the requester something they can add to
whatever calendar they already use.

This is a two-layer design:

1. **ICS / iCalendar is the baseline.** A downloadable `.ics` file works with Google
   Calendar, Outlook, Microsoft 365, Apple Calendar, Thunderbird, Fastmail, and everything
   else that has ever read RFC 5545. No OAuth application, no consent screen, no tokens,
   no vendor review.
2. **Provider deeplinks are a convenience layer.** One-click "add to Google Calendar" and
   "add to Outlook" buttons, offered *alongside* the ICS file and never instead of it.

## The `CalendarHandoff` schema

Returned in the accept response and rendered on the result page.

```
event:
  uid: str                # "<booking_id>@<CALON_INSTANCE_HOST>"
  sequence: int           # starts at 0, increments on amendment
  title: str
  description: str
  location: str | None
  start_utc: datetime
  end_utc: datetime
  timezone: str           # for display
  organizer: {name, email} | None
ics_url: str              # "/api/v1/bookings/{id}/calendar.ics"
ics_filename: str         # "calon-<booking_id>.ics"
links:
  google: str
  outlook_office: str     # Microsoft 365 / work accounts
  outlook_live: str       # Outlook.com / personal accounts
```

## ICS output

A `VCALENDAR` containing one `VEVENT`:

```
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//vidual-labs//calon 0.1.0//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:0192f3c1-...@calon.example.com
DTSTAMP:20260901T120000Z
DTSTART:20260903T090000Z
DTEND:20260903T093000Z
SUMMARY:Consultation with Alex Rivera
DESCRIPTION:Booked via calon.
STATUS:CONFIRMED
SEQUENCE:0
END:VEVENT
END:VCALENDAR
```

Served as `Content-Type: text/calendar; charset=utf-8` with
`Content-Disposition: attachment`.

### Three decisions worth explaining

**UTC instants with the `Z` suffix.** Emitting `DTSTART:20260903T090000Z` means no
`VTIMEZONE` block is needed and there is nothing to get wrong across a DST transition. The
requester's calendar renders it in their local time, correctly, without calon shipping a
copy of the timezone database inside every file it generates.

**`METHOD:PUBLISH`, not `METHOD:REQUEST`.** `REQUEST` signals an iTIP meeting invitation
and makes clients expect an RSVP and an email workflow that calon does not implement —
which produces confusing "respond to this invitation" prompts that go nowhere. `PUBLISH` is
the correct semantic for "here is an event; add it to your own calendar."

**A stable `UID` plus an incrementing `SEQUENCE`.** The `UID` is derived from the booking
ID and stays fixed for the life of that booking. If the requester downloads the file twice,
or the booking is amended and they download it again, their calendar **updates the existing
entry** instead of creating a duplicate. This is the difference between an exporter that
demos well and one people can actually rely on. It is also why `CALON_INSTANCE_HOST` must
stay stable for the life of an instance: changing it changes every future `UID`.

Line folding at 75 octets and the escaping of commas, semicolons, and newlines are handled
by the `icalendar` package rather than by hand — those rules are exactly the kind of thing
that appears to work until someone puts a comma in their name.

## Deeplinks

Built with `urllib.parse.urlencode`; no dependency.

**Google Calendar**

```
https://calendar.google.com/calendar/render
  ?action=TEMPLATE
  &text=<title>
  &dates=YYYYMMDDTHHMMSSZ/YYYYMMDDTHHMMSSZ
  &details=<description>
  &location=<location>
```

**Microsoft 365 / Outlook (work)**

```
https://outlook.office.com/calendar/0/deeplink/compose
  ?path=/calendar/action/compose
  &rru=addevent
  &subject=<title>
  &startdt=<ISO 8601 UTC>
  &enddt=<ISO 8601 UTC>
  &body=<description>
  &location=<location>
```

**Outlook.com (personal)** — identical path and parameters on `https://outlook.live.com`.

Deeplinks are lossy by nature: they carry no `UID`, so they cannot deduplicate or update,
and the parameters are undocumented conveniences the providers can change without notice.
That is precisely why they are the second layer. The ICS file is the contract; the buttons
are a shortcut.

Unit tests assert the exact query string produced for a fixed event, so a silent change in
encoding behavior fails the build rather than quietly producing broken buttons.

## Deferred: direct provider writes

Writing events straight into a requester's Google or Microsoft calendar would need OAuth
applications on both platforms, consent screens, per-user token storage and refresh, and
vendor review — more work than the entire rest of the MVP, and the component most likely to
break unattended when a token expires or an API version is retired.

It is deferred to a post-1.0, opt-in feature behind a `CalendarWriter` interface. It will
never be required for the core booking flow. See
[ADR 0004](adr/0004-ics-first-calendar-handoff.md).
