"""Judge again (ADR 0015). A judged answer whose judgement was made with older judging rules can be judged
again with today's, once, when the learner asks. The Record only grows: the new judgement is a new event
that points at the one it replaces, the earlier judgement stays on the item, and everything that reads
judgements reads the one that counts now."""
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-judge-again-import-"))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ai as ai_mod  # noqa: E402
from app import evidence, play  # noqa: E402
from app.budget import Refused  # noqa: E402
from app.core import Learner, learner_key  # noqa: E402
from app.learning import GRADING_CHANGES, GRADING_VERSION, judge_again_refusal, judgement_version  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

# The stub grader meets every point of an answer with "everything" in it, quoting its first five words.
ANSWER = "I would cover everything here: the first idea and the second idea, in plain words."
WHY = "My answer says both ideas. Did you read it?"


def old_grader(user: str) -> dict:
    """A judgement as older rules could make it: "met" with no quote of the answer, which counts as not met."""
    return {"points": [{"id": rid, "met": True, "learner_quote": "", "why": "met by omission"}
                       for rid in ai_mod._RUBRIC.findall(user)],
            "feedback": "Older feedback.", "misconception": "Thinks the second idea is optional."}


class JudgeAgainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-judge-again-")
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.c = TestClient(self.app)
        self.dojo = self.app.state.dojo
        self.me = Learner("local:dev", learner_key("local:dev"), "Local learner")
        self.pid = "gh-300"
        self.pkg = self.dojo.packages.get(self.pid)
        self.sids = [s for d in self.pkg["domains"] for g in d["groups"] for s in g["skills"]
                     if self.pkg["skills"][s]["sources"] and self.pkg["skills"][s]["kind"] == "scenario"]
        self.turn = 0

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

    def post(self, path: str, body: dict | None = None, status: int | None = None) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        if status is None:
            self.assertLess(r.status_code, 300, r.text)
        else:
            self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def answered(self, old: bool = True, confidence: str | None = None) -> tuple[str, str]:
        """An answered check item, judged 0 of 2 by older rules (old=True), or by today's."""
        sid = self.sids[self.turn % len(self.sids)]
        self.turn += 1
        item = self.wait(self.post("/api/items", {"package": self.pid, "skill": sid, "mode": "check"}))["item"]
        body = {"text": ANSWER, "elapsed_s": 40, **({"confidence": confidence} if confidence else {})}
        if old:
            with mock.patch.object(self.dojo.ai, "_grade", old_grader):
                self.wait(self.post(f"/api/items/{item}/answer", body))
            it = self.dojo.item(self.me, item)
            it["result"].pop("version")   # judged before versions were kept
            self.dojo._save_item(self.me, it)
        else:
            self.wait(self.post(f"/api/items/{item}/answer", body))
        return item, sid

    def view(self, item: str) -> dict:
        return self.c.get(f"/api/items/{item}").json()

    def judge_again(self, item: str) -> dict:
        return self.wait(self.post(f"/api/items/{item}/judge-again"))

    def events(self, kind: str | None = None) -> list[dict]:
        evs = self.dojo.store.events(self.me.key)
        return [e for e in evs if kind is None or e["type"] == kind]

    def attempt(self, sid: str, item: str) -> dict:
        skill = self.c.get(f"/api/skills/{self.pid}/{sid}").json()
        return next(a for a in skill["evidence"]["attempts"] if a["item"] == item)

    # ---- who can ask, and how often

    def test_an_old_judgement_can_be_judged_again_once(self):
        item, _ = self.answered()
        before = self.view(item)
        self.assertEqual((before["result"]["met"], before["result"]["total"]), (0, 2))
        self.assertEqual(before["judge_again"], {"busy": False, "version": 1, "current": GRADING_VERSION, "waits": None,
                                                 "changes": [GRADING_CHANGES[v] for v in range(2, GRADING_VERSION + 1)]})
        self.assertEqual(before["history"], [])
        self.assertEqual(self.judge_again(item), {"item": item, "met": 2, "total": 2, "rejudged": True})
        after = self.view(item)
        r = after["result"]
        self.assertEqual((r["met"], r["total"], r["version"]), (2, 2, GRADING_VERSION))
        self.assertEqual(r["rejudged"]["at"], r["judged_at"])
        self.assertEqual({k: r["rejudged"]["replaces"][k] for k in ("judged_at", "met", "total", "version", "disputed")},
                         {"judged_at": before["result"]["judged_at"], "met": 0, "total": 2, "version": 1, "disputed": False})
        # The earlier judgement stays on the item, as it was.
        self.assertEqual(len(after["history"]), 1)
        self.assertEqual(after["history"][0]["result"], before["result"])
        self.assertEqual(after["history"][0]["replaced_at"], r["judged_at"])
        self.assertIsNone(after["judge_again"])
        # Once only, whichever way it came out.
        refused = self.post(f"/api/items/{item}/judge-again", status=409)
        self.assertIn("already judged again once", refused["detail"])
        self.assertEqual(len(self.events("answer.rejudged")), 1)

    def test_a_judgement_with_todays_rules_or_none_cannot_be_judged_again(self):
        item, _ = self.answered(old=False)
        it = self.view(item)
        self.assertEqual(it["result"]["version"], GRADING_VERSION)
        self.assertIsNone(it["judge_again"])
        refused = self.post(f"/api/items/{item}/judge-again", status=409)
        self.assertIn("today's judging rules", refused["detail"])
        fresh = self.wait(self.post("/api/items", {"package": self.pid, "skill": self.sids[0], "mode": "check"}))["item"]
        self.assertIn("no answer", self.post(f"/api/items/{fresh}/judge-again", status=409)["detail"])
        self.assertEqual(self.events("answer.rejudged"), [])
        # The rule itself: a missing version is the oldest, and only one judging again ever.
        self.assertEqual(judgement_version({}), 1)
        self.assertEqual(judgement_version({"version": True}), 1)
        self.assertEqual(judgement_version({"version": GRADING_VERSION}), GRADING_VERSION)
        judged = {"answer": {"text": "x"}, "result": {"met": 0, "total": 1}}
        self.assertIsNone(judge_again_refusal(judged))
        self.assertIn("not judged yet", judge_again_refusal({"answer": {"text": "x"}, "result": None}))
        self.assertIn("once", judge_again_refusal({**judged, "history": [{"result": {}}]}))
        # Every version after the first says, in the learner's words, what it changed.
        self.assertEqual(sorted(GRADING_CHANGES), list(range(2, GRADING_VERSION + 1)))
        self.assertTrue(all(isinstance(t, str) and t.strip() for t in GRADING_CHANGES.values()))

    def test_only_a_judgement_that_can_be_judged_again_shows_it_running(self):
        """A grade job is busy until it ends, a moment after it has saved its judgement. Then the answer has
        today's judgement, or was judged again already, and is not shown as being judged again."""
        new, _ = self.answered(old=False)
        old, _ = self.answered()
        again, _ = self.answered()
        self.judge_again(again)
        with mock.patch.object(self.dojo.jobs, "busy", return_value=True):
            self.assertIsNone(self.view(new)["judge_again"])
            self.assertIsNone(self.view(again)["judge_again"])
            self.assertEqual(self.view(old)["judge_again"], {"busy": True})
        self.assertFalse(self.view(old)["judge_again"]["busy"])

    def test_another_learner_cannot_judge_my_answer_again(self):
        item, _ = self.answered()
        other = Learner("local:other", learner_key("local:other"), "Someone else")
        with self.assertRaises(HTTPException) as caught:
            self.dojo.judge_again(other, item)
        self.assertEqual(caught.exception.status_code, 404)

    def test_judging_again_is_closed_while_a_timed_exam_runs(self):
        item, _ = self.answered()
        with mock.patch.object(self.app.state.exams, "open_attempt", return_value={"id": "exam-x"}):
            refused = self.post(f"/api/items/{item}/judge-again", status=409)
        self.assertIn("timed practice exam is running", refused["detail"])
        self.assertEqual(self.events("answer.rejudged"), [])
        self.assertIsNotNone(self.view(item)["judge_again"])

    def test_a_refused_model_call_changes_nothing(self):
        item, _ = self.answered()
        before = self.dojo.item(self.me, item)
        with mock.patch.object(self.dojo, "_judge", side_effect=Refused("The shared cap is full.")):
            job = self.wait(self.post(f"/api/items/{item}/judge-again"), ok=False)
        self.assertEqual(job["state"], "failed")
        self.assertIn("Nothing was changed: the earlier judgement stands.", job["error"])
        self.assertEqual(self.dojo.item(self.me, item), before)
        self.assertEqual(self.events("answer.rejudged"), [])
        self.assertIsNotNone(self.view(item)["judge_again"])   # it can still be asked for

    def test_a_checker_that_cannot_be_reached_changes_nothing(self):
        """An answer is judged again only once, so never with a checker that could not check the judgement."""
        item, _ = self.answered()
        before = self.dojo.item(self.me, item)
        with mock.patch.object(self.dojo, "_gate_judgement", side_effect=ai_mod.AIError("gate model: HTTP 503")):
            job = self.wait(self.post(f"/api/items/{item}/judge-again"), ok=False)
        self.assertEqual(job["state"], "failed")
        self.assertIn("Nothing was changed: the earlier judgement stands. The checker could not be reached", job["error"])
        self.assertEqual(self.dojo.item(self.me, item), before)
        self.assertEqual(self.events("answer.rejudged"), [])
        self.assertIsNotNone(self.view(item)["judge_again"])   # the one time is not used up

    def test_a_second_request_while_one_runs_is_refused_before_it_costs(self):
        """The check and the start of the job are one step, even when a quick check said nothing runs."""
        item, _ = self.answered()
        go, real = threading.Event(), self.dojo._judge

        def slow(progress, it):
            go.wait(10)
            return real(progress, it)
        with mock.patch.object(self.dojo, "_judge", side_effect=slow) as judge, \
                mock.patch.object(self.dojo.jobs, "busy", return_value=False):
            first = self.post(f"/api/items/{item}/judge-again")
            refused = self.post(f"/api/items/{item}/judge-again", status=409)
            go.set()
            self.wait(first)
        self.assertIn("already being judged", refused["detail"])
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(len(self.events("answer.rejudged")), 1)

    def test_an_exam_that_starts_before_the_job_runs_stops_it_before_any_model_call(self):
        item, _ = self.answered()
        before = self.dojo.item(self.me, item)
        # The request came in before the exam started (the route let it through); the job then finds it open.
        with mock.patch.object(self.dojo, "open_attempt", return_value={"id": "exam-x"}), \
                mock.patch.object(self.dojo, "_judge") as judge:
            job = self.wait(self.post(f"/api/items/{item}/judge-again"), ok=False)
        judge.assert_not_called()
        self.assertEqual(job["state"], "failed")
        self.assertIn("A timed practice exam started, so nothing was changed", job["error"])
        self.assertEqual(self.dojo.item(self.me, item), before)
        self.assertEqual(self.events("answer.rejudged"), [])

    def test_an_exam_that_starts_while_judging_again_changes_nothing(self):
        item, _ = self.answered()
        before = self.dojo.item(self.me, item)
        opened, real = [], self.dojo._judge

        def judge_then_open(progress, it):
            out = real(progress, it)
            opened.append(True)   # the learner starts a timed exam while the answer is being judged
            return out
        with mock.patch.object(self.dojo, "_judge", side_effect=judge_then_open), \
                mock.patch.object(self.dojo, "open_attempt", side_effect=lambda learner: {"id": "exam-x"} if opened else None):
            job = self.wait(self.post(f"/api/items/{item}/judge-again"), ok=False)
        self.assertEqual(opened, [True])
        self.assertEqual(job["state"], "failed")
        self.assertIn("A timed practice exam started, so nothing was changed", job["error"])
        self.assertEqual(self.dojo.item(self.me, item), before)
        self.assertEqual(self.events("answer.rejudged"), [])
        self.assertIsNotNone(self.view(item)["judge_again"])   # it can be asked for after the exam

    # ---- the Record only grows

    def test_the_record_only_grows_and_the_new_event_points_at_the_old_one(self):
        item, _ = self.answered()
        before = self.events()
        judged = next(e for e in before if e["type"] == "answer.judged" and e["data"]["item"] == item)
        self.judge_again(item)
        after = self.events()
        self.assertEqual(after[:len(before)], before)   # nothing earlier was changed or removed
        added = [e for e in after[len(before):]]
        self.assertEqual([e["type"] for e in added], ["answer.rejudged"])
        d = added[0]["data"]
        self.assertEqual((d["item"], d["met"], d["total"], d["version"]), (item, 2, 2, GRADING_VERSION))
        self.assertTrue(all(p["met"] and p["learner_quote"] for p in d["points"]))
        self.assertEqual((d["replaces"]["event"], d["replaces"]["hash"]), (judged["id"], judged["hash"]))
        self.assertEqual((d["replaces"]["met"], d["replaces"]["version"]), (0, 1))
        self.assertEqual(self.dojo.store.verify_chain(self.me.key), {"ok": True, "events": len(after)})
        # The studio log says what happened, and the recall log marks its line.
        log = [x for x in self.c.get("/api/quality").json() if x.get("item") == item and x["kind"] == "judged again"]
        self.assertEqual(len(log), 1)
        self.assertIn("2 of 2 points met. The earlier judgement (0 of 2, version 1) no longer counts", log[0]["reason"])
        lines = [x for x in self.dojo.store.read_lines("learners", self.me.key, "recall_log.jsonl") if x.get("item") == item]
        self.assertEqual([bool(x.get("rejudged")) for x in lines], [False, True])

    # ---- everything reads the judgement that counts now

    def test_evidence_know_relearn_readiness_moments_and_export_read_the_new_judgement(self):
        item, sid = self.answered(confidence="certain")
        a = self.attempt(sid, item)
        self.assertEqual((a["met"], a["all_met"]), (0, False))
        skill = self.c.get(f"/api/skills/{self.pid}/{sid}").json()
        self.assertEqual(skill["evidence"]["state"], "not_met")
        self.assertFalse(skill["relearn"]["started"])   # a miss does not start the recall ladder
        cal = self.c.get(f"/api/calibration?package={self.pid}").json()
        self.assertEqual((cal["levels"]["certain"]["right"], cal["levels"]["certain"]["total"]), (0, 1))
        self.assertEqual([e["skill"] for e in cal["errors"]], [sid])
        ready = self.c.get(f"/api/readiness?package={self.pid}").json()
        self.assertEqual(next(r for r in ready["rules"] if r["id"] == "rule3")["found"], 1)
        moments = play.moments(self.events(), {self.pid: self.pkg}, 20)
        self.assertFalse(any(m["skill"] == sid for m in moments))

        self.judge_again(item)

        a = self.attempt(sid, item)
        self.assertEqual((a["met"], a["total"], a["all_met"], a["disputed"]), (2, 2, True, False))
        self.assertEqual(a["earlier"], [{"at": a["at"], "met": 0, "total": 2, "disputed": False}])
        self.assertIsNotNone(a["rejudged_at"])
        self.assertIn("Judged again with newer judging rules: the earlier judgement (0 of 2) no longer counts", a["conditions"])
        skill = self.c.get(f"/api/skills/{self.pid}/{sid}").json()
        self.assertEqual(skill["evidence"]["state"], "met_unaided")
        self.assertTrue(skill["evidence"]["checks"]["unaided"])
        self.assertEqual(len(skill["evidence"]["attempts"]), 1)   # the same answer, not a second attempt
        self.assertEqual(skill["evidence"]["last_teaching"][:19], a["rejudged_at"][:19])   # the new feedback teaches
        self.assertTrue(skill["relearn"]["started"])   # the new success starts it
        self.assertEqual(skill["relearn"]["step"], 0)
        listed = next(x for x in skill["items"] if x["id"] == item)
        self.assertEqual(listed["result"], {"met": 2, "total": 2})
        cal = self.c.get(f"/api/calibration?package={self.pid}").json()
        self.assertEqual((cal["levels"]["certain"]["right"], cal["levels"]["certain"]["total"]), (1, 1))
        self.assertEqual(cal["errors"], [])
        ready = self.c.get(f"/api/readiness?package={self.pid}").json()
        self.assertEqual(next(r for r in ready["rules"] if r["id"] == "rule3")["found"], 0)   # the new judgement names none
        moments = play.moments(self.events(), {self.pid: self.pkg}, 20)
        self.assertEqual([m["kind"] for m in moments if m["skill"] == sid], ["changed"])
        out = self.c.get("/api/record/export").json()
        doc = next(x for x in out["items"] if x["id"] == item)
        self.assertEqual((doc["result"]["met"], doc["history"][0]["result"]["met"]), (2, 0))
        self.assertEqual([e["type"] for e in out["events"] if (e.get("data") or {}).get("item") == item][-1], "answer.rejudged")

    def test_play_points_count_the_answer_once(self):
        item, _ = self.answered()
        points = play.points(self.events(), datetime.now(timezone.utc).date())
        self.judge_again(item)
        self.assertEqual(play.points(self.events(), datetime.now(timezone.utc).date()), points)

    # ---- a dispute stays with the judgement it was about

    def open_dispute(self, item: str, reason: str) -> None:
        """A dispute whose review could not run (the referee unreachable): it stays open (ADR 0017)."""
        with mock.patch.object(self.dojo, "_referee", side_effect=ai_mod.AIError("author model: HTTP 503")):
            out = self.post(f"/api/items/{item}/dispute", {"reason": reason})
            self.assertEqual(self.wait(out["review"], ok=False)["state"], "failed")

    def test_a_dispute_stays_on_record_with_the_earlier_judgement(self):
        item, sid = self.answered()
        self.open_dispute(item, WHY)
        filed = self.events("dispute.filed")[-1]["data"]
        first = self.dojo.item(self.me, item)
        self.assertEqual((filed["judged_at"], filed["version"]), (first["result"]["judged_at"], 1))
        a = self.attempt(sid, item)
        self.assertTrue(a["disputed"])
        self.assertEqual(self.c.get(f"/api/skills/{self.pid}/{sid}").json()["evidence"]["state"], "untested")
        it = self.view(item)
        self.assertIsNotNone(it["judge_again"])   # a disputed old judgement can be judged again too

        self.judge_again(item)

        it = self.view(item)
        self.assertIsNone(it["disputed"])   # the new judgement is not disputed
        self.assertEqual(it["history"][0]["disputed"]["reason"], WHY)
        self.assertTrue(it["result"]["rejudged"]["replaces"]["disputed"])
        self.assertEqual(self.events("dispute.filed")[-1]["data"], filed)   # still on record, unchanged
        a = self.attempt(sid, item)
        self.assertFalse(a["disputed"])
        self.assertEqual(a["earlier"][0]["disputed"], True)
        self.assertIn("Judged again with newer judging rules: the earlier judgement (0 of 2) no longer counts. "
                      "Your dispute of it stays on record", a["conditions"])
        self.assertEqual(self.c.get(f"/api/skills/{self.pid}/{sid}").json()["evidence"]["state"], "met_unaided")
        log = [x for x in self.c.get("/api/quality").json() if x.get("item") == item and x["kind"] == "judged again"]
        self.assertIn("Your dispute of it stays on record.", log[0]["reason"])

        # The new judgement can be disputed on its own, and then it counts neither way while its review is open.
        self.open_dispute(item, "The new one is wrong too.")
        again = self.events("dispute.filed")[-1]["data"]
        self.assertEqual(again["version"], GRADING_VERSION)
        self.assertEqual(again["judged_at"], self.dojo.item(self.me, item)["result"]["judged_at"])
        self.assertTrue(self.attempt(sid, item)["disputed"])
        self.assertEqual(self.c.get(f"/api/skills/{self.pid}/{sid}").json()["evidence"]["state"], "untested")
        self.assertIn("already disputed", self.post(f"/api/items/{item}/dispute", {"reason": "Once more."}, status=409)["detail"])


class DeriveTests(unittest.TestCase):
    """evidence.derive on a Record with an answer judged again: pure, no app."""

    def setUp(self):
        self.pkg = {"meta": {"id": "p-1"}, "skills": {"s1": {"kind": "scenario", "text": "Skill one"}}}
        self.t0 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

    def ev(self, minutes: int, etype: str, **data) -> dict:
        return {"type": etype, "at": (self.t0 + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                "data": {"package": "p-1", "skill": "s1", "item": "i1", **data}}

    def record(self, *extra: dict) -> list[dict]:
        return [self.ev(0, "item.issued", mode="check", kind="scenario", changed_condition=False),
                self.ev(5, "answer.submitted", mode="check"),
                self.ev(6, "answer.judged", mode="check", met=0, total=4, unverified=3, feedback_shown=True),
                *extra]

    def test_the_new_judgement_replaces_the_old_in_place(self):
        out = evidence.derive(self.record(self.ev(600, "answer.rejudged", met=4, total=4, unverified=0, feedback_shown=True)),
                              self.pkg, 20)["s1"]
        [a] = out["attempts"]
        self.assertEqual((a["met"], a["total"], a["all_met"], a["unverified_points"]), (4, 4, True, 0))
        self.assertEqual(a["at"], self.ev(6, "x")["at"])   # answered when it was answered
        self.assertIsNone(a["minutes_since_teaching"])     # under the conditions it was answered in
        self.assertEqual(a["earlier"], [{"at": a["at"], "met": 0, "total": 4, "disputed": False}])
        self.assertEqual(a["rejudged_at"], self.ev(600, "x")["at"])
        self.assertNotIn("3 judgement(s) could not be verified and count as not met", a["conditions"])
        self.assertEqual(out["state"], "met_unaided")
        self.assertEqual(out["last_teaching"], self.ev(600, "x")["at"])

    def test_a_dispute_belongs_to_the_judgement_it_was_filed_against(self):
        disputed_first = self.record(self.ev(10, "dispute.filed", reason="r"),
                                     self.ev(600, "answer.rejudged", met=4, total=4, unverified=0, feedback_shown=True))
        out = evidence.derive(disputed_first, self.pkg, 20)["s1"]
        self.assertFalse(out["attempts"][0]["disputed"])
        self.assertTrue(out["attempts"][0]["earlier"][0]["disputed"])
        self.assertEqual((out["state"], out["disputed"]), ("met_unaided", 0))
        disputed_both = disputed_first + [self.ev(700, "dispute.filed", reason="again")]
        out = evidence.derive(disputed_both, self.pkg, 20)["s1"]
        self.assertTrue(out["attempts"][0]["disputed"])
        self.assertEqual((out["state"], out["disputed"]), ("untested", 1))
        self.assertIn("Disputed: not counted either way", out["attempts"][0]["conditions"])

    def test_a_lower_new_judgement_counts_too(self):
        lenient = self.record()[:2] + [self.ev(6, "answer.judged", mode="check", met=4, total=4, unverified=0)]
        out = evidence.derive(lenient + [self.ev(600, "answer.rejudged", met=3, total=4, unverified=1)], self.pkg, 20)["s1"]
        self.assertEqual((out["state"], out["checks"]["unaided"]), ("not_met", False))

    def test_a_judgement_again_with_no_first_judgement_is_ignored(self):
        lone = [self.ev(600, "answer.rejudged", met=4, total=4)]
        self.assertEqual(evidence.derive(lone, self.pkg, 20)["s1"]["attempts"], [])


if __name__ == "__main__":
    unittest.main()
