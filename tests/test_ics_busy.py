"""Reading busy time out of a published ICS feed (ADR 0017), as a pure function.

No network and no fixtures: every case is an iCalendar document written inline and the
spans it should reduce to. The boundaries that actually bite are here — a recurrence
crossing a DST change, an all-day event in a resource timezone that is not UTC, an
``EXDATE``, a ``RECURRENCE-ID`` override, and a feed with one broken event among good
ones (``CLAUDE.md`` §8).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from calon.calendarkit._ics_busy import IcsFeedError, busy_spans_from_ics

BERLIN = "Europe/Berlin"


def _feed(*events: str) -> str:
    body = "\r\n".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{body}\r\nEND:VCALENDAR\r\n"


def _event(**properties: str) -> str:
    lines = "\r\n".join(f"{key.replace('_', '-')}:{value}" for key, value in properties.items())
    return f"BEGIN:VEVENT\r\n{lines}\r\nEND:VEVENT"


def _spans(
    text: str,
    *,
    start: datetime,
    end: datetime,
    timezone: str = BERLIN,
) -> list[tuple[datetime, datetime]]:
    return [
        (span.starts_at_utc, span.ends_at_utc)
        for span in busy_spans_from_ics(
            text, window_start_utc=start, window_end_utc=end, default_timezone=timezone
        )
    ]


WINDOW_START = datetime(2026, 3, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 5, 1, tzinfo=UTC)


class TestSingleEvents:
    def test_a_utc_event_becomes_one_span(self) -> None:
        text = _feed(
            _event(
                UID="a@example.com",
                DTSTART="20260310T090000Z",
                DTEND="20260310T103000Z",
                SUMMARY="Standup",
            )
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == [
            (datetime(2026, 3, 10, 9, 0, tzinfo=UTC), datetime(2026, 3, 10, 10, 30, tzinfo=UTC))
        ]

    def test_a_zoned_event_is_converted_to_utc(self) -> None:
        text = _feed(
            "BEGIN:VEVENT\r\nUID:b@example.com\r\n"
            "DTSTART;TZID=Europe/Berlin:20260310T090000\r\n"
            "DTEND;TZID=Europe/Berlin:20260310T100000\r\nEND:VEVENT"
        )
        # 09:00 CET (UTC+1 in early March) is 08:00 UTC.
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == [
            (datetime(2026, 3, 10, 8, 0, tzinfo=UTC), datetime(2026, 3, 10, 9, 0, tzinfo=UTC))
        ]

    def test_a_floating_event_is_read_in_the_resource_timezone(self) -> None:
        text = _feed(
            _event(UID="c@example.com", DTSTART="20260310T090000", DTEND="20260310T100000")
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == [
            (datetime(2026, 3, 10, 8, 0, tzinfo=UTC), datetime(2026, 3, 10, 9, 0, tzinfo=UTC))
        ]

    def test_an_all_day_event_blocks_the_whole_local_day(self) -> None:
        # Written the long way so the VALUE=DATE parameter survives.
        text = _feed(
            "BEGIN:VEVENT\r\nUID:d@example.com\r\nDTSTART;VALUE=DATE:20260701\r\nEND:VEVENT"
        )
        assert _spans(
            text,
            start=datetime(2026, 6, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, tzinfo=UTC),
        ) == [
            # 1 July 2026 in Berlin is CEST (UTC+2): 30 June 22:00Z → 1 July 22:00Z.
            (datetime(2026, 6, 30, 22, 0, tzinfo=UTC), datetime(2026, 7, 1, 22, 0, tzinfo=UTC))
        ]

    def test_duration_is_used_when_there_is_no_end(self) -> None:
        text = _feed(_event(UID="e@example.com", DTSTART="20260310T090000Z", DURATION="PT45M"))
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == [
            (datetime(2026, 3, 10, 9, 0, tzinfo=UTC), datetime(2026, 3, 10, 9, 45, tzinfo=UTC))
        ]

    def test_a_zero_length_event_blocks_nothing(self) -> None:
        text = _feed(
            _event(UID="f@example.com", DTSTART="20260310T090000Z", DTEND="20260310T090000Z")
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == []

    def test_spans_are_clipped_to_the_window(self) -> None:
        text = _feed(
            _event(UID="g@example.com", DTSTART="20260228T000000Z", DTEND="20260302T000000Z")
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == [
            (WINDOW_START, datetime(2026, 3, 2, tzinfo=UTC))
        ]

    def test_an_event_entirely_outside_the_window_is_dropped(self) -> None:
        text = _feed(
            _event(UID="h@example.com", DTSTART="20260610T090000Z", DTEND="20260610T100000Z")
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == []


class TestWhatDoesNotCount:
    def test_a_cancelled_event_is_ignored(self) -> None:
        text = _feed(
            _event(
                UID="i@example.com",
                DTSTART="20260310T090000Z",
                DTEND="20260310T100000Z",
                STATUS="CANCELLED",
            )
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == []

    def test_a_transparent_event_is_ignored(self) -> None:
        """ "Show as free" is the publisher saying this does not occupy their time."""
        text = _feed(
            _event(
                UID="j@example.com",
                DTSTART="20260310T090000Z",
                DTEND="20260310T100000Z",
                TRANSP="TRANSPARENT",
            )
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == []

    def test_a_todo_is_not_busy_time(self) -> None:
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            "BEGIN:VTODO\r\nUID:k@example.com\r\nDUE:20260310T090000Z\r\nEND:VTODO\r\n"
            "END:VCALENDAR\r\n"
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == []


class TestRecurrence:
    def test_a_weekly_rule_keeps_its_local_hour_across_a_dst_change(self) -> None:
        """The case that makes naive UTC expansion wrong.

        A 09:00 Berlin meeting is 08:00 UTC before the 29 March change and 07:00 UTC
        after it. Expanding in UTC would have kept it at 08:00 and silently freed an
        hour that is actually busy.
        """
        text = _feed(
            "BEGIN:VEVENT\r\nUID:l@example.com\r\n"
            "DTSTART;TZID=Europe/Berlin:20260323T090000\r\n"
            "DTEND;TZID=Europe/Berlin:20260323T100000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=3\r\nEND:VEVENT"
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == [
            (datetime(2026, 3, 23, 8, 0, tzinfo=UTC), datetime(2026, 3, 23, 9, 0, tzinfo=UTC)),
            (datetime(2026, 3, 30, 7, 0, tzinfo=UTC), datetime(2026, 3, 30, 8, 0, tzinfo=UTC)),
            (datetime(2026, 4, 6, 7, 0, tzinfo=UTC), datetime(2026, 4, 6, 8, 0, tzinfo=UTC)),
        ]

    def test_only_occurrences_inside_the_window_are_returned(self) -> None:
        text = _feed(
            _event(
                UID="m@example.com",
                DTSTART="20260302T090000Z",
                DTEND="20260302T100000Z",
                RRULE="FREQ=DAILY",
            )
        )
        spans = _spans(
            text,
            start=datetime(2026, 3, 4, tzinfo=UTC),
            end=datetime(2026, 3, 7, tzinfo=UTC),
        )
        assert spans == [
            (datetime(2026, 3, 4, 9, 0, tzinfo=UTC), datetime(2026, 3, 4, 10, 0, tzinfo=UTC)),
            (datetime(2026, 3, 5, 9, 0, tzinfo=UTC), datetime(2026, 3, 5, 10, 0, tzinfo=UTC)),
            (datetime(2026, 3, 6, 9, 0, tzinfo=UTC), datetime(2026, 3, 6, 10, 0, tzinfo=UTC)),
        ]

    def test_an_occurrence_starting_before_the_window_still_overlaps_it(self) -> None:
        text = _feed(
            _event(
                UID="n@example.com",
                DTSTART="20260302T230000Z",
                DTEND="20260303T020000Z",
                RRULE="FREQ=DAILY",
            )
        )
        spans = _spans(
            text,
            start=datetime(2026, 3, 4, tzinfo=UTC),
            end=datetime(2026, 3, 4, 12, tzinfo=UTC),
        )
        assert spans == [(datetime(2026, 3, 4, tzinfo=UTC), datetime(2026, 3, 4, 2, 0, tzinfo=UTC))]

    def test_exdate_removes_an_occurrence(self) -> None:
        text = _feed(
            _event(
                UID="o@example.com",
                DTSTART="20260302T090000Z",
                DTEND="20260302T100000Z",
                RRULE="FREQ=DAILY;COUNT=3",
                EXDATE="20260303T090000Z",
            )
        )
        starts = [start for start, _ in _spans(text, start=WINDOW_START, end=WINDOW_END)]
        assert starts == [
            datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            datetime(2026, 3, 4, 9, 0, tzinfo=UTC),
        ]

    def test_rdate_adds_an_occurrence(self) -> None:
        text = _feed(
            _event(
                UID="p@example.com",
                DTSTART="20260302T090000Z",
                DTEND="20260302T100000Z",
                RDATE="20260305T140000Z",
            )
        )
        starts = [start for start, _ in _spans(text, start=WINDOW_START, end=WINDOW_END)]
        assert starts == [
            datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            datetime(2026, 3, 5, 14, 0, tzinfo=UTC),
        ]

    def test_a_recurrence_id_override_replaces_that_instance(self) -> None:
        text = _feed(
            _event(
                UID="q@example.com",
                DTSTART="20260302T090000Z",
                DTEND="20260302T100000Z",
                RRULE="FREQ=DAILY;COUNT=3",
            ),
            _event(
                UID="q@example.com",
                RECURRENCE_ID="20260303T090000Z",
                DTSTART="20260303T140000Z",
                DTEND="20260303T150000Z",
            ),
        )
        starts = [start for start, _ in _spans(text, start=WINDOW_START, end=WINDOW_END)]
        assert starts == [
            datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            datetime(2026, 3, 3, 14, 0, tzinfo=UTC),  # moved, not 09:00
            datetime(2026, 3, 4, 9, 0, tzinfo=UTC),
        ]

    def test_a_cancelled_override_frees_that_instance(self) -> None:
        text = _feed(
            _event(
                UID="r@example.com",
                DTSTART="20260302T090000Z",
                DTEND="20260302T100000Z",
                RRULE="FREQ=DAILY;COUNT=3",
            ),
            _event(
                UID="r@example.com",
                RECURRENCE_ID="20260303T090000Z",
                DTSTART="20260303T090000Z",
                DTEND="20260303T100000Z",
                STATUS="CANCELLED",
            ),
        )
        starts = [start for start, _ in _spans(text, start=WINDOW_START, end=WINDOW_END)]
        assert datetime(2026, 3, 3, 9, 0, tzinfo=UTC) not in starts
        assert len(starts) == 2

    def test_an_until_bounded_rule_stops(self) -> None:
        text = _feed(
            _event(
                UID="s@example.com",
                DTSTART="20260302T090000Z",
                DTEND="20260302T100000Z",
                RRULE="FREQ=DAILY;UNTIL=20260304T090000Z",
            )
        )
        starts = [start for start, _ in _spans(text, start=WINDOW_START, end=WINDOW_END)]
        assert starts[0] == datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        assert starts[-1] <= datetime(2026, 3, 4, 10, 0, tzinfo=UTC)

    def test_an_absurd_frequency_is_ignored_rather_than_expanded(self) -> None:
        """A hostile feed must not be able to make expansion cost millions of steps."""
        text = _feed(
            _event(
                UID="t@example.com",
                DTSTART="20260302T090000Z",
                DTEND="20260302T090100Z",
                RRULE="FREQ=SECONDLY",
            )
        )
        spans = _spans(text, start=WINDOW_START, end=WINDOW_END)
        assert spans == [
            (datetime(2026, 3, 2, 9, 0, tzinfo=UTC), datetime(2026, 3, 2, 9, 1, tzinfo=UTC))
        ]


class TestRobustness:
    def test_one_broken_event_does_not_hide_the_others(self) -> None:
        text = _feed(
            _event(UID="u@example.com", SUMMARY="no start at all"),
            _event(UID="v@example.com", DTSTART="20260310T090000Z", DTEND="20260310T100000Z"),
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == [
            (datetime(2026, 3, 10, 9, 0, tzinfo=UTC), datetime(2026, 3, 10, 10, 0, tzinfo=UTC))
        ]

    def test_a_document_that_is_not_icalendar_raises(self) -> None:
        with pytest.raises(IcsFeedError):
            busy_spans_from_ics(
                "<html>404 Not Found</html>",
                window_start_utc=WINDOW_START,
                window_end_utc=WINDOW_END,
            )

    def test_an_empty_calendar_is_simply_empty(self) -> None:
        text = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nEND:VCALENDAR\r\n"
        assert _spans(text, start=WINDOW_START, end=WINDOW_END) == []

    def test_an_unknown_timezone_falls_back_to_utc(self) -> None:
        text = _feed(
            _event(UID="w@example.com", DTSTART="20260310T090000", DTEND="20260310T100000")
        )
        assert _spans(text, start=WINDOW_START, end=WINDOW_END, timezone="Mars/Olympus") == [
            (datetime(2026, 3, 10, 9, 0, tzinfo=UTC), datetime(2026, 3, 10, 10, 0, tzinfo=UTC))
        ]
