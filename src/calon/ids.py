"""Sortable identifiers.

Every row in calon is keyed by a UUIDv7-style identifier stored as a string. Version 7
puts a millisecond timestamp in the high bits, so identifiers sort chronologically — which
means an index on the primary key is also an index on creation order, and rows come back
roughly in the order they were written without a secondary sort.

Roughly, not exactly: two identifiers minted in the same millisecond are ordered by their
random tails. Where a strict order matters — the audit log, whose events share a timestamp
by design — there is an explicit sequence column instead.

Python 3.12's ``uuid`` module has no version 7 generator, and the layout is about twenty
lines (RFC 9562 §5.7), so this is hand-rolled rather than pulled in as a dependency.
"""

from __future__ import annotations

import secrets
import time
from uuid import UUID

__all__ = ["new_id", "uuid7"]


def uuid7() -> UUID:
    """A UUIDv7: 48 bits of Unix milliseconds, then 74 bits of randomness.

    The variant and version bits are set per RFC 9562, so the result is a well-formed UUID
    that other tools will recognise rather than a lookalike string.
    """
    unix_ms = time.time_ns() // 1_000_000
    payload = bytearray(secrets.token_bytes(16))

    payload[0:6] = unix_ms.to_bytes(6, "big")
    # Version 7 in the top nibble of byte 6, RFC 4122 variant in the top bits of byte 8.
    payload[6] = (payload[6] & 0x0F) | 0x70
    payload[8] = (payload[8] & 0x3F) | 0x80

    return UUID(bytes=bytes(payload))


def new_id() -> str:
    """A fresh identifier in the canonical hyphenated form, ready to store."""
    return str(uuid7())
