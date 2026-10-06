"""Team Dojo, PR 4 (docs/adr/0009-team-dojo.md): lab isolation per learner and the mirror of the deletion
list, the membership revocations and Pause in the lab database.

The database is a stub here (an in-memory Mirror and FakeRunner); tests/test_labs.py holds the SQL text
tests and, behind DOJO_LAB_LOCALDB_TEST=1, the same rules on a dedicated LocalDB instance."""
import copy
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from app.core import iso, sha256, utcnow
from app.labs import FakeRunner, LabRoom, LabUser, SqlRunner
from app.mirror import Mirror
from app.team import NO_RESTORE
from tests.test_team import OWNER, OWNER_KEY, TeamCase, code, make, who


class StubMirror:
    """The dojo_meta tables in memory. `up` False is an outage (or the free allowance used up)."""
    def __init__(self):
        self.doc = {"epoch": 0, "paused": False, "tombstones": [], "changes": [], "retired": []}
        self.up = True
        self.reads = 0

    def read(self) -> dict:
        if not self.up:
            raise ConnectionError("database unavailable")
        self.reads += 1
        return copy.deepcopy(self.doc)

    def write(self, doc: dict, cutoff: str) -> None:
        if not self.up:
            raise ConnectionError("database unavailable")
        self.doc = {k: copy.deepcopy(doc[k]) for k in ("epoch", "paused", "tombstones", "changes", "retired")}


class MirrorCase(TeamCase):
    def setUp(self):
        super().setUp()
        self.db = StubMirror()
        self.team.attach_mirror(self.db, wait=60)
        self.team.reconcile()   # the startup reconcile, without the thread

    def backup(self) -> Path:
        """A copy of /home as App Service backs it up."""
        to = Path(tempfile.mkdtemp(prefix="dojo-home-")) / "home"
        shutil.copytree(self.tmp, to)
        return to

    def restore(self, backup: Path, reconcile: bool = True) -> None:
        """/home goes back to the backup; the lab database does not. Then the app starts again."""
        shutil.rmtree(self.tmp)
        shutil.copytree(backup, self.tmp)
        self.app, self.c, _ = make(self.tmp)
        self.team.attach_mirror(self.db, wait=60)
        if reconcile:
            self.team.reconcile()


# ---------------------------------------------------------------- the startup gate

class StartupTests(MirrorCase):
    def test_members_wait_for_the_reconcile_and_the_owner_does_not(self):
        h = self.admit("m-1")
        self.app, self.c, _ = make(self.tmp)
        self.db.up = False
        self.team.attach_mirror(self.db, wait=60)
        r = self.call("GET", "/api/settings", h)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("retry-after"), "60")
        self.assertEqual(self.call("GET", "/api/me", who("stranger")).status_code, 503)
        self.ok("GET", "/api/settings", OWNER)
        self.assertEqual(self.ok("GET", "/api/team/members", OWNER)["sync"]["state"], "waiting")
        # The database stays away past the wait: members come in from the file, and Studio says so.
        self.team._open_unsynced()
        self.ok("GET", "/api/settings", h)
        sync = self.ok("GET", "/api/team/members", OWNER)["sync"]
        self.assertEqual(sync["state"], "unsynced")
        self.assertIn("Tombstones not synced", sync["text"])
        self.assertEqual(self.ok("GET", "/api/diag", OWNER)["team_sync"]["state"], "unsynced")
        # It answers later: synced, and nothing else changes for members.
        self.db.up = True
        self.team.reconcile()
        self.assertEqual(self.ok("GET", "/api/team/members", OWNER)["sync"]["state"], "synced")
        self.ok("GET", "/api/settings", h)

    def test_the_mirror_thread_admits_members_once_synced(self):
        h = self.admit("m-1")
        self.app, self.c, _ = make(self.tmp)
        self.team.attach_mirror(self.db, wait=30)
        self.team.start()
        for _ in range(200):
            if self.team.admitting():
                break
            time.sleep(0.05)
        self.assertEqual(self.team.sync["state"], "synced")
        self.ok("GET", "/api/settings", h)
        # A change wakes the thread; nothing else does (the database is never woken on a timer).
        reads = self.db.reads
        self.ok("POST", f"/api/team/members/{self.row('m-1')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        for _ in range(200):
            if self.db.reads > reads and any(c["member"] == sha256(self.key("m-1")) for c in self.db.doc["changes"]):
                break
            time.sleep(0.05)
        self.assertTrue(any(c["member"] == sha256(self.key("m-1")) and c["status"] == "removed"
                            for c in self.db.doc["changes"]))

    def test_azure_wiring_team_on_and_off(self):
        env = {"DOJO_LAB_SERVER": "dojo-lab.database.windows.net", "DOJO_LAB_DATABASE": "lab"}
        with mock.patch.dict(os.environ, env), mock.patch.object(SqlRunner, "connect",
                                                                side_effect=AssertionError("no database at startup")):
            app, c, _ = make(team=False, in_azure=True)
            self.assertIsNone(app.state.team.mirror)
            self.assertTrue(app.state.team.admitting())
            self.assertEqual(c.get("/api/settings", headers=OWNER).status_code, 200)
            app, c, _ = make(team=True, in_azure=True)
            self.assertIsInstance(app.state.team.mirror, Mirror)
            self.assertFalse(app.state.team.admitting())
            self.assertEqual(c.get("/api/settings", headers=OWNER).status_code, 200)
            self.assertEqual(c.get("/api/me", headers=who("m-1")).status_code, 503)


class TokenGateTests(MirrorCase):
    """The feed and calendar links carry no sign-in, so they wait for the startup reconcile like members do."""

    def tokens(self, h) -> tuple[str, str]:
        feed = self.ok("POST", "/api/feed/link/new", h)["feeds"][0]["url"].split("/")[4]
        cal = self.ok("POST", "/api/calendar/link", h)["url"].split("/")[4]
        return feed, cal

    def status(self, feed: str, cal: str) -> tuple[int, int]:
        return self.c.get(f"/feed/{feed}/gh-300.xml").status_code, self.c.get(f"/cal/{cal}/dojo.ics").status_code

    def test_member_links_wait_for_the_reconcile_and_the_owners_do_not(self):
        h = self.admit("m-1")
        member, owner = self.tokens(h), self.tokens(OWNER)
        self.assertEqual(self.status(*member), (200, 200))
        backup = self.backup()
        self.ok("POST", f"/api/team/members/{self.row('m-1')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.team.reconcile()
        self.assertEqual(self.status(*member), (404, 404))
        # /home goes back to before the removal; the database still knows it. Its read is slow.
        self.restore(backup, reconcile=False)
        self.assertEqual(self.row("m-1")["status"], "active")
        import threading
        release, reading = threading.Event(), threading.Event()
        real = self.db.read

        def slow():
            reading.set()
            release.wait(10)
            return real()

        with mock.patch.object(self.db, "read", side_effect=slow):
            worker = threading.Thread(target=self.team.reconcile)
            worker.start()
            self.assertTrue(reading.wait(10))
            self.assertEqual(self.call("GET", "/api/me", h).status_code, 503)
            self.assertEqual(self.status(*member), (404, 404), "a restored member link waits for the reconcile")
            self.assertEqual(self.status(*owner), (200, 200), "the owner's links never wait")
            self.assertEqual(self.app.state.podcast.links.learner_for(member[0]), None)
            release.set()
            worker.join(10)
        self.assertEqual(self.row("m-1")["status"], "removed")
        self.assertEqual(self.status(*member), (404, 404))
        self.assertEqual(self.status(*owner), (200, 200))

    def test_a_restored_pause_holds_the_links_and_an_ordinary_restart_lets_them_back(self):
        h = self.admit("m-1")
        member = self.tokens(h)
        backup = self.backup()
        self.ok("POST", "/api/team/pause", OWNER)
        self.team.reconcile()
        self.restore(backup, reconcile=False)
        self.assertFalse(self.team.paused())   # the file is from before the Pause
        self.assertEqual(self.status(*member), (404, 404))
        self.team.reconcile()
        self.assertTrue(self.team.paused())
        self.assertEqual(self.status(*member), (404, 404))
        # An ordinary restart, no Pause: waiting, then served once synced, or after the wait without the database.
        self.ok("POST", "/api/team/resume", OWNER)
        self.app, self.c, _ = make(self.tmp)
        self.team.attach_mirror(self.db, wait=60)
        self.assertEqual(self.status(*member), (404, 404))
        self.assertIsNone(self.team.active_member(self.key("m-1")))
        self.team.reconcile()
        self.assertEqual(self.status(*member), (200, 200))
        self.app, self.c, _ = make(self.tmp)
        self.db.up = False
        self.team.attach_mirror(self.db, wait=60)
        self.assertEqual(self.status(*member), (404, 404))
        self.team._open_unsynced()
        self.assertEqual(self.status(*member), (200, 200))
        self.assertEqual(self.team.active_member(self.key("m-1")).key, self.key("m-1"))


# ---------------------------------------------------------------- restores of /home

class RestoreTests(MirrorCase):
    def test_a_restore_is_purged_before_any_member_is_admitted(self):
        h = self.admit("m-1")
        key = self.key("m-1")
        backup = self.backup()
        self.ok("POST", "/api/team/leave", h, {"confirm": "LEAVE"})
        self.team.reconcile()
        self.assertTrue(any(t["key"] == sha256(key) for t in self.db.doc["tombstones"]))
        self.restore(backup, reconcile=False)
        self.assertTrue(self.folder(key).exists())   # the backup is from before the Leave
        self.assertEqual(self.call("GET", "/api/settings", h).status_code, 503)
        out = self.team.reconcile()
        self.assertTrue(out["restored"])
        self.assertFalse(self.folder(key).exists())
        self.refused("GET", "/api/me", h, "not_member")
        self.assertNotIn("colleague@example.com", (Path(self.tmp) / "team" / "members.json").read_text("utf-8"))
        tombs = json.loads((Path(self.tmp) / "team" / "tombstones.json").read_text("utf-8"))
        self.assertGreaterEqual(tombs["epoch"], self.db.doc["epoch"])

    def test_restored_revocations_for_every_reason(self):
        for reason in ("removed", "expired", "team-ended"):
            with self.subTest(reason=reason):
                oid = f"m-{reason}"
                h = self.admit(oid)
                key = self.key(oid)
                backup = self.backup()
                if reason == "removed":
                    self.ok("POST", f"/api/team/members/{self.row(oid)['id']}/remove", OWNER, {"confirm": "REMOVE"})
                else:
                    with self.store.lock:
                        self.team._set_removed(self.row(oid), reason, utcnow())
                        self.team._save()
                self.team.reconcile()
                self.restore(backup)
                if reason == "team-ended":
                    self.assertFalse(self.folder(key).exists())
                    self.refused("GET", "/api/me", h, "not_member")
                else:
                    self.refused("GET", "/api/settings", h, "removed")
                    self.assertEqual(self.row(oid)["reason"], reason)
                    self.assertEqual(self.row(oid)["removed_at"][:10], iso()[:10])
                    self.assertTrue(self.folder(key).exists())   # the 30 days of grace still run
                    self.ok("GET", "/api/record/export", h)

    def test_the_grace_counts_from_the_original_date(self):
        h = self.admit("m-1")
        key = self.key("m-1")
        backup = self.backup()
        with self.store.lock:
            self.team._set_removed(self.row("m-1"), "removed", utcnow() - timedelta(days=31))
            self.team._save()
        self.team.reconcile()
        self.restore(backup)
        self.assertFalse(self.folder(key).exists())
        self.refused("GET", "/api/me", h, "not_member")

    def test_a_restore_from_before_a_rebind_retires_the_old_account(self):
        old = self.admit("m-1")
        key = self.key("m-1")
        backup = self.backup()
        new = who("m-1-new")
        self.ok("POST", "/api/access/request", new, {"used_before": True})
        self.ok("POST", f"/api/team/requests/{self.request_id('m-1-new')}/rebind", OWNER,
                {"member": self.row("m-1")["id"], "checked": True})
        self.team.reconcile()
        self.ok("GET", "/api/settings", new)   # synced, the rebound account keeps working
        self.assertTrue(self.db.doc["retired"])
        self.restore(backup)
        # The restored row still names the old account, which the database knows was retired.
        self.refused("GET", "/api/settings", old, "removed")
        self.assertTrue(self.folder(key).exists())
        self.refused("GET", "/api/me", new, "not_member")   # asks again; the owner can re-bind in the grace

    def test_a_member_approved_after_the_backup_asks_again(self):
        backup = self.backup()
        h = self.admit("m-1")
        self.team.reconcile()
        self.restore(backup)
        self.refused("GET", "/api/me", h, "not_member")
        self.ok("POST", "/api/access/request", h, {"used_before": False})

    def test_no_identities_in_the_database(self):
        self.admit("m-1", email="someone@example.com", name="Some One")
        h = self.admit("m-2")
        self.ok("POST", f"/api/team/members/{self.row('m-1')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.ok("POST", "/api/team/leave", h, {"confirm": "LEAVE"})
        self.team.reconcile()
        text = json.dumps(self.db.doc)
        for leak in ("m-1", "m-2", "tenant-1", "someone@example.com", "Some One", self.key("m-1"), OWNER_KEY):
            self.assertNotIn(leak, text)


# ---------------------------------------------------------------- Pause and Resume

class PauseTests(MirrorCase):
    def test_resume_needs_the_database_or_the_logged_override(self):
        h = self.admit("m-1")
        self.ok("POST", "/api/team/pause", OWNER)
        self.team.reconcile()
        self.assertTrue(self.db.doc["paused"])
        self.assertTrue(self.ok("GET", "/api/team/members", OWNER)["sync"]["pause_recorded"])
        self.db.up = False
        r = self.call("POST", "/api/team/resume", OWNER, {})
        self.assertEqual((r.status_code, code(r)), (409, "needs_database"))
        self.refused("GET", "/api/settings", h, "paused")
        self.ok("POST", "/api/team/resume", OWNER, {"without_database": True}, status=400)
        self.refused("GET", "/api/settings", h, "paused")
        self.ok("POST", "/api/team/resume", OWNER, {"without_database": True, "no_restore": True})
        self.ok("GET", "/api/settings", h)
        audit = (Path(self.tmp) / "team" / "audit.jsonl").read_text("utf-8")
        self.assertIn("resume without the database: " + NO_RESTORE, audit)
        # When the database answers again, the later Resume in the file wins over its older Pause.
        self.db.up = True
        self.team.reconcile()
        self.assertFalse(self.db.doc["paused"])
        self.ok("GET", "/api/settings", h)

    def test_resume_with_the_database(self):
        h = self.admit("m-1")
        self.ok("POST", "/api/team/pause", OWNER)
        self.ok("POST", "/api/team/resume", OWNER)
        self.assertFalse(self.db.doc["paused"])
        self.ok("GET", "/api/settings", h)

    def test_a_pause_survives_a_restored_state_file(self):
        h = self.admit("m-1")
        backup = self.backup()
        self.ok("POST", "/api/team/pause", OWNER)
        self.team.reconcile()
        self.restore(backup)
        self.refused("GET", "/api/settings", h, "paused")
        self.assertTrue(self.team.paused())

    def test_team_mode_off_has_no_mirror_and_a_plain_pause(self):
        app, c, _ = make(team=False)
        self.assertIsNone(app.state.team.mirror)
        self.assertEqual(app.state.team.sync_view()["state"], "file")


# ---------------------------------------------------------------- labs, per learner

class LabTests(TeamCase):
    def test_surrogates_are_random_and_keyed_to_nothing(self):
        self.admit("m-1")
        self.admit("m-2")
        a, b = self.row("m-1")["lab"], self.row("m-2")["lab"]
        self.assertRegex(a, r"^[0-9a-f]{12}$")
        self.assertNotEqual(a, b)
        for s in (a, b):
            for part in ("m-1", "m-2", "tenant-1", self.key("m-1")[:12], self.key("m-2")[:12],
                         sha256("tenant-1:m-1")[:12], sha256("tenant-1:m-2")[:12]):
                self.assertNotIn(s, part)
                self.assertNotIn(part, s)

    def test_a_row_from_before_pr4_gets_a_surrogate_once(self):
        self.admit("m-1")
        with self.store.lock:
            self.row("m-1")["lab"] = "0123456789abcdef"   # token_hex(8), as PR 1 to 3 wrote it
            self.team._save()
        first = self.team.lab_surrogate(self.key("m-1"))
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(self.team.lab_surrogate(self.key("m-1")), first)
        self.assertEqual(self.row("m-1")["lab"], first)

    def test_two_members_and_the_owner_each_have_their_own_lab(self):
        a, b = self.admit("m-1"), self.admit("m-2")
        schemas = {self.ok("GET", "/api/labs/search", h)["schema"] for h in (a, b, OWNER)}
        self.assertEqual(len(schemas), 3)
        self.assertIn("lab_search", schemas)
        sql = """SELECT TOP (2) Id, VECTOR_DISTANCE('cosine', Embedding, '[1,0,0]') AS distance
                 INTO Answers FROM Products ORDER BY distance;"""
        self.ok("POST", "/api/labs/search/run", a, {"sql": sql})
        self.assertTrue(self.ok("POST", "/api/labs/search/check", a)["passed"])
        self.assertFalse(self.ok("POST", "/api/labs/search/check", b)["passed"])
        self.assertFalse(self.ok("POST", "/api/labs/search/check", OWNER)["passed"])
        self.ok("POST", "/api/labs/search/reset", b)
        self.assertTrue(self.ok("POST", "/api/labs/search/check", a)["passed"])
        # Leaving takes the member's lab with it.
        runner = self.app.state.labs.runner
        s = self.row("m-1")["lab"]
        self.assertIn(f"lr_{s}_search", runner.runs)
        self.ok("POST", "/api/team/leave", a, {"confirm": "LEAVE"})
        self.assertNotIn(f"lr_{s}_search", runner.runs)

    def test_member_reset_and_repair(self):
        h = self.admit("m-1")
        self.ok("POST", "/api/labs/vector/reset", h)
        self.refused("POST", "/api/diag/lab/repair", h, "admin_only", body={"confirm": True})
        self.ok("POST", "/api/diag/lab/repair", OWNER, {}, status=400)
        self.ok("POST", "/api/diag/lab/repair", OWNER, {"confirm": True})

    def test_the_owner_keeps_the_legacy_schemas_and_one_surrogate_after_a_lost_account(self):
        self.ok("POST", "/api/labs/vector/run", OWNER, {"sql": "SELECT 1"})
        s = self.app.state.labs.owner_user().surrogate
        self.assertEqual(json.loads((Path(self.tmp) / "team" / "owner.json").read_text("utf-8"))["lab_surrogate"], s)
        self.ok("GET", "/api/me", OWNER)   # writes the owner's name beside the surrogate
        self.app, self.c, _ = make(self.tmp, owner_oid="owner-2", owner_key=OWNER_KEY)
        new_owner = who("owner-2", name="Alex", email=None)
        self.assertEqual(self.ok("GET", "/api/labs/vector", new_owner)["schema"], "lab_vector")
        self.ok("POST", "/api/labs/vector/run", new_owner, {"sql": "SELECT 1"})
        self.assertEqual(self.app.state.labs.owner_user().surrogate, s)
        self.assertEqual(self.app.state.labs.owner_user().schema("vector"), "lab_vector")


class LabRoomTests(unittest.TestCase):
    def setUp(self):
        from app.core import Store
        self.tmp = tempfile.mkdtemp(prefix="dojo-labroom-")
        self.store = Store(Path(self.tmp))

    def test_the_owner_surrogate_comes_back_from_the_database(self):
        runner = mock.Mock()
        runner.fetch_all.return_value = [[["lr_abcdefabcdef_vector"]]]
        room = LabRoom(runner, False, self.store)
        self.assertEqual(room.owner_user().surrogate, "abcdefabcdef")
        self.assertEqual(self.store.read("team", "owner")["lab_surrogate"], "abcdefabcdef")
        # Known from the file from now on: the database is not asked again.
        runner.fetch_all.reset_mock()
        self.assertEqual(LabRoom(runner, False, self.store).owner_user().surrogate, "abcdefabcdef")
        runner.fetch_all.assert_not_called()

    def test_no_owner_runner_in_the_database_draws_a_new_one(self):
        runner = mock.Mock()
        runner.fetch_all.return_value = [[]]
        s = LabRoom(runner, False, self.store).owner_user()
        self.assertTrue(s.legacy)
        self.assertRegex(s.surrogate, r"^[0-9a-f]{12}$")

    def test_the_reconciler_drops_only_tombstoned_member_schemas(self):
        dead, alive, stray, owner = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd"
        runner = mock.Mock()
        runner.fetch_all.return_value = [
            [["lab_vector"], ["lab_search"], [f"lab_vector_{dead}"], [f"lab_search_{dead}"],
             [f"lab_vector_{alive}"], [f"lab_hybrid_{stray}"], ["lab_vector_x"]],
            [[f"lr_{dead}_vector"], [f"lr_{owner}_vector"], [f"lr_{alive}_vector"], ["lr_nonsense"]],
        ]
        room = LabRoom(runner, False, self.store)
        room.migrated = True
        out = room.reconcile({sha256(dead)}, {alive, owner})
        self.assertEqual(out["dropped"], 1)
        dropped = [c.args for c in runner.drop.call_args_list]
        self.assertEqual(dropped, [LabUser(dead).names(lab) for lab in ("vector", "search", "hybrid")])
        self.assertEqual(out["unknown"], [f"lab_hybrid_{stray}"])
        for args in dropped:
            self.assertNotIn(args[0], ("lab_vector", "lab_search", "lab_hybrid"))

    def test_local_demo_never_asks_a_database(self):
        room = LabRoom(FakeRunner(), True, self.store)
        self.assertEqual(room.reconcile({sha256("x")}, set()), {"dropped": 0, "unknown": []})
        room.operation("vector", "run", LabUser("0123456789ab"), "SELECT 1")
        self.assertIn("lr_0123456789ab_vector", room.runner.runs)
        room.forget("0123456789ab")
        self.assertNotIn("lr_0123456789ab_vector", room.runner.runs)


class MirrorSqlTests(unittest.TestCase):
    def test_only_hashes_dates_and_statuses_reach_the_sql(self):
        runner = mock.Mock()
        m = Mirror(runner)
        h = sha256("x")
        m.write({"epoch": 7, "paused": True, "tombstones": [{"key": h, "lab": h, "at": "2026-01-02"}],
                  "changes": [{"member": h, "epoch": 7, "status": "removed", "reason": "team-ended", "date": "2026-01-02"}],
                  "retired": [{"principal": h, "at": "2026-01-02"}]}, "2025-12-01")
        sql = "\n".join(runner.fetch_all.call_args.args[0])
        self.assertIn("SET epoch = 7, paused = 1", sql)
        self.assertIn("DELETE FROM dojo_meta.tombstones WHERE deleted < '2025-12-01'", sql)
        self.assertIn(f"principal_hash NOT IN ('{h}')", sql)
        for bad in ({"tombstones": [{"key": "l" + "0" * 31, "at": "2026-01-02"}]},
                    {"changes": [{"member": h, "status": "removed'); DROP TABLE x;--", "date": "2026-01-02"}]},
                    {"changes": [{"member": h, "status": "removed", "reason": "x' OR 1=1", "date": "2026-01-02"}]},
                    {"retired": [{"principal": h, "at": "2026-01-02'; --"}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.write({"epoch": 1, **bad}, "2025-12-01")

    def test_read_creates_the_tables_and_parses_rows(self):
        runner = mock.Mock()
        h = sha256("x")
        from app.mirror import SETUP
        runner.fetch_all.return_value = [[]] * len(SETUP) + [
            [[12, 1]], [[h, h, "2026-01-02"]], [[h, 12, "removed ", "left", "2026-01-02"]], [[h, "2026-01-03"]]]
        doc = Mirror(runner).read()
        self.assertEqual((doc["epoch"], doc["paused"]), (12, True))
        self.assertEqual(doc["changes"], [{"member": h, "epoch": 12, "status": "removed", "reason": "left", "date": "2026-01-02"}])
        self.assertEqual(doc["retired"], [{"principal": h, "at": "2026-01-03"}])
        self.assertIn("AUTHORIZATION [dbo]", runner.fetch_all.call_args.args[0][0])


class LabsOffTests(unittest.TestCase):
    """In Azure with DOJO_LAB_SERVER and DOJO_LAB_DATABASE empty there is no lab database (docs/operations.md):
    the app starts with no runner at all, every lab route says so calmly, and nothing waits for a mirror."""
    OFF = {"DOJO_LAB_SERVER": "", "DOJO_LAB_DATABASE": ""}
    POST = {**OWNER, "X-Dojo": "1"}

    def start(self, team: bool):
        with mock.patch.dict(os.environ, self.OFF), \
                mock.patch.object(SqlRunner, "__init__", side_effect=AssertionError("no runner when labs are off")), \
                mock.patch.object(FakeRunner, "__init__", side_effect=AssertionError("no demo runner in Azure")):
            return make(team=team, in_azure=True)

    def test_the_app_starts_and_every_lab_route_refuses_calmly(self):
        for team in (False, True):
            app, c, _ = self.start(team)
            self.assertIsNone(app.state.labs)
            self.assertIsNone(app.state.team.mirror, "no lab database, so no mirror to wait for")
            self.assertTrue(app.state.team.admitting())
            self.assertEqual(c.get("/api/settings", headers=OWNER).status_code, 200)
            for method, path, body in (("GET", "/api/labs", None), ("GET", "/api/labs/vector", None),
                                       ("POST", "/api/labs/vector/run", {"sql": "SELECT 1"}),
                                       ("POST", "/api/labs/vector/check", None),
                                       ("POST", "/api/labs/vector/reset", None),
                                       ("POST", "/api/labs/vector/hint", {"index": 0}),
                                       ("POST", "/api/diag/lab/repair", {"confirm": True})):
                r = c.request(method, path, headers=self.POST, json=body)
                self.assertEqual((r.status_code, r.json().get("detail")), (404, "Labs are not set up on this Dojo."),
                                 f"{method} {path}")
            diag = c.get("/api/diag", headers=OWNER).json()["labs"]
            self.assertFalse(diag["on"])
            self.assertIn("not set up", diag["mode"])
            self.assertEqual(c.post("/api/diag/lab", headers=self.POST).json()["ok"], False)
            self.assertFalse(app.state.plan.labs_on)
            self.assertIs(c.get("/api/me", headers=OWNER).json()["labs"], False, "the SPA hides lab links")

    def test_me_says_labs_are_on_when_they_are(self):
        _, c, _ = make(team=False)
        self.assertIs(c.get("/api/me", headers=OWNER).json()["labs"], True)

    def test_no_page_links_to_a_lab_when_labs_are_off(self):
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text("utf-8")
        self.assertIn('const labsOn = () => !state.me || state.me.labs !== false;', script)
        self.assertIn('const LABS_OFF = "Labs are not set up on this Dojo.";', script)
        links = [line for line in script.splitlines() if "#/lab/${" in line]
        self.assertEqual(len(links), 4, "the course panel, the skill drawer, the skill page and Studio's drift list")
        for line in links:
            self.assertIn("labsOn()", line + script.splitlines()[script.splitlines().index(line) - 1], line.strip())

    def test_the_plan_schedules_no_lab_when_labs_are_off(self):
        from app.labs import LABS
        app, _, _ = make(team=False)
        ev = {spec["skill"]: {"checks": {"unaided": False, "later": False}} for spec in LABS.values()}
        today = utcnow().date()
        self.assertTrue(app.state.plan.labs_on, "the local demo keeps its labs")
        self.assertIsNotNone(app.state.plan._lab_need("dp-800", ev, [], today))
        app.state.plan.labs_on = False
        self.assertIsNone(app.state.plan._lab_need("dp-800", ev, [], today))

    def test_one_lab_setting_without_the_other_still_fails_loudly(self):
        with mock.patch.dict(os.environ, {"DOJO_LAB_SERVER": "dojo-lab.database.windows.net", "DOJO_LAB_DATABASE": ""}):
            with self.assertRaises(ValueError):
                make(team=False, in_azure=True)


if __name__ == "__main__":
    unittest.main()
