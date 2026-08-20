"""FakeCalendar unit tests: the provider seam is testable without a network.

The :class:`FakeCalendar` is the in-memory implementation of :class:`CalendarProvider`
that the wiring tests (Batch 3) and the provider tests (Batch 4/5) both lean on. Here we
pin its contract directly: free/busy returns exactly the seeded spans that overlap the
window, upserts store events keyed by UID, and the same object can be asked to fail so a
call site can exercise its degrade-to-calon-only path without an HTTP mock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from calon.calendars import CalendarEvent, CalendarProvider, CalendarProviderError, FakeCalendar
from calon.domain import FreeBusySpan

TUESDAY = 2026, 1, 6  # a real Tuesday


def at(hour: int, minute: int = 0) -> datetime:
    """A 2026 instant in UTC at the given hour/minute, built the same way as the domain tests."""
    return datetime(*TUESDAY, hour, minute, tzinfo=UTC)


class TestFreeBusyContract:
    def test_an_empty_calendar_returns_no_spans(self):
        provider = FakeCalendar()
        assert provider.free_busy("default", at(10, 0), at(11, 0)) == ()

    def test_a_seeded_span_that_overlaps_the_window_is_returned(self):
        provider = FakeCalendar()
        provider.seed_busy("default", at(10, 30), at(11, 30), reason="lunch")
        (span,) = provider.free_busy("default", at(10, 0), at(12, 0))
        assert isinstance(span, FreeBusySpan)
        assert span.starts_at_utc == at(10, 30)
        assert span.ends_at_utc == at(11, 30)
        assert span.reason == "lunch"

    def test_a_seeded_span_outside_the_window_is_not_returned(self):
        provider = FakeCalendar()
        provider.seed_busy("default", at(2, 0), at(3, 0), reason="early")
        assert provider.free_busy("default", at(10, 0), at(12, 0)) == ()

    def test_only_the_overlapping_spans_are_returned_and_sorted(self):
        provider = FakeCalendar()
        provider.seed_busy("default", at(13, 0), at(14, 0), reason="b")
        provider.seed_busy("default", at(10, 0), at(10, 30), reason="a")
        provider.seed_busy("other", at(10, 0), at(10, 30), reason="wrong-resource")
        spans = provider.free_busy("default", at(9, 0), at(15, 0))
        assert [s.reason for s in spans] == ["a", "b"]

    def test_a_provider_configured_to_fail_raises_the_provider_error(self):
        provider = FakeCalendar()
        provider.fail_free_busy = True
        with pytest.raises(CalendarProviderError):
            provider.free_busy("default", at(10, 0), at(12, 0))

    def test_free_busy_uses_a_half_open_overlap_so_back_to_back_is_free(self):
        # A span ending exactly at the window start does not overlap: [a, b) is standard.
        provider = FakeCalendar()
        provider.seed_busy("default", at(9, 0), at(10, 0), reason="prior")
        assert provider.free_busy("default", at(10, 0), at(11, 0)) == ()


class TestUpsertContract:
    def _event(self, uid: str = "abc", summary: str = "Consultation with Ada") -> CalendarEvent:
        return CalendarEvent(
            uid=uid,
            summary=summary,
            starts_at_utc=at(10, 0),
            ends_at_utc=at(11, 0),
        )

    def test_upsert_stores_the_event_keyed_by_its_uid(self):
        provider = FakeCalendar()
        provider.upsert_event("default", self._event("uid-1"))
        stored = provider.event("default", "uid-1")
        assert stored is not None
        assert stored == self._event("uid-1")

    def test_upsert_is_idempotent_for_the_same_uid(self):
        provider = FakeCalendar()
        provider.upsert_event("default", self._event("uid-1"))
        provider.upsert_event("default", self._event("uid-1"))
        assert len(provider.events("default")) == 1

    def test_upsert_replacing_an_existing_uid_does_not_duplicate(self):
        provider = FakeCalendar()
        provider.upsert_event("default", self._event("uid-1"))
        provider.upsert_event("default", self._event("uid-1", summary="amended"))
        stored = provider.event("default", "uid-1")
        assert stored is not None
        assert stored.summary == "amended"

    def test_a_provider_configured_to_fail_raises_in_upsert(self):
        provider = FakeCalendar()
        provider.fail_upsert = True
        with pytest.raises(CalendarProviderError):
            provider.upsert_event("default", self._event())

    def test_an_event_that_ends_before_it_starts_is_rejected(self):
        with pytest.raises(ValueError):
            CalendarEvent(
                uid="x",
                summary="bad",
                starts_at_utc=at(11, 0),
                ends_at_utc=at(10, 0),
            )


class TestProviderContract:
    def test_fake_calendar_is_a_calendar_provider(self):
        assert isinstance(FakeCalendar(), CalendarProvider)

    def test_the_name_can_be_overridden(self):
        assert FakeCalendar().name == "fake"
        assert FakeCalendar(name="demo").name == "demo"
