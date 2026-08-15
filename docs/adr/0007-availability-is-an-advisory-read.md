# 7. Publish availability as an advisory read, and never as a hold

- **Status:** Accepted
- **Date:** 2026-08-15

## Context

Until now the only way to learn anything about availability was to submit a booking request
and read the answer. A rejection carries the failing rules and up to three next-available
slots, which is a good correction — "that one is taken, here is the next" — but it is not a
menu. Nothing in the design lets a caller ask what is free before committing to a time.

That is thin in three places:

- **The native booking form (phase 4).** A form that asks someone to pick a time without
  showing what is free is a bad form. This is the first and heaviest consumer, and it is
  entirely internal to calon.
- **External sources (phase 5+).** A lead arrives as "Tuesday afternoon" and the adapter has
  to invent a concrete instant. Every wrong guess costs a round trip and writes an immutable
  rejected `booking_intent`.
- **Retries.** Idempotency is enforced on `(source, idempotency_key)` and a replay returns
  the stored response without re-evaluating. A caller correcting its time must therefore mint
  a fresh key — a subtle requirement to place on a webhook sender nobody controls.

The question was whether to add an availability read, and if so, whether it belongs to the
API phase or the UI phase.

## Decision

**Add a read-only availability query in phase 2, alongside the native intake API.**

It needs persistence and the rule chain and nothing else, so phase 2 is where it fits
naturally. Phase 4's booking form then becomes presentation over an endpoint that already
exists, rather than an API addition wearing a UI.

It is framed as a **native** capability that external sources inherit, not as a feature
built for any provider. Building it because calon's own form needs it keeps ADR 0005's
boundary honest; building it because a vendor asked is how a generic API starts being shaped
around one caller.

**It is served by the same domain code path as suggestions.** `slots.suggest_slots()`
already walks the granularity grid across allowed weekdays and re-checks the complete rule
chain per candidate. Answering "every free slot between A and B" is that same search with an
explicit range instead of an origin-and-horizon, so both callers stay on one implementation
for the same reason native intake is itself an adapter: two implementations of "what is
free" would eventually disagree.

**Availability is advisory. It is never a hold, a lock, or a reservation.**

Anything returned is stale the moment it is computed. The authoritative answer remains the
`BEGIN IMMEDIATE` transaction with the conflict re-check immediately before insert, exactly
as it is today. The endpoint must say so plainly, and the response must not carry anything
that reads like a claim on a slot — no token, no expiry, no identifier.

Real holds would mean a reservation state machine, expiry, and a sweeper to reclaim
abandoned ones — which is a background worker, gated by ADR 0003 and `CLAUDE.md` §10. If
holds are ever wanted, they get their own ADR and their own justification.

**It discloses free/busy times only.** Never a requester, a subject, or any booking content.
In practice this publishes nothing new: `/book` is a public form, so the free/busy shape is
already inferable by anyone willing to probe it.

## Consequences

- The `0.1.0` HTTP contract gains one endpoint. This is additive and does not move
  `/api/v1`.
- Callers — the native form and external sources alike — can choose a time instead of
  guessing, which cuts round trips and keeps the audit log free of noise from blind attempts.
- **The advisory nature has to be stated at the endpoint, in the docs, and in the response
  shape,** because the obvious misreading is "I queried, therefore it is mine." A caller that
  treats a query as a reservation will still lose the race, correctly, at submit time.
- A busy availability query is a rule-chain evaluation per candidate slot. At the target
  scale — bookings per day, not per second — that is affordable, and the candidate generator
  only walks in-window times on allowed weekdays. It is worth watching if a range ever grows
  to a whole horizon.
- Free/busy becomes explicitly, deliberately public rather than incidentally inferable.
  An operator who does not want that must restrict it at the proxy, as `docs/self-hosting.md`
  already suggests for `/api/v1/…`.
