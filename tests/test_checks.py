"""The 3-minute check: picking the skills, writing the questions in the background, reading only the
question out loud, and staying closed while a timed practice exam runs."""
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-checks-import-"))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.checks import LIMIT, MINUTES, pick  # noqa: E402
from app.core import APP_DIR, Learner, Store, iso, learner_key, utcnow  # noqa: E402
from app.main import create_app  # noqa: E402
from app.packages import Packages  # noqa: E402
from app.speech import StubSpeech, build_ssml  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402
from tests.test_podcast import mp3  # noqa: E402

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
SECRET_KEYS = {"rubric", "model_answer", "hints", "changed_condition", "sources"}


def when(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def records(**skills: dict) -> dict[str, dict]:
    """One evidence record per skill, with the fields the picker reads."""
    return {sid: {"checks": {"unaided": False, "later": False}, "last_teaching": None, "attempts": [], **given}
            for sid, given in skills.items()}


def keys_in(value) -> set:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_in(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_in(v)}
    return set()


class PickTests(unittest.TestCase):
    """Who gets asked, and why, said in plain words."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = Packages(APP_DIR / "packages").get("gh-300")
        cls.ids = sorted(cls.pkg["skills"])

    def test_due_later_comes_before_untested(self):
        a, b, c = self.ids[0], self.ids[1], self.ids[2]
        ev = records(**{
            a: {},
            b: {"checks": {"unaided": True, "later": False}, "last_teaching": when(NOW - timedelta(hours=30))},
            c: {},
        })
        for sid in self.ids[3:]:  # attempted, but never met: neither due later nor untested
            ev[sid] = {"checks": {"unaided": False, "later": False}, "last_teaching": None, "attempts": [{"met": 1}]}
        picks = pick(self.pkg, ev, NOW, 20, LIMIT)
        self.assertEqual(picks[0]["skill"], b)
        self.assertEqual(picks[0]["picked"], "due_later")
        self.assertIn("30 hours ago", picks[0]["why"])
        self.assertEqual({p["skill"] for p in picks[1:]}, {a, c})
        self.assertTrue(all(p["picked"] == "untested" for p in picks[1:]))

    def test_a_skill_that_is_not_later_yet_is_not_due(self):
        a = self.ids[0]
        ev = records(**{a: {"checks": {"unaided": True, "later": False},
                            "last_teaching": when(NOW - timedelta(hours=5))}})
        for sid in self.ids[1:]:
            ev[sid] = {"checks": {"unaided": False, "later": False}, "last_teaching": None, "attempts": [{"met": 1}]}
        self.assertEqual(pick(self.pkg, ev, NOW, 20, LIMIT), [])
        picks = pick(self.pkg, ev, NOW, 4, LIMIT)  # the learner's own window, whatever they set it to
        self.assertEqual([p["skill"] for p in picks], [a])
        self.assertIn("4 hours", picks[0]["why"])

    def test_a_skill_already_shown_later_is_left_alone(self):
        a = self.ids[0]
        ev = records(**{a: {"checks": {"unaided": True, "later": True}, "attempts": [{"met": 2}],
                            "last_teaching": when(NOW - timedelta(days=4))}})
        for sid in self.ids[1:]:
            ev[sid] = {"checks": {"unaided": False, "later": False}, "last_teaching": None, "attempts": [{"met": 1}]}
        self.assertEqual(pick(self.pkg, ev, NOW, 20, LIMIT), [])

    def test_untested_skills_come_heaviest_first_with_the_reason(self):
        ev = records(**{sid: {} for sid in self.ids})
        picks = pick(self.pkg, ev, NOW, 20, LIMIT)
        self.assertEqual(len(picks), LIMIT)
        self.assertEqual([p["weight"] for p in picks], sorted((p["weight"] for p in picks), reverse=True))
        heaviest = max(s["share"] for s in self.pkg["skills"].values())
        self.assertAlmostEqual(picks[0]["weight"], round(heaviest * 100, 1))
        for p in picks:
            self.assertIn("Not tested yet", p["why"])
            self.assertIn("% of the exam", p["why"])
            self.assertTrue(p["text"] and p["domain"] and p["kind"])

    def test_the_longest_overdue_check_comes_first(self):
        a, b = self.ids[0], self.ids[1]
        met = {"checks": {"unaided": True, "later": False}}
        ev = records(**{a: {**met, "last_teaching": when(NOW - timedelta(hours=30))},
                        b: {**met, "last_teaching": when(NOW - timedelta(days=6))}})
        picks = pick(self.pkg, ev, NOW, 20, LIMIT)
        self.assertEqual([p["skill"] for p in picks], [b, a])
        self.assertIn("6 days ago", picks[0]["why"])

    def test_nothing_due_is_an_empty_list_not_a_guess(self):
        ev = records(**{sid: {"attempts": [{"met": 1}]} for sid in self.ids})
        self.assertEqual(pick(self.pkg, ev, NOW, 20, LIMIT), [])


class SpyingSpeech(StubSpeech):
    """Remembers every line it was asked to say, so a test can prove what reached Speech."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.said: list[list[dict]] = []
        self.calls = 0

    def synthesize(self, lines: list[dict], **kw) -> str:
        self.said.append([dict(line) for line in lines])
        return super().synthesize(lines, **kw)

    def _call(self, ssml: str, output_format: str = "") -> bytes:
        self.calls += 1
        return super()._call(ssml, output_format)


class CheckSessionTests(unittest.TestCase):
    """A whole session against the local stub: three questions, answered spoken and typed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-checks-")
        s = settings(cls.tmp)
        cls.speech = SpyingSpeech(s, None, Store(s.data_dir))
        cls.app = create_app(s, speech=cls.speech, http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.pid = "gh-300"
        cls.key = learner_key("local:dev")
        cls.store = cls.app.state.dojo.store

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

    def settled(self, cid: str) -> dict:
        view: dict = {}
        for _ in range(900):
            view = self.c.get(f"/api/checks/{cid}").json()
            if not view["coming"]:
                return view
            time.sleep(0.05)
        self.fail(f"the check never settled: {view}")

    def forget(self, cid: str) -> None:
        """Wait until no loop of ours is writing this session any more, which is what a restart does
        to every session at once."""
        for _ in range(300):
            if cid not in self.app.state.checks._filling:
                return
            time.sleep(0.05)
        self.fail("the loop that writes the questions never stopped")

    def item_doc(self, item_id: str) -> dict:
        return self.store.read("learners", self.key, "items", item_id)

    def test_01_the_plan_says_what_it_would_ask_and_why(self):
        plan = self.c.get(f"/api/checks?package={self.pid}").json()
        self.assertEqual((plan["minutes"], plan["limit"]), (MINUTES, LIMIT))
        self.assertEqual(len(plan["picks"]), LIMIT)
        self.assertIsNone(plan["running"])
        for p in plan["picks"]:
            self.assertTrue(p["why"])
            self.assertIn(p["picked"], ("due_later", "untested"))
        # Reading the plan writes nothing: no item, no session, no event.
        self.assertEqual(plan, self.c.get(f"/api/checks?package={self.pid}").json())
        self.assertEqual(self.store.names("learners", self.key, "items"), [])
        self.assertEqual(self.c.get("/api/record/export").json()["events"], [])

    def test_02_a_session_writes_check_items_and_reads_only_the_question(self):
        started = self.post("/api/checks", {"package": self.pid})
        self.assertEqual(started["state"], "getting_ready")
        self.assertEqual((started["total"], started["ready"], started["answered"]), (LIMIT, 0, 0))
        self.assertTrue(all(q["stem"] == "" for q in started["questions"]))
        view = self.settled(started["id"])
        self.assertEqual(view["ready"], LIMIT, view)
        self.assertEqual((view["state"], view["skipped"], view["voice"]), ("running", 0, "guide"))
        for q in view["questions"]:
            self.assertTrue(q["stem"])
            self.assertTrue(q["audio"].startswith("a"))
            self.assertFalse(q["answered"])
            self.assertTrue(q["why"] and q["text"] and q["domain"])
            item = self.c.get(f"/api/items/{q['item']}").json()
            self.assertEqual((item["mode"], item["stem"], item["hints_left"]), ("check", q["stem"], 0))
            self.assertEqual(self.item_doc(q["item"])["mode"], "check")
        self.assertEqual(self.c.get(f"/api/checks?package={self.pid}").json()["running"], view["id"])
        type(self).session = view

    def test_03_no_rubric_model_answer_or_hint_reaches_the_browser_or_speech(self):
        view = self.session
        body = self.c.get(f"/api/checks/{view['id']}")
        raw, parsed = body.text, body.json()
        self.assertEqual(keys_in(parsed) & SECRET_KEYS, set())
        for q in view["questions"]:
            item = self.item_doc(q["item"])
            secrets = [item["model_answer"], *[r["point"] for r in item["rubric"]],
                       *[r.get("quote", "") for r in item["rubric"]], *item["hints"],
                       item.get("changed_condition") or ""]
            secrets = [s for s in secrets if s]
            self.assertTrue(secrets)
            for secret in secrets:
                self.assertNotIn(secret, raw, "a secret reached the browser")
            spoken = [lines for lines in self.speech.said if lines and lines[0]["text"] == item["stem"]]
            self.assertTrue(spoken, "the question was never read out loud")
            for lines in spoken:
                self.assertEqual(lines, [{"voice": "guide", "text": item["stem"]}])
                for secret in secrets:
                    self.assertNotIn(secret, build_ssml(lines), "a secret reached Speech")

    def test_04_the_question_audio_is_made_once_and_kept_by_its_content(self):
        stem = self.session["questions"][0]["stem"]
        room, before = self.app.state.checks, self.speech.calls
        again = room._say(self.app.state.preparer.owner, stem)
        self.assertEqual(self.speech.calls, before, "the same question was sent to Speech twice")
        self.assertEqual(again, self.session["questions"][0]["audio"])
        other = room._say(self.app.state.preparer.owner, stem + " And one more thing?")
        self.assertNotEqual(again, other)
        self.assertEqual(self.speech.calls, before + 1)
        media = self.c.get(f"/api/media/{again}")
        self.assertEqual((media.status_code, media.headers["content-type"]), (200, "audio/mpeg"))

    def test_05_answers_are_ordinary_check_answers_spoken_or_typed(self):
        first, second, third = self.c.get(f"/api/checks/{self.session['id']}").json()["questions"]
        self.wait(self.post(f"/api/items/{first['item']}/answer",
                            {"text": "Everything I can remember about this, said in my own words.",
                             "elapsed_s": 30, "input": "typed"}))
        heard = self.c.post(f"/api/transcribe?item={second['item']}", content=b"\x1aE\xdf\xa3" + bytes(400),
                            headers={**POST, "Content-Type": "audio/webm"}).json()
        self.wait(self.post(f"/api/items/{second['item']}/answer",
                            {"text": heard["text"], "elapsed_s": 25, "input": "spoken",
                             "edited_before_send": False, "receipts": [heard["receipt"]]}))
        after = self.c.get(f"/api/checks/{self.session['id']}").json()
        self.assertEqual((after["answered"], after["total"]), (2, 3))
        self.assertEqual([q["answered"] for q in after["questions"]], [True, True, False])
        self.assertFalse(after["questions"][2]["answered"])
        self.assertEqual(after["questions"][2]["item"], third["item"])
        events = self.c.get("/api/record/export").json()["events"]
        answers = [e for e in events if e["type"] == "answer.submitted"]
        self.assertEqual([a["data"]["input"] for a in answers], ["typed", "spoken"])
        self.assertTrue(all(a["data"]["mode"] == "check" and a["data"]["hints_seen"] == 0 for a in answers))
        # Reading a question out loud is the question, not teaching: nothing is written down for it.
        self.assertEqual({e["type"] for e in events}, {"item.issued", "answer.submitted", "answer.judged"})
        ended = self.post(f"/api/checks/{self.session['id']}/end")
        self.assertEqual(ended["state"], "ended")
        self.assertEqual([q["answered"] for q in ended["questions"]], [True, True, False])
        # Ending a check stays open during a timed exam, so ending never hands the questions out.
        self.assertTrue(all(q["stem"] == "" and q["audio"] == "" for q in ended["questions"]))
        self.assertIsNone(self.c.get(f"/api/checks?package={self.pid}").json()["running"])

    def test_06_one_check_at_a_time(self):
        first = self.post("/api/checks", {"package": self.pid})
        self.assertEqual(self.post("/api/checks", {"package": self.pid})["id"], first["id"])
        self.settled(first["id"])
        self.post(f"/api/checks/{first['id']}/end")

    def test_07_closed_while_a_timed_practice_exam_runs(self):
        attempt = self.post("/api/exams", {"package": self.pid, "length": "short"})
        aid = self.wait(attempt)["attempt"]
        self.post(f"/api/exams/{aid}/start")
        try:
            calls = [self.c.get(f"/api/checks?package={self.pid}"),
                     self.c.get(f"/api/checks/{self.session['id']}"),
                     self.c.post("/api/checks", json={"package": self.pid}, headers=POST)]
            for r in calls:
                self.assertEqual(r.status_code, 409, r.text)
                self.assertIn("A timed practice exam is running.", r.json()["detail"])
                self.assertIn("The 3-minute check is closed", r.json()["detail"])
        finally:
            self.post(f"/api/exams/{aid}/submit", {"reason": "submitted"})
        self.assertEqual(self.c.get(f"/api/checks?package={self.pid}").status_code, 200)

    def test_08_bad_ids_and_cross_site_posts_are_refused(self):
        self.assertEqual(self.c.get("/api/checks/not-an-id").status_code, 404)
        self.assertEqual(self.c.get("/api/checks/check-0123456789abcdef0123").status_code, 404)
        self.assertEqual(self.c.post("/api/checks", json={"package": "nope"}, headers=POST).status_code, 404)
        self.assertEqual(self.c.post("/api/checks", json={"package": self.pid}).status_code, 403)

    def test_09_a_question_nobody_is_writing_any_more_is_left_out(self):
        """Dojo is restarted mid-check: the loop that watches the jobs dies with the process. The
        session must settle and say so, instead of waiting for a question that is never coming."""
        started = self.post("/api/checks", {"package": self.pid})
        cid = started["id"]
        self.settled(cid)
        doc = self.store.read("learners", self.key, "checks", cid)
        doc["state"] = "running"
        doc["questions"][0].update(state="writing", item=None, audio="", note="", job="job-deadbeef")
        self.store.write("learners", self.key, "checks", cid, value=doc)

        view = self.c.get(f"/api/checks/{cid}").json()
        self.assertEqual(view["coming"], 0, "a stranded question kept the session waiting")
        self.assertEqual(view["questions"][0]["state"], "skipped")
        self.assertTrue(view["questions"][0]["note"])
        self.post(f"/api/checks/{cid}/end")

        doc["questions"] = [dict(q, state="waiting", item=None, audio="", job=None) for q in doc["questions"]]
        doc["state"] = "getting_ready"
        doc["ended"] = None
        doc["created"] = iso(utcnow() - timedelta(hours=1))
        self.forget(cid)
        self.store.write("learners", self.key, "checks", cid, value=doc)
        self.assertIsNone(self.c.get(f"/api/checks?package={self.pid}").json()["running"],
                          "a check nobody is writing any more was offered as still running")
        self.assertEqual(self.c.get(f"/api/checks/{cid}").json()["coming"], 0)

    def test_10_a_restart_does_not_leave_a_session_nobody_is_writing(self):
        """The same, a minute after it started: nothing may wait on a thread the restart took away.
        The session is dropped at once and the learner can start a new check."""
        started = self.post("/api/checks", {"package": self.pid})
        cid = started["id"]
        self.settled(cid)
        self.post(f"/api/checks/{cid}/end")
        self.forget(cid)
        doc = self.store.read("learners", self.key, "checks", cid)
        doc.update(state="getting_ready", ended=None, created=iso(),  # just now, and no loop left
                   questions=[dict(q, state="waiting", item=None, audio="", note="", job=None) for q in doc["questions"]])
        self.store.write("learners", self.key, "checks", cid, value=doc)
        self.assertNotIn(cid, self.app.state.checks._filling)
        view = self.c.get(f"/api/checks/{cid}").json()
        self.assertEqual(view["coming"], 0, "the page would poll for questions nobody is writing")
        self.assertEqual(view["skipped"], view["total"])
        self.assertIn("Dojo stopped", view["questions"][0]["note"], "the reason is not said in plain words")
        self.assertIsNone(self.c.get(f"/api/checks?package={self.pid}").json()["running"])
        fresh = self.post("/api/checks", {"package": self.pid})
        self.assertNotEqual(fresh["id"], cid, "the learner was sent back to the dead session")
        self.settled(fresh["id"])
        self.post(f"/api/checks/{fresh['id']}/end")

    def test_11_two_starts_in_the_same_moment_make_one_session(self):
        """Two taps, or two tabs: one session. Otherwise both write three items and spend two sets of
        author and gate calls on the same three skills."""
        room, learner = self.app.state.checks, Learner("local:dev", self.key, "Owner")
        before = set(self.store.names("learners", self.key, "checks"))
        start, out = threading.Barrier(2), []
        def go() -> None:
            start.wait()
            try:
                out.append(room.create(learner, self.pid)["id"])
            except Exception as e:  # noqa: BLE001 - reported below
                out.append(repr(e))
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(set(out)), 1, out)
        made = set(self.store.names("learners", self.key, "checks")) - before
        self.assertEqual(len(made), 1, "two sessions were started at once")
        cid = out[0]
        self.settled(cid)
        self.post(f"/api/checks/{cid}/end")

    def test_12_ending_a_check_stops_a_question_being_written(self):
        """The writer and the learner change the same line. Whoever gives up wins: a question that was
        skipped never turns back into a question, and no item is written for an ended check."""
        room, learner = self.app.state.checks, Learner("local:dev", self.key, "Owner")
        started = self.post("/api/checks", {"package": self.pid})
        cid = started["id"]
        self.settled(cid)
        self.post(f"/api/checks/{cid}/end")
        doc = self.store.read("learners", self.key, "checks", cid)
        doc["questions"][0].update(state="writing", item=None, audio="", note="", job="job-deadbeef")
        self.store.write("learners", self.key, "checks", cid, value=doc)
        self.assertEqual(room._one(lambda step: None, learner, cid, 0), {"stopped": True},
                         "a question was written for a check that had ended")
        self.assertEqual(self.store.read("learners", self.key, "checks", cid)["questions"][0]["item"], None)

        doc = self.store.read("learners", self.key, "checks", cid)
        doc["state"] = "running"
        doc["questions"][0].update(state="skipped", note="given up on")
        self.store.write("learners", self.key, "checks", cid, value=doc)
        self.assertEqual(room._one(lambda step: None, learner, cid, 0), {"stopped": True})
        room._change(learner, cid, 0, ("waiting", "writing"), state="ready", item="item-late", audio="a1")
        after = self.store.read("learners", self.key, "checks", cid)["questions"][0]
        self.assertEqual((after["state"], after["item"]), ("skipped", None), "a late writer undid the skip")
        self.post(f"/api/checks/{cid}/end")  # leave no running check for the tests after this one

    def test_13_a_check_only_takes_a_work_slot_nobody_is_holding(self):
        """The exam pool and the delivery check hold one of the same three slots without a job document.
        A check must see those and leave them alone, or it makes those workers time out."""
        jobs = self.app.state.dojo.jobs
        learner = Learner("local:dev", self.key, "Owner")
        self.assertEqual(jobs.free(), jobs.parallel)
        with jobs.slots:  # a slot held the way the pool keeper holds one
            self.assertEqual(jobs.free(), jobs.parallel - 1)
            self.assertEqual(jobs.slots.held(), 1)
            held = [s for s in range(jobs.parallel - 1) if jobs.slots.acquire(timeout=0.2)]
            try:
                self.assertEqual(len(held), jobs.parallel - 1, "the slots could not be taken")
                self.assertEqual(jobs.free(), 0)
                with self.assertRaises(HTTPException) as refused:
                    jobs.start(learner, "item", lambda step: {"ok": True}, only_if_free=True)
                self.assertEqual(refused.exception.status_code, 429)
            finally:
                for _ in held:
                    jobs.slots.release()
            self.assertEqual(jobs.free(), jobs.parallel - 1)
            job = jobs.start(learner, "item", lambda step: {"ok": True}, only_if_free=True)
            for _ in range(200):  # the started job is one of the three, held or queued for a slot
                if self.c.get(f"/api/jobs/{job['id']}").json()["state"] not in ("queued", "running"):
                    break
                time.sleep(0.05)
        self.assertEqual(jobs.free(), jobs.parallel)

    def test_14_the_export_holds_back_the_key_of_a_question_still_to_answer(self):
        """The export is one link away and a check question sits open on the phone. Until the answer
        has been judged the rubric, the model answer and the unseen hints stay on the server.

        Its own app and record: the stub model has five situations per skill, and the tests before this
        one can use them all up, so every new question would be refused as too close to an earlier one."""
        s = settings(tempfile.mkdtemp(prefix="dojo-checks-export-"))
        self.app = create_app(s, http=httpx.Client(transport=httpx.MockTransport(web)))
        self.addCleanup(self.app.state.podcast.close)
        self.c = TestClient(self.app)
        self.store = self.app.state.dojo.store
        first = self.post("/api/checks", {"package": self.pid})
        first_view = self.settled(first["id"])
        self.wait(self.post(f"/api/items/{first_view['questions'][0]['item']}/answer",
                            {"text": "Everything I can remember about this.", "elapsed_s": 20, "input": "typed"}))
        self.post(f"/api/checks/{first['id']}/end")
        started = self.post("/api/checks", {"package": self.pid})
        view = self.settled(started["id"])
        dump = self.c.get("/api/record/export")
        items = {d["id"]: d for d in dump.json()["items"]}
        asked = [q for q in view["questions"] if q["item"]]
        self.assertTrue(asked, f"no question was written for this check: {view}")
        for q in asked:
            doc, out = self.item_doc(q["item"]), items[q["item"]]
            self.assertEqual(keys_in(out) & {"rubric", "model_answer", "changed_condition"}, set())
            self.assertEqual(out["hints"], [])
            self.assertTrue(out["withheld"])
            self.assertEqual(out["stem"], doc["stem"], "the question itself stays in the export")
            written = json.dumps(out)
            for secret in [doc["model_answer"], *[r["point"] for r in doc["rubric"]],
                           *[r.get("quote", "") for r in doc["rubric"]], *doc["hints"]]:
                if secret:
                    self.assertNotIn(secret, written, "the export handed out the key mid-check")
        judged = [d for d in items.values() if d.get("result")]
        self.assertTrue(judged, "the earlier answers should be in this export")
        for d in judged:
            self.assertTrue(d["model_answer"] and d["rubric"], "a judged item keeps everything")
            self.assertNotIn("withheld", d)
            stored = self.item_doc(d["id"])
            for key in ("model_answer", "rubric", "hints", "sources", "changed_condition", "answer", "result"):
                if key in stored:
                    self.assertEqual(d.get(key), stored[key], f"a judged item's {key} changed in the export")
        self.post(f"/api/checks/{started['id']}/end")

    def test_15_ending_a_check_while_it_is_being_written_stops_everything(self):
        """The real race, not a staged one: the jobs are on their way when the learner ends the check.
        Nothing may be read out loud afterwards, and no question may arrive after the end."""
        learner = Learner("local:dev", self.key, "Owner")
        said, made = len(self.speech.said), len(self.store.names("learners", self.key, "items"))
        started = self.post("/api/checks", {"package": self.pid})
        cid = started["id"]
        ended = self.post(f"/api/checks/{cid}/end")
        self.forget(cid)
        jobs = self.app.state.dojo.jobs
        for _ in range(1200):  # let anything already on its way finish
            if not [j for j in jobs.for_learner(learner)
                    if (j.get("ref") or "").startswith(cid) and j.get("state") in ("queued", "running")]:
                break
            time.sleep(0.05)
        doc = self.store.read("learners", self.key, "checks", cid)
        ready = [q for q in doc["questions"] if q["state"] == "ready"]
        self.assertEqual(doc["state"], "ended")
        self.assertTrue([q for q in doc["questions"] if q["state"] == "skipped"],
                        "nothing was still being written, so this did not test the race")
        self.assertEqual(len(ready), ended["ready"], "a question arrived after the check had ended")
        self.assertLessEqual(len(self.speech.said) - said, ended["ready"],
                             "the coach read a question out loud for a check that had ended")
        self.assertLessEqual(len(self.store.names("learners", self.key, "items")) - made, LIMIT,
                             "an ended check kept writing items")
        for q in doc["questions"]:
            if q["state"] == "skipped":
                self.assertIsNone(q["item"], "a skipped question was given a question after all")
                self.assertTrue(q["note"])


class CheckPodcastTests(unittest.TestCase):
    """Check answers go through the ordinary answer route, which refuses one without a reply when the podcast
    question applies. So a question whose episode the feed has offered carries the same question the answer
    form asks, and nothing else does."""

    def setUp(self):
        self.app = create_app(settings(tempfile.mkdtemp(prefix="dojo-checks-pod-")),
                              http=httpx.Client(transport=httpx.MockTransport(web)))
        self.c = TestClient(self.app)
        self.dojo, self.podcast = self.app.state.dojo, self.app.state.podcast
        self.podcast.prune_after_s = 3600  # pruning is not what this tests
        self.addCleanup(self.podcast.close)
        self.owner = self.app.state.preparer.owner

    def wait(self, job: dict) -> dict:
        for _ in range(900):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def test_a_question_whose_episode_the_feed_offered_asks_first(self):
        pid = "dp-800"
        pkg = self.dojo.packages.get(pid)
        first = pkg["domains"][0]["groups"][0]["skills"][0]
        self.assertEqual(pkg["skills"][first]["kind"], "scenario", "its items change a condition, so with no teaching they are asked about")
        lesson = self.dojo.make_lesson(lambda step: None, self.owner, pid, first)["lesson"]
        for i, m in enumerate(self.dojo.slide_media(self.dojo.lesson(lesson))):
            self.dojo.store.write_bytes("media", m + ".mp3", data=mp3(20, fill=40 + i))
        token = self.podcast.links.create()
        self.assertIn(lesson, self.c.get(f"/feed/{token}/{pid}.xml").text, "a podcast app was offered the episode")

        started = self.c.post("/api/checks", json={"package": pid}, headers=POST).json()
        self.assertTrue(all(q.get("podcast_question") is None for q in started["questions"]), "nothing is ready to answer yet")
        view: dict = {}
        for _ in range(900):
            view = self.c.get(f"/api/checks/{started['id']}").json()
            if not view["coming"]:
                break
            time.sleep(0.05)
        self.assertEqual(view["ready"], view["total"], view)
        asked = {q["skill"]: q["podcast_question"] for q in view["questions"]}
        self.assertEqual((asked[first]["lesson"], asked[first]["since"], asked[first]["feed_on"]), (lesson, None, True))
        self.assertTrue(all(v is None for s, v in asked.items() if s != first), "the feed offered no other skill's episode")
        q = next(q for q in view["questions"] if q["skill"] == first)
        self.assertEqual(asked[first], self.c.get(f"/api/items/{q['item']}").json()["podcast_question"],
                         "the same question the answer form asks")

        body = {"text": "I would keep the change small and check it against the new condition.", "elapsed_s": 20, "input": "typed"}
        refused = self.c.post(f"/api/items/{q['item']}/answer", json=body, headers=POST)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertTrue(refused.json()["detail"].startswith("Answer the podcast question first"), refused.text)
        sent = self.c.post(f"/api/items/{q['item']}/answer", json={**body, "podcast_heard": False}, headers=POST)
        self.assertEqual(sent.status_code, 200, sent.text)
        self.wait(sent.json())
        events = [e for e in self.dojo.store.events(self.owner.key) if e["data"].get("item") == q["item"]]
        self.assertEqual([e["type"] for e in events][:3], ["item.issued", "podcast.declared", "answer.submitted"])
        self.assertEqual((events[1]["data"]["heard"], events[1]["data"]["lesson"]), (False, lesson))
        after = self.c.get(f"/api/checks/{started['id']}").json()
        done = next(x for x in after["questions"] if x["item"] == q["item"])
        self.assertEqual((done["answered"], done["podcast_question"]), (True, None), "an answered question asks nothing")


if __name__ == "__main__":
    unittest.main()
