"""Reading busy time out of a published ICS feed (ADR 0017).

The mirror image of ``_ics.py``: that module *writes* the one event calon hands to a
requester, this one *reads* somebody else's whole calendar and reduces it to the only
thing the rule chain cares about — a tuple of busy spans in UTC.

Pure and network-free on purpose. :func:`busy_spans_from_ics` takes the feed's text and
the window, and returns :class:`~calon.domain.FreeBusySpan` values; fetching the text is
the provider's job (``calon.calendars.ics_feed``). That split is what lets every
recurrence, DST, and all-day edge case below be a unit test with no HTTP at all.

What counts as busy, and why:

* ``VEVENT`` only. ``VTODO``/``VJOURNAL``/``VFREEBUSY`` components are ignored — a task
  due at 14:00 does not occupy 14:00.
* ``STATUS:CANCELLED`` and ``TRANSP:TRANSPARENT`` are skipped. The first is not happening;
  the second is the publisher explicitly saying "this does not block my time" (that is
  what Google's "free" and Outlook's "show as free" set).
* Recurrences are expanded properly, in the event's **own** timezone, so a weekly 09:00
  meeting stays at 09:00 across a DST boundary instead of drifting by an hour.
  ``RRULE``, ``RDATE``, and ``EXDATE`` are honoured, and a ``RECURRENCE-ID`` component
  replaces the single instance it overrides.
* An all-day event (a ``DATE``-valued ``DTSTART``) blocks whole days in
  ``default_timezone`` — the resource's own zone, which is what an operator means when
  they mark a day off.

A feed this module cannot parse at all raises :class:`IcsFeedError`; the provider turns
that into the usual degrade-to-calon-only. A *single* malformed event is skipped instead,
because one unreadable entry in a year of calendar should not blind the resource to the
other 364 days.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrule, rruleset, rrulestr
from icalendar import Calendar

from calon.domain import FreeBusySpan

__all__ = ["IcsFeedError", "busy_spans_from_ics"]

logger = logging.getLogger("calon.calendarkit.ics_busy")

#: Recurrence frequencies no human calendar uses, and which a hostile or broken feed
#: could use to make expansion arbitrarily expensive. Skipped rather than expanded.
_ABSURD_FREQUENCIES = frozenset({"SECONDLY", "MINUTELY"})

#: Hard cap on expanded occurrences per event, whatever the window asks for. A bounded
#: window already bounds this; the cap is the backstop for a pathological rule.
_MAX_OCCURRENCES = 1000


class IcsFeedError(ValueError):
    """The feed could not be parsed as an iCalendar document at all."""


def busy_spans_from_ics(
    text: str,
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
    default_timezone: str = "UTC",
    reason: str = "",
) -> tuple[FreeBusySpan, ...]:
    """Every busy span in ``text`` overlapping ``[window_start_utc, window_end_utc)``.

    Spans are clipped to the window, so a week-long event contributes only the part
    inside it. The result is sorted by start; overlapping spans are *not* merged, because
    the rule chain checks overlap per span and merging would lose the distinction between
    two separate commitments.
    """
    try:
        calendar = Calendar.from_ical(text)
    except Exception as exc:  # icalendar raises a bare ValueError subclass family
        raise IcsFeedError(f"the feed is not a readable iCalendar document: {exc}") from exc

    zone = _zone(default_timezone)
    masters, overrides = _partition(calendar)

    spans: list[FreeBusySpan] = []
    for uid, event in masters.items():
        try:
            spans.extend(
                _spans_for_event(
                    event,
                    overrides=overrides.get(uid, {}),
                    window_start_utc=window_start_utc,
                    window_end_utc=window_end_utc,
                    zone=zone,
                    reason=reason,
                )
            )
        except Exception as exc:  # one bad event must not blind the whole feed
            logger.warning("skipping an unreadable event in the calendar feed: %s", exc)

    # Overrides whose master is absent from the feed (a common shape when a feed only
    # publishes a window) are still real commitments.
    for uid, by_recurrence_id in overrides.items():
        if uid in masters:
            continue
        for event in by_recurrence_id.values():
            if _is_skipped(event):
                continue
            try:
                spans.extend(
                    _single_span(
                        event,
                        window_start_utc=window_start_utc,
                        window_end_utc=window_end_utc,
                        zone=zone,
                        reason=reason,
                    )
                )
            except Exception as exc:
                logger.warning("skipping an unreadable event in the calendar feed: %s", exc)

    return tuple(sorted(spans, key=lambda span: (span.starts_at_utc, span.ends_at_utc)))


# ---------------------------------------------------------------------------
# Component walk
# ---------------------------------------------------------------------------


def _partition(
    calendar: Calendar,
) -> tuple[dict[str, Any], dict[str, dict[datetime | date, Any]]]:
    """Split the feed's events into masters and ``RECURRENCE-ID`` overrides, by UID.

    An event with no UID cannot be correlated with an override, so it is given a unique
    synthetic key and treated as its own master.
    """
    masters: dict[str, Any] = {}
    overrides: dict[str, dict[datetime | date, Any]] = {}

    for index, component in enumerate(calendar.walk("VEVENT")):
        recurrence_id = component.get("RECURRENCE-ID")
        uid = str(component.get("UID", "")) or f"\x00no-uid-{index}"
        if recurrence_id is None:
            if not _is_skipped(component):
                masters[uid] = component
            continue
        # A *cancelled* override is kept deliberately: it is how a feed says "this one
        # instance of the series is off", so the master must still learn to exclude that
        # occurrence. Dropping it here would leave the instance busy forever.
        overrides.setdefault(uid, {})[recurrence_id.dt] = component
    return masters, overrides


def _is_skipped(component: Any) -> bool:
    """A cancelled or explicitly free event does not occupy the resource's time."""
    status = str(component.get("STATUS", "")).upper()
    transparency = str(component.get("TRANSP", "")).upper()
    return status == "CANCELLED" or transparency == "TRANSPARENT"


# ---------------------------------------------------------------------------
# One event → its spans
# ---------------------------------------------------------------------------


def _spans_for_event(
    component: Any,
    *,
    overrides: dict[datetime | date, Any],
    window_start_utc: datetime,
    window_end_utc: datetime,
    zone: ZoneInfo,
    reason: str,
) -> Iterator[FreeBusySpan]:
    """Expand one master event (with its overrides) into the spans inside the window."""
    start_raw = _required_dt(component, "DTSTART")
    all_day = isinstance(start_raw, date) and not isinstance(start_raw, datetime)
    event_zone = _event_zone(start_raw, zone)
    duration = _duration(component, start_raw, all_day=all_day)
    if duration <= timedelta(0):
        return

    # Overrides replace the instance they name, so their own times are emitted here and
    # their original occurrence is excluded from the master's set below. A cancelled or
    # transparent override contributes nothing but still does that excluding.
    for override in overrides.values():
        if _is_skipped(override):
            continue
        yield from _single_span(
            override,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            zone=zone,
            reason=reason,
        )

    local_start = _to_local_naive(start_raw, event_zone)
    rule_text = _rule_text(component)

    if rule_text is None and not component.get("RDATE"):
        starts_local = [local_start]
    else:
        starts_local = _expand(
            component,
            rule_text=rule_text,
            local_start=local_start,
            event_zone=event_zone,
            duration=duration,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
        )

    excluded = {_to_local_naive(key, event_zone) for key in overrides}
    for occurrence in starts_local:
        if occurrence in excluded:
            continue
        starts_at = occurrence.replace(tzinfo=event_zone).astimezone(UTC)
        span = _clip(starts_at, starts_at + duration, window_start_utc, window_end_utc, reason)
        if span is not None:
            yield span


def _single_span(
    component: Any,
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
    zone: ZoneInfo,
    reason: str,
) -> Iterator[FreeBusySpan]:
    """One non-recurring occurrence — an override, or an event with no rule."""
    start_raw = _required_dt(component, "DTSTART")
    all_day = isinstance(start_raw, date) and not isinstance(start_raw, datetime)
    event_zone = _event_zone(start_raw, zone)
    duration = _duration(component, start_raw, all_day=all_day)
    if duration <= timedelta(0):
        return
    starts_at = _to_local_naive(start_raw, event_zone).replace(tzinfo=event_zone).astimezone(UTC)
    span = _clip(starts_at, starts_at + duration, window_start_utc, window_end_utc, reason)
    if span is not None:
        yield span


def _expand(
    component: Any,
    *,
    rule_text: str | None,
    local_start: datetime,
    event_zone: ZoneInfo,
    duration: timedelta,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> list[datetime]:
    """Occurrence starts (naive, in the event's own zone) that can touch the window.

    Expansion happens in local wall-clock time and is converted to UTC by the caller —
    that order is what keeps a 09:00 weekly meeting at 09:00 across a DST change.
    """
    rules = rruleset()
    if rule_text is not None:
        parsed = rrulestr(rule_text, dtstart=local_start)
        if not isinstance(parsed, rrule):
            # Only reachable if a single RRULE line somehow parses to a set; expanding an
            # unbounded set blindly is not safe, so treat it as unreadable.
            raise ValueError("the recurrence rule could not be read as a single rule")
        rules.rrule(parsed)
    else:
        rules.rdate(local_start)

    for value in _date_values(component, "RDATE"):
        rules.rdate(_to_local_naive(value, event_zone))
    for value in _date_values(component, "EXDATE"):
        rules.exdate(_to_local_naive(value, event_zone))

    # The window is in UTC; the search bounds must be in the same local space as the
    # rule. An event that starts before the window can still overlap it, hence the
    # duration-wide margin at the front.
    search_from = window_start_utc.astimezone(event_zone).replace(tzinfo=None) - duration
    search_to = window_end_utc.astimezone(event_zone).replace(tzinfo=None)

    occurrences: list[datetime] = []
    for occurrence in rules.xafter(search_from, count=_MAX_OCCURRENCES, inc=True):
        if occurrence >= search_to:
            break
        occurrences.append(occurrence)
    return occurrences


# ---------------------------------------------------------------------------
# Property readers
# ---------------------------------------------------------------------------


def _required_dt(component: Any, name: str) -> datetime | date:
    prop = component.get(name)
    if prop is None:
        raise ValueError(f"the event has no {name}")
    value = prop.dt
    if not isinstance(value, date):
        raise ValueError(f"{name} is not a date or datetime")
    return value


def _duration(component: Any, start: datetime | date, *, all_day: bool) -> timedelta:
    """The event's length, from ``DTEND`` or ``DURATION``.

    RFC 5545 allows either, and neither: a ``DATE``-valued event with no end is one whole
    day, and a ``DATE-TIME`` one is instantaneous (and therefore blocks nothing).
    """
    end_prop = component.get("DTEND")
    if end_prop is not None:
        end = end_prop.dt
        if isinstance(end, datetime) != isinstance(start, datetime):
            raise ValueError("DTSTART and DTEND disagree about being dates or datetimes")
        return _as_naive(end) - _as_naive(start)

    duration_prop = component.get("DURATION")
    if duration_prop is not None:
        value = duration_prop.dt
        if not isinstance(value, timedelta):
            raise ValueError("DURATION is not a duration")
        return value

    return timedelta(days=1) if all_day else timedelta(0)


def _rule_text(component: Any) -> str | None:
    """The event's ``RRULE`` as a string dateutil can parse, or ``None``.

    A rule whose frequency is absurd for a calendar (see :data:`_ABSURD_FREQUENCIES`) is
    dropped: expanding it could cost millions of iterations for a feed calon does not
    control, and no real meeting recurs every second.
    """
    rule = component.get("RRULE")
    if rule is None:
        return None
    if isinstance(rule, list):  # multiple RRULEs are legal but vanishingly rare
        rule = rule[0]
    text = rule.to_ical().decode("utf-8")
    frequency = ""
    for part in text.split(";"):
        if part.upper().startswith("FREQ="):
            frequency = part.split("=", 1)[1].upper()
    if frequency in _ABSURD_FREQUENCIES:
        logger.warning("ignoring a %s recurrence rule in the calendar feed", frequency)
        return None
    return _normalise_until(text)


def _normalise_until(rule_text: str) -> str:
    """Strip ``UNTIL``'s timezone so it matches the naive local ``DTSTART``.

    dateutil refuses a rule whose ``UNTIL`` and ``DTSTART`` disagree about awareness, and
    every real feed writes ``UNTIL`` in UTC while we expand in local time. Dropping the
    marker keeps the rule usable; the resulting end bound can be off by the zone's offset
    for the final occurrence only, which cannot change whether earlier ones are busy.
    """
    parts = []
    for part in rule_text.split(";"):
        if part.upper().startswith("UNTIL=") and part.upper().endswith("Z"):
            parts.append(part[:-1])
        else:
            parts.append(part)
    return ";".join(parts)


def _date_values(component: Any, name: str) -> Iterable[datetime | date]:
    """Every value of a possibly-repeated date-list property (``RDATE``/``EXDATE``)."""
    prop = component.get(name)
    if prop is None:
        return []
    entries = prop if isinstance(prop, list) else [prop]
    values: list[datetime | date] = []
    for entry in entries:
        for item in getattr(entry, "dts", []):
            if isinstance(item.dt, date):
                values.append(item.dt)
    return values


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown timezone %r for a calendar feed; reading it as UTC", name)
        return ZoneInfo("UTC")


def _event_zone(start: datetime | date, fallback: ZoneInfo) -> ZoneInfo:
    """The zone an event's wall-clock times are expressed in.

    A ``DATE`` value and a floating ``DATE-TIME`` both carry no zone; RFC 5545 says the
    reader supplies one, and the resource's own timezone is the reading an operator
    means. A zone-aware value keeps its own — including one icalendar built from the
    feed's ``VTIMEZONE``.
    """
    if isinstance(start, datetime) and start.tzinfo is not None:
        offset_zone = start.tzinfo
        if isinstance(offset_zone, ZoneInfo):
            return offset_zone
        # A fixed-offset tzinfo (a UTC "Z" value, or a VTIMEZONE-derived one): expansion
        # in it is exact for that offset, which is what the publisher asserted.
        return offset_zone  # type: ignore[return-value]
    return fallback


def _as_naive(value: datetime | date) -> datetime:
    """A comparable datetime for arithmetic between two values of the same kind."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(UTC)
    # Deliberately naive: a DATE has no zone, and this value is only ever subtracted from
    # another DATE to get a length. Attaching a zone here would invent one.
    return datetime(value.year, value.month, value.day)  # noqa: DTZ001


def _to_local_naive(value: datetime | date, zone: ZoneInfo) -> datetime:
    """A wall-clock datetime in ``zone``, without a tzinfo, for rule expansion."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(zone).replace(tzinfo=None)
    # Deliberately naive: rule expansion happens in wall-clock time and the caller
    # attaches ``zone`` to each occurrence afterwards (see :func:`_spans_for_event`).
    return datetime(value.year, value.month, value.day)  # noqa: DTZ001


def _clip(
    starts_at_utc: datetime,
    ends_at_utc: datetime,
    window_start_utc: datetime,
    window_end_utc: datetime,
    reason: str,
) -> FreeBusySpan | None:
    """The part of one occurrence that falls inside the window, or ``None``."""
    start = max(starts_at_utc, window_start_utc)
    end = min(ends_at_utc, window_end_utc)
    if end <= start:
        return None
    return FreeBusySpan(starts_at_utc=start, ends_at_utc=end, reason=reason)
