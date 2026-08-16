# 6. Give the domain its own request value, and split the rule chain into gating and collected rules

- **Status:** Accepted
- **Date:** 2026-08-15

## Context

Phase 1 built the pure scheduling core. Three questions came up that the existing decision
records did not answer, and each of them shapes what every later phase has to do.

**What does the rule chain take as input?** The obvious answer is `BookingIntentIn`, the
canonical Pydantic contract every source produces. But that model carries the requester's
name, email, phone, subject, notes, and `metadata` — none of which can affect whether a
slot is bookable. Passing it into the domain would put a Pydantic import and a pile of
provider-adjacent fields inside the layer that is supposed to have neither.

**What happens when a request fails several rules at once?** The domain model says `code`
is the first failure and `violations` holds all of them. That is straightforward for the
policy rules, but not for the structural ones: a booking whose end precedes its start would
also be reported as ending outside business hours, and a request naming a resource that
does not exist would be judged against a policy that is not its own.

**Where do next-available suggestions come from?** Searching for alternatives means running
the rule chain repeatedly. If `evaluate()` did that itself, every rejection would silently
cost a search, including the ones nobody will act on.

## Decision

**1. The domain defines its own `BookingRequest`:** `resource_slug`, `start`, `timezone`,
and an optional `end`. Nothing else. The service layer translates `BookingIntentIn` into it
on the way in and persists the full intent separately.

This makes "`metadata` is never read by core logic" true by construction rather than by
discipline — the core cannot read a field it was never handed. It also keeps Pydantic out
of `domain/`, which is what lets the domain tests run with no fixtures at all.

**2. The first three decision codes are gating.** `INVALID_INPUT`, `RESOURCE_UNKNOWN`, and
`DURATION_NOT_ALLOWED` stop evaluation immediately, and the decision carries that single
violation. Codes 4 through 10 are all evaluated and all their failures reported.

The dividing line is whether the remaining rules would be reasoning about a coherent
request. A Sunday at 3am is a real request that fails two real rules, and the requester
should hear both. A booking that ends before it starts is not a request at all, and listing
four further complaints about it is noise dressed up as thoroughness.

Gating rejections also carry no suggestions, for the same reason: there is no next
available slot for a duration that is not a duration.

**3. Rule evaluation does not search.** `evaluate()` returns a decision with no
suggestions; `suggest_slots()` finds alternatives; `Decision.with_suggestions()` attaches
them; and `calon.domain.decide()` composes all three for callers who want the whole answer.

The slot search re-runs the complete chain on every candidate rather than a cheaper subset.
A suggestion that turns out to sit inside a blackout, or on top of another booking's
buffer, is worse than offering nothing at all. The candidate generator only walks in-window
times on allowed weekdays, which keeps that affordable.

## Consequences

- The domain layer has no framework dependency and no knowledge of who is booking, only of
  what is being booked and when.
- The service layer owns one translation — `BookingIntentIn` to `BookingRequest` — which is
  a small, obvious, and testable seam. It is also the natural place to persist the intent
  before a decision exists.
- **Cost:** a request is now represented twice, and adding a field that genuinely affects
  scheduling means touching both. That is deliberate friction: it forces the question of
  whether a new field is really a scheduling input or just more payload.
- Rejections are cheap by default. Callers opt into the cost of a search.
- The gating split is a behavioural contract, not an implementation detail. Moving a code
  across that line changes what clients receive and is a breaking change.
