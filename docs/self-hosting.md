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
- The booking form has a per-IP rate limit and a honeypot field. If your instance is public
  and you see abuse, tighten `CALON_RATE_LIMIT_PER_MINUTE` and add a rate limit at the proxy
  as well.
