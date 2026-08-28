# 11. The public booking form is a thin wrapper over `submit_intent`, not a second path

- **Status:** Accepted — the no-JavaScript constraint is revised by
  [ADR 0018](0018-booking-widget-day-and-time-picker.md), which adds the day-and-time
  picker as progressive enhancement; everything else here still holds
- **Date:** 2026-08-18

## Context

Through phase 3, the only way to submit a booking was `curl` against
`POST /api/v1/bookings`. The operator panel was login-gated and showed what had been
booked — but a person who wants to book something has to know the API shape,
assemble a JSON payload with an aware `datetime`, and send it. That is fine for a
developer; it is not fine for the actual user.

The form must do two things at the same time:

1. **Be usable without any client-side code.** No JavaScript framework, no SPA, no
   client-side date picker. calon serves one HTML file and a POST back to itself; the
   operator's only job is to keep the form working, not to bundle a frontend.
2. **Not create a second booking code path.** The rules engine, the conflict check, the
   audit log, and the calendar handoff all live in `services/booking_service.py`. The
   form must call `submit_intent` exactly as the API does — with
   `source="native"` — so that a booking made through the form and one made through
   the API are indistinguishable in the audit log and in the domain layer.

A naive implementation would re-implement parts of the validation (email format,
date parsing, etc.) in the web layer. That would create two sources of truth and two
failure modes. The form layer's job is only to adapt HTML form data into a
`BookingIntentIn` and to render whatever the domain decided.

## Decision

**`GET /book`** renders a server-rendered Jinja2 template with no JavaScript. The form
fields are: name, email, phone (optional), date (`<input type="date">`), time
(`<input type="time" step="900">`), subject, and notes (optional). The template context
carries the resource's timezone, the booking window, and the default duration so the
user can see what times are valid before they click submit.

**`POST /book`** does the following, in order:

1. Strips and collects the form fields from the POST body.
2. Runs form-level validation (required-field presence, date/time parseability) and
   renders `422` with the form re-displayed and an error banner listing every missing
   field — **before** constructing a `BookingIntentIn`.
3. Constructs an aware `datetime` in the resource's timezone (the form's date and time
   are both in the resource's local time; the template says so).
4. Constructs a `BookingIntentIn` and calls `submit_intent(source="native")` through a
   `database.write()` transaction — the exact same call the API route makes.
5. On **acceptance** (`submission.accepted`): renders a success page showing the booked
   slot in the requester's timezone, the `.ics` URL, the Google Calendar and Outlook
   deeplinks, and the booking reference ID. The form is not re-shown.
6. On **rejection** (`submission.accepted` is `False`): re-renders the form with all
   user-entered values preserved in the fields and a red banner containing the domain
   layer's `reason`, each `violation.message`, and up to three "next available"
   suggestion lines in the requester's timezone.

No CSRF token is added. calon is a self-hosted, single-operator service without a
public login; the operator's reverse proxy is the appropriate layer for CSRF and
rate-limiting concerns. Adding a token would require a session on the public (no-login)
form, which contradicts the design.

## Consequences

- The form and the API share one code path. Auditing a booking does not require
  knowing which route created it — `source="native"` covers both.
- The form is the only human-facing booking path. The operator panel
  (`/bookings`) remains the only place the operator sees the bookings list. The API
  remains the machine-facing path.
- Rejections show the same domain-layer messages a `curl` caller would see in the
  JSON body — no human-readable copy exists in the template, so there is nothing to
  keep in sync.
- The form is accessible without a login. This is by design: it is the booking form,
  not the operator panel. The only login-gated routes are `/login`, `/bookings`,
  `/logout`, and `/api/v1/bookings/{id}/calendar.ics`.
- No new runtime dependency is added. Jinja2 is already a dependency (the operator
  panel uses it); the form reuses the same `Jinja2Templates` instance.
