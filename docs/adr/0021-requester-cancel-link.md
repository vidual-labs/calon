# 21. The requester can cancel through a signed link

- **Status:** Proposed
- **Date:** 2026-10-01
- **Supersedes (narrowly):** the sentence "There is no public cancel endpoint" in point 1 of
  [ADR 0020](0020-operator-cancellation.md). Everything else in ADR 0020 still holds, and
  this ADR reuses its cancellation path unchanged.

## Context

ADR 0020 let the operator cancel a booking from the dashboard. A requester who can no longer
come still has to reach the operator by phone or email, and the operator then cancels by
hand. Until then the slot stays blocked for everyone else.

`CLAUDE.md` §3 deferred requester-facing cancel and reschedule links until they had a
decision of their own. The open question in ADR 0020 was how to know that the requester is
the one asking, when calon has no accounts, no sessions for the public, and sends no email.
Accounts and requester logins are out of scope (`CLAUDE.md` §3, §10). So whoever holds the
link must be able to cancel, and nobody else.

This ADR covers cancelling only. Rescheduling has a question of its own: if the new slot is
rejected, does the old booking survive? It gets its own decision.

## Decision

1. **A capability link, signed rather than stored.** An accepted booking gets a cancel URL
   of the form `{base_url}/cancel/{booking_id}/{signature}`. The signature is
   HMAC-SHA256 over `cancel:v1:{booking_id}`, base64url without padding, and is checked
   with `hmac.compare_digest`. It uses only the standard library, needs no new table or
   column, and can be computed again at any time. A replayed external-intake response
   (ADR 0012) therefore carries the same link as the original response.
2. **The signing key comes from `CALON_SECRET_KEY`** (ADR 0019), through a derived subkey:
   `HMAC-SHA256(secret_key, "calon cancel-link v1")`. The derivation keeps the key that
   encrypts calendar secrets and the key that signs links separate, so the two uses cannot
   be confused with each other. While `CALON_SECRET_KEY_PREVIOUS` is set, a link signed
   with the previous key is still accepted. New links are always signed with the current
   key.
3. **No key means no link, and nothing else changes.** Without `CALON_SECRET_KEY`, calon
   issues no cancel links and serves no cancel page, and every other path works exactly as
   it does today. Booking, rules, conflicts, the handoff and the audit log never depend on
   the key (`CLAUDE.md` §2). A requester link is optional, like a connected calendar.
4. **Two steps: GET to confirm, POST to cancel.** `GET /cancel/{booking_id}/{signature}`
   renders a page that shows the resource and the booked time, and a *Cancel this booking*
   button. Only `POST` to the same path cancels. Mail scanners, chat link previews and
   browser prefetching all follow GET links. With a single GET step, they could cancel a
   booking nobody meant to cancel.
5. **The page shows as little as possible.** It shows the resource and the time, in the
   booking's timezone. It does not show the name, email, phone, subject or notes. A
   forwarded or leaked link lets someone cancel the booking. It does not give them the
   requester's personal data.
6. **A link works until the booking starts.** A link for a booking that has already started
   or ended, or that was already cancelled, opens a page that says so and has no button.
   An invalid signature and an unknown booking id get the same `404`, so the page never
   reveals whether a booking exists. The link itself never expires, because the booking's
   own state is its limit. A minimum cancellation notice (for example "not within 24
   hours") would be an operator rule. It is not part of this decision.
7. **The same cancellation as the operator's, with a different actor.**
   `booking_service.cancel_booking()` takes the actor as a parameter. The requester route
   passes `requester`, so the audit log records `booking.cancelled` with actor `requester`.
   Everything else is ADR 0020: the row is marked cancelled, never deleted, inside
   `Database.write()`. The slot is free again at once. A connected calendar's event is
   removed after the commit, and a failed removal never undoes the cancellation.
   Cancelling twice is a no-op.
8. **Where the link appears:**
   - on the `/book` confirmation page, under the calendar links, with a line telling the
     requester to keep it;
   - in the JSON response, as a new optional field `booking.cancel_url` (a string, or
     `null` when there is no key). It is in both the native API and the external-intake
     response, so a lead source that talks to the requester can pass the link on.

   It is **not** written into the `.ics` file, and it is not written into the event that
   calon writes into a connected calendar. That event lives in the operator's calendar,
   which may be shared, and anyone who can read the calendar could cancel. Leaving the ICS
   unchanged also keeps the handoff output and its golden files as they are.
9. **calon still notifies nobody.** The operator sees the cancellation in the dashboard and
   in the audit log, as *Cancelled* with actor `requester`. calon sends no email
   (`CLAUDE.md` §3).

### Alternatives considered

- **A random token stored per booking** (a new `cancel_token` column). This allows revoking
  one link by clearing its column, but it needs a migration, a stored secret per booking,
  and a lookup that has to be made timing-safe. A signed token needs none of that.
  Revoking a single link is not needed: once a booking is cancelled or has started, its
  link stops working anyway. Rotating `CALON_SECRET_KEY` revokes every link at once.
  Rejected.
- **A token with an expiry built into it.** It duplicates what the booking's start time
  already limits, and calon would have to decide on a lifetime. Rejected.
- **A separate `CALON_LINK_KEY` setting.** It would allow rotating link signing separately
  from calendar encryption, at the cost of one more secret for the operator to generate
  and keep. The derived subkey gives the same separation in use with no new setting.
  Rejected for now. If a separate key is ever needed, the subkey's `v1` label lets it be
  introduced cleanly.
- **Confirm by email (send a code to the requester's address).** Needs outbound email,
  which is deferred (`CLAUDE.md` §3) and would make an SMTP server a dependency.
  Rejected.
- **Put the link in the `.ics` description.** It would reach the requester's calendar, but
  through write-back (ADR 0009) it would also reach the operator's calendar, and with it
  everyone that calendar is shared with. Rejected, see point 8.

## Consequences

- **Schema change, additive:** `BookingOut` gains an optional `cancel_url`. Per
  `CLAUDE.md` §6 this is not breaking. The canonical schemas are the public contract,
  though, so the field needs the maintainer's approval (`CLAUDE.md` §10), and
  `docs/domain-model.md` and `docs/external-intake.md` describe it.
- **No migration, no new dependency, no new setting.** `CALON_SECRET_KEY` gains a second
  use, and `docs/self-hosting.md` and `.env.example` say so: without it there are no cancel
  links.
- **Rotating the key invalidates outstanding links** once `CALON_SECRET_KEY_PREVIOUS` is
  removed. A requester with an old link then has to contact the operator, as before this
  ADR. `docs/self-hosting.md` mentions this next to the rotation steps.
- **Whoever holds the link can cancel.** That is the trade for having no accounts, and the
  confirmation page and the docs say so. The signature is 256 bits, so links cannot be
  guessed. They can only leak.
- **A requester who loses the link** contacts the operator, who cancels from the
  dashboard (ADR 0020). calon does not send the link again.
- **The public surface grows by one route pair** with no login. It shows nothing personal
  and changes nothing on a GET, and its only effect on POST is the same cancellation the
  operator can already do.
- Rescheduling stays "cancel and book again" until its own ADR.
- `CLAUDE.md` §3 and the README roadmap should be updated to say that requester
  cancellation is decided here, and that rescheduling is still deferred.
