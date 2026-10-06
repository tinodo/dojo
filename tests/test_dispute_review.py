"""The review of a dispute (ADR 0017). Filing a dispute takes the judgement out of what the Record counts, and the
referee - neither the grader nor the checker - reviews the whole judgement with the learner's reason. Upheld: its
judgement replaces the disputed one, which stays on the item, as when an answer is judged again. Not upheld: the
judgement counts again, with the referee's reasons. When the review cannot run, the dispute stays open."""
import contextlib
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-dispute-import-"))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ai as ai_mod  # noqa: E402
from app.budget import Refused  # noqa: E402
from app.core import Learner, learner_key  # noqa: E402
from app.learning import GRADING_VERSION, REVIEW_ALL_REFEREED, REVIEW_EXAM, REVIEW_OWN, judge_again_refusal  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

# The stub grader meets every point of an answer with "everything" in it, quoting its first five words.
ANSWER = "I would cover everything here: the first idea and the second idea, in plain words."
QUOTE = "I would cover everything here:"
REASON = "The second point is in my answer: 'the second idea'."


def grader_refusing(rid: str):
    """A grader that meets every point but one."""
    def grade(user: str) -> dict:
        return {"points": [{"id": r, "met": r != rid, "learner_quote": "" if r == rid else QUOTE, "why": f"Grader on {r}."}
                           for r in ai_mod._RUBRIC.findall(user)],
                "feedback": "Stub feedback.", "misconception": ""}
    return grade


def referee_refusing(rid: str):
    """A referee that agrees with grader_refusing(rid): every point met but that one."""
    def referee(user: str) -> dict:
        return {"points": [{"id": r, "met": r != rid, "learner_quote": "" if r == rid else QUOTE, "why": f"Referee on {r}."}
                           for r in ai_mod._RUBRIC.findall(user)]}
    return referee


class DisputeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-dispute-")
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.c = TestClient(self.app)
        self.dojo = self.app.state.dojo
        self.me = Learner("local:dev", learner_key("local:dev"), "Local learner")
        self.pid = "gh-300"
        pkg = self.dojo.packages.get(self.pid)
        self.sid = next(s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]
                        if pkg["skills"][s]["sources"] and pkg["skills"][s]["kind"] == "scenario")

    def wait(self, job: dict, ok: bool = True) -> dict:
        for _ in range(900):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        if ok:
            self.assertEqual(job["state"], "done", job.get("error"))
            return job["result"]
        return job

    def judged_one_short(self) -> str:
        """An answered check item, judged with every point met but r2."""
        r = self.c.post("/api/items", json={"package": self.pid, "skill": self.sid, "mode": "check"}, headers=POST)
        item = self.wait(r.json())["item"]
        with mock.patch.object(self.dojo.ai, "_grade", grader_refusing("r2")):
            self.wait(self.c.post(f"/api/items/{item}/answer", json={"text": ANSWER, "elapsed_s": 30}, headers=POST).json())
        return item

    def dispute(self, item: str, referee=None) -> dict:
        with mock.patch.object(self.dojo.ai, "_referee", referee) if referee else contextlib.nullcontext():
            out = self.c.post(f"/api/items/{item}/dispute", json={"reason": REASON}, headers=POST)
            self.assertEqual(out.status_code, 200, out.text)
            out = out.json()
            if out.get("review"):
                out["job"] = self.wait(out["review"], ok=False)
        return out

    def view(self, item: str) -> dict:
        return self.c.get(f"/api/items/{item}").json()

    def attempt(self, item: str) -> dict:
        skill = self.c.get(f"/api/skills/{self.pid}/{self.sid}").json()
        return next(a for a in skill["evidence"]["attempts"] if a["item"] == item)

    def events(self, item: str, kind: str) -> list[dict]:
        return [e for e in self.dojo.store.events(self.me.key) if e["type"] == kind and e["data"].get("item") == item]

    def log(self, item: str) -> list[str]:
        return [q["reason"] for q in self.dojo.store.read_lines("learners", self.me.key, "quality.jsonl") if q.get("item") == item]

    def test_an_upheld_dispute_replaces_the_judgement_and_keeps_the_old_one(self):
        item = self.judged_one_short()
        before = self.view(item)["result"]
        out = self.dispute(item)   # the stub referee meets every point
        self.assertEqual(out["job"]["state"], "done", out["job"].get("error"))
        self.assertEqual((out["job"]["result"]["outcome"], out["job"]["result"]["changed"]), ("upheld", ["r2"]))
        v = self.view(item)
        r = v["result"]
        self.assertEqual((r["met"], r["total"], r["version"]), (before["total"], before["total"], GRADING_VERSION))
        r2 = next(p for p in r["points"] if p["id"] == "r2")
        self.assertEqual((r2["learner_quote"], r2["refereed"]), (QUOTE, {"grader_met": False, "objection": "Your dispute."}))
        self.assertTrue(r["feedback_withheld"])
        self.assertEqual(r["rejudged"]["by"], "dispute review")
        self.assertIn("referee", r["models"])
        self.assertIsNone(v["disputed"])
        self.assertFalse(v["dispute_open"])
        old = v["history"][-1]
        self.assertEqual(old["result"]["met"], before["met"])
        self.assertEqual((old["disputed"]["reason"], old["disputed"]["review"]["outcome"]), (REASON, "upheld"))
        a = self.attempt(item)
        self.assertEqual((a["met"], a["disputed"]), (before["total"], False), "the corrected judgement counts")
        self.assertEqual(a["earlier"][-1]["disputed"], True)
        self.assertEqual(self.events(item, "dispute.reviewed")[0]["data"]["outcome"], "upheld")
        self.assertEqual(self.events(item, "answer.rejudged")[0]["data"]["by"], "dispute review")
        self.assertIsNotNone(judge_again_refusal(self.dojo.item(self.me, item)))
        self.assertTrue(any("Your dispute was upheld" in x for x in self.log(item)))

    def test_a_dispute_not_upheld_lets_the_judgement_count_again(self):
        item = self.judged_one_short()
        out = self.dispute(item, referee=referee_refusing("r2"))
        self.assertEqual(out["job"]["result"]["outcome"], "not_upheld")
        v = self.view(item)
        self.assertEqual(v["result"]["met"], v["result"]["total"] - 1, "the judgement is unchanged")
        self.assertEqual(v["disputed"]["review"]["outcome"], "not_upheld")
        self.assertEqual([p["why"] for p in v["disputed"]["review"]["points"] if p["id"] == "r2"], ["Referee on r2."])
        self.assertFalse(v["dispute_open"])
        self.assertIsNone(v["dispute_review"])
        self.assertFalse(self.attempt(item)["disputed"], "it counts again")
        self.assertEqual(len(self.events(item, "answer.rejudged")), 0)
        self.assertTrue(any("not upheld" in x for x in self.log(item)))
        again = self.c.post(f"/api/items/{item}/dispute", json={"reason": REASON}, headers=POST)
        self.assertEqual(again.status_code, 409, "one dispute per judgement")

    def test_a_review_that_fails_leaves_the_dispute_open_and_can_run_again(self):
        item = self.judged_one_short()
        with mock.patch.object(self.dojo, "_referee", side_effect=ai_mod.AIError("author model: HTTP 503")):
            out = self.dispute(item)
        self.assertEqual(out["job"]["state"], "failed")
        v = self.view(item)
        self.assertTrue(v["dispute_open"])
        self.assertEqual(v["dispute_review"], {"busy": False, "refusal": None, "waits": None})
        self.assertTrue(self.attempt(item)["disputed"], "an open dispute counts neither way")
        self.assertEqual(len(self.events(item, "dispute.reviewed")), 0)
        job = self.c.post(f"/api/items/{item}/dispute/review", headers=POST)
        self.assertEqual(job.status_code, 200, job.text)
        self.assertEqual(self.wait(job.json())["outcome"], "upheld")
        self.assertFalse(self.attempt(item)["disputed"])

    def test_the_referee_does_not_review_its_own_decision(self):
        item = self.judged_one_short()
        self.dispute(item)   # upheld: the referee's judgement replaces the grader's
        out = self.dispute(item)
        self.assertIsNone(out["review"])
        self.assertEqual(out["review_waits"], REVIEW_OWN)
        v = self.view(item)
        self.assertTrue(v["dispute_open"])
        self.assertEqual(v["dispute_review"]["refusal"], REVIEW_OWN)
        self.assertTrue(self.attempt(item)["disputed"])
        refused = self.c.post(f"/api/items/{item}/dispute/review", headers=POST)
        self.assertEqual((refused.status_code, refused.json()["detail"]), (409, REVIEW_OWN))

    def test_a_timed_exam_that_opens_first_stops_the_review(self):
        item = self.judged_one_short()
        with mock.patch.object(self.dojo, "open_attempt", lambda learner: {"id": "exam-1"}):
            out = self.dispute(item)
        self.assertEqual(out["job"]["state"], "failed")
        self.assertIn(REVIEW_EXAM, out["job"]["error"])
        self.assertTrue(self.view(item)["dispute_open"])

    def test_points_the_referee_decided_when_judged_are_not_reviewed_again(self):
        item = self.judged_one_short()
        it = self.dojo.item(self.me, item)
        for p in it["result"]["points"]:
            if p["id"] == "r1":
                p["refereed"] = {"grader_met": True, "objection": "The checker found the reason does not hold."}
        self.dojo._save_item(self.me, it)
        asked: list[list[str]] = []

        def referee(user: str) -> dict:
            asked.append(ai_mod._RUBRIC.findall(user))
            return {"points": [{"id": r, "met": True, "learner_quote": QUOTE, "why": "Referee."} for r in asked[-1]]}
        out = self.dispute(item, referee=referee)
        self.assertEqual(asked, [["r2"]], "only the points the grader decided are reviewed")
        self.assertEqual(out["job"]["result"]["changed"], ["r2"])
        v = self.view(item)
        r1 = next(p for p in v["result"]["points"] if p["id"] == "r1")
        self.assertEqual(r1["refereed"]["objection"], "The checker found the reason does not hold.", "r1 stands as decided")
        self.assertEqual(v["history"][-1]["disputed"]["review"]["not_reviewed"], ["r1"])
        self.assertTrue(v["result"]["all_met"])

    def test_a_judgement_the_referee_decided_entirely_is_not_reviewed(self):
        item = self.judged_one_short()
        it = self.dojo.item(self.me, item)
        for p in it["result"]["points"]:
            p["refereed"] = {"grader_met": p["met"], "objection": "x"}
        self.dojo._save_item(self.me, it)
        out = self.dispute(item)
        self.assertEqual((out["review"], out["review_waits"]), (None, REVIEW_ALL_REFEREED))
        self.assertTrue(self.view(item)["dispute_open"])

    def test_a_dispute_is_filed_even_when_its_review_cannot_start(self):
        item = self.judged_one_short()
        with mock.patch.object(self.dojo, "review_dispute", side_effect=HTTPException(409, "This answer is already being judged.")):
            out = self.dispute(item)
        self.assertIsNone(out["review"])
        self.assertIn("Your dispute is filed", out["review_waits"])
        self.assertIn("This answer is already being judged.", out["review_waits"])
        self.assertTrue(self.view(item)["dispute_open"])
        self.assertEqual(len(self.events(item, "dispute.filed")), 1)

    def test_when_the_budget_refuses_the_dispute_is_filed_and_its_review_waits(self):
        item = self.judged_one_short()
        with mock.patch.object(self.app.state.budget, "action", side_effect=Refused("You have used today's allowance.")):
            out = self.dispute(item)
        self.assertIsNone(out["review"])
        self.assertIn("Your dispute is filed", out["review_waits"])
        self.assertIn("You have used today's allowance.", out["review_waits"])
        self.assertTrue(self.view(item)["dispute_open"])
        self.assertEqual(len(self.events(item, "dispute.filed")), 1)


if __name__ == "__main__":
    unittest.main()
