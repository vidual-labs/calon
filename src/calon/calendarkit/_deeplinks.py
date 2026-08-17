"""Build one-click add-to-calendar URLs for the three major providers.

Deeplinks are the convenience layer of the handoff (docs/calendar-handoff.md): the
"add to Google Calendar" / "add to Outlook" buttons a requester can click. They are built
with only the standard library, and they are deliberately **lossy** — a link carries no
``UID``, so clicking it cannot deduplicate against or update an entry the requester added
from the ICS file. The ``.ics`` file is the contract; these URLs are the shortcut.

The Google and Microsoft parameters are undocumented conveniences that the providers may
change without notice. That is tolerable for a convenience layer, and it is the reason
every URL here is covered by an exact-match unit test: a silent change in how a parameter
is encoded must fail the build, not quietly rewrite buttons that used to work.
"""

from __future__ import annotations

from urllib.parse import urlencode

from calon.calendarkit import CalendarEvent

#: ``20260901T060000Z/YYYYMMDDTHHMMSSZ`` — the shape Google Calendar's ``dates`` parameter
#: requires for timed events, and ``UTC`` the shape Microsoft's ``startdt``/``enddt``
#: accept.
_GOOGLE_INSTANT = "%Y%m%dT%H%M%SZ"
_MS_INSTANT = "%Y-%m-%dT%H:%M:%SZ"


def _google(event: CalendarEvent) -> str:
    """A Google Calendar "create event" template, one click to fill the form.

    ``action=TEMPLATE`` opens the compose form pre-filled rather than writing to a
    calendar directly — the provider's own documented way of deep-linking an event that
    does not already belong to an account.
    """
    params: dict[str, str] = {
        "action": "TEMPLATE",
        "text": event.title,
        "dates": (
            f"{event.start_utc.strftime(_GOOGLE_INSTANT)}/{event.end_utc.strftime(_GOOGLE_INSTANT)}"
        ),
        "details": event.description,
    }
    if event.location:
        params["location"] = event.location
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _outlook(event: CalendarEvent, host: str) -> str:
    """An Outlook web compose deep link, for one of the two Outlook hostnames.

    ``outlook.office.com`` serves work and school accounts; ``outlook.live.com`` serves
    personal Outlook.com accounts. Same path, same parameters, different host — the
    provider documents the two hosts separately, so we keep them separate.
    """
    params: dict[str, str] = {
        "path": "/calendar/action/compose",
        "rru": "addevent",
        "subject": event.title,
        "startdt": event.start_utc.strftime(_MS_INSTANT),
        "enddt": event.end_utc.strftime(_MS_INSTANT),
        "body": event.description,
    }
    if event.location:
        params["location"] = event.location
    return f"https://{host}/calendar/0/deeplink/compose?" + urlencode(params)


def render_all(event: CalendarEvent) -> dict[str, str]:
    """The three provider links, keyed by provider.

    The keys are the public names callers branch on — ``google``, ``outlook_office``, and
    ``outlook_live`` — so they are stable just like decision codes.
    """
    return {
        "google": _google(event),
        "outlook_office": _outlook(event, host="outlook.office.com"),
        "outlook_live": _outlook(event, host="outlook.live.com"),
    }
