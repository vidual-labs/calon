"""The HTTP edge.

Routes translate between HTTP and the service layer and do nothing else. No scheduling
logic lives here (``CLAUDE.md`` §4.2) — a route's whole job is to parse a request, hand it
to a service, and shape the answer back into JSON.
"""
