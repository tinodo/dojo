"""Team Dojo, PR 2 (docs/adr/0009-team-dojo.md): per-learner podcast and calendar links and AIRED notes,
enrollment, background work across learners through one scheduler, per-learner question pools, exam
removal across learners, and two-learner isolation over every learner route.

Like tests/test_team.py, every app runs as in Azure (local=False, signed in through X-MS-CLIENT-PRINCIPAL)
with stub models, on its own temporary data folder. The lifespan is not entered unless a test says so,
so no background thread runs behind a test's back."""
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import core
from app.ai import AIError
from app.background import BackgroundScheduler
from app.core import Slots
from app.exam import (POOL_DAILY_BATCHES, POOL_MAX_WAIT, POOL_MEMBER_DAILY_BATCHES, POOL_MEMBERS_DAILY_BATCHES,
                      POOL_MIN_WAIT, POOL_TEAM_LOOK)
from app.podcast import AIRED
from tests.test_phone import device
from tests.test_podcast import id3v2, mp3
from tests.test_team import OWNER, OWNER_KEY, TeamCase, classes, make, sample_path, who

ID = re.compile(r"\b[a-z]+-[0-9a-f]{20}\b")
# Path parameters that name shared content or a package, not a learner's own object.
SHARED_PARAMS = {"pid", "package", "sid", "lab", "lesson_id", "token", "name", "rest"}
# Every path parameter a learner route may carry that names the learner's own object. A new one must be
# added here, and so to the isolation test, before the inventory passes.
OWN_PARAMS = {"session_id", "moment", "item_id", "rid", "cid", "aid", "ask_id", "job_id", "media_id", "device_id",
              "circle", "invite", "share", "duel"}
# Routes that act on the caller's own list whatever the id: hiding a moment on one's own Play page (ADR 0010)
# answers 200 for any id. The snapshot of A's folder shows it never reaches A.
OWN_LIST_ONLY = {("DELETE", "/api/play/moments/{moment}"), ("DELETE", "/api/push/devices/{device_id}")}
# One body that every learner route with an id accepts, so a refusal is never a 422 on the body.
ANY_BODY = {"text": "my answer", "reason": "this is not fair", "index": 0, "choice": 0, "event": "open", "seconds": 1,
            "flagged": True, "start": (core.utcnow() + timedelta(days=7)).strftime("%Y-%m-%dT09:00"), "pinned": True,
            "elapsed_s": 1, "podcast_heard": False}


def params(path: str) -> set[str]:
    return set(re.findall(r"\{(\w+)(?::path)?\}", path))


def snapshot(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


class Team2Case(TeamCase):
    def learner_routes(self, *kinds: str):
        kinds = kinds or ("learner", "learner_exit")
        for r in self.app.routes:
            if isinstance(r, APIRoute) and classes(r) & set(kinds):
                for m in sorted(r.methods - {"HEAD"}):
                    yield r, m

    def wait_all(self, key: str) -> None:
        jobs = self.app.state.dojo.jobs
        for _ in range(600):
            # A check writes its questions from a thread of its own, which gives back its reservation when done.
            if not any(j.get("state") in ("queued", "running") for j in jobs.for_learner(core.Learner("", key, ""))) \
                    and not self.app.state.checks._filling:
                return
            time.sleep(0.05)
        self.fail("the learner's jobs did not finish")

    def skill(self, pid: str = "gh-300", kind: str | None = None) -> str:
        p = self.ok("GET", f"/api/packages/{pid}", OWNER)
        return next(s["id"] for d in p["domains"] for g in d["groups"] for s in g["skills"] if kind in (None, s["kind"]))


# ---------------------------------------------------------------- two learners, every learner route

class IsolationTests(Team2Case):
    def setUp(self):
        super().setUp()
        self.a = self.admit("m-a", email="a@example.com")
        self.b = self.admit("m-b", email="b@example.com")
        self.ka, self.kb = self.key("m-a"), self.key("m-b")
        # Not about money: a cap that the worst-case reservations of "one of everything" fit under.
        self.app.state.budget.set_limits(None, 100.0)

    def make_objects(self) -> tuple[set[str], set[str]]:
        """A makes one of everything a learner can own. Returns A's object ids and A's media ids."""
        a, sid = self.a, self.skill()
        item = self.wait_job(self.ok("POST", "/api/items", a, {"package": "gh-300", "skill": sid, "mode": "practice"}), a)
        self.assertEqual(item["state"], "done", item)
        item_id = next(iter(ID.findall(json.dumps(item["result"]))))
        self.call("POST", f"/api/items/{item_id}/answer", a, {"text": "An answer of mine.", "podcast_heard": False})
        self.wait_job(self.ok("POST", "/api/rehearsals", a, {"package": "gh-300", "count": 4}), a)
        self.wait_job(self.ok("POST", "/api/ask", a, {"package": "gh-300", "skill": sid, "question": "Why is that?"}), a)
        self.ok("POST", "/api/checks", a, {"package": "gh-300"})
        self.ok("POST", "/api/play/join", a, {})
        self.ok("POST", "/api/feed/link/new", a)
        self.ok("POST", "/api/calendar/link", a)
        dev = self.ok("POST", "/api/push/devices", a, device("a-phone"))["devices"][0]["id"]   # ADR 0013
        exam = self.ok("POST", "/api/exams", a, {"package": "gh-300", "length": "short"})
        self.wait_job(exam, a)
        self.aid = exam["attempt"]
        self.ok("POST", f"/api/exams/{self.aid}/start", a)   # left open: B tries the routes of an open attempt too
        plan = self.ok("GET", "/api/plan", a)
        self.wait_all(self.ka)
        # Check-question audio is A's own (ADR 0009). The stub speaks no audio, so one is put there.
        audio = "a" + "1" * 39
        self.store.write_bytes("learners", self.ka, "media", audio + ".mp3", data=id3v2() + mp3(20))
        folder = self.folder(self.ka)
        ids = {m for p in folder.rglob("*.json") for m in ID.findall(p.stem)}
        ids |= set(ID.findall(json.dumps(plan)))
        ids |= {j["id"] for j in self.app.state.dojo.jobs.for_learner(core.Learner("", self.ka, ""))}
        ids.add(dev)
        lessons = {m["id"] for m in self.app.state.dojo.lesson_index()}
        ids -= lessons   # shared content
        media = {p.stem for p in (folder / "media").glob("*.mp3")}
        return ids, media

    def test_the_route_inventory_names_every_own_object(self):
        found = set()
        for r, _ in self.learner_routes():
            found |= params(r.path) - SHARED_PARAMS
        self.assertEqual(found, OWN_PARAMS)

    def test_b_cannot_read_or_change_anything_of_a(self):
        ids, media = self.make_objects()
        prefixes = {i.split("-")[0] for i in ids}
        self.assertTrue({"item", "job"} <= prefixes, prefixes)
        self.assertGreaterEqual(len(prefixes), 5, prefixes)
        self.ok("POST", "/api/play/join", self.b, {})   # so a Play route reaches its lookup, not "Play is off"
        before = snapshot(self.folder(self.ka))
        wrong = set()
        for r, m in self.learner_routes():
            own = params(r.path) & OWN_PARAMS
            if not own:
                continue
            for value in sorted(media if own == {"media_id"} else ids):
                path = re.sub(r"\{(\w+)(?::path)?\}", lambda g: value if g.group(1) in OWN_PARAMS else sample_path(g.group(0)), r.path)
                body = {**ANY_BODY, "reason": "submitted"} if r.path.endswith("/submit") else ANY_BODY
                resp = self.call(m, path, self.b, body)
                if resp.status_code != 404 and not ((m, r.path) in OWN_LIST_ONLY and resp.status_code == 200):
                    wrong.add((m, r.path, resp.status_code, resp.text[:120]))
        self.maxDiff = None
        self.assertEqual(sorted(wrong), [])
        self.wait_all(self.kb)
        self.assertEqual(snapshot(self.folder(self.ka)), before)
        # And A still reaches its own audio once its exam has ended.
        self.ok("POST", f"/api/exams/{self.aid}/submit", self.a, {"reason": "submitted", "podcast_heard": False})
        self.assertEqual(self.call("GET", f"/api/media/{next(iter(media))}", self.a).status_code, 200)

    def test_b_never_sees_an_id_of_a(self):
        ids, media = self.make_objects()
        leaks = []
        for r, m in self.learner_routes():
            if m != "GET" or params(r.path) & OWN_PARAMS:
                continue
            resp = self.c.get(sample_path(r.path), params={"package": "gh-300"}, headers=self.b)
            text = resp.text
            leaks += [(r.path, x) for x in ids | media if x in text]
        self.assertEqual(leaks, [])
        self.ok("POST", f"/api/exams/{self.aid}/submit", self.a, {"reason": "submitted", "podcast_heard": False})
        export = json.dumps(self.ok("GET", "/api/record/export", self.a))
        self.assertTrue(any(x in export for x in ids))   # the check above looked at real ids

    def test_a_package_one_is_not_enrolled_in_answers_like_one_that_does_not_exist(self):
        c = self.admit("m-c", enroll=False)
        self.ok("PUT", "/api/settings", c, {"enrolled": ["gh-300"], "active_package": "gh-300"})
        other = self.skill("dp-800")
        wrong = []
        for r, m in self.learner_routes("learner"):
            model = r.body_field.field_info.annotation if r.body_field is not None else None
            fields = set(getattr(model, "model_fields", {}))
            query = {q.name for q in r.dependant.query_params}
            if not ({"pid", "package"} & params(r.path) or "package" in fields or "package" in query):
                continue
            path = re.sub(r"\{(pid|package)\}", "dp-800", sample_path(r.path.replace("{pid}", "dp-800").replace("{package}", "dp-800")))
            body = {"package": "dp-800", "skill": other, "mode": "practice", "question": "Why is that?", "count": 4,
                    "length": "short", "modality": "read", "earned": "2026-01-01"}
            resp = self.c.request(m, path, params={"package": "dp-800"}, json=body if m != "GET" else None,
                                  headers={**c, **({"X-Dojo": "1"} if m != "GET" else {})})
            if resp.status_code != 404:
                wrong.append((m, r.path, resp.status_code, resp.text[:100]))
        self.assertEqual(wrong, [])
        self.assertEqual(self.ok("GET", "/api/me", c)["enrolled"], ["gh-300"])
        self.ok("GET", "/api/packages/gh-300", c)
        plan = self.ok("GET", "/api/plan", c)
        self.assertEqual([x["package"] for x in plan["week"]["packages"]], ["gh-300"])
        self.assertEqual([x["package"] for x in plan["horizon"]], ["gh-300"])


# ---------------------------------------------------------------- podcast and calendar links per learner

class TokenTests(Team2Case):
    def setUp(self):
        super().setUp()
        dojo = self.app.state.dojo
        self.ok("PUT", "/api/settings", OWNER, {"later_hours": 30})   # the owner's folder
        self.lesson = dojo.make_lesson(lambda step: None, self.app.state.preparer.owner, "gh-300", self.skill())["lesson"]
        for i, m in enumerate(dojo.slide_media(dojo.lesson(self.lesson))):
            self.store.write_bytes("media", m + ".mp3", data=id3v2() + mp3(40 + i, fill=20 + i))
        self.a = self.admit("m-a", email="a@example.com")
        self.b = self.admit("m-b", email="b@example.com")

    def feed_token(self, h) -> str:
        return self.ok("POST", "/api/feed/link/new", h)["feeds"][0]["url"].split("/")[4]

    def cal_token(self, h) -> str:
        return self.ok("POST", "/api/calendar/link", h)["url"].split("/")[4]

    def learner(self, oid: str) -> core.Learner:
        return core.Learner("", self.key(oid), "")

    def test_each_link_serves_its_own_learner_and_notes_only_theirs(self):
        podcast = self.app.state.podcast
        ta, tb, to = self.feed_token(self.a), self.feed_token(self.b), self.feed_token(OWNER)
        self.assertEqual(len({ta, tb, to}), 3)
        self.assertEqual(podcast.links.learner_for(ta).key, self.key("m-a"))
        self.assertEqual(podcast.links.learner_for(tb).key, self.key("m-b"))
        self.assertEqual(podcast.links.learner_for(to).key, OWNER_KEY)
        r = self.c.get(f"/feed/{ta}/gh-300.xml")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.lesson, r.text)
        self.assertIn(self.lesson, json.dumps(podcast.notes(self.learner("m-a"))))
        self.assertEqual(podcast.notes(self.learner("m-b")), {})
        self.assertEqual(podcast.notes(podcast.links.owner), {})
        self.assertTrue((self.folder(self.key("m-a")) / f"{AIRED}.json").is_file())
        self.assertFalse((Path(self.tmp) / "podcast" / f"{AIRED}.json").exists())
        self.assertTrue(podcast.could_be_on_phone(self.learner("m-a")))
        # B's link is on, but nothing was listed for B: no podcast question comes from A's listening.
        self.ok("DELETE", "/api/feed/link", self.b)
        self.assertFalse(podcast.could_be_on_phone(self.learner("m-b")))
        ca, cb = self.cal_token(self.a), self.cal_token(self.b)
        self.assertNotEqual(ca, cb)
        self.assertEqual(self.c.get(f"/cal/{ca}/dojo.ics").status_code, 200)
        self.assertEqual(self.c.get(f"/cal/{cb}/dojo.ics").status_code, 200)

    def test_a_removed_member_link_answers_not_found(self):
        ta, ca = self.feed_token(self.a), self.cal_token(self.a)
        self.ok("POST", f"/api/team/members/{self.row('m-a')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 404)
        self.assertEqual(self.c.get(f"/feed/{ta}/ep/{self.lesson}.mp3").status_code, 404)
        self.assertEqual(self.c.get(f"/cal/{ca}/dojo.ics").status_code, 404)

    def test_paused_and_left_members_links_stop_and_the_owner_always_works(self):
        to, co, ta = self.feed_token(OWNER), self.cal_token(OWNER), self.feed_token(self.a)
        self.ok("POST", "/api/team/pause", OWNER)
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 404)
        self.assertEqual(self.c.get(f"/feed/{to}/gh-300.xml").status_code, 200)
        self.assertEqual(self.c.get(f"/cal/{co}/dojo.ics").status_code, 200)
        self.ok("POST", "/api/team/resume", OWNER)
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 200)
        # members.json unreadable: the members' links stop, the owner's do not.
        (Path(self.tmp) / "team" / "members.json").write_text("{not json", "utf-8")
        self.assertEqual(self.c.get(f"/feed/{to}/gh-300.xml").status_code, 200)
        self.assertEqual(self.c.get(f"/cal/{co}/dojo.ics").status_code, 200)
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 404)

    def test_an_expired_membership_stops_its_links_before_the_sweeper_runs(self):
        to, ta, ca, tb = self.feed_token(OWNER), self.feed_token(self.a), self.cal_token(self.a), self.feed_token(self.b)
        with self.store.lock:
            row = self.row("m-a")
            row["renewed"] = "2020-01"   # twelve months over long ago; the row still says active
            self.team._save()
        self.assertEqual(self.row("m-a")["status"], "active")
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 404)
        self.assertEqual(self.c.get(f"/cal/{ca}/dojo.ics").status_code, 404)
        self.assertNotIn(self.key("m-a"), [x.key for x in self.team.active_learners()])
        self.assertEqual(self.c.get(f"/feed/{tb}/gh-300.xml").status_code, 200)
        self.assertEqual(self.c.get(f"/feed/{to}/gh-300.xml").status_code, 200)

    def test_on_the_end_date_members_links_stop_and_the_owner_keeps_working(self):
        to, co, ta, ca = self.feed_token(OWNER), self.cal_token(OWNER), self.feed_token(self.a), self.cal_token(self.a)
        st = self.team.state()
        st["end"] = {"told_at": core.iso(core.utcnow() - timedelta(days=20)), "date": core.utcnow().date().isoformat()}
        self.team._save_state(st)   # the date has come; the hourly sweeper has not run yet
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 404)
        self.assertEqual(self.c.get(f"/cal/{ca}/dojo.ics").status_code, 404)
        self.assertEqual(self.team.active_learners(), [])
        self.assertEqual(self.c.get(f"/feed/{to}/gh-300.xml").status_code, 200)
        self.assertEqual(self.c.get(f"/cal/{co}/dojo.ics").status_code, 200)
        # The member's next request finds the access ended, and export and delete still answer.
        self.refused("GET", "/api/plan/today", self.a, "removed")
        self.assertEqual(self.row("m-a")["reason"], "team-ended")
        self.ok("GET", "/api/me", self.a)

    def test_the_owner_link_works_with_team_mode_off(self):
        to, co, ta = self.feed_token(OWNER), self.cal_token(OWNER), self.feed_token(self.a)
        self.app, self.c, _ = make(self.tmp, team=False)
        self.assertEqual(self.c.get(f"/feed/{to}/gh-300.xml").status_code, 200)
        self.assertEqual(self.c.get(f"/cal/{co}/dojo.ics").status_code, 200)
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 404)

    def test_leaving_deletes_the_links_and_notes(self):
        ta, ca = self.feed_token(self.a), self.cal_token(self.a)
        self.c.get(f"/feed/{ta}/gh-300.xml")
        key = self.key("m-a")
        self.ok("POST", "/api/team/leave", self.a, {"confirm": "LEAVE"})
        self.assertFalse(self.folder(key).exists())
        self.assertEqual(self.c.get(f"/feed/{ta}/gh-300.xml").status_code, 404)
        self.assertEqual(self.c.get(f"/cal/{ca}/dojo.ics").status_code, 404)
        self.assertEqual(self.app.state.podcast.notes(core.Learner("", key, "")), {})


# ---------------------------------------------------------------- moving the owner's AIRED notes

class AiredMigrationTests(unittest.TestCase):
    LEGACY = {"gh-300": {"s1": {"les-00000000000000000001": "2026-03-01T10:00:00+00:00",
                                "les-00000000000000000002": "2026-03-03T10:00:00+00:00"}}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-aired-")
        self.old = Path(self.tmp) / "podcast" / f"{AIRED}.json"
        self.new = Path(self.tmp) / "learners" / OWNER_KEY / f"{AIRED}.json"
        self.moved = Path(self.tmp) / "podcast" / f"{AIRED}.json.migrated"

    def write(self, path: Path, doc) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc), "utf-8")

    def start(self, team: bool = False):
        app, c, _ = make(self.tmp, team=team)
        self.addCleanup(app.state.podcast.close)
        return app

    def test_first_start_moves_and_the_next_start_changes_nothing(self):
        self.write(self.old, self.LEGACY)
        app = self.start()
        self.assertFalse(self.old.exists())
        self.assertTrue(self.moved.exists())
        self.assertEqual(json.loads(self.new.read_text("utf-8")), self.LEGACY)
        self.assertEqual(app.state.podcast.notes(app.state.podcast.links.owner), self.LEGACY)
        before = (self.new.read_bytes(), self.moved.read_bytes())
        app = self.start(team=True)
        self.assertEqual((self.new.read_bytes(), self.moved.read_bytes()), before)
        self.assertEqual(app.state.podcast.migrate_aired(), "nothing to move")

    def test_a_crash_after_copying_is_merged_with_the_earlier_times(self):
        self.write(self.old, self.LEGACY)
        self.write(self.new, {"gh-300": {"s1": {"les-00000000000000000001": "2026-03-02T10:00:00+00:00",
                                                "les-00000000000000000003": "2026-03-04T10:00:00+00:00"}}})
        self.start()
        self.assertEqual(json.loads(self.new.read_text("utf-8")), {"gh-300": {"s1": {
            "les-00000000000000000001": "2026-03-01T10:00:00+00:00",
            "les-00000000000000000002": "2026-03-03T10:00:00+00:00",
            "les-00000000000000000003": "2026-03-04T10:00:00+00:00"}}})
        self.assertFalse(self.old.exists())

    def test_an_unreadable_old_file_stays_the_owners(self):
        self.write(self.old, "{not json")
        app = self.start()
        self.assertTrue(self.old.exists())
        self.assertFalse(self.new.exists())
        self.assertTrue(app.state.podcast._legacy)
        with self.assertRaises(ValueError):   # as before PR 2: nothing is guessed, the file is left for a person
            app.state.podcast.notes(app.state.podcast.links.owner)

    def test_a_failed_copy_keeps_reading_the_old_file(self):
        self.write(self.old, self.LEGACY)
        with mock.patch("app.core.Store.write", side_effect=OSError("share is read-only")):
            app = self.start()
        self.assertTrue(self.old.exists())
        self.assertEqual(app.state.podcast.notes(app.state.podcast.links.owner), self.LEGACY)


# ---------------------------------------------------------------- one background coordinator

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fake_jobs(count: int = 3):
    slots = Slots(count)
    return SimpleNamespace(slots=slots, take_spare=lambda: slots.take_spare())


class SchedulerTests(unittest.TestCase):
    def scheduler(self, names, wait=0.0):
        jobs, clock = fake_jobs(), FakeClock()
        s = BackgroundScheduler(jobs, clock)
        ran = []
        for n in names:
            s.add(n, (lambda n=n: ran.append((n, jobs.slots.held())) or wait), 0, wait=lambda r: float(r))
        return s, jobs, clock, ran

    def test_no_batch_while_two_slots_are_held(self):
        s, jobs, _, ran = self.scheduler(["a", "b"])
        jobs.slots.acquire(who="x")
        jobs.slots.acquire(who="y")
        self.assertIsNone(s.turn())
        self.assertEqual(ran, [])
        jobs.slots.release()
        self.assertEqual(s.turn(), 0)
        self.assertEqual(ran, [("a", 2)])   # it took one slot, so two were held while it ran
        self.assertEqual(jobs.slots.held(), 1)

    def test_no_batch_while_a_learner_waits_for_a_slot(self):
        s, jobs, _, ran = self.scheduler(["a"])
        jobs.slots.acquire(who="x")
        waiting = threading.Thread(target=lambda: (jobs.slots.acquire(who="y"), jobs.slots.release()))
        with mock.patch.object(jobs.slots, "_waiting", [("y", 99)]):
            self.assertIsNone(s.turn())
        self.assertEqual(ran, [])
        waiting.start()
        waiting.join(5)

    def test_a_waiting_service_keeps_its_turn(self):
        s, jobs, _, ran = self.scheduler(["a", "b", "c"])
        s.turn()
        for _ in range(2):
            jobs.slots.acquire(who="x")
        self.assertIsNone(s.turn())
        self.assertIsNone(s.turn())
        jobs.slots.release()
        s.turn()
        self.assertEqual([n for n, _ in ran], ["a", "b"])

    def test_the_rotation_is_real(self):
        s, _, _, ran = self.scheduler(["a", "b", "c", "d", "e"])
        for _ in range(10):
            self.assertEqual(s.turn(), 0)
        self.assertEqual([n for n, _ in ran], list("abcde") * 2)
        self.assertEqual([x.runs for x in s.services], [2] * 5)

    def test_a_service_not_due_passes_its_turn(self):
        s, _, clock, ran = self.scheduler(["a", "b"], wait=100.0)
        s.turn()
        s.turn()
        self.assertEqual(s.turn(), 100.0)   # nothing is due: it says how long until something is
        clock.now += 100
        s.turn()
        self.assertEqual([n for n, _ in ran], ["a", "b", "a"])

    def test_a_wake_brings_a_service_forward(self):
        jobs, clock = fake_jobs(), FakeClock()
        s, ran, wake = BackgroundScheduler(jobs, clock), [], threading.Event()
        s.add("prepare", lambda: ran.append(1) or False, 120, wait=lambda worked: 1800, wake=wake)
        self.assertGreater(s.turn(), 0)
        wake.set()
        self.assertEqual(s.turn(), 0)
        self.assertEqual(ran, [1])

    def test_never_two_batches_at_once(self):
        jobs, clock = fake_jobs(), FakeClock()
        s = BackgroundScheduler(jobs, clock)
        active, most, lock = [0], [0], threading.Lock()

        def step():
            with lock:
                active[0] += 1
                most[0] = max(most[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            return 0

        for n in "abc":
            s.add(n, step, 0)
        threads = [threading.Thread(target=lambda: [s.turn() for _ in range(5)]) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(most[0], 1)
        self.assertEqual(jobs.slots.held(), 0)

    def test_a_crashing_step_releases_its_slot_and_the_others_go_on(self):
        jobs, clock = fake_jobs(), FakeClock()
        s, ran = BackgroundScheduler(jobs, clock), []
        s.add("bad", lambda: 1 / 0, 0)
        s.add("good", lambda: ran.append(1) or 0, 0)
        with self.assertLogs("dojo.background", "ERROR"):
            s.turn()
        s.turn()
        self.assertEqual((ran, jobs.slots.held()), ([1], 0))

    def test_slots_serve_the_least_recently_served_learner_first(self):
        slots, order = Slots(1), []
        slots.acquire(who="A")

        def ask(who):
            slots.acquire(who=who)
            order.append(who)
            slots.release()

        threads = []
        for who_ in ("A", "A", "B", "C"):
            t = threading.Thread(target=ask, args=(who_,))
            t.start()
            threads.append(t)
            for _ in range(200):
                if len(slots._waiting) == len(threads):
                    break
                time.sleep(0.005)
        slots.release()
        for t in threads:
            t.join(5)
        self.assertEqual(order, ["B", "C", "A", "A"])


class SchedulerWiringTests(Team2Case):
    def test_team_mode_runs_every_service_through_the_one_scheduler(self):
        env = {"DOJO_PREPARE": "1", "DOJO_CHECK": "1", "DOJO_DRIFT": "1"}
        object.__setattr__(self.app.state.settings, "real_ai", True)   # the lifespan reads this one object
        with mock.patch.dict("os.environ", env), mock.patch("app.background.BackgroundScheduler.start"), \
                mock.patch.object(self.app.state.selftest, "start"), mock.patch.object(self.app.state.team, "start"):
            with TestClient(self.app):
                names = [s.name for s in self.app.state.background.services]
        self.assertEqual(names, ["prepare", "pool", "drift", "deeper", "delivery"])
        for svc in ("preparer", "pool", "drift", "deeper", "checker"):
            self.assertTrue(getattr(self.app.state, svc).scheduled, svc)
        pool = next(s for s in self.app.state.background.services if s.name == "pool")
        self.assertIs(pool.wake, self.app.state.pool.wake)   # a nudge brings the pool's turn forward

    def test_team_mode_off_keeps_each_service_on_its_own(self):
        self.app, self.c, _ = make(self.tmp, team=False)
        with mock.patch.dict("os.environ", {"DOJO_DRIFT": "1"}), mock.patch("app.drift.DriftCheck.start") as start:
            with TestClient(self.app):
                pass
        self.assertEqual(self.app.state.background.services, [])
        start.assert_called_once()
        self.assertFalse(self.app.state.pool.scheduled)


# ---------------------------------------------------------------- question pools per learner

class PoolTests(Team2Case):
    def setUp(self):
        super().setUp()
        self.ok("PUT", "/api/settings", OWNER, {"later_hours": 30})
        self.pool, self.room = self.app.state.pool, self.app.state.exams
        self.calls = []

    def top_up(self, learner, pid, **kw):
        self.calls.append((learner.key, core.current_writer(), pid))
        return {"added": 1, "state": "done", "blocked": 0}

    def turns(self, n: int) -> list[str]:
        with mock.patch.object(self.room, "top_up", self.top_up):
            for _ in range(n):
                self.pool._due.clear()
                self.pool.step()
        return [k for k, _, _ in self.calls]

    def test_learners_take_turns_and_each_batch_is_theirs(self):
        a, b = self.admit("m-a"), self.admit("m-b")
        ka, kb = self.key("m-a"), self.key("m-b")
        self.assertEqual([x.key for x in self.pool.learners()], [OWNER_KEY, ka, kb])
        self.assertEqual(self.turns(6), [OWNER_KEY, ka, kb] * 2)
        for key, writer, _ in self.calls:
            self.assertEqual(writer, key)   # written and attributed as the learner whose pool it fills
        for key in (OWNER_KEY, ka, kb):
            self.assertEqual(self.store.read("learners", key, "pool-budget")["batches"], 2)

    def test_a_member_studies_only_their_exams(self):
        c = self.admit("m-c", enroll=False)
        self.ok("PUT", "/api/settings", c, {"enrolled": ["dp-800"], "active_package": "dp-800"})
        self.turns(2)
        self.assertEqual(self.calls[1][2], "dp-800")
        self.assertEqual(self.pool._next_package(core.Learner("", self.key("m-c"), "")), "dp-800")

    def test_a_busy_or_paused_learner_is_skipped_and_the_others_go_on(self):
        self.admit("m-a")
        ka = self.key("m-a")
        busy = lambda who: who.key == ka  # noqa: E731
        with mock.patch.object(self.pool, "_busy", busy):
            self.assertEqual(self.turns(3), [OWNER_KEY] * 3)
        self.calls.clear()
        self.pool._paused.add(ka)   # a deletion of A's record is running (Pool.paused holds this while it runs)
        self.assertEqual(self.turns(2), [OWNER_KEY] * 2)
        self.pool._paused.clear()
        self.calls.clear()
        self.pool._paused.add(OWNER_KEY)
        self.assertEqual(self.turns(1), [ka])
        self.pool._paused.clear()

    def test_an_inactive_member_has_no_pool_kept(self):
        self.admit("m-a")
        events = self.folder(self.key("m-a")) / "events.jsonl"
        old = time.time() - 15 * 86400
        os.utime(events, (old, old))
        self.assertEqual([x.key for x in self.pool.learners()], [OWNER_KEY])

    def test_with_team_mode_off_the_owner_alone_as_today(self):
        self.admit("m-a")
        self.app, self.c, _ = make(self.tmp, team=False)
        self.pool, self.room = self.app.state.pool, self.app.state.exams
        self.assertEqual([x.key for x in self.pool.learners()], [OWNER_KEY])
        with mock.patch.object(self.room, "top_up", self.top_up):
            self.assertEqual(self.pool.step(), POOL_MIN_WAIT)
        self.assertEqual([k for k, _, _ in self.calls], [OWNER_KEY])

    def test_members_pools_share_one_daily_cap_and_the_owner_keeps_his_own(self):
        self.admit("m-a")
        ka = self.key("m-a")
        member, owner = core.Learner("", ka, ""), self.pool.owner
        self.assertEqual(self.pool._limit(member), POOL_MEMBER_DAILY_BATCHES)
        self.assertEqual(self.pool._limit(owner), POOL_DAILY_BATCHES)
        self.assertIsNone(self.pool._charge(member))
        shared = self.store.read("team", "pool-budget")
        self.assertEqual(set(shared), {"day", "batches", "updated"})   # one count for all members, no learner in it
        self.assertEqual(shared["batches"], 1)
        self.assertNotIn(ka, json.dumps(shared))
        # The members' total for today is used up: a member's batch is refused, the owner's is not.
        self.store.write("team", "pool-budget", value={**shared, "batches": POOL_MEMBERS_DAILY_BATCHES})
        self.assertIn("together", self.pool._charge(member))
        self.assertEqual(self.store.read("learners", ka, "pool-budget")["batches"], 1)   # refused: nothing written
        self.assertTrue(self.pool._spent(member))
        self.assertIsNone(self.pool._charge(owner))
        self.assertEqual(self.store.read("team", "pool-budget")["batches"], POOL_MEMBERS_DAILY_BATCHES)
        # Yesterday's total no longer counts; a member's own allowance still does.
        self.store.write("team", "pool-budget", value={"day": "2000-01-01", "batches": 99})
        self.assertIsNone(self.pool._charge(member))
        self.store.write("learners", ka, "pool-budget",
                         value={**self.pool._budget(member), "batches": POOL_MEMBER_DAILY_BATCHES})
        self.assertIn(f"limit of {POOL_MEMBER_DAILY_BATCHES}", self.pool._charge(member))

    def test_with_nothing_to_fill_team_mode_looks_again_within_half_an_hour(self):
        self.assertLessEqual(POOL_TEAM_LOOK, 1800)
        with mock.patch.object(self.pool, "_next_package", return_value=None):
            self.pool.scheduled = True
            self.assertEqual(self.pool.step(), POOL_TEAM_LOOK)
            self.pool.scheduled = False   # the owner's own loop, team mode off: six hours, as today
            self.assertEqual(self.pool.step(), POOL_MAX_WAIT)

    def test_a_change_of_exams_gives_that_members_pool_its_turn_now(self):
        c = self.admit("m-c", enroll=False)
        kc = self.key("m-c")
        self.pool.scheduled = True
        self.pool._due.update({OWNER_KEY: time.monotonic() + POOL_MAX_WAIT, kc: time.monotonic() + POOL_MAX_WAIT})
        self.pool.wake.clear()
        self.ok("PUT", "/api/settings", c, {"enrolled": ["dp-800"], "active_package": "dp-800"})
        self.assertTrue(self.pool.wake.is_set())   # the scheduler gives the pool a turn now
        self.assertNotIn(kc, self.pool._due)
        self.assertIn(OWNER_KEY, self.pool._due)   # only that member's turn comes forward
        with mock.patch.object(self.room, "top_up", self.top_up):
            self.pool.step()
        self.assertEqual(self.calls, [(kc, kc, "dp-800")])
        self.pool.wake.clear()
        self.ok("PUT", "/api/settings", c, {"later_hours": 20})   # not a change of exams: no nudge
        self.assertFalse(self.pool.wake.is_set())


# ---------------------------------------------------------------- removing an added exam across learners

class ExamRemovalTests(Team2Case):
    def setUp(self):
        super().setUp()
        raw = copy.deepcopy(self.app.state.dojo.packages.builtin_raw("gh-300"))
        raw.update(id="zz-900", exam="ZZ-900")
        self.app.state.dojo.packages.add(raw)
        self.a = self.admit("m-a", email="a@example.com", name="Ann Example")
        self.b = self.admit("m-b", email="b@example.com")
        self.ka, self.kb = self.key("m-a"), self.key("m-b")

    def test_refused_while_a_member_has_an_open_attempt_then_removed_for_everyone(self):
        exam = self.ok("POST", "/api/exams", self.a, {"package": "zz-900", "length": "short"})
        self.assertEqual(self.wait_job(exam, self.a)["state"], "done")
        aid = exam["attempt"]
        self.ok("POST", f"/api/exams/{aid}/start", self.a)
        self.store.write("learners", self.kb, "pool", "zz-900", value={"questions": []})
        r = self.call("POST", "/api/exam-packages/zz-900/remove", OWNER, {"confirm": "REMOVE"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("1 timed practice exam", r.text)
        for private in ("Ann", "a@example.com", self.ka, "m-a"):
            self.assertNotIn(private, r.text)
        self.ok("POST", f"/api/exams/{aid}/submit", self.a, {"reason": "submitted", "podcast_heard": False})
        self.ok("POST", "/api/exam-packages/zz-900/remove", OWNER, {"confirm": "REMOVE"})
        packages = self.app.state.dojo.packages
        self.assertFalse(packages.is_added("zz-900"))
        for key in (self.ka, self.kb):
            self.assertFalse((self.folder(key) / "pool" / "zz-900.json").exists())
            self.assertNotIn("zz-900", self.store.read("learners", key, "profile")["enrolled"])
        self.assertNotIn("zz-900", self.ok("GET", "/api/me", self.a)["enrolled"])
        past = self.ok("GET", f"/api/exams/{aid}", self.a)
        self.assertIn("ZZ-900 (removed)", json.dumps(past))
        self.ok("POST", "/api/exams", self.a, {"package": "zz-900", "length": "short"}, status=404)


class AttemptLiveTests(Team2Case):
    """An attempt is worked on only while its exam is live and the learner studies it; a past attempt can
    always be read and an open one can always be handed in (ADR 0009, review of PR 2)."""

    def setUp(self):
        super().setUp()
        self.packages = self.app.state.dojo.packages
        self.raw = copy.deepcopy(self.packages.builtin_raw("gh-300"))
        self.raw.update(id="zz-900", exam="ZZ-900")
        self.packages.add(self.raw)
        self.room = self.app.state.exams
        self.a = self.admit("m-a", email="a@example.com")

    def ready(self, pid: str = "zz-900") -> str:
        exam = self.ok("POST", "/api/exams", self.a, {"package": pid, "length": "short"})
        self.assertEqual(self.wait_job(exam, self.a)["state"], "done")
        return exam["attempt"]

    def conflict(self, method: str, path: str, says: str, body=None) -> None:
        r = self.call(method, path, self.a, body)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn(says, r.text)

    def submit(self, aid: str) -> None:
        self.ok("POST", f"/api/exams/{aid}/submit", self.a, {"reason": "submitted", "podcast_heard": False})

    def test_a_ready_attempt_of_an_exam_left_out_does_not_start(self):
        aid = self.ready()
        self.ok("PUT", "/api/settings", self.a, {"enrolled": ["gh-300"], "active_package": "gh-300"})
        self.conflict("POST", f"/api/exams/{aid}/start", "no longer study")
        self.assertEqual(self.ok("GET", f"/api/exams/{aid}", self.a)["state"], "ready")

    def test_an_exam_cannot_be_left_out_while_its_attempt_runs(self):
        aid = self.ready()
        self.ok("POST", f"/api/exams/{aid}/start", self.a)
        self.conflict("PUT", "/api/settings", "running", {"enrolled": ["gh-300"], "active_package": "gh-300"})
        self.assertIn("zz-900", self.ok("GET", "/api/me", self.a)["enrolled"])
        self.submit(aid)
        self.ok("PUT", "/api/settings", self.a, {"enrolled": ["gh-300"], "active_package": "gh-300"})
        self.assertEqual(self.ok("GET", f"/api/exams/{aid}", self.a)["state"], "closed")   # history stays

    def test_a_ready_attempt_of_a_removed_exam_does_not_start(self):
        aid = self.ready()
        self.ok("POST", "/api/exam-packages/zz-900/remove", OWNER, {"confirm": "REMOVE"})
        self.conflict("POST", f"/api/exams/{aid}/start", "removed")
        self.assertIn("ZZ-900 (removed)", json.dumps(self.ok("GET", f"/api/exams/{aid}", self.a)))

    def test_while_an_exam_is_being_removed_nothing_opens_or_changes_and_hand_in_works(self):
        aid = self.ready()
        other = self.ready()
        self.room.closing.add("zz-900")
        try:
            self.conflict("POST", "/api/exams", "removed", {"package": "zz-900", "length": "short"})
            self.conflict("POST", f"/api/exams/{other}/start", "removed")
            self.room.closing.discard("zz-900")
            self.ok("POST", f"/api/exams/{aid}/start", self.a)
            self.room.closing.add("zz-900")
            for op in ("answer", "visit", "learn", "flag"):
                self.conflict("POST", f"/api/exams/{aid}/{op}", "removed", ANY_BODY)
            self.ok("GET", f"/api/exams/{aid}", self.a)
            self.submit(aid)
        finally:
            self.room.closing.discard("zz-900")

    def test_removal_and_a_start_never_overlap(self):
        """The reproduction: a start that lands while the exam is being deleted. It is refused, and after a
        removal that failed half-way the exam is open again."""
        aid = self.ready()
        seen = {}
        real = self.packages.remove

        def remove(pid):
            # Another request starts the ready attempt while the deletion runs.
            t = threading.Thread(target=lambda: seen.update(r=self.call("POST", f"/api/exams/{aid}/start", self.a)))
            t.start()
            t.join(10)
            self.assertFalse(t.is_alive(), "the start waited for the deletion: the lock is held too long")
            real(pid)

        with mock.patch.object(self.packages, "remove", remove):
            self.ok("POST", "/api/exam-packages/zz-900/remove", OWNER, {"confirm": "REMOVE"})
        self.assertEqual(seen["r"].status_code, 409, seen["r"].text)
        self.assertEqual(self.room.closing, set())
        self.assertEqual(self.ok("GET", f"/api/exams/{aid}", self.a)["state"], "ready")

    def test_a_removal_that_fails_leaves_the_exam_open(self):
        aid = self.ready()
        with mock.patch.object(self.packages, "remove", side_effect=OSError("the share went away")):
            with self.assertRaises(OSError):
                self.call("POST", "/api/exam-packages/zz-900/remove", OWNER, {"confirm": "REMOVE"})
        self.assertEqual(self.room.closing, set())
        self.ok("POST", f"/api/exams/{aid}/start", self.a)

    def test_an_exam_added_again_does_not_change_how_old_attempts_read(self):
        aid = self.ready()
        old = self.packages.version("zz-900")
        self.assertEqual(self.store.read("learners", self.key("m-a"), "exams", aid)["package_version"], old)
        self.ok("POST", "/api/exam-packages/zz-900/remove", OWNER, {"confirm": "REMOVE"})
        first = self.raw["domains"][0]["groups"][0]["skills"][0]
        again = copy.deepcopy(self.raw)
        again["domains"][0]["groups"][0]["skills"][0]["text"] = "A different skill, written later"
        self.packages.add(again)
        self.assertNotEqual(self.packages.version("zz-900"), old)
        past = self.packages.get_any("zz-900", old)
        self.assertEqual(past["skills"][first["id"]]["text"], first["text"])
        self.assertTrue(past["meta"].get("retired"))
        self.assertIn("ZZ-900 (removed)", json.dumps(self.ok("GET", f"/api/exams/{aid}", self.a)))
        # A new attempt is written from the live version.
        self.assertEqual(self.packages.get_any("zz-900", self.packages.version("zz-900"))["skills"][first["id"]]["text"],
                         "A different skill, written later")

    def member(self) -> core.Learner:
        return core.Learner("", self.key("m-a"), "")

    def attempts(self) -> list[dict]:
        folder = self.folder(self.key("m-a")) / "exams"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]

    def remove_now(self) -> None:
        self.app.state.newexams.remove(self.app.state.pool.owner, "zz-900")

    def test_an_exam_removed_while_an_attempt_is_reserved_leaves_no_attempt(self):
        """The reproduction: the removal runs while create() works out the exam's length (spec)."""
        import app.exam as exam_module
        real = exam_module.spec

        def spec(*args, **kw):
            self.remove_now()
            return real(*args, **kw)

        with mock.patch("app.exam.spec", spec), self.assertRaises(HTTPException) as e:
            self.room.create(self.member(), "zz-900", "short")
        self.assertEqual(e.exception.status_code, 409)
        self.assertFalse(self.packages.is_added("zz-900"))
        self.assertEqual([d for d in self.attempts() if d["package"] == "zz-900"], [])

    def test_a_build_whose_exam_was_removed_is_refused_and_marked_broken(self):
        doc = self.room.create(self.member(), "zz-900", "short")
        self.remove_now()   # nothing is open, so the removal goes ahead while the attempt is being built
        with self.assertRaises(core.UserError):
            self.room.build(lambda _: None, self.member(), doc["id"])
        saved = self.store.read("learners", self.key("m-a"), "exams", doc["id"])
        self.assertEqual((saved["state"], saved["items"]), ("broken", []))
        self.assertFalse((self.folder(self.key("m-a")) / "pool" / "zz-900.json").exists())   # no pool written back
        self.assertEqual(self.call("POST", f"/api/exams/{doc['id']}/start", self.a).status_code, 409)

    def test_a_removal_in_the_middle_of_a_build_stops_it(self):
        doc = self.room.create(self.member(), "zz-900", "short")
        real = self.room._write
        calls = []

        def write(*args, **kw):
            out = real(*args, **kw)
            if not calls:
                self.remove_now()
            calls.append(1)
            return out

        with mock.patch.object(self.room, "_write", write), self.assertRaises(Exception):
            self.room.build(lambda _: None, self.member(), doc["id"])
        self.assertEqual(calls, [1])   # the build stopped at its next save
        saved = self.store.read("learners", self.key("m-a"), "exams", doc["id"])
        self.assertEqual((saved["state"], saved["items"]), ("broken", []))

    def failing_build(self, error: Exception, remove: bool) -> tuple[Exception, dict]:
        """The question writer fails; with `remove`, the exam is removed while it writes."""
        doc = self.room.create(self.member(), "zz-900", "short")

        def write(*args, **kw):
            if remove:
                self.remove_now()
            raise error

        with mock.patch.object(self.room, "_write", write), self.assertRaises(Exception) as e:
            self.room.build(lambda _: None, self.member(), doc["id"])
        return e.exception, self.store.read("learners", self.key("m-a"), "exams", doc["id"])

    def test_a_refused_build_whose_exam_was_removed_is_marked_broken(self):
        for error in (core.UserError("No question could be checked."), AIError("the model is away"), OSError("disk")):
            with self.subTest(type(error).__name__):
                if not self.packages.is_added("zz-900"):
                    self.packages.add(self.raw)
                    self.ok("PUT", "/api/settings", self.a, {"enrolled": ["gh-300", "zz-900"]})
                raised, saved = self.failing_build(error, remove=True)
                self.assertIsInstance(raised, core.UserError)
                self.assertIn("removed", str(raised))
                self.assertEqual((saved["state"], saved["items"]), ("broken", []))

    def test_a_refused_build_of_a_live_exam_fails_as_today(self):
        raised, saved = self.failing_build(core.UserError("No question could be checked."), remove=False)
        self.assertEqual(str(raised), "No question could be checked.")
        self.assertEqual(saved["state"], "building")   # the page marks it broken once the job has ended (_fresh)
        self.assertTrue(self.packages.is_added("zz-900"))

    def test_an_attempt_from_before_versions_is_bound_to_the_retired_version(self):
        aid = self.ready()
        old = self.packages.version("zz-900")
        legacy = self.store.read("learners", self.key("m-a"), "exams", aid)
        legacy.pop("package_version")   # written before this PR
        self.store.write("learners", self.key("m-a"), "exams", aid, value=legacy)
        self.ok("POST", "/api/exam-packages/zz-900/remove", OWNER, {"confirm": "REMOVE"})
        self.assertEqual(self.store.read("learners", self.key("m-a"), "exams", aid)["package_version"], old)
        first = self.raw["domains"][0]["groups"][0]["skills"][0]
        again = copy.deepcopy(self.raw)
        again["domains"][0]["groups"][0]["skills"][0]["text"] = "A different skill, written later"
        self.packages.add(again)
        self.assertIn("ZZ-900 (removed)", json.dumps(self.ok("GET", f"/api/exams/{aid}", self.a)))
        self.assertEqual(self.packages.get_any("zz-900", old)["skills"][first["id"]]["text"], first["text"])


# ---------------------------------------------------------------- team mode off: the owner exactly as today

class TeamOffTests(Team2Case):
    team_on = False

    def test_the_owner_studies_every_exam_and_keeps_one_link(self):
        me = self.ok("GET", "/api/me", OWNER)
        self.assertEqual(me["enrolled"], [p["id"] for p in me["packages"]])
        feeds = self.ok("POST", "/api/feed/link/new", OWNER)["feeds"]
        self.assertEqual({f["package"] for f in feeds}, {p["id"] for p in me["packages"]})
        self.assertEqual(self.c.get("/api/me", headers=who("m-a")).status_code, 403)
        self.assertEqual(self.app.state.pool.learners(), [self.app.state.pool.owner])


if __name__ == "__main__":
    unittest.main()
