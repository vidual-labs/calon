"""Encryption at rest for the calendar secrets calon stores in ``calon.db`` (ADR 0019).

Three columns hold something that grants access to an operator's calendar: the refresh
token (``calendar_credential.refresh_token``), the OAuth app's secret
(``calendar_oauth_client.client_secret``), and a published feed's secret address
(``calendar_feed.url``). With ``CALON_SECRET_KEY`` set, those values are sealed with
Fernet (AES-128-CBC plus HMAC-SHA256, encrypt-then-MAC) before they are written, so a
backup or copy of the database that travels without the key does not carry the calendars
with it.

A sealed value is stored as ``enc:v1:<fernet token>``. Anything without that prefix is a
plain-text value written before encryption existed; :meth:`SecretBox.unseal` passes it
through unchanged, and :func:`calon.services.calendar_connect_service.reseal_secrets`
encrypts it at the next start once a key is set.

Rotation: ``CALON_SECRET_KEY_PREVIOUS`` names the key being retired. Values sealed with it
are still readable, and the start-up reseal re-encrypts them under the new key, after
which the previous key can be dropped.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

__all__ = [
    "SEALED_PREFIX",
    "SecretBox",
    "SecretKeyError",
    "SecretKeyMissingError",
    "SecretUnreadableError",
]

#: Marks a sealed value. Versioned so a future scheme can sit beside this one.
SEALED_PREFIX = "enc:v1:"

_KEY_BYTES = 32


class SecretKeyError(ValueError):
    """A configured key is not usable — a startup configuration error."""


class SecretKeyMissingError(RuntimeError):
    """A secret was about to be stored, but no ``CALON_SECRET_KEY`` is configured."""


class SecretUnreadableError(RuntimeError):
    """A sealed value cannot be opened: no key, the wrong key, or a damaged value.

    The message never includes the value itself.
    """


def _fernet(key: str, *, setting: str) -> Fernet:
    """A Fernet from 32 random bytes, base64-encoded in either alphabet.

    Accepts both ``openssl rand -base64 32`` (standard alphabet) and Fernet's own
    URL-safe output, so the operator can generate the key with whatever tool is at hand.
    """
    text = key.strip().replace("-", "+").replace("_", "/")
    try:
        # Strict: stray characters or data after the padding (say, a key pasted twice)
        # are an error, not something to silently drop.
        raw = binascii.a2b_base64(text + "=" * (-len(text) % 4), strict_mode=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != _KEY_BYTES:
        raise SecretKeyError(
            f"{setting} must be {_KEY_BYTES} random bytes, base64-encoded; "
            "generate one with: openssl rand -base64 32"
        )
    return Fernet(base64.urlsafe_b64encode(raw))


class SecretBox:
    """Seals and opens stored calendar secrets; built once at startup from the settings.

    A box without a key still opens legacy plain-text values, so an instance that never
    set a key keeps working, but it refuses to :meth:`seal` anything new — storing a
    calendar secret requires a key (ADR 0019).
    """

    def __init__(self, key: str | None = None, *, previous_key: str | None = None) -> None:
        if previous_key and not key:
            raise SecretKeyError(
                "CALON_SECRET_KEY_PREVIOUS is set but CALON_SECRET_KEY is not; set the new "
                "key too, or remove the previous one"
            )
        self._primary: Fernet | None = None
        self._all: MultiFernet | None = None
        if key:
            self._primary = _fernet(key, setting="CALON_SECRET_KEY")
            keys = [self._primary]
            if previous_key:
                keys.append(_fernet(previous_key, setting="CALON_SECRET_KEY_PREVIOUS"))
            self._all = MultiFernet(keys)

    @property
    def has_key(self) -> bool:
        return self._primary is not None

    def seal(self, plaintext: str) -> str:
        """Encrypt a secret for storage, or raise :class:`SecretKeyMissingError`."""
        if self._primary is None:
            raise SecretKeyMissingError(
                "storing calendar credentials requires CALON_SECRET_KEY; generate one with "
                "`openssl rand -base64 32`, set it, and restart calon"
            )
        return SEALED_PREFIX + self._primary.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def unseal(self, stored: str) -> str:
        """The secret a stored value holds. Plain-text (legacy) values pass through."""
        if not stored.startswith(SEALED_PREFIX):
            return stored
        if self._all is None:
            raise SecretUnreadableError(
                "a stored calendar secret is encrypted, but CALON_SECRET_KEY is not set"
            )
        try:
            return self._all.decrypt(stored[len(SEALED_PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise SecretUnreadableError(
                "a stored calendar secret cannot be decrypted with CALON_SECRET_KEY or "
                "CALON_SECRET_KEY_PREVIOUS"
            ) from exc

    def needs_reseal(self, stored: str) -> bool:
        """Whether a stored value should be rewritten under the current key.

        True for a plain-text value and for one sealed with the previous key. False when
        there is no key to seal with, and for a value no configured key can open — that
        one is left untouched rather than destroyed.
        """
        if self._primary is None:
            return False
        if not stored.startswith(SEALED_PREFIX):
            return True
        try:
            self._primary.decrypt(stored[len(SEALED_PREFIX) :].encode("ascii"))
        except (InvalidToken, UnicodeError):
            try:
                self.unseal(stored)
            except SecretUnreadableError:
                return False
            return True
        return False
