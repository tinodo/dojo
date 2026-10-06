"""Play, the personal layer (ADR 0010): study points, the study day, weeks met, the moments shelf,
reported passes, turning Play on, leaving, and the rules that keep it private and effort-only."""
import json
import os
import random
import re
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import play  # noqa: E402
from app.core import Learner, iso, learner_key, new_id  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings  # noqa: E402

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
PKG = {"meta": {"id": "t-1", "exam": "T-1", "title": "Test exam"},
       "skills": {"s1": {"text": "Handle a scenario", "kind": "scenario", "domain_title": "Domain A"},
                  "s2": {"text": "Know a fact", "kind": "concept", "domain_title": "Domain A"}}}
PACKAGES = {"t-1": PKG}


def T(y, m, d, hh=10, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def ev(etype: str, at: datetime, **data) -> dict:
    return {"id": new_id("ev"), "at": iso(at), "type": etype, "data": data}


def answer(sent: datetime, sid="s1", mode="probe", met=3, total=3, delay=30, changed=False, hints=0, item=None,
           judged=True) -> list[dict]:
    """An open answer sent at `sent`, issued `delay` seconds before, judged two seconds after."""
    item = item or new_id("it")
    base = {"package": "t-1", "skill": sid}
    issued = sent - timedelta(seconds=delay)
    out = [ev("item.issued", issued, item=item, mode=mode, kind="x", changed_condition=changed, **base)]
    out += [ev("hint.shown", issued + timedelta(seconds=1), item=item, **base) for _ in range(hints)]
    out.append(ev("answer.submitted", sent, item=item, elapsed_s=delay, input="typed", **base))
    if judged:
        out.append(ev("answer.judged", sent + timedelta(seconds=2), item=item, met=met, total=total, mode=mode, **base))
    return out


def rehearsal(start: datetime, n: int, gap=6, correct=True) -> list[dict]:
    rid = new_id("re")
    out = [ev("rehearsal.created", start, rehearsal=rid, package="t-1")]
    out += [ev("mcq.answered", start + timedelta(seconds=gap * (i + 1)), rehearsal=rid, index=i, correct=correct,
               package="t-1", skill="s1") for i in range(n)]
    return out


def exam(start: datetime, answered: int, seconds: int, correct=True, started=True) -> list[dict]:
    aid = new_id("ex")
    out = [ev("exam.started", start, attempt=aid, package="t-1", questions=answered + 5)] if started else []
    close = start + timedelta(seconds=seconds)
    out += [ev("exam.answered", close, attempt=aid, package="t-1", skill="s1", index=i, correct=correct)
            for i in range(answered)]
    out.append(ev("exam.closed", close, attempt=aid, package="t-1", answered=answered, correct=answered if correct else 0))
    return out


def lab(at: datetime, name="lab-a", runs=1, demo=False, passed=True) -> list[dict]:
    data = {"lab": name, "runs": runs, "passed": passed, "package": "t-1", "skill": "s2"}
    if demo:
        data["local_demo"] = True
    return [ev("lab.checked", at, **data)]


def lesson(at: datetime, sid="s1") -> list[dict]:
    return [ev("lesson.viewed", at, package="t-1", skill=sid, lesson="le-1", modality="read")]


def week_points(events, today=date(2026, 10, 1)) -> dict:
    return play.points(events, today)


MON = T(2026, 9, 28)  # a Monday, 12:00 in Berlin


class PointsTests(unittest.TestCase):
    def test_open_answer_and_study_day(self):
        p = week_points(answer(MON))
        self.assertEqual(p["total"], 13)
        self.assertEqual(p["by_kind"], {"study_day": 10, "answers": 3, "lessons": 0, "labs": 0})
        self.assertEqual(p["study_days"], 1)

    def test_study_day_counts_once_whatever_the_activity(self):
        evs = answer(MON) + answer(MON + timedelta(hours=1)) + lab(MON + timedelta(hours=2)) + exam(MON + timedelta(hours=3), 10, 600)
        p = week_points(evs)
        self.assertEqual(p["by_kind"]["study_day"], 10)
        for one in (answer(MON), rehearsal(MON, 5), exam(MON, 10, 50), lab(MON)):
            self.assertEqual(week_points(one)["by_kind"]["study_day"], 10)

    def test_answer_cap_is_shared_by_open_and_choice(self):
        evs = sum((answer(MON + timedelta(minutes=i)) for i in range(11)), [])
        self.assertEqual(week_points(evs)["by_kind"]["answers"], 30)
        evs = sum((answer(MON + timedelta(minutes=i)) for i in range(8)), []) + rehearsal(MON + timedelta(hours=1), 10)
        self.assertEqual(week_points(evs)["by_kind"]["answers"], 30)

    def test_choice_answers(self):
        p = week_points(rehearsal(MON, 4))
        self.assertEqual((p["total"], p["study_days"]), (4, 0))
        p = week_points(rehearsal(MON, 5))
        self.assertEqual((p["total"], p["study_days"]), (15, 1))
        self.assertEqual(week_points(rehearsal(MON, 5, correct=False))["total"], 15)

    def test_choice_under_five_seconds_earns_nothing(self):
        p = week_points(rehearsal(MON, 10, gap=4))
        self.assertEqual((p["total"], p["study_days"]), (0, 0))

    def test_choice_without_its_rehearsal_earns_nothing(self):
        evs = [e for e in rehearsal(MON, 6) if e["type"] != "rehearsal.created"]
        self.assertEqual(week_points(evs)["by_kind"]["answers"], 5)  # the first has no start to measure from
        self.assertEqual(week_points(evs[:1])["total"], 0)

    def test_lesson_views_cap_and_never_a_study_day(self):
        p = week_points(sum((lesson(MON + timedelta(minutes=i)) for i in range(7)), []))
        self.assertEqual((p["total"], p["study_days"]), (10, 0))

    def test_labs(self):
        self.assertEqual(week_points(lab(MON))["total"], 20)
        self.assertEqual(week_points(lab(MON) + lab(MON + timedelta(hours=1)))["total"], 20)  # once per lab a day
        self.assertEqual(week_points(lab(MON, passed=False))["total"], 20)  # whatever the result
        self.assertEqual(week_points(lab(MON, demo=True))["total"], 0)
        self.assertEqual(week_points(lab(MON, runs=0))["total"], 0)
        week = sum((lab(MON + timedelta(days=i), name=f"lab-{i}") for i in range(5)), [])
        p = week_points(week, date(2026, 10, 4))
        self.assertEqual(p["by_kind"]["labs"], 30)  # three a week
        self.assertEqual(p["study_days"], 5)

    def test_day_and_week_caps(self):
        day = (sum((answer(MON + timedelta(minutes=i)) for i in range(12)), []) + rehearsal(MON + timedelta(hours=1), 9)
               + sum((lesson(MON + timedelta(hours=2, minutes=i)) for i in range(8)), [])
               + sum((lab(MON + timedelta(hours=3), name=f"lab-{i}") for i in range(4)), []))
        self.assertEqual(week_points(day)["total"], play.DAY_CAP)
        days = sorted(play.day_points(play.activities(day)).values(), key=lambda d: d["total"])
        self.assertLessEqual(days[-1]["total"], play.DAY_CAP)
        week = []
        for i in range(7):
            at = MON + timedelta(days=i)
            week += sum((answer(at + timedelta(minutes=j)) for j in range(12)), []) + sum((lesson(at + timedelta(hours=2, minutes=j)) for j in range(6)), [])
            week += lab(at + timedelta(hours=3), name=f"lab-{i}")
        p = week_points(week, date(2026, 10, 4))
        self.assertLessEqual(p["total"], play.WEEK_CAP)
        self.assertEqual(p["total"], 7 * 50 + 30)
        self.assertEqual(p["caps"], {"day": 80, "week": 400, "answers": 30})

    def test_zero_point_rules(self):
        base = {"package": "t-1", "skill": "s1", "item": "it-x"}
        nothing = [ev("dispute.filed", MON, **base), ev("hint.shown", MON, **base), ev("ask.answered", MON, **base),
                   ev("podcast.declared", MON, heard=True, **base), ev("exam.rationales_shown", MON, package="t-1", skills=["s1"]),
                   ev("coach.answered", MON, **base)]
        p = week_points(nothing)
        self.assertEqual((p["total"], p["study_days"]), (0, 0))
        self.assertEqual(week_points(answer(MON, delay=4))["total"], 0)  # under five seconds
        self.assertEqual(week_points(answer(MON, judged=False))["total"], 0)  # never judged
        no_issue = [e for e in answer(MON) if e["type"] != "item.issued"]
        self.assertEqual(week_points(no_issue)["total"], 0)  # a missing event never counts for the learner
        evs = answer(MON, item="it-a")
        regraded = evs + [ev("answer.judged", MON + timedelta(minutes=5), item="it-a", met=0, total=3, package="t-1", skill="s1")]
        resent = evs + [ev("answer.submitted", MON + timedelta(minutes=6), item="it-a", package="t-1", skill="s1")]
        self.assertEqual(week_points(regraded)["total"], 13)
        self.assertEqual(week_points(resent)["total"], 13)

    def test_correctness_never_changes_points(self):
        for met in (0, 1, 3):
            self.assertEqual(week_points(answer(MON, met=met))["total"], 13)
        disputed = answer(MON, item="it-d") + [ev("dispute.filed", MON + timedelta(minutes=1), item="it-d", package="t-1", skill="s1")]
        self.assertEqual(week_points(disputed)["total"], 13)
        self.assertEqual(week_points(answer(MON, mode="practice", hints=2))["total"], 13)

    def test_exam(self):
        p = week_points(exam(MON, 12, 120))
        self.assertEqual((p["by_kind"]["answers"], p["study_days"]), (12, 1))
        p = week_points(exam(MON, 10, 49))  # ten answered in under 50 seconds: rushed
        self.assertEqual((p["by_kind"]["answers"], p["study_days"]), (9, 0))
        p = week_points(exam(MON, 10, 50))
        self.assertEqual((p["total"], p["study_days"]), (20, 1))
        p = week_points(exam(MON, 9, 3600))
        self.assertEqual((p["total"], p["study_days"]), (9, 0))
        self.assertEqual(week_points(exam(MON, 12, 600, started=False))["total"], 0)
        self.assertEqual(week_points(exam(MON, 40, 3600))["by_kind"]["answers"], 30)  # the shared cap; no bonus
        self.assertEqual(week_points(exam(MON, 12, 600, correct=False))["total"], week_points(exam(MON, 12, 600))["total"])

    def test_exam_close_carries_answered(self):
        src = (ROOT / "app" / "exam.py").read_text(encoding="utf-8")
        self.assertRegex(src, r'append_event\(learner\.key, "exam\.closed", \{[^}]*"answered": self\._answered')

    def test_monday_reset_in_berlin(self):
        # 23:59 Sunday and 00:00 Monday, Europe/Berlin: summer time, the last October weekend, the last March weekend.
        for sunday_2359, monday_0000, monday in (
                (T(2026, 10, 4, 21, 59), T(2026, 10, 4, 22, 0, 30), date(2026, 10, 5)),
                (T(2026, 10, 25, 22, 59), T(2026, 10, 25, 23, 0, 30), date(2026, 10, 26)),
                (T(2027, 3, 28, 21, 59), T(2027, 3, 28, 22, 0, 30), date(2027, 3, 29))):
            p = play.points(answer(sunday_2359) + answer(monday_0000), monday)
            self.assertEqual(p["week"], monday.isoformat())
            self.assertEqual(p["total"], 13)
            self.assertEqual(p["weeks"][-2], {"week": (monday - timedelta(days=7)).isoformat(), "total": 13,
                                              "by_kind": {"study_day": 10, "answers": 3, "lessons": 0, "labs": 0}, "study_days": 1})

    def test_eight_weeks_and_no_lifetime_total(self):
        evs = sum((answer(MON + timedelta(weeks=i)) for i in range(12)), [])
        p = play.points(evs, date(2026, 12, 20))
        self.assertEqual(len(p["weeks"]), 8)
        self.assertNotIn("lifetime", json.dumps(p))
        p = play.points(evs, date(2026, 12, 20), since=iso(T(2026, 12, 2)))
        self.assertEqual([w["week"] for w in p["weeks"]], ["2026-11-30", "2026-12-07", "2026-12-14"])
        self.assertEqual(p["weeks"][0]["total"], 0)  # the answer on Monday 30 Nov came before `since`


def random_record(rng: random.Random) -> list[dict]:
    evs: list[dict] = []
    start = T(2026, 9, 1)
    for _ in range(rng.randint(10, 40)):
        at = start + timedelta(minutes=rng.randint(0, 60 * 24 * 30))
        kind = rng.choice(["open", "open", "choice", "exam", "lab", "lesson"])
        if kind == "open":
            total = rng.randint(1, 4)
            evs += answer(at, sid=rng.choice(["s1", "s2"]), mode=rng.choice(["probe", "practice", "check"]),
                          met=rng.randint(0, total), total=total, delay=rng.randint(1, 90), changed=rng.random() < 0.3,
                          hints=rng.choice([0, 0, 1]))
        elif kind == "choice":
            evs += rehearsal(at, rng.randint(1, 8), gap=rng.randint(2, 9), correct=rng.random() < 0.5)
        elif kind == "exam":
            evs += exam(at, rng.randint(5, 20), rng.randint(20, 3000), correct=rng.random() < 0.5)
        elif kind == "lab":
            evs += lab(at, name=f"lab-{rng.randint(1, 5)}", passed=rng.random() < 0.5)
        else:
            evs += lesson(at)
    evs.sort(key=lambda e: e["at"])
    return evs


def flipped(evs: list[dict], rng: random.Random) -> list[dict]:
    """The same activities with every judgement, correctness, dispute and confidence changed."""
    out = []
    for e in evs:
        e = json.loads(json.dumps(e))
        d = e["data"]
        if e["type"] == "answer.judged":
            d["met"] = rng.randint(0, d["total"])
            d["confidence"] = rng.choice(["sure", "unsure", "guess"])
        elif e["type"] in ("mcq.answered", "exam.answered"):
            d["correct"] = not d["correct"]
            d["confidence"] = rng.choice(["sure", "unsure"])
        elif e["type"] == "exam.closed":
            d["correct"] = rng.randint(0, d["answered"])
        elif e["type"] == "lab.checked":
            d["passed"] = not d["passed"]
        out.append(e)
        if e["type"] == "answer.judged" and rng.random() < 0.4:
            out.append(ev("dispute.filed", datetime.fromisoformat(e["at"]) + timedelta(seconds=1), item=d["item"],
                          package="t-1", skill=d["skill"]))
    return out


class InvariantTests(unittest.TestCase):
    """Points, study days and weeks met depend only on the activities done, never on correctness."""

    def test_flipping_any_judgement_changes_nothing(self):
        settings_doc = {"since": None, "rhythm": {"on": True, "targets": [{"from": "2026-09-01", "days": 3}]}}
        for seed in range(60):
            rng = random.Random(seed)
            evs = random_record(rng)
            other = flipped(evs, rng)
            self.assertEqual(play.day_points(play.activities(evs)), play.day_points(play.activities(other)), seed)
            self.assertEqual(play.study_days(play.activities(evs)), play.study_days(play.activities(other)), seed)
            for today in (date(2026, 9, 10), date(2026, 9, 30), date(2026, 10, 12)):
                self.assertEqual(play.points(evs, today), play.points(other, today), seed)
                self.assertEqual(play.rhythm(evs, settings_doc, today), play.rhythm(other, settings_doc, today), seed)

    def test_points_and_rhythm_read_no_judgement(self):
        src = (ROOT / "app" / "play.py").read_text(encoding="utf-8")
        body = src.split("# ---------------------------------------------------------------- moments")[0]
        for word in ('get("met")', '["met"]', '"all_met"', 'get("correct")', '["correct"]', '"confidence"', 'get("passed")', "relearn", '"dispute.filed"'):
            self.assertNotIn(word, body)

    def test_play_never_feeds_evidence(self):
        for path in (ROOT / "app").glob("*.py"):
            if path.name in ("main.py", "play.py", "play_social.py", "play_board.py", "play_duel.py"):
                continue
            src = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"from \.play import|from \. import play|import app\.play", src), path.name)


def doc(turned_on: str, days=3, since: str | None = "set", on=True) -> dict:
    return {"since": iso(datetime.fromisoformat(turned_on + "T06:00:00+00:00")) if since == "set" else since,
            "rhythm": {"on": on, "targets": [{"from": turned_on, "days": days}]}, "hidden": []}


class RhythmTests(unittest.TestCase):
    def days(self, *ds: date) -> list[dict]:
        return sum((answer(datetime(d.year, d.month, d.day, 10, tzinfo=UTC)) for d in ds), [])

    def test_first_week_is_scaled(self):
        # Turned on Friday 2 Oct with 3 days: Friday to Sunday is 3 of 7 days, so ceil(3 x 3 / 7) = 2.
        r = play.rhythm(self.days(date(2026, 10, 2), date(2026, 10, 3)), doc("2026-10-02"), date(2026, 10, 3))
        self.assertEqual(r["this_week"], {"days": 2, "needed": 2, "met": True})
        self.assertEqual(r["weeks_met"], 1)
        r = play.rhythm([], doc("2026-09-28", days=6), date(2026, 9, 28))
        self.assertEqual(r["this_week"]["needed"], 6)  # turned on a Monday: the whole week

    def test_one_retrieval_counts_a_day_and_a_lesson_does_not(self):
        r = play.rhythm(lesson(MON) + lesson(MON + timedelta(days=1)), doc("2026-09-28"), date(2026, 9, 30))
        self.assertEqual(r["this_week"]["days"], 0)
        r = play.rhythm(answer(MON) + answer(MON + timedelta(hours=1)), doc("2026-09-28"), date(2026, 9, 30))
        self.assertEqual(r["this_week"]["days"], 1)
        self.assertTrue(r["studied_today"] is False)
        self.assertEqual(play.rhythm(rehearsal(MON, 5), doc("2026-09-28"), date(2026, 9, 28))["this_week"]["days"], 1)
        self.assertEqual(play.rhythm(rehearsal(MON, 4), doc("2026-09-28"), date(2026, 9, 28))["this_week"]["days"], 0)
        self.assertEqual(play.rhythm(exam(MON, 10, 50), doc("2026-09-28"), date(2026, 9, 28))["this_week"]["days"], 1)
        self.assertEqual(play.rhythm(exam(MON, 9, 900), doc("2026-09-28"), date(2026, 9, 28))["this_week"]["days"], 0)
        self.assertEqual(play.rhythm(exam(MON, 10, 49), doc("2026-09-28"), date(2026, 9, 28))["this_week"]["days"], 0)

    def test_weeks_met_never_goes_down(self):
        studied = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 6), date(2026, 10, 20),
                   date(2026, 10, 21), date(2026, 10, 22)]
        evs = self.days(*studied)
        seen = []
        day = date(2026, 9, 28)
        while day <= date(2026, 11, 30):
            r = play.rhythm(evs, doc("2026-09-28"), day)
            seen.append(r["weeks_met"])
            day += timedelta(days=1)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 2)

    def test_no_run_freeze_or_pause(self):
        r = play.rhythm(self.days(date(2026, 9, 28)), doc("2026-09-28"), date(2026, 10, 14))
        text = json.dumps(r).lower()
        for word in ("streak", "in_a_row", "row", "freeze", "pause", "missed", "lost", "behind", "level", "rank"):
            self.assertNotIn(word, text)
        self.assertEqual(set(r), {"on", "weeks_met", "this_week", "last_week_met", "target", "next_target", "studied_today"})

    def test_include_record_counts_earlier_weeks(self):
        evs = self.days(date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3))
        self.assertEqual(play.rhythm(evs, doc("2026-09-28", since=None), date(2026, 9, 28))["weeks_met"], 1)
        self.assertEqual(play.rhythm(evs, doc("2026-09-28"), date(2026, 9, 28))["weeks_met"], 0)

    def test_target_from_next_monday(self):
        d = doc("2026-09-28")
        d["rhythm"]["targets"].append({"from": "2026-10-05", "days": 5})
        evs = self.days(date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))
        r = play.rhythm(evs, d, date(2026, 10, 1))
        self.assertEqual((r["target"], r["next_target"], r["this_week"]["met"]), (3, 5, True))
        r = play.rhythm(evs + self.days(date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)), d, date(2026, 10, 8))
        self.assertEqual((r["target"], r["this_week"]["needed"], r["this_week"]["met"], r["weeks_met"]), (5, 5, False, 1))

    def test_same_whenever_computed(self):
        evs = self.days(date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))
        a = play.rhythm(evs, doc("2026-09-28"), date(2026, 10, 20))["weeks_met"]
        b = play.rhythm(evs + self.days(date(2026, 10, 21)), doc("2026-09-28"), date(2026, 10, 20))["weeks_met"]
        self.assertEqual(a, b)

    def test_no_targets_no_rhythm(self):
        self.assertIsNone(play.rhythm([], {"since": None, "rhythm": {"on": False, "targets": []}}, date(2026, 10, 1)))


T0 = T(2026, 9, 1, 8)


class MomentTests(unittest.TestCase):
    def shelf(self, evs, since=None, rechecks=None):
        return play.moments(evs, PACKAGES, 20, since, {"t-1": rechecks or {}})

    def test_later_moment(self):
        evs = lesson(T0) + answer(T0 + timedelta(hours=21))
        ms = self.shelf(evs)
        self.assertEqual([m["kind"] for m in ms], ["later"])
        self.assertEqual(ms[0]["at"], evs[-1]["at"])
        self.assertEqual(ms[0]["line"], "From Dojo's own questions. Not a certification.")
        self.assertIsNone(ms[0]["refresh"])

    def test_nothing_smaller_is_a_moment(self):
        self.assertEqual(self.shelf(lesson(T0) + answer(T0 + timedelta(hours=2))), [])  # right after teaching
        self.assertEqual(self.shelf(lesson(T0) + answer(T0 + timedelta(hours=21), mode="practice")), [])  # help offered
        self.assertEqual(self.shelf(lesson(T0) + answer(T0 + timedelta(hours=21), hints=1)), [])
        self.assertEqual(self.shelf(lesson(T0) + answer(T0 + timedelta(hours=21), met=2)), [])
        self.assertEqual(self.shelf(rehearsal(T0, 10) + exam(T0 + timedelta(days=2), 20, 900) + lab(T0)), [])

    def test_new_situation_only_for_scenarios(self):
        ms = self.shelf(answer(T0, changed=True))
        self.assertEqual([m["kind"] for m in ms], ["changed"])
        self.assertEqual(self.shelf(answer(T0, sid="s2", changed=True)), [])
        ms = self.shelf(lesson(T0) + answer(T0 + timedelta(hours=21), changed=True))
        self.assertEqual(sorted(m["kind"] for m in ms), ["changed", "later"])

    def test_a_dispute_moves_or_removes_the_moment(self):
        first = answer(T0 + timedelta(hours=21), item="it-first")
        second = answer(T0 + timedelta(days=3), item="it-second")
        dispute = [ev("dispute.filed", T0 + timedelta(days=4), item="it-first", package="t-1", skill="s1")]
        self.assertEqual(self.shelf(lesson(T0) + first + second)[0]["at"], first[-1]["at"])
        self.assertEqual(self.shelf(lesson(T0) + first + second + dispute)[0]["at"], second[-1]["at"])
        self.assertEqual(self.shelf(lesson(T0) + first + dispute), [])

    def test_refresh_after_a_miss_until_the_next_success(self):
        evs = lesson(T0) + answer(T0 + timedelta(hours=21))
        miss = answer(T0 + timedelta(days=5), mode="practice", met=1)
        ms = self.shelf(evs + miss)
        self.assertEqual(len(ms), 1)  # a later miss never removes it
        self.assertIn("not yet met", ms[0]["refresh"])
        self.assertIn("worth", "worth a refresh")
        soon = answer(T0 + timedelta(days=5, hours=2))  # unaided, but right after the feedback taught it
        self.assertIsNotNone(self.shelf(evs + miss + soon)[0]["refresh"])
        again = answer(T0 + timedelta(days=6, hours=2))
        self.assertIsNone(self.shelf(evs + miss + again)[0]["refresh"])

    def test_disputed_success_never_clears_a_refresh(self):
        evs = lesson(T0) + answer(T0 + timedelta(hours=21)) + answer(T0 + timedelta(days=5), met=0)
        again = answer(T0 + timedelta(days=6, hours=2), item="it-again")
        dispute = [ev("dispute.filed", T0 + timedelta(days=7), item="it-again", package="t-1", skill="s1")]
        self.assertIsNotNone(self.shelf(evs + again + dispute)[0]["refresh"])

    def test_time_alone_never_tags(self):
        evs = lesson(T0) + answer(T0 + timedelta(hours=21))
        self.assertIsNone(self.shelf(evs)[0]["refresh"])

    def test_drift_recheck_tags_until_a_new_success(self):
        evs = lesson(T0) + answer(T0 + timedelta(hours=21))
        changed = {"s1": iso(T0 + timedelta(days=3))}
        self.assertIn("official page", self.shelf(evs, rechecks=changed)[0]["refresh"])
        self.assertIsNone(self.shelf(evs, rechecks={"s1": iso(T0)})[0]["refresh"])  # before the moment
        after = answer(T0 + timedelta(days=4))
        self.assertIsNone(self.shelf(evs + after, rechecks=changed)[0]["refresh"])

    def test_since_and_ids(self):
        evs = lesson(T0) + answer(T0 + timedelta(hours=21))
        self.assertEqual(self.shelf(evs, since=iso(T0 + timedelta(days=1))), [])
        ms = self.shelf(evs, since=iso(T0))
        self.assertRegex(ms[0]["id"], r"^[a-z]+-[0-9a-f]{20}$")
        self.assertEqual(ms[0]["id"], play.moment_id("later", "t-1", "s1"))

    def test_pass_moment(self):
        evs = [ev("credential.reported", T0, package="t-1", exam="T-1", title="Test exam", earned="2026-08-30",
                  source="self", checked_at=None)]
        ms = self.shelf(evs)
        self.assertEqual(ms[0]["kind"], "passed")
        self.assertEqual(ms[0]["at"], "2026-08-30")
        self.assertEqual(ms[0]["line"], "Self-reported. Dojo did not check it.")
        self.assertEqual(self.shelf(evs, since=iso(T0 + timedelta(seconds=1))), [])


class RelearnedMomentTests(unittest.TestCase):
    """Moments from Know's ladder (ADR 0010 section 1, ADR 0011): relearned, every skill of a domain, every domain."""

    def setUp(self):
        src = [{"url": "https://learn.microsoft.com/x"}]
        self.pkg = {"meta": {"id": "t-1", "exam": "T-1", "title": "Test exam"},
                    "domains": [{"id": "d1", "title": "Domain A"}, {"id": "d2", "title": "Domain B"}],
                    "skills": {"s1": {"text": "Skill one", "kind": "concept", "domain": "d1", "domain_title": "Domain A", "sources": src},
                               "s2": {"text": "Skill two", "kind": "concept", "domain": "d1", "domain_title": "Domain A", "sources": src},
                               "s3": {"text": "Skill three", "kind": "concept", "domain": "d2", "domain_title": "Domain B", "sources": src},
                               "s4": {"text": "No verified page", "kind": "concept", "domain": "d2", "domain_title": "Domain B", "sources": []}}}

    @staticmethod
    def ladder(sid, start, days=(0, 1, 4, 11), tag=None):
        """Successes on these days: the first starts Know's ladder, each next one is on time."""
        return sum((answer(start + timedelta(days=d), sid=sid, item=f"{tag or sid}-{d}") for d in days), [])

    def shelf(self, evs, gone=None, rechecks=None, pkg=None, since=None):
        """Only the moments from Know's ladder: the next day's answer also fills a "later" pip, tested above."""
        ms = play.moments(evs, {"t-1": pkg or self.pkg}, 20, since, {"t-1": rechecks or {}}, None, {"t-1": gone or set()})
        return [m for m in ms if m["kind"] not in ("later", "changed")]

    def kinds(self, ms):
        return sorted((m["kind"], m.get("skill") or m["skill_text"]) for m in ms)

    def test_relearned_after_three_on_time_successes(self):
        evs = self.ladder("s1", T0)
        ms = self.shelf(evs)
        self.assertEqual(self.kinds(ms), [("relearned", "s1")])
        self.assertEqual(ms[0]["at"], evs[-1]["at"])  # the third on-time success
        self.assertEqual(ms[0]["title"], "Relearned")
        self.assertEqual(ms[0]["line"], play.NOT_A_CERTIFICATE)
        self.assertIn("on time", ms[0]["made_by"])
        self.assertIsNone(ms[0]["refresh"])
        self.assertEqual(ms[0]["id"], play.moment_id("relearned", "t-1", "s1"))
        self.assertEqual(self.shelf(self.ladder("s1", T0, days=(0, 1, 4))), [])  # two on time: not yet
        cram = sum((answer(T0 + timedelta(hours=h), item=f"cram-{h}") for h in range(4)), [])
        self.assertEqual(self.shelf(cram), [])  # cramming is not spacing

    def test_a_dispute_of_any_of_the_three_successes_moves_or_removes_it(self):
        four = self.ladder("s1", T0)
        five = self.ladder("s1", T0, days=(0, 1, 4, 11, 30))
        for d in (1, 4, 11):  # the first, the second and the last on-time success
            dispute = [ev("dispute.filed", T0 + timedelta(days=40), item=f"s1-{d}", package="t-1", skill="s1")]
            self.assertEqual(self.shelf(four + dispute), [], d)
            ms = self.shelf(five + dispute)
            self.assertEqual(self.kinds(ms), [("relearned", "s1")], d)
            self.assertEqual(ms[0]["at"], five[-1]["at"], d)  # moved to the next success that qualifies
        dispute = [ev("dispute.filed", T0 + timedelta(days=40), item="s1-0", package="t-1", skill="s1")]
        self.assertEqual(self.shelf(four + dispute), [])  # the one that started the ladder too

    def test_domain_and_exam_moments(self):
        evs = self.ladder("s1", T0) + self.ladder("s2", T0 + timedelta(days=2)) + self.ladder("s3", T0 + timedelta(days=5))
        ms = self.shelf(evs)
        self.assertEqual(self.kinds(ms), [("domain", "Domain A"), ("domain", "Domain B"), ("exam", ""),
                                          ("relearned", "s1"), ("relearned", "s2"), ("relearned", "s3")])
        by = {m["kind"] + m["skill_text"]: m for m in ms}
        s2_last = self.ladder("s2", T0 + timedelta(days=2))[-1]["at"]
        self.assertEqual(by["domainDomain A"]["at"], s2_last)  # the last of them
        self.assertEqual(by["exam"]["at"], evs[-1]["at"])
        self.assertEqual(by["exam"]["id"], play.moment_id("exam", "t-1"))
        self.assertEqual(by["domainDomain B"]["id"], play.moment_id("domain", "t-1", "d2"))
        for m in ms:
            self.assertEqual(m["line"], play.NOT_A_CERTIFICATE)
            self.assertTrue(m["made_by"])
        # Domain B ignores s4 (no verified page). Without s2, domain A and the exam are not marked.
        ms = self.shelf(self.ladder("s1", T0) + self.ladder("s3", T0))
        self.assertEqual(self.kinds(ms), [("domain", "Domain B"), ("relearned", "s1"), ("relearned", "s3")])
        # A skill whose pages are all gone is not askable, so it does not hold the domain back.
        ms = self.shelf(self.ladder("s1", T0) + self.ladder("s3", T0), gone={"s2"})
        self.assertEqual(self.kinds(ms), [("domain", "Domain A"), ("domain", "Domain B"), ("exam", ""),
                                          ("relearned", "s1"), ("relearned", "s3")])

    def test_a_skill_added_to_a_domain_takes_the_moment_away_until_it_is_relearned(self):
        evs = self.ladder("s1", T0) + self.ladder("s2", T0) + self.ladder("s3", T0)
        self.assertIn(("domain", "Domain A"), self.kinds(self.shelf(evs)))
        grown = json.loads(json.dumps(self.pkg))
        grown["skills"]["s5"] = {**grown["skills"]["s1"], "text": "Skill five"}
        ms = self.shelf(evs, pkg=grown)
        self.assertEqual(self.kinds(ms), [("domain", "Domain B"), ("relearned", "s1"), ("relearned", "s2"),
                                          ("relearned", "s3")])
        later = self.ladder("s5", T0 + timedelta(days=20))
        ms = self.shelf(evs + later, pkg=grown)
        by = {m["kind"] + m["skill_text"]: m for m in ms}
        self.assertEqual(by["domainDomain A"]["at"], later[-1]["at"])  # back, with the new date
        self.assertEqual(by["exam"]["at"], later[-1]["at"])

    def test_a_later_miss_tags_a_refresh_until_relearned_again(self):
        evs = self.ladder("s1", T0) + self.ladder("s2", T0) + self.ladder("s3", T0)
        miss = answer(T0 + timedelta(days=20), sid="s1", met=1, item="s1-miss")
        ms = self.shelf(evs + miss)
        by = {m["kind"] + (m["skill"] or m["skill_text"]): m for m in ms}
        self.assertEqual(len(ms), 6)  # a later miss never removes a moment
        self.assertIn("not yet met", by["relearneds1"]["refresh"])
        self.assertIn("back in your recalls", by["relearneds1"]["refresh"])
        self.assertIsNotNone(by["domainDomain A"]["refresh"])
        self.assertIsNotNone(by["exam"]["refresh"])
        self.assertIsNone(by["domainDomain B"]["refresh"])
        self.assertEqual(by["relearneds1"]["at"], self.ladder("s1", T0)[-1]["at"])  # the date stays
        again = self.ladder("s1", T0 + timedelta(days=21), days=(0, 3, 10), tag="s1-again")
        self.assertTrue(all(m["refresh"] is None for m in self.shelf(evs + miss + again)))
        disputed = again + [ev("dispute.filed", T0 + timedelta(days=40), item="s1-again-10", package="t-1", skill="s1")]
        self.assertIsNotNone({m["kind"] + (m["skill"] or ""): m for m in self.shelf(evs + miss + disputed)}
                             ["relearneds1"]["refresh"])  # a disputed answer never clears a tag

    def test_drift_tags_until_relearned_again_and_time_alone_never_does(self):
        """PR #40 review: after a page change the tag goes only when the skill is relearned again (ADR 0010
        section 1: "relearned again (relearned and the domain and exam above it)"), not after one success."""
        evs = self.ladder("s1", T0) + self.ladder("s2", T0) + self.ladder("s3", T0)
        self.assertTrue(all(m["refresh"] is None for m in self.shelf(evs)))
        changed = {"s1": iso(T0 + timedelta(days=15))}
        tagged = lambda more: {m["kind"] + (m["skill"] or m["skill_text"]): m["refresh"]  # noqa: E731
                               for m in self.shelf(evs + more, rechecks=changed)}
        self.assertIn("official page", tagged([])["relearneds1"])
        # After day 11 the next recall is due on day 27, then day 62, then day 97 (ADR 0011 intervals).
        early = answer(T0 + timedelta(days=16), sid="s1", item="s1-16")  # a success, but not on time
        on_time = [answer(T0 + timedelta(days=d), sid="s1", item=f"s1-{d}") for d in (27, 62, 97)]
        for more in ([], early, early + on_time[0], early + on_time[0] + on_time[1]):
            t = tagged(more)
            self.assertIn("official page", t["relearneds1"])
            self.assertIsNotNone(t["domainDomain A"])
            self.assertIsNotNone(t["exam"])
        disputed = sum(on_time, []) + [ev("dispute.filed", T0 + timedelta(days=98), item="s1-62", package="t-1", skill="s1")]
        self.assertIsNotNone(tagged(early + disputed)["relearneds1"])  # a disputed success never clears it
        t = tagged(early + sum(on_time, []))
        self.assertEqual((t["relearneds1"], t["domainDomain A"], t["exam"]), (None, None, None))
        self.assertIsNone(tagged([])["relearneds2"])

    def test_the_exact_answer_that_relearned_it_not_one_in_the_same_second(self):
        """PR #40 review: an answer after joining, in the same whole second as the one that first reached
        relearned, must not bring a pre-join moment onto the new shelf."""
        evs = self.ladder("s1", T0)
        start = len(evs)  # Play turned on here
        same = answer(T0 + timedelta(days=11), sid="s1", item="s1-same-second")
        self.assertEqual(same[-1]["at"], evs[-1]["at"])
        ms = play.moments(evs + same, {"t-1": self.pkg}, 20, iso(T0), None, start)
        self.assertEqual([m for m in ms if m["kind"] == "relearned"], [])
        later = self.ladder("s2", T0 + timedelta(days=30), tag="s2-new")
        ms = play.moments(evs + same + later, {"t-1": self.pkg}, 20, iso(T0), None, start)
        self.assertEqual([m["skill"] for m in ms if m["kind"] == "relearned"], ["s2"])

    def test_a_domain_moment_is_made_by_its_last_answer_in_the_record(self):
        """Two skills relearned in the same whole second, one before joining and one after: the domain moment
        is made after joining, whichever order the skills are listed in."""
        base = self.ladder("s3", T0, tag="s3")
        for first, second in (("s1", "s2"), ("s2", "s1")):
            pre = self.ladder(first, T0, tag=first)
            post = self.ladder(second, T0, tag=second)
            self.assertEqual(pre[-1]["at"], post[-1]["at"])
            evs = base + pre + post[:-3]
            ms = play.moments(evs + post[-3:], {"t-1": self.pkg}, 20, iso(T0), None, len(evs))
            self.assertEqual(sorted((m["kind"], m["skill"] or m["skill_text"]) for m in ms
                                    if m["kind"] not in ("later", "changed")),
                             [("domain", "Domain A"), ("exam", ""), ("relearned", second)], first)

    def test_since_counts_only_moments_made_from_then_on(self):
        evs = self.ladder("s1", T0)
        self.assertEqual(self.shelf(evs, since=iso(T0 + timedelta(days=12))), [])
        self.assertEqual(len(self.shelf(evs, since=iso(T0 + timedelta(days=5)))), 1)  # the third success is later
        start = len(evs)
        self.assertEqual([m for m in play.moments(evs, {"t-1": self.pkg}, 20, iso(T0), None, start)], [])

    def test_a_package_without_domains_has_skill_moments_only(self):
        self.assertEqual(sorted(m["kind"] for m in play.moments(self.ladder("s1", T0), PACKAGES, 20)), ["later", "relearned"])


class ShownMomentTests(unittest.TestCase):
    def test_every_moment_view_shows_its_support_and_limit(self):
        """ADR 0010: wherever a moment is shown, so are what made it and "Not a certification" (PR #35 review)."""
        js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        for name in ("shelfPanel", "momentCard"):
            body = re.search(rf"\nfunction {name}\(.*?\n}}\n", js.replace("\r\n", "\n"), re.S).group(0)
            self.assertIn("m.made_by", body, name)
            self.assertIn("m.line", body, name)
        renders = re.findall(r"\nfunction (\w+)\([^)]*\) \{(?:(?!\nfunction ).)*?\.moments\b(?:(?!\nfunction ).)*?\bm\.title\b",
                             js.replace("\r\n", "\n"), re.S)
        self.assertEqual(sorted(set(renders)), ["shelfPanel"])  # every other view renders through momentCard
        self.assertIn("Not a certification", play.NOT_A_CERTIFICATE)


class BoundaryTests(unittest.TestCase):
    """Play counts from a position in the Record, not a time: times are whole seconds (PR #35 review)."""

    def setUp(self):
        t = T(2026, 9, 29, 9)
        self.old = answer(t) + [ev("credential.reported", t, package="t-1", exam="T-1", title="Test exam",
                                   earned="2026-09-28")]
        self.new = answer(t)  # sent in the very same second, after Play was turned on again
        self.events = self.old + self.new
        for i, e in enumerate(self.events):
            e["hash"] = f"h{i}"
        self.settings = {**doc("2026-09-29"), "since": iso(t), "after": self.old[-1]["hash"]}

    def test_same_second_counts_only_later_events(self):
        start = play.boundary(self.events, self.settings)
        self.assertEqual(start, len(self.old))
        p = play.points(self.events, date(2026, 10, 1), self.settings["since"], start)
        self.assertEqual(p["by_kind"], {"study_day": 10, "answers": 3, "lessons": 0, "labs": 0})
        self.assertEqual(play.moments(self.events, PACKAGES, 20, self.settings["since"], None, start), [])
        # What a time alone would do: the old answer and the old pass come back.
        p = play.points(self.events, date(2026, 10, 1), self.settings["since"])
        self.assertEqual(p["by_kind"]["answers"], 6)
        self.assertEqual([m["kind"] for m in play.moments(self.events, PACKAGES, 20, self.settings["since"])], ["passed"])

    def test_a_mark_that_is_gone_keeps_pre_join_events_out(self):
        """PR #36 review: a non-empty mark missing from the Record (restored or changed while play.json
        survived) fails closed to the time filter; it never counts from position 0."""
        t = T(2026, 9, 29, 9)
        before = answer(t - timedelta(hours=1)) + [ev("credential.reported", t - timedelta(hours=1), package="t-1",
                                                       exam="T-1", title="Test exam", earned="2026-09-28")]
        events = before + answer(t + timedelta(hours=1))
        for i, e in enumerate(events):
            e["hash"] = f"r{i}"  # a Record rebuilt since: the join mark is not in it
        settings = {**doc("2026-09-29"), "since": iso(t), "after": "h-not-here"}
        start = play.boundary(events, settings)
        self.assertEqual(start, len(before))
        p = play.points(events, date(2026, 10, 1), settings["since"], start)
        self.assertEqual(p["by_kind"], {"study_day": 10, "answers": 3, "lessons": 0, "labs": 0})
        self.assertEqual(play.moments(events, PACKAGES, 20, settings["since"], None, start), [])
        # Counting from 0 would bring the pre-join answer and pass back.
        self.assertEqual(play.points(events, date(2026, 10, 1), settings["since"], 0)["by_kind"]["answers"], 6)
        self.assertEqual([m["kind"] for m in play.moments(events, PACKAGES, 20, None, None, 0)], ["passed"])

    def test_marks(self):
        self.assertIsNone(play.boundary(self.events, {**self.settings, "since": None, "after": None}))
        self.assertEqual(play.boundary(self.events, {**self.settings, "after": ""}), 0)
        legacy = {k: v for k, v in self.settings.items() if k != "after"}  # a play.json from before the mark
        later = next(i for i, e in enumerate(self.events) if e["at"] > self.settings["since"])  # strictly later
        self.assertEqual(play.boundary(self.events, legacy), later)
        self.assertEqual(play.boundary(self.events, {**self.settings, "after": "gone"}), later)
        self.assertIsNone(play.boundary(self.events, None))
        moments = play.moments(self.events, PACKAGES, 20, None, None, 0)
        self.assertTrue(all(not k.startswith("_") for m in moments for k in m))


class RoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-play-")
        cls.app = create_app(settings(cls.tmp))
        cls.room = cls.app.state.play
        cls.store = cls.app.state.dojo.store
        cls.pid = next(iter(cls.app.state.dojo.packages.by_id))

    def learner(self, name: str) -> Learner:
        lid = f"test:{name}:{new_id('x')}"
        return Learner(lid, learner_key(lid), name)

    def files(self, who: Learner) -> tuple:
        return (self.store.read("learners", who.key, "social", "play"),
                self.store.read("learners", who.key, "social", "play-offer"))

    def test_off_by_default_and_offer(self):
        a = self.learner("a")
        self.assertEqual(self.room.view(a), {"on": False, "offer": True})
        self.assertEqual(self.room.dismiss_offer(a), {"on": False, "offer": False})
        self.assertEqual(self.files(a)[0], None)

    def test_view_shows_relearned_and_domain_moments_and_reads_gone_skills(self):
        """PlayRoom.view re-derives Know's ladder from the Record and asks the Dojo which skills are gone."""
        a, b = self.learner("a"), self.learner("b")
        pkg = self.app.state.dojo.packages.by_id[self.pid]
        d1 = pkg["domains"][0]["id"]
        sid = next(s for s, x in pkg["skills"].items() if x["domain"] == d1 and x["sources"])
        others = {s for s, x in pkg["skills"].items() if x["domain"] == d1 and s != sid}
        self.room.join(a, True, None)
        self.room.join(b, True, None)
        times = iter(iso(T0 + timedelta(days=d, seconds=s)) for d in (0, 1, 4, 11) for s in (0, 30, 32))
        clock = lambda *args: next(times)  # noqa: E731
        with mock.patch("app.core.iso", clock):
            for d in (0, 1, 4, 11):
                item = f"it-{d}"
                base = {"package": self.pid, "skill": sid, "item": item}
                self.store.append_event(a.key, "item.issued", {**base, "mode": "probe", "kind": "x"})
                self.store.append_event(a.key, "answer.submitted", {**base, "elapsed_s": 30, "input": "typed"})
                self.store.append_event(a.key, "answer.judged", {**base, "met": 2, "total": 2, "mode": "probe"})
        kinds = lambda v: sorted(m["kind"] for m in v["moments"] if m["kind"] not in ("later", "changed"))  # noqa: E731
        self.assertEqual(kinds(self.room.view(a)), ["relearned"] if others else ["domain", "relearned"])
        with mock.patch.object(self.app.state.dojo, "gone_skills", lambda pid: others if pid == self.pid else set()):
            v = self.room.view(a)
            self.assertEqual(kinds(v), ["domain", "relearned"])
            dom = next(m for m in v["moments"] if m["kind"] == "domain")
            self.assertEqual((dom["skill_text"], dom["line"]), (pkg["domains"][0]["title"], play.NOT_A_CERTIFICATE))
            self.assertEqual(self.room.view(b)["moments"], [])  # another learner's ladder never shows
        with mock.patch.object(self.app.state.dojo, "gone_skills", side_effect=RuntimeError("drift down")):
            self.assertEqual(kinds(self.room.view(a)), ["relearned"] if others else ["domain", "relearned"])
        self.assertIsNone(self.files(a)[0].get("moments"))  # nothing about moments is stored
        self.room.leave(a)
        self.room.leave(b)

    def test_join_leave_rejoin(self):
        a = self.learner("a")
        self.room.dismiss_offer(a)
        v = self.room.join(a, False, None)
        self.assertTrue(v["on"])
        self.assertIsNotNone(v["since"])
        self.assertIsNone(v["rhythm"])  # the rhythm stays off until a target is picked
        self.assertEqual(v["moments"], [])
        self.assertIsNone(self.files(a)[1])
        with self.assertRaises(HTTPException):
            self.room.join(a, False, 3)
        before = len(self.store.events(a.key))
        self.assertEqual(self.room.leave(a), {"on": False, "offer": True})
        self.assertEqual(self.files(a), (None, None))
        self.assertEqual(len(self.store.events(a.key)), before)  # the Record is untouched
        again = self.room.join(a, False, 4)
        self.assertGreaterEqual(again["since"], v["since"])
        self.assertEqual(again["rhythm"]["target"], 4)
        self.assertEqual(again["rhythm"]["weeks_met"], 0)

    def test_include_record_and_invalid_targets(self):
        a = self.learner("a")
        self.assertIsNone(self.room.join(a, True, 3)["since"])
        b = self.learner("b")
        for bad in (2, 7):
            with self.assertRaises(HTTPException):
                self.room.join(b, False, bad)
        self.assertIsNone(self.files(b)[0])

    def test_target_change_applies_next_monday(self):
        a = self.learner("a")
        self.room.today = lambda: date(2026, 9, 30)
        try:
            self.room.join(a, False, 3)
            v = self.room.update(a, 5, None, None)
            self.assertEqual((v["rhythm"]["target"], v["rhythm"]["next_target"]), (3, 5))
            self.assertEqual(self.files(a)[0]["rhythm"]["targets"],
                             [{"from": "2026-09-30", "days": 3}, {"from": "2026-10-05", "days": 5}])
            v = self.room.update(a, 3, None, None)  # back to this week's target: nothing pending
            self.assertIsNone(v["rhythm"]["next_target"])
            v = self.room.update(a, None, False, None)
            self.assertFalse(v["rhythm"]["on"])
        finally:
            del self.room.today

    def test_pass_hide_and_show(self):
        a = self.learner("a")
        self.room.join(a, False, None)
        today = play.local_day(play.utcnow()).isoformat()
        r = self.room.report_pass(a, self.pid, today)
        self.assertEqual(r["earned"], today)
        self.room.report_pass(a, self.pid, today)  # the same day again: recorded once
        reported = [e for e in self.store.events(a.key) if e["type"] == "credential.reported"]
        self.assertEqual(len(reported), 1)
        self.assertEqual(set(reported[0]["data"]), {"package", "exam", "title", "earned", "source", "checked_at"})
        v = self.room.view(a)
        self.assertEqual([m["kind"] for m in v["moments"]], ["passed"])
        self.assertIn("Self-reported", v["moments"][0]["line"])
        v = self.room.hide(a, v["moments"][0]["id"])
        self.assertEqual((v["moments"], v["hidden"]), ([], 1))
        v = self.room.update(a, None, None, True)
        self.assertEqual((len(v["moments"]), v["hidden"]), (1, 0))
        future = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
        for bad in (future, "2014-12-31", "31.12.2025"):
            with self.assertRaises(HTTPException):
                self.room.report_pass(a, self.pid, bad)

    def test_saving_a_target_turns_the_weekly_target_back_on(self):
        """Gemini review of PR #35: after "Turn the weekly target off", saving a target turns it on again."""
        a = self.learner("a")
        self.room.join(a, False, 3)
        self.assertFalse(self.room.update(a, None, False, None)["rhythm"]["on"])
        for days in (3, 5):  # the same target and a changed one
            self.room.update(a, None, False, None)
            v = self.room.update(a, days, None, None)
            self.assertTrue(v["rhythm"]["on"], days)
            self.assertTrue(self.files(a)[0]["rhythm"]["on"])
        self.assertFalse(self.room.update(a, 4, False, None)["rhythm"]["on"])  # an explicit "off" still wins
        self.assertTrue(self.room.update(a, 4, True, None)["rhythm"]["on"])  # what the Save button sends
        b = self.learner("b")
        self.room.join(b, False, None)
        self.assertTrue(self.room.update(b, 4, None, None)["rhythm"]["on"])  # the first target, as before

    def test_pass_again_after_rejoin(self):
        a = self.learner("a")
        self.room.join(a, False, None)
        today = play.local_day(play.utcnow()).isoformat()
        self.room.report_pass(a, self.pid, today)
        reported_at = self.store.events(a.key)[-1]["at"]
        self.room.leave(a)
        # Rejoin within the same second as the old report: `since` equals its time, yet nothing comes back.
        with mock.patch.object(play, "iso", lambda *args: reported_at):
            v = self.room.join(a, False, None)
        self.assertEqual(v["since"], reported_at)
        self.assertEqual((v["moments"], v["hidden"]), ([], 0))  # rejoin starts empty
        self.room.report_pass(a, self.pid, today)  # an explicit choice to put it back
        self.assertEqual([m["kind"] for m in self.room.view(a)["moments"]], ["passed"])
        self.room.report_pass(a, self.pid, today)
        self.assertEqual(len([e for e in self.store.events(a.key) if e["type"] == "credential.reported"]), 2)

    def test_isolation(self):
        a, b = self.learner("a"), self.learner("b")
        self.room.join(a, False, 3)
        self.room.join(b, False, 5)
        self.room.report_pass(a, self.pid, play.local_day(play.utcnow()).isoformat())
        self.assertEqual(len(self.room.view(a)["moments"]), 1)
        vb = self.room.view(b)
        self.assertEqual((vb["moments"], vb["rhythm"]["target"]), ([], 5))
        self.room.leave(b)
        self.assertIsNotNone(self.files(a)[0])
        self.assertEqual(len(self.room.view(a)["moments"]), 1)

    def test_moments_through_the_room(self):
        a = self.learner("a")
        sid = next(s for s, x in self.app.state.dojo.packages.by_id[self.pid]["skills"].items() if x["kind"] == "scenario")
        events = [ev("item.issued", T0, item="it-r", package=self.pid, skill=sid, mode="probe", changed_condition=True),
                  ev("answer.submitted", T0 + timedelta(seconds=40), item="it-r", package=self.pid, skill=sid),
                  ev("answer.judged", T0 + timedelta(seconds=42), item="it-r", package=self.pid, skill=sid, met=2, total=2)]
        self.store.events = lambda key, _orig=self.store.events: events if key == a.key else _orig(key)
        try:
            self.store.write("learners", a.key, "social", "play", value=doc("2026-08-01", since=None))
            v = self.room.view(a)
            self.assertEqual([m["kind"] for m in v["moments"]], ["changed"])
            self.store.write("learners", a.key, "social", "play", value=doc("2026-09-02"))
            self.assertEqual(self.room.view(a)["moments"], [])  # made before `since`
        finally:
            del self.store.events


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-play-api-")
        cls.c = TestClient(create_app(settings(cls.tmp)))

    def setUp(self):
        # One local learner for every test here: each starts from an empty Record with Play off,
        # so a failure in one test cannot leave state that breaks the next.
        self.c.post("/api/play/leave", headers=POST)
        self.c.post("/api/record/delete", json={"confirm": "DELETE"}, headers=POST)

    def test_flow(self):
        c = self.c
        self.assertEqual(c.get("/api/play").json(), {"on": False, "offer": True})
        self.assertEqual(c.post("/api/play/join", json={"days": 3}).status_code, 403)  # needs the X-Dojo header
        self.assertEqual(c.post("/api/play/join", json={"days": 7}, headers=POST).status_code, 422)
        v = c.post("/api/play/join", json={"days": 3}, headers=POST).json()
        self.assertTrue(v["on"])
        self.assertEqual(c.post("/api/play/join", json={}, headers=POST).status_code, 409)
        self.assertEqual(c.put("/api/play/settings", json={"days": 2}, headers=POST).status_code, 422)
        self.assertEqual(c.put("/api/play/settings", json={"days": 4}, headers=POST).status_code, 200)
        self.assertFalse(c.put("/api/play/settings", json={"rhythm_on": False}, headers=POST).json()["rhythm"]["on"])
        self.assertTrue(c.put("/api/play/settings", json={"days": 4, "rhythm_on": True}, headers=POST).json()["rhythm"]["on"])
        js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('playApi("/settings", "PUT", { days: Number(target.value), rhythm_on: true })', js)
        today = c.get("/api/play").json()["today"]
        self.assertEqual(c.post("/api/passes", json={"package": "nope", "earned": today}, headers=POST).status_code, 404)
        r = c.post("/api/passes", json={"package": "gh-300", "earned": today, "score": 900}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        mid = c.get("/api/play").json()["moments"][0]["id"]
        self.assertEqual(c.delete("/api/play/moments/not-an-id", headers=POST).status_code, 404)
        self.assertEqual(c.delete(f"/api/play/moments/{mid}", headers=POST).json()["hidden"], 1)
        data = c.get("/api/record/export").json()
        self.assertEqual(data["play"]["settings"]["hidden"], [mid])
        reported = [e["data"] for e in data["events"] if e["type"] == "credential.reported"]
        self.assertEqual(len(reported), 1)
        self.assertEqual(set(reported[0]), {"package", "exam", "title", "earned", "source", "checked_at"})  # no score
        self.assertNotIn(900, reported[0].values())
        self.assertNotIn("900", [str(v) for v in reported[0].values()])
        self.assertEqual(c.post("/api/play/leave", headers=POST).json(), {"on": False, "offer": True})
        self.assertIsNone(c.get("/api/record/export").json()["play"]["settings"])
        c.post("/api/play/join", json={}, headers=POST)
        c.post("/api/record/delete", json={"confirm": "DELETE"}, headers=POST)
        self.assertEqual(c.get("/api/play").json(), {"on": False, "offer": True})

    def test_empty_record_join_counts_a_pass_in_the_same_second(self):
        """An empty Record at join means "count every event from position 0", even within one second."""
        c = self.c
        fixed = "2026-10-02T07:00:00+00:00"
        clock = lambda *args, **kwargs: fixed  # noqa: E731
        with mock.patch.object(play, "iso", clock), mock.patch("app.core.iso", clock):
            v = c.post("/api/play/join", json={}, headers=POST).json()
            self.assertEqual(v["since"], fixed)
            self.assertEqual(c.get("/api/record/export").json()["play"]["settings"]["after"], "")
            r = c.post("/api/passes", json={"package": "gh-300", "earned": "2026-10-01"}, headers=POST)
            self.assertEqual(r.status_code, 200, r.text)
            events = c.get("/api/record/export").json()["events"]
            self.assertEqual([e["at"] for e in events], [fixed])  # the pass is in the very same second
            self.assertEqual([m["kind"] for m in c.get("/api/play").json()["moments"]], ["passed"])

    def test_log_names_no_moment(self):
        c = self.c
        c.post("/api/play/join", json={}, headers=POST)
        c.post("/api/passes", json={"package": "gh-300", "earned": c.get("/api/play").json()["today"]}, headers=POST)
        mid = c.get("/api/play").json()["moments"][0]["id"]
        with self.assertLogs("dojo", level="INFO") as logs:
            c.delete(f"/api/play/moments/{mid}", headers=POST)
        self.assertTrue(any("/api/play/moments/-" in line for line in logs.output))
        self.assertNotIn(mid, "\n".join(logs.output))
        c.post("/api/play/leave", headers=POST)


if __name__ == "__main__":
    unittest.main()
