# 16. OAuth app credentials may be entered in the dashboard, stored in SQLite

- **Status:** Accepted
- **Date:** 2026-08-27
- **Extends:** [ADR 0014](0014-operator-initiated-google-connect-flow.md), which moved the
  *refresh token* out of `config/calon.toml` and into calon's own OAuth exchange. This ADR
  does the same for the OAuth *app* credentials that flow needs. ADR 0013's out-of-band
  path and ADR 0008's "the TOML is authoritative" both stay Accepted, with the boundary
  drawn below.

## Context

ADR 0014 left one prerequisite in the config file: `client_id` and `client_secret` had to
be in a `[calendars.<slug>]` block before the "Connect with Google" button existed at all.
On the first real deployment that turned out to be the whole barrier. The operator's
instance runs as a container behind a domain; adding a TOML block there means rebuilding or
remounting config and restarting the service, and until that happened the dashboard could
only tell them to go and edit a file they could not conveniently reach. The button they had
been given was, in practice, unreachable — twice reported as "OAuth is still missing".

Registering the OAuth app in Google Cloud Console is genuinely un-automatable for a
self-hosted instance: it is tied to that instance's own redirect URI. But *typing the
resulting two strings into calon* does not need a file edit and a restart.

`CLAUDE.md` §10 says to ask before adding a config path outside the TOML. The maintainer
asked for it explicitly, twice, after that boundary was raised.

## Decision

### A resource's OAuth client may come from the dashboard, stored in `calendar_oauth_client`

`POST /calendars/{slug}/oauth-client` (operator-gated, like every other dashboard route)
stores `client_id`, `client_secret`, and `calendar_id` in a new SQLite table, one row per
resource. The Calendars panel renders that form for any resource that has no calendar
configured; on save, the resource becomes connectable and the unchanged ADR 0014 consent
round trip follows. `POST /calendars/{slug}/oauth-client/forget` removes the row and, with
it, any connection built on it — a grant cannot outlive the app it was issued to.

### `config/calon.toml` wins wherever it has an entry

Resolution is: TOML entry if present, else the stored row, else no calendar. A file the
operator edited is never silently overridden by a database row. The form refuses outright
for a resource that has a `[calendars.<slug>]` entry, rather than storing something that
would never take effect. calon never writes to `config/calon.toml` — ADR 0008 holds; the
scheduling rules in particular remain file-only, and this table holds no rules.

### Credentials alone are not a connection

A stored client with no credential builds no provider, at boot or otherwise: the resource
stays standalone until the consent round trip completes. This keeps `CLAUDE.md` §2 exact —
nothing about entering credentials changes the booking path, and a provider that could
never refresh is worse than no provider.

### Plaintext, one trust boundary

No column-level encryption, for the reason ADR 0014 already gave for the refresh token
beside it: any key calon could use to decrypt would live on the same host, in the same
config, readable by the same operator account. The secret sits at the same trust level as
the `client_secret` an operator would otherwise have typed into `config/calon.toml` in
plaintext, and as the requester PII in `booking_intent`. `calon.db` is inside the boundary;
back it up accordingly. The client secret is never rendered back into the page.

### Google only, provider-keyed storage

The connect flow remains Google-only (ADR 0014's scope). The table carries a `provider`
column rather than assuming Google, so Microsoft 365 can join the dashboard flow later
without a schema change; until it does, Microsoft stays on the out-of-band/TOML path and
the form refuses any other provider.

## Consequences

- A fresh self-hosted instance can go from "no calendar" to "connected" entirely in the
  browser: register the OAuth client with the redirect URI the panel prints, paste two
  strings, click Connect. No file edit, no restart.
- calon now has a second place a calendar can be configured. That is a real cost — an
  operator debugging "why is it using that client id" has two places to look — and it is
  why the precedence rule is one line, printed on the row itself ("configured in
  config/calon.toml" versus a dashboard row with a Forget button).
- The form posts a secret through the browser to calon. It is gated by the operator login
  over the same session cookie as the rest of the dashboard, which is `SameSite=lax` and
  `Secure` whenever `CALON_BASE_URL` is https. Anyone who can reach the dashboard could
  already read bookings and disconnect calendars; this does not widen that boundary.
- No CSRF token, consistent with the existing `POST /logout` and
  `POST /calendars/{slug}/disconnect`. `SameSite=lax` is what blocks a cross-site POST
  today. If calon ever grows a form whose forgery matters more than these, it needs tokens
  — and that is one decision for all of them, not a special case here.
- Still no admin UI for scheduling rules. This table holds credentials, not rules, and the
  restart-to-apply story for `config/calon.toml` is unchanged.
