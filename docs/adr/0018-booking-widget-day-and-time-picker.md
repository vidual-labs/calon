# 18. The booking form gets a day-and-time picker, as progressive enhancement

- **Status:** Accepted
- **Date:** 2026-08-28
- **Extends:** [ADR 0011](0011-public-booking-form.md) (revises its "no JavaScript" constraint)

## Context

[ADR 0011](0011-public-booking-form.md) built the public form as one server-rendered page
with no client-side code: name, email, phone, a `<input type="date">`, a
`<input type="time">`, subject, notes. That kept the form honest — one code path, nothing
to build, nothing to bundle — but it puts the whole scheduling policy on the requester.
They type a date and a time blind, and the rules answer afterwards. A Sunday, a slot
outside the window, a slot someone else already has, a time inside the notice period: all
of them look identical while typing and all of them are found out only after submitting.

Meanwhile `GET /api/v1/availability` (ADR 0007) already answers exactly the question the
requester is guessing at — which slots would be accepted right now — and answers it with
the same rule chain the booking itself runs. Nothing needs to be computed twice; the
information simply is not on the page where the decision is made.

The obvious shape for showing it is the one every booking product has converged on: a
month grid with the bookable days lit up, a list of times for the selected day, then the
details form. Implementing that means client-side code on `/book`, which ADR 0011 ruled
out. What ADR 0011 was actually protecting is worth separating from how it phrased it: no
build step, no framework, no second deployment artifact, and no second booking code path.
A picker can be added without giving up any of those.

## Decision

`/book` renders a three-pane booking widget — what is being booked, a month grid, the
times for the selected day — and moves to the details form once a slot is picked. The
picker is **progressive enhancement over the ADR 0011 form, not a replacement for it**:

1. **The POST contract is unchanged.** The picker writes into the same `date` and `time`
   fields the form has always had; `POST /book` is byte-for-byte the same request, and
   `submit_intent(source="native")` is still the only downstream path (ADR 0011 stands).
2. **The served HTML is still the whole form.** The date and time inputs are in the
   markup, visible and `required`. Scripting off, or blocked, and `/book` is the ADR 0011
   form exactly as before. The picker turns those two inputs into hidden fields it drives
   only once it has taken over.
3. **A failed availability read degrades to the same place.** If
   `GET /api/v1/availability` errors or is unreachable, the widget puts the date and time
   inputs back and says so. It never blocks a booking on being able to read availability.
4. **No build step, no framework, no dependency.** One `<script>` block inside
   `book.html`, vanilla DOM, `fetch`, and `Intl.DateTimeFormat` for the one timezone
   conversion it needs. Nothing to compile and nothing new to ship.
5. **Availability stays advisory (ADR 0007).** A lit-up slot is not held. Two people can
   click the same one; the second is rejected inside the write transaction, and the
   rejection renders on the details step with the reasons and alternatives ADR 0011
   already specified.
6. **The resource timezone is the only timezone.** Slots are requested and displayed in
   the resource's zone, which the widget names on screen, and the submitted `date` and
   `time` are in that zone — as the server has always read them. A requester-side timezone
   selector would mean converting on the way back in; it is not part of this decision.

Today's date, the resource slug, and the slot duration are rendered into `data-*`
attributes on the widget element rather than inferred in the browser, so the grid opens on
the resource's today whatever timezone the requester's machine is set to.

## Consequences

- A requester sees what is bookable before choosing, and the common rejections
  (closed day, outside the window, inside the notice period, already taken) stop being a
  round trip. The rejection path is still there and still correct, because the rules did
  not move.
- `GET /api/v1/availability` is now load-bearing for the public page. It was already
  public and unauthenticated; the widget makes one request per month viewed.
- `/book` is no longer literally "no JavaScript", which
  [ADR 0011](0011-public-booking-form.md) said it was. The properties that mattered — one
  booking path, no build step, no framework, and a form that works without scripting —
  all still hold, and a self-hoster still deploys exactly one artifact.
- The template grew a script block. It is the only place client-side code lives; if a
  second page ever needs one, that is the moment to reconsider serving static assets.
