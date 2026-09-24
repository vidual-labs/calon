"""Unit tests for ``calon.security.secretbox`` (ADR 0019): no database, no app."""

from __future__ import annotations

import pytest

from calon.security.secretbox import (
    SEALED_PREFIX,
    SecretBox,
    SecretKeyError,
    SecretKeyMissingError,
    SecretUnreadableError,
)

KEY_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
#: The same kind of key in the standard base64 alphabet (``openssl rand -base64 32``).
KEY_B = "+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/8="
#: The URL-safe spelling of KEY_B, as Fernet itself prints keys.
KEY_B_URLSAFE = "-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_8="


class TestSealing:
    def test_a_sealed_value_round_trips_and_does_not_contain_the_secret(self) -> None:
        box = SecretBox(KEY_A)
        sealed = box.seal("1//refresh-token")
        assert sealed.startswith(SEALED_PREFIX)
        assert "refresh-token" not in sealed
        assert box.unseal(sealed) == "1//refresh-token"

    def test_sealing_the_same_value_twice_gives_different_ciphertexts(self) -> None:
        box = SecretBox(KEY_A)
        assert box.seal("x") != box.seal("x")

    def test_a_plain_text_value_from_before_encryption_passes_through(self) -> None:
        assert SecretBox(KEY_A).unseal("GOCSPX-legacy") == "GOCSPX-legacy"
        assert SecretBox().unseal("GOCSPX-legacy") == "GOCSPX-legacy"

    def test_both_base64_alphabets_name_the_same_key(self) -> None:
        sealed = SecretBox(KEY_B).seal("s")
        assert SecretBox(KEY_B_URLSAFE).unseal(sealed) == "s"


class TestWithoutAKey:
    def test_nothing_can_be_sealed(self) -> None:
        box = SecretBox()
        assert not box.has_key
        with pytest.raises(SecretKeyMissingError, match="CALON_SECRET_KEY"):
            box.seal("s")

    def test_a_sealed_value_is_unreadable(self) -> None:
        sealed = SecretBox(KEY_A).seal("s")
        with pytest.raises(SecretUnreadableError, match="not set"):
            SecretBox().unseal(sealed)


class TestBadKeys:
    @pytest.mark.parametrize("key", ["too-short", "!" * 44, KEY_A + KEY_A])
    def test_a_key_that_is_not_32_bytes_of_base64_is_refused(self, key: str) -> None:
        with pytest.raises(SecretKeyError, match="openssl rand -base64 32"):
            SecretBox(key)

    def test_a_previous_key_without_a_current_one_is_refused(self) -> None:
        with pytest.raises(SecretKeyError, match="CALON_SECRET_KEY_PREVIOUS"):
            SecretBox(None, previous_key=KEY_A)

    def test_the_wrong_key_cannot_read_a_value(self) -> None:
        sealed = SecretBox(KEY_A).seal("s")
        with pytest.raises(SecretUnreadableError):
            SecretBox(KEY_B).unseal(sealed)

    def test_a_tampered_value_is_unreadable_rather_than_raising_something_else(self) -> None:
        box = SecretBox(KEY_A)
        sealed = box.seal("s")
        for tampered in (sealed[:-4] + "AAAA", SEALED_PREFIX + "not-a-token", SEALED_PREFIX + "ä"):
            with pytest.raises(SecretUnreadableError):
                box.unseal(tampered)


class TestRotation:
    def test_a_value_sealed_with_the_previous_key_stays_readable(self) -> None:
        sealed = SecretBox(KEY_A).seal("s")
        assert SecretBox(KEY_B, previous_key=KEY_A).unseal(sealed) == "s"

    def test_needs_reseal(self) -> None:
        old = SecretBox(KEY_A).seal("s")
        rotating = SecretBox(KEY_B, previous_key=KEY_A)
        assert rotating.needs_reseal("plain")  # legacy plain text
        assert rotating.needs_reseal(old)  # sealed under the key being retired
        assert not rotating.needs_reseal(rotating.seal("s"))  # already current
        # A value no configured key opens is left alone, never overwritten.
        stranger = SecretBox("AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=").seal("s")
        assert not rotating.needs_reseal(stranger)
        # Without a key there is nothing to reseal with.
        assert not SecretBox().needs_reseal("plain")
