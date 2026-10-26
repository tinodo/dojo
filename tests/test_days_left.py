"""The days to the exam are Berlin calendar days, the same count the Plan makes. In the hour or two after
Berlin midnight the UTC date is still yesterday, and Today said one day too many."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-days-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

PID = "gh-300"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class DaysLeftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings(tempfile.mkdtemp(prefix="dojo-days-")), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)

    def days_left(self, exam: str, now: datetime) -> int:
        r = self.c.put("/api/settings", json={"exam_dates": {PID: exam}}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        with mock.patch("app.learning.utcnow", lambda: now):
            today = self.c.get(f"/api/today/{PID}").json()
        return today["plan"]["days_left"]

    def test_just_after_midnight_in_winter_the_new_day_has_begun(self):
        # 00:30 on 15 Jan 2027 in Berlin is still 14 Jan in UTC
        self.assertEqual(self.days_left("2027-02-15", utc(2027, 1, 14, 23, 30)), 31)
        self.assertEqual(self.days_left("2027-02-15", utc(2027, 1, 14, 23, 0)), 31)

    def test_just_before_midnight_the_old_day_has_not_ended(self):
        self.assertEqual(self.days_left("2027-02-15", utc(2027, 1, 14, 22, 59)), 32)
        self.assertEqual(self.days_left("2027-02-15", utc(2027, 1, 15, 22, 30)), 31)

    def test_just_after_midnight_in_summer_time(self):
        # 00:30 on 1 Jul 2027 in Berlin (UTC+2) is still 30 Jun in UTC, and so is 01:59
        self.assertEqual(self.days_left("2027-07-10", utc(2027, 6, 30, 22, 30)), 9)
        self.assertEqual(self.days_left("2027-07-10", utc(2027, 6, 30, 21, 59)), 10)

    def test_through_the_day_the_count_does_not_move(self):
        for hour in (0, 6, 12, 18, 21):
            self.assertEqual(self.days_left("2027-02-15", utc(2027, 1, 15, hour)), 31, hour)

    def test_the_exam_day_itself_is_zero_days_to_go(self):
        self.assertEqual(self.days_left("2027-02-15", utc(2027, 2, 14, 23, 30)), 0)


if __name__ == "__main__":
    unittest.main()
