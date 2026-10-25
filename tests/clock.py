"""Days in the tests. Dojo counts calendar days in Berlin, and a calendar day is 23 or 25 hours long on the
two nights the clocks change."""
from datetime import datetime, timedelta

from app.core import utcnow
from app.plan import to_local, to_utc


def days_from_now(n: int) -> datetime:
    """The same Berlin wall-clock time n calendar days away (n may be negative), as a UTC moment.

    `utcnow() + timedelta(days=n)` is n x 24 hours: after a clock change that is an hour off, and in the
    hour before or after midnight an hour off is a different Berlin day, so "two days ago" became "one
    day ago" or "three" and the recalls were not due when the test expected them."""
    return to_utc(to_local(utcnow()) + timedelta(days=n))
