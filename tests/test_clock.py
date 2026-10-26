"""days_from_now keeps the Berlin wall-clock time, and so the Berlin day, across the two clock changes."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.plan import exists, to_local
from tests.clock import days_from_now

UTC = timezone.utc


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class DaysFromNowTests(unittest.TestCase):
    def at(self, now: datetime):
        return mock.patch("tests.clock.utcnow", lambda: now)

    def test_autumn_the_clocks_went_back_on_25_oct_2026(self):
        with self.at(utc(2026, 10, 26, 22, 30)):        # 23:30 in Berlin
            self.assertEqual(days_from_now(-2), utc(2026, 10, 24, 21, 30))     # 23:30 on the 24th, summer time
            self.assertEqual(days_from_now(-3), utc(2026, 10, 23, 21, 30))
            self.assertEqual(days_from_now(1), utc(2026, 10, 27, 22, 30))
            self.assertNotEqual(days_from_now(-2), utc(2026, 10, 24, 22, 30))   # 48 hours back is 00:30 on the 25th

    def test_spring_the_clocks_went_forward_on_28_mar_2027(self):
        with self.at(utc(2027, 3, 29, 22, 30)):         # 00:30 on the 30th in Berlin
            self.assertEqual(days_from_now(-2), utc(2027, 3, 27, 23, 30))      # 00:30 on the 28th, still winter time
            self.assertNotEqual(days_from_now(-2), utc(2027, 3, 27, 22, 30))   # 48 hours back is 23:30 on the 27th
        with self.at(utc(2027, 3, 27, 22, 30)):         # 23:30 on the 27th in Berlin
            self.assertEqual(days_from_now(1), utc(2027, 3, 28, 21, 30))       # 23:30 on the 28th, summer time

    def test_the_berlin_day_moves_by_exactly_n_at_every_hour_around_both_changes(self):
        wrong_the_plain_way = 0
        for start in (utc(2026, 10, 20, 0, 30), utc(2027, 3, 24, 0, 30)):
            for hour in range(14 * 24):
                now = start + timedelta(hours=hour)
                here = to_local(now)
                with self.at(now):
                    for n in (-3, -2, -1, 1, 2, 3):
                        got = to_local(days_from_now(n))
                        self.assertEqual(got.date(), here.date() + timedelta(days=n), (now, n))
                        if exists(here + timedelta(days=n)):
                            self.assertEqual(got.time(), here.time(), (now, n))
                        if to_local(now + timedelta(days=n)).date() != here.date() + timedelta(days=n):
                            wrong_the_plain_way += 1
        self.assertGreater(wrong_the_plain_way, 0, "plain 24-hour steps are now right: this helper is not needed")


if __name__ == "__main__":
    unittest.main()
