"""The exam countdown in the page header counts days in Berlin, like Today and the Plan, and says the exam was when
the day has passed. It used the browser's own day, and rounded the days after the exam to "0 weeks"."""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const APP = fs.readFileSync(PATH, 'utf8');
const A = APP.indexOf("function parseDay(");
const B = APP.indexOf("const chip = ");
const C = APP.indexOf("function examWhen(");
const D = APP.indexOf("// Studio, Plan and Play show every exam together");
if (A < 0 || B < A || C < 0 || D < C) throw new Error("the exam countdown code is not where it was");
const context = vm.createContext({ state: { me: { profile: { exam_dates: { "gh-300": EXAM } } } }, Intl, Date, Number, Object, String, Math, isNaN });
vm.runInContext(APP.slice(A, B) + "\n" + APP.slice(C, D) + "\nthis.out = { daysTo, examWhen };", context);
const at = (s) => new Date(s);
console.log(JSON.stringify(CASES.map(([now, exam]) => {
  context.state.me.profile.exam_dates["gh-300"] = exam;
  return { days: context.out.daysTo(exam, at(now)), text: (context.out.examWhen("gh-300", at(now)) || {}).text };
})));
"""


def run(cases: list[tuple[str, str]], tz: str = "America/Los_Angeles") -> list[dict]:
    """(now, exam day) -> days to go and the header text, in a browser whose own zone is `tz`."""
    script = f"const PATH = {json.dumps(str(APP_JS))}; const EXAM = {json.dumps(cases[0][1])}; const CASES = {json.dumps(cases)};" + HARNESS
    done = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60, env={**os.environ, "TZ": tz})
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "node is needed to run the page code")
class ExamCountdownTests(unittest.TestCase):
    def test_the_day_is_the_berlin_day_whatever_zone_the_browser_is_in(self):
        # 23:30 UTC on 14 Jan 2027 is 00:30 on the 15th in Berlin, and still the 14th (afternoon) in Los Angeles
        for tz in ("America/Los_Angeles", "UTC", "Europe/Berlin", "Asia/Tokyo"):
            got = run([("2027-01-14T23:30:00Z", "2027-01-18"), ("2027-01-14T22:59:00Z", "2027-01-18")], tz)
            self.assertEqual([g["days"] for g in got], [3, 4], tz)

    def test_in_summer_time_too(self):
        got = run([("2027-06-30T22:30:00Z", "2027-07-10"), ("2027-06-30T21:59:00Z", "2027-07-10")])
        self.assertEqual([g["days"] for g in got], [9, 10])

    def test_the_header_counts_weeks_and_the_exam_day_is_not_past(self):
        got = run([("2027-01-04T12:00:00Z", "2027-01-18"), ("2027-01-14T12:00:00Z", "2027-01-18"), ("2027-01-18T12:00:00Z", "2027-01-18"),
                   ("2027-01-17T12:00:00Z", "2027-01-18")])
        self.assertEqual([g["days"] for g in got], [14, 4, 0, 1])
        self.assertTrue(got[0]["text"].endswith("2 weeks"), got[0])
        self.assertTrue(got[1]["text"].endswith("1 week"), got[1])
        self.assertTrue(got[2]["text"].endswith("0 weeks") and got[2]["text"].startswith("exam "), got[2])
        self.assertTrue(got[3]["text"].endswith("0 weeks"), got[3])

    def test_after_the_exam_it_says_the_exam_was_not_zero_weeks(self):
        for now in ("2027-01-19T12:00:00Z", "2027-01-20T12:00:00Z", "2027-01-21T12:00:00Z", "2027-02-20T12:00:00Z"):
            got = run([(now, "2027-01-18")])[0]
            self.assertLess(got["days"], 0, now)
            self.assertTrue(got["text"].startswith("exam was "), (now, got))
            self.assertNotIn("week", got["text"], now)

    def test_a_date_that_is_not_a_day_has_no_count(self):
        got = run([("2027-01-14T12:00:00Z", "soon")])[0]
        self.assertIsNone(got["days"])


if __name__ == "__main__":
    unittest.main()
