# 4. Start with ICS and deeplinks, not direct calendar-provider writes

- **Status:** Accepted
- **Date:** 2026-08-15

## Context

Once calon accepts a booking, the requester needs it in their calendar. The requirement is
that this works across major ecosystems — Google Calendar and Gmail, Microsoft 365 and
Outlook, and ideally everything else.

Two approaches were considered:

1. **Direct provider integration** — authenticate against Google Calendar and Microsoft
   Graph and write the event into the requester's calendar via API.
2. **Generic handoff** — produce a standard iCalendar file plus provider "add to calendar"
   deeplinks, and let the requester's own client do the work.

Direct integration is what a mature product eventually offers. The question was whether it
belongs in the first milestone.

## Decision

The MVP produces a **generic calendar handoff**: an RFC 5545 `.ics` file as the baseline,
with Google Calendar and Outlook deeplinks as a convenience layer offered alongside it.

calon does not authenticate against any calendar provider in `0.1.0`.

Three implementation choices follow, and each is load-bearing:

- **Instants are emitted in UTC with the `Z` suffix.** No `VTIMEZONE` block is required, and
  there is nothing to get wrong across a DST transition.
- **`METHOD:PUBLISH`, not `METHOD:REQUEST`.** `REQUEST` signals an iTIP invitation and makes
  clients expect an RSVP and email workflow that calon does not implement, producing
  "respond to this invitation" prompts that lead nowhere. `PUBLISH` correctly means "here is
  an event, add it to your calendar."
- **Stable `UID` plus incrementing `SEQUENCE`.** The `UID` is derived from the booking ID.
  A second download, or an amended booking, **updates** the existing calendar entry instead
  of creating a duplicate.

Direct provider writes were rejected for the first milestone because they require OAuth
applications on two platforms, consent screens, per-user token storage and refresh, and
vendor review processes — more work than the entire rest of the MVP, and the component most
likely to break unattended when a token expires or an API version is retired. ICS reaches
every calendar in existence with no credentials at all.

## Consequences

- calon works with any calendar on day one, including ones nobody thought about, with no
  setup by the operator and no accounts held by calon.
- No OAuth secrets, no refresh tokens, and no third-party account data stored anywhere. A
  substantial security and privacy surface simply does not exist.
- **The requester takes one manual step** — open the file or click a button. Accepted: it is
  one click, and it is the price of not holding credentials for anyone's calendar.
- **calon cannot see the requester's existing calendar.** Conflict detection covers calon's
  own bookings only. This must be stated plainly in the docs so operators are not surprised.
- Deeplinks are lossy — no `UID`, so no deduplication — and the query parameters are
  undocumented conveniences that providers may change. They are the second layer for exactly
  this reason, and their output is covered by exact-match unit tests so a silent change
  fails the build.
- `CALON_INSTANCE_HOST` becomes long-lived state: it forms the `UID` domain and cannot be
  changed without breaking in-place updates for already-issued events.
- Direct provider writes remain possible later, post-1.0, behind a `CalendarWriter`
  interface and always opt-in. They will never be required for the core booking flow.
