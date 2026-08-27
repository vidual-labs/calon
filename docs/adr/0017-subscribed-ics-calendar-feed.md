# 17. A subscribed ICS feed as a second, OAuth-free way to read a resource's busy time

- **Status:** Accepted
- **Date:** 2026-08-27
- **Extends:** [ADR 0009](0009-optional-resource-calendar-sync.md) (the provider contract),
  [ADR 0014](0014-operator-initiated-google-connect-flow.md) and
  [ADR 0016](0016-dashboard-entered-oauth-client-credentials.md) (the OAuth path). All
  three stay Accepted; this adds a third way to fill the same `CalendarProvider` seam.

## Context

ADR 0016 removed the config-file edit from connecting a calendar, but not the step before
it: somebody still has to register an OAuth client in Google Cloud Console. For a large
share of operators that step is not merely tedious, it is unavailable — a managed Microsoft
365 or Google Workspace tenant commonly forbids app registration to non-admins, and a
non-technical operator running calon for their own practice has no reason to ever open a
developer console.

The obvious wish is a "sync with Google/Microsoft" button that needs no console at all.
That does not exist and cannot: OAuth requires *someone* to register the app, so a
console-free button means calon itself would have to be a registered application, which
in turn needs a client secret that cannot be shipped in a public repository, a redirect
URI per self-hosted domain (i.e. a central broker service the project operates), and
provider verification with the project as the publisher of everyone's calendar access.
Every part of that contradicts `CLAUDE.md` §2. It was raised with the maintainer as
impossible, and this ADR is the alternative they asked for instead.

There *is* a console-free path, and both providers already support it: a user can publish
their calendar as a secret ICS URL from the calendar's own settings. That URL is read-only
and it is its own credential.

## Decision

### `provider = "ics"` is a third calendar provider

A published ICS URL becomes an ordinary `CalendarProvider` (`calon.calendars.ics_feed`).
It fills the same seam as Google and Microsoft, so the rule chain, the `PROVIDER_CONFLICT`
code, the degrade-on-failure behaviour, and the audit log are all unchanged — a feed is not
a new subsystem, which is what keeps this inside `CLAUDE.md` §3's "not a general-purpose
calendar sync engine".

It is configurable the same three ways the others are: a `[calendars.<slug>]` block with
`provider = "ics"` and `feed_url`, or a URL entered in the dashboard (stored in
`calendar_feed`), with the TOML winning where present, exactly as ADR 0016 decided.

### Read-only, and the contract says so

The provider declares `writable = False`, and `CalendarProviderRegistry.writes_back()` is
the single place callers ask. The write-back skips such a resource entirely rather than
attempting a write and auditing a failure per booking — nothing failed, there is simply
nowhere to write. Accepted bookings still reach the operator's calendar the way they
always have: the `.ics` handoff and the Google/Outlook deeplinks (ADR 0004).

### One calendar per resource

A resource can have a feed **or** an OAuth client, not both. The dashboard refuses the
second and tells the operator to remove the first. Two sources would mean two answers to
"which calendar is this resource's", with no principled way to merge a read-only one and a
writable one.

### Recurrence is expanded properly, in the event's own timezone

`calon.calendarkit._ics_busy` expands `RRULE`/`RDATE`/`EXDATE` and applies `RECURRENCE-ID`
overrides (including a cancelled override, which frees that one instance). Expansion
happens in the event's own zone and is converted to UTC per occurrence, so a weekly 09:00
meeting stays at 09:00 across a DST change rather than drifting an hour — the alternative
considered was "handle simple rules and ignore the rest", which silently under-reports busy
time, and under-reporting busy time is how a booking lands on top of a real commitment.

This costs one runtime dependency, `python-dateutil`, for `rrule`. It is already an
`icalendar` dependency, so nothing new is installed; it is declared explicitly because we
import it directly (`CLAUDE.md` §8).

### Bounded work, bounded trust

A feed is fetched at most once per `DEFAULT_CACHE_TTL_SECONDS` (5 minutes) per process, is
capped at 5 MB, has a 10-second timeout, and `FREQ=SECONDLY`/`MINUTELY` rules are ignored
rather than expanded. One unreadable event is skipped; only an unreadable *document*
degrades the whole resource.

The operator-supplied URL is fetched server-side and calon deliberately does **not**
blocklist private address ranges: a self-hoster subscribing to a Nextcloud or Radicale feed
on their own LAN is a first-class use of this, and the operator already chooses what the
process reads (they can point `[calendars]` at anything today). The URL is a secret — it is
what authorizes the read — so it is never rendered back into the page, logged, or included
in an error message.

## Consequences

- An operator with no developer console access, and no wish to get one, can have their real
  commitments respected: publish, paste, done. That is the majority of what "sync my
  calendar" means to someone running a one-person practice.
- It is **not** two-way. The panel says so on the row (`free/busy only`) and in the setup
  text, because an operator who believes bookings are being written into their calendar and
  finds out otherwise has been actively misled.
- It is **not** immediate. Providers publish these feeds on their own cache schedule, which
  can lag by hours; calon's own 5-minute cache sits on top. A booking accepted seconds ago
  in another system will not be visible for a while. Stated in the UI and in
  `docs/self-hosting.md`.
- `calon.db` gains a third calendar-related table, and with it a third place to look when
  answering "where is this resource's calendar configured". The dashboard row names the
  source, which is what keeps that answerable.
- No background worker and no polling loop: the feed is fetched lazily, inside the request
  that needs it, exactly like the other providers' API calls (`CLAUDE.md` §4.7 stands).
