"""Know what you know (ADR 0011): confidence after answering, the calibration view and successive
relearning. Confidence never changes a mark or a pip; recalls come back on expanding days, a miss brings
them back the next day, and nothing of one learner reaches another."""
import contextvars
import copy
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-know-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import know, relearn  # noqa: E402
from app.core import APP_DIR, Abandoned, Learner, learner_key, utcnow  # noqa: E402
from app.evidence import derive  # noqa: E402
from app.main import ExamAnswer, create_app  # noqa: E402
from app.packages import Packages  # noqa: E402
from app.plan import Planner, _matches  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402
from tests.test_team import classes  # noqa: E402

D0 = date(2026, 3, 2)   # a Monday in winter time: noon UTC is 13:00 in Berlin, the same calendar day


def at(day: date, hour: int = 12) -> str:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc).isoformat(timespec="seconds")


def attempt(day: date, ok: bool = True, mode: str = "check", unaided: bool | None = None, disputed: bool = False,
            item: str = "", total: int = 2, hour: int = 12) -> dict:
    met = total if ok else 0
    return {"item": item or f"item-{day.isoformat()}-{hour}-{mode}", "at": at(day, hour), "mode": mode, "met": met,
            "total": total, "all_met": ok and total > 0, "unaided": mode in ("probe", "check") if unaided is None else unaided,
            "disputed": disputed, "minutes_since_teaching": None}


def ev(attempts: list[dict], unaided: bool = False, later: bool = False, taught: str | None = None) -> dict:
    return {"checks": {"unaided": unaided, "later": later}, "last_teaching": taught, "attempts": attempts}


def day(n: int) -> date:
    return D0 + timedelta(days=n)


class ScheduleTests(unittest.TestCase):
    """The ladder: 1, 3, 7, 16 and 35 days; a miss resets it; relearned after three on-time days."""

    def test_first_success_is_due_the_next_day(self):
        s = relearn.schedule([attempt(day(0))])
        self.assertEqual((s["step"], s["due"], s["relearned"]), (0, day(1), False))
        self.assertEqual(s["started"], at(day(0)))

    def test_nothing_is_scheduled_before_a_first_success(self):
        s = relearn.schedule([attempt(day(0), ok=False), attempt(day(1), ok=False, mode="practice")])
        self.assertEqual((s["step"], s["due"], s["repair"]), (None, None, None))

    def test_help_or_a_dispute_is_not_a_success(self):
        helped = attempt(day(0), mode="practice", unaided=False)
        disputed = attempt(day(0), disputed=True)
        self.assertIsNone(relearn.schedule([helped, disputed])["step"])

    def test_on_time_successes_climb_the_ladder(self):
        days, d, got = [], day(0), []
        for gap in (0, 1, 3, 7, 16, 35, 35):
            d = d + timedelta(days=gap)
            days.append(attempt(d))
            got.append(relearn.schedule(days)["due"] - d)
        self.assertEqual([g.days for g in got], [1, 3, 7, 16, 35, 35, 35])
        self.assertEqual(relearn.INTERVALS, (1, 3, 7, 16, 35))

    def test_an_early_success_changes_nothing(self):
        base = [attempt(day(0)), attempt(day(1))]          # due on day 4 now
        before = relearn.schedule(base)
        after = relearn.schedule(base + [attempt(day(2)), attempt(day(3), hour=8)])
        self.assertEqual((after["step"], after["due"], after["on_time"]), (before["step"], before["due"], before["on_time"]))
        self.assertEqual(after["last_success"], at(day(3), 8))

    def test_late_is_simply_due_and_counts_from_the_day_it_is_done(self):
        s = relearn.schedule([attempt(day(0)), attempt(day(20))])
        self.assertEqual((s["step"], s["due"]), (1, day(23)))

    def test_a_miss_resets_to_the_next_day_and_points_to_repair(self):
        good = [attempt(day(0)), attempt(day(1)), attempt(day(4))]
        miss = attempt(day(11), ok=False, item="the-miss")
        s = relearn.schedule(good + [miss])
        self.assertEqual((s["step"], s["due"], s["on_time"], s["repair"]), (0, day(12), [], "the-miss"))
        healed = relearn.schedule(good + [miss, attempt(day(12))])
        self.assertEqual((healed["step"], healed["due"], healed["repair"]), (1, day(15), None))

    def test_a_disputed_or_practice_miss_still_brings_it_back(self):
        """A missing or contested result is never read in the learner's favour."""
        for miss in (attempt(day(3), ok=False, disputed=True), attempt(day(3), ok=False, mode="practice")):
            s = relearn.schedule([attempt(day(0)), miss])
            self.assertEqual((s["step"], s["due"]), (0, day(4)))

    def test_an_answer_with_nothing_to_judge_is_neither(self):
        s = relearn.schedule([attempt(day(0)), attempt(day(1), ok=False, total=0)])
        self.assertEqual((s["step"], s["due"]), (0, day(1)))

    def test_relearned_after_three_on_time_days_and_the_milestone_stays(self):
        a = [attempt(day(0)), attempt(day(1)), attempt(day(4))]
        self.assertFalse(relearn.schedule(a)["relearned"])
        a.append(attempt(day(11)))
        s = relearn.schedule(a)
        self.assertTrue(s["relearned"])
        self.assertEqual((len(s["on_time"]), s["relearned_at"]), (3, at(day(11))))
        a.append(attempt(day(15), ok=False))
        s = relearn.schedule(a)
        self.assertFalse(s["relearned"])
        self.assertEqual(s["relearned_at"], at(day(11)))

    def test_two_successes_on_one_day_are_one_day(self):
        a = [attempt(day(0)), attempt(day(1)), attempt(day(4)), attempt(day(4), hour=15)]
        self.assertEqual(len(relearn.schedule(a)["on_time"]), 2)

    def test_days_are_berlin_calendar_days(self):
        self.assertEqual(relearn.local_day("2026-03-10T23:30:00+00:00"), date(2026, 3, 11))   # 00:30 CET
        self.assertEqual(relearn.local_day("2026-07-10T21:59:00+00:00"), date(2026, 7, 10))   # 23:59 CEST
        self.assertEqual(relearn.local_day("2026-07-10T22:01:00+00:00"), date(2026, 7, 11))


class DueTests(unittest.TestCase):
    """Which skills are due, in which order, and how many fit a day."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = Packages(APP_DIR / "packages").get("gh-300")
        sk = cls.pkg["skills"]
        cls.ids = sorted((s for s in sk if sk[s]["sources"]), key=lambda s: (-sk[s]["share"], s))

    def test_oldest_due_first_then_heaviest(self):
        heavy, light, older = self.ids[0], self.ids[-1], self.ids[1]
        evidence = {heavy: ev([attempt(day(5))], unaided=True, later=True),
                    light: ev([attempt(day(5))], unaided=True, later=True),
                    older: ev([attempt(day(1))], unaided=True, later=True)}
        due = relearn.due_recalls(self.pkg, evidence, day(10))
        self.assertEqual([r["skill"] for r in due], [older, heavy, light])
        self.assertEqual(relearn.due_recalls(self.pkg, evidence, day(1)), [])
        self.assertEqual([r["due"] for r in due], [day(2).isoformat(), day(6).isoformat(), day(6).isoformat()])

    def test_gone_pages_no_pages_and_waiting_for_later_are_left_out(self):
        a, b, c = self.ids[0], self.ids[1], self.ids[2]
        pkg = copy.deepcopy(self.pkg)
        pkg["skills"][c]["sources"] = []
        evidence = {a: ev([attempt(day(0))], unaided=True, later=False, taught=at(day(0), 9)),
                    b: ev([attempt(day(0))], unaided=True, later=True),
                    c: ev([attempt(day(0))], unaided=True, later=True)}
        self.assertEqual(relearn.due_recalls(pkg, evidence, day(3), gone={b}), [])
        # Never taught in Dojo: there is no later check to wait for, so it goes on the ladder.
        evidence[a]["last_teaching"] = None
        self.assertEqual([r["skill"] for r in relearn.due_recalls(pkg, evidence, day(3), gone={b})], [a])

    def test_the_daily_cap_counts_recall_answers_of_that_day_only(self):
        def sent(when: str, recall: bool) -> dict:
            return {"type": "answer.submitted", "at": when, "data": {"recall": recall}}
        events = [sent(at(day(0), 9), True), sent(at(day(0), 10), True), sent(at(day(0), 11), False),
                  sent(at(day(-1), 12), True), {"type": "answer.judged", "at": at(day(0)), "data": {"recall": True}}]
        self.assertEqual(relearn.done_on(events, day(0)), 2)
        self.assertEqual(relearn.left_today(events, day(0)), relearn.DAY_CAP - 2)
        self.assertEqual(relearn.left_today(events * 3, day(0)), 0)
        self.assertEqual((relearn.DAY_CAP, relearn.DAY_MINUTES), (5, 10))

    def test_the_daily_budget_counts_questions_given_to_checks_not_only_answers(self):
        # Review 1: a check ended unanswered must not free its recall places, in any exam.
        def q(state: str, picked: str = "recall", job: str | None = None, item: str | None = None) -> dict:
            return {"picked": picked, "state": state, "job": job, "item": item}
        today = {"id": "c1", "package": "gh-300", "created": at(day(0), 8), "state": "ended",
                 "questions": [q("ready", job="j1", item="it-1"), q("skipped", job="j2"), q("skipped"),
                               q("ready", picked="untested", job="j3", item="it-3")]}
        other = {"id": "c2", "package": "dp-800", "created": at(day(0), 9), "state": "running",
                 "questions": [q("writing", job="j4")]}
        old = {"id": "c0", "package": "gh-300", "created": at(day(-1), 9), "state": "ended",
               "questions": [q("ready", job="j0", item="it-0")]}
        sent = [{"type": "answer.submitted", "at": at(day(0), 10), "data": {"recall": True, "item": "it-1"}},
                {"type": "answer.submitted", "at": at(day(0), 10), "data": {"recall": True, "item": "it-0"}}]
        # it-1 and the failed write (j2) in gh-300, the one being written in dp-800, and yesterday's it-0
        # answered today. The question that ended before any writer had it is free again.
        self.assertEqual(relearn.used_on(sent, [today, other, old], day(0)), 4)
        self.assertEqual(relearn.left_today(sent, day(0), [today, other, old]), 1)
        self.assertEqual(relearn.left_today([], day(1), [today, other, old]), relearn.DAY_CAP)
        open_now = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)
        self.assertEqual(relearn.waiting_in_checks([], [today, other], "dp-800", open_now, 2), 1)
        self.assertEqual(relearn.waiting_in_checks([], [today, other], "gh-300", open_now, 2), 0)   # ended

    def test_a_backlog_spreads_over_short_days(self):
        backlog = [(day(-30 + i), f"s{i}") for i in range(12)]
        out = relearn.spread(backlog, day(0), 7, cap_today=3)
        self.assertEqual([len(out[d]) for d in sorted(out)], [3, 5, 4])
        self.assertEqual(out[day(0)], ["s0", "s1", "s2"])     # oldest first
        later = relearn.spread([(day(4), "x")], day(0), 7, cap_today=5)
        self.assertEqual(list(later), [day(4)])
        self.assertEqual(relearn.spread([(day(0), "x")], day(0), 3, cap_today=0), {day(1): ["x"]})

    def test_the_log_line_has_what_a_later_scheduler_needs(self):
        before = [attempt(day(0)), attempt(day(1))]
        now = attempt(day(4), item="it-9")
        line = relearn.log_line("key-a", before, now, "fair", True, "gh-300", self.ids[0])
        for field in ("at", "learner", "skill", "item", "result", "confidence", "days_since_previous"):
            self.assertIn(field, line)
        self.assertEqual((line["learner"], line["item"], line["result"], line["confidence"]), ("key-a", "it-9", "met", "fair"))
        self.assertEqual((line["days_since_previous"], line["step_before"], line["step_after"]), (3.0, 1, 2))
        first = relearn.log_line("key-a", [], attempt(day(0), ok=False), None, False, "gh-300", self.ids[0])
        self.assertEqual((first["result"], first["days_since_previous"], first["due_before"]), ("missed", None, None))


class CalibrationTests(unittest.TestCase):
    """How sure you were next to how often you were right, never shown on too few answers."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = Packages(APP_DIR / "packages").get("gh-300")
        cls.ids = sorted(s for s in cls.pkg["skills"] if cls.pkg["skills"][s]["sources"])

    def row(self, sid: str, confidence: str, right: bool, n: int) -> dict:
        return {"skill": sid, "at": at(day(n // 20), n % 20),
                "kind": "open", "confidence": confidence, "right": right, "item": f"i{n}", "mode": "check"}

    def test_too_few_answers_show_no_rate(self):
        rows = [self.row(self.ids[0], "certain", True, i) for i in range(know.MIN_ANSWERS - 1)]
        t = know.table(rows)
        self.assertEqual((t["certain"]["total"], t["certain"]["right"], t["certain"]["enough"], t["certain"]["rate"]),
                         (4, 4, False, None))
        rows.append(self.row(self.ids[0], "certain", False, 9))
        t = know.table(rows)
        self.assertEqual((t["certain"]["enough"], t["certain"]["rate"]), (True, 80))
        self.assertEqual((t["guess"]["total"], t["guess"]["rate"]), (0, None))

    def test_only_answers_with_a_confidence_count_and_disputes_count_neither_way(self):
        pid, a, b = "gh-300", self.ids[0], self.ids[1]

        def sent(item: str, sid: str, **extra) -> dict:
            return {"type": "answer.submitted", "at": at(day(0)), "data": {"package": pid, "item": item, "skill": sid, **extra}}
        events = [sent("i1", a, confidence="certain"), sent("i2", a, confidence=None), sent("i3", a),
                  sent("i4", a, confidence="fair"), sent("i5", a, confidence="guess"),
                  {"type": "mcq.answered", "at": at(day(1)), "data": {"package": pid, "skill": b, "confidence": "fair",
                                                                     "correct": False, "rehearsal": "r1", "index": 2}},
                  {"type": "mcq.answered", "at": at(day(1)), "data": {"package": pid, "skill": b, "correct": True}},
                  {"type": "mcq.answered", "at": at(day(1)), "data": {"package": "dp-800", "skill": b, "confidence": "fair"}}]
        evidence = {a: ev([attempt(day(0), item="i1"), attempt(day(0), item="i2"), attempt(day(0), item="i3"),
                           attempt(day(0), item="i4", disputed=True), attempt(day(0), item="i5", total=0)])}
        rows = know.answers(events, self.pkg, evidence)
        self.assertEqual([(r["kind"], r.get("item"), r["confidence"], r["right"]) for r in rows],
                         [("open", "i1", "certain", True), ("mcq", None, "fair", False)])

    def test_confident_errors_certain_first_until_put_right(self):
        a, b, c = self.ids[0], self.ids[1], self.ids[2]
        rows = [self.row(a, "fair", False, 1), self.row(b, "certain", False, 2), self.row(c, "guess", False, 3),
                self.row(a, "fair", False, 4), self.row(b, "certain", False, 5)]
        errs = know.confident_errors(rows)
        self.assertEqual([(e["skill"], e["item"]) for e in errs], [(b, "i5"), (b, "i2"), (a, "i4"), (a, "i1")])
        rows.append(self.row(b, "guess", True, 6))      # right later, even on a guess: it drops off
        self.assertEqual([e["skill"] for e in know.confident_errors(rows)], [a, a])
        rows.append(self.row(a, "certain", False, 7))
        self.assertEqual([e["item"] for e in know.confident_errors(rows)], ["i7", "i4", "i1"])
        many = [self.row(a, "certain", False, i) for i in range(30)]
        self.assertEqual(len(know.confident_errors(many)), know.MAX_ERRORS)

    def test_a_right_answer_with_confidence_skipped_still_clears_a_confident_error(self):
        # Review 3: skipping the question keeps an answer out of the rates, never keeps an error listed.
        pid, a, b, c = "gh-300", self.ids[0], self.ids[1], self.ids[2]
        events = [{"type": "answer.submitted", "at": at(day(0)), "data": {"package": pid, "item": "i1", "skill": a,
                                                                         "confidence": "certain"}},
                  {"type": "answer.submitted", "at": at(day(1)), "data": {"package": pid, "item": "i2", "skill": a}},
                  {"type": "answer.submitted", "at": at(day(0)), "data": {"package": pid, "item": "i3", "skill": b,
                                                                         "confidence": "certain"}},
                  {"type": "mcq.answered", "at": at(day(2)), "data": {"package": pid, "skill": b, "correct": True}},
                  {"type": "answer.submitted", "at": at(day(0)), "data": {"package": pid, "item": "i4", "skill": c,
                                                                         "confidence": "fair"}},
                  {"type": "answer.submitted", "at": at(day(1)), "data": {"package": pid, "item": "i5", "skill": c}}]
        evidence = {a: ev([attempt(day(0), ok=False, item="i1"), attempt(day(1), item="i2")]),
                    b: ev([attempt(day(0), ok=False, item="i3")]),
                    c: ev([attempt(day(0), ok=False, item="i4"), attempt(day(1), item="i5", disputed=True)])}
        rows = know.answers(events, self.pkg, evidence)
        self.assertEqual(sorted(r["item"] for r in rows), ["i1", "i3", "i4"])   # the rates leave i2 and i5 out
        self.assertEqual(len(know.confident_errors(rows)), 3)
        right = know.last_right(events, self.pkg, evidence)
        self.assertEqual(set(right), {a, b})            # a disputed right answer counts neither way
        self.assertEqual([e["item"] for e in know.confident_errors(rows, right)], ["i4"])

    def test_two_errors_in_the_same_second_go_newest_first_by_their_place_in_the_record(self):
        # Record times are to the second: a quick question and a judged answer can share one.
        pid, a, b = "gh-300", self.ids[0], self.ids[1]
        miss = attempt(day(0), ok=False, item="i1")
        same = miss["at"]
        events = [{"type": "mcq.answered", "at": same, "data": {"package": pid, "skill": b, "confidence": "certain",
                                                                "correct": False, "rehearsal": "r1", "index": 0}},
                  {"type": "answer.submitted", "at": same, "data": {"package": pid, "item": "i1", "skill": a,
                                                                    "confidence": "certain"}},
                  {"type": "answer.judged", "at": same, "data": {"package": pid, "item": "i1", "skill": a}}]
        rows = know.answers(events, self.pkg, {a: ev([miss])})
        self.assertEqual([r["kind"] for r in rows], ["mcq", "open"])
        self.assertEqual([e["kind"] for e in know.confident_errors(rows)], ["open", "mcq"])

    def test_a_right_answer_in_the_same_second_after_an_error_clears_it(self):
        # Fix check of review 3: "later" is (time, place in the Record), for clearing as for sorting.
        pid, a, b = "gh-300", self.ids[0], self.ids[1]
        miss_a, right_a = attempt(day(0), ok=False, item="i1"), attempt(day(0), item="i2")
        miss_b = attempt(day(0), ok=False, item="i3")
        same = miss_a["at"]

        def sent(item: str, sid: str, **extra) -> dict:
            return {"type": "answer.submitted", "at": same, "data": {"package": pid, "item": item, "skill": sid, **extra}}

        def judged(item: str, sid: str) -> dict:
            return {"type": "answer.judged", "at": same, "data": {"package": pid, "item": item, "skill": sid}}
        events = [sent("i1", a, confidence="certain"), judged("i1", a),
                  sent("i2", a), judged("i2", a),                          # right, confidence skipped
                  {"type": "mcq.answered", "at": same, "data": {"package": pid, "skill": b, "correct": True}},
                  sent("i3", b, confidence="certain"), judged("i3", b)]    # wrong after the right one
        evidence = {a: ev([miss_a, right_a]), b: ev([miss_b])}
        rows = know.answers(events, self.pkg, evidence)
        right = know.last_right(events, self.pkg, evidence)
        self.assertEqual([e["item"] for e in know.confident_errors(rows)], ["i3", "i1"])
        self.assertEqual([e["item"] for e in know.confident_errors(rows, right)], ["i3"])
        rated = [dict(r, right=True, item="i9", seq=r["seq"] + 1) for r in rows if r["item"] == "i1"]
        self.assertEqual([e["item"] for e in know.confident_errors(rows + rated)], ["i3"])

    def test_per_domain_and_skill(self):
        dom = self.pkg["domains"][0]
        sid = dom["groups"][0]["skills"][0]
        rows = [self.row(sid, "certain", i % 2 == 0, i) for i in range(6)]
        out = know.calibration(self.pkg, rows, [{"confidence": "guess", "matched": "yes"},
                                                {"confidence": None, "matched": "no"}])
        first = next(d for d in out["domains"] if d["id"] == dom["id"])
        self.assertEqual((first["answers"], first["levels"]["certain"]["rate"]), (6, 50))
        self.assertTrue(all(d["answers"] == 0 for d in out["domains"] if d["id"] != dom["id"]))
        self.assertEqual(out["skills"][sid]["levels"]["certain"]["total"], 6)
        self.assertEqual((out["selfchecks"]["answers"], out["selfchecks"]["levels"]["guess"]["right"]), (1, 1))
        self.assertEqual(out["min_answers"], know.MIN_ANSWERS)

    def test_the_section_that_teaches_the_missed_point(self):
        lesson = {"sections": [{"heading": "One", "points": [{"text": "Tokens expire after an hour", "quote": ""}]},
                               {"heading": "Two", "points": [{"text": "Policies apply per repository",
                                                              "quote": "content exclusion policies repository"}]}]}
        self.assertEqual(know.best_section(lesson, ["Exclusion policies apply at repository level"]), 1)
        self.assertIsNone(know.best_section(lesson, ["zzz"]))
        self.assertIsNone(know.best_section(lesson, []))


class NeverEvidenceTests(unittest.TestCase):
    """Confidence is a mirror: no pip, no mark, no readiness and no timed exam ever reads it."""

    def test_the_evidence_and_readiness_code_never_reads_confidence(self):
        for name in ("evidence.py", "readiness.py", "exam.py"):
            self.assertNotIn("confidence", (APP_DIR / name).read_text(encoding="utf-8"), name)
        self.assertNotIn("confidence", ExamAnswer.model_fields)


class BerlinDayTests(unittest.TestCase):
    """Review 4: whether a recall is due today is decided in the schedule's own day, not the browser's."""

    def test_the_server_says_whether_the_next_recall_is_today(self):
        from app.learning import Dojo
        e = ev([attempt(day(0))])                               # due on day(1)
        late = datetime(2026, 3, 2, 23, 30, tzinfo=timezone.utc)   # 00:30 on day(1) in Berlin, still day(0) in UTC
        with mock.patch("app.learning.utcnow", lambda: late):
            s = Dojo.relearn_status("gh-300", e)
        self.assertEqual((s["due"], s["today"], s["due_today"]), (day(1).isoformat(), day(1).isoformat(), True))
        with mock.patch("app.learning.utcnow", lambda: late - timedelta(hours=1)):
            self.assertFalse(Dojo.relearn_status("gh-300", e)["due_today"])
        js = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("r.due_today", js)
        self.assertNotIn('toLocaleDateString("en-CA")', js)


class CheckPollTests(unittest.TestCase):
    """Review 6: one network glitch while questions are written must not leave the learner stuck."""

    def test_the_check_screen_tries_again_and_then_says_it_lost_touch(self):
        js = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
        run = js[js.index("async function viewCheckRun("):]
        run = run[:run.index("\n}\n")]
        self.assertNotIn("catch (e) { return; }", run)
        self.assertIn("const CHECK_POLL_TRIES = ", js)
        self.assertIn("if (++fails < CHECK_POLL_TRIES) continue;", run)
        self.assertIn("fails = 0;", run)                       # a good look starts the count again
        self.assertIn("Dojo cannot reach the server just now", run)
        self.assertIn('button("Try again", () => { lost = false; drawWaiting(); poll(); }', run)


class PlanRecallTests(unittest.TestCase):
    """The Plan gets one short Recall session per exam and day, sized by what is due."""

    def test_recall_sessions_are_capped_spread_and_before_the_exam(self):
        today = day(0)
        recalls = [(day(-3 + i % 3), 0.0, ("gh-300", f"s{i}", day(2))) for i in range(12)] + \
                  [(day(1), 0.0, ("dp-800", "x", None))]
        needs = Planner._recall_needs(recalls, [], today)
        got = [(n.key.split(":")[1], n.key.split(":")[2], n.alts[0].minutes) for n in needs]
        # Five a day across both exams, oldest first; none on or after the gh-300 exam day; two minutes a
        # recall, exactly as Today says it.
        self.assertEqual(got, [("gh-300", day(0).isoformat(), 10), ("gh-300", day(1).isoformat(), 10),
                               ("dp-800", day(2).isoformat(), 2)])
        sent = [{"type": "answer.submitted", "at": at(today, 8), "data": {"recall": True}}] * 4
        first = Planner._recall_needs(recalls, sent, today)[0]
        self.assertEqual((first.key, first.alts[0].minutes), (f"recall:gh-300:{today.isoformat()}", 2))
        self.assertTrue(all(n.alts[0].kind == "recall" for n in needs))

    def test_same_day_recalls_go_heaviest_first_in_the_plan_as_on_today(self):
        # Review 5: the blueprint weight is kept. Seven due today in two exams: the five heaviest go today,
        # the two lightest tomorrow, whatever order they came in.
        weights = {"a": 0.02, "b": 0.09, "c": 0.01, "d": 0.05, "e": 0.07, "f": 0.03, "g": 0.08}
        recalls = [(day(0), -w, ("gh-300" if s in "abcd" else "dp-800", s, None)) for s, w in weights.items()]
        recalls.append((day(-1), -0.001, ("dp-800", "old", None)))   # older still comes first, however light
        needs = Planner._recall_needs(recalls, [], day(0))
        got = {(n.key.split(":")[1], n.key.split(":")[2]): n.alts[0].minutes for n in needs}
        # Today: old, then b .09, g .08, e .07, d .05. Tomorrow: f .03 (dp-800), a .02 and c .01 (gh-300).
        self.assertEqual(got, {("dp-800", day(0).isoformat()): relearn.minutes(3), ("gh-300", day(0).isoformat()): relearn.minutes(2),
                               ("dp-800", day(1).isoformat()): relearn.minutes(1), ("gh-300", day(1).isoformat()): relearn.minutes(2)})
        pkg = Packages(APP_DIR / "packages").get("gh-300")
        ids = sorted(s for s in pkg["skills"] if pkg["skills"][s]["sources"])
        by_share = sorted(ids, key=lambda s: pkg["skills"][s]["share"])
        light, heavy = by_share[0], by_share[-1]
        self.assertLess(pkg["skills"][light]["share"], pkg["skills"][heavy]["share"])
        evidence = {light: ev([attempt(day(-1))]), heavy: ev([attempt(day(-1))])}
        self.assertEqual([x[2] for x in relearn.upcoming(pkg, evidence)], [heavy, light])

    def test_the_plan_and_today_say_the_same_minutes(self):
        three = [(day(0), 0.0, ("gh-300", f"s{i}", None)) for i in range(3)]
        need = Planner._recall_needs(three, [], day(0))[0]
        self.assertEqual(need.alts[0].minutes, relearn.minutes(3))
        self.assertEqual(relearn.minutes(3), 6)

    def test_recalls_that_cannot_come_before_their_exam_do_not_take_another_exams_places(self):
        # Review 2: gh-300's exam is tomorrow, so of its twelve old recalls only five fit, today. The rest
        # must not fill tomorrow and push dp-800's recall to the day after.
        recalls = [(day(-5), 0.0, ("gh-300", f"s{i:02}", day(1))) for i in range(12)] + [(day(1), 0.0, ("dp-800", "x", None))]
        got = [(n.key.split(":")[1], n.key.split(":")[2]) for n in Planner._recall_needs(recalls, [], day(0))]
        self.assertEqual(got, [("gh-300", day(0).isoformat()), ("dp-800", day(1).isoformat())])
        out = relearn.spread([(day(0), ("a", day(0))), (day(0), ("b", None))], day(0), 3, 1, before=lambda x: x[1])
        self.assertEqual(out, {day(0): [("b", None)]})

    def test_the_plan_counts_the_questions_todays_checks_already_took(self):
        five = [(day(0), 0.0, ("gh-300", f"s{i}", None)) for i in range(5)]
        session = {"package": "gh-300", "created": at(day(0), 8), "state": "ended",
                   "questions": [{"picked": "recall", "state": "skipped", "job": f"j{i}", "item": None} for i in range(4)]}
        needs = Planner._recall_needs(five, [], day(0), [session])
        self.assertEqual([(n.key.split(":")[2], n.alts[0].minutes) for n in needs],
                         [(day(0).isoformat(), 2), (day(1).isoformat(), 8)])

    def test_a_recall_session_is_done_only_by_that_days_recall_answers(self):
        s = {"kind": "recall", "package": "gh-300", "start": at(day(0), 8)}
        def sent(when: str, recall: bool) -> dict:
            return {"type": "answer.submitted", "at": when, "data": {"package": "gh-300", "recall": recall, "mode": "check"}}
        self.assertTrue(_matches(s, sent(at(day(0), 15), True), set()))
        self.assertFalse(_matches(s, sent(at(day(1), 9), True), set()))
        self.assertFalse(_matches(s, sent(at(day(0), 15), False), set()))
        check3 = {"kind": "check3", "package": "gh-300", "start": at(day(0), 8)}
        self.assertFalse(_matches(check3, sent(at(day(0), 15), True), set()))
        self.assertTrue(_matches(check3, sent(at(day(0), 15), False), set()))


class KnowApiTests(unittest.TestCase):
    """The whole loop against the local stub, and two learners who never see each other's answers."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-know-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.dojo = cls.app.state.dojo
        cls.store = cls.dojo.store
        cls.pid = "gh-300"
        cls.me = Learner("local:dev", learner_key("local:dev"), "Local learner")
        cls.other = Learner("local:other", learner_key("local:other"), "Other learner")
        pkg = Packages(APP_DIR / "packages").get(cls.pid)
        cls.sid = next(s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]
                       if pkg["skills"][s]["sources"] and pkg["skills"][s]["kind"] == "scenario")
        cls.prompts: list[tuple[str, str]] = []
        real = cls.dojo.ai.chat

        def spy(role, system, user, *a, **k):
            cls.prompts.append((system, user))
            return real(role, system, user, *a, **k)
        cls.dojo.ai.chat = spy

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def wait(self, job: dict) -> dict:
        for _ in range(900):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def item(self, mode: str = "check") -> str:
        return self.wait(self.post("/api/items", {"package": self.pid, "skill": self.sid, "mode": mode}))["item"]

    def answer(self, item: str, text: str, confidence: str | None = None) -> dict:
        body = {"text": text, "elapsed_s": 30}
        if confidence:
            body["confidence"] = confidence
        self.wait(self.post(f"/api/items/{item}/answer", body))
        return self.c.get(f"/api/items/{item}").json()

    def events(self, learner: Learner | None = None) -> list[dict]:
        return self.store.events((learner or self.me).key)

    def tomorrow(self):
        later = utcnow() + timedelta(days=1)
        return mock.patch("app.learning.utcnow", lambda: later), mock.patch("app.checks.utcnow", lambda: later)

    def test_01_confidence_is_kept_with_the_answer_but_never_judged(self):
        # Answered three days ago, and again two days ago (a later check): the second puts the skill on
        # the ladder's second step, due in three days, which is tomorrow.
        first = self.item()
        start = len(self.prompts)
        with mock.patch("app.core.utcnow", lambda: utcnow() - timedelta(days=3)):
            view = self.answer(first, "Everything: r1 and r2 both apply here.", "certain")
        graded = [u.lower() for s, u in self.prompts[start:] if "TASK: grade" in s]
        self.assertTrue(graded, "the answer was never judged")
        for text in graded:
            for word in ("confidence", "fairly sure", "guessing", "how sure"):
                self.assertNotIn(word, text)
        self.assertEqual(view["answer"]["confidence"], "certain")
        sent = [e for e in self.events() if e["type"] == "answer.submitted" and e["data"]["item"] == first]
        self.assertEqual(sent[0]["data"]["confidence"], "certain")
        second = self.item()
        with mock.patch("app.core.utcnow", lambda: utcnow() - timedelta(days=2)):
            same = self.answer(second, "Everything: r1 and r2 both apply here.")
        self.assertEqual((same["result"]["met"], same["result"]["total"]), (view["result"]["met"], view["result"]["total"]))
        self.assertNotIn("confidence", [e for e in self.events() if e["type"] == "answer.submitted"
                                        and e["data"]["item"] == second][0]["data"])
        bad = self.c.post(f"/api/items/{self.item()}/answer", json={"text": "x", "confidence": "very"}, headers=POST)
        self.assertEqual(bad.status_code, 422)
        self.assertTrue(self.store.verify_chain(self.me.key)["ok"])

    def test_02_the_pips_are_the_same_with_or_without_confidence(self):
        pkg = Packages(APP_DIR / "packages").get(self.pid)
        events = self.events()
        stripped = copy.deepcopy(events)
        for e in stripped:
            (e.get("data") or {}).pop("confidence", None)
        self.assertEqual(derive(events, pkg), derive(stripped, pkg))
        skill = self.c.get(f"/api/skills/{self.pid}/{self.sid}").json()
        self.assertEqual(skill["evidence"]["state"], "met_unaided_later")
        self.assertEqual((skill["relearn"]["step"], skill["relearn"]["interval"], skill["relearn"]["on_time"]), (1, 3, 1))

    def test_03_a_success_comes_back_as_a_recall_the_next_day(self):
        self.assertEqual(self.dojo.recalls(self.me, self.pid)["due"], [])
        a, b = self.tomorrow()
        with a, b:
            due = self.dojo.recalls(self.me, self.pid)
            self.assertEqual([r["skill"] for r in due["due"]], [self.sid])
            self.assertEqual((due["waiting"], due["left"], due["due"][0]["missed"]), (0, 5, False))
            plan = self.c.get(f"/api/checks?package={self.pid}&recall=true").json()
            self.assertEqual([(p["skill"], p["picked"]) for p in plan["picks"]], [(self.sid, "recall")])
            self.assertEqual(plan["minutes"], relearn.RECALL_MINUTES)
            spoken = self.c.get(f"/api/checks?package={self.pid}").json()
            self.assertEqual(spoken["picks"][0]["skill"], self.sid, "the spoken check asks due recalls first")
            self.assertEqual(spoken["picks"][0]["picked"], "recall")
            today = self.c.get(f"/api/today/{self.pid}").json()
            self.assertEqual([r["skill"] for r in today["recalls"]["due"]], [self.sid])
            started = self.post("/api/checks", {"package": self.pid, "recall": True})
            view = started
            for _ in range(900):
                view = self.c.get(f"/api/checks/{started['id']}").json()
                if not view["coming"]:
                    break
                time.sleep(0.05)
            self.assertTrue(view["recall"])
            q = view["questions"][0]
            stems = {self.c.get(f"/api/items/{i}").json()["stem"] for i in self._earlier_items()}
            self.assertNotIn(q["stem"], stems, "the recall asked a question it asked before")
            self.answer(q["item"], "Everything: r1 and r2, said again.", "fair")
            self.post(f"/api/checks/{started['id']}/end")
        recall_sent = [e for e in self.events() if e["type"] == "answer.submitted" and e["data"].get("recall") is True]
        self.assertEqual(len(recall_sent), 1)
        log = self.store.read_lines("learners", self.me.key, "recall_log.jsonl")
        mine = [x for x in log if x["item"] == q["item"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["recall"], mine[0]["confidence"], mine[0]["result"], mine[0]["learner"]),
                         (True, "fair", "met", self.me.key))
        self.assertIsNotNone(mine[0]["days_since_previous"])
        self.assertEqual(relearn.done_on(self.events(), relearn.local_day(utcnow())), 1)

    def _earlier_items(self) -> list[str]:
        return [e["data"]["item"] for e in self.events() if e["type"] == "answer.submitted"]

    def test_04_quick_question_confidence_is_kept_and_changes_nothing(self):
        rid = self.wait(self.post("/api/rehearsals", {"package": self.pid, "count": 4}))["rehearsal"]
        out = self.post(f"/api/rehearsals/{rid}/answer", {"index": 0, "choice": 0, "confidence": "certain"})
        plain = self.post(f"/api/rehearsals/{rid}/answer", {"index": 1, "choice": 0})
        self.assertEqual(set(out), set(plain))
        answered = [e for e in self.events() if e["type"] == "mcq.answered" and e["data"].get("rehearsal") == rid]
        self.assertEqual([e["data"].get("confidence") for e in answered], ["certain", None])
        self.assertEqual(answered[0]["data"]["correct"], out["correct"])
        R = self.c.get(f"/api/rehearsals/{rid}").json()
        self.assertEqual(R["items"][0]["confidence"], "certain")
        type(self).mcq_right = out["correct"]

    def test_05_a_confident_miss_is_shown_kindly_and_brings_the_skill_back_tomorrow(self):
        lesson = self.wait(self.post("/api/lessons", {"package": self.pid, "skill": self.sid}))["lesson"]
        type(self).lesson = lesson
        miss = self.item()
        self.answer(miss, "I am not sure what to do here.", "certain")
        cal = self.c.get(f"/api/calibration?package={self.pid}").json()
        self.assertEqual(cal["errors"][0]["item"], miss)
        self.assertEqual((cal["errors"][0]["label"], cal["errors"][0]["where"]["lesson"]), ("Certain", lesson))
        self.assertIn("never fills a pip", cal["note"])
        self.assertEqual(cal["levels"]["certain"]["total"], 3)   # the first check, the quick question, this miss
        self.assertIsNone(cal["levels"]["certain"]["rate"])      # too few to say
        self.assertEqual(cal["answers"], 4)                       # 2 certain open, 1 fair recall, 1 certain quick question
        a, b = self.tomorrow()
        with a, b:
            due = self.dojo.recalls(self.me, self.pid)["due"]
        self.assertEqual([r["skill"] for r in due], [self.sid])
        self.assertTrue(due[0]["missed"])
        self.assertEqual(due[0]["repair"]["lesson"], lesson)
        skill = self.c.get(f"/api/skills/{self.pid}/{self.sid}").json()
        self.assertEqual((skill["relearn"]["step"], skill["relearn"]["repair"]), (0, True))

    def test_06_the_lesson_self_check_is_kept_apart_from_the_record(self):
        before = len(self.events())
        self.assertEqual(self.post(f"/api/lessons/{self.lesson}/selfcheck",
                                   {"index": 0, "confidence": "guess", "matched": "partly"}), {"kept": True})
        self.assertEqual(self.c.post(f"/api/lessons/{self.lesson}/selfcheck", json={"index": 9, "matched": "yes"},
                                     headers=POST).status_code, 404)
        self.assertEqual(self.c.post(f"/api/lessons/{self.lesson}/selfcheck", json={"index": 0, "matched": "maybe"},
                                     headers=POST).status_code, 422)
        self.assertEqual(len(self.events()), before, "a self-report reached the Record")
        lines = self.store.read_lines("learners", self.me.key, "selfchecks.jsonl")
        self.assertEqual([(x["confidence"], x["matched"], x["skill"]) for x in lines], [("guess", "partly", self.sid)])
        cal = self.c.get(f"/api/calibration?package={self.pid}").json()
        self.assertEqual(cal["selfchecks"]["answers"], 1)

    def test_07_the_export_carries_the_self_checks_and_the_recall_log(self):
        out = self.c.get("/api/record/export").json()
        self.assertEqual(len(out["selfchecks"]), 1)
        self.assertTrue(out["recall_log"])

    def test_08_two_learners_never_share_anything(self):
        a, b = self.tomorrow()
        with a, b:
            self.assertEqual(self.dojo.recalls(self.other, self.pid)["due"], [])
            self.assertEqual(self.app.state.checks.plan(self.other, self.pid, True)["picks"], [])
            self.assertTrue(self.dojo.recalls(self.me, self.pid)["due"])
        cal = self.dojo.calibration(self.other, self.pid)
        self.assertEqual((cal["answers"], cal["errors"], cal["selfchecks"]["answers"]), (0, [], 0))
        self.dojo.selfcheck(self.other, self.lesson, 0, "certain", "yes")
        self.assertEqual(self.dojo.calibration(self.me, self.pid)["selfchecks"]["answers"], 1)
        self.assertEqual(self.dojo.calibration(self.other, self.pid)["selfchecks"]["answers"], 1)
        root = Path(self.tmp)
        mine = list(root.rglob(f"learners/{self.me.key}/selfchecks.jsonl"))
        theirs = list(root.rglob(f"learners/{self.other.key}/selfchecks.jsonl"))
        self.assertEqual((len(mine), len(theirs)), (1, 1))
        self.assertEqual(list(root.rglob(f"learners/{self.other.key}/recall_log.jsonl")), [])
        self.assertEqual(self.events(self.other), [])

    def test_08b_the_new_routes_are_learner_routes_and_deletion_stays_deleted(self):
        # Team Dojo (ADR 0009): every route of ADR 0011 takes the active learner, and the self-checks and
        # the recall log live under the learner's folder, so leaving or deleting removes them and a late
        # write is refused.
        from fastapi.routing import APIRoute
        mine = {("GET", "/api/calibration"), ("POST", "/api/lessons/{lesson_id}/selfcheck"),
                ("GET", "/api/checks"), ("POST", "/api/checks"), ("GET", "/api/today/{pid}")}
        found = {(m, r.path): classes(r) for r in self.app.routes if isinstance(r, APIRoute) for m in r.methods}
        for route in mine:
            self.assertEqual(found.get(route), {"learner"}, route)
        gone = Learner("local:gone", learner_key("local:gone"), "Gone learner")
        self.dojo.selfcheck(gone, self.lesson, 0, "fair", "yes")
        self.store.append_line("learners", gone.key, "recall_log.jsonl", value={"probe": 1})
        folder = Path(self.tmp).joinpath("learners", gone.key)
        self.assertTrue(folder.joinpath("selfchecks.jsonl").exists())
        late = contextvars.Context()      # a request that began before the deletion
        late.run(self.store.bind_writer, gone.key, self.store.generation(gone.key))
        self.dojo.delete_all(gone)
        self.assertFalse(folder.exists())
        with self.assertRaises(Abandoned):
            late.run(self.dojo.selfcheck, gone, self.lesson, 0, "fair", "yes")
        self.assertFalse(folder.exists())
        self.store.tombstone(gone.key)            # left the Dojo
        with self.assertRaises(Abandoned):
            self.store.append_line("learners", gone.key, "recall_log.jsonl", value={"probe": 2})
        with self.assertRaises(Abandoned):
            self.dojo.selfcheck(gone, self.lesson, 0, "fair", "yes")
        self.assertFalse(folder.exists())
        self.assertEqual(self.dojo.calibration(gone, self.pid)["selfchecks"]["answers"], 0)

    def test_09_the_plan_has_a_short_recall_session_on_the_day_it_is_due(self):
        # The next weekday from tomorrow, early in the morning, so the day still has its study windows.
        later = utcnow() + timedelta(days=1)
        while later.weekday() >= 5:
            later += timedelta(days=1)
        later = later.replace(hour=5, minute=0, second=0, microsecond=0)
        with mock.patch("app.learning.utcnow", lambda: later):
            view = self.app.state.plan.view(self.me, now=later)
        found = [s for s in self._sessions(view) if s.get("kind") == "recall"]
        self.assertTrue(found, view)
        self.assertTrue(all(s.get("package") == self.pid for s in found))
        other = self.app.state.plan.view(self.other, now=later)
        self.assertEqual([s for s in self._sessions(other) if s.get("kind") == "recall"], [])

    def test_10_ending_a_recall_check_unanswered_does_not_free_its_place(self):
        # Review 1: each recall check started reserves its questions before any writer runs, so start,
        # end, start again stops at the daily cap, and Today then shows nothing more to start.
        later = utcnow() + timedelta(days=2)
        clocks = [mock.patch(f"app.{m}.utcnow", lambda: later) for m in ("core", "learning", "checks")]
        for c in clocks:
            c.start()
        try:
            self.assertTrue(self.dojo.recalls(self.me, self.pid)["due"])
            started = 0
            for _ in range(relearn.DAY_CAP + 2):
                r = self.c.post("/api/checks", json={"package": self.pid, "recall": True}, headers=POST)
                if r.status_code >= 300:
                    self.assertIn("No recalls are due", r.text)
                    break
                started += 1
                cid = r.json()["id"]
                for _ in range(900):     # a writer has it: the model may now be spending
                    if self.store.read("learners", self.me.key, "checks", cid)["questions"][0].get("job"):
                        break
                    time.sleep(0.02)
                if started == 1:
                    today = self.dojo.recalls(self.me, self.pid)
                    self.assertEqual([x["skill"] for x in today["due"]], [self.sid], "an open check's recall left Today")
                self.post(f"/api/checks/{cid}/end")
            self.assertEqual(started, relearn.DAY_CAP)
            today = self.dojo.recalls(self.me, self.pid)
            self.assertEqual((today["due"], today["left"], today["waiting"]), ([], 0, 1))
            spoken = self.app.state.checks.plan(self.me, self.pid)["picks"]
            self.assertEqual([p for p in spoken if p["picked"] == "recall"], [], "the spoken check went over the cap")
            self.assertEqual(self.dojo.recalls(self.other, self.pid)["left"], relearn.DAY_CAP)
        finally:
            for c in clocks:
                c.stop()
            for _ in range(900):     # let the writers finish before the next test reads the Record
                if not self.app.state.checks._filling:
                    break
                time.sleep(0.05)

    @staticmethod
    def _sessions(view) -> list[dict]:
        out: list[dict] = []

        def walk(x):
            if isinstance(x, dict):
                if "kind" in x and ("start" in x or "minutes" in x):
                    out.append(x)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(view)
        return out


if __name__ == "__main__":
    unittest.main()
