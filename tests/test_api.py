"""End-to-end API tests in local mode: stub models, stub speech and a mocked web for the sources.
The quote checks, gates, evidence and record run for real."""
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.speech import MAX_AUDIO_BYTES, StubSpeech  # noqa: E402

GITHUB_MD = """# Content exclusion for GitHub Copilot

You can use content exclusion to configure Copilot to ignore certain files in a repository.

When you exclude content from Copilot, code completion is not available in the affected files and the content does not inform suggestions in other files.

Repository administrators and organization owners can manage content exclusion settings for their repositories and organizations.

Content exclusion settings may take up to 30 minutes to take effect in IDEs where the settings are already loaded.
"""
LEARN_HTML = """<html><head><title>Doc</title></head><body><nav>Skip to main content</nav><main>
<h1>CREATE TABLE</h1>
<p>Creates a new table in the database and defines its columns, data types, and constraints for the stored data.</p>
<p>A columnstore index is a technology for storing, retrieving, and managing data by using a columnar data format.</p>
<p>Rowstore indexes perform best on queries that seek into the data, when searching for a particular value.</p>
<p>You can create a clustered columnstore index on a table to improve query performance for analytics workloads.</p>
</main><footer>Feedback</footer></body></html>"""


def web(request: httpx.Request) -> httpx.Response:
    if request.url.host == "docs.github.com":
        return httpx.Response(200, text=GITHUB_MD, headers={"content-type": "text/markdown"})
    if request.url.host == "learn.microsoft.com":
        return httpx.Response(200, text=LEARN_HTML, headers={"content-type": "text/html"})
    return httpx.Response(404)


def settings(tmp: str, local: bool = True) -> Settings:
    return Settings(local=local, real_ai=False, in_azure=False, data_dir=Path(tmp), ai_endpoint="", ai_resource_id="",
                    speech_endpoint="", speech_region="swedencentral", models={}, owner_tid="tenant-1", owner_oid="owner-1",
                    auth_client_id="", identity_client_id="", tenant_id="", build="test")


POST = {"X-Dojo": "1"}


class ApiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-api-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.pid = "gh-300"
        p = cls.c.get(f"/api/packages/{cls.pid}").json()
        cls.sid = next(s["id"] for d in p["domains"] for g in d["groups"] for s in g["skills"] if s["kind"] == "scenario")

    def wait(self, job: dict) -> dict:
        self.assertIn("id", job, job)
        for _ in range(200):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def test_01_health_and_me(self):
        health = self.c.get("/healthz").json()
        self.assertEqual(set(health), {"status", "build"})
        self.assertEqual(health["build"], "test")
        me = self.c.get("/api/me").json()
        self.assertEqual(me["name"], "Local learner")
        self.assertEqual({p["id"] for p in me["packages"]}, {"gh-300", "dp-800"})
        index = self.c.get("/")
        self.assertIn("Content-Security-Policy", index.headers)
        self.assertIn("script-src 'self'", index.headers["Content-Security-Policy"])

    def test_02_state_changes_need_the_dojo_header(self):
        r = self.c.post("/api/lessons", json={"package": self.pid, "skill": self.sid})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/lessons", json={"package": self.pid, "skill": self.sid}, headers={**POST, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_02b_same_site_posts_pass_behind_the_platform(self):
        # Browsers send Origin: null in some same-site cases; App Service may forward a different Host.
        for extra in ({"Origin": "null"}, {"Origin": "https://dojo.example", "X-Forwarded-Host": "dojo.example"}, {}):
            r = self.c.put("/api/settings", json={"later_hours": 20}, headers={**POST, **extra})
            self.assertEqual(r.status_code, 200, (extra, r.text))
        self.assertEqual(self.c.get("/").headers["Referrer-Policy"], "same-origin")

    def test_03_lesson_quotes_are_checked_and_removed_parts_are_listed(self):
        lesson_id = self.wait(self.post("/api/lessons", {"package": self.pid, "skill": self.sid}))["lesson"]
        L = self.c.get(f"/api/lessons/{lesson_id}").json()
        texts = [p["text"] for s in L["sections"] for p in s["points"]]
        self.assertIn("First point.", texts)
        self.assertNotIn("Invented point.", texts)
        self.assertTrue(any("word for word" in b["reason"] for b in L["blocked"]))
        self.assertEqual(L["sources"][0]["license"], "CC-BY-4.0")
        self.post(f"/api/lessons/{lesson_id}/viewed", {"modality": "read"})
        audio = self.wait(self.post(f"/api/lessons/{lesson_id}/audio"))["audio"]
        self.assertEqual(len(audio), len(L["slides"]))
        m = self.c.get(f"/api/media/{audio[0]}")
        self.assertEqual((m.status_code, m.headers["content-type"]), (200, "audio/mpeg"))
        quality = self.c.get("/api/quality").json()
        self.assertTrue(any(q["kind"] == "lesson" for q in quality))

    def test_04_probe_practice_check_dispute(self):
        probe = self.wait(self.post("/api/items", {"package": self.pid, "skill": self.sid, "mode": "probe"}))["item"]
        view = self.c.get(f"/api/items/{probe}").json()
        self.assertNotIn("rubric", view)
        self.assertNotIn("model_answer", view)
        self.assertEqual(self.c.post(f"/api/items/{probe}/hint", headers=POST).status_code, 409)
        self.wait(self.post(f"/api/items/{probe}/answer", {"text": "I am not sure what to do here.", "elapsed_s": 40}))
        view = self.c.get(f"/api/items/{probe}").json()
        self.assertEqual((view["result"]["met"], view["result"]["total"]), (0, 2))
        self.assertIn("rubric", view)
        self.assertEqual(self.c.post(f"/api/items/{probe}/answer", json={"text": "again"}, headers=POST).status_code, 409)
        skill = self.c.get(f"/api/skills/{self.pid}/{self.sid}").json()
        self.assertEqual(skill["evidence"]["state"], "not_met")

        practice = self.wait(self.post("/api/items", {"package": self.pid, "skill": self.sid, "mode": "practice"}))["item"]
        self.assertNotEqual(practice, probe)
        hint = self.post(f"/api/items/{practice}/hint")
        self.assertEqual(hint["index"], 0)
        self.wait(self.post(f"/api/items/{practice}/answer", {"text": "Cover r1 and r2, everything relevant.", "elapsed_s": 60}))
        skill = self.c.get(f"/api/skills/{self.pid}/{self.sid}").json()
        self.assertEqual(skill["evidence"]["state"], "met_with_help")

        check = self.wait(self.post("/api/items", {"package": self.pid, "skill": self.sid, "mode": "check"}))["item"]
        self.wait(self.post(f"/api/items/{check}/answer", {"text": "Everything: r1 and r2 both apply here.", "elapsed_s": 50}))
        skill = self.c.get(f"/api/skills/{self.pid}/{self.sid}").json()
        self.assertEqual(skill["evidence"]["state"], "met_unaided")
        self.assertTrue(skill["evidence"]["checks"]["unaided"])
        self.assertFalse(skill["evidence"]["checks"]["later"])

        self.post(f"/api/items/{check}/dispute", {"reason": "It was too lenient."})
        skill = self.c.get(f"/api/skills/{self.pid}/{self.sid}").json()
        self.assertEqual(skill["evidence"]["state"], "met_with_help")
        self.assertEqual(skill["evidence"]["disputed"], 1)
        self.assertEqual(self.c.post(f"/api/items/{check}/dispute", json={"reason": "again"}, headers=POST).status_code, 409)

        today = self.c.get(f"/api/today/{self.pid}").json()
        self.assertTrue(today["suggestions"])
        self.assertLess(today["coverage"]["weight_not_shown_unaided"], 101)

    def test_04c_package_view_says_what_was_seen_taught_and_disputed(self):
        # The course page draws the "seen" eye and the disputed tag from the package view,
        # so it repeats three flags the server has already worked out for each skill.
        p = self.c.get(f"/api/packages/{self.pid}").json()
        skills = {s["id"]: s for d in p["domains"] for g in d["groups"] for s in g["skills"]}
        mine, other = skills[self.sid], next(s for sid, s in skills.items() if sid != self.sid)
        self.assertEqual((mine["seen"], mine["has_lesson"], mine["disputed"]), (True, True, True))
        self.assertEqual((other["seen"], other["has_lesson"], other["disputed"]), (False, False, False))

    def test_05_rehearsal(self):
        res = self.wait(self.post("/api/rehearsals", {"package": "dp-800", "count": 5}))
        R = self.c.get(f"/api/rehearsals/{res['rehearsal']}").json()
        self.assertEqual(len(R["items"]), 5)
        self.assertNotIn("answer", R["items"][0])
        q = R["items"][0]
        out = self.post(f"/api/rehearsals/{R['id']}/answer", {"index": 0, "choice": 0})
        self.assertIn("correct", out)
        self.assertEqual(out["source"]["license"], "CC-BY-4.0")
        R = self.c.get(f"/api/rehearsals/{R['id']}").json()
        self.assertEqual(R["answered"], 1)
        self.assertEqual(self.c.post(f"/api/rehearsals/{R['id']}/answer", json={"index": 0, "choice": 1}, headers=POST).status_code, 409)
        self.assertTrue(q["stem"])

    def test_06_ask(self):
        ask = self.wait(self.post("/api/ask", {"package": self.pid, "skill": self.sid, "question": "What does this do?"}))["ask"]
        A = self.c.get(f"/api/ask/{ask}").json()
        self.assertTrue(A["points"])
        self.assertTrue(A["points"][0]["quote"])

    def test_07_settings_export_delete(self):
        s = self.c.put("/api/settings", json={"exam_dates": {"gh-300": "2027-02-15"}, "later_hours": 24}, headers=POST).json()
        self.assertEqual((s["exam_dates"]["gh-300"], s["later_hours"]), ("2027-02-15", 24))
        self.assertEqual(self.c.put("/api/settings", json={"exam_dates": {"gh-300": "soon"}}, headers=POST).status_code, 400)
        data = self.c.get("/api/record/export").json()
        self.assertTrue(data["chain"]["ok"])
        self.assertGreater(len(data["events"]), 5)
        self.assertEqual(self.c.post("/api/record/delete", json={"confirm": "yes"}, headers=POST).status_code, 422)
        self.post("/api/record/delete", {"confirm": "DELETE"})
        self.assertEqual(self.c.get("/api/record/chain").json(), {"ok": True, "events": 0})

    def test_08_bad_ids_are_refused(self):
        self.assertEqual(self.c.get("/api/items/..%2F..%2Fsecret").status_code, 400)  # refused before routing
        self.assertEqual(self.c.get("/api/items/not-an-id").status_code, 404)
        self.assertEqual(self.c.get("/api/lessons/not-an-id").status_code, 404)
        self.assertEqual(self.c.get("/api/media/abc").status_code, 404)
        self.assertEqual(self.c.get("/api/skills/gh-300/zz.zz").status_code, 404)


class HealthTests(unittest.TestCase):
    """/healthz is open to anyone: it says whether the self-test passed and which build runs, nothing more."""

    def setUp(self):
        self.app = create_app(settings(tempfile.mkdtemp(prefix="dojo-health-")), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.c = TestClient(self.app)

    def health(self, state: str, checks: dict) -> dict:
        self.app.state.selftest.result = {"state": state, "checks": checks, "details": {"ai.author": "secret-ish detail"},
                                          "at": "2026-10-05T08:00:00Z", "build": "test"}
        r = self.c.get("/healthz")
        self.assertEqual(r.status_code, 200, "the platform health check needs 200 even when degraded")
        return r.json()

    def test_only_status_and_build_are_public(self):
        self.assertEqual(self.health("done", {"data": True, "ai.author": True}), {"status": "ok", "build": "test"})
        self.assertEqual(self.health("done", {"data": True, "ai.author": False}), {"status": "degraded", "build": "test"})
        self.assertEqual(self.health("running", {"data": True}), {"status": "degraded", "build": "test"})
        self.assertEqual(self.health("pending", {}), {"status": "degraded", "build": "test"})
        self.assertEqual(self.health("done", {}), {"status": "degraded", "build": "test"}, "no checks is not a pass")

    def test_the_signed_in_diag_keeps_the_detail(self):
        self.health("done", {"data": True, "ai.author": False})
        diag = self.c.get("/api/diag").json()["selftest"]
        self.assertEqual(diag["checks"], {"data": True, "ai.author": False})
        self.assertEqual(diag["at"], "2026-10-05T08:00:00Z")


class AboutRegionTests(unittest.TestCase):
    """The About page names the region the app runs in, as App Service reports it, and never a fixed one."""

    def storage(self, env: dict) -> str:
        with mock.patch.dict(os.environ, env):
            if not env:
                os.environ.pop("REGION_NAME", None)
            app = create_app(settings(tempfile.mkdtemp(prefix="dojo-about-")), http=httpx.Client(transport=httpx.MockTransport(web)))
            return TestClient(app).get("/api/about").json()["storage"]

    def test_the_region_comes_from_app_service(self):
        self.assertIn("in Azure (West Europe)", self.storage({"REGION_NAME": "West Europe"}))
        self.assertNotIn("Sweden", self.storage({"REGION_NAME": "West Europe"}))

    def test_without_a_region_none_is_named(self):
        text = self.storage({})
        self.assertIn("in the operator's Azure region", text)
        self.assertNotIn("Sweden", text)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings(tempfile.mkdtemp(prefix="dojo-conc-")), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.dojo = self.app.state.dojo
        self.me = self.app.state.preparer.owner

    def test_delete_refuses_while_something_runs(self):
        import threading
        release = threading.Event()
        self.dojo.jobs.start(self.me, "slow", lambda progress: release.wait(5))
        with self.assertRaises(Exception) as ctx:
            self.dojo.delete_all(self.me)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)
        release.set()
        for _ in range(100):
            if not any(j["state"] in ("queued", "running") for j in self.dojo.jobs.for_learner(self.me)):
                break
            time.sleep(0.05)
        self.assertEqual(self.dojo.delete_all(self.me), {"deleted": True})

    def test_job_limit_holds_under_concurrent_starts(self):
        import threading
        release = threading.Event()
        started, refused = [], []

        def go():
            try:
                started.append(self.dojo.jobs.start(self.me, "slow", lambda progress: release.wait(5)))
            except Exception:
                refused.append(1)

        threads = [threading.Thread(target=go) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        release.set()
        self.assertEqual(len(started), 6)
        self.assertEqual(len(refused), 6)


class LessonViewTests(unittest.TestCase):
    """Every start of a lesson is exposure; repeats within 30 minutes are kept once."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings(tempfile.mkdtemp(prefix="dojo-views-")), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.dojo, cls.me = cls.app.state.dojo, cls.app.state.preparer.owner
        cls.sid = sorted(cls.dojo.packages.get("gh-300")["skills"])[0]
        cls.lesson = cls.dojo.make_lesson(lambda step: None, cls.me, "gh-300", cls.sid)["lesson"]

    def view(self, modality: str) -> dict:
        r = self.c.post(f"/api/lessons/{self.lesson}/viewed", json={"modality": modality}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def views(self) -> list[tuple[str, str]]:
        return [(e["data"]["modality"], e["data"]["lesson"]) for e in self.dojo.store.events(self.me.key) if e["type"] == "lesson.viewed"]

    def test_repeats_within_thirty_minutes_are_kept_once_per_way_of_viewing(self):
        from datetime import timedelta
        from unittest import mock

        from app.core import utcnow
        before = len(self.views())
        self.assertTrue(self.view("watch")["recorded"])
        self.assertFalse(self.view("watch")["recorded"])
        self.assertTrue(self.view("listen")["recorded"])
        self.assertEqual(len(self.views()), before + 2)
        now = utcnow()
        for minutes, recorded in ((29, False), (31, True)):
            later = now + timedelta(minutes=minutes)
            with mock.patch("app.learning.utcnow", return_value=later), mock.patch("app.core.utcnow", return_value=later):
                self.assertEqual(self.view("watch")["recorded"], recorded, f"a watch start {minutes} minutes later")
        event = [e for e in self.dojo.store.events(self.me.key) if e["type"] == "lesson.viewed"][-1]
        self.assertEqual(event["data"], {"package": "gh-300", "skill": self.sid, "lesson": self.lesson, "modality": "watch", "repeats_within_min": 30})
        self.assertTrue(self.dojo.store.verify_chain(self.me.key)["ok"])

    def test_watch_is_a_way_of_viewing_and_nothing_else_is(self):
        self.view("watch")
        for modality in ("podcast", "video"):
            r = self.c.post(f"/api/lessons/{self.lesson}/viewed", json={"modality": modality}, headers=POST)
            self.assertEqual(r.status_code, 422, modality)
        exposures = self.c.get(f"/api/skills/gh-300/{self.sid}").json()["evidence"]["exposures"]
        self.assertIn("watch", [x["modality"] for x in exposures])
        self.assertNotIn("podcast", [x["modality"] for x in exposures])
        self.assertTrue(all(x["lesson"] == self.lesson and x["extent"] == "unknown" for x in exposures))


class PreparationTests(unittest.TestCase):
    def test_prepares_the_heaviest_missing_lesson_first_and_waits_for_the_learner(self):
        app = create_app(settings(tempfile.mkdtemp(prefix="dojo-prep-")), http=httpx.Client(transport=httpx.MockTransport(web)))
        prep, dojo = app.state.preparer, app.state.dojo
        pkg = dojo.packages.get("gh-300")
        heaviest = sorted(pkg["skills"].values(), key=lambda s: (-s["share"], s["id"]))[0]["id"]
        self.assertEqual(prep.next_skill(), ("gh-300", heaviest))
        self.assertTrue(prep.step())
        self.assertIn(heaviest, dojo.lesson_skills("gh-300"))
        lesson = dojo.lessons_for("gh-300", heaviest)[0]["id"]
        self.assertTrue(all(dojo.lesson_view(lesson)["audio"]), "narration should be ready")
        self.assertNotEqual(prep.next_skill(), ("gh-300", heaviest))
        busy = {"id": "job-00000000000000000000", "kind": "item", "learner": prep.owner.key, "ref": None, "state": "running",
                "step": "", "created": "2026-09-28T00:00:00+00:00", "heartbeat": "2026-09-28T00:00:00+00:00", "result": None, "error": None}
        dojo.store.write("jobs", busy["id"], value=busy)
        before = len(dojo.lesson_skills("gh-300"))
        self.assertTrue(prep.step())
        self.assertEqual(len(dojo.lesson_skills("gh-300")), before, "must not prepare while the learner has work running")
        self.assertEqual(prep.status()["packages"]["gh-300"]["ready"], before)

    def test_narrates_lessons_whose_audio_is_missing_once_every_skill_has_one(self):
        app = create_app(settings(tempfile.mkdtemp(prefix="dojo-narr-")), http=httpx.Client(transport=httpx.MockTransport(web)))
        prep, dojo = app.state.preparer, app.state.dojo
        self.assertTrue(prep.step())
        meta = dojo.lesson_index()[0]
        lesson, pid, sid = meta["id"], meta["package"], meta["skill"]
        for media in dojo.lesson_view(lesson)["audio"]:
            dojo.store.delete_file("media", media + ".mp3")
        prep._narrated.clear()
        self.assertTrue(dojo.lesson_audio_missing(lesson))
        prep.next_skill = lambda: None  # every skill has a lesson now
        self.assertEqual(prep.narration_targets(), [(pid, sid, lesson)])
        self.assertEqual(prep.status()["needs_narration"], 1)
        self.assertEqual(prep.next_narration(), (pid, sid, lesson))
        self.assertTrue(prep.step())
        self.assertFalse(dojo.lesson_audio_missing(lesson))
        self.assertEqual(prep.state["narrated"], 1)
        self.assertEqual(prep.status()["needs_narration"], 0)
        self.assertTrue(prep.step(), "then the lesson's Deeper Listen (ADR 0007)")
        self.assertTrue(dojo.deep_ready(lesson))
        self.assertFalse(prep.step(), "nothing left to do")


class SpokenAnswerTests(unittest.TestCase):
    """The route that writes down what the learner said, and the honest record of how an answer was given."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-spoken-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.pid = "gh-300"
        p = cls.c.get(f"/api/packages/{cls.pid}").json()
        cls.scenarios = [s["id"] for d in p["domains"] for g in d["groups"] for s in g["skills"] if s["kind"] == "scenario"]
        cls.sid = cls.scenarios[0]
        cls.spare = iter(cls.scenarios[1:])  # a fresh skill per item: Dojo will not repeat an item it has already asked

    def files(self) -> set:
        return {str(p) for p in Path(self.tmp).rglob("*")}

    def new_item(self, sid: str | None = None) -> str:
        skill = sid or next(self.spare)
        return self.wait(self.c.post("/api/items", json={"package": self.pid, "skill": skill, "mode": "check"}, headers=POST).json())["item"]

    def say(self, audio: bytes = b"fake opus bytes", kind: str = "audio/webm", item: str = "ask", **extra):
        return self.c.post(f"/api/transcribe?item={item}", content=audio, headers={**POST, "Content-Type": kind, **extra})

    def receipt_for(self, item: str) -> list[str]:
        return [self.say(item=item).json()["receipt"]]

    def wait(self, job: dict) -> dict:
        for _ in range(200):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def test_the_stub_writes_down_a_fixed_sentence_and_stores_nothing(self):
        before = self.files()
        r = self.say()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["text"], StubSpeech.STUB_TRANSCRIPT)
        self.assertGreater(r.json()["seconds"], 0)
        self.assertTrue(r.json()["receipt"], "the browser needs a receipt to show the answer was really spoken")
        self.assertEqual(self.files(), before, "nothing may be written while writing down what was said")

    def test_the_receipt_says_nothing_about_what_was_said(self):
        receipt = self.say().json()["receipt"]
        held = self.app.state.dojo.receipts._open[receipt]
        self.assertEqual(len(held), 3, "only the learner, which answer it is for, and when it runs out")
        self.assertNotIn(StubSpeech.STUB_TRANSCRIPT, json.dumps([receipt, held[0], held[1]]))

    def test_the_recording_has_to_say_which_answer_it_is_for(self):
        self.assertEqual(self.say(item="ask").status_code, 200, "the coach box is allowed to have no item")
        self.assertEqual(self.say(item="not-an-item").status_code, 404)

    def test_it_needs_the_dojo_header(self):
        r = self.c.post("/api/transcribe", content=b"x", headers={"Content-Type": "audio/webm"})
        self.assertEqual(r.status_code, 403)

    def test_only_audio_types_a_browser_records_are_accepted(self):
        for kind in ("audio/webm", "audio/webm;codecs=opus", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav"):
            self.assertEqual(self.say(kind=kind).status_code, 200, kind)
        for kind in ("application/json", "text/plain", "video/webm", "audio/flac"):
            self.assertEqual(self.say(kind=kind).status_code, 415, kind)

    def test_a_huge_body_is_refused_before_it_is_read(self):
        big = self.c.post("/api/transcribe", content=b"0" * (MAX_AUDIO_BYTES + 1), headers={**POST, "Content-Type": "audio/webm"})
        self.assertEqual(big.status_code, 413)

        def chunks():  # no Content-Length: the cap has to hold while the body is read
            for _ in range(12):
                yield b"0" * (1024 * 1024)

        streamed = self.c.post("/api/transcribe", content=chunks(), headers={**POST, "Content-Type": "audio/webm"})
        self.assertEqual(streamed.status_code, 413)

    def test_the_microphone_is_allowed_for_this_site_only(self):
        policy = self.c.get("/").headers["Permissions-Policy"]
        self.assertIn("microphone=(self)", policy)
        for off in ("camera=()", "geolocation=()", "payment=()", "usb=()"):
            self.assertIn(off, policy)
        self.assertNotIn("blob:", self.c.get("/").headers["Content-Security-Policy"])

    def spoken_answer(self, receipts=None, sid: str | None = None) -> tuple[str, str, dict]:
        """Answer one item on a skill of its own, claiming it was spoken. Returns the skill, the item and
        its view. `receipts` may be a list, or a function of the item id, so a test can ask for a receipt
        made for this very answer."""
        skill = sid or next(self.spare)
        item = self.new_item(skill)
        tickets = receipts(item) if callable(receipts) else (receipts or [])
        self.wait(self.c.post(f"/api/items/{item}/answer", headers=POST,
                              json={"text": "Everything: r1 and r2 both apply here.", "elapsed_s": 30, "input": "spoken",
                                    "edited_before_send": True, "receipts": tickets}).json())
        return skill, item, self.c.get(f"/api/items/{item}").json()

    def test_a_spoken_answer_is_kept_like_a_typed_one_and_says_how_it_was_given(self):
        skill, item, view = self.spoken_answer(self.receipt_for, self.sid)
        self.assertEqual(view["answer"]["text"], "Everything: r1 and r2 both apply here.")
        self.assertEqual(view["answer"]["input"], "spoken")
        self.assertTrue(view["answer"]["edited_before_send"])
        events = self.c.get("/api/record/export").json()["events"]
        submitted = [e for e in events if e["type"] == "answer.submitted" and e["data"]["item"] == item]
        self.assertEqual(submitted[0]["data"]["input"], "spoken")
        self.assertTrue(submitted[0]["data"]["edited_before_send"])
        self.assertNotIn("audio", json.dumps(submitted[0]))
        record = self.c.get(f"/api/skills/{self.pid}/{skill}").json()["evidence"]
        attempt = record["attempts"][-1]
        self.assertIn("Spoken: Dojo wrote down a recording for this question before the answer was sent,"
                      " and it was corrected before sending", attempt["conditions"])
        self.assertTrue(attempt["unaided"], "speaking is a condition, never a reason to count for less")

    def test_a_claim_of_speaking_without_a_receipt_is_recorded_as_unconfirmed(self):
        skill, _, view = self.spoken_answer([])
        self.assertEqual(view["answer"]["input"], "spoken_unconfirmed")
        self.assertTrue(view["answer"]["edited_before_send"])
        attempt = self.c.get(f"/api/skills/{self.pid}/{skill}").json()["evidence"]["attempts"][-1]
        self.assertIn("Sent as spoken, but Dojo has no record of writing down a recording for this question",
                      attempt["conditions"])
        self.assertTrue(attempt["unaided"], "an unconfirmed claim is still not help and not a penalty")

    def test_a_receipt_only_speaks_for_the_answer_it_was_made_for(self):
        mine = self.new_item()
        receipt = self.receipt_for(mine)
        other = self.spoken_answer(receipt)[2]  # the same receipt, a different question
        self.assertEqual(other["answer"]["input"], "spoken_unconfirmed")
        self.wait(self.c.post(f"/api/items/{mine}/answer", headers=POST,
                              json={"text": "Everything: r1 and r2 both apply here.", "elapsed_s": 30,
                                    "input": "spoken", "receipts": receipt}).json())
        self.assertEqual(self.c.get(f"/api/items/{mine}").json()["answer"]["input"], "spoken",
                         "a receipt is not used up by the answer it did not belong to")
        for_the_coach = [self.say().json()["receipt"]]  # the header box records for "ask", never for an item
        self.assertEqual(self.spoken_answer(for_the_coach)[2]["answer"]["input"], "spoken_unconfirmed")

    def test_a_made_up_or_forgotten_receipt_is_unconfirmed_not_an_error(self):
        self.assertEqual(self.spoken_answer(["not-a-real-receipt"])[2]["answer"]["input"], "spoken_unconfirmed")
        kept = self.new_item()
        receipt = self.receipt_for(kept)
        self.app.state.dojo.receipts = type(self.app.state.dojo.receipts)()  # as after a restart
        self.wait(self.c.post(f"/api/items/{kept}/answer", headers=POST,
                              json={"text": "Everything: r1 and r2 both apply here.", "elapsed_s": 30,
                                    "input": "spoken", "receipts": receipt}).json())
        self.assertEqual(self.c.get(f"/api/items/{kept}").json()["answer"]["input"], "spoken_unconfirmed")

    def test_a_receipt_is_used_once_runs_out_and_belongs_to_one_learner_and_one_answer(self):
        book = self.app.state.dojo.receipts
        mine = book.issue("me", "item-1")
        self.assertFalse(book.consume("someone-else", "item-1", [mine]), "a receipt is no proof for another learner")
        self.assertFalse(book.consume("me", "item-2", [mine]), "nor for another question")
        self.assertTrue(book.consume("me", "item-1", [mine]))
        self.assertFalse(book.consume("me", "item-1", [mine]), "and it only ever counts once")
        old = book.issue("me", "item-1")
        book._open[old] = (*book._open[old][:2], time.time() - 1)
        self.assertFalse(book.consume("me", "item-1", [old]), "an old receipt no longer counts")
        self.assertTrue(book.consume("me", "item-1", ["nonsense", book.issue("me", "item-1")]))

    def test_the_grader_is_only_told_about_speaking_when_dojo_wrote_it_down(self):
        dojo = self.app.state.dojo
        s = dojo.packages.skill(self.pid, self.sid)
        it = {"stem": "Say what you would do.", "rubric": [{"id": "r1", "point": "Mention the setting."}],
              "answer": {"input": "spoken_unconfirmed"}}
        self.assertNotIn("a machine wrote down", dojo._grade_prompt(s, [], it, "co-pilot is enabled"))

    def test_a_typed_answer_says_nothing_about_speaking(self):
        skill = next(self.spare)
        item = self.new_item(skill)
        self.wait(self.c.post(f"/api/items/{item}/answer", headers=POST, json={"text": "Both r1 and r2 apply here.", "elapsed_s": 20}).json())
        view = self.c.get(f"/api/items/{item}").json()
        self.assertEqual(view["answer"]["input"], "typed")
        record = self.c.get(f"/api/skills/{self.pid}/{skill}").json()["evidence"]
        self.assertFalse([c for c in record["attempts"][-1]["conditions"] if "poken" in c])

    def test_the_grader_is_told_when_an_answer_was_spoken(self):
        dojo = self.app.state.dojo
        s = dojo.packages.skill(self.pid, self.sid)
        it = {"stem": "Say what you would do.", "rubric": [{"id": "r1", "point": "Mention the setting."}],
              "answer": {"input": "spoken"}}
        spoken = dojo._grade_prompt(s, [], it, "co-pilot is enabled")
        self.assertIn("a machine wrote down", spoken)
        self.assertIn("word for word", spoken)
        typed = dojo._grade_prompt(s, [], dict(it, answer={"input": "typed"}), "copilot is enabled")
        self.assertNotIn("a machine wrote down", typed)


def principal(tid: str, oid: str) -> str:
    claims = [{"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tid},
              {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": oid},
              {"typ": "name", "val": "Alex"}]
    return base64.b64encode(json.dumps({"auth_typ": "aad", "claims": claims}).encode()).decode()


class SignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(create_app(settings(tempfile.mkdtemp(prefix="dojo-auth-"), local=False)))

    def test_no_sign_in_is_refused(self):
        self.assertEqual(self.c.get("/api/me").status_code, 401)

    def test_someone_else_is_refused(self):
        self.assertEqual(self.c.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "someone-else")}).status_code, 403)
        self.assertEqual(self.c.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL": principal("other-tenant", "owner-1")}).status_code, 403)

    def test_mismatched_principal_id_is_refused(self):
        headers = {"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "owner-1"), "X-MS-CLIENT-PRINCIPAL-ID": "someone-else"}
        self.assertEqual(self.c.get("/api/me", headers=headers).status_code, 403)

    def test_garbage_is_refused(self):
        self.assertEqual(self.c.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL": "not base64 !!"}).status_code, 401)

    def test_owner_is_let_in(self):
        r = self.c.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "owner-1"), "X-MS-CLIENT-PRINCIPAL-ID": "owner-1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "Alex")

    def test_health_needs_no_sign_in(self):
        self.assertEqual(self.c.get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
