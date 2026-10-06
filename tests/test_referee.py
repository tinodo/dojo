"""The referee (judging rules version 3, ADR 0016). When the checker disagrees with the grader about a rubric
point - the grader's reason for it does not hold, or the fragments it quoted do not show the point - a third
model decides that point from the answer itself. Its decision counts, its quote is checked like the grader's,
and the grader's feedback is withheld when the referee changed a point, since it may now say the opposite."""
import contextlib
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-referee-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ai as ai_mod  # noqa: E402
from app.core import Learner, learner_key  # noqa: E402
from app.learning import GRADING_VERSION, learner_quote_found  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

# The stub grader meets every point of an answer with "everything" in it, quoting its first five words.
ANSWER = "I would cover everything here: the first idea and the second idea, in plain words."
OBJECTION = "The answer never says this."


def gate_rejecting(*ids: str):
    """A checker that finds the listed elements do not hold, and every other one holds."""
    def gate(user: str) -> dict:
        return {"verdicts": [{"id": e, "holds": e not in ids, "reason": OBJECTION if e in ids else ""}
                             for e in ai_mod._ELEMENT.findall(user)]}
    return gate


def referee_saying(**met: tuple[bool, str]):
    """A referee that decides each listed point as given: (met, quote)."""
    def referee(user: str) -> dict:
        return {"points": [{"id": rid, "met": met[rid][0], "learner_quote": met[rid][1], "why": f"Referee on {rid}."}
                           for rid in ai_mod._RUBRIC.findall(user)]}
    return referee


def grader_refusing(*ids: str):
    """A grader that meets every point but the listed ones."""
    def grade(user: str) -> dict:
        quote = " ".join(ai_mod._ANSWER.search(user).group(1).split()[:5])
        return {"points": [{"id": rid, "met": rid not in ids, "learner_quote": "" if rid in ids else quote,
                            "why": f"Grader on {rid}."} for rid in ai_mod._RUBRIC.findall(user)],
                "feedback": "Stub feedback.", "misconception": "A stub misconception."}
    return grade


class RefereeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-referee-")
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.c = TestClient(self.app)
        self.dojo = self.app.state.dojo
        self.me = Learner("local:dev", learner_key("local:dev"), "Local learner")
        pkg = self.dojo.packages.get("gh-300")
        self.sid = next(s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]
                        if pkg["skills"][s]["sources"] and pkg["skills"][s]["kind"] == "scenario")

    def wait(self, job: dict) -> dict:
        for _ in range(900):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def judged(self, **patches) -> dict:
        r = self.c.post("/api/items", json={"package": "gh-300", "skill": self.sid, "mode": "check"}, headers=POST)
        item = self.wait(r.json())["item"]
        with mock.patch.multiple(self.dojo.ai, **patches) if patches else contextlib.nullcontext():
            self.wait(self.c.post(f"/api/items/{item}/answer", json={"text": ANSWER, "elapsed_s": 30}, headers=POST).json())
        self.item = item
        return self.c.get(f"/api/items/{item}").json()["result"]

    def point(self, result: dict, rid: str) -> dict:
        return next(p for p in result["points"] if p["id"] == rid)

    def counted(self, result: dict, rid: str) -> bool:
        p = self.point(result, rid)
        return bool(p["met"] and p["verified"])

    def test_a_point_the_checker_rejects_is_decided_by_the_referee(self):
        r = self.judged(_gate_judgement=gate_rejecting("w.r1"), _referee=referee_saying(r1=(False, "")))
        p = self.point(r, "r1")
        self.assertFalse(self.counted(r, "r1"), "the referee's 'not met' counts, not the grader's 'met'")
        self.assertEqual(p["refereed"], {"grader_met": True, "objection": OBJECTION})
        self.assertEqual(p["why"], "Referee on r1.")
        self.assertEqual(r["met"], r["total"] - 1)
        self.assertEqual(r["version"], GRADING_VERSION)
        self.assertIn("referee", r["models"])
        self.assertTrue(r["feedback_withheld"], "feedback written for the grader's points may now contradict them")
        self.assertIn("referee changed a point", r["withheld_reason"])
        self.assertEqual(r["misconception"], "")
        judged = next(e for e in self.dojo.store.events(self.me.key) if e["type"] == "answer.judged")
        self.assertEqual([p for p in judged["data"]["points"] if p["id"] == "r1"], [{"id": "r1", "met": False, "learner_quote": "", "refereed": True}])
        log = [q for q in self.dojo.store.read_lines("learners", self.me.key, "quality.jsonl") if q.get("item") == self.item]
        self.assertTrue(any("a third model decided it: not met (the grader had said met)" in q["reason"] for q in log), log)

    def test_the_referee_can_give_a_point_the_grader_refused(self):
        r = self.judged(_grade=grader_refusing("r2"), _gate_judgement=gate_rejecting("w.r2"),
                        _referee=referee_saying(r2=(True, "I would cover everything here:")))
        self.assertTrue(self.counted(r, "r2"))
        self.assertEqual(self.point(r, "r2")["refereed"]["grader_met"], False)
        self.assertEqual(self.point(r, "r2")["learner_quote"], "I would cover everything here:")
        self.assertTrue(r["all_met"])

    def test_a_referee_quote_that_is_not_in_the_answer_counts_as_not_met(self):
        r = self.judged(_gate_judgement=gate_rejecting("w.r1"),
                        _referee=referee_saying(r1=(True, "these words are nowhere in the learner's answer")))
        p = self.point(r, "r1")
        self.assertFalse(self.counted(r, "r1"))
        self.assertEqual((p["learner_quote"], p["why"]), ("", ""), "a reason that argues 'met' is not shown under 'not met'")

    def test_a_referee_that_agrees_keeps_the_feedback(self):
        r = self.judged(_gate_judgement=gate_rejecting("w.r1"), _referee=referee_saying(r1=(True, "I would cover everything here:")))
        self.assertTrue(self.counted(r, "r1"))
        self.assertTrue(self.point(r, "r1")["refereed"]["grader_met"])
        self.assertFalse(r["feedback_withheld"])
        self.assertTrue(r["all_met"])

    def test_without_a_referee_the_verdict_stands_and_its_reason_goes(self):
        with mock.patch.object(self.dojo, "_referee", side_effect=ai_mod.AIError("author model: HTTP 503")):
            r = self.judged(_gate_judgement=gate_rejecting("w.r1"))
        p = self.point(r, "r1")
        self.assertTrue(self.counted(r, "r1"))
        self.assertNotIn("refereed", p)
        self.assertEqual(p["why"], "")
        self.assertNotIn("referee", r["models"])
        log = [q for q in self.dojo.store.read_lines("learners", self.me.key, "quality.jsonl") if q.get("item") == self.item]
        self.assertTrue(any("The referee could not be reached" in q["reason"] for q in log), log)

    def test_no_disagreement_no_referee_call(self):
        self.dojo.ai.calls.clear()
        r = self.judged()
        self.assertTrue(r["all_met"])
        self.assertNotIn(("author", "referee"), self.dojo.ai.calls)
        self.assertFalse(any("refereed" in p for p in r["points"]))


class NotNull(unittest.TestCase):
    def test_not_null_after_a_fragment_does_not_turn_it_around(self):
        spoken = "tenant id an int not null with a foreign key then case id big int not null and activity time datetime2 not null"
        self.assertTrue(learner_quote_found("tenant id an int\ncase id big int", spoken))
        self.assertTrue(learner_quote_found("tenant id an int … activity time datetime2", spoken))
        listed = "orderid: int not nullable\ncustomerid: int not null"
        self.assertTrue(learner_quote_found("orderid: int\ncustomerid: int", listed))

    def test_other_negations_still_turn_it_around(self):
        self.assertFalse(learner_quote_found("orderid primary key … customerid foreign key",
                                             "orderid primary key not used\ncustomerid foreign key"))
        self.assertFalse(learner_quote_found("orderid primary key … customerid foreign key",
                                             "orderid primary key: no\ncustomerid foreign key"))


if __name__ == "__main__":
    unittest.main()
