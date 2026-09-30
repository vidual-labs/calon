# 20. The operator can cancel a booking

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

The `booking` table has carried `status IN ('confirmed', 'cancelled')` and a
`cancelled_at_utc` column since the first migration, and `booking.cancelled` has been a
documented audit event type, but nothing ever set them. A booking, once accepted, could only
be undone by editing `calon.db` by hand. When a lead called off an appointment, its slot stayed
blocked, and on a resource with a connected calendar (ADR 0009) the event stayed in the
operator's calendar.

Requester-facing cancel and reschedule links remain deferred by design (`CLAUDE.md` §3):
they need a way to identify the requester without accounts, and a decision of their own. What
is missing today is narrower — the operator, who already signs in to the dashboard, has no way
to act on a cancellation they learned of by phone or email.

## Decision

1. **Operator only.** A dashboard form posts to `POST /admin/bookings/{id}/cancel`, gated by
   the operator login like every other dashboard write. There is no public cancel endpoint,
   and no JSON API route; the dashboard route also accepts `CALON_API_KEY` for scripting.
2. **Mark, never delete.** The booking row is set to `cancelled` with `cancelled_at_utc`, and
   `booking.cancelled` is audited with actor `operator`. Conflict detection and availability
   already count only `confirmed` bookings, so this alone frees the slot, and the booking
   stays in the history. Cancelling twice is a no-op.
3. **The calendar follows, after the commit.** `CalendarProvider` gains a third call,
   `remove_event`, beside `free_busy` and `upsert_event`. It runs after the cancellation has
   committed, is audited `booking.calendar_removed` or `booking.calendar_remove_failed`, and a
   failure never undoes the cancellation — the same degrade-not-fail contract as the
   write-back (ADR 0009, `CLAUDE.md` §2). Removing an event that is already gone is success.
   A read-only feed (ADR 0017) has nothing to remove.
4. **The requester is not notified.** calon sends no email (`CLAUDE.md` §3); the operator
   tells the requester through whatever channel the cancellation arrived on. The dashboard's
   confirmation prompt says so.

## Consequences

- A cancelled slot is immediately bookable again, through every intake path.
- An external-intake retry of the original request still replays its stored decision
  (ADR 0012); it does not re-create a cancelled booking.
- The `.ics` file a requester already downloaded is not withdrawn. The dashboard stops
  offering the download for a cancelled booking.
- Each provider implements one more call. Google deletes by the same derived event id the
  upsert writes; Microsoft Graph finds the event by its `iCalUID` on its own day, as the
  upsert does, and deletes it.
- Rescheduling stays "cancel and book again" until requester-facing links are decided.
