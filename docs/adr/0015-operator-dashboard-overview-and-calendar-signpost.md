# 15. The operator dashboard shows a functions overview, and a calendar signpost when nothing is configured

- **Status:** Accepted
- **Date:** 2026-08-27
- **Amends (narrowly):** the UI gating decided in
  [ADR 0014](0014-operator-initiated-google-connect-flow.md) — "no calendars configured,
  no calendars panel". Everything else in ADR 0014 (the routes, the state signing, the
  credential store, Google-only scope) is unchanged and still Accepted.

## Context

Two reports from the operator running the first real deployment:

1. After logging in, the dashboard showed a list of bookings and nothing else. There was
   no way to see, from inside the operator area, what the instance actually does or which
   rules it is enforcing — that lived only in `config/calon.toml` on the server and in
   `docs/`. An operator who wants to check "is Saturday closed?" or "which intake sources
   are live?" had to open a shell.
2. The "Connect with Google" button from ADR 0014 was invisible on that deployment,
   because the panel holding it was rendered only when a `[calendars.<slug>]` block
   already existed. That gating was meant to protect the standalone-first boundary
   (`CLAUDE.md` §2): an instance with no calendar integration should not be nagged about
   one. In practice it also made the feature undiscoverable — the operator concluded the
   integration did not exist, since the only way to find it was to already have configured
   it.

## Decision

### The dashboard leads with an overview panel

`GET /bookings` renders an **Overview** panel above the bookings list: the functions this
instance exposes (booking form, booking and availability APIs, calendar handoff, external
intake, calendar sync, API-key access, API docs) each with their live status, and the
scheduling rules currently in force (resource, timezone, open days, window, duration, slot
grid, notice, horizon, buffers, daily cap, blackout count, event title), plus any
configured intake sources with their endpoints.

It is a **view of the config as calon actually parsed it**, not a second place to edit it.
`config/calon.toml` remains authoritative (ADR 0008) and a change still requires a
restart; the panel says so.

### The calendars panel is always rendered; an unconfigured resource gets a signpost

Every resource gets a row. A resource with a `[calendars.<slug>]` entry behaves exactly as
ADR 0014 specified (Connect / Disconnect / "set up via config/calon.toml" for Microsoft). A
resource **without** one gets a `Not configured` row and a collapsed explanation of how to
set it up: the exact OAuth redirect URI to register (computed from this instance's own
`base_url`), and the TOML block to paste, keyed to that resource's real slug.

The signpost carries **no action**: no connect link, no form, no way to write config from
the browser.

## Consequences

- The standalone-first boundary is intact. Nothing here builds a provider, requires a
  provider, or changes the booking path: an instance with no `[calendars]` block still
  checks conflicts against calon's own bookings only and still hands off `.ics` plus
  deeplinks. What changes is that the operator can *see* that this is the state they are
  in, and what the alternative would cost them.
- The invariant "no calendars configured ⇒ the string `Calendars` never appears on the
  dashboard" is deliberately given up. Its test is replaced by one asserting the weaker,
  still meaningful property: no connect action is offered for an unconfigured resource.
- The overview panel is one more thing to update when a function or a rule is added.
  That is accepted: an operator-facing surface that silently omits half the rules is worse
  than one that has to be kept current, and the panel reads the same parsed config the
  evaluator does, so a rule with a value has a value to render.
- Still no admin UI. Writing config from the browser would mean secret storage, a write
  path around the TOML file, and a boot-vs-runtime split in where rules come from — all of
  which ADR 0008 exists to avoid. If that is ever wanted, it needs its own ADR.
