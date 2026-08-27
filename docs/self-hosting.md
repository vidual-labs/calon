# Self-hosting calon

> Status: real. calon runs as a single Docker container via `docker compose`, with an
> operator web panel (login-gated), the public booking API, and the calendar handoff.
> The configuration, database, security, and backup sections below apply today.

calon is designed to run on one small server with no external services. A single
container, a single SQLite file, and a reverse proxy in front of it.

## Requirements

- A host with Docker and Docker Compose
- A domain name and TLS (calon does not terminate TLS itself)
- Roughly 256 MB of RAM and a little disk — the database is a few megabytes for years of
  bookings

## Install

```bash
git clone https://github.com/vidual-labs/calon.git
cd calon

cp .env.example .env
cp config/calon.example.toml config/calon.toml

# Edit config/calon.toml: your timezone, weekdays, hours, notice, buffers, blackouts.
# Edit .env: at minimum CALON_BASE_URL, CALON_INSTANCE_HOST, and CALON_LOGIN.

docker compose up -d --build
```

To develop without Docker:

```bash
make install
make dev
```

Both `cp` steps are optional. With no `.env` and no `config/calon.toml`, calon starts on
the defaults `config/calon.example.toml` documents — it is fully usable with nothing
configured. The database file and its schema are created on first start.

The generated API reference is at `/docs` (disable with `CALON_DOCS_ENABLED=false` in
production). The operator panel is at `/login`; the public booking form at `/book`
arrives in phase 4.

## Configuration

Two files, with a deliberate split:

- **`.env`** — runtime settings: where the database lives, the public URL, log level.
- **`config/calon.toml`** — scheduling rules: weekdays, hours, timezone, notice, advance
  window, buffers, blackout dates, and any external sources.

Neither is tracked in git. `config/calon.toml` may contain per-source shared secrets.

There is an operator web panel (`/login` and `/bookings`), gated by the `CALON_LOGIN`
key. It is deliberately a **single shared login, not per-user accounts** — calon models
one operator per instance, and the panel exists so the operator can see the bookings and
download the calendar handoffs. It is not a public form; the public booking flow still
goes through the API at `/api/v1/bookings` (and, later, the public booking form at
`/book`). The login session is a short-lived, memory-only cookie; `CALON_LOGIN` keeps the
login secret and is the only credential the instance stores. See
[ADR 0010](adr/0010-operator-login-and-web-panel.md).

Your scheduling rules are a plain text file you can diff, review, and keep in a private
repository. Restart the service after changing it.

**The file wins at every startup.** calon rewrites the rules it holds in the database from
`config/calon.toml` each time it starts, so editing the file and restarting is the whole
configuration workflow, and editing the database by hand accomplishes nothing. A file calon
cannot understand — an unrecognised key, a window that ends before it begins, a timezone
that is not an IANA name — **stops startup** with the offending key named, rather than being
half-applied. Existing bookings are never touched by a rule change: a booking accepted under
yesterday's rules stays accepted. See
[ADR 0008](adr/0008-operator-config-is-toml-authoritative.md).

### External sources

`config/calon.toml` is also where external lead sources are enabled. Each enabled source is a
`[sources.<slug>]` block:

```toml
[sources.openflow]
enabled = true
secret = "generate-with-openssl-rand-hex-32"
resource_slug = "default"
# timestamp_window_seconds = 300
```

A source with no `[sources.<slug>]` block does not exist for calon and its endpoint returns
`404`. Sources are **disabled by default**, and CI runs the full suite with none configured,
so calon stays fully standalone. The endpoint is `POST /api/v1/<slug>`, authenticated by
HMAC-SHA256 with the per-source `secret` (see `docs/external-intake.md` and
[ADR 0012](adr/0012-external-intake-final.md)). The only files here are the adapter implementation
and the config block — nothing in the scheduling core changes when a new source is added, and
no provider-specific adapter ships in `0.1.0` (the framework is proven with a synthetic test
source; the first real adapter lands in `0.2.0`).

## `CALON_INSTANCE_HOST` — set it once, then leave it

This value forms the domain part of every calendar event's `UID`
(`<booking-id>@<instance-host>`). Calendars use the `UID` to recognise an event they already
have.

If you change it later, previously issued events can no longer be updated in place — a
re-downloaded event will appear as a duplicate in the requester's calendar rather than
replacing the original. Pick a stable hostname before your first real booking.

## Security

### The operator login

Set `CALON_LOGIN` in `.env`. It is the single credential that gates the operator surface:

- the web panel — `/login` (the only public page) and `/bookings` (login-gated);
- the calendar handoff — `GET /api/v1/bookings/{id}/calendar.ics` and the deeplinks
  returned in the accept response. That endpoint returns a requester's name and subject,
  so it must never be public.

The login is **one shared key, not per-user accounts**. The session is an HTTP-only
`calon_session` cookie whose value is a random token the server keeps in memory; the
cookie name and token are meaningless without the server state. It is `SameSite=Lax`,
`HttpOnly`, and marked `Secure` when `CALON_BASE_URL` is `https://`. A restart clears all
sessions (in-memory, by design) — no session store on disk, nothing to leak.

The **public booking intake** (`POST /api/v1/bookings` for a booking by a requester) and
**availability** (`GET /api/v1/availability`) remain unauthenticated by design — anyone
should be able to book or check free times. Only the *operator* surface and the
*personal-data* endpoints require the login.

If `CALON_LOGIN` is left empty, the operator panel and the `.ics` endpoint return `503`
("login not configured") — the instance *fails closed* rather than opening the panel to
anyone. The public booking API still works. Set `CALON_LOGIN` before you expose the
instance publicly.

### Optional shared API key

`CALON_API_KEY` (optional) issues a `Bearer` token on the same endpoints the login gates
(`Authorization: Bearer <key>`). Set it if you want to script the operator panel from
cron or wire up an external system. It shares the same authorisation as the login; a
request with either the valid cookie **or** the Bearer key is admitted. Leave it empty to
disable the Bearer path.

### TLS

calon does not terminate TLS. Terminate it at the reverse proxy and forward to port 8000.
Anything that sets `X-Forwarded-Proto` correctly qualifies — Caddy, nginx, or Traefik.
With TLS, set `CALON_BASE_URL` to the `https://` address so the session cookie gains the
`Secure` attribute and the calendar links are absolute and correct.

## Resource calendar sync

This is optional and off by default. With no `[calendars.<resource_slug>]` block in
`config/calon.toml`, a resource has **no external calendar**: availability is checked
against calon's own bookings only, and the requester gets the `.ics` file plus the Google
and Outlook deeplinks. That standalone behaviour is the default and is fully available.

There are two ways to connect a resource's **own** calendar, and they differ in what they
can do and what they cost to set up:

| | Subscribed feed | OAuth connection |
| --- | --- | --- |
| Setup | Publish the calendar, paste the URL | Register an OAuth app in the provider's console |
| Reads busy time | yes | yes |
| Writes accepted bookings | **no** (.ics handoff only) | yes |
| Freshness | the provider's own publish schedule (can lag hours) | live |
| Providers | Google, Microsoft 365, anything publishing ICS | Google (dashboard), Microsoft 365 (out-of-band) |

Start with the feed if you have no developer-console access, or no wish to get any; use the
OAuth connection when you want bookings written into the calendar automatically.

### Subscribing to a published calendar feed

No app registration, no admin rights, no restart. In the calendar's own settings, publish
it and copy the **secret iCal address** (Google Calendar: Settings → *Settings for my
calendars* → the calendar → *Secret address in iCal format*; Outlook / Microsoft 365:
*Publish a calendar* → the ICS link). Paste that address into the **Calendars** panel on
the operator dashboard and click *Subscribe to this feed*.

calon then reads busy time from it and rejects clashing requests with `PROVIDER_CONFLICT`,
exactly like a connected calendar. It is **read-only**: accepted bookings reach your
calendar through the `.ics` file and the one-click links, not by being written in. Anyone
holding the URL can read your calendar, so treat it as a secret — it is stored in
`calon.db` and never shown again.

The same thing in the config file, if you prefer:

```toml
[calendars.default]
provider = "ics"
feed_url = "https://calendar.google.com/calendar/ical/.../basic.ics"
enabled = true
```

A resource uses either a feed or an OAuth connection, never both.

### Connecting with OAuth

To connect a resource's **own** Google Calendar or Microsoft 365 calendar this way you need
an **OAuth app** registered with the provider (a Google Cloud OAuth client, or an Azure AD
app registration). For **Google**, its `client_id` and `client_secret` can go either into
the config file or straight into the operator dashboard (ADR 0016) — the dashboard path
needs no file edit and no restart, which is the one to use on a container or a managed
deployment. For **Microsoft 365** they go into the config.
Registering that OAuth app is always a manual, one-time step: it happens in the provider's
own developer console, and calon cannot do it for you. From there, how the **refresh
token** — the credential that actually authorizes calon to read/write the calendar — gets
into calon differs by provider:

- **Google:** click **Connect with Google** on the operator dashboard (`/bookings`). calon
  runs the OAuth exchange itself and stores the result; nothing to copy-paste, no restart.
- **Microsoft 365, or a scripted/headless Google install:** obtain the refresh token
  **out-of-band** — run the provider's OAuth flow once yourself, outside calon — and paste
  it into `refresh_token` in the config.

When enabled, calon reads that calendar's free/busy when checking availability and writes
each accepted booking back to it, so you do not copy bookings in by hand.

The config block (see `config/calon.example.toml`). `client_id` and `client_secret` are
always required whenever `enabled = true`: without them the provider can never refresh an
access token at all, so calon refuses to start rather than syncing nothing forever without
you noticing. `refresh_token` is required too unless you connect through the dashboard
button instead (Google only).

```toml
[calendars.default]
provider = "google"            # or "microsoft"
calendar_id = "you@example.com"
enabled = true
client_id = "..."              # the OAuth app's client id
client_secret = "..."          # the OAuth app's client secret
refresh_token = "..."          # only if not using the dashboard's Connect button
```

### Google Calendar

**Entirely from the dashboard (recommended — no file edit, no restart):**

1. In Google Cloud Console, create a project and enable the **Calendar API**.
2. Create an **OAuth client ID** of type **Web application**. Add
   `<CALON_BASE_URL>/calendars/google/callback` (e.g.
   `https://booking.example.com/calendars/google/callback`) as an **authorized redirect
   URI** — this must match `CALON_BASE_URL` exactly, protocol included. The Calendars panel
   on the dashboard prints the exact URI your instance will use; copy it from there rather
   than typing it, since a mismatch is the most common cause of a failed connect.
3. Log in to the operator dashboard (`/bookings`), and in the **Calendars** panel paste the
   client id and client secret, plus the `calendar_id` (the account's email for its primary
   calendar; leave blank for `primary`). Save.
4. Click **Connect with Google**. Approve the consent screen — Google requests the
   `https://www.googleapis.com/auth/calendar.events` scope — and you are redirected back,
   connected. Nothing to restart.

The credentials are stored in `calon.db`, so **that file now holds the client secret as
well as the refresh token** — give it the same care as any secret, and include it in your
backups. **Forget credentials** on the panel removes them again, along with the connection
built on them.

**Via the config file:** if you prefer to keep credentials in `config/calon.toml`, put them
there as `client_id` and `client_secret` with `provider = "google"` and `enabled = true`,
leave `refresh_token` blank, and restart calon; then use the dashboard's **Connect with
Google** button for step 4 above. A `[calendars.<slug>]` entry always wins over anything
entered in the dashboard, and the dashboard form refuses to overwrite it.

**Out-of-band (scripted/headless installs, or an OAuth client you cannot expose a
callback URL for):**

1. Create the OAuth client as a *Desktop app* type instead, and run the one-time
   device/browser flow yourself, requesting the same
   `https://www.googleapis.com/auth/calendar.events` scope. When it completes you receive
   an access token **and a refresh token** — use the refresh token.
2. Put `client_id`, `client_secret`, and `refresh_token` in the config as before. The
   dashboard's Connect button still works if you switch to it later — a connection made
   through it takes precedence over this file's `refresh_token`.

### Microsoft 365

1. In the Azure portal, register an application and add the **Graph** `Calendars.ReadWrite`
   (or `Mail.ReadWrite`) delegated permission; note the application (client) id and the
   client secret — these go into `client_id` and `client_secret`.
2. Run the one-time authorization-code flow with `offline_access` in the scope so the
   response includes a refresh token; use that refresh token.
3. `calendar_id` is the **user** the calendar belongs to, e.g.
   `you@tenant.onmicrosoft.com` — the primary calendar of that user is the one synced.

### Security and degradation

`config/calon.toml` always holds the client secret, and holds the refresh token too for
any resource set up out-of-band — treat both like passwords: keep them out of version
control (the example file is a template only), and restrict file permissions on a
production host. For a resource set up through the dashboard instead,
the refresh token lives in `calon.db`'s `calendar_credential` table (ADR 0014), and — if
you entered the OAuth client there rather than in the config file — the `client_id` and
`client_secret` live in its `calendar_oauth_client` table (ADR 0016). Give that file the
same file-permission care as `config/calon.toml`, since it is now also a secrets file, not
just application data. Either way, the token is held in memory for the running
process's lifetime; if the provider rotates it *during that process's uptime*, the
rotation is adopted in memory only and is not written back to the TOML or the database
(unchanged from ADR 0013 — still an open question, not something the Connect button
solves). If a connection stops refreshing, the fix is the same for both paths: reconnect
(via the dashboard button, or by re-running the out-of-band flow) to obtain a fresh token.

When the provider API is unreachable or errors, calon degrades to its own database as the
sole source of truth **for that request** rather than failing the booking — an unreachable
calendar makes availability less accurate, it does not take booking down.

## Backups



**The SQLite database file is the entirety of your booking state.** Back it up.

Copy it with `sqlite3 calon.db ".backup /path/to/backup.db"` rather than `cp` — a plain copy
of a database in WAL mode can capture an inconsistent snapshot. Back up
`config/calon.toml` alongside it.

## Upgrading

```bash
git pull
docker compose up -d --build
```

Read `CHANGELOG.md` first. calon is pre-1.0, so a minor version bump may contain breaking
changes; those are marked `**BREAKING:**` in the changelog. Database migrations run
automatically at startup — take a backup before upgrading.

## Operating notes

- Booking data is personal data. Consider your retention obligations before keeping a long
  audit history.
- The audit log is append-only and grows slowly; it is the record of why calon accepted or
  rejected any given request.
- The booking form has no built-in per-IP rate limit or honeypot field in `0.1.0`. If your
  instance is public and you see abuse, add a rate limit at the reverse proxy in front of
  calon; that is where per-IP throttling belongs for a self-hosted instance.
