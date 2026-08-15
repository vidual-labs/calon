# Self-hosting calon

> Status: planned (phase 6). Nothing here works yet — there is no release. This documents
> the intended deployment shape so it can be reviewed before it is built.

calon is designed to run on one small server with no external services. A single container,
a single SQLite file, and a reverse proxy in front of it.

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
# Edit .env: at minimum CALON_BASE_URL and CALON_INSTANCE_HOST.

docker compose up -d
```

The booking form is at `/book` and the generated API reference at `/docs`.

## Configuration

Two files, with a deliberate split:

- **`.env`** — runtime settings: where the database lives, the public URL, log level.
- **`config/calon.toml`** — scheduling rules: weekdays, hours, timezone, notice, advance
  window, buffers, blackout dates, and any external sources.

Neither is tracked in git. `config/calon.toml` may contain per-source shared secrets.

There is no admin UI, which is a deliberate simplification rather than an omission: no admin
UI means no login, no sessions, and no password storage anywhere in calon. Your rules are a
plain text file you can diff, review, and keep in a private repository. Restart the service
after changing it.

## `CALON_INSTANCE_HOST` — set it once, then leave it

This value forms the domain part of every calendar event's `UID`
(`<booking-id>@<instance-host>`). Calendars use the `UID` to recognise an event they already
have.

If you change it later, previously issued events can no longer be updated in place — a
re-downloaded event will appear as a duplicate in the requester's calendar rather than
replacing the original. Pick a stable hostname before your first real booking.

## Reverse proxy

Terminate TLS in front of calon and forward to port 8000. Anything that sets
`X-Forwarded-Proto` and `X-Forwarded-For` correctly will do — Caddy, nginx, or Traefik.

Consider restricting `/api/v1/…` at the proxy if you only need the public booking form. The
form at `/book` is meant to be public; the API generally is not.

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
