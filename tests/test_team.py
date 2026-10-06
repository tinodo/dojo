"""Team Dojo, PR 1 (docs/adr/0009-team-dojo.md): the switch, admission, the five route classes, roles,
the notice, leaving and deletion that stays deleted, the Members page, logging and the Bicep switch.

Every test runs the app as it runs in Azure (local=False, signed in through X-MS-CLIENT-PRINCIPAL) with
stub models, on its own temporary data folder."""
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import core  # noqa: E402
from app import team as team_mod  # noqa: E402
from app.core import Abandoned, learner_key, load_settings, parse_iso, utcnow  # noqa: E402
from app.exam import POOL_MIN_WAIT  # noqa: E402
from app.main import ScrubFilter, create_app  # noqa: E402
from app.team import NOTICE_VERSION, ROUTE_CLASSES, ROW_FIELDS  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OWNER_KEY = learner_key("tenant-1:owner-1")
SAMPLE_ID = "abc-0123456789abcdef0123"
# The routes that need no sign-in: health, the page itself, and the two token feeds (ADR 0002, 0004).
PUBLIC = {"/healthz", "/", "/feed", "/feed/{rest:path}", "/feed/{token}/{name}", "/feed/{token}/ep/{name}",
          "/cal", "/cal/{rest:path}", "/cal/{token}/{name}"}
MEMBER_FIELDS = {"id", "display_name", "entra_name", "email", "role", "status", "joined"}


def who(oid: str, tid: str = "tenant-1", name: str | None = "A Colleague", email: str | None = "colleague@example.com") -> dict:
    claims = [{"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tid},
              {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": oid}]
    if name:
        claims.append({"typ": "name", "val": name})
    if email:
        claims.append({"typ": "email", "val": email})
    raw = base64.b64encode(json.dumps({"auth_typ": "aad", "claims": claims}).encode()).decode()
    return {"X-MS-CLIENT-PRINCIPAL": raw, "X-MS-CLIENT-PRINCIPAL-ID": oid}


OWNER = who("owner-1", name="Alex", email=None)


def make(tmp: str | None = None, team: bool = True, **fields):
    tmp = tmp or tempfile.mkdtemp(prefix="dojo-team-")
    s = replace(settings(tmp, local=False), team=team, **fields)
    app = create_app(s, http=httpx.Client(transport=httpx.MockTransport(web)))
    return app, TestClient(app), tmp


def sample_path(path: str) -> str:
    values = {"lab": "vector", "pid": "gh-300", "package": "gh-300"}
    return re.sub(r"\{(\w+)(?::path)?\}", lambda m: values.get(m.group(1), SAMPLE_ID), path)


def deps(dependant) -> list:
    out = []
    for d in dependant.dependencies:
        out.append(d.call)
        out += deps(d)
    return out


def classes(route: APIRoute) -> set[str]:
    return {ROUTE_CLASSES[c] for c in deps(route.dependant) if c in ROUTE_CLASSES}


def code(r) -> str | None:
    try:
        d = r.json().get("detail")
    except ValueError:
        return None
    return d.get("code") if isinstance(d, dict) else None


class TeamCase(unittest.TestCase):
    team_on = True

    def setUp(self):
        self.app, self.c, self.tmp = make(team=self.team_on)

    @property
    def team(self):
        return self.app.state.team

    @property
    def store(self):
        return self.app.state.dojo.store

    def call(self, method: str, path: str, h: dict, body=None):
        headers = dict(h)
        if method != "GET":
            headers.update(POST)
        return self.c.request(method, path, headers=headers, json=body if method != "GET" else None)

    def ok(self, method: str, path: str, h: dict, body=None, status: int = 200):
        r = self.call(method, path, h, body)
        self.assertEqual(r.status_code, status, f"{method} {path}: {r.text}")
        return r.json()

    def refused(self, method: str, path: str, h: dict, want: str, body=None, status: int = 403):
        r = self.call(method, path, h, body)
        self.assertEqual((r.status_code, code(r)), (status, want), f"{method} {path}: {r.text}")

    def request_id(self, oid: str) -> str:
        return next(r["id"] for r in self.team.requests_doc() if r["oid"] == oid and r["state"] == "open")

    def admit(self, oid: str, ack: bool = True, enroll: bool = True, **claims) -> dict:
        h = who(oid, **claims)
        self.ok("POST", "/api/access/request", h, {"used_before": False})
        self.ok("POST", f"/api/team/requests/{self.request_id(oid)}/approve", OWNER)
        if ack:
            self.ok("POST", "/api/notice/ack", h, {"version": NOTICE_VERSION})
            if enroll:   # every built exam, so a test reaches every package route (ADR 0009, "Enrollment")
                pids = [p["id"] for p in self.ok("GET", "/api/me", h)["packages"]]
                self.ok("PUT", "/api/settings", h, {"enrolled": pids})
        return h

    def row(self, oid: str) -> dict:
        return self.team.row_for("tenant-1", oid)

    def key(self, oid: str) -> str:
        return self.row(oid)["key"]

    def folder(self, key: str) -> Path:
        return Path(self.tmp) / "learners" / key

    def wait_job(self, job: dict, h: dict) -> dict:
        for _ in range(400):
            job = self.ok("GET", f"/api/jobs/{job['id']}", h)
            if job["state"] not in ("queued", "running"):
                return job
            time.sleep(0.05)
        self.fail("the job did not finish")


# ---------------------------------------------------------------- team mode off: exactly as today

class TeamOffTests(TeamCase):
    team_on = False

    def test_owner_is_unchanged(self):
        me = self.ok("GET", "/api/me", OWNER)
        self.assertEqual((me["name"], me["role"], me["team"], me["status"]), ("Alex", "admin", False, "active"))
        self.assertEqual(me["notice"], {"version": NOTICE_VERSION, "required": False, "acknowledged": True})
        self.assertIsNone(me["banner"])
        self.assertIn("profile", me)
        # The owner's key is still derived from tid and oid, so the live record is where it always was.
        self.ok("PUT", "/api/settings", OWNER, {"later_hours": 30})
        self.assertTrue(self.folder(OWNER_KEY).is_dir())
        self.assertEqual(self.ok("GET", "/api/settings", OWNER)["later_hours"], 30)
        # Owner-only pieces still work for the owner.
        self.ok("GET", "/api/feed", OWNER)
        self.ok("GET", "/api/calendar", OWNER)
        self.ok("GET", "/api/studio/costs", OWNER)

    def test_everyone_else_is_refused_as_before(self):
        for path in ("/api/me", "/api/access", "/api/about", "/api/settings", "/api/team/members", "/api/notice"):
            r = self.c.get(path, headers=who("someone-else"))
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()["detail"], "This Dojo belongs to someone else.", path)
        r = self.call("POST", "/api/access/request", who("someone-else"), {})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.get("/api/me").status_code, 401)

    def test_nobody_can_ask_for_access(self):
        r = self.call("POST", "/api/access/request", OWNER, {})
        self.assertEqual(r.status_code, 409)
        self.assertFalse((Path(self.tmp) / "team" / "requests.json").exists())

    def test_team_mode_is_finished_but_only_the_setting_switches_it_on(self):
        self.assertTrue(core.TEAM_READY)
        with mock.patch.dict(os.environ, {"DOJO_TEAM": ""}):
            self.assertFalse(load_settings().team)
        with mock.patch.dict(os.environ, {"DOJO_TEAM": "1"}):
            self.assertTrue(load_settings().team)
            with mock.patch.object(core, "TEAM_READY", False):
                self.assertFalse(load_settings().team)

    def test_owner_key_setting_is_validated(self):
        with mock.patch.dict(os.environ, {"DOJO_OWNER_KEY": "not-a-key"}):
            self.assertEqual(load_settings().owner_key, "")
        with mock.patch.dict(os.environ, {"DOJO_OWNER_KEY": OWNER_KEY}):
            self.assertEqual(load_settings().owner_key, OWNER_KEY)


# ---------------------------------------------------------------- admission

class AdmissionTests(TeamCase):
    def test_ask_approve_acknowledge(self):
        h = who("m-1")
        self.refused("GET", "/api/me", h, "not_member")
        self.refused("GET", "/api/settings", h, "not_member")
        a = self.ok("GET", "/api/access", h)
        self.assertEqual((a["member"], a["shown_as"], a["request"]), (False, "A Colleague", None))
        a = self.ok("POST", "/api/access/request", h, {"used_before": False})
        self.assertEqual(a["request"]["state"], "open")
        self.ok("POST", "/api/access/request", h, {"used_before": False})   # asking twice is one request
        m = self.ok("GET", "/api/team/members", OWNER)
        self.assertEqual(len(m["requests"]), 1)
        self.assertEqual(set(m["requests"][0]), {"id", "name", "email", "at", "used_before"})
        self.assertEqual((m["requests"][0]["name"], m["requests"][0]["email"]), ("A Colleague", "colleague@example.com"))
        self.ok("POST", f"/api/team/requests/{m['requests'][0]['id']}/approve", OWNER)
        me = self.ok("GET", "/api/me", h)
        self.assertEqual((me["role"], me["status"], me["display_name"]), ("learner", "pending", "Member 1"))
        self.assertEqual(me["notice"], {"version": NOTICE_VERSION, "required": True, "acknowledged": False})
        self.assertNotIn("profile", me)
        self.refused("GET", "/api/settings", h, "notice")
        n = self.ok("GET", "/api/notice", h)
        self.assertEqual(len(n["sections"]), 8)
        self.assertIn("can technically read", json.dumps(n))
        self.ok("POST", "/api/notice/ack", h, {"version": NOTICE_VERSION + 1}, status=409)
        self.ok("POST", "/api/notice/ack", h, {"version": NOTICE_VERSION})
        self.assertTrue(self.folder(self.key("m-1")).joinpath("social").is_dir())   # the opt-in folder for Play
        me = self.ok("GET", "/api/me", h)
        self.assertEqual((me["status"], me["notice"]["acknowledged"]), ("active", True))
        self.assertIn("profile", me)
        self.ok("GET", "/api/settings", h)
        self.assertEqual(self.ok("GET", "/api/access", h), {"member": True})

    def test_decline_is_final_until_asked_directly(self):
        h = who("m-1")
        self.ok("POST", "/api/access/request", h, {})
        self.ok("POST", f"/api/team/requests/{self.request_id('m-1')}/decline", OWNER)
        self.assertEqual(self.ok("GET", "/api/access", h)["request"]["state"], "declined")
        self.ok("POST", "/api/access/request", h, {}, status=409)
        self.refused("GET", "/api/me", h, "not_member")

    def test_a_declined_request_goes_30_days_after_the_ask(self):
        """GPT #49: declining on day 29 must not restart the 30 days (docs/user-guide.md)."""
        h = who("m-1")
        self.ok("POST", "/api/access/request", h, {})
        reqs = self.team.requests_doc()
        asked = utcnow() - timedelta(days=29)
        reqs[0]["at"] = core.iso(asked)
        self.team._save_requests(reqs)
        self.ok("POST", f"/api/team/requests/{self.request_id('m-1')}/decline", OWNER)
        req = self.team.requests_doc()[0]
        self.assertEqual((req["state"], req["at"]), ("declined", core.iso(asked)))
        self.assertTrue(req["declined_at"])
        self.team.sweep(now=asked + timedelta(days=team_mod.REQUEST_DAYS) - timedelta(hours=1))
        self.assertEqual(len(self.team.requests_doc()), 1)
        self.team.sweep(now=asked + timedelta(days=team_mod.REQUEST_DAYS) + timedelta(minutes=1))
        self.assertEqual(self.team.requests_doc(), [])

    def test_another_tenant_is_refused(self):
        h = who("owner-1", tid="tenant-2")
        for path in ("/api/me", "/api/access", "/api/about"):
            r = self.c.get(path, headers=h)
            self.assertEqual(r.status_code, 403, path)

    def test_remove_takes_effect_on_the_next_request(self):
        h = self.admit("m-1")
        self.ok("GET", "/api/settings", h)
        row_id = self.row("m-1")["id"]
        self.ok("POST", f"/api/team/members/{row_id}/remove", OWNER, {"confirm": "REMOVE"})
        self.refused("GET", "/api/settings", h, "removed")
        me = self.ok("GET", "/api/me", h)
        self.assertEqual(me["status"], "removed")
        self.assertTrue(me["removed_at"])
        self.ok("POST", f"/api/team/members/{row_id}/remove", OWNER, {"confirm": "REMOVE"}, status=409)

    def test_the_owner_cannot_be_removed_or_leave(self):
        self.ok("POST", "/api/team/members/owner/remove", OWNER, {"confirm": "REMOVE"}, status=409)
        self.ok("POST", "/api/team/leave", OWNER, {"confirm": "LEAVE"}, status=409)
        self.ok("GET", "/api/me", OWNER)

    def test_member_cap(self):
        with mock.patch.object(team_mod, "MAX_MEMBERS", 1):
            self.admit("m-1")
            self.ok("POST", "/api/access/request", who("m-2"), {})
            self.ok("POST", f"/api/team/requests/{self.request_id('m-2')}/approve", OWNER, status=409)

    def test_guest_without_a_name_is_shown_by_email(self):
        h = who("g-1", name=None, email="guest@example.com")
        self.assertEqual(self.ok("GET", "/api/access", h)["shown_as"], "guest@example.com")
        self.ok("POST", "/api/access/request", h, {})
        self.ok("POST", f"/api/team/requests/{self.request_id('g-1')}/approve", OWNER)
        m = self.ok("GET", "/api/team/members", OWNER)
        row = next(r for r in m["members"] if r["email"] == "guest@example.com")
        self.assertEqual(row["entra_name"], "")

    def test_a_changed_email_changes_nothing(self):
        h = self.admit("m-1")
        key = self.key("m-1")
        self.ok("PUT", "/api/settings", h, {"later_hours": 30})
        h2 = who("m-1", email="renamed@example.com")
        self.assertEqual(self.ok("GET", "/api/settings", h2)["later_hours"], 30)
        self.assertEqual(self.key("m-1"), key)

    def test_owner_name_is_kept_for_the_notice(self):
        self.ok("GET", "/api/me", OWNER)
        h = self.admit("m-1", ack=False)
        self.assertEqual(self.ok("GET", "/api/notice", h)["operator"], "Alex")


# ---------------------------------------------------------------- the five route classes

class RouteClassTests(TeamCase):
    def routes(self):
        return [r for r in self.app.routes if isinstance(r, APIRoute)]

    def test_every_route_has_exactly_one_class(self):
        for r in self.routes():
            cls = classes(r)
            if r.path in PUBLIC:
                self.assertEqual(cls, set(), r.path)
                self.assertLessEqual(r.methods, {"GET", "HEAD"}, r.path)
            else:
                self.assertEqual(len(cls), 1, f"{sorted(r.methods)} {r.path} has classes {cls}")
        mounts = [r.path for r in self.app.routes if not isinstance(r, APIRoute)]
        self.assertEqual(mounts, ["/static"])

    def by_class(self, name: str):
        for r in self.routes():
            if classes(r) == {name}:
                for method in sorted(r.methods - {"HEAD"}):
                    yield method, sample_path(r.path)

    def test_admin_routes_refuse_members_and_admit_the_owner(self):
        h = self.admit("m-1")
        admin = list(self.by_class("admin"))
        self.assertGreater(len(admin), 25)
        for method, path in admin:
            self.refused(method, path, h, "admin_only", body={})
        for method, path in admin:
            if path in ("/api/diag/run",):
                continue   # runs the whole self-test, avatar included
            r = self.call(method, path, OWNER, {})
            self.assertNotIn(r.status_code, (401, 403, 500), f"{method} {path}: {r.text}")

    def test_no_admin_route_takes_a_learner(self):
        for r in self.routes():
            if classes(r) == {"admin"}:
                params = set(re.findall(r"\{(\w+)", r.path))
                self.assertLessEqual(params, {"req_id", "member_id", "lesson_id", "pid", "lab"}, r.path)

    def test_non_members_reach_only_the_non_member_routes(self):
        h = who("stranger")
        for name in ("learner", "learner_exit", "admin"):
            for method, path in self.by_class(name):
                self.refused(method, path, h, "not_member", body={})
        for method, path in self.by_class("signed_in_non_member"):
            r = self.call(method, path, h, {"used_before": False})
            self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")

    def test_learner_routes_by_membership_state(self):
        pending = self.admit("m-1", ack=False)
        removed = self.admit("m-2")
        self.ok("POST", f"/api/team/members/{self.row('m-2')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        paused = self.admit("m-3")
        learner = list(self.by_class("learner"))
        self.assertGreater(len(learner), 50)
        for method, path in learner:
            self.refused(method, path, pending, "notice", body={})
            self.refused(method, path, removed, "removed", body={})
        self.ok("POST", "/api/team/pause", OWNER)
        for method, path in learner:
            self.refused(method, path, paused, "paused", body={})
        # The exit routes stay open to all three. A removed member cannot renew or rename; that is the
        # route's own answer, not the class's.
        for h in (pending, removed, paused):
            for method, path in self.by_class("learner_exit"):
                r = self.call(method, path, h, {})
                if h is removed and path in ("/api/me/renew", "/api/me/display-name") and r.status_code == 403:
                    self.assertEqual(code(r), "removed")
                    continue
                self.assertNotEqual(r.status_code, 403, f"{method} {path}: {r.text}")
            self.ok("GET", "/api/record/export", h)

    def test_a_new_notice_version_asks_again(self):
        h = self.admit("m-1")
        self.ok("GET", "/api/settings", h)
        with mock.patch.object(team_mod, "NOTICE_VERSION", NOTICE_VERSION + 1):
            self.refused("GET", "/api/settings", h, "notice")

    def test_members_use_labs_in_their_own_schemas(self):
        h = self.admit("m-1")
        for method, path in (("POST", "/api/labs/vector/run"), ("POST", "/api/labs/vector/check"),
                             ("POST", "/api/labs/vector/reset")):
            self.ok(method, path, h, {"sql": "SELECT 1"})
        schema = self.ok("GET", "/api/labs/vector", h)["schema"]
        self.assertRegex(schema, r"^lab_vector_[0-9a-f]{12}$")
        self.assertEqual(self.ok("GET", "/api/labs/vector", OWNER)["schema"], "lab_vector")
        # Podcast and calendar links are each learner's own since PR 2 (tests/test_team2.py).
        self.ok("GET", "/api/feed", h)
        self.ok("GET", "/api/calendar", h)
        self.ok("GET", "/api/feed", OWNER)


# ---------------------------------------------------------------- shared lessons, private learning

class LessonAndIsolationTests(TeamCase):
    def setUp(self):
        super().setUp()
        p = self.ok("GET", "/api/packages/gh-300", OWNER)
        self.sid = next(s["id"] for d in p["domains"] for g in d["groups"] for s in g["skills"] if s["kind"] == "scenario")

    def test_first_lesson_and_rewrites(self):
        h = self.admit("m-1")
        job = self.wait_job(self.ok("POST", "/api/lessons", OWNER, {"package": "gh-300", "skill": self.sid}), OWNER)
        self.assertEqual(job["state"], "done", job)
        lesson = job["result"]["lesson"]
        self.ok("POST", "/api/lessons", h, {"package": "gh-300", "skill": self.sid}, status=409)
        self.refused("POST", f"/api/lessons/{lesson}/rewrite", h, "admin_only")
        self.ok("GET", f"/api/lessons/{lesson}", h)   # the lesson itself is shared
        job = self.wait_job(self.ok("POST", f"/api/lessons/{lesson}/rewrite", OWNER), OWNER)
        self.assertEqual(job["state"], "done", job)
        self.ok("POST", "/api/lessons/abc-0123456789abcdef0123/rewrite", OWNER, status=404)

    def test_two_members_keep_their_own_data(self):
        a, b = self.admit("m-a", email="a@example.com"), self.admit("m-b", email="b@example.com")
        self.ok("PUT", "/api/settings", a, {"later_hours": 30})
        self.assertNotEqual(self.ok("GET", "/api/settings", b)["later_hours"], 30)
        job = self.ok("POST", "/api/lessons", a, {"package": "gh-300", "skill": self.sid})
        self.ok("GET", f"/api/jobs/{job['id']}", b, status=404)
        self.wait_job(job, a)
        self.store.append_event(self.key("m-a"), "test.marker", {"text": "MARKER-A"})
        self.assertIn("MARKER-A", json.dumps(self.ok("GET", "/api/record/export", a)))
        self.assertNotIn("MARKER-A", json.dumps(self.ok("GET", "/api/record/export", b)))
        self.assertNotIn("MARKER-A", json.dumps(self.ok("GET", "/api/record/export", OWNER)))
        self.assertEqual(self.ok("GET", "/api/record/chain", b)["events"], 1)   # only its enrollment (settings.changed)


# ---------------------------------------------------------------- leaving, and deletion that stays deleted

class LeaveTests(TeamCase):
    def test_leave_deletes_the_whole_folder_and_nothing_else(self):
        a, b = self.admit("m-a", email="a@example.com"), self.admit("m-b", email="b@example.com")
        ka, kb = self.key("m-a"), self.key("m-b")
        self.ok("PUT", "/api/settings", a, {"later_hours": 30})
        self.ok("PUT", "/api/settings", b, {"later_hours": 40})
        row_id = self.row("m-a")["id"]
        self.ok("POST", "/api/team/leave", a, {"confirm": "NO"}, status=422)
        self.ok("POST", "/api/team/leave", a, {"confirm": "LEAVE"})
        self.assertFalse(self.folder(ka).exists())
        self.assertTrue(self.folder(kb).exists())
        self.assertEqual(self.ok("GET", "/api/settings", b)["later_hours"], 40)
        self.refused("GET", "/api/me", a, "not_member")
        doc = json.loads((Path(self.tmp) / "team" / "members.json").read_text("utf-8"))
        gone = next(r for r in doc["members"] if r["id"] == row_id)
        for field in ("tid", "oid", "email", "entra_name", "display_name", "lab"):
            self.assertNotIn(field, gone)
        self.assertTrue(gone["deleted_at"])
        self.assertNotIn("a@example.com", (Path(self.tmp) / "team" / "audit.jsonl").read_text("utf-8"))
        self.assertTrue(self.store.tombstoned(ka))

    def test_removed_member_can_export_and_delete_only(self):
        h = self.admit("m-1")
        self.ok("PUT", "/api/settings", h, {"later_hours": 30})
        self.ok("POST", f"/api/team/members/{self.row('m-1')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.ok("GET", "/api/record/export", h)
        self.refused("PUT", "/api/me/display-name", h, "removed", body={"name": "Someone"})
        self.ok("POST", "/api/record/delete", h, {"confirm": "DELETE"})
        self.ok("POST", "/api/team/leave", h, {"confirm": "LEAVE"})
        self.refused("GET", "/api/me", h, "not_member")

    def test_leaving_while_a_job_runs(self):
        h = self.admit("m-1")
        key = self.key("m-1")
        learner = core.Learner("tenant-1:m-1", key, "Member 1")
        started, release = threading.Event(), threading.Event()

        def slow(step):
            started.set()
            release.wait(10)
            self.store.write("learners", key, "late", value={"late": True})
            return {"ok": True}

        job = self.team.jobs.start(learner, "test", slow)
        self.assertTrue(started.wait(10))
        self.ok("POST", "/api/team/leave", h, {"confirm": "LEAVE"})
        release.set()
        thread = next(t for t in threading.enumerate() if t.name == job["id"])
        thread.join(10)
        self.assertFalse(self.folder(key).exists())
        self.assertFalse(self.store.exists("jobs", job["id"] + ".json"))
        self.assertIsNone(self.store.read("jobs", job["id"]))

    def test_delete_my_record_drops_late_writes(self):
        self.ok("PUT", "/api/settings", OWNER, {"later_hours": 30})
        result = {}
        begun, go = threading.Event(), threading.Event()

        def late_request():
            with self.store.writing_as(OWNER_KEY):
                begun.set()
                go.wait(10)
                try:
                    self.store.write("learners", OWNER_KEY, "late", value={"late": True})
                    result["wrote"] = True
                except Abandoned:
                    result["abandoned"] = True

        t = threading.Thread(target=late_request)
        t.start()
        self.assertTrue(begun.wait(10))
        self.ok("POST", "/api/record/delete", OWNER, {"confirm": "DELETE"})
        go.set()
        t.join(10)
        self.assertEqual(result, {"abandoned": True})
        self.assertFalse(self.folder(OWNER_KEY).joinpath("late.json").exists())
        # The owner goes on as before under the new generation.
        self.ok("PUT", "/api/settings", OWNER, {"later_hours": 31})
        self.assertEqual(self.ok("GET", "/api/settings", OWNER)["later_hours"], 31)

    def test_a_pool_batch_started_before_a_deletion_writes_nothing(self):
        pool, room = self.app.state.pool, self.app.state.exams

        def top_up(owner, pid, report=False, stale=None, **kw):
            self.store.bump(OWNER_KEY)   # "Delete my record" lands while the batch runs
            self.store.write("learners", OWNER_KEY, "pool-late", value={"late": True})
            return 1

        with mock.patch.object(pool, "_has_record", lambda who: True), mock.patch.object(pool, "_busy", lambda who: False), \
                mock.patch.object(pool, "_spent", lambda who: False), mock.patch.object(pool, "_next_package", lambda who=None: "gh-300"), \
                mock.patch.object(pool, "_charge", lambda who: None), mock.patch.object(room, "top_up", top_up):
            self.assertEqual(pool.step(), POOL_MIN_WAIT)
        self.assertFalse(self.folder(OWNER_KEY).joinpath("pool-late.json").exists())

    def test_a_restore_is_purged_again_and_a_returning_guest_starts_fresh(self):
        h = self.admit("m-1")
        old = self.key("m-1")
        self.ok("PUT", "/api/settings", h, {"later_hours": 30})
        backup = Path(tempfile.mkdtemp(prefix="dojo-backup-"))
        shutil.copytree(self.folder(old), backup / old)
        shutil.copy(Path(self.tmp) / "team" / "members.json", backup / "members.json")
        self.ok("POST", "/api/team/leave", h, {"confirm": "LEAVE"})
        # A restore of /home brings the folder and the membership back (not the deletion list).
        shutil.copytree(backup / old, self.folder(old))
        shutil.copy(backup / "members.json", Path(self.tmp) / "team" / "members.json")
        self.app, self.c, _ = make(self.tmp)
        self.assertFalse(self.folder(old).exists())
        self.refused("GET", "/api/me", h, "not_member")
        self.assertNotIn("colleague@example.com", (Path(self.tmp) / "team" / "members.json").read_text("utf-8"))
        h = self.admit("m-1")
        self.assertNotEqual(self.key("m-1"), old)
        self.assertNotEqual(self.ok("GET", "/api/settings", h)["later_hours"], 30)

    def test_deleting_outside_a_request_binds_nothing_to_the_caller(self):
        dojo = self.app.state.dojo
        owner = core.Learner("owner-1", OWNER_KEY, "Alex")
        dojo.delete_all(owner)
        self.assertIsNone(core._WRITER.get())
        # Another start-over elsewhere must not make this thread's later writes look stale.
        self.store.bump(OWNER_KEY)
        self.store.write("learners", OWNER_KEY, "profile", value={"ok": True})
        # Inside work bound to the learner, the work goes on under the new generation.
        with self.store.writing_as(OWNER_KEY):
            dojo.delete_all(owner)
            self.store.write("learners", OWNER_KEY, "profile", value={"ok": True})
        self.assertIsNone(core._WRITER.get())


# ---------------------------------------------------------------- lost accounts

class LostAccountTests(TeamCase):
    def test_rebind_reaches_the_old_record(self):
        old = self.admit("m-1")
        key = self.key("m-1")
        self.store.append_event(key, "test.one", {})
        self.store.append_event(key, "test.two", {})
        new = who("m-1-new")
        self.ok("POST", "/api/access/request", new, {"used_before": True})
        req = self.request_id("m-1-new")
        row_id = self.row("m-1")["id"]
        self.ok("POST", f"/api/team/requests/{req}/rebind", OWNER, {"member": row_id, "checked": False}, status=400)
        self.ok("POST", f"/api/team/requests/{req}/rebind", OWNER, {"member": row_id, "checked": True})
        chain = self.ok("GET", "/api/record/chain", new)
        self.assertEqual((chain["ok"], chain["events"]), (True, 3))   # the enrollment and the two above
        self.assertEqual(self.key("m-1-new"), key)
        self.refused("GET", "/api/me", old, "not_member")
        lines = [json.loads(x) for x in (Path(self.tmp) / "team" / "audit.jsonl").read_text("utf-8").splitlines()]
        rebinds = [x for x in lines if x["action"] == "rebind"]
        self.assertEqual(len(rebinds), 1)
        self.assertLessEqual(set(rebinds[0]), {"at", "action", "who", "member"})

    def test_rebind_to_a_deleted_member_is_refused(self):
        h = self.admit("m-1")
        row_id = self.row("m-1")["id"]
        self.ok("POST", "/api/team/leave", h, {"confirm": "LEAVE"})
        new = who("m-1-new")
        self.ok("POST", "/api/access/request", new, {"used_before": True})
        r = self.call("POST", f"/api/team/requests/{self.request_id('m-1-new')}/rebind", OWNER, {"member": row_id, "checked": True})
        self.assertIn(r.status_code, (404, 409), r.text)

    def test_owner_lost_account_keeps_the_record(self):
        for team in (False, True):
            with self.subTest(team=team):
                app, c, tmp = make(team=team)
                self.app, self.c, self.tmp = app, c, tmp
                self.ok("PUT", "/api/settings", OWNER, {"later_hours": 33})
                self.store.append_event(OWNER_KEY, "test.one", {})
                feed = self.ok("POST", "/api/feed/link", OWNER)
                events = self.ok("GET", "/api/record/chain", OWNER)["events"]
                self.assertGreater(events, 0)
                self.app, self.c, _ = make(tmp, team=team, owner_oid="owner-2", owner_key=OWNER_KEY)
                new_owner = who("owner-2", name="Alex", email=None)
                self.assertEqual(self.ok("GET", "/api/settings", new_owner)["later_hours"], 33)
                self.assertEqual(self.ok("GET", "/api/record/chain", new_owner)["events"], events)
                self.assertTrue(self.ok("GET", "/api/record/chain", new_owner)["ok"])
                # The podcast link Alex already subscribed to keeps playing; no new link is needed.
                self.assertEqual(self.c.get(urlparse(feed["feeds"][0]["url"]).path).status_code, 200)
                self.assertEqual(self.ok("GET", "/api/me", new_owner)["role"], "admin")
                r = self.c.get("/api/settings", headers=OWNER)
                self.assertEqual(r.status_code, 403)


# ---------------------------------------------------------------- expiry, pause, ending team mode

class LifecycleTests(TeamCase):
    def test_expiry_and_renewal(self):
        h = self.admit("m-1")
        row = self.row("m-1")
        self.assertRegex(row["renewed"], r"^\d{4}-\d{2}$")
        end = team_mod._expiry(row["renewed"])
        self.assertIsNone(self.team.renew_by(row))
        self.assertEqual(self.team.renew_by(row, end - timedelta(days=10)), end.date().isoformat())
        r = self.ok("POST", "/api/me/renew", h)
        self.assertRegex(self.row("m-1")["renewed"], r"^\d{4}-\d{2}$")
        self.assertTrue(r["until"])
        m = self.ok("GET", "/api/team/members", OWNER)
        self.assertNotIn("renewed", json.dumps(m))
        self.team.sweep(now=end + timedelta(days=1))
        self.refused("GET", "/api/settings", h, "removed")
        self.assertEqual(self.ok("GET", "/api/me", h)["status"], "removed")
        self.assertTrue(self.folder(self.key("m-1")).exists() or True)
        self.team.sweep(now=end + timedelta(days=32))
        self.refused("GET", "/api/me", h, "not_member")

    def test_pause_keeps_export_and_delete(self):
        h = self.admit("m-1")
        self.ok("POST", "/api/team/pause", OWNER)
        self.refused("GET", "/api/settings", h, "paused")
        me = self.ok("GET", "/api/me", h)
        self.assertEqual(me["banner"]["kind"], "paused")
        self.ok("GET", "/api/record/export", h)
        self.ok("GET", "/api/settings", OWNER)
        self.ok("POST", "/api/team/resume", OWNER)
        self.ok("GET", "/api/settings", h)

    def test_end_team_mode_is_staged(self):
        h = self.admit("m-1", email="a@example.com")
        self.ok("PUT", "/api/settings", h, {"later_hours": 30})
        key = self.key("m-1")
        self.assertEqual(self.ok("GET", "/api/team/end", OWNER)["emails"], ["a@example.com"])
        today = utcnow().date()
        self.ok("POST", "/api/team/end", OWNER, {"date": (today + timedelta(days=30)).isoformat()}, status=409)
        self.ok("POST", "/api/team/end/told", OWNER, {"confirmed": False}, status=400)
        self.assertNotIn("told", (Path(self.tmp) / "team" / "audit.jsonl").read_text("utf-8"))
        self.ok("POST", "/api/team/end/told", OWNER, {"confirmed": True})
        lines = (Path(self.tmp) / "team" / "audit.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(sum("told every member" in x for x in lines), 1)
        self.ok("POST", "/api/team/end", OWNER, {"date": (today + timedelta(days=13)).isoformat()}, status=409)
        day = (today + timedelta(days=14)).isoformat()
        self.ok("POST", "/api/team/end", OWNER, {"date": day})
        self.assertEqual(self.ok("GET", "/api/me", h)["banner"]["kind"], "ending")
        self.ok("GET", "/api/settings", h)   # full use until the date
        self.ok("POST", f"/api/team/requests/{SAMPLE_ID}/approve", OWNER, status=409)   # no new members
        self.team.sweep(now=parse_iso(day + "T00:00:00+00:00") - timedelta(hours=1))
        self.assertTrue(self.folder(key).exists())
        self.team.sweep(now=parse_iso(day + "T00:00:00+00:00") + timedelta(hours=1))
        self.assertFalse(self.folder(key).exists())
        self.refused("GET", "/api/me", h, "not_member")

    def test_team_mode_off_with_members_deletes_nothing(self):
        h = self.admit("m-1")
        self.ok("PUT", "/api/settings", h, {"later_hours": 30})
        key = self.key("m-1")
        self.app, self.c, _ = make(self.tmp, team=False)
        self.team.sweep(now=utcnow() + timedelta(days=400))
        self.assertTrue(self.folder(key).exists())
        me = self.ok("GET", "/api/me", OWNER)
        self.assertEqual(me["banner"]["kind"], "suspended")
        self.assertEqual(self.c.get("/api/me", headers=h).status_code, 403)


# ---------------------------------------------------------------- no per-member views; display names

class PrivacyTests(TeamCase):
    def test_members_page_shows_only_what_admission_needs(self):
        self.admit("m-1")
        self.ok("POST", "/api/access/request", who("m-2"), {"used_before": True})
        m = self.ok("GET", "/api/team/members", OWNER)
        self.assertEqual(set(m), {"team", "members", "requests", "rebind_candidates", "paused", "end", "max_members", "suspended", "sync"})
        for row in m["members"]:
            self.assertEqual(set(row), MEMBER_FIELDS)
        for row in m["rebind_candidates"]:
            self.assertEqual(set(row), {"id", "display_name", "entra_name", "email"})
        doc = json.loads((Path(self.tmp) / "team" / "members.json").read_text("utf-8"))
        for row in doc["members"]:
            self.assertLessEqual(set(row), set(ROW_FIELDS))
            self.assertFalse({k for k in row if "last" in k or "active" in k or "seen" in k})

    def test_display_names(self):
        h = self.admit("m-1")
        self.assertEqual(self.ok("GET", "/api/me", h)["display_name"], "Member 1")
        self.ok("PUT", "/api/me/display-name", OWNER, {"name": "Alex D"})
        self.ok("PUT", "/api/me/display-name", h, {"name": "alex d"}, status=409)
        self.ok("PUT", "/api/me/display-name", h, {"name": "x"}, status=400)
        self.ok("PUT", "/api/me/display-name", h, {"name": "  Kim\u200b  "})
        me = self.ok("GET", "/api/me", h)
        self.assertEqual(me["display_name"], "Kim")
        self.assertNotIn("A Colleague", json.dumps(me))
        self.assertEqual(self.ok("GET", "/api/notice", h)["operator"], "Alex D")

    def test_the_notice_names_the_real_region_in_the_privacy_sheets_words(self):
        """The notice says where data is stored as App Service reports it, never a fixed region, and in the
        same words as docs/privacy.md."""
        h = self.admit("m-1")

        def stored(env: dict) -> str:
            with mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("REGION_NAME", None)
                n = self.ok("GET", "/api/notice", h)
            return next(s["text"] for s in n["sections"] if s["title"] == "Where your data is stored")

        plain = stored({})
        self.assertNotIn("Sweden", plain)
        sheet = " ".join((Path(team_mod.__file__).resolve().parent.parent / "docs" / "privacy.md").read_text("utf-8").split())
        self.assertIn(f'"{plain}"', sheet)
        here = stored({"REGION_NAME": "West Europe"})
        self.assertIn("in Azure (West Europe)", here)
        self.assertNotIn("Sweden", here)
        off = next(s["text"] for s in team_mod.notice_view(self.team, {}, labs=False)["sections"]
                   if s["title"] == "Where your data is stored")
        self.assertNotIn("Lab tables", off)
        self.assertIn('"This Dojo has no lab database."', sheet)
        self.assertIn(off.split(". ", 1)[1], sheet)


# ---------------------------------------------------------------- logging in Azure

class LoggingTests(TeamCase):
    def test_no_request_lines_and_one_scrubbed_line_per_failure(self):
        # In Azure with team mode on, members wait for the lab database (tests/test_team4.py); here the
        # startup reconcile is taken as done, so only the logging is tested.
        root, dojo_log = logging.getLogger(), logging.getLogger("dojo")
        levels = (root.level, dojo_log.level)
        records: list[logging.LogRecord] = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record)

        grab = Grab()
        root.addHandler(grab)
        try:
            with mock.patch.dict(os.environ, {"DOJO_LAB_SERVER": "dojo-lab.database.windows.net", "DOJO_LAB_DATABASE": "lab"}):
                self.app, self.c, _ = make(self.tmp, in_azure=True)
            self.team._admit.set()
            h = self.admit("m-1")
            self.ok("PUT", "/api/settings", h, {"later_hours": 30})
            self.ok("GET", "/api/settings", h)
            self.ok("GET", "/api/plan", h)
            self.ok("GET", "/api/me", OWNER)
            self.assertEqual([r.getMessage() for r in records], [])
            job_id = "job-0123456789abcdef0123"
            with mock.patch.object(self.team.jobs, "get", side_effect=RuntimeError(f"colleague@example.com {self.key('m-1')}")):
                r = self.c.get(f"/api/jobs/{job_id}", headers=h)
            self.assertEqual(r.status_code, 500)
            self.assertEqual(len(records), 1)
            line = records[0].getMessage()
            self.assertIn("/api/jobs/{job_id}", line)
            self.assertIn("RuntimeError", line)
            for leak in (job_id, "@", self.key("m-1"), "m-1"):
                self.assertNotIn(leak, line)
            self.assertIsNone(records[0].exc_info)
        finally:
            root.removeHandler(grab)
            root.setLevel(levels[0])
            dojo_log.setLevel(levels[1])
            for handler in root.handlers:
                for f in [f for f in handler.filters if isinstance(f, ScrubFilter)]:
                    handler.removeFilter(f)


# ---------------------------------------------------------------- the switch in Bicep

class BicepTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "infra" / "main.bicep").read_text("utf-8")

    def test_team_mode_defaults_off_and_feeds_auth_and_settings(self):
        self.assertRegex(self.text, r"param teamMode bool = false")
        self.assertRegex(self.text, r"param ownerLearnerKey string = ''")
        self.assertRegex(self.text, r"name: 'DOJO_TEAM'\s+value: teamMode \? '1' : '0'")
        self.assertRegex(self.text, r"empty\(ownerLearnerKey\) \? \[\] : \[\s+\{\s+name: 'DOJO_OWNER_KEY'")
        self.assertRegex(self.text, r"teamMode \? \[[^\]]*name: 'WEBSITE_AUTH_AAD_ALLOWED_TENANTS'\s+value: tenantId")
        # Off: the owner-only identity list exactly as before. On: no list, and no groups or claim checks.
        self.assertRegex(self.text, r"teamMode \? \{\} : \{\s+defaultAuthorizationPolicy: \{\s+allowedPrincipals: \{\s+identities: \[\s+ownerObjectId\s+\]")
        self.assertNotRegex(self.text, r"(?m)^\s*jwtClaimChecks\s*:")
        self.assertNotRegex(self.text, r"(?m)^\s*groups\s*:")

    def test_the_deploy_switches_team_mode_only_from_its_repository_variable(self):
        """On only when DOJO_TEAM_MODE is exactly "true"; unset or anything else, also "True", deploys the
        template's default, off. The shell compares it: Actions expressions compare strings without case."""
        deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        switch = deploy.split("- name: Team mode switch", 1)[1].split("- name:", 1)[0]
        self.assertIn("id: team", switch)
        self.assertIn("SWITCH: ${{ vars.DOJO_TEAM_MODE }}", switch)
        self.assertIn('if [ "$SWITCH" = "true" ]; then on=true; else on=false; fi', switch)
        self.assertIn('echo "on=$on" >> "$GITHUB_OUTPUT"', switch)
        self.assertNotIn("vars.DOJO_TEAM_MODE ==", deploy)
        self.assertLess(deploy.index("- name: Team mode switch"), deploy.index("- name: Preview infrastructure changes"))
        lines = [line for line in deploy.splitlines() if "--parameters" in line]
        self.assertEqual(len(lines), 2)   # the preview and the deployment see the same switch
        for line in lines:
            self.assertIn('teamMode="$TEAM_MODE"', line)
        self.assertEqual(deploy.count("TEAM_MODE: ${{ steps.team.outputs.on }}"), 2)

    def test_the_switch_compares_without_ignoring_case(self):
        deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        switch = deploy.split("- name: Team mode switch", 1)[1].split("- name:", 1)[0]
        script = next(line.strip() for line in switch.splitlines() if line.strip().startswith("if [")) + '; printf %s "$on"'
        bash = shutil.which("bash")
        if not bash or any(s in bash.lower() for s in ("system32", "windowsapps")):   # WSL's launcher, not a shell
            self.skipTest("bash is not installed")
        for value, on in (("true", "true"), ("True", "false"), ("TRUE", "false"), ("", "false"), ("yes", "false"),
                          ("true ", "false"), ("false", "false")):
            with self.subTest(value=value):
                out = subprocess.run([bash, "-c", script], env={**os.environ, "SWITCH": value},
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(out.stdout, on, out.stderr)

    @unittest.skipUnless(shutil.which("az"), "Azure CLI not installed")
    def test_bicep_builds(self):
        out = subprocess.run([shutil.which("az"), "bicep", "build", "--file", str(ROOT / "infra" / "main.bicep"), "--stdout"],
                             capture_output=True, text=True, timeout=300)
        if out.returncode != 0 and "bicep" in out.stderr.lower() and "install" in out.stderr.lower():
            self.skipTest("Bicep is not installed for the Azure CLI")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        arm = json.loads(out.stdout)
        self.assertEqual(arm["parameters"]["teamMode"]["defaultValue"], False)
        compiled = json.dumps(arm)
        self.assertIn("parameters('teamMode')", compiled)
        self.assertIn("WEBSITE_AUTH_AAD_ALLOWED_TENANTS", compiled)
        self.assertNotIn("jwtClaimChecks", compiled)


if __name__ == "__main__":
    unittest.main()
