# 19. Encrypt stored calendar secrets with an operator-held key

- **Status:** Accepted. The maintainer approved the new runtime dependency
  (`CLAUDE.md` §8, §10).
- **Date:** 2026-09-24
- **Supersedes:** the "No encryption at rest" paragraph of
  [ADR 0014](0014-operator-initiated-google-connect-flow.md), and the matching "No
  column-level encryption" reasoning ADR 0016 and ADR 0017 inherit from it. Nothing else
  in those ADRs changes.

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

Since the security review that accompanies this ADR, calon creates `calon.db` with mode
`0600`. That protects the live file from other accounts on the host. It does nothing for
the copies above.

## Decision

1. A new runtime setting `CALON_SECRET_KEY` holds a key the operator generates once: 32
   random bytes, base64-encoded (`openssl rand -base64 32`; either base64 alphabet is
   accepted). No key is needed to book, to run the rule chain, or to use the `.ics`
   handoff, so the standalone path (`CLAUDE.md` §2) is untouched.
2. **Storing a calendar secret requires the key.** Without it, the dashboard refuses to
   save an OAuth client, to start a Google connection, or to subscribe to a feed, and says
   why. Plain text is never written again. Secrets kept in `config/calon.toml` are the
   operator's own file and are not affected.
3. The three columns above are stored as `enc:v1:<token>`, with authenticated encryption:
   Fernet (AES-128-CBC with HMAC-SHA256, encrypt-then-MAC) from the `cryptography`
   package. A value without the prefix is plain text from before this ADR. calon still
   reads it, so an instance that upgrades without setting a key keeps its connections, and
   it is encrypted at the next start once a key is set, with no reconnecting.
4. **Rotation:** `CALON_SECRET_KEY_PREVIOUS` names the key being retired. Values sealed
   with either key are readable, and at startup every value not sealed with the current
   key is re-encrypted under it. After one start, the previous key can be removed.
5. Only these secret columns are encrypted. Booking data, personal data included, stays as
   it is: it is calon's own state, and encrypting it would make a lost key lose the
   bookings. Calendar secrets can always be obtained again by reconnecting.
6. A value no configured key can decrypt (no key, or the wrong one) is left untouched,
   never overwritten. Its resource degrades to calon-only availability, and the dashboard
   marks its credentials as unreadable. calon never refuses to start or to book over it.
   A key that is not valid base64 of 32 bytes, by contrast, is a configuration error that
   stops startup, like a malformed `config/calon.toml`.

### Alternatives considered

- **Keep plain text, rely on file permissions** (the state after the accompanying change).
  It protects the live file but not backups or copies, which is the gap described above.
- **Make the key optional, and keep writing plain text without one** (this ADR's first
  draft). Upgrading would then change nothing for an operator who never reads the
  changelog, and their backups would keep carrying their calendars. Rejected: requiring
  the key before anything new is stored makes the protection the default for every new
  connection.
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
  Authority, but it is a compiled package and adds to the image size.
- **Breaking for dashboard users:** an instance that set up calendars from the dashboard
  keeps them after upgrading, but cannot save, connect or subscribe again until
  `CALON_SECRET_KEY` is set.
- **A lost key means reconnecting calendars.** It never loses bookings.
- `docs/self-hosting.md` and `.env.example` document both settings. The operator is told
  to store the key **apart from** their backups of `calon.db`, since keeping them
  together gives up the protection.
- An attacker with access to the host still gets both the database and the key. As ADR
  0014 said, encryption does not change that, and this ADR does not claim it does.
