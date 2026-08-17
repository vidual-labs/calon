# 9. Optional two-way sync with a resource's calendar

- **Status:** Accepted
- **Date:** 2026-08-17

## Context

ADR 0004 established the requester-facing calendar handoff: an `.ics` file plus provider
deeplinks, with no direct writes to any provider in `0.1.0`. That decision stands and is
unaffected by this one — it concerns what the *requester* receives after a booking is
accepted.

A separate need has since been confirmed by the maintainer: the *resource* being booked
(the person, room, or service) commonly already has a Google Calendar or Microsoft 365
calendar with its own busy time — meetings, personal appointments, anything not created
through calon. Two gaps follow from that:

1. **Availability blind spot.** calon's conflict detector only sees bookings already in its
   own SQLite database (ADR 0004, Consequences). Anything on the resource's external
   calendar is invisible, so calon can offer a slot that is actually already taken.
2. **Manual double-entry.** Today, an accepted booking exists in calon and nowhere else
   unless the operator copies it into their calendar by hand.

## Decision

calon will support an **optional, per-resource calendar sync** with a connected Google
Calendar or Microsoft 365 calendar, implemented behind a `CalendarProvider` interface:

- **Read:** when evaluating availability, additionally query the connected calendar's
  free/busy for the candidate window and treat provider-reported busy time as a conflict,
  alongside calon's own bookings.
- **Write:** once a booking is accepted, create (and on amendment, update) a corresponding
  event on the connected calendar, in addition to calon's own record and the existing ICS
  handoff to the requester.

This is strictly additive, per the prime directive in `CLAUDE.md` §2:

- A resource with no provider configured behaves exactly as it does today — conflict
  detection against calon's own bookings only, ICS/deeplink handoff, no external calls.
- Configuring a provider for a resource is opt-in, per resource, in `config/calon.toml`
  (mirroring how `[sources.*]` external intake is configured today).
- If the provider API is unreachable or errors, calon falls back to its own database as the
  sole source of truth for that request rather than failing the booking — an unreachable
  external calendar degrades availability accuracy, it does not take calon down.
- The native test suite continues to run, and pass, with no provider configured.

Scope is deliberately narrow: this is free/busy read plus write of calon-originated events
for one resource's own calendar, not general two-way sync of arbitrary events, multiple
calendars, or attendee management. See `CLAUDE.md` §3.

## Consequences

- New runtime dependencies are required for the Google Calendar API and Microsoft Graph API
  clients (or a minimal stdlib-based HTTP client against their REST APIs — to be decided at
  implementation time and justified in the PR per `CLAUDE.md` §8).
- calon must store OAuth credentials (refresh tokens) per resource. This reintroduces the
  credential-storage surface ADR 0004 avoided — scoped narrowly to the operator's own
  resource calendars, not any requester's calendar, and clearly documented as opt-in.
- Token refresh and expiry handling becomes an operational concern; a resource with an
  expired or revoked connection must degrade to calon-only availability rather than break
  booking.
- A new decision code is needed to distinguish "rejected due to a provider-reported
  conflict" from calon's existing own-booking conflict code, per `CLAUDE.md` §5.
- The ICS/deeplink handoff to the requester (ADR 0004) is unchanged and remains the
  baseline; provider sync is an additional layer on the resource side.
- Implementation is tracked as roadmap phase 8 (`README.md`), targeting `0.3.0`.
