"""The grader's quote of a learner's answer (learning.learner_quote_found). A quote is the whole passage
word for word, or, for a list-like answer, several exact fragments in the answer's order. Nothing
reworded, reordered, too short or missing counts: then the point is not met."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-quotes-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ai as ai_mod  # noqa: E402
from app.core import Learner, learner_key  # noqa: E402
from app.learning import GRADE_SYSTEM, Dojo, learner_quote_check, learner_quote_found  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

# A made-up table design, one line per column, as a learner might type it.
ANSWER = ("orderid: int, non-null, primary key\n"
          "customerid: int, non-null, foreign key to customers unless it is a guest checkout\n"
          "placedat: datetime2, non-null, default sysutcdatetime\n"
          "notes: nvarchar, null, max. ...")

STITCHED = "orderid: int, non-null\ncustomerid: int, non-null\nplacedat: datetime2, non-null"


class QuoteTests(unittest.TestCase):
    def test_line_fragments_stitched_together_are_found(self):
        self.assertTrue(learner_quote_found(STITCHED, ANSWER))
        self.assertTrue(learner_quote_found("orderid: int, non-null … customerid: int, non-null", ANSWER))
        self.assertTrue(learner_quote_found("orderid: int, non-null ... notes: nvarchar, null", ANSWER))
        self.assertTrue(learner_quote_found("orderid: int, non-null\r\nplacedat: datetime2, non-null", ANSWER))

    def test_a_whole_passage_word_for_word_is_still_found(self):
        self.assertTrue(learner_quote_found("customerid: int, non-null, foreign key to customers", ANSWER))
        self.assertTrue(learner_quote_found("Primary Key customerid: INT, non-null", ANSWER))   # across a line break
        self.assertTrue(learner_quote_found("notes: nvarchar, null, max. ...", ANSWER))

    def test_a_reworded_fragment_is_not_found(self):
        self.assertFalse(learner_quote_found("orderid: integer, non-null\ncustomerid: int, non-null", ANSWER))

    def test_fragments_out_of_order_are_not_found(self):
        self.assertFalse(learner_quote_found("customerid: int, non-null\norderid: int, non-null", ANSWER))

    def test_the_same_fragment_twice_is_not_found_twice(self):
        self.assertFalse(learner_quote_found("orderid: int, non-null\norderid: int, non-null", ANSWER))

    def test_a_one_word_fragment_is_not_enough(self):
        self.assertFalse(learner_quote_found("orderid: int, non-null\ncustomerid:\nplacedat: datetime2", ANSWER))

    def test_too_few_words_altogether_is_not_enough(self):
        self.assertFalse(learner_quote_found("non-null, primary", ANSWER))
        self.assertFalse(learner_quote_found("orderid: int,\nnon-null", ANSWER))

    def test_a_two_word_fragment_counts_in_a_terse_list(self):
        terse = "id: guid\ntenantid: guid\nname: nvarchar\nnotes: nvarchar"
        self.assertTrue(learner_quote_found("id: guid\ntenantid: guid\nname: nvarchar", terse))
        self.assertTrue(learner_quote_found("orderid: int,\ncustomerid: int, non-null", ANSWER))
        self.assertFalse(learner_quote_found("id: guid\ntenantid:\nname: nvarchar", terse))   # a one-word piece

    def test_dots_inside_a_line_of_code_stay_in_their_fragment(self):
        code = "SELECT ... FROM t\nWHERE t.id = 1\nfn(...args) spreads them\nreturn early"
        self.assertTrue(learner_quote_found("SELECT ... FROM t\nfn(...args) spreads them", code))
        self.assertEqual(learner_quote_check("SELECT ... FROM t\nfn(...args) spreads them", code), "pieces")
        # One line, fragments separated by " … ", with "..." inside a fragment.
        self.assertTrue(learner_quote_found("SELECT ... FROM t … fn(...args) spreads them", code))
        # Cut at every "...", the same quote would leave one-word pieces ("select", "fn(") and fail.
        self.assertFalse(learner_quote_found("select\nfrom t\nfn(\nargs) spreads them", code))

    def test_a_fragment_must_start_and_end_on_a_word_edge(self):
        self.assertFalse(learner_quote_found("rderid: int, non-null\ncustomerid: int, non-null", ANSWER))
        self.assertFalse(learner_quote_found("orderid: int, non-nul\ncustomerid: int, non-null", ANSWER))

    def test_a_fragment_that_leaves_out_a_negation_after_it_is_not_found(self):
        self.assertFalse(learner_quote_found("primary key … foreign key", "primary key: no\nforeign key: no"))
        negated = "orderid primary key: no\ncustomerid foreign key: no"
        self.assertFalse(learner_quote_found("orderid primary key … customerid foreign key", negated))
        self.assertFalse(learner_quote_found("orderid primary key\ncustomerid foreign key", negated))
        self.assertTrue(learner_quote_found("orderid primary key … customerid foreign key",
                                            "orderid primary key: yes\ncustomerid foreign key: yes"))
        for word in ("not", "none", "never", "n/a", "false", "without", "isn't", "doesn’t", "don't", "can't", "cannot", "nor"):
            answer = f"orderid primary key = {word} really\ncustomerid foreign key: yes"
            self.assertFalse(learner_quote_found("orderid primary key … customerid foreign key", answer), word)

    def test_a_negation_in_the_next_clause_does_not_count_against_a_fragment(self):
        answer = "caseid: uniqueidentifier type, not null\ntenantid: uniqueidentifier type, not null"
        self.assertTrue(learner_quote_found("caseid: uniqueidentifier type … tenantid: uniqueidentifier type", answer))

    def test_a_fragment_that_ends_its_own_clause_is_read_like_one_without_the_comma(self):
        """Copied with the comma that ends it, a fragment is exact too: what comes after the comma is the
        next clause, not a word that turns the fragment around."""
        answer = "orderid: int, non-null,  not sure about a default\ncustomerid: int, non-null, no default"
        self.assertTrue(learner_quote_found("orderid: int, non-null\ncustomerid: int, non-null", answer))
        self.assertTrue(learner_quote_found("orderid: int, non-null,\ncustomerid: int, non-null,", answer))
        self.assertTrue(learner_quote_found("orderid: int, non-null, … customerid: int, non-null,", answer))
        # A joiner such as ":" ends no clause: the word after it still turns the fragment around.
        self.assertFalse(learner_quote_found("orderid primary key:\ncustomerid foreign key:",
                                             "orderid primary key: no\ncustomerid foreign key: no"))

    def test_a_fragment_that_leaves_out_a_negation_before_it_is_not_found(self):
        answer = "orderid is not a primary key here\ncustomerid is a foreign key here"
        self.assertFalse(learner_quote_found("a primary key here … a foreign key here", answer))
        self.assertTrue(learner_quote_found("a primary key here … a foreign key here", answer.replace("not ", "")))
        self.assertFalse(learner_quote_found("a primary key here … a foreign key here",
                                             "orderid: never a primary key here\ncustomerid is a foreign key here"))

    def test_a_later_occurrence_without_the_negation_still_counts(self):
        answer = "not a primary key here, says the old design\nbut now a primary key here\nand a foreign key here"
        self.assertTrue(learner_quote_found("a primary key here … a foreign key here", answer))

    def test_how_a_quote_was_found(self):
        self.assertEqual(learner_quote_check("customerid: int, non-null, foreign key to customers", ANSWER), "whole")
        self.assertEqual(learner_quote_check(STITCHED, ANSWER), "pieces")
        self.assertEqual(learner_quote_check("orderid: integer", ANSWER), "")

    def test_an_empty_quote_is_not_found(self):
        for quote in ("", "   ", "\n", " … ", "..."):
            self.assertFalse(learner_quote_found(quote, ANSWER), repr(quote))

    def test_a_fragment_not_in_the_answer_is_not_found(self):
        self.assertFalse(learner_quote_found("orderid: int, non-null\nshipdate: date, null", ANSWER))

    def test_a_spoken_answer_is_judged_as_before(self):
        spoken = "so the order id is an int and it is never null and it is the primary key of the table"
        self.assertTrue(learner_quote_found("the order id is an int and it is never null", spoken))
        self.assertFalse(learner_quote_found("the order ID is an integer that is never null", spoken))

    def test_verify_judgements_keeps_exactly_what_was_quoted(self):
        it = {"rubric": [{"id": "r1", "point": "types"}, {"id": "r2", "point": "nulls"},
                         {"id": "r3", "point": "columnstore"}, {"id": "r4", "point": "keys"}]}
        data = {"points": [{"id": "r1", "met": True, "learner_quote": STITCHED, "why": "x"},
                           {"id": "r2", "met": True, "learner_quote": "orderid: int, non-null … notes: nvarchar, null"},
                           {"id": "r3", "met": True, "learner_quote": ""},
                           {"id": "r4", "met": True, "learner_quote": "orderid: int, primary key"}]}
        points, bad = Dojo._verify_judgements(data, it, ANSWER)
        self.assertEqual([p["verified"] for p in points], [True, True, False, False])
        self.assertEqual(points[0]["learner_quote"], STITCHED)
        self.assertEqual([b["id"] for b in bad], ["r3", "r4"])

    def test_the_grader_is_told_it_may_quote_fragments(self):
        self.assertIn('separated by " … "', GRADE_SYSTEM)
        self.assertIn("never reword a fragment", GRADE_SYSTEM)
        self.assertIn('one per line (preferred) or separated by " … "', GRADE_SYSTEM)


class GradeFlowTests(unittest.TestCase):
    """The whole grading path with a grader that quotes a list-like answer in fragments."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-quotes-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.dojo = cls.app.state.dojo
        cls.me = Learner("local:dev", learner_key("local:dev"), "Local learner")
        cls.pid = "gh-300"
        pkg = cls.dojo.packages.get(cls.pid)
        # A new item each time: the stub writes only a few distinct items per skill, so the tests take turns.
        cls.sids = [s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]
                    if pkg["skills"][s]["sources"] and pkg["skills"][s]["kind"] == "scenario"]
        cls.turn = 0

    def wait(self, job: dict) -> dict:
        for _ in range(900):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def post(self, path: str, body: dict) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def grade(self, quote: str, input_: str = "typed", gate=None) -> tuple[dict, list[str]]:
        type(self).turn += 1
        sid = self.sids[self.turn % len(self.sids)]
        item = self.wait(self.post("/api/items", {"package": self.pid, "skill": sid, "mode": "check"}))["item"]
        prompts: list[str] = []

        def grader(user: str) -> dict:
            prompts.append(user)
            ids = ai_mod._RUBRIC.findall(user)
            return {"points": [{"id": rid, "met": True, "learner_quote": quote, "why": "stub"} for rid in ids],
                    "feedback": "Stub feedback.", "misconception": ""}
        self.gate_prompts: list[str] = []
        real_gate = self.dojo.ai._gate_judgement

        def gatekeeper(user: str) -> dict:
            self.gate_prompts.append(user)
            return (gate or real_gate)(user)
        # A spoken answer is recorded as spoken only with a receipt from Dojo's own transcription.
        with mock.patch.object(self.dojo.ai, "_grade", grader), \
                mock.patch.object(self.dojo.ai, "_gate_judgement", gatekeeper), \
                mock.patch.object(self.dojo.receipts, "consume", lambda *a: True):
            self.wait(self.post(f"/api/items/{item}/answer", {"text": ANSWER, "elapsed_s": 30, "input": input_,
                                                               "receipts": ["r"] if input_ == "spoken" else []}))
        return self.c.get(f"/api/items/{item}").json(), prompts

    def judged(self, item: str) -> dict:
        return next(e["data"] for e in self.dojo.store.events(self.me.key)
                    if e["type"] == "answer.judged" and e["data"]["item"] == item)

    def test_stitched_fragments_are_met_at_the_first_try_and_kept_as_quoted(self):
        it, prompts = self.grade(STITCHED)
        self.assertEqual(len(prompts), 1, "no retry was needed")
        self.assertEqual((it["result"]["met"], it["result"]["unverified"]), (it["result"]["total"], 0))
        quotes = {p["learner_quote"] for p in self.judged(it["id"])["points"]}
        self.assertEqual(quotes, {STITCHED})
        # The stitched quote rides on the gate call that already runs: one element per point, no extra call.
        self.assertEqual(len(self.gate_prompts), 1)
        self.assertIn("[q.r1] The learner's words 'orderid: int, non-null … customerid: int, non-null … "
                      "placedat: datetime2, non-null' show the rubric point", self.gate_prompts[0])
        self.assertTrue(all(p["stitched"] for p in it["result"]["points"]))

    @staticmethod
    def gate_rejecting_fragments(user: str) -> dict:
        return {"verdicts": [{"id": eid, "holds": not eid.startswith("q."), "reason": "the fragments drop a 'no'"}
                             for eid in ai_mod._ELEMENT.findall(user)]}

    def test_a_stitched_quote_the_gate_rejects_goes_to_the_referee(self):
        def referee(user: str) -> dict:   # agrees with the checker
            return {"points": [{"id": rid, "met": False, "learner_quote": "", "why": "The fragments leave out a 'no'."}
                               for rid in ai_mod._RUBRIC.findall(user)]}
        with mock.patch.object(self.dojo.ai, "_referee", referee):
            it, prompts = self.grade(STITCHED, gate=self.gate_rejecting_fragments)
        r = it["result"]
        self.assertEqual((r["met"], r["unverified"]), (0, 0))
        self.assertTrue(all(p["refereed"] == {"grader_met": True, "objection": "the fragments drop a 'no'"} for p in r["points"]))
        self.assertTrue(all(not p["met"] for p in self.judged(it["id"])["points"]))
        log = [x for x in self.c.get("/api/quality").json() if x.get("item") == it["id"]]
        decided = [x for x in log if "a third model decided it: not met (the grader had said met)" in x["reason"]]
        self.assertEqual(len(decided), r["total"])

    def test_a_stitched_quote_the_checker_gives_no_verdict_on_goes_to_the_referee(self):
        def gate(user: str) -> dict:   # leaves out every verdict on fragments
            return {"verdicts": [{"id": eid, "holds": True, "reason": ""} for eid in ai_mod._ELEMENT.findall(user)
                                 if not eid.startswith("q.")]}
        it, _ = self.grade(STITCHED, gate=gate)   # the stub referee decides as the stub grader: met, quoting the fragments
        r = it["result"]
        self.assertEqual((r["met"], r["unverified"]), (r["total"], 0))
        self.assertTrue(all(p["refereed"] == {"grader_met": True, "objection": "The checker gave no verdict on the quoted fragments."}
                            for p in r["points"]))

    def test_a_stitched_quote_the_gate_rejects_is_not_met_and_logged_without_a_referee(self):
        with mock.patch.object(self.dojo, "_referee", side_effect=ai_mod.AIError("author model: down")):
            it, prompts = self.grade(STITCHED, gate=self.gate_rejecting_fragments)
        r = it["result"]
        self.assertEqual((r["met"], r["unverified"]), (0, r["total"]))
        self.assertTrue(all(p["not_shown"] and not p["why"] and p["learner_quote"] == STITCHED for p in r["points"]))
        self.assertTrue(all(not p["met"] for p in self.judged(it["id"])["points"]))
        log = [x for x in self.c.get("/api/quality").json() if x.get("item") == it["id"]]
        self.assertEqual(len([x for x in log if "do not show this point (the fragments drop a 'no')" in x["reason"]]), r["total"])
        self.assertEqual(len([x for x in log if "The referee could not be reached" in x["reason"]]), 1)

    def test_a_stitched_quote_is_not_met_when_the_gate_cannot_be_reached(self):
        def gate(user: str) -> dict:
            raise ai_mod.AIError("down")
        it, _ = self.grade(STITCHED, gate=gate)
        self.assertEqual(it["result"]["met"], 0)

    def test_a_whole_quote_keeps_todays_path_without_a_fragment_check(self):
        def gate(user: str) -> dict:
            return {"verdicts": [{"id": eid, "holds": not eid.startswith("q."), "reason": ""}
                                 for eid in ai_mod._ELEMENT.findall(user)]}
        it, _ = self.grade("customerid: int, non-null, foreign key to customers", gate=gate)
        self.assertEqual(it["result"]["met"], it["result"]["total"])
        self.assertNotIn("[q.", self.gate_prompts[0])
        self.assertFalse(any(p["stitched"] for p in it["result"]["points"]))

    def test_a_reworded_fragment_is_retried_with_the_fragment_rule_and_then_not_met(self):
        it, prompts = self.grade("orderid: integer, non-null\ncustomerid: int, non-null")
        self.assertEqual(len(prompts), 2)
        self.assertIn("several exact fragments in the order they appear", prompts[1])
        self.assertEqual((it["result"]["met"], it["result"]["unverified"]), (0, it["result"]["total"]))

    def test_a_spoken_answer_still_gets_the_spoken_note_and_a_whole_quote_counts(self):
        it, prompts = self.grade("customerid: int, non-null, foreign key to customers", "spoken")
        self.assertEqual(it["answer"]["input"], "spoken")
        self.assertIn("recording that a machine wrote down", prompts[0])
        self.assertEqual(it["result"]["met"], it["result"]["total"])


class ShownBackTests(unittest.TestCase):
    """Wherever the learner's own words are shown back, they keep the line breaks they typed, through
    a class (strict CSP: no inline styles)."""

    STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

    def test_the_mine_class_keeps_line_breaks(self):
        css = (self.STATIC / "app.css").read_text(encoding="utf-8")
        rule = re.search(r"^\.mine \{([^}]*)\}", css, re.M)
        self.assertIsNotNone(rule)
        self.assertIn("white-space: pre-wrap", rule.group(1))

    def test_every_echo_of_the_learners_words_carries_it(self):
        js = (self.STATIC / "app.js").read_text(encoding="utf-8")
        # The written answer (judging and judged), the grader's quote of it, the warm-up answer.
        echoes = re.findall(r'h\("(\w+)", (\{[^}]*\}|null), (it\.answer\.text|fragmentLines\(p\.learner_quote\)|text)\)', js)
        sources = {src for _, _, src in echoes}
        self.assertTrue({"it.answer.text", "fragmentLines(p.learner_quote)"} <= sources)
        self.assertTrue(any(src == "text" and "saidq" in attrs for _, attrs, src in echoes))
        for tag, attrs, src in echoes:
            if src == "text" and "saidq" not in attrs:
                continue   # other local `text` values are not the learner's
            self.assertIn("mine", attrs, f"{tag} showing {src}")
        self.assertGreaterEqual(sum(1 for *_, s in echoes if s == "it.answer.text"), 2)
        self.assertNotIn("style=", js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_fragments_separated_by_an_ellipsis_show_one_per_line(self):
        js = (self.STATIC / "app.js").read_text(encoding="utf-8")
        line = next(x for x in js.splitlines() if x.startswith("const fragmentLines = "))
        cases = ["id: guid … tenantid: guid", "SELECT ... FROM t … fn(...args)", "a\nb", "no…gap", ""]
        out = subprocess.run(["node", "-e", f"{line}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(fragmentLines)))"],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout
        self.assertEqual(json.loads(out), ["id: guid\ntenantid: guid", "SELECT ... FROM t\nfn(...args)", "a\nb", "no…gap", ""])


if __name__ == "__main__":
    unittest.main()
