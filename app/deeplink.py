"""Links Dojo hands out to be opened later: /?go=<route> (ADR 0004 and ADR 0013, amended 5 Oct 2026).

Easy Auth loses the part of an address after the "#" when it sends a visitor to sign in, and keeps the path and
the query. A link in a calendar event, a nudge or a podcast episode's notes is often opened signed out, so it is
/?go=lesson/<id>/listen and not /#/lesson/<id>/listen: the app turns the query into the page route when it
starts (app.js, openDeepLink). Dojo's own pages still link with # routes.

One rule says what a route may be, and app.js (GO_ROUTE) and sw.js (ROUTE) say the same: 1 to 6 parts split by
"/", each of lowercase letters and digits that may be joined by one "." "_" or "-", and at most 120 characters.
There is no "%", no backslash, no empty part and no part of dots only, so a route cannot be an address, a path out
of the app or a script, and nothing is decoded twice. tests/test_deeplink.py keeps the three copies the same.
"""
from __future__ import annotations

import re

MAX_LENGTH = 120
SEGMENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
SOURCE = rf"{SEGMENT}(?:/{SEGMENT}){{0,5}}"
ROUTE = re.compile(SOURCE)


def route_ok(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_LENGTH and ROUTE.fullmatch(value) is not None


def go_link(route: str, base: str = "") -> str:
    """The link to a page, to be opened later: `base` + "/?go=" + route. `base` is the site's address for a
    link that leaves Dojo (a calendar event, an episode's notes) and empty for one the app itself opens (a nudge).
    A route that fails the rule gives the start page: a link that opens Dojo beats a feed or a nudge that fails."""
    return f"{base}/?go={route}" if route_ok(route) else f"{base}/"
