# 10. The operator has one shared login; personal-data endpoints are behind it

- **Status:** Accepted
- **Date:** 2026-08-17

## Context

ADR 0008 chose an explicit *no admin UI*: "no admin UI means no login, no sessions, no
CSRF, and no password storage." That was right for a system with no human-facing
surface — the only interface was the API, and the API was public by design.

Phase 3 breaks that. The calendar handoff now carries **personal data**: an accepted
booking carries the requester's name and subject in the response, and a new endpoint
serves the RFC 5545 file. That file is not free/busy — it is the requester's name, a
"Consultation with …" summary, a start time, and a location. Two consequences follow:

1. **It cannot be public.** The same reasoning that makes the availability endpoint safe
   to publish ("it discloses nothing a requester has not already offered") does not
   apply here. A file that contains a person's name and meeting details must not be
   reachable by whoever guesses the URL.
2. **The operator needs a place to look.** The operator must be able to see *who* booked
   *what* and re-send the calendar file. With no admin UI, the only way to inspect a
   booking was to read `calon.db` directly with `sqlite3`. That works, but it is not a
   workflow, and it is not a way for the operator to hand a requester a fresh `.ics`.

Both of these push in the same direction: *calon now has an operator-facing surface, and
it needs a door.*

## Why one shared login, not per-user accounts

calon models **one operator per instance**. There is no "the requester logs in to see
their own booking" flow — a requester books, and the requester's calendar file is theirs
from that moment on. The only human who needs to log in is the operator, who:

- lists every booking (accepted or rejected) to see what the week looks like;
- downloads the `.ics` for a booking when a file needs re-sending;
- runs the instance on a single small server.

Per-user accounts would mean a `user` table, per-user token storage, a "forgot password"
flow, and a way for a requester to reach their own data — all of it for a product that, by
the scope rules in the README, is **not a CRM** and is **not multi-tenant**. One shared
login is the smallest change that satisfies both requirements above, and it matches the
single-server, single-operator shape the rest of the design assumes.

## Decision

**The operator surface — the web panel (`/login`, `/bookings`, `/logout`) and every
endpoint that returns personal data (the `.ics` endpoint and the deeplinks) — is gated
behind a single shared operator login, `CALON_LOGIN`.**

Public by design, and never behind the login:

- `POST /api/v1/bookings` (the intake — a requester books; nobody should need to log in
  to book);
- `GET /api/v1/availability` (free/busy — already documented as safe to publish);
- `GET /healthz`.

Everything else a human would want to do is behind the login. The login is not a
password in the usual sense: it is a **secret key** the operator sets once in `.env` and
keeps (like the API key of any self-hosted service), and the session it opens is a
short-lived, memory-only cookie.

### The session model

When the operator types `CALON_LOGIN` at `/login`, the server:

1. compares what was typed to the stored secret using `hmac.compare_digest` (constant
   time);
2. if it matches, mints a random 256-bit **token** and keeps it in an in-process
   dictionary with an expiry;
3. sets a `calon_session` cookie carrying that token.

The cookie is `HttpOnly` (no script reads it), `SameSite=Lax` (blocks cross-site CSRF on
state-changing requests), and `Secure` when `CALON_BASE_URL` is `https://`. The token is
not the secret, not a JWT, not signed with anything the browser can re-derive — it is
looked up against the in-process table. A restart drops the whole table, so sessions end
with the process.

### Fail closed

If `CALON_LOGIN` is not set, the operator surface returns `503` rather than opening. The
instance is *unsecured* only in the sense that the operator can log in; the personal-data
endpoints simply refuse to run. A public booking API still works, so nobody who forgets
to set a key is locked out of taking bookings — but they cannot, by accident, leave the
operator panel or the `.ics` endpoint wide open.

The same gate also accepts an optional `Authorization: Bearer <CALON_API_KEY>` header, so
the operator can script the panel from cron or wire up an external system without a
browser. The cookie path and the Bearer path are the **same** authorisation; either one
is enough.

## Consequences

- **ADR 0008 stands, scoped.** It said "no admin UI in the MVP" because nothing else
  wrote the rules; the rules still come from the file and restart is the only way to
  apply them. What changed is that there is now a *read-side* for bookings, and it has a
  door. The ADR's stronger claim — "no login, no sessions, no password storage" — no
  longer applies to the operator surface, and this ADR records that boundary.
- **No password storage in the database.** The operator's login is a secret the operator
  keeps (in `.env` or a password manager); calon stores it hashed in the session table
  only while a session is open, and never writes it to disk. There is no `user` table,
  no `password_hash` column, and no recovery flow — consistent with the "not a CRM /
  not multi-tenant" scope.
- **The `.ics` endpoint moved from public to authenticated.** Any deployment that relied
  on the ICS being a public URL must now send the operator's cookie (or Bearer key). For
  the one-restaurant case that is the operator, on their own machine, behind their own
  reverse proxy — not a problem in practice.
- **TLS is now strongly recommended, not optional.** The login and the session cookie
  travel on the same connection. Caldon does not terminate TLS itself; the reverse proxy
  does (see the self-hosting doc). With TLS, the session cookie gains the `Secure`
  attribute and stops being sent in cleartext.
- **One instance, one operator.** If a second person ever needs the operator panel, the
  answer is a second instance or a second key, not a second user row. That is on purpose,
  and it is the same reason calon is not multi-tenant.
