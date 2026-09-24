# 19. Encrypt stored calendar secrets with an operator-held key

- **Status:** Proposed — awaiting maintainer approval, because it adds a runtime dependency
  (`CLAUDE.md` §8, §10). Nothing described here is implemented yet.
- **Date:** 2026-09-24
- **Would supersede:** the "No encryption at rest" paragraph of
  [ADR 0014](0014-operator-initiated-google-connect-flow.md), and the matching "No
  column-level encryption" reasoning ADR 0016 and ADR 0017 inherit from it. Nothing else
  in those ADRs would change.

## Context

Since ADR 0014, 0016 and 0017, `calon.db` holds three kinds of calendar secret in plain
text: the Google refresh token (`calendar_credential.refresh_token`), the OAuth app's
secret (`calendar_oauth_client.client_secret`), and the secret address of a published feed
(`calendar_feed.url`). Each grants read access to, or write access into, the operator's
calendar.

ADR 0014 rejected encryption because a key "stored *somewhere*" only moves the problem
while there is one trust boundary, the operator's host. That holds for an attacker who has
the host. It does not hold for the copies of `calon.db` that leave the host without its
environment:

- backups — `docs/self-hosting.md` tells every operator to make them, and they end up in
  object storage, on a NAS, in a backup provider;
- volume snapshots and disk images;
- a database copied off the host to debug an issue, or attached to a bug report.

Each of these carries the calendar credentials along with it. The key, kept in `.env`,
does not travel with them. That separation is what encryption buys, and it is the common
way a self-hosted database leaks.

As of the security review that accompanies this ADR, calon creates `calon.db` with mode
`0600`. That protects the live file from other accounts on the host. It does nothing for
the copies above.

## Decision (proposed)

1. A new, **optional** runtime setting `CALON_SECRET_KEY` holds a key the operator
   generates once. When it is unset, calon behaves exactly as today (plain text). No key
   is needed to book, to run the rule chain, or to use the `.ics` handoff, so the
   standalone path (`CLAUDE.md` §2) is untouched.
2. When it is set, the three columns above are stored encrypted with authenticated
   encryption: Fernet (AES-128-CBC with HMAC-SHA256, encrypt-then-MAC) from the
   `cryptography` package. At startup calon encrypts any value still in plain text, so
   turning the key on protects an existing instance without reconnecting anything.
3. Only these secret columns are encrypted. Booking data, personal data included, stays as
   it is: it is calon's own state, and encrypting it would make a lost key lose the
   bookings. Calendar secrets can always be obtained again by reconnecting.
4. A value that cannot be decrypted — a wrong or lost key — is treated as "not connected":
   the resource degrades to calon-only availability and the dashboard says to reconnect.
   calon never refuses to start or to book over it.

### Alternatives considered

- **Keep plain text, rely on file permissions** (the state after the accompanying change).
  It protects the live file but not backups or copies, which is the gap described above.
- **Derive the key from `CALON_LOGIN`.** No new setting, but changing the login would
  silently make every stored secret unreadable. It would also tie a credential people
  type in to one that must never change. Rejected.
- **Build the encryption from the standard library** (an HMAC-SHA256 keystream with an HMAC
  tag). This fits within `CLAUDE.md` §8's "a stdlib solution under ~50 lines" in size. It
  is still a home-made cipher construction, which a small project cannot review the way a
  cryptography library is reviewed. Rejected, because this is the one place where a
  dependency is the safer choice.
- **SQLCipher (encrypt the whole database file).** It would cover personal data too, but
  it replaces the SQLite driver, needs a native build on every platform, and a lost key
  would lose the bookings. Rejected per ADR 0003.

## Consequences

- **A new runtime dependency**, `cryptography`. It is widely packaged, ships wheels for
  every platform the Docker image targets, and is maintained by the Python Cryptographic
  Authority, but it is a compiled package and adds to the image size. This is the reason
  the ADR needs approval.
- **A lost key means reconnecting calendars.** It never loses bookings. Rotating the key
  needs a documented procedure (decrypt with the old key, re-encrypt with the new one at
  startup). Whether that needs its own setting is a question for implementation.
- `docs/self-hosting.md` and `.env.example` gain the setting. The operator is told to
  store the key **apart from** their backups of `calon.db`, since keeping them together
  gives up the protection.
- An attacker with access to the host still gets both the database and the key. As ADR
  0014 said, encryption does not change that, and this ADR does not claim it does.
