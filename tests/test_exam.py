"""The timed practice exam, its results and the readiness advice.

The rules these tests hold to: the deadline is the server's, not the browser's; a broken attempt can
be resumed while time is left; questions already seen are not asked again; the teaching clock starts
when the rationales are shown, not when the answer was given; multiple choice fills no pip; and the
export and the delete cover exam attempts and advice.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import exam, readiness  # noqa: E402
from app.core import Learner, Store, canonical, iso, sha256, utcnow  # noqa: E402
from app.evidence import derive  # noqa: E402
from app.main import create_app  # noqa: E402
from app.packages import Packages  # noqa: E402
from app.core import APP_DIR  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402
from tests.test_podcast import mp3  # noqa: E402

PACKAGES = Packages(APP_DIR / "packages")
_LEARNER = Learner("local:dev", "learner-test", "Owner")


class _ExamClient(unittest.TestCase):
    """A fresh app with a fresh record, and the few calls the page makes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-exam-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.room = cls.app.state.exams
        cls.dojo = cls.app.state.dojo
        cls.learner = _owner(cls.app)
        # A learner who has used Dojo before, which is what the background pool needs: it tops up an
        # existing record and never creates one.
        cls.dojo.store.path("learners", cls.learner.key).mkdir(parents=True, exist_ok=True)

    def post(self, path: str, body: dict | None = None, status: int = 200) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def wait(self, job: dict) -> dict:
        for _ in range(400):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def build(self, length: str = "short") -> str:
        started = self.post("/api/exams", {"package": "gh-300", "length": length})
        self.assertEqual(started["attempt"][:5], "exam-")
        self.wait(started)
        return started["attempt"]

    def backdate_last_event(self, minutes: int) -> None:
        """Move the last event back in time and re-link the chain, so the record still verifies."""
        store = self.dojo.store
        events = store.events(self.learner.key)
        events[-1]["at"] = iso(utcnow() - timedelta(minutes=minutes))
        prev = "0" * 64
        for ev in events:
            ev["prev"] = prev
            body = {k: v for k, v in ev.items() if k != "hash"}
            ev["hash"] = prev = sha256(canonical(body))
        path = store.path("learners", self.learner.key, "events.jsonl")
        with store.lock:
            path.write_text("".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events), "utf-8")
            store._heads[self.learner.key] = prev


class ExamApiTests(_ExamClient):
    """One built exam, driven through the API the way the page drives it."""

    # ---- the shape of a practice exam

    def test_lengths_come_from_what_learn_states(self):
        out = self.c.get("/api/exams?package=gh-300").json()
        self.assertEqual(out["lengths"]["full"]["questions"], 50)
        self.assertEqual(out["lengths"]["full"]["minutes"], 100)
        self.assertFalse(out["lengths"]["full"]["count_is_stated"])
        self.assertEqual(out["lengths"]["short"]["minutes"], 30)   # 15 of 50 questions, time scaled
        self.assertIn("100 minutes", out["lengths"]["full"]["duration_quote"])

    def test_dp_800_uses_its_own_stated_duration(self):
        s = exam.spec(PACKAGES.get("dp-800"), "full")
        self.assertEqual((s["minutes"], s["questions"], s["real_questions"]), (120, 50, None))

    def test_blueprint_weight_decides_the_questions(self):
        pkg = PACKAGES.get("gh-300")
        skills = exam.plan_skills(pkg, 50)
        self.assertEqual(len(skills), 50)
        per = {d["id"]: 0 for d in pkg["domains"]}
        for sid in skills:
            per[pkg["skills"][sid]["domain"]] += 1
        for d in pkg["domains"]:
            share = 100 * per[d["id"]] / 50
            self.assertTrue(d["weight_min"] - 2 <= share <= d["weight_max"] + 2, (d["id"], share))

    # ---- running one

    def test_a_full_run_scores_and_keeps_the_rationales_back(self):
        aid = self.build()
        view = self.c.get(f"/api/exams/{aid}").json()
        self.assertEqual(view["state"], "ready")
        self.assertEqual(view["items"], [], "the questions are not sent before the clock starts")
        self.assertGreater(view["questions"], 0)
        open_view = self.post(f"/api/exams/{aid}/start")
        self.assertEqual(open_view["state"], "open")
        self.assertGreater(open_view["seconds_left"], 0)
        self.assertNotIn("answer", open_view["items"][0])
        for i in range(len(open_view["items"])):
            self.post(f"/api/exams/{aid}/answer", {"index": i, "choice": i % 4})
        self.post(f"/api/exams/{aid}/flag", {"index": 1, "flagged": True})
        closed = self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        self.assertEqual(closed["state"], "closed")
        self.assertNotIn("answer", closed["items"][0], "the rationales wait for the results page")
        self.assertFalse(closed["rationales_shown"])
        shown = self.post(f"/api/exams/{aid}/rationales")
        self.assertTrue(shown["rationales_shown"])
        self.assertIn("answer", shown["items"][0])
        self.assertEqual(shown["correct"], sum(1 for it in shown["items"] if it["correct"]))
        self.assertEqual(shown["new_questions"], shown["questions"])
        self.assertIn("Not a pass prediction", shown["diagnosis_note"])
        self.assertEqual(sum(d["questions"] for d in shown["by_domain"]), shown["questions"])
        # No 700-point scale anywhere. Ids are random hex, so "700" may appear inside one: match a whole number only.
        self.assertIsNone(re.search(r"\b700\b", json.dumps(shown)), "no mapping onto the 700-point pass scale")

    def test_answers_are_refused_after_the_deadline(self):
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        doc = self.room.attempt(self.learner, aid)
        doc["deadline"] = iso(utcnow() - timedelta(seconds=1))
        self.dojo.store.write("learners", self.learner.key, "exams", aid, value=doc)
        late = self.post(f"/api/exams/{aid}/answer", {"index": 0, "choice": 1}, status=409)
        self.assertIn("Time is up", late["detail"])
        view = self.c.get(f"/api/exams/{aid}").json()
        self.assertEqual((view["state"], view["closed_reason"]), ("closed", "time"))
        self.assertEqual(view["answered"], 0)

    def test_an_exam_resumes_while_time_is_left(self):
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.post(f"/api/exams/{aid}/answer", {"index": 0, "choice": 2})
        self.post(f"/api/exams/{aid}/flag", {"index": 2, "flagged": True})
        listing = self.c.get("/api/exams?package=gh-300").json()
        self.assertEqual(listing["running"], {"id": aid, "package": "gh-300"})
        again = self.c.get(f"/api/exams/{aid}").json()   # as after a reload
        self.assertEqual(again["state"], "open")
        self.assertEqual(again["items"][0]["choice"], 2)
        self.assertEqual(again["flagged"], [2])
        self.assertGreater(again["seconds_left"], 0)
        self.post(f"/api/exams/{aid}/submit")
        self.assertIsNone(self.c.get("/api/exams?package=gh-300").json()["running"])

    def test_a_started_exam_closes_the_coach(self):
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        skill = next(iter(PACKAGES.get("gh-300")["skills"]))
        refused = self.post("/api/ask", {"package": "gh-300", "skill": skill, "question": "Tell me the answer?"}, status=409)
        self.assertIn("coach is closed", refused["detail"])
        self.post(f"/api/exams/{aid}/submit")
        self.assertIn("id", self.post("/api/ask", {"package": "gh-300", "skill": skill, "question": "What now?"}))

    def test_a_second_exam_cannot_be_started_while_one_runs(self):
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        refused = self.post("/api/exams", {"package": "gh-300", "length": "short"}, status=409)
        self.assertIn("Finish it", refused["detail"])
        self.post(f"/api/exams/{aid}/submit")
        self.post("/api/exams", {"package": "gh-300", "length": "short"})   # and it is allowed again

    def test_a_started_exam_closes_everything_that_teaches(self):
        """A rehearsal shows its rationale straight away, so it is help. So is a hint, an item's
        feedback, and a lesson in any form. While an exam is open, all of them are refused."""
        res = self.wait(self.post("/api/rehearsals", {"package": "gh-300", "count": 4}))
        rid = res["rehearsal"]
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        for path, body in ((f"/api/rehearsals/{rid}/answer", {"index": 0, "choice": 0}),
                           ("/api/rehearsals", {"package": "gh-300", "count": 4}),
                           ("/api/items/item-x/hint", None),
                           ("/api/items/item-x/answer", {"text": "something", "elapsed_s": 3}),
                           ("/api/items/item-x/grade", None),
                           ("/api/items/item-x/judge-again", None),
                           ("/api/items/item-x/dispute", {"reason": "I disagree"}),
                           ("/api/items", {"package": "gh-300", "skill": "d1.g1.s1", "mode": "practice"}),
                           ("/api/lessons", {"package": "gh-300", "skill": "d1.g1.s1"}),
                           ("/api/lessons/lesson-x/audio", None),
                           ("/api/lessons/lesson-x/video", None)):
            out = self.post(path, body, status=409)
            self.assertIn("timed practice exam is running", out["detail"])
        for path in (f"/api/rehearsals/{rid}", "/api/items/item-x", "/api/lessons/lesson-x", "/api/media/media-x",
                     "/api/ask/ask-x", "/api/quality", "/api/studio/voices"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 409, path)
            self.assertIn("timed practice exam is running", r.json()["detail"])
        closed = self.c.get("/api/record/export")
        self.assertEqual(closed.status_code, 409)
        self.assertIn("Export is closed", closed.json()["detail"])
        self.post(f"/api/exams/{aid}/submit")
        self.assertIn("correct", self.post(f"/api/rehearsals/{rid}/answer", {"index": 0, "choice": 0}))
        self.assertEqual(self.c.get("/api/record/export").status_code, 200)

    def test_an_earlier_exams_answers_are_closed_while_one_is_running(self):
        """An old results page shows answers and rationales, which is teaching. While an exam is open,
        the only exam you can open is that one."""
        done = self.build()
        self.post(f"/api/exams/{done}/start")
        self.post(f"/api/exams/{done}/submit")
        self.post(f"/api/exams/{done}/rationales")
        running = self.build()
        self.post(f"/api/exams/{running}/start")
        for path in (f"/api/exams/{done}", f"/api/exams/{running}"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 409 if path.endswith(done) else 200, path)
        self.assertEqual(self.c.post(f"/api/exams/{done}/rationales", headers=POST).status_code, 409)
        self.post(f"/api/exams/{running}/submit")
        self.assertIn("answer", self.c.get(f"/api/exams/{done}").json()["items"][0])

    def test_an_open_exam_never_shows_a_score(self):
        """While the clock runs there is no correct count and no answer key anywhere, so the options
        cannot be probed against a running score. Both arrive when the exam has ended."""
        aid = self.build()
        for _ in range(2):   # ready, then open
            listed = next(a for a in self.c.get("/api/exams?package=gh-300").json()["attempts"] if a["id"] == aid)
            self.assertIsNone(listed["correct"])
            view = self.c.get(f"/api/exams/{aid}").json()
            self.assertNotIn("correct", view)
            for it in view["items"]:
                self.assertNotIn("answer", it)
                self.assertNotIn("rationale", it)
            self.post(f"/api/exams/{aid}/start") if view["state"] == "ready" else None
        self.post(f"/api/exams/{aid}/answer", {"index": 0, "choice": 0})
        self.assertNotIn("correct", self.c.get(f"/api/exams/{aid}").json())
        closed = self.post(f"/api/exams/{aid}/submit")
        self.assertIsInstance(closed["correct"], int)
        listed = next(a for a in self.c.get("/api/exams?package=gh-300").json()["attempts"] if a["id"] == aid)
        self.assertEqual(listed["correct"], closed["correct"])

    def test_a_rationale_seen_during_an_exam_makes_the_conditions_unknown(self):
        """The routes refuse it, so this should not happen. If it happened anyway - an old tab, a
        background job landing late - the attempt cannot claim it was taken without help."""
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.dojo.store.append_event(self.learner.key, "mcq.answered",
                                     {"package": "gh-300", "skill": "1.1", "rehearsal": "reh-x", "correct": True})
        closed = self.post(f"/api/exams/{aid}/submit")
        self.assertEqual(closed["conditions"], "unknown")
        self.assertIn("rehearsal question with its rationale", closed["conditions_note"])
        self.assertNotIn("no hints", closed["conditions_note"])

    def test_a_lesson_still_playing_when_the_exam_starts_makes_the_conditions_unknown(self):
        """A lesson view is recorded when it starts and says for how long it may still be playing.
        One that started 10 minutes before the exam can still be running inside it."""
        aid = self.build()
        self.dojo.store.append_event(self.learner.key, "lesson.viewed",
                                     {"package": "gh-300", "skill": "1.1", "lesson": "les-x",
                                      "modality": "watch", "repeats_within_min": 30})
        self.backdate_last_event(10)
        self.post(f"/api/exams/{aid}/start")
        closed = self.post(f"/api/exams/{aid}/submit")
        self.assertEqual(closed["conditions"], "unknown")
        self.assertIn("a lesson", closed["conditions_note"])
        self.assertTrue(self.dojo.store.verify_chain(self.learner.key)["ok"])

    def test_seen_questions_are_avoided_and_counted(self):
        first = self.build()
        view = self.c.get(f"/api/exams/{first}").json()
        self.post(f"/api/exams/{first}/start")
        seen = exam.seen_keys(self.dojo.store, self.learner.key, "gh-300")
        for it in view["items"]:
            self.assertIn(exam.question_key(it["stem"]), seen)
        self.post(f"/api/exams/{first}/submit")   # one exam at a time
        second = self.build()
        doc = self.room.attempt(self.learner, second)
        keys = {exam.question_key(it["stem"]) for it in doc["items"]}
        self.assertFalse(keys & {exam.question_key(it["stem"]) for it in view["items"]},
                         "an exam must not repeat a question the learner has seen")
        self.assertEqual(doc["new_questions"], len(doc["items"]))

    def test_a_seen_question_is_used_rather_than_a_short_exam(self):
        """When no new question can be written, the exam is still built and says how many were new."""
        room, learner = self.room, self.learner
        written, _ = self.dojo.write_mcqs("gh-300", exam.plan_skills(PACKAGES.get("gh-300"), 4))
        exam.mark_seen(self.dojo.store, learner.key, "gh-300", [exam.question_key(it["stem"]) for it in written])
        doc = room.create(learner, "gh-300", "short")
        old = self.dojo.write_mcqs
        try:
            self.dojo.write_mcqs = lambda pid, skills, on_progress=None: (list(written), [])
            out = room.build(lambda _t: None, learner, doc["id"])
        finally:
            self.dojo.write_mcqs = old
        self.assertEqual(out["new_questions"], 0)
        self.assertEqual(out["questions"], len(written))
        view = self.c.get(f"/api/exams/{doc['id']}").json()
        self.assertEqual(view["new_questions"], 0)
        self.assertEqual(view["state"], "ready")

    def test_an_interrupted_build_is_not_taken_for_a_finished_one(self):
        doc = self.room.create(self.learner, "gh-300", "short")
        broken = self.c.get(f"/api/exams/{doc['id']}").json()
        self.assertEqual(broken["state"], "broken")
        self.assertEqual(broken["conditions"], "unknown")
        self.post(f"/api/exams/{doc['id']}/start", status=409)

    def test_help_during_an_exam_makes_the_conditions_unknown(self):
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.dojo.store.append_event(self.learner.key, "hint.shown",
                                     {"package": "gh-300", "skill": "1.1", "item": "item-x", "described": True})
        closed = self.post(f"/api/exams/{aid}/submit")
        self.assertEqual(closed["conditions"], "unknown")
        self.assertIn("unknown", closed["conditions_note"])
        self.assertNotIn("no hints", closed["conditions_note"], "a missing condition is never read in your favour")

    def test_the_list_never_calls_an_overdue_exam_running(self):
        """The deadline is the server's. An exam past it is over, so the list says so - and only then
        does it say how many were right."""
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.post(f"/api/exams/{aid}/answer", {"index": 0, "choice": 0})
        doc = self.room.attempt(self.learner, aid)
        doc["deadline"] = iso(utcnow() - timedelta(seconds=1))
        self.dojo.store.write("learners", self.learner.key, "exams", aid, value=doc)
        listing = self.c.get("/api/exams?package=gh-300").json()
        row = next(a for a in listing["attempts"] if a["id"] == aid)
        self.assertEqual((row["state"], row["closed_reason"]), ("closed", "time"))
        self.assertIsNotNone(row["correct"])
        self.assertIsNone(listing["running"])
        self.assertEqual(self.c.get("/api/record/export").status_code, 200,
                         "and an exam whose clock has run out no longer closes the export")

    def test_the_pool_fills_in_the_background_and_yields_to_your_own_work(self):
        """A pool keeps the wait short, but the learner's own jobs always come first."""
        keeper = exam.PoolKeeper(self.room, self.learner)
        jobs = self.dojo.jobs
        for_learner = jobs.for_learner
        try:
            jobs.for_learner = lambda learner: [{"state": "running", "kind": "lesson"}]
            before = len(self.room.pool(self.learner, "gh-300"))
            self.assertEqual(keeper.step(), exam.POOL_MIN_WAIT, "a busy learner means the keeper waits, not stops")
            self.assertEqual(len(self.room.pool(self.learner, "gh-300")), before)
        finally:
            jobs.for_learner = for_learner
        keeper.step()
        pool = self.room.pool(self.learner, "gh-300")
        self.assertTrue(pool, "the keeper writes questions when nothing of the learner's is running")
        for q in pool:
            self.assertTrue(q["quote"] and q["source"], "every pooled question keeps its quote and source")
        seen = exam.seen_keys(self.dojo.store, self.learner.key, "gh-300")
        self.assertFalse({exam.question_key(q["stem"]) for q in pool} & seen,
                         "the pool never holds a question already seen")

    def test_a_seen_question_leaves_the_pool(self):
        """A question the learner has already had is no use to a new exam. It does not count towards
        the pool's target, and the next time the pool is written it is gone."""
        room, learner = self.room, self.learner
        written, _ = self.dojo.write_mcqs("gh-300", exam.plan_skills(PACKAGES.get("gh-300"), 2))
        old, fresh = written[0], written[1]
        room._write_pool(learner, "gh-300", [old, fresh])
        exam.mark_seen(self.dojo.store, learner.key, "gh-300", [exam.question_key(old["stem"])])
        waiting = room.pool(learner, "gh-300")
        self.assertEqual([q["stem"] for q in waiting], [fresh["stem"]], "a seen question is not waiting for you")
        doc = room.create(learner, "gh-300", "short")
        room.build(lambda _t: None, learner, doc["id"])
        left = self.dojo.store.read("learners", learner.key, "pool", "gh-300", default={"items": []})["items"]
        self.assertNotIn(old["stem"], [q["stem"] for q in left], "and it is dropped for good")

    def test_the_export_withholds_the_answer_key_until_the_exam_has_ended(self):
        """The export is one link away. While an exam can still be taken it leaves without its answers,
        or reading it mid-exam would make "no hints, no coach" a lie. While one is actually running the
        export is closed altogether, so the running attempt is checked through the same shaping code the
        export uses."""
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.post(f"/api/exams/{aid}/answer", {"index": 0, "choice": 0})
        running = exam.for_export(self.dojo.store.read("learners", self.learner.key, "exams", aid))
        self.assertTrue(running["items"])
        for it in running["items"]:
            self.assertNotIn("answer", it)
            self.assertNotIn("rationale", it)
            self.assertTrue(it["stem"], "the questions you can already read stay in the export")
        for given in running["answers"].values():
            self.assertNotIn("correct", given, "which ones you got right is part of the answer key")
        self.assertIn("withheld", running)

        def exported() -> dict:
            return next(a for a in self.c.get("/api/record/export").json()["exam_attempts"] if a["id"] == aid)

        self.post(f"/api/exams/{aid}/submit")
        self.assertNotIn("answer", exported()["items"][0],
                         "closed is not enough: the answers are shown on the results page, and that moment is recorded")
        self.post(f"/api/exams/{aid}/rationales")
        shown = exported()
        self.assertIn("answer", shown["items"][0])
        self.assertIn("rationale", shown["items"][0])
        self.assertNotIn("withheld", shown)

    def test_the_teaching_moment_is_recorded_before_the_answers_are_shown(self):
        """The teaching clock starts when the rationales appear, so that moment goes into the Record
        first. A flag lost on the way back never loses the moment, and never records it twice."""
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.post(f"/api/exams/{aid}/submit")
        self.post(f"/api/exams/{aid}/rationales")

        def moments() -> list[dict]:
            return [e for e in self.dojo.store.events(self.learner.key)
                    if e["type"] == "exam.rationales_shown" and (e.get("data") or {}).get("attempt") == aid]

        first = moments()
        self.assertEqual(len(first), 1)
        doc = self.room.attempt(self.learner, aid)
        self.assertEqual(doc["rationales_shown_at"], first[0]["at"])
        doc["rationales_shown_at"] = None   # as if the save after the event had failed
        self.dojo.store.write("learners", self.learner.key, "exams", aid, value=doc)
        again = self.post(f"/api/exams/{aid}/rationales")
        self.assertTrue(again["rationales_shown"])
        self.assertEqual(len(moments()), 1, "the moment is repaired from the Record, not written again")
        self.assertEqual(self.room.attempt(self.learner, aid)["rationales_shown_at"], first[0]["at"])

    def test_a_deletion_waits_for_a_pool_batch_in_flight(self):
        """Deleting the record holds the background pool still first, so nothing lands behind it."""
        keeper = self.app.state.pool
        started, release = threading.Event(), threading.Event()
        written, _ = self.dojo.write_mcqs("gh-300", exam.plan_skills(PACKAGES.get("gh-300"), 2))
        old = self.dojo.write_mcqs
        self.dojo.write_mcqs = _blocking_author(written, started, release)
        batch = threading.Thread(target=keeper.step)
        deleting = threading.Thread(target=self.dojo.delete_all, args=(self.learner,))
        try:
            batch.start()
            self.assertTrue(started.wait(10), "the keeper never started a batch")
            deleting.start()
            time.sleep(0.3)
            self.assertTrue(deleting.is_alive(), "a deletion waits for a batch that is already running")
        finally:
            release.set()
            batch.join(10)
            deleting.join(10)
            self.dojo.write_mcqs = old
        self.assertEqual(self.room.pool(self.learner, "gh-300"), [])
        self.assertEqual(self.c.get("/api/exams?package=gh-300").json()["attempts"], [])

    def test_a_late_pool_batch_writes_nothing_after_a_deletion(self):
        """And if the batch outlasts the short wait, its result is dropped: the record stays deleted."""
        keeper = self.app.state.pool
        started, release = threading.Event(), threading.Event()
        written, _ = self.dojo.write_mcqs("gh-300", exam.plan_skills(PACKAGES.get("gh-300"), 2))
        old, patience = self.dojo.write_mcqs, exam.POOL_PAUSE_WAIT
        self.dojo.write_mcqs = _blocking_author(written, started, release)
        exam.POOL_PAUSE_WAIT = 0.1
        batch = threading.Thread(target=keeper.step)
        try:
            batch.start()
            self.assertTrue(started.wait(10), "the keeper never started a batch")
            self.dojo.delete_all(self.learner)   # goes ahead without waiting for the batch
            release.set()
            batch.join(10)
        finally:
            release.set()
            batch.join(10)
            self.dojo.write_mcqs, exam.POOL_PAUSE_WAIT = old, patience
        self.assertIsNone(self.dojo.store.read("learners", self.learner.key, "pool", "gh-300", default=None),
                          "a batch that finished after the deletion must write nothing")

    def test_export_and_delete_cover_attempts_and_advice(self):
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.post(f"/api/exams/{aid}/answer", {"index": 0, "choice": 0})
        self.post(f"/api/exams/{aid}/submit")
        self.post(f"/api/exams/{aid}/rationales")
        kept = self.post("/api/readiness", {"package": "gh-300"})["kept"]
        dump = self.c.get("/api/record/export").json()
        self.assertIn(aid, [a["id"] for a in dump["exam_attempts"]])
        self.assertIn(kept["id"], [a["id"] for a in dump["advice"]])
        self.assertTrue(dump["questions_seen"])
        self.assertTrue([e for e in dump["events"] if e["type"] == "advice.given"])
        self.post("/api/record/delete", {"confirm": "DELETE"})
        after = self.c.get("/api/record/export").json()
        self.assertEqual((after["exam_attempts"], after["advice"], after["questions_seen"]), ([], [], []))
        self.assertEqual(self.c.get("/api/exams?package=gh-300").json()["attempts"], [])


def _owner(app) -> object:
    from app.core import Learner, learner_key
    s = app.state.settings
    oid = "local:dev" if s.local else f"{s.owner_tid}:{s.owner_oid}"
    return Learner(oid, learner_key(oid), "Owner")


class ExamGuardTests(_ExamClient):
    """What Dojo closes while an exam runs, and what it deliberately does not.

    Its own record: every test here builds an exam, and questions already seen are not asked again,
    so these would eat into the supply the tests above depend on."""

    def test_a_lesson_that_plays_during_an_exam_is_recorded_not_refused(self):
        """A lesson tab open before the exam can start playing after it. Refusing the event would lose
        real exposure and leave the attempt looking like exam conditions, so Dojo takes it and says the
        conditions are unknown instead."""
        lesson = self.wait(self.post("/api/lessons", {"package": "gh-300", "skill": "d1.g1.s1"}))["lesson"]
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        seen = self.post(f"/api/lessons/{lesson}/viewed", {"modality": "watch"})
        self.assertTrue(seen["recorded"], "a lesson that plays during an exam is still written down")
        closed = self.post(f"/api/exams/{aid}/submit")
        self.assertEqual(closed["conditions"], "unknown")
        self.assertIn("a lesson", closed["conditions_note"])
        self.assertIn("lesson.viewed", [e["type"] for e in self.dojo.store.events(self.learner.key)])

    def test_the_export_holds_back_a_written_but_unstarted_exam(self):
        """A built exam has not been shown, so its questions still count as ones you have not seen.
        Reading them in the export would quietly turn them into questions you have."""
        aid = self.build()
        waiting = next(a for a in self.c.get("/api/record/export").json()["exam_attempts"] if a["id"] == aid)
        self.assertEqual(waiting["state"], "ready")
        self.assertTrue(waiting["items"], "how many questions it holds is not a secret")
        for it in waiting["items"]:
            self.assertNotIn("stem", it)
            self.assertNotIn("options", it)
            self.assertNotIn("answer", it)
        self.assertIn("not started", waiting["withheld"])
        self.post(f"/api/exams/{aid}/start")
        self.assertTrue(self.c.get(f"/api/exams/{aid}").json()["items"][0]["stem"], "starting shows them")
        self.post(f"/api/exams/{aid}/submit")

    def test_an_earlier_coach_answer_is_closed_while_an_exam_runs(self):
        """The answer is written down, so reading it back would be the coach by another route. It is
        closed while the clock runs, and readable again the moment the exam ends."""
        made = self.wait(self.post("/api/ask", {"package": "gh-300", "skill": "d1.g1.s1",
                                                "question": "What does a ruleset protect?"}))
        ask = made["id"] if "id" in made else made["ask"]
        self.assertTrue(self.c.get(f"/api/ask/{ask}").json()["points"])
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        closed = self.c.get(f"/api/ask/{ask}")
        self.assertEqual(closed.status_code, 409)
        self.assertIn("earlier answers from the coach are closed", closed.json()["detail"])
        self.post(f"/api/exams/{aid}/submit")
        self.assertTrue(self.c.get(f"/api/ask/{ask}").json()["points"], "and it is yours again afterwards")

    def test_a_running_exam_is_reported_whatever_package_the_page_shows(self):
        """One exam runs at a time and it closes what teaches everywhere, so a page showing another
        exam's package is told about it too - or it would offer a download the server refuses."""
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        elsewhere = self.c.get("/api/exams?package=dp-800").json()
        self.assertEqual(elsewhere["running"], {"id": aid, "package": "gh-300"})
        self.assertEqual(self.c.get("/api/record/export").status_code, 409)
        self.post(f"/api/exams/{aid}/submit")
        self.assertIsNone(self.c.get("/api/exams?package=dp-800").json()["running"])

    def test_two_written_exams_cannot_both_be_running(self):
        """Two exams can be written and wait. Starting the second while the first runs is refused by
        the server, not only by the page that builds them."""
        first, second = self.build(), self.build()
        self.post(f"/api/exams/{first}/start")
        refused = self.post(f"/api/exams/{second}/start", status=409)
        self.assertIn("already running", refused["detail"])
        self.post(f"/api/exams/{first}/submit")
        self.post(f"/api/exams/{second}/start")
        self.post(f"/api/exams/{second}/submit")


class PodcastDuringExamTests(_ExamClient):
    """Dojo cannot see what a podcast app plays. An exam taken while an episode could be on the phone (here:
    the feed is on) asks before it closes whether a lesson played in one, and only an explicit "no" lets it
    claim exam conditions. No lesson here was ever narrated, so once the feed is off nothing is asked.

    Its own record, for the same reason as ExamGuardTests."""

    def tearDown(self):
        self.c.delete("/api/feed/link", headers=POST)

    def link(self) -> str:
        return self.post("/api/feed/link/new")["feeds"][0]["url"].split("/")[4]

    def events(self) -> list[dict]:
        return self.dojo.store.events(self.learner.key)

    def declared(self) -> list[dict]:
        return [e["data"] for e in self.events() if e["type"] == "podcast.declared"]

    def past_deadline(self, aid: str) -> None:
        store = self.dojo.store
        doc = store.read("learners", self.learner.key, "exams", aid)
        doc["deadline"] = iso(utcnow() - timedelta(seconds=5))
        store.write("learners", self.learner.key, "exams", aid, value=doc)

    def test_without_a_feed_nothing_is_asked(self):
        aid = self.build()
        self.assertFalse(self.c.get(f"/api/exams/{aid}").json()["podcast_question"])
        self.assertFalse(self.post(f"/api/exams/{aid}/start")["podcast_question"])
        closed = self.post(f"/api/exams/{aid}/submit")
        self.assertEqual(closed["conditions"], "exam")
        self.assertNotIn("podcast", closed["conditions_note"])
        self.assertNotIn(aid, [d.get("attempt") for d in self.declared()])

    def test_with_the_feed_on_finishing_waits_for_the_reply(self):
        self.link()
        aid = self.build()
        self.assertTrue(self.c.get(f"/api/exams/{aid}").json()["podcast_question"], "the brief says so before the start")
        self.assertTrue(self.post(f"/api/exams/{aid}/start")["podcast_question"])
        before = len(self.events())
        refused = self.post(f"/api/exams/{aid}/submit", status=409)
        self.assertTrue(refused["detail"].startswith("Answer the podcast question first"), refused["detail"])
        self.assertEqual(len(self.events()), before, "a refused finish records nothing")
        self.assertEqual(self.c.get(f"/api/exams/{aid}").json()["state"], "open", "and the exam goes on")
        closed = self.post(f"/api/exams/{aid}/submit", {"reason": "submitted", "podcast_heard": False})
        self.assertEqual((closed["state"], closed["conditions"]), ("closed", "exam"))
        self.assertIn("You said you did not listen to a lesson in a podcast app", closed["conditions_note"])
        self.assertFalse(closed["podcast_question"])
        self.assertEqual(self.declared()[-1], {"package": "gh-300", "attempt": aid, "heard": False, "asked": True})
        types = [e["type"] for e in self.events()]
        self.assertLess(len(types) - 1 - types[::-1].index("podcast.declared"),
                        len(types) - 1 - types[::-1].index("exam.closed"), "the reply is in the Record first")
        self.assertEqual([e for e in self.events() if e["type"] == "exam.closed"][-1]["data"]["conditions"], "exam")

    def test_yes_leaves_the_conditions_unknown(self):
        self.link()
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        closed = self.post(f"/api/exams/{aid}/submit", {"podcast_heard": True})
        self.assertEqual(closed["conditions"], "unknown")
        self.assertIn("You said you listened to a lesson in a podcast app", closed["conditions_note"])
        self.assertTrue(self.declared()[-1]["heard"])
        self.assertEqual([e for e in self.events() if e["type"] == "exam.closed"][-1]["data"]["conditions"], "unknown")

    def test_time_running_out_without_a_reply_is_never_read_as_no(self):
        """The page's clock says time is up and no reply came: the exam closes, and its conditions are unknown."""
        self.link()
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        closed = self.post(f"/api/exams/{aid}/submit", {"reason": "time"})
        self.assertEqual((closed["closed_reason"], closed["conditions"]), ("time", "unknown"))
        self.assertIn("ended before you said whether you listened", closed["conditions_note"])
        self.assertNotIn(aid, [d.get("attempt") for d in self.declared()])

    def test_a_deadline_that_passes_without_a_reply_closes_it_unknown(self):
        self.link()
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.past_deadline(aid)
        view = self.c.get(f"/api/exams/{aid}").json()
        self.assertEqual((view["state"], view["closed_reason"], view["conditions"]), ("closed", "time", "unknown"))
        self.assertIn("ended before you said", view["conditions_note"])
        self.assertFalse(view["podcast_question"])

    def test_the_reply_can_come_with_the_time_up(self):
        """At the deadline the page asks first and sends the reply with the time-up."""
        self.link()
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.past_deadline(aid)
        closed = self.post(f"/api/exams/{aid}/submit", {"reason": "time", "podcast_heard": False})
        self.assertEqual((closed["closed_reason"], closed["conditions"]), ("time", "exam"))
        self.assertEqual(self.declared()[-1]["attempt"], aid)

    def test_turning_the_feed_off_during_the_exam_still_asks(self):
        """Episodes downloaded while it was on can still play."""
        self.link()
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        self.assertFalse(self.c.delete("/api/feed/link", headers=POST).json()["active"], "turning it off is never refused")
        self.assertTrue(self.c.get(f"/api/exams/{aid}").json()["podcast_question"])
        self.post(f"/api/exams/{aid}/submit", status=409)
        self.assertEqual(self.post(f"/api/exams/{aid}/submit", {"podcast_heard": False})["conditions"], "exam")

    def test_no_podcast_link_is_made_while_an_exam_runs(self):
        """So "was the feed on during this exam" is known from its start and its end."""
        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        for path in ("/api/feed/link", "/api/feed/link/new"):
            refused = self.post(path, status=409)
            self.assertIn("A timed practice exam is running", refused["detail"])
        self.assertFalse(self.c.get("/api/feed").json()["active"])
        self.assertEqual(self.post(f"/api/exams/{aid}/submit")["conditions"], "exam")
        self.link()

    def test_the_feed_answers_like_a_wrong_link_while_an_exam_runs(self):
        """Dojo shows nothing that teaches during an exam; what is already on the phone is what it asks about."""
        token = self.link()
        audio = Path(self.tmp) / "episode.mp3"
        audio.write_bytes(b"ID3" + bytes(64))
        paths = (f"/feed/{token}/gh-300.xml", f"/feed/{token}/gh-300.png", f"/feed/{token}/ep/lesson-{'0' * 20}.mp3")
        with mock.patch.object(self.app.state.podcast, "episode_file", return_value=audio):
            self.assertEqual([self.c.get(p).status_code for p in paths], [200, 200, 200])
            aid = self.build()
            self.assertEqual([self.c.get(p).status_code for p in paths], [200, 200, 200], "a written exam is not running")
            self.post(f"/api/exams/{aid}/start")
            for p in paths:
                for method in ("GET", "HEAD"):
                    r = self.c.request(method, p)
                    self.assertEqual((r.status_code, r.content), (404, b""), (method, p))
            self.post(f"/api/exams/{aid}/submit", {"podcast_heard": False})
            self.assertEqual([self.c.get(p).status_code for p in paths], [200, 200, 200], "and it answers again after")


class PodcastAfterTheFeedIsOffTests(_ExamClient):
    """Episodes a podcast app downloaded still play after the feed is turned off. So an exam asks whenever an
    episode could be on the phone: the feed is on, or it has listed or served one. Only deleting the record
    forgets that.

    Its own record and lessons: what was once in the feed stays asked about."""

    def narrated_lesson(self) -> str:
        sid = PACKAGES.get("gh-300")["domains"][0]["groups"][0]["skills"][0]
        lesson = self.dojo.make_lesson(lambda step: None, self.learner, "gh-300", sid)["lesson"]
        for i, m in enumerate(self.dojo.slide_media(self.dojo.lesson(lesson))):
            self.dojo.store.write_bytes("media", m + ".mp3", data=mp3(20, fill=30 + i))
        return lesson

    def link(self) -> str:
        return self.post("/api/feed/link")["feeds"][0]["url"].split("/")[4]

    def test_an_exam_after_the_feed_is_off_asks_while_an_episode_could_be_on_the_phone(self):
        self.link()
        self.assertFalse(self.c.delete("/api/feed/link", headers=POST).json()["on_phone"])
        aid = self.build()
        self.assertFalse(self.post(f"/api/exams/{aid}/start")["podcast_question"], "no lesson had an episode while it was on")
        self.assertEqual(self.post(f"/api/exams/{aid}/submit")["conditions"], "exam")

        self.link()
        lesson = self.narrated_lesson()
        self.assertFalse(self.c.delete("/api/feed/link", headers=POST).json()["on_phone"],
                         "a feed no podcast app fetched put nothing on the phone")
        token = self.link()
        feed = self.c.get(f"/feed/{token}/gh-300.xml")
        self.assertEqual(feed.status_code, 200)
        self.assertIn(lesson, feed.text, "the podcast app saw the episode")
        self.assertTrue(self.c.delete("/api/feed/link", headers=POST).json()["on_phone"])
        aid = self.build()
        self.assertTrue(self.c.get(f"/api/exams/{aid}").json()["podcast_question"], "the brief says so before the start")
        self.assertTrue(self.post(f"/api/exams/{aid}/start")["podcast_question"])
        refused = self.post(f"/api/exams/{aid}/submit", status=409)
        self.assertTrue(refused["detail"].startswith("Answer the podcast question first"), refused["detail"])
        closed = self.post(f"/api/exams/{aid}/submit", {"podcast_heard": False})
        self.assertEqual(closed["conditions"], "exam")
        self.assertIn("You said you did not listen to a lesson in a podcast app", closed["conditions_note"])

        aid = self.build()
        self.post(f"/api/exams/{aid}/start")
        closed = self.post(f"/api/exams/{aid}/submit", {"reason": "time"})
        self.assertEqual(closed["conditions"], "unknown", "no reply is never read as no")
        self.assertIn("Episodes from your podcast feed could be on your phone", closed["conditions_note"])

        self.post("/api/record/delete", {"confirm": "DELETE"})
        self.assertIsNone(self.dojo.store.read("learners", self.dojo.owner_key, "aired"))
        self.assertFalse(self.c.get("/api/feed").json()["on_phone"], "deleting the record forgets what the feed listed")


class ConditionsTests(unittest.TestCase):
    """What a finished attempt may claim about the conditions it ran under."""

    def conditions(self, events: list[dict], started_minutes_ago: int = 20, **fields) -> tuple[str, str]:
        room = object.__new__(exam.ExamRoom)
        room.store = type("S", (), {"events": staticmethod(lambda key: events)})()
        doc = {"id": "exam-1", "started": iso(utcnow() - timedelta(minutes=started_minutes_ago)),
               "deadline": iso(utcnow() + timedelta(minutes=10)), **fields}
        return room._conditions(_LEARNER, doc)

    @staticmethod
    def viewed(minutes_ago: int, window: int = 30) -> dict:
        return {"at": iso(utcnow() - timedelta(minutes=minutes_ago)), "type": "lesson.viewed",
                "data": {"package": "gh-300", "skill": "1.1", "lesson": "les-x", "modality": "watch",
                         "repeats_within_min": window}}

    def test_a_quiet_window_may_claim_exam_conditions(self):
        state, note = self.conditions([self.viewed(90)])
        self.assertEqual(state, "exam")
        self.assertIn("no hints", note)

    def test_a_lesson_still_playing_at_the_start_makes_them_unknown(self):
        """A view is recorded when it starts and states how long it may still run. One that began
        10 minutes before a 20-minute-old attempt could have been playing inside it."""
        state, note = self.conditions([self.viewed(25)])
        self.assertEqual(state, "unknown")
        self.assertIn("a lesson", note)

    def test_a_view_with_no_stated_window_still_counts_for_half_an_hour(self):
        """An event that does not say how long the lesson may play gets the same window the teaching
        clock uses, VIEW_MINUTES. Reading a missing field as zero would read it in the learner's
        favour, which is exactly what must never happen."""
        def old(minutes_ago: int) -> dict:
            return {"at": iso(utcnow() - timedelta(minutes=minutes_ago)), "type": "lesson.viewed",
                    "data": {"package": "gh-300", "skill": "1.1", "lesson": "les-x", "modality": "read"}}
        self.assertEqual(self.conditions([old(25)])[0], "unknown", "25 minutes ago, window 30: still playing")
        self.assertEqual(self.conditions([old(90)])[0], "exam", "and an hour and a half ago is over")

    def test_only_a_lesson_gets_a_window(self):
        """A hint or a coach answer is one moment, not a window: one shown before the exam started
        does not make the attempt unknown."""
        hint = {"at": iso(utcnow() - timedelta(minutes=25)), "type": "hint.shown",
                "data": {"package": "gh-300", "skill": "1.1", "item": "item-x"}}
        self.assertEqual(self.conditions([hint])[0], "exam")
        self.assertEqual(self.conditions([hint], started_minutes_ago=30)[0], "unknown")

    def test_with_the_podcast_feed_on_only_an_explicit_no_claims_them(self):
        """A missing reply is never read in the learner's favour."""
        self.assertEqual(self.conditions([], podcast_feed=True)[0], "unknown")
        self.assertEqual(self.conditions([], podcast_feed=True, podcast_heard=None)[0], "unknown")
        self.assertEqual(self.conditions([], podcast_feed=True, podcast_heard=True)[0], "unknown")
        state, note = self.conditions([], podcast_feed=True, podcast_heard=False)
        self.assertEqual(state, "exam")
        self.assertIn("did not listen to a lesson in a podcast app", note)
        self.assertEqual(self.conditions([self.viewed(25)], podcast_feed=True, podcast_heard=False)[0], "unknown",
                         "a lesson Dojo saw still counts after a 'no'")
        self.assertEqual(self.conditions([], podcast_feed=False)[0], "exam", "no feed, no question")

    def test_an_answer_judged_and_judged_again_is_named_once(self):
        """Both judgements are feedback on an answer (ADR 0015): the note names that help once."""
        def judged(kind: str, minutes_ago: int) -> dict:
            return {"at": iso(utcnow() - timedelta(minutes=minutes_ago)), "type": kind,
                    "data": {"package": "gh-300", "skill": "1.1", "item": "item-x"}}
        hint = {"at": iso(utcnow() - timedelta(minutes=3)), "type": "hint.shown",
                "data": {"package": "gh-300", "skill": "1.1", "item": "item-y"}}
        state, note = self.conditions([judged("answer.judged", 15), judged("answer.rejudged", 5), hint])
        self.assertEqual(state, "unknown")
        self.assertIn("(feedback on an answer, a hint)", note)


def _blocking_author(written: list[dict], started, release):
    """An author model that hangs until the test lets it go, so a batch can be caught in flight."""
    def slow(pid, skills, on_progress=None):
        started.set()
        release.wait(10)
        return [dict(it) for it in written], []
    return slow


class _FakeJobs:
    def __init__(self):
        self.running: list[dict] = []
        self.slots = threading.BoundedSemaphore(3)

    def for_learner(self, learner):
        return self.running


class _FakeDojo:
    def __init__(self):
        self.jobs = _FakeJobs()
        self.quality_lines: list[dict] = []

    def profile(self, learner):
        return {"active_package": "gh-300"}

    def quality(self, key, kind, detail):
        self.quality_lines.append(detail)


class _FakeRoom:
    """Just enough room for the keeper: a pool per package and a top-up the test decides."""

    def __init__(self, outcome):
        self.dojo = _FakeDojo()
        self.packages = PACKAGES
        self.pools: dict[str, list] = {pid: [] for pid in PACKAGES.ids()}
        self.outcome = outcome
        self.calls = 0
        self.store = Store(Path(tempfile.mkdtemp(prefix="dojo-keeper-")))
        self.store.path("learners", _LEARNER.key).mkdir(parents=True)

    def pool(self, learner, pid, seen=None):
        return self.pools[pid]

    def top_up(self, learner, pid, report=True, stale=None, **kw):
        self.calls += 1
        return self.outcome(self, pid)


def _adds_one(room, pid):
    room.pools[pid].append({"stem": f"q{len(room.pools[pid])}"})
    return {"added": 1, "blocked": 0, "state": "added"}


class PoolKeeperTests(unittest.TestCase):
    """The background pool spends money, so what it may spend is bounded and said out loud."""

    def keeper(self, outcome):
        room = _FakeRoom(outcome)
        return exam.PoolKeeper(room, _LEARNER), room

    def test_a_batch_that_adds_nothing_waits_twice_as_long_each_time(self):
        keeper, room = self.keeper(lambda r, pid: {"added": 0, "blocked": 4, "state": "nothing-new"})
        waits = [keeper.step() for _ in range(10)]
        self.assertEqual(waits[:3], [120, 240, 480], "no progress doubles the wait")
        self.assertEqual(waits[-1], exam.POOL_MAX_WAIT, "and it stops doubling at six hours")
        self.assertEqual(len(room.dojo.quality_lines), 1, "one quality line per run of bad luck, not one per try")
        self.assertIn("waits longer", room.dojo.quality_lines[0]["reason"])
        room.outcome = _adds_one
        self.assertEqual(keeper.step(), exam.POOL_MIN_WAIT, "a batch that works starts again from a minute")
        self.assertEqual(keeper.state["misses"], 0)

    def test_a_failing_batch_backs_off_too(self):
        def boom(room, pid):
            raise RuntimeError("the author model is down")
        keeper, room = self.keeper(boom)
        first, second = keeper.step(), keeper.step()
        self.assertGreater(second, first)
        self.assertEqual(len(room.dojo.quality_lines), 1)
        self.assertIn("the author model is down", room.dojo.quality_lines[0]["reason"])

    def test_the_daily_cap_stops_the_paid_calls(self):
        keeper, room = self.keeper(_adds_one)
        for _ in range(exam.POOL_DAILY_BATCHES + 5):
            keeper.step()
        self.assertEqual(room.calls, exam.POOL_DAILY_BATCHES, "the pool never runs more batches a day than its cap")
        self.assertEqual(keeper.step(), exam.POOL_MAX_WAIT)
        self.assertIn("daily limit", keeper.status()["note"])
        self.assertEqual(keeper.status()["batches_today"], exam.POOL_DAILY_BATCHES)

    def test_a_full_pool_asks_for_nothing(self):
        keeper, room = self.keeper(_adds_one)
        room.pools = {pid: [{"stem": f"q{i}"} for i in range(exam.POOL_TARGET)] for pid in room.pools}
        self.assertEqual(keeper.step(), exam.POOL_MAX_WAIT)
        self.assertEqual(room.calls, 0)
        self.assertIn("full", keeper.status()["note"])

    def test_your_own_work_comes_first(self):
        keeper, room = self.keeper(_adds_one)
        room.dojo.jobs.running = [{"state": "running", "kind": "lesson"}]
        self.assertEqual(keeper.step(), exam.POOL_MIN_WAIT)
        self.assertEqual(room.calls, 0)

    def test_a_batch_waits_for_one_of_the_three_work_slots(self):
        """The pool is not a fourth worker: it holds one of the same slots the learner's own jobs use,
        so it can never add a model call beside three of theirs."""
        keeper, room = self.keeper(_adds_one)
        taken = [room.dojo.jobs.slots.acquire() for _ in range(3)]
        self.assertTrue(all(taken))
        patience = exam.POOL_SLOT_WAIT
        exam.POOL_SLOT_WAIT = 0.1
        try:
            self.assertEqual(keeper.step(), exam.POOL_MIN_WAIT)
            self.assertEqual(room.calls, 0, "no slot, no batch")
            self.assertIn("slots are busy", keeper.status()["note"])
        finally:
            exam.POOL_SLOT_WAIT = patience
            for _ in range(3):
                room.dojo.jobs.slots.release()
        self.assertEqual(keeper.step(), exam.POOL_MIN_WAIT)
        self.assertEqual(room.calls, 1, "and it runs again once a slot is free")

    def test_a_deleted_record_is_not_quietly_filled_again(self):
        """After "delete everything" Dojo does not write a new pool under the learner's key. The
        keeper starts again when the learner does."""
        keeper, room = self.keeper(_adds_one)
        with keeper.paused(_LEARNER):
            pass
        shutil.rmtree(room.store.path("learners", _LEARNER.key))
        self.assertEqual(keeper.step(), exam.POOL_MAX_WAIT)
        self.assertEqual(room.calls, 0)
        self.assertIn("deleted", keeper.status()["note"])
        self.assertFalse(room.store.path("learners", _LEARNER.key).exists(), "and it creates nothing there")
        room.store.path("learners", _LEARNER.key).mkdir(parents=True)
        self.assertEqual(keeper.step(), exam.POOL_MIN_WAIT)
        self.assertEqual(room.calls, 1)

    def test_the_daily_cap_survives_a_restart(self):
        """The cap is on money, so it is written on the share before each batch. A restart - a new
        keeper on the same data - does not hand the pool a fresh allowance."""
        keeper, room = self.keeper(_adds_one)
        for _ in range(3):
            keeper.step()
        self.assertEqual(room.store.read("learners", _LEARNER.key, "pool-budget")["batches"], 3)
        after_restart = exam.PoolKeeper(room, _LEARNER)
        self.assertEqual(after_restart.status()["batches_today"], 3, "a new keeper reads what was spent")
        for _ in range(exam.POOL_DAILY_BATCHES + 2):
            after_restart.step()
        self.assertEqual(room.calls, exam.POOL_DAILY_BATCHES, "the two keepers share one day's allowance")
        self.assertEqual(after_restart.step(), exam.POOL_MAX_WAIT)
        self.assertIn("daily limit", after_restart.status()["note"])

    def test_yesterdays_spending_does_not_count(self):
        keeper, room = self.keeper(_adds_one)
        room.store.write("learners", _LEARNER.key, "pool-budget",
                         value={"day": "2000-01-01", "batches": exam.POOL_DAILY_BATCHES})
        self.assertEqual(keeper.step(), exam.POOL_MIN_WAIT)
        self.assertEqual(room.calls, 1)

    def test_a_keeper_without_a_record_writes_nothing_at_all(self):
        """A restart after a delete must not be the thing that creates a learner folder again."""
        keeper, room = self.keeper(_adds_one)
        shutil.rmtree(room.store.path("learners", _LEARNER.key))
        self.assertEqual(keeper.step(), exam.POOL_MAX_WAIT)
        self.assertEqual(room.calls, 0)
        self.assertFalse(room.store.path("learners", _LEARNER.key).exists())
        self.assertEqual(room.dojo.quality_lines, [])

    def test_a_deletion_stops_the_line_about_a_bad_batch(self):
        """The quality log is learner data too. A batch that came to nothing while a deletion was
        waiting writes neither questions nor a line about itself."""
        keeper, room = self.keeper(lambda r, pid: {"added": 0, "blocked": 4, "state": "nothing-new"})

        def delete_midway(r, pid):
            r.calls += 1
            shutil.rmtree(r.store.path("learners", _LEARNER.key))
            keeper._gens[_LEARNER.key] = keeper._gen(_LEARNER) + 1   # what a deletion does: this batch belongs to an older record
            return {"added": 0, "blocked": 4, "state": "nothing-new"}

        room.outcome = delete_midway
        keeper.step()
        self.assertEqual(room.dojo.quality_lines, [], "nothing is written under a record that was deleted")
        self.assertFalse(room.store.path("learners", _LEARNER.key).exists())


class TeachingClockTests(unittest.TestCase):
    """A rationale teaches when it is read. In a timed exam that is at the end, not at answer time."""

    def setUp(self):
        self.pkg = PACKAGES.get("gh-300")
        self.sid = next(iter(self.pkg["skills"]))
        self.t0 = utcnow() - timedelta(hours=30)

    def at(self, hours: float) -> str:
        return iso(self.t0 + timedelta(hours=hours))

    def events(self, *rows: tuple) -> list[dict]:
        return [{"at": at, "type": kind, "data": data} for at, kind, data in rows]

    def test_exam_answers_do_not_start_the_clock(self):
        evs = self.events((self.at(0), "exam.answered", {"package": "gh-300", "skill": self.sid, "attempt": "exam-1", "correct": False}))
        rec = derive(evs, self.pkg)[self.sid]
        self.assertIsNone(rec["last_teaching"], "an unseen rationale teaches nothing")
        self.assertEqual(rec["rehearsal_total"], 1)

    def test_the_clock_starts_when_the_rationales_are_shown(self):
        evs = self.events(
            (self.at(0), "exam.answered", {"package": "gh-300", "skill": self.sid, "attempt": "exam-1", "correct": False}),
            (self.at(2), "exam.rationales_shown", {"package": "gh-300", "attempt": "exam-1", "skills": [self.sid]}),
            (self.at(21), "item.issued", {"package": "gh-300", "skill": self.sid, "item": "item-1", "mode": "check"}),
            (self.at(21), "answer.judged", {"package": "gh-300", "skill": self.sid, "item": "item-1", "mode": "check",
                                            "met": 2, "total": 2}),
        )
        rec = derive(evs, self.pkg, later_hours=20)[self.sid]
        self.assertEqual(rec["last_teaching"][:19], self.at(21)[:19], "judging teaches too")
        self.assertTrue(rec["checks"]["unaided"])
        self.assertFalse(rec["checks"]["later"], "19 hours after the rationales is not 'later'")
        self.assertEqual(rec["state"], "met_unaided")

    def test_the_same_answer_counts_as_later_when_the_rationales_were_early(self):
        evs = self.events(
            (self.at(0), "exam.answered", {"package": "gh-300", "skill": self.sid, "attempt": "exam-1", "correct": False}),
            (self.at(0.5), "exam.rationales_shown", {"package": "gh-300", "attempt": "exam-1", "skills": [self.sid]}),
            (self.at(21), "item.issued", {"package": "gh-300", "skill": self.sid, "item": "item-1", "mode": "check"}),
            (self.at(21), "answer.judged", {"package": "gh-300", "skill": self.sid, "item": "item-1", "mode": "check",
                                            "met": 2, "total": 2}),
        )
        rec = derive(evs, self.pkg, later_hours=20)[self.sid]
        self.assertTrue(rec["checks"]["later"])

    def test_multiple_choice_fills_no_pip(self):
        evs = self.events(
            (self.at(0), "exam.answered", {"package": "gh-300", "skill": self.sid, "attempt": "exam-1", "correct": True}),
            (self.at(1), "mcq.answered", {"package": "gh-300", "skill": self.sid, "rehearsal": "reh-1", "correct": True}),
            (self.at(2), "exam.rationales_shown", {"package": "gh-300", "attempt": "exam-1", "skills": [self.sid]}),
        )
        rec = derive(evs, self.pkg)[self.sid]
        self.assertEqual(rec["checks"], {"unaided": False, "later": False} if self.pkg["skills"][self.sid]["kind"] != "scenario"
                         else {"unaided": False, "later": False, "changed": False})
        self.assertEqual(rec["state"], "untested")
        self.assertEqual((rec["rehearsal_total"], rec["rehearsal_correct"]), (2, 2))


class ReadinessTests(unittest.TestCase):
    """The four rules, on records seeded by hand."""

    def setUp(self):
        self.pkg = PACKAGES.get("gh-300")
        self.now = utcnow()

    def evidence_with(self, later_skills: set[str]) -> dict:
        return {sid: {"checks": {"unaided": sid in later_skills, "later": sid in later_skills}, "attempts": [],
                      "last_teaching": None, "last_lesson": None, "latest": None}
                for sid in self.pkg["skills"]}

    def attempt(self, correct_share: float, new: int | None = None, conditions: str = "exam", days_ago: int = 1,
                weak_domain: str | None = None, count: int = 50) -> dict:
        """One closed attempt whose score is the same in every domain, so nothing depends on the shuffle."""
        skills = exam.plan_skills(self.pkg, count)
        items = [{"skill": sid, "stem": f"Q{i}", "options": ["a", "b"], "answer": 0, "rationale": "", "quote": "", "source": ""}
                 for i, sid in enumerate(skills)]
        by_domain: dict[str, list[int]] = {}
        for i, it in enumerate(items):
            by_domain.setdefault(self.pkg["skills"][it["skill"]]["domain"], []).append(i)
        answers = {}
        for did, idx in by_domain.items():
            share = 0.0 if did == weak_domain else correct_share
            wrong = len(idx) - round(share * len(idx))
            for n, i in enumerate(idx):
                answers[str(i)] = {"choice": 1 if n < wrong else 0, "at": iso(self.now)}
        return {"id": f"exam-{correct_share}-{days_ago}-{weak_domain or ''}", "package": "gh-300",
                "created": iso(self.now - timedelta(days=days_ago)), "closed_at": iso(self.now - timedelta(days=days_ago)),
                "state": "closed", "items": items, "answers": answers, "conditions": conditions,
                "new_questions": len(items) if new is None else new, "length": "full"}

    def test_rule_one_counts_domains_not_skills(self):
        rule = readiness.rule_one(self.pkg, self.evidence_with(set()), 20)
        self.assertFalse(rule["met"])
        self.assertEqual(rule["value"], f"0 of {len(self.pkg['domains'])} domains")
        rule = readiness.rule_one(self.pkg, self.evidence_with(set(self.pkg["skills"])), 20)
        self.assertTrue(rule["met"])
        self.assertIn("20 hours", rule["definition"])
        one = self.pkg["domains"][0]
        sids = {sid for g in one["groups"] for sid in g["skills"]}
        rule = readiness.rule_one(self.pkg, self.evidence_with(set(self.pkg["skills"]) - sids), 20)
        self.assertFalse(rule["met"])
        self.assertEqual(rule["value"], f"{len(self.pkg['domains']) - 1} of {len(self.pkg['domains'])} domains")

    def test_rule_two_wants_two_recent_exams_with_new_questions(self):
        self.assertEqual(readiness.rule_two(self.pkg, [], self.now)["value"], "0 of 2")
        good = [self.attempt(0.9, days_ago=1), self.attempt(0.9, days_ago=3)]
        self.assertTrue(readiness.rule_two(self.pkg, good, self.now)["met"])
        old = [self.attempt(0.9, days_ago=1), self.attempt(0.9, days_ago=90)]
        self.assertFalse(readiness.rule_two(self.pkg, old, self.now)["met"])
        stale = [self.attempt(0.9, days_ago=1), self.attempt(0.9, new=2, days_ago=2)]
        self.assertEqual(readiness.rule_two(self.pkg, stale, self.now)["value"], "1 of 2")
        helped = [self.attempt(0.9, days_ago=1), self.attempt(0.9, conditions="unknown", days_ago=2)]
        self.assertEqual(readiness.rule_two(self.pkg, helped, self.now)["value"], "1 of 2")

    def test_a_short_exam_has_too_few_questions_per_domain_to_count(self):
        short = [self.attempt(0.9, days_ago=1, count=15), self.attempt(0.9, days_ago=2, count=15)]
        rule = readiness.rule_two(self.pkg, short, self.now)
        self.assertEqual(rule["value"], "0 of 2")
        self.assertTrue(rule["attempts"][0]["too_few"])
        self.assertFalse(rule["attempts"][0]["weak"], "too few questions is not the same as weak")
        self.assertIn("too few questions to tell", rule["definition"])

    def test_rule_two_names_a_weak_domain(self):
        weak = self.pkg["domains"][0]["id"]
        rule = readiness.rule_two(self.pkg, [self.attempt(1.0, weak_domain=weak), self.attempt(1.0, days_ago=2)], self.now)
        self.assertFalse(rule["met"])
        self.assertEqual([a["weak"] for a in rule["attempts"] if a["weak"]][0][0]["id"], weak)
        self.assertIn("below your own average", rule["definition"])

    def test_an_exam_left_blank_does_not_count(self):
        """Nothing answered scores zero in every domain, so no domain is below the average. That is not
        an exam without a weak domain: it is an exam that was not taken."""
        blank = self.attempt(0.9, days_ago=1)
        blank["id"], blank["answers"] = "exam-blank", {}
        rule = readiness.rule_two(self.pkg, [blank, self.attempt(0.9, days_ago=2)], self.now)
        self.assertEqual(rule["value"], "1 of 2")
        self.assertNotIn("exam-blank", rule["counted"])
        read = [a for a in rule["attempts"] if a["id"] == "exam-blank"][0]
        self.assertFalse(read["weak"], "a blank exam has no weak domain, which is exactly the trap")
        self.assertFalse(read["enough_answered"])
        self.assertEqual(read["answered_share"], 0)
        self.assertIn("left blank", rule["definition"])
        half = self.attempt(0.9, days_ago=1)
        half["id"] = "exam-half"
        half["answers"] = {k: v for i, (k, v) in enumerate(half["answers"].items()) if i < len(half["items"]) // 2}
        self.assertNotIn("exam-half", readiness.rule_two(self.pkg, [half], self.now)["counted"])
        lines = readiness.reasons([readiness.rule_one(self.pkg, self.evidence_with(set()), 20),
                                   readiness.rule_two(self.pkg, [blank], self.now),
                                   readiness.rule_three([], 20), readiness.rule_four(self.pkg, [])],
                                  self.evidence_with(set()), self.pkg)
        self.assertTrue(any("0% of it answered" in s for s in lines), lines)

    def test_credit_follows_the_blueprint_the_attempt_followed(self):
        """A practice exam keeps the mark of its blueprint. One made before Dojo kept the mark counts on a
        built-in exam, whose blueprint has not changed since; one on another blueprint never counts."""
        mark = self.pkg["meta"]["blueprint"]
        self.assertTrue(self.pkg["meta"]["builtin"])
        legacy = [self.attempt(0.9, days_ago=1), self.attempt(0.9, days_ago=2)]
        self.assertNotIn("blueprint", legacy[0])
        self.assertTrue(readiness.rule_two(self.pkg, legacy, self.now)["met"])
        self.assertTrue(readiness.rule_two(self.pkg, [dict(a, blueprint=mark) for a in legacy], self.now)["met"])
        rule = readiness.rule_two(self.pkg, [dict(a, blueprint="0" * 16) for a in legacy], self.now)
        self.assertEqual((rule["value"], rule["counted"]), ("0 of 2", []))
        self.assertFalse(rule["attempts"][0]["current_blueprint"])
        self.assertIn("current blueprint", rule["definition"])
        lines = readiness.reasons([readiness.rule_one(self.pkg, self.evidence_with(set()), 20), rule,
                                   readiness.rule_three([], 20), readiness.rule_four(self.pkg, [])],
                                  self.evidence_with(set()), self.pkg)
        self.assertTrue(any("earlier blueprint" in s for s in lines), lines)
        added = {**self.pkg, "meta": {**self.pkg["meta"], "builtin": False}}
        self.assertEqual(readiness.rule_two(added, legacy, self.now)["value"], "0 of 2", "an added exam needs the mark")

    def test_every_domain_of_the_current_blueprint_needs_its_own_questions(self):
        """A question on a skill the package does not have counts for nothing, and a domain the attempt
        did not ask about is not judged: such an attempt never counts, however many questions it had."""
        gone = self.pkg["domains"][0]
        full = self.attempt(0.9, days_ago=1)
        items = [dict(it, skill="old." + it["skill"]) if self.pkg["skills"][it["skill"]]["domain"] == gone["id"] else it
                 for it in full["items"]]
        read = readiness.read_attempt(dict(full, items=items), self.pkg)
        self.assertEqual([d["id"] for d in read["domains"]], sorted(d["id"] for d in self.pkg["domains"]))
        first = next(d for d in read["domains"] if d["id"] == gone["id"])
        self.assertEqual((first["questions"], first["correct"], first["score"]), (0, 0, 0))
        self.assertIn(gone["title"], read["too_few"])
        self.assertFalse(read["every_domain_judged"])
        self.assertFalse(read["weak"], "a domain without questions is not judged, so it is not weak either")
        alien = []
        for n in (1, 2):
            a = self.attempt(0.9, days_ago=n)
            a["id"], a["items"] = f"exam-alien-{n}", [dict(it, skill=f"old.{i}") for i, it in enumerate(a["items"])]
            alien.append(a)
        rule = readiness.rule_two(self.pkg, alien, self.now)
        self.assertEqual(rule["value"], "0 of 2")
        self.assertFalse({d["id"] for a in rule["attempts"] for d in a["domains"]} - {d["id"] for d in self.pkg["domains"]})
        self.assertTrue(all(d["questions"] == 0 for a in rule["attempts"] for d in a["domains"]))

    def test_rule_three_says_not_measured_when_nothing_is_stored(self):
        rule = readiness.rule_three([], 20)
        self.assertEqual(rule["value"], "not measured yet")
        self.assertFalse(rule["met"])
        self.assertFalse(rule["measured"])
        self.assertIn("will not invent a number", rule["note"])

    def test_rule_three_counts_the_skill_when_a_wrong_idea_was_stored(self):
        sid = next(iter(self.pkg["skills"]))
        items = [{"id": "item-1", "package": "gh-300", "skill": sid, "created": iso(self.now - timedelta(days=5)),
                  "result": {"misconception": "Thinks exclusions are instant.", "judged_at": iso(self.now - timedelta(days=5))}}]
        rule = readiness.rule_three(items, 20)
        self.assertTrue(rule["measured"])
        evidence = {sid: {"attempts": [{"at": iso(self.now - timedelta(days=1)), "all_met": True, "unaided": True,
                                        "disputed": False, "minutes_since_teaching": 3000}]}}
        closed = readiness.close_rule_three(rule, {sid: [{"at": items[0]["result"]["judged_at"], "text": "x"}]}, evidence, 20)
        self.assertTrue(closed["met"])
        self.assertEqual(closed["value"], "1 of 1 cleared")
        still = readiness.close_rule_three(dict(rule), {sid: [{"at": iso(self.now), "text": "x"}]}, evidence, 20)
        self.assertFalse(still["met"])
        self.assertEqual(still["value"], "0 of 1 cleared")

    def test_rule_four_uses_only_a_verified_assessment(self):
        rule = readiness.rule_four(self.pkg, [])
        self.assertTrue(rule["available"])
        self.assertEqual(rule["value"], "not taken yet")
        self.assertIn("assessmentId=218035372", rule["url"])
        taken = readiness.rule_four(self.pkg, [{"at": iso(self.now), "type": "official_assessment.taken", "data": {"package": "gh-300"}}])
        self.assertTrue(taken["met"])
        none = readiness.rule_four(PACKAGES.get("dp-800"), [])
        self.assertEqual(none["value"], "none verified")
        self.assertFalse(none["met"])
        self.assertFalse(none["blocking"])

    def test_the_verdict_never_predicts_and_never_refuses(self):
        rules = [readiness.rule_one(self.pkg, self.evidence_with(set()), 20), readiness.rule_two(self.pkg, [], self.now),
                 readiness.rule_three([], 20), readiness.rule_four(self.pkg, [])]
        sure = readiness.how_sure(rules, self.evidence_with(set()), 0)
        out = readiness.verdict(rules, sure)
        self.assertEqual(out["say"], "not-yet")
        self.assertEqual(sure["level"], "low")
        self.assertIn("unproven", out["headline"])
        self.assertTrue(any("not measured yet" in s for s in out["scope"]))
        self.assertNotIn("%", out["headline"] + out["why"])
        full = self.evidence_with(set(self.pkg["skills"]))
        rules = [readiness.rule_one(self.pkg, full, 20),
                 readiness.rule_two(self.pkg, [self.attempt(0.95), self.attempt(0.95, days_ago=2)], self.now),
                 readiness.rule_three([], 20), readiness.rule_four(self.pkg, [])]
        out = readiness.verdict(rules, readiness.how_sure(rules, full, 2))
        self.assertEqual(out["say"], "book")
        self.assertIn("supports booking", out["headline"])
        self.assertTrue(any("not a prediction" in s for s in out["scope"]))

    def test_the_reasons_count_the_learners_own_record(self):
        """The bullets under the verdict are numbers from the Record, not adjectives."""
        empty = self.evidence_with(set())
        rules = [readiness.rule_one(self.pkg, empty, 20), readiness.rule_two(self.pkg, [], self.now),
                 readiness.rule_three([], 20), readiness.rule_four(self.pkg, [])]
        lines = readiness.reasons(rules, empty, self.pkg)
        total = len(self.pkg["skills"])
        self.assertTrue(any(f"{total} of {total} skills have not been answered" in s for s in lines))
        self.assertTrue(any("No practice exam finished yet" in s for s in lines))
        self.assertTrue(any("rule 3 is not measured" in s for s in lines))
        self.assertTrue(any("not taken yet" in s for s in lines))
        self.assertFalse(any("%" in s or "pass" in s.lower() for s in lines))

    def test_the_reasons_name_the_weak_domain_of_the_last_exam(self):
        weak = self.attempt(0.9, weak_domain="d3")
        rules = [readiness.rule_one(self.pkg, self.evidence_with(set()), 20),
                 readiness.rule_two(self.pkg, [weak], self.now), readiness.rule_three([], 20),
                 readiness.rule_four(self.pkg, [])]
        lines = readiness.reasons(rules, self.evidence_with(set()), self.pkg)
        self.assertTrue(any("none counting yet" in s for s in lines), lines)

    def test_dp_800_advice_is_not_blocked_by_the_missing_assessment(self):
        pkg = PACKAGES.get("dp-800")
        full = {sid: {"checks": {"unaided": True, "later": True}} for sid in pkg["skills"]}
        rules = [readiness.rule_one(pkg, full, 20),
                 readiness.rule_two(pkg, [], self.now), readiness.rule_three([], 20), readiness.rule_four(pkg, [])]
        out = readiness.verdict(rules, readiness.how_sure(rules, full, 0))
        self.assertEqual(out["say"], "close")
        self.assertTrue(any("does not block" in s for s in out["scope"]))


class ReadinessApiTests(unittest.TestCase):
    """The whole advice, over the API, on a record built through the API."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-ready-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)

    def test_advice_is_shown_stored_and_free_of_predictions(self):
        out = self.c.get("/api/readiness?package=gh-300").json()
        self.assertEqual([r["id"] for r in out["rules"]], ["rule1", "rule2", "rule3", "rule4"])
        self.assertEqual(out["say"], "not-yet")
        self.assertEqual(out["sure"]["level"], "low")
        self.assertIn("book whenever you like", " ".join(out["scope"]))
        text = str(out)
        self.assertNotIn("700", text)
        self.assertNotIn("probability", text.lower())
        self.assertNotIn("pass prediction", text.lower().replace("not a pass prediction", ""))
        shown = self.c.post("/api/readiness", json={"package": "gh-300"}, headers=POST).json()
        self.assertEqual(shown["say"], out["say"])
        self.assertEqual(len(shown["rules"]), 4)
        again = self.c.post("/api/readiness", json={"package": "gh-300"}, headers=POST).json()
        self.assertEqual(again["kept"]["id"], shown["kept"]["id"], "the same advice on the same day is kept once")
        history = self.c.get("/api/readiness?package=gh-300").json()["history"]
        self.assertEqual(history[0]["id"], shown["kept"]["id"])

    def test_the_advice_on_the_screen_is_the_advice_in_the_record(self):
        """The page shows what one call computed and stored, so it can never show advice that was
        never kept, nor keep advice the learner never saw."""
        shown = self.c.post("/api/readiness", json={"package": "gh-300"}, headers=POST).json()
        kept = shown["kept"]
        self.assertEqual(kept["fingerprint"], shown["fingerprint"])
        self.assertEqual(kept["say"], shown["say"])
        self.assertEqual(kept["sure"], shown["sure"]["level"])
        self.assertEqual([r["value"] for r in kept["rules"]], [r["value"] for r in shown["rules"]])
        self.assertEqual([r["title"] for r in kept["rules"]], [r["title"] for r in shown["rules"]])
        self.assertLessEqual(kept["at"], shown["at"], "the kept advice is this one, or the identical one kept earlier today")
        self.assertIn(kept["id"], [d["id"] for d in self.c.get("/api/readiness?package=gh-300").json()["history"]])
        event = [e for e in self.c.get("/api/record/export").json()["events"] if e["type"] == "advice.given"][-1]
        self.assertEqual(event["data"]["fingerprint"], shown["fingerprint"])

    def test_the_official_assessment_can_be_marked_taken_only_where_one_is_verified(self):
        before = self.c.get("/api/readiness?package=gh-300").json()["rules"][3]
        self.assertEqual(before["value"], "not taken yet")
        self.c.post("/api/readiness/official", json={"package": "gh-300"}, headers=POST)
        after = self.c.get("/api/readiness?package=gh-300").json()["rules"][3]
        self.assertEqual(after["value"], "taken")
        r = self.c.post("/api/readiness/official", json={"package": "dp-800"}, headers=POST)
        self.assertEqual(r.status_code, 409, r.text)
        dp = self.c.get("/api/readiness?package=dp-800").json()["rules"][3]
        self.assertEqual(dp["value"], "none verified")
        self.assertIn("no official practice assessment", dp["note"].lower())


class SeenQuestionTests(unittest.TestCase):
    def test_the_same_question_is_recognised_however_it_is_spaced(self):
        store = Store(Path(tempfile.mkdtemp(prefix="dojo-seen-")))
        a = exam.question_key("Which setting   blocks a file?\n")
        self.assertEqual(a, exam.question_key("which setting blocks a file?"))
        self.assertNotEqual(a, exam.question_key("Which setting blocks a folder?"))
        exam.mark_seen(store, "k", "gh-300", [a, a])
        exam.mark_seen(store, "k", "gh-300", [a, "other"])
        self.assertEqual(store.read("learners", "k", "seen", "gh-300")["keys"], [a, "other"])
        self.assertEqual(exam.seen_keys(store, "k", "gh-300"), {a, "other"})
        self.assertEqual(exam.seen_keys(store, "k", "dp-800"), set())


if __name__ == "__main__":
    unittest.main()
