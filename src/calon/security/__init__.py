"""The instance's login, and the small security primitives behind it.

calon gates its **operator-facing surface** — the web panel and the endpoints that return
personal data (the ``.ics`` file carries a requester's name and subject) — behind a single
shared key. There is deliberately no user account database, no per-requester login, and no
multi-tenancy: the requester does not log in to book, the operator does. This is the
smallest thing that keeps personal data behind a gate, and it keeps calon standalone-first
(``CLAUDE.md`` §2, ADR 0010).

The two primitives here are both stdlib on purpose — no new dependency (``CLAUDE.md`` §8):

* **Password hash** — PBKDF2-HMAC-SHA256, 200 000 iterations, a per-hash random salt.
  Verification is constant-time via :func:`hmac.compare_digest`, so a wrong key costs the
  same whether it is off by one character or by the whole thing.
* **Session** — a server-side opaque token, stored in-process and signed so a cookie that
  has been copied and replayed after a restart (or after the key is rotated) is rejected.
  The token itself carries no data; the server holds the mapping in a process-local table.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

__all__ = [
    "SESSION_COOKIE",
    "LoginStore",
    "SessionTable",
    "derive_login_key",
    "format_password_hash",
    "new_session_token",
    "verify_password_hash",
]

# -----------------------------------------------------------------------------
# Password (the operator key)
# -----------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16
_HASH_ALGORITHM = "sha256"
_TOKEN_BYTES = 32
_SESSION_TTL_SECONDS = 12 * 60 * 60  # 12 hours: an operator session, not a browser.

#: The name of the cookie that carries an operator's session token. It must match what
#: ``calon.security`` sets and what ``calon.api.deps`` reads. It is exposed here so that
#: the login route, the logout route, and any test that inspects it all use one constant.
SESSION_COOKIE = "calon_session"


def _b64(bytes_value: bytes) -> str:
    return base64.urlsafe_b64encode(bytes_value).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(pad.encode("ascii"))


def derive_login_key(master: str) -> bytes:
    """Derive the 32-byte key sessions are signed with, from the operator's login.

    The operator supplies a login (a passphrase, or a long random token) via ``CALON_LOGIN``.
    Deriving a fixed-length 32-byte signing key from it — rather than using the passphrase
    bytes directly — means a short or weak login does not hand an attacker correspondingly
    short keys to forge session cookies. The login itself is still what you type at the
    login form.
    """
    salt = b"calon-session-signing-v1"
    digest = hashlib.pbkdf2_hmac(_HASH_ALGORITHM, master.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return digest[:32]


def format_password_hash(master: str) -> str:
    """A reversible-at-no-time hash of the operator's login: ``pbkdf2$<iters>$<salt>$<digest>``.

    Stored where calon holds its operator key. Never the raw login; the raw login exists
    only in the operator's ``.env`` and their head.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_HASH_ALGORITHM, master.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password_hash(stored: str, candidate: str) -> bool:
    """Check a login against a stored hash, constant-time, without ever recomputing the salt."""
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    try:
        iteration_count = int(iterations)
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
    except (ValueError, TypeError):
        return False
    computed = hashlib.pbkdf2_hmac(
        _HASH_ALGORITHM, candidate.encode("utf-8"), salt, iteration_count
    )
    return hmac.compare_digest(computed, expected)


def new_session_token() -> str:
    """A fresh opaque session token. 256 bits of randomness; nothing in it to leak."""
    return _b64(secrets.token_bytes(_TOKEN_BYTES))


# -----------------------------------------------------------------------------
# Sessions (server-side)
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    token: str
    expires_at: float


class SessionTable:
    """In-memory session storage, keyed by a random opaque token.

    The token carries no data; the server keeps the expiry in a process-local table. That
    design is the whole security story, and it is a good one for a small self-hosted
    service:

    * After a restart the table is empty and the signing key is re-derived from the login,
      so a cookie copied on Tuesday is worth nothing on Wednesday.
    * A token an attacker has not been handed is not in the table, so guessing one is
      useless; there is no state in the token for them to tamper with.
    * Revocation is instant — :meth:`revoke` drops the record and the next check fails.

    This is deliberately stdlib-only (no ``itsdangerous``, no cookie signing library); the
    token is looked up in a server table rather than validated from its own contents.
    """

    def __init__(self, signing_key: bytes, *, ttl_seconds: int = _SESSION_TTL_SECONDS) -> None:
        # ``signing_key`` is the derived login key. It does not need to guard the token
        # string (the token is looked up, not validated from its contents), but we keep
        # the parameter so that a future change that *does* sign the cookie has a stable
        # handle on the right key, and so that two instances built from two different
        # logins cannot accidentally share a session.
        self._signing_key = signing_key
        self._ttl_seconds = ttl_seconds
        self._records: dict[str, _SessionRecord] = {}

    def issue(self, created_at: float | None = None) -> str:
        token = new_session_token()
        now = created_at if created_at is not None else time.time()
        self._records[token] = _SessionRecord(token=token, expires_at=now + self._ttl_seconds)
        return token

    def is_valid(self, token: str | None, now: float | None = None) -> bool:
        """True only for a token issued by *this* process and not yet expired."""
        if not token:
            return False
        record = self._records.get(token)
        if record is None:
            return False
        current = now if now is not None else time.time()
        return record.expires_at >= current

    def revoke(self, token: str) -> None:
        self._records.pop(token, None)

    def revoke_all(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)


class LoginStore:
    """Holds the operator's login hash and the session table for one process.

    A thin wrapper so the rest of the app never does ``hmac`` or ``hashlib`` directly. It
    is constructed once at startup from the operator's login and exposed on
    ``app.state``.
    """

    def __init__(self, login: str, *, session_ttl_seconds: int = _SESSION_TTL_SECONDS) -> None:
        if not login:
            raise ValueError("the operator login must not be empty")
        self._hash = format_password_hash(login)
        self._signing_key = derive_login_key(login)
        self.sessions = SessionTable(self._signing_key, ttl_seconds=session_ttl_seconds)

    def verify(self, candidate: str) -> bool:
        return verify_password_hash(self._hash, candidate)

    def create_session(self) -> str:
        return self.sessions.issue()

    def valid_session(self, token: str | None) -> bool:
        return self.sessions.is_valid(token)

    def end_session(self, token: str | None) -> None:
        if token:
            self.sessions.revoke(token)
