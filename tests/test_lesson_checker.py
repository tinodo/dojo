"""A checker that skips elements must not cost a lesson its slides (app/learning.py, gate_elements and make_lesson).

On 15 October the checker answered with verdicts for other elements but not for the 40 slide elements of one
lesson. Dojo took each missing verdict as "does not hold", dropped every slide but one, and kept the lesson:
its score counted only the points of its sections. Listen, Watch and Present are made from the slides."""
import os
import re
import shutil
import tempfile
import unittest
from contextlib import nullcontext
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import learning  # noqa: E402
from app.ai import StubAI  # noqa: E402
from app.budget import GATE_RETRY, GATE_RETRY_ELEMENTS, GATE_RETRY_SIZE, TRIES  # noqa: E402
from app.core import UserError  # noqa: E402
from app.learning import MIN_SLIDES, NO_VERDICT  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import settings, web  # noqa: E402

SLIDE = re.compile(r"^s\d+\.")


class Checker(StubAI):
    """The stand-in checker, with ways to leave elements without a verdict."""

    def __init__(self) -> None:
        super().__init__()
        self.skip = None        # (element id, how many times the checker has been asked) -> leave it out
        self.bracket = False    # give every id the way the list shows it: [s0.t]
        self.refuse: set[str] = set()      # elements it says do not hold
        self.contradict: str | None = None  # an element it refuses and then, in the same answer, passes as [id]
        self.late: tuple[int, str] | None = None   # (call number, id): that call also gives a verdict for an element it was not asked about
        self.asked: list[list[str]] = []

    def _gate_elements(self, user: str) -> dict:
        out = super()._gate_elements(user)
        self.asked.append([v["id"] for v in out["verdicts"]])
        if self.skip:
            out["verdicts"] = [v for v in out["verdicts"] if not self.skip(v["id"], len(self.asked))]
        for v in out["verdicts"]:
            if v["id"] in self.refuse or v["id"] == self.contradict:
                v.update(holds=False, reason="Not supported.")
        if self.contradict:
            out["verdicts"].append({"id": f"[{self.contradict}]", "holds": True, "reason": ""})
        if self.late and len(self.asked) == self.late[0]:
            out["verdicts"].append({"id": self.late[1], "holds": True, "reason": ""})
        if self.bracket:
            for v in out["verdicts"]:
                v["id"] = f"[{v['id']}]"
        if not out["verdicts"]:
            out["verdicts"] = [{"id": "unrelated", "holds": True, "reason": ""}]   # not empty: only the ids are wrong
        return out


class LessonCheckerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="dojo-lesson-checker-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.ai = Checker()
        self.app = create_app(settings(tmp), ai=self.ai, http=httpx.Client(transport=httpx.MockTransport(web)))
        self.c = TestClient(self.app)
        self.dojo = self.app.state.dojo
        self.owner = self.app.state.preparer.owner
        p = self.c.get("/api/packages/gh-300").json()
        self.sid = next(s["id"] for d in p["domains"] for g in d["groups"] for s in g["skills"] if s["kind"] == "scenario")

    def make(self) -> dict:
        lesson = self.dojo.make_lesson(lambda step: None, self.owner, "gh-300", self.sid)["lesson"]
        return self.c.get(f"/api/lessons/{lesson}").json()

    def no_verdicts(self) -> list[dict]:
        return [q for q in self.c.get("/api/quality").json() if q.get("reason") == NO_VERDICT]

    def test_a_checker_that_skips_the_slides_is_asked_again_about_them(self):
        self.ai.skip = lambda eid, asked: asked == 1 and bool(SLIDE.match(eid))
        lesson = self.make()
        self.assertEqual(len(lesson["slides"]), 2, "both slides kept")
        self.assertEqual([s["narration"][0]["text"] for s in lesson["slides"]], ["Here is the idea.", "That is all."])
        self.assertEqual(self.no_verdicts(), [])
        self.assertEqual(len(self.ai.asked), 2)
        self.assertTrue(all(SLIDE.match(eid) for eid in self.ai.asked[1]), "the second question is only about what was skipped")

    def test_an_element_it_still_skips_does_not_hold(self):
        self.ai.skip = lambda eid, asked: eid == "p0.0"
        lesson = self.make()
        self.assertNotIn("First point.", [p["text"] for sec in lesson["sections"] for p in sec["points"]])
        skipped = self.no_verdicts()
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0]["text"].startswith("First point."), skipped[0]["text"])
        self.assertEqual(self.ai.asked[1], ["p0.0"], "asked once more, about this element alone")
        self.assertEqual(len(lesson["slides"]), 2)

    def test_a_checker_that_never_judges_the_slides_keeps_no_lesson(self):
        self.ai.skip = lambda eid, asked: bool(SLIDE.match(eid))
        with self.assertRaises(UserError) as caught:
            self.make()
        self.assertIn("Only 0 of the 2 slides written held", str(caught.exception))
        self.assertEqual(self.dojo.lessons_for("gh-300", self.sid), [], "no lesson was saved")
        self.assertTrue(self.no_verdicts(), "the quality log still says what was removed")

    def test_the_rewrite_is_not_told_that_its_own_author_failed_when_the_checker_skipped(self):
        prompts: list[str] = []
        real = self.ai.chat

        def chat(role, system, user, json_mode=True):
            if "TASK: lesson" in system:
                prompts.append(user)
            return real(role, system, user, json_mode)

        self.ai.chat = chat
        self.ai.skip = lambda eid, asked: bool(SLIDE.match(eid))
        with self.assertRaises(UserError):
            self.make()
        self.assertEqual(len(prompts), 2, "a second round was tried")
        self.assertNotIn(NO_VERDICT, prompts[1])

    def test_ids_given_the_way_the_list_shows_them_are_read(self):
        self.ai.bracket = True
        lesson = self.make()
        self.assertEqual(len(lesson["slides"]), 2)
        self.assertEqual(len(self.ai.asked), 1, "no second question was needed")
        self.assertEqual(self.no_verdicts(), [])

    def test_a_lesson_keeps_as_many_slides_as_were_written_when_fewer_than_the_minimum(self):
        self.assertGreater(MIN_SLIDES, 2)
        self.assertEqual(len(self.make()["slides"]), 2)

    def points(self, lesson: dict) -> list[str]:
        return [p["text"] for sec in lesson["sections"] for p in sec["points"]]

    def test_a_refusal_is_not_undone_by_the_same_element_passed_under_its_bracketed_id(self):
        self.ai.contradict = "p0.0"
        lesson = self.make()
        self.assertNotIn("First point.", self.points(lesson))
        self.assertIn("Not supported.", [b["reason"] for b in lesson["blocked"]], "the refusal's own reason is kept")

    def test_a_second_question_never_replaces_a_verdict_the_first_gave(self):
        self.ai.skip = lambda eid, asked: asked == 1 and bool(SLIDE.match(eid))
        self.ai.refuse = {"p0.0"}
        self.ai.late = (2, "p0.0")      # the answer to the second question also "passes" an element it was not asked about
        lesson = self.make()
        self.assertGreaterEqual(len(self.ai.asked), 2)
        self.assertTrue(all(SLIDE.match(eid) for eid in self.ai.asked[1]), "the second question was about the slides")
        self.assertNotIn("First point.", self.points(lesson))
        self.assertEqual(len(lesson["slides"]), 2, "the slides it was asked about were judged")

    def test_a_member_whose_hold_cannot_cover_the_second_question_is_not_asked_it(self):
        real = learning.claim_slot
        covered = []

        def claim_slot(ai, calls, first=False):
            if calls is GATE_RETRY:
                covered.append(calls)
                return nullcontext(False)   # what is left of the hold does not cover it
            return real(ai, calls, first)

        self.ai.skip = lambda eid, asked: eid == "p0.0"
        with mock.patch.object(learning, "claim_slot", claim_slot):
            lesson = self.make()
        self.assertEqual(len(covered), len(self.ai.asked), "asked for once in each round, and never answered")
        self.assertTrue(all(len(ids) > GATE_RETRY_SIZE for ids in self.ai.asked), "every question was a first, whole one")
        self.assertEqual(len(self.no_verdicts()), 1, "the element simply does not hold")
        self.assertNotIn("First point.", self.points(lesson))

    def test_the_second_question_is_asked_in_small_parts_and_its_claim_matches(self):
        self.assertEqual(GATE_RETRY, (("gate", "gate_elements", GATE_RETRY_ELEMENTS // GATE_RETRY_SIZE * TRIES),))
        self.ai.skip = lambda eid, call: call == 1   # the whole first answer is without verdicts
        lesson = self.make()
        self.assertGreater(len(self.ai.asked[0]), GATE_RETRY_SIZE, "the first question was one big part")
        self.assertGreater(len(self.ai.asked), 2)
        self.assertTrue(all(len(ids) <= GATE_RETRY_SIZE for ids in self.ai.asked[1:]), [len(a) for a in self.ai.asked])
        self.assertEqual(len(lesson["slides"]), 2)
        self.assertEqual(self.no_verdicts(), [])

    def test_a_lesson_whose_author_wrote_no_usable_slide_is_not_kept(self):
        real = self.ai._lesson
        self.ai._lesson = lambda user: {**real(user), "slides": [None, "not a slide"]}
        with self.assertRaises(UserError) as caught:
            self.make()
        self.assertIn("The author wrote no usable slide", str(caught.exception))
        self.assertEqual(self.dojo.lessons_for("gh-300", self.sid), [])

    def test_slides_with_nothing_on_them_do_not_make_a_lesson_whose_real_slides_all_held_too_short(self):
        real = self.ai._lesson

        def lesson(user):
            out = real(user)
            return {**out, "slides": out["slides"] + [{"title": "Closing", "bullets": [], "narration": []}] * 4}
        self.ai._lesson = lesson
        kept = self.make()
        self.assertEqual(len(kept["slides"]), 2, "both real slides held, and the empty ones never could")
        self.assertEqual(self.no_verdicts(), [])


if __name__ == "__main__":
    unittest.main()
