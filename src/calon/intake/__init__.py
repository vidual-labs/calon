"""Source adapters.

An adapter's entire job is to turn some source's payload into a
:class:`calon.schemas.BookingIntentIn` and stop. It does not read availability rules, does
not check conflicts, and does not produce a decision (``CLAUDE.md`` §4.3). Anything it
cannot map goes into ``metadata`` untouched.

calon's own intake lives here too, as one adapter among the others rather than as a
shortcut past them.
"""
