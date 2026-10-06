"""Exam realism (ADR 0012): the real exam's question types, its sections, its clock and its open-book rule.

The rules these tests hold to: every new kind is written with a quote for every part of its key, and an
item whose quote is not on the page, or that the checker rejects, is dropped; scoring gives a point per
right part and takes nothing off; a yes/no statement cannot be changed once passed, and a series or a case
study is locked once left; Microsoft Learn is allowed only where the real exam allows it, and Dojo counts
lookups and time away; the answer key stays hidden while an exam can be taken; no selected-response answer
fills a pip; and every learner's attempts and pool are their own.
"""
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import exam, itemwriter  # noqa: E402
from app import items as exam_items  # noqa: E402
from app.core import APP_DIR, Learner, learner_key, iso, utcnow  # noqa: E402
from app.evidence import derive  # noqa: E402
from app.main import create_app  # noqa: E402
from app.packages import Packages  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

PACKAGES = Packages(APP_DIR / "packages")
SRC = {"url": "https://learn.microsoft.com/x", "title": "X"}


def _q(part: str) -> dict:
    return {"part": part, "quote": f"A sentence for {part}.", "source": SRC}


# Hand-made items of every kind, with their keys, for the scoring and answer rules.
SINGLE = {"stem": "Which?", "options": ["a", "b", "c", "d"], "answer": 2, "quote": "q", "source": SRC}
MULTI = {"kind": "multi", "stem": "Which two?", "options": ["a", "b", "c", "d", "e"], "answers": [1, 3], "choose": 2,
         "quotes": [_q("correct option 1"), _q("correct option 2")]}
SEQUENCE = {"kind": "sequence", "stem": "Order?", "options": ["s1", "s2", "x", "s3"], "order": [1, 0, 3],
            "quotes": [_q("step 1"), _q("step 2"), _q("step 3")]}
MATCH = {"kind": "match", "stem": "Match", "targets": ["t1", "t2", "t3"], "options": ["v1", "v2", "v3", "v4"],
         "pairs": [2, 0, 1], "quotes": [_q("t1"), _q("t2"), _q("t3")]}
HOT = {"kind": "hotarea", "stem": "Fill", "text": "a: [[1]]\nb: [[2]]",
       "blanks": [{"options": ["x", "y", "z"]}, {"options": ["p", "q", "r"]}], "blank_answers": [1, 2],
       "quotes": [_q("drop-down 1"), _q("drop-down 2")]}
YESNO = {"kind": "yesno", "stem": "Goal?", "statements": ["s1", "s2", "s3"], "yes": [True, False, True],
         "quotes": [_q("statement 1"), _q("statement 2"), _q("statement 3")]}
CASE = {"kind": "case", "title": "Contoso", "stem": "Read.", "exhibits": [{"title": "A", "text": "a"}, {"title": "B", "text": "b"}],
        "questions": [dict(SINGLE, kind="single", quotes=[_q("question 1, the answer")]),
                      dict(MULTI, quotes=[_q("question 2, correct option 1"), _q("question 2, correct option 2")]),
                      dict(SINGLE, kind="single", answer=0, quotes=[_q("question 3, the answer")]),
                      dict(SINGLE, kind="single", answer=1, quotes=[_q("question 4, the answer")])],
        "quote": "A sentence for question 1, the answer.", "source": SRC}
ALL = {"single": SINGLE, "multi": MULTI, "sequence": SEQUENCE, "match": MATCH, "hotarea": HOT, "yesno": YESNO, "case": CASE}


def _r(response: dict) -> dict:
    return {"response": response}


def _keys_anywhere(value) -> set[str]:
    """Every answer-key field name found at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("answer", "answers", "order", "pairs", "blank_answers", "yes", "rationale", "quotes"):
                found.add(k)
            found |= _keys_anywhere(v)
    elif isinstance(value, list):
        for v in value:
            found |= _keys_anywhere(v)
    return found


class ItemRulesTests(unittest.TestCase):
    """Scoring, answers and what the page may see, one kind at a time (app/items.py)."""

    def test_a_point_per_right_part_and_nothing_taken_off(self):
        cases = [
            (MULTI, _r({"choices": [1, 3]}), 2, 2), (MULTI, _r({"choices": [1, 2]}), 1, 2), (MULTI, _r({"choices": [0, 2]}), 0, 2),
            (SEQUENCE, _r({"order": [1, 0, 3]}), 1, 1), (SEQUENCE, _r({"order": [1, 0]}), 0, 1),
            (SEQUENCE, _r({"order": [0, 1, 3]}), 0, 1),   # a build list is right only as a whole
            (MATCH, _r({"pairs": [2, 0, 0]}), 2, 3), (MATCH, _r({"pairs": [None, None, 1]}), 1, 3),
            (HOT, _r({"picks": [1, 0]}), 1, 2), (HOT, _r({"picks": [1, 2]}), 2, 2),
            (YESNO, _r({"yes": [True, True, None]}), 1, 3), (YESNO, _r({"yes": [True, False, True]}), 3, 3),
            (CASE, _r({"parts": {"0": {"choice": 2}, "1": {"choices": [1, 4]}, "3": {"choice": 0}}}), 2, 5),
        ]
        for it, given, got, out_of in cases:
            with self.subTest(kind=it["kind"], given=given):
                self.assertEqual(exam_items.points(it, given), got)
                self.assertEqual(exam_items.max_points(it), out_of)
                self.assertEqual(exam_items.full(it, given), got == out_of)
                self.assertGreaterEqual(exam_items.points(it, given), 0, "a wrong part costs nothing")

    def test_an_item_written_before_the_kinds_scores_as_before(self):
        self.assertEqual(exam_items.kind_of(SINGLE), "single")
        self.assertEqual((exam_items.points(SINGLE, {"choice": 2}), exam_items.points(SINGLE, {"choice": 1})), (1, 0))
        self.assertEqual((exam_items.size(SINGLE), exam_items.max_points(SINGLE)), (1, 1))
        self.assertEqual(exam_items.apply(SINGLE, None, {"choice": 3}), {"choice": 3})
        self.assertEqual({k: exam_items.shown(SINGLE)[k] for k in ("stem", "options")}, exam_items.public(SINGLE))

    def test_sizes_count_questions(self):
        self.assertEqual({k: exam_items.size(it) for k, it in ALL.items()},
                         {"single": 1, "multi": 1, "sequence": 1, "match": 1, "hotarea": 1, "yesno": 3, "case": 4})

    def test_taking_every_part_back_leaves_no_answer(self):
        self.assertEqual(exam_items.apply(MULTI, None, {"choices": []}), {})
        self.assertEqual(exam_items.apply(SEQUENCE, None, {"order": []}), {})
        self.assertEqual(exam_items.apply(MATCH, None, {"pairs": [None, None, None]}), {})
        self.assertEqual(exam_items.apply(HOT, None, {"picks": [None, None]}), {})
        given = exam_items.apply(CASE, None, {"choices": [1]}, part=1)
        self.assertEqual(given, _r({"parts": {"1": {"choices": [1]}}}))
        self.assertEqual(exam_items.apply(CASE, given, {"choices": []}, part=1), {}, "the last part taken back")

    def test_a_yes_no_statement_cannot_be_changed_once_passed(self):
        given = exam_items.apply(YESNO, None, {"yes": True}, part=0)
        given = exam_items.apply(YESNO, given, {"yes": False}, part=1)
        with self.assertRaises(LookupError):
            exam_items.apply(YESNO, given, {"yes": True}, part=0)
        with self.assertRaises(LookupError):
            exam_items.apply(YESNO, given, {"yes": True}, part=1)
        self.assertEqual(exam_items.apply(YESNO, given, {"yes": True}, part=2), _r({"yes": [True, False, True]}))

    def test_yes_no_statements_are_answered_one_at_a_time_in_order(self):
        # Review 2 (Gemini): answering statement 3 first would have locked 1 and 2 unanswered.
        for part in (1, 2):
            with self.subTest(part=part), self.assertRaises(ValueError) as e:
                exam_items.apply(YESNO, None, {"yes": True}, part=part)
            self.assertIn("Answer statement 1 first", str(e.exception))
        given = exam_items.apply(YESNO, None, {"yes": False}, part=0)
        with self.assertRaises(ValueError):
            exam_items.apply(YESNO, given, {"yes": True}, part=2)
        self.assertEqual(exam_items.apply(YESNO, given, {"yes": True}, part=1), _r({"yes": [False, True, None]}))

    def test_answers_the_item_cannot_take_are_refused(self):
        bad = [(MULTI, {"choices": [0, 1, 2]}, None), (MULTI, {"choices": [1, 1]}, None), (MULTI, {"choices": [9]}, None),
               (SEQUENCE, {"order": [0, 0]}, None), (MATCH, {"pairs": [0, 1]}, None), (HOT, {"picks": [0, 7]}, None),
               (YESNO, {"yes": "yes"}, 0), (YESNO, {"yes": True}, 5), (CASE, {"choice": 9}, 0)]
        for it, raw, part in bad:
            with self.subTest(kind=it["kind"], raw=raw), self.assertRaises(ValueError):
                exam_items.apply(it, None, raw, part)

    def test_a_short_list_of_nothing_does_not_clear_a_saved_answer(self):
        # Review 1, finding 4: the length is checked before an all-empty list counts as taking the answer back.
        saved_match, saved_hot = _r({"pairs": [2, 0, 1]}), _r({"picks": [1, 2]})
        for it, given, raw in ((MATCH, saved_match, {"pairs": [None]}), (MATCH, saved_match, {"pairs": [None] * 4}),
                               (HOT, saved_hot, {"picks": [None]}), (HOT, saved_hot, {"picks": []})):
            with self.subTest(kind=it["kind"], raw=raw), self.assertRaises(ValueError):
                exam_items.apply(it, given, raw)
        self.assertEqual(exam_items.apply(MATCH, saved_match, {"pairs": [None, None, None]}), {})
        self.assertEqual(exam_items.apply(HOT, saved_hot, {"picks": [None, None]}), {})

    def test_what_the_page_sees_has_no_key(self):
        for kind, it in ALL.items():
            with self.subTest(kind=kind):
                self.assertEqual(_keys_anywhere(exam_items.shown(it)), set())
                self.assertEqual(_keys_anywhere(exam_items.public(it)), set())
        shown = exam_items.shown(CASE)
        self.assertEqual([q["kind"] for q in shown["questions"]], ["single", "multi", "single", "single"])
        self.assertEqual(shown["questions"][1]["choose"], 2, "the page says how many to choose")

    def test_a_case_study_lists_each_quote_once(self):
        parts = [x["part"] for x in exam_items.quotes(CASE)]
        self.assertEqual(parts, ["question 1, the answer", "question 2, correct option 1", "question 2, correct option 2",
                                 "question 3, the answer", "question 4, the answer"])

    def test_the_mix(self):
        full = exam_items.mix(50)
        self.assertEqual(full, {"single": 29, "multi": 5, "sequence": 3, "match": 3, "hotarea": 3, "yesno": 1, "case": 1})
        self.assertEqual(exam_items.questions_in(full), 50)
        for n in (1, 9, 10, 11, 12, 15, 29, 30, 40, 60, 100):
            m = exam_items.mix(n)
            self.assertEqual(exam_items.questions_in(m), n, n)
        self.assertEqual({k for k, n in exam_items.mix(9).items() if n}, {"single"}, "below 10 questions only single choice")
        self.assertEqual(exam_items.mix(29).get("case", 0), 0, "a case study needs a 30-question exam")
        self.assertEqual(exam_items.mix(11).get("yesno", 0), 0, "a yes/no series needs 12 questions")
        self.assertEqual(exam_items.questions_in(exam_items.POOL_MIX), exam.POOL_TARGET)


class DurationAndOpenBookTests(unittest.TestCase):
    """The clock and the open-book rule come from what Microsoft and GitHub state (ADR 0012)."""

    @staticmethod
    def pkg(issuer: str, level: str | None = None, minutes: int | None = None, code: str = "XX-100") -> dict:
        fmt = {k: v for k, v in {"level": level, "duration_minutes": minutes}.items() if v is not None}
        return {"meta": {"issuer": issuer, "exam": code, "exam_format": fmt}}

    def test_a_stated_duration_wins(self):
        self.assertEqual(exam.duration(self.pkg("Microsoft", "associate", 120)), (120, "stated"))
        self.assertEqual(exam.duration(PACKAGES.get("dp-800")), (120, "stated"))

    def test_without_one_microsofts_typical_length_for_the_level(self):
        self.assertEqual(exam.duration(self.pkg("Microsoft", "associate")), (100, "typical"))
        self.assertEqual(exam.duration(self.pkg("Microsoft", "expert")), (100, "typical"))
        self.assertEqual(exam.duration(self.pkg("Microsoft", "fundamentals")), (45, "typical"))

    def test_a_missing_fact_is_not_filled_in(self):
        self.assertEqual(exam.duration(self.pkg("Microsoft")), (0, ""), "no level, no guess")
        self.assertEqual(exam.duration(self.pkg("GitHub")), (0, ""), "a GitHub exam without a stated duration stays untimed")

    def test_learn_is_allowed_only_in_associate_and_expert_exams(self):
        self.assertTrue(exam.open_book(self.pkg("Microsoft", "associate"))["allowed"])
        self.assertTrue(exam.open_book(self.pkg("Microsoft", "expert"))["allowed"])
        self.assertTrue(exam.open_book(PACKAGES.get("dp-800"))["allowed"], "DP-800 is an associate exam")
        for pkg, why in ((self.pkg("Microsoft", "fundamentals"), "fundamentals"), (self.pkg("GitHub", "associate"), "GitHub"),
                         (PACKAGES.get("gh-300"), "GitHub"), (self.pkg("Microsoft"), "does not know")):
            with self.subTest(why=why):
                rule = exam.open_book(pkg)
                self.assertFalse(rule["allowed"])
                self.assertIn(why, rule["reason"])
                self.assertEqual(rule["source"], exam.EXAM_DURATION_PAGE)

    def test_the_learn_line_says_what_was_counted(self):
        line = exam.learn_line({"learn": {"lookups": 3, "away_seconds": 360}})
        self.assertIn("3 lookups", line)
        self.assertIn("about 6 minutes", line)
        self.assertIn("less than a minute", exam.learn_line({"learn": {"lookups": 1, "away_seconds": 20}}))
        self.assertIn("1 lookup,", exam.learn_line({"learn": {"lookups": 1, "away_seconds": 20}}))


class _Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-realism-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.room = cls.app.state.exams
        cls.dojo = cls.app.state.dojo
        s = cls.app.state.settings
        oid = "local:dev" if s.local else f"{s.owner_tid}:{s.owner_oid}"
        cls.learner = Learner(oid, learner_key(oid), "Owner")
        cls.dojo.store.path("learners", cls.learner.key).mkdir(parents=True, exist_ok=True)

    def post(self, path: str, body: dict | None = None, status: int = 200) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def wait(self, job: dict) -> dict:
        for _ in range(600):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def close_open(self) -> None:
        for pid in ("gh-300", "dp-800"):
            for a in self.c.get(f"/api/exams?package={pid}").json()["attempts"]:
                if a["state"] in ("open", "ready"):
                    if a["state"] == "ready":
                        self.post(f"/api/exams/{a['id']}/start")
                    self.post(f"/api/exams/{a['id']}/submit", {"reason": "submitted"})

    def started(self, package: str = "gh-300", length: str = "full", style: str = "exam", open_book: bool = False) -> tuple[str, dict]:
        self.close_open()
        job = self.post("/api/exams", {"package": package, "length": length, "style": style, "open_book": open_book})
        self.wait(job)
        return job["attempt"], self.post(f"/api/exams/{job['attempt']}/start")

    def first(self, view: dict, kind: str) -> int:
        return next(i for i, it in enumerate(view["items"]) if it.get("kind", "single") == kind)

    def doc(self, aid: str) -> dict:
        return self.room.attempt(self.learner, aid)


class WriterTests(_Client):
    """Each new kind is written and checked like single choice, with a quote for every part of its key."""

    def setUp(self):
        self.pkg = PACKAGES.get("gh-300")
        self.skill = exam.plan_skills(self.pkg, 1)[0]

    def test_each_quote_moves_with_its_option_when_the_options_are_shuffled(self):
        # Review 1, finding 1: the correct options were sorted after the shuffle but their quotes were not.
        class Dojo:
            def quote_ok(self, g, quote, n):
                return True

            def source_refs(self, g):
                return [{"n": 1, "url": SRC["url"]}]

        class Reverse(random.Random):
            def shuffle(self, x):
                x.reverse()

        raw = {"stem": "Which two?", "options": ["Opt zero", "Opt one", "Opt two", "Opt three", "Opt four"],
               "correct": [{"option": 0, "quote": "The quote that supports option zero.", "source": 1},
                           {"option": 3, "quote": "The quote that supports option three.", "source": 1}]}
        for where in ("", "question 2, "):
            with self.subTest(where=where):
                it = itemwriter.ItemWriter(Dojo())._choice(dict(raw), [], "multi", Reverse(), where)
                self.assertEqual(it["answers"], [1, 4])
                self.assertEqual([it["options"][a] for a in it["answers"]], ["Opt three", "Opt zero"])
                self.assertEqual([q["quote"] for q in it["quotes"]],
                                 ["The quote that supports option three.", "The quote that supports option zero."])
                self.assertEqual([q["part"] for q in it["quotes"]], [f"{where}correct option 1", f"{where}correct option 2"])
                self.assertEqual(it["quote"], "The quote that supports option three.")
                said = itemwriter.ItemWriter.describe(dict(it, rationale=""))
                self.assertIn('Correct: B (quote from source 1: "The quote that supports option three.")', said)
                self.assertIn('Correct: E (quote from source 1: "The quote that supports option zero.")', said)

    def test_every_kind_has_a_quote_for_every_part_of_its_key(self):
        parts = {"multi": lambda it: 2, "sequence": lambda it: len(it["order"]), "match": lambda it: len(it["targets"]),
                 "hotarea": lambda it: len(it["blanks"]), "yesno": lambda it: 3, "case": lambda it: 5}
        for kind in exam_items.NEW_KINDS:
            with self.subTest(kind=kind):
                items, lost = self.dojo.write_items("gh-300", kind, [self.skill])
                self.assertEqual((len(items), lost), (1, []))
                it = items[0]
                self.assertEqual((exam_items.kind_of(it), it["skill"]), (kind, self.skill))
                quotes = exam_items.quotes(it)
                self.assertEqual(len(quotes), parts[kind](it))
                g = self.dojo.grounding("gh-300", self.skill,
                                        limit=itemwriter.CASE_CHARS if kind == "case" else itemwriter.ITEM_CHARS)
                for x in quotes:
                    self.assertTrue(self.dojo.quote_ok(g, x["quote"], x["source"]["n"]), x)
                self.assertGreater(exam_items.max_points(it), 0)

    def _patched(self, change):
        real = itemwriter.ask_json

        def fake(ai, role, system, user, **kw):
            return change(role, real(ai, role, system, user, **kw))
        return mock.patch.object(itemwriter, "ask_json", fake)

    def test_a_quote_not_on_the_page_drops_the_item(self):
        def invent(role, data):
            if role == "author":
                for raw in data["items"]:
                    raw["statements"][1]["quote"] = "Copilot reads your mind and writes the exam for you."
            return data
        with self._patched(invent):
            items, lost = self.dojo.write_items("gh-300", "yesno", [self.skill])
        self.assertEqual(items, [], "an unverifiable statement is never shown")
        self.assertIn("statement 2", lost[0]["reason"])
        self.assertIn("word for word", lost[0]["reason"])

    def test_the_checker_can_drop_an_item(self):
        def reject(role, data):
            if role == "gate":
                return {"verdicts": [{"index": 0, "holds": False, "reason": "Pair 2 is not what the page says."}]}
            return data
        with self._patched(reject):
            items, lost = self.dojo.write_items("gh-300", "match", [self.skill])
        self.assertEqual(items, [])
        self.assertEqual(lost[0]["reason"], "Pair 2 is not what the page says.")

    def test_a_key_that_does_not_fit_its_kind_is_dropped(self):
        def no_no(role, data):
            if role == "author":
                for raw in data["items"]:
                    for s in raw["statements"]:
                        s["answer"] = True
            return data
        with self._patched(no_no):
            items, lost = self.dojo.write_items("gh-300", "yesno", [self.skill])
        self.assertEqual(items, [])
        self.assertIn("one Yes and one No", lost[0]["reason"])


class ExamRealismApiTests(_Client):
    """A practice exam in the real exam's format, driven through the API the way the page drives it."""

    def test_a_full_exam_follows_the_mix_and_shows_no_key(self):
        aid, view = self.started()
        self.assertEqual(view["style"], "exam")
        self.assertEqual(view["questions"], 50)
        self.assertEqual(exam.kinds_in(view["items"]), exam_items.mix(50))
        self.assertEqual(view["mix"], {k: n * exam_items.UNIT_SIZE.get(k, 1) for k, n in exam_items.mix(50).items()})
        kinds = [it.get("kind", "single") for it in view["items"]]
        self.assertEqual(kinds[-2:], ["yesno", "case"], "the sections that lock come last")
        self.assertEqual(_keys_anywhere(view["items"]), set())
        self.assertEqual(view["minutes"], 100)
        self.assertGreater(view["seconds_left"], 99 * 60)
        self.assertFalse(view["open_book"])
        self.assertFalse(view["open_book_rule"]["allowed"])

    def test_the_single_style_is_single_choice_as_before(self):
        aid, view = self.started(length="short", style="single")
        self.assertEqual({it.get("kind", "single") for it in view["items"]}, {"single"})
        self.post(f"/api/exams/{aid}/answer", {"index": 0, "choice": 1})
        self.assertEqual(self.doc(aid)["answers"]["0"]["choice"], 1)

    def test_partial_credit_with_nothing_taken_off(self):
        aid, view = self.started()
        doc = self.doc(aid)
        items = doc["items"]
        m, s, t, h = (self.first(view, k) for k in ("multi", "sequence", "match", "hotarea"))
        wrong_multi = next(o for o in range(5) if o not in items[m]["answers"])
        self.post(f"/api/exams/{aid}/answer", {"index": m, "response": {"choices": sorted([items[m]["answers"][0], wrong_multi])}})
        self.post(f"/api/exams/{aid}/answer", {"index": s, "response": {"order": list(reversed(items[s]["order"]))}})
        pairs = list(items[t]["pairs"])
        pairs[0] = next(v for v in range(len(items[t]["options"])) if v != pairs[0])
        self.post(f"/api/exams/{aid}/answer", {"index": t, "response": {"pairs": pairs}})
        self.post(f"/api/exams/{aid}/answer", {"index": h, "response": {"picks": items[h]["blank_answers"]}})
        y = self.first(view, "yesno")
        for n, v in enumerate(items[y]["yes"]):
            self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": v if n else not v}, "part": n})
        c = self.first(view, "case")
        for n, q in enumerate(items[c]["questions"]):
            raw = {"choices": q["answers"]} if q.get("kind") == "multi" else {"choice": q["answer"]}
            self.post(f"/api/exams/{aid}/answer", {"index": c, "response": raw, "part": n})
        self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        shown = self.post(f"/api/exams/{aid}/rationales")
        got = {i: shown["items"][i] for i in (m, s, t, h, y, c)}
        self.assertEqual((got[m]["earned"], got[m]["points"], got[m]["correct"]), (1, 2, False))
        self.assertEqual((got[s]["earned"], got[s]["points"]), (0, 1), "a build list in the wrong order earns nothing")
        self.assertEqual(got[t]["earned"], len(pairs) - 1)
        self.assertEqual((got[h]["earned"], got[h]["correct"]), (2, True))
        self.assertEqual((got[y]["earned"], got[y]["points"]), (2, 3))
        self.assertEqual(got[c]["earned"], got[c]["points"])
        expect = 1 + 0 + (len(pairs) - 1) + 2 + 2 + got[c]["points"]
        self.assertEqual(shown["correct"], expect, "single choice was not answered, so the rest is all there is")
        self.assertEqual(shown["points"], sum(exam_items.max_points(it) for it in items))
        self.assertGreaterEqual(shown["partial"], 3)
        self.assertEqual(sum(d["points"] for d in shown["by_domain"]), shown["points"])
        self.assertIsNone(re.search(r"\b700\b", json.dumps(shown)), "no mapping onto the 700-point pass scale")
        self.assertIn("Not a pass prediction", shown["diagnosis_note"])
        # The key, merged per case-study question, now that the answers are shown.
        self.assertIn("answer", got[c]["questions"][0])

    def test_an_answer_taken_back_is_no_answer(self):
        aid, view = self.started()
        m = self.first(view, "multi")
        out = self.post(f"/api/exams/{aid}/answer", {"index": m, "response": {"choices": [0]}})
        self.assertEqual((out["answers"], out["response"]), (1, {"choices": [0]}))
        out = self.post(f"/api/exams/{aid}/answer", {"index": m, "response": {"choices": []}})
        self.assertEqual((out["answers"], out["response"]), (0, None))
        self.assertNotIn(str(m), self.doc(aid)["answers"])

    def test_no_going_back_in_a_yes_no_series(self):
        aid, view = self.started()
        y = self.first(view, "yesno")
        ahead = self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 2}, status=400)
        self.assertIn("Answer statement 1 first", ahead["detail"])
        self.assertNotIn(str(y), self.doc(aid)["answers"], "skipping ahead stores nothing")
        self.assertNotIn(str(y), self.doc(aid).get("revs") or {}, "and counts no write")
        self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 0})
        self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 1})
        back = self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": False}, "part": 0}, status=409)
        self.assertIn("cannot be changed", back["detail"])
        self.assertEqual(self.doc(aid)["answers"][str(y)]["response"]["yes"], [True, True, None])

    def test_a_series_or_case_study_locks_once_left(self):
        aid, view = self.started()
        y, c = self.first(view, "yesno"), self.first(view, "case")
        self.assertEqual(self.post(f"/api/exams/{aid}/visit", {"index": y}), {"locked": [], "inside": y, "seconds_left": mock.ANY})
        self.post(f"/api/exams/{aid}/visit", {"index": 0})
        self.assertEqual(self.doc(aid)["locked"], [y])
        self.assertIn("locked", self.post(f"/api/exams/{aid}/visit", {"index": y}, status=409)["detail"])
        self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 0}, status=409)
        self.post(f"/api/exams/{aid}/flag", {"index": y, "flagged": True}, status=409)
        # Answering a question elsewhere is leaving too: the page cannot skip the lock.
        self.post(f"/api/exams/{aid}/answer", {"index": c, "response": {"choice": 0}, "part": 0})
        # Moving between the case study's own questions does not lock it; only leaving it does.
        self.post(f"/api/exams/{aid}/answer", {"index": c, "response": {"choice": 1}, "part": 0})
        self.post(f"/api/exams/{aid}/visit", {"index": c})
        self.assertEqual((self.doc(aid)["locked"], self.doc(aid)["inside"]), ([y], c))
        self.post(f"/api/exams/{aid}/visit", {"index": -1})
        self.assertEqual(self.doc(aid)["locked"], [y, c])
        view = self.c.get(f"/api/exams/{aid}").json()
        self.assertEqual(view["locked"], [y, c])
        self.assertEqual(view["locked_note"], exam.LOCKED)

    def test_a_refused_answer_changes_nothing_not_even_the_locks(self):
        # Review 1, finding 2: a malformed answer elsewhere returned 400 but still locked the case study.
        aid, view = self.started()
        c, t = self.first(view, "case"), self.first(view, "match")
        self.post(f"/api/exams/{aid}/visit", {"index": c})
        self.post(f"/api/exams/{aid}/answer", {"index": t, "response": {"pairs": [0]}}, status=400)
        self.post(f"/api/exams/{aid}/answer", {"index": t, "response": {"pairs": "x"}}, status=400)
        self.post(f"/api/exams/{aid}/answer", {"index": self.first(view, "single"), "choice": 5}, status=400)
        doc = self.doc(aid)
        self.assertEqual((doc["locked"], doc["inside"]), ([], c), "still inside the case study, and nothing locked")
        self.assertNotIn(str(t), doc["answers"])
        # A yes/no statement already passed is refused without moving the learner either.
        y = self.first(view, "yesno")
        self.post(f"/api/exams/{aid}/answer", {"index": c, "response": {"choice": 0}, "part": 0})
        self.post(f"/api/exams/{aid}/visit", {"index": y})
        self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 0})
        self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 0}, status=409)
        self.assertEqual((self.doc(aid)["locked"], self.doc(aid)["inside"]), ([c], y))
        # A good answer elsewhere is a real move, and locks the section that was left.
        self.post(f"/api/exams/{aid}/answer", {"index": t, "response": {"pairs": [0, None, None]}})
        self.assertEqual((self.doc(aid)["locked"], self.doc(aid)["inside"]), ([y, c] if y < c else [c, y], None))

    def test_a_refused_visit_changes_nothing_not_even_the_locks(self):
        # Fix check of review 1: a 409 visit to a locked series still locked the case study the learner was in.
        aid, view = self.started()
        y, c = self.first(view, "yesno"), self.first(view, "case")
        self.post(f"/api/exams/{aid}/visit", {"index": y})
        self.post(f"/api/exams/{aid}/visit", {"index": c})
        self.assertEqual((self.doc(aid)["locked"], self.doc(aid)["inside"]), ([y], c))
        before = self.doc(aid)
        self.assertIn("locked", self.post(f"/api/exams/{aid}/visit", {"index": y}, status=409)["detail"])
        self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 0}, status=409)
        doc = self.doc(aid)
        self.assertEqual((doc["locked"], doc["inside"]), ([y], c), "still inside the case study, which stays open")
        self.assertEqual(doc, before, "and nothing else changed")
        # The case study still takes answers.
        self.post(f"/api/exams/{aid}/answer", {"index": c, "response": {"choice": 0}, "part": 0})
        self.assertEqual((self.doc(aid)["locked"], self.doc(aid)["inside"]), ([y], c))

    def test_an_older_write_never_replaces_a_later_one(self):
        # Review 1, finding 3: every answer carries the item's write count, and a stale one changes nothing.
        aid, view = self.started()
        m = self.first(view, "multi")
        self.assertEqual(view["items"][m]["rev"], 0)
        out = self.post(f"/api/exams/{aid}/answer", {"index": m, "rev": 2, "response": {"choices": [0, 1]}})
        self.assertEqual((out["stale"], out["rev"], out["response"]), (False, 2, {"choices": [0, 1]}))
        late = self.post(f"/api/exams/{aid}/answer", {"index": m, "rev": 1, "response": {"choices": [3]}})
        self.assertEqual((late["stale"], late["rev"], late["response"]), (True, 2, {"choices": [0, 1]}),
                         "the reply says what the server holds")
        same = self.post(f"/api/exams/{aid}/answer", {"index": m, "rev": 2, "response": {"choices": []}})
        self.assertTrue(same["stale"], "a repeated write count is stale too")
        self.assertEqual(self.doc(aid)["answers"][str(m)]["response"], {"choices": [0, 1]})
        self.assertEqual(self.c.get(f"/api/exams/{aid}").json()["items"][m]["rev"], 2)
        # A cleared answer keeps its count, so an older write cannot bring it back.
        self.post(f"/api/exams/{aid}/answer", {"index": m, "rev": 3, "response": {"choices": []}})
        self.assertTrue(self.post(f"/api/exams/{aid}/answer", {"index": m, "rev": 2, "response": {"choices": [4]}})["stale"])
        self.assertNotIn(str(m), self.doc(aid)["answers"])
        # A page from before the write count still answers, and its write counts as the next one.
        s = self.first(view, "single")
        self.post(f"/api/exams/{aid}/answer", {"index": s, "choice": 1})
        self.assertEqual(self.doc(aid)["revs"][str(s)], 1)
        late = self.post(f"/api/exams/{aid}/answer", {"index": s, "rev": 1, "choice": 2})
        self.assertEqual((late["stale"], late["choice"]), (True, 1))
        self.post(f"/api/exams/{aid}/answer", {"index": m, "rev": 0, "response": {"choices": [0]}}, status=422)

    def test_the_page_sends_answers_and_moves_in_order_and_draws_only_the_latest_reply(self):
        # Review 1, finding 3, on the page: run the exam runner's own queue and send under Node with a slow
        # first request, and check the order the server saw and what the page ends up showing.
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        script = (APP_DIR / "static" / "app.js").read_text("utf-8")
        block = script.split("  let writes = Promise.resolve();", 1)[1].split("  const pick = async", 1)[0]
        harness = """
let ending = false; const X = { id: "x", locked: [] }; const kindOf = (q) => q.kind || "single";
const log = []; const failed = (e) => log.push("failed " + e.message); const render = () => {};
const server = { rev: 0, response: null };
const api = (url, opts) => new Promise((resolve) => {
  const b = opts.body, what = url.split("/").pop() + (b.rev ? " " + b.rev : "");
  log.push("start " + what);
  setTimeout(() => {
    log.push("end " + what);
    if (url.endsWith("/answer")) {
      const stale = b.rev <= server.rev;
      if (!stale) { server.rev = b.rev; server.response = b.response; }
      resolve({ stale, rev: server.rev, response: server.response, locked: [] });
    } else resolve({ locked: [0], inside: null });
  }, b.rev === 1 ? 60 : 5);
});
let writes = Promise.resolve();
""" + block + """
(async () => {
  const q = { index: 0, kind: "multi", rev: 0 };
  send(q, { response: { choices: [0] } });
  q.response = { choices: [0, 1] };
  send(q, { response: { choices: [0, 1] } });
  queue(() => api("/api/exams/x/visit", { body: { index: 1 } }));
  await settled();
  const other = { index: 1, kind: "multi", rev: 0 };
  server.rev = 7; server.response = { choices: [4] };
  await send(other, { response: { choices: [2] } });
  console.log(JSON.stringify({ log, q, other }));
})();
"""
        out = subprocess.run([node, "-"], input=harness, capture_output=True, text=True, timeout=30, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["log"][:6], ["start answer 1", "end answer 1", "start answer 2", "end answer 2", "start visit", "end visit"],
                         "one write at a time, and the move after the answers sent before it")
        self.assertEqual((got["q"]["rev"], got["q"]["response"]), (2, {"choices": [0, 1]}), "the learner's last choice stands")
        self.assertEqual((got["other"]["rev"], got["other"]["response"]), (7, {"choices": [4]}),
                         "a write another tab overtook shows what the server holds")

    def test_the_section_you_are_in_locks_when_the_exam_ends(self):
        aid, view = self.started()
        c = self.first(view, "case")
        self.post(f"/api/exams/{aid}/visit", {"index": c})
        self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        self.assertIn(c, self.doc(aid)["locked"])
        self.assertIsNone(self.doc(aid)["inside"])

    def test_the_export_holds_back_every_key_while_the_exam_runs(self):
        aid, _ = self.started()
        y = self.first(self.c.get(f"/api/exams/{aid}").json(), "yesno")
        self.post(f"/api/exams/{aid}/answer", {"index": y, "response": {"yes": True}, "part": 0})
        out = exam.for_export(self.doc(aid))
        self.assertEqual(_keys_anywhere(out["items"]), set(), "case-study questions included")
        self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        self.post(f"/api/exams/{aid}/rationales")
        self.assertIn("yes", exam.for_export(self.doc(aid))["items"][y])

    def test_closing_records_the_kind_and_points_and_fills_no_pip(self):
        aid, view = self.started()
        m = self.first(view, "multi")
        it = self.doc(aid)["items"][m]
        self.post(f"/api/exams/{aid}/answer", {"index": m, "response": {"choices": it["answers"]}})
        self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        events = self.dojo.store.events(self.learner.key)
        ev = next(e for e in reversed(events) if e["type"] == "exam.answered" and e["data"]["attempt"] == aid)
        self.assertEqual((ev["data"]["kind"], ev["data"]["points"], ev["data"]["max_points"], ev["data"]["correct"]),
                         ("multi", 2, 2, True))
        closed = next(e for e in reversed(events) if e["type"] == "exam.closed" and e["data"]["attempt"] == aid)
        self.assertEqual(closed["data"]["points"], sum(exam_items.max_points(x) for x in self.doc(aid)["items"]))
        pkg = PACKAGES.get("gh-300")
        t0 = utcnow() - timedelta(hours=30)
        for kind in exam_items.NEW_KINDS:
            with self.subTest(kind=kind):
                rows = [{"at": iso(t0), "type": "exam.answered", "data": {"package": "gh-300", "skill": it["skill"], "attempt": "exam-1",
                                                                         "correct": True, "kind": kind, "points": 3, "max_points": 3}},
                        {"at": iso(t0 + timedelta(hours=1)), "type": "exam.rationales_shown",
                         "data": {"package": "gh-300", "attempt": "exam-1", "skills": [it["skill"]]}}]
                rec = derive(rows, pkg)[it["skill"]]
                self.assertEqual(rec["state"], "untested", "a selected-response answer never fills a pip")
                self.assertFalse(any(rec["checks"].values()))
                self.assertEqual((rec["rehearsal_total"], rec["rehearsal_correct"]), (1, 1))

    def test_the_list_shows_points_style_and_open_book(self):
        aid, _ = self.started(length="short")
        self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        out = self.c.get("/api/exams?package=gh-300").json()
        row = next(a for a in out["attempts"] if a["id"] == aid)
        self.assertEqual((row["style"], row["open_book"]), ("exam", False))
        self.assertEqual(row["points"], sum(exam_items.max_points(x) for x in self.doc(aid)["items"]))
        self.assertFalse(out["open_book"]["allowed"])
        self.assertEqual(out["mix"]["full"]["case"], 4)


class OpenBookTests(_Client):
    """Microsoft Learn beside the exam: only where the real exam allows it, counted, never seen into."""

    def test_a_github_exam_cannot_be_taken_open_book(self):
        self.close_open()
        out = self.post("/api/exams", {"package": "gh-300", "length": "short", "style": "exam", "open_book": True}, status=422)
        self.assertIn("GitHub exams are closed-book", out["detail"])

    def test_learn_on_a_closed_book_exam_is_refused(self):
        aid, _ = self.started(length="short")
        self.post(f"/api/exams/{aid}/learn", {"event": "open"}, status=409)

    def test_dp_800_counts_lookups_and_time_away(self):
        aid, view = self.started(package="dp-800", length="short", open_book=True)
        self.assertTrue(view["open_book"])
        self.assertTrue(view["open_book_rule"]["allowed"])
        self.assertEqual(view["learn"], {"lookups": 0, "away_seconds": 0})
        self.post(f"/api/exams/{aid}/learn", {"event": "open"})
        self.post(f"/api/exams/{aid}/learn", {"event": "open"})
        # Never more time away than the exam has been open.
        doc = self.doc(aid)
        doc["started"] = iso(utcnow() - timedelta(minutes=10))
        self.dojo.store.write("learners", self.learner.key, "exams", aid, value=doc)
        out = self.post(f"/api/exams/{aid}/learn", {"event": "away", "seconds": 240})
        self.assertEqual(out, {"lookups": 2, "away_seconds": 240})
        out = self.post(f"/api/exams/{aid}/learn", {"event": "away", "seconds": 36000})
        self.assertLessEqual(out["away_seconds"], 10 * 60 + 5)
        self.post(f"/api/exams/{aid}/learn", {"event": "peek"}, status=422)
        closed = self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        self.assertIn("2 lookups", closed["conditions_note"])
        self.assertIn("Dojo cannot see what you read there", closed["conditions_note"])
        self.post(f"/api/exams/{aid}/learn", {"event": "open"}, status=409)
        row = next(a for a in self.c.get("/api/exams?package=dp-800").json()["attempts"] if a["id"] == aid)
        self.assertTrue(row["open_book"])


class TwoLearnerTests(_Client):
    """Attempts, locks, Learn counts and pools live under each learner (learners/<key>/...)."""

    def test_each_learner_has_their_own_attempts_and_pool(self):
        a = Learner("local:a", learner_key("local:a"), "A")
        b = Learner("local:b", learner_key("local:b"), "B")
        for who in (a, b):
            self.dojo.store.path("learners", who.key).mkdir(parents=True, exist_ok=True)
        ids = {}
        for who in (a, b):
            doc = self.room.create(who, "dp-800", "short", style="exam", open_book_=True)
            self.room.build(lambda _: None, who, doc["id"])
            self.room.start(who, doc["id"])
            ids[who.key] = doc["id"]
        va = self.room.view(a, ids[a.key])
        y = self.first(va, "yesno")
        self.room.visit(a, ids[a.key], y)
        self.room.visit(a, ids[a.key], 0)
        self.room.learn(a, ids[a.key], "open")
        self.room.answer(a, ids[a.key], self.first(va, "single"), choice=0)
        vb = self.room.view(b, ids[b.key])
        self.assertEqual((vb["locked"], vb["answered"], vb["learn"]["lookups"]), ([], 0, 0))
        self.assertEqual(self.room.view(a, ids[a.key])["locked"], [y])
        with self.assertRaises(HTTPException) as e:
            self.room.attempt(b, ids[a.key])
        self.assertEqual(e.exception.status_code, 404)
        self.assertTrue(self.dojo.store.path("learners", a.key, "exams").exists())
        # The pool: one learner's questions are not another's.
        out = self.room.top_up(a, "gh-300", kind="yesno", report=False)
        self.assertEqual((out["state"], out["added"]), ("added", 3))
        self.assertEqual(self.room.pool_size(a, "gh-300"), 3)
        self.assertEqual(self.room.pool_size(b, "gh-300"), 0)
        self.assertTrue(self.dojo.store.path("learners", a.key, "pool").exists())


class PoolByKindTests(unittest.TestCase):
    def test_the_pool_tops_up_the_kind_it_is_shortest_of(self):
        self.assertEqual(exam.kind_needed([]), "single", "single choice wins a tie")
        singles = [dict(SINGLE, stem=f"s{i}") for i in range(12)]
        self.assertEqual(exam.kind_needed(singles), "multi")
        full = singles + [MULTI, dict(MULTI, stem="m2"), SEQUENCE, MATCH, HOT, YESNO, CASE]
        self.assertEqual(exam.questions_in(full), exam.POOL_TARGET)
        self.assertEqual(exam.kind_needed(full), "single")


if __name__ == "__main__":
    unittest.main()
