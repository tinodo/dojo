"""Performance monitoring of the lab database (ADR 0023): the T-SQL, what Dojo remembers about it, the owner's button, and the
promise that nothing else connects to the free serverless database for it.

Nothing here reaches Azure. The driver is stood in for by a connection that answers the way the real one does for a batch."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import team as team_mod
from app.core import Store, UserError
from app.labs import (FakeRunner, LabRoom, LabUser, MONITORING_PROPERTY, MONITORING_READ, MONITORING_SET, SqlRunner, is_waking,
                      monitoring_state)
from tests.test_api import POST
from tests.test_team import OWNER, TeamCase, make

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text("utf-8")
NODE = shutil.which("node")
SERVER, DATABASE = "dojo-lab.database.windows.net", "sqldb-lab-test"
WAKING = "Database 'sqldb-lab-test' on server 'dojo-lab' is not currently available. Please retry the connection later."
SET_ON = MONITORING_SET.format(name=MONITORING_PROPERTY, value="true")
SET_OFF = MONITORING_SET.format(name=MONITORING_PROPERTY, value="false")
READ = MONITORING_READ.format(name=MONITORING_PROPERTY)


class Cursor:
    """Answers a batch like the driver does: statements that return nothing are result sets without a description, and
    `nextset()` moves on to the one that has rows (SqlRunner.execute reads it that way)."""

    def __init__(self, conn):
        self.conn, self.description, self._sets, self._at, self._rows = conn, None, [None], 0, []

    def execute(self, sql):
        self.conn.statements.append(sql)
        self._sets = list(self.conn.answer(sql) if self.conn.answer else (self.conn.script.pop(0) if self.conn.script else [None]))
        self._at = 0
        self._load()

    def _load(self):
        current = self._sets[self._at]
        self.description, self._rows = (None, []) if current is None else ([("value",)], list(current))

    def nextset(self):
        if self._at + 1 >= len(self._sets):
            return False
        self._at += 1
        self._load()
        return True

    def fetchmany(self, size):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        pass


class Connection:
    def __init__(self, script, answer=None):
        self.statements, self.script, self.answer, self.closed, self.timeout = [], script, answer, False, 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        pass

    def close(self):
        self.closed = True


def sql_runner() -> SqlRunner:
    """A real SqlRunner, so its `admin()`, `execute()` and `run()` are the ones that run; only the driver is a stand-in."""
    stub = types.ModuleType("mssql_python")
    stub.pooling = lambda enabled=False: None
    with mock.patch.dict(sys.modules, {"mssql_python": stub}):
        return SqlRunner(SERVER, DATABASE, object())


class Wired:
    """Hands out one scripted connection for each statement the app sends (or answers each statement with `answer`), and keeps
    them. A script is a list of result sets: None for a statement that returns nothing, or the rows of one that does."""

    def __init__(self, *scripts, answer=None, runner=None):
        self.runner = runner or sql_runner()
        self.scripts, self.answer, self.connections = list(scripts), answer, []
        self.runner.connect = self.connect

    def connect(self):
        outcome = self.scripts.pop(0) if self.scripts else None
        if isinstance(outcome, Exception):
            raise outcome
        conn = Connection(list(outcome) if outcome is not None else [], self.answer)
        if outcome is None and self.answer is None:
            raise AssertionError("a connection nobody scripted")
        self.connections.append(conn)
        return conn

    @property
    def statements(self):
        return [s for c in self.connections for s in c.statements]


ON = [[None, None, [("true",)]]]     # NOCOUNT and the EXEC answer with nothing; then the one row that reads the value back
OFF = [[None, None, [("false",)]]]


class StatementTests(unittest.TestCase):
    def test_it_is_learns_script_without_the_go_that_only_ssms_reads(self):
        self.assertEqual(MONITORING_PROPERTY, "MS_EnablePerformanceMonitoringPreview")
        for value, sql in (("true", SET_ON), ("false", SET_OFF)):
            self.assertIn("IF EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 0 AND name = N'MS_EnablePerformanceMonitoringPreview')", sql)
            self.assertIn(f"EXEC sys.sp_updateextendedproperty @name = N'MS_EnablePerformanceMonitoringPreview', @value = N'{value}';", sql)
            self.assertIn(f"EXEC sys.sp_addextendedproperty @name = N'MS_EnablePerformanceMonitoringPreview', @value = N'{value}';", sql)
            self.assertFalse(re.search(r"(?im)^\s*GO\s*$", sql), "GO is a client batch separator: the server would refuse it")
            self.assertTrue(sql.startswith("SET NOCOUNT ON;"))
            self.assertEqual(sql.count("SELECT CONVERT"), 1, "one row answers: the value as it is now")
            self.assertNotIn("{", sql)

    def test_the_other_value_never_appears_in_a_statement(self):
        self.assertNotIn("N'false'", SET_ON)
        self.assertNotIn("N'true'", SET_OFF)

    def test_the_read_is_learns_verification_query(self):
        self.assertEqual(READ, "SELECT CONVERT(NVARCHAR(128), value) AS value FROM sys.extended_properties "
                               "WHERE class = 0 AND name = N'MS_EnablePerformanceMonitoringPreview'")

    def test_what_a_read_says(self):
        for rows, want in [([], "unset"), ([("true",)], "on"), ([["TRUE"]], "on"), ([(" True ",)], "on"), ([("false",)], "off"),
                           ([("False",)], "off"), ([("maybe",)], None), ([("",)], None), ([("true",), ("true",)], None), ([()], None)]:
            self.assertEqual(monitoring_state(rows), want, rows)


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-labmon-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Store(self.tmp)

    def room(self, *scripts, answer=None) -> tuple[LabRoom, Wired]:
        wired = Wired(*scripts, answer=answer)
        return LabRoom(wired.runner, False, self.store), wired

    def test_nothing_is_known_until_the_owner_has_pressed(self):
        room, wired = self.room()
        self.assertEqual(room.monitoring(), {"observed": None, "checked_at": None, "attempted_at": None, "error": None, "database": None})
        self.assertEqual(wired.connections, [], "asking connects to nothing")

    def test_turning_it_on_sets_the_property_and_reads_it_back_in_one_connection(self):
        room, wired = self.room(ON)
        done = room.set_monitoring(True)
        self.assertEqual(len(wired.connections), 1)
        self.assertEqual(wired.statements, [SET_ON])
        self.assertTrue(wired.connections[0].closed)
        self.assertEqual((done["observed"], done["error"], done["database"]), ("on", None, DATABASE))
        self.assertTrue(done["checked_at"] and done["checked_at"] == done["attempted_at"])
        self.assertTrue(room.lock.acquire(blocking=False), "the lab lock is free again")
        room.lock.release()

    def test_what_was_confirmed_is_still_there_after_a_restart(self):
        room, _ = self.room(ON)
        done = room.set_monitoring(True)
        again, wired = self.room()
        self.assertEqual(again.monitoring(), {k: done[k] for k in ("observed", "checked_at", "attempted_at", "error", "database")})
        self.assertEqual(wired.connections, [])
        self.assertEqual(json.loads((self.tmp / "labs" / "monitoring.json").read_text("utf-8"))["observed"], "on")

    def test_turning_it_off(self):
        room, wired = self.room(ON, OFF)
        room.set_monitoring(True)
        done = room.set_monitoring(False)
        self.assertEqual(wired.statements, [SET_ON, SET_OFF])
        self.assertEqual((done["observed"], done["error"]), ("off", None))

    def test_a_value_that_is_not_what_was_set_is_never_recorded_as_set(self):
        for answer in ([[None, None, [("false",)]]], [[None, None, []]], [[None, None]], [[None, None, [("true",), ("true",)]]]):
            room, _ = self.room(answer)
            with self.assertRaisesRegex(UserError, "did not confirm"):
                room.set_monitoring(True)
            now = room.monitoring()
            self.assertEqual((now["observed"], now["checked_at"], now["error"]), (None, None, "the database did not confirm it"), answer)

    def test_turning_it_off_needs_the_database_to_say_off(self):
        room, _ = self.room(ON, [[None, None, [("true",)]]])
        room.set_monitoring(True)
        with self.assertRaisesRegex(UserError, "did not confirm"):
            room.set_monitoring(False)
        self.assertEqual(room.monitoring()["observed"], "on", "what was last confirmed stays")

    def test_a_failed_try_keeps_what_was_confirmed_and_says_why(self):
        room, _ = self.room(ON, Exception(WAKING))
        first = room.set_monitoring(True)
        with self.assertRaises(Exception) as caught:
            room.set_monitoring(False)
        self.assertTrue(is_waking(caught.exception))
        now = room.monitoring()
        self.assertEqual((now["observed"], now["checked_at"]), ("on", first["checked_at"]), "what was last confirmed stays")
        self.assertEqual(now["error"], "database waking")
        self.assertGreaterEqual(now["attempted_at"], first["attempted_at"])

    def test_a_good_try_clears_the_error(self):
        room, _ = self.room(Exception(WAKING), ON)
        with self.assertRaises(Exception):
            room.set_monitoring(True)
        self.assertEqual(room.monitoring()["error"], "database waking")
        self.assertIsNone(room.set_monitoring(True)["error"])

    def test_a_busy_room_waits_then_refuses_and_nothing_is_sent(self):
        room, wired = self.room(ON)
        asked = []

        class Busy:
            def acquire(self, blocking=True, timeout=-1):
                asked.append(timeout)
                return False

        room.lock = Busy()
        with self.assertRaisesRegex(UserError, "lab query is running"):
            room.set_monitoring(True)
        self.assertEqual((asked, wired.connections), ([30], []))

    def test_the_local_demo_has_no_database_to_monitor(self):
        room = LabRoom(FakeRunner(), True, self.store)
        with self.assertRaisesRegex(UserError, "local demo"):
            room.set_monitoring(True)
        self.assertIsNone(room.monitoring()["observed"])

    def test_without_a_store_it_is_remembered_for_the_process(self):
        wired = Wired(ON)
        room = LabRoom(wired.runner, False)
        room.set_monitoring(True)
        self.assertEqual(room.monitoring()["observed"], "on")

    def test_preparing_resetting_and_reconciling_never_touch_it(self):
        def answer(sql):
            return [[("0",)]] if "wrong_type" in sql else [None]

        room, wired = self.room(answer=answer)
        room.prepare("search")
        room.operation("search", "reset")
        room.reconcile(set(), set())
        self.assertTrue(len(wired.statements) > 5)
        self.assertFalse([s for s in wired.statements if MONITORING_PROPERTY in s or "extendedproperty" in s.lower()])

    def probe_with(self, reading, fail_read=False):
        room, wired = self.room(answer=None)
        room._owner, room.migrated = LabUser("aaaaaaaaaaaa", legacy=True), True
        runner_name = room._owner.names("vector")[2]

        def answer(sql):
            if sql == READ:
                if fail_read:
                    raise RuntimeError("the read failed")
                return [reading]
            if sql.startswith("SELECT DB_NAME()"):
                return [[(DATABASE, "dbo")]]
            if "wrong_type" in sql:
                return [[("0",)]]
            if sql.lstrip().startswith("SELECT USER_NAME() AS principal"):
                return [[(runner_name, "1", "0", "0", "0", "0")]]
            return [None]

        wired.answer = answer
        return room, wired, room.probe()

    def test_the_probe_reads_the_live_value_and_keeps_the_record_honest(self):
        for reading, state in (([("true",)], "on"), ([], "unset"), ([("False",)], "off")):
            room, _, probe = self.probe_with(reading)
            now = room.monitoring()
            self.assertEqual((probe["ok"], probe["performance_monitoring"], now["observed"], now["database"]), (True, state, state, DATABASE))
            self.assertTrue(now["checked_at"])
        room, _, probe = self.probe_with([("maybe",)])
        self.assertEqual((probe["performance_monitoring"], room.monitoring()["observed"]), (None, "off"),
                         "a value that is neither is not recorded: what was last confirmed (off, just above) stays")

    def test_a_read_that_fails_does_not_fail_the_probe(self):
        room, _, probe = self.probe_with([], fail_read=True)
        self.assertEqual((probe["ok"], probe["database_checked"], probe["performance_monitoring"]), (True, True, None))
        self.assertIsNone(room.monitoring()["observed"])


class NothingSetsItByItself(unittest.TestCase):
    """The lab database is on the free offer and pauses when idle: every connection is a wake that uses the month's allowance."""

    def test_only_the_owners_button_calls_it(self):
        callers = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            text = path.read_text("utf-8")
            callers += [f"{path.relative_to(ROOT).as_posix()}:{n}" for n, line in enumerate(text.splitlines(), 1)
                        if "set_monitoring(" in line and "def set_monitoring" not in line]
            if path.name != "labs.py":
                self.assertNotIn("MONITORING_SET", text, path.name)
        self.assertEqual(len(callers), 1, callers)
        self.assertTrue(callers[0].startswith("app/main.py:"), callers)
        main = (ROOT / "app" / "main.py").read_text("utf-8")
        route = main.split('@app.post("/api/studio/lab/monitoring")', 1)[1].split("@app.", 1)[0]
        self.assertIn("learner: Learner = Admin", route)
        self.assertIn("body: LabMonitoring", route)

    def test_the_body_is_a_strict_bool(self):
        main = (ROOT / "app" / "main.py").read_text("utf-8")
        self.assertIn("class LabMonitoring(BaseModel):\n    on: StrictBool", main)

    def test_building_and_asking_a_room_connects_to_nothing(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-labmon-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(SqlRunner, "connect", side_effect=AssertionError("no database")):
            room = LabRoom(sql_runner(), False, Store(tmp))
            room.monitoring()


class RouteCase(unittest.TestCase):
    """Studio's lab database in an app that has one (the private Azure SQL database), with a stand-in driver. The runner's
    `connect` refuses until a test wires it, so anything that reaches for the database without being asked fails loudly."""
    ENV = {"DOJO_LAB_SERVER": SERVER, "DOJO_LAB_DATABASE": DATABASE}

    def setUp(self):
        guard = mock.patch.object(SqlRunner, "connect", side_effect=AssertionError("the lab database was reached"))
        guard.start()
        self.addCleanup(guard.stop)
        stub = types.ModuleType("mssql_python")
        stub.pooling = lambda enabled=False: None
        with mock.patch.dict(os.environ, self.ENV), mock.patch.dict(sys.modules, {"mssql_python": stub}):
            self.app, self.c, self.tmp = make(team=False, in_azure=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.labs = self.app.state.labs

    def wire(self, *scripts) -> Wired:
        return Wired(*scripts, runner=self.labs.runner)

    def post(self, body, headers=None):
        return self.c.post("/api/studio/lab/monitoring", headers=headers or {**OWNER, **POST}, json=body)


class RouteTests(RouteCase):
    def test_reading_never_connects(self):
        for path in ("/api/studio/lab", "/api/diag", "/api/studio/courses", "/api/studio/activity", "/api/me"):
            self.assertEqual(self.c.get(path, headers=OWNER).status_code, 200, path)
        self.app.state.studio.courses(True)
        self.app.state.studio.activity()
        self.assertEqual(self.c.get("/api/studio/lab", headers=OWNER).json(), {
            "on": True, "demo": False, "database": DATABASE,
            "monitoring": {"observed": None, "checked_at": None, "attempted_at": None, "error": None, "database": None}})
        self.assertIsNone(self.c.get("/api/diag", headers=OWNER).json()["labs"]["performance_monitoring"]["observed"])

    def test_the_owner_turns_it_on_and_the_page_and_diag_say_so(self):
        wired = self.wire(ON)
        r = self.post({"on": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["monitoring"]["observed"], "on")
        self.assertEqual(wired.statements, [SET_ON])
        got = self.c.get("/api/studio/lab", headers=OWNER).json()["monitoring"]
        self.assertEqual((got["observed"], got["database"]), ("on", DATABASE))
        self.assertEqual(self.c.get("/api/diag", headers=OWNER).json()["labs"]["performance_monitoring"]["observed"], "on")
        self.assertEqual(len(wired.connections), 1, "reading it did not connect again")

    def test_a_database_that_is_waking_says_so_and_keeps_the_record(self):
        self.wire(Exception(WAKING))
        r = self.post({"on": True})
        self.assertEqual((r.status_code, r.json()["detail"]), (503, "Waking the database, about a minute. Try again shortly."))
        self.assertEqual(self.c.get("/api/studio/lab", headers=OWNER).json()["monitoring"]["error"], "database waking")

    def test_another_failure_is_a_plain_unavailable_and_shows_no_driver_text(self):
        self.wire(RuntimeError("login failed for a@b.example"))
        r = self.post({"on": True})
        self.assertEqual((r.status_code, r.json()["detail"]), (503, "The database is unavailable. Try again."))
        self.assertNotIn("a@b.example", r.text)
        self.assertEqual(self.c.get("/api/studio/lab", headers=OWNER).json()["monitoring"]["error"], "database error")

    def test_an_answer_that_is_not_what_was_set_is_refused(self):
        self.wire([[None, None, [("false",)]]])
        r = self.post({"on": True})
        self.assertEqual(r.status_code, 422)
        self.assertIn("did not confirm", r.json()["detail"])

    def test_only_true_or_false_is_taken(self):
        wired = self.wire()
        for body in ({"on": "true"}, {"on": 1}, {"on": 0}, {"on": None}, {"on": "yes"}, {}, {"off": True}):
            self.assertEqual(self.post(body).status_code, 422, body)
        self.assertEqual(wired.connections, [], "a refused body connects to nothing")

    def test_a_call_without_the_header_is_refused(self):
        wired = self.wire()
        r = self.c.post("/api/studio/lab/monitoring", headers=OWNER, json={"on": True})
        self.assertIn(r.status_code, (400, 403))
        self.assertEqual(wired.connections, [])


class RefusalTests(TeamCase):
    def test_a_member_cannot_read_or_press(self):
        h = self.admit("m-1")
        self.refused("GET", "/api/studio/lab", h, "admin_only")
        self.refused("POST", "/api/studio/lab/monitoring", h, "admin_only", body={"on": True})
        self.ok("GET", "/api/studio/lab", OWNER)

    def test_the_local_demo_says_so_and_a_press_does_nothing(self):
        got = self.ok("GET", "/api/studio/lab", OWNER)
        self.assertEqual((got["on"], got["demo"], got["database"]), (True, True, None))
        r = self.call("POST", "/api/studio/lab/monitoring", OWNER, {"on": True})
        self.assertEqual(r.status_code, 422)
        self.assertIn("local demo", r.json()["detail"])


class LabsOffTests(unittest.TestCase):
    def test_with_no_lab_database_there_is_nothing_to_show_or_press(self):
        with mock.patch.dict(os.environ, {"DOJO_LAB_SERVER": "", "DOJO_LAB_DATABASE": ""}), \
                mock.patch.object(SqlRunner, "__init__", side_effect=AssertionError("no runner when labs are off")), \
                mock.patch.object(FakeRunner, "__init__", side_effect=AssertionError("no demo runner in Azure")):
            app, c, tmp = make(team=False, in_azure=True)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(c.get("/api/studio/lab", headers=OWNER).json(), {"on": False})
        r = c.post("/api/studio/lab/monitoring", headers={**OWNER, **POST}, json={"on": True})
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "Labs are not set up on this Dojo."))


# ---------------------------------------------------------------- Studio's panel, run in node

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const APP = fs.readFileSync(PATH, 'utf8');
const START = APP.indexOf("const LAB_MON = ");
const END = APP.indexOf("// ---------------------------------------------------------------- Studio: deeper lessons (ADR 0008)");
if (START < 0 || END < START) throw new Error("the lab panel is not where it was");
const flat = (list) => list.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
class El { constructor(tag, attrs) { this.tag = tag; this.attrs = attrs || {}; this.kids = []; this.textContent = ""; this.disabled = false; } }
const h = (tag, attrs, ...kids) => { const el = new El(tag, attrs); el.kids = flat(kids); return el; };
const put = (el, ...kids) => { el.kids = flat(kids); };
const text = (el) => (typeof el === "string" ? el : (el.textContent || "") + el.kids.map(text).join(" "));
const find = (el, pred) => (typeof el === "string" ? [] : [...(pred(el) ? [el] : []), ...el.kids.flatMap((k) => find(k, pred))]);
const calls = [], asked = [];
let answers = [], confirms = [];
const context = vm.createContext({
  h, put, button: (label, onclick, kind) => h("button", { onclick, kind }, label),
  chip: (label, kind) => h("span", { chip: kind }, label), eyebrow: (icon, label) => h("p", { eyebrow: icon }, label),
  fmtDate: (s) => `D(${s})`,
  api: async (path, opts) => { calls.push([path, opts && opts.method, opts && opts.body]); const a = answers.shift(); if (a instanceof Error) throw a; return a; },
  window: { confirm: (message) => { asked.push(message); return confirms.shift(); } },
});
vm.runInContext(APP.slice(START, END) + "\nthis.labRow = labRow;", context);
const labRow = context.labRow;
const view = (l) => {
  const el = labRow(l);
  if (!el) return null;
  const chips = find(el, (e) => e.attrs && e.attrs.chip !== undefined).map((e) => [text(e), e.attrs.chip]);
  const buttons = find(el, (e) => e.tag === "button");
  return { el, text: text(el), chips, buttons, labels: buttons.map((b) => text(b)) };
};
const tick = () => new Promise((r) => setTimeout(r, 5));
const mon = (extra) => ({ observed: null, checked_at: null, attempted_at: null, error: null, database: null, ...extra });
const lab = (m, extra) => ({ on: true, demo: false, database: "sqldb-lab", monitoring: mon(m), ...extra });
"""


def run_node(script: str) -> object:
    prelude = f"const PATH = {json.dumps(str(ROOT / 'app' / 'static' / 'app.js'))};\n"
    done = subprocess.run([NODE, "-"], input=prelude + HARNESS + "\n" + script, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class PanelTests(unittest.TestCase):
    def test_no_panel_without_a_lab_database_or_in_the_local_demo(self):
        out = run_node("console.log(JSON.stringify([labRow({on: false}) === null, labRow(lab({}, {demo: true})) === null]));")
        self.assertEqual(out, [True, True])

    def test_an_error_is_said_plainly(self):
        out = run_node("const v = view({error: 'Dojo hit a temporary server error'}); console.log(JSON.stringify([v.text, v.labels]));")
        self.assertIn("Lab database", out[0])
        self.assertIn("Dojo hit a temporary server error", out[0])
        self.assertEqual(out[1], [])

    def test_each_state_says_what_dojo_last_confirmed(self):
        out = run_node("""
const states = [{}, {observed: 'unset', checked_at: 'T1'}, {observed: 'off', checked_at: 'T2'}, {observed: 'on', checked_at: 'T3'}];
console.log(JSON.stringify(states.map((m) => { const v = view(lab(m)); return [v.chips, v.labels, v.text]; })));""")
        never, unset, off, on = out
        self.assertEqual((never[0], never[1]), ([["performance monitoring: not turned on yet", "line"]], ["Turn on"]))
        self.assertIn("Dojo has not confirmed it as on.", never[2])
        self.assertEqual((unset[0], unset[1]), ([["performance monitoring: not turned on", "line"]], ["Turn on"]))
        self.assertEqual((off[0], off[1]), ([["performance monitoring: off", "line"]], ["Turn on"]))
        self.assertIn("Turned off, last confirmed D(T2).", off[2])
        self.assertEqual((on[0], on[1]), ([["performance monitoring: on", "sage"]], ["Turn off"]))
        self.assertIn("Last confirmed on D(T3).", on[2])
        self.assertIn("Fabric Database Hub, on the Performance page", on[2])
        self.assertNotIn("onfirmed on D(T", never[2] + unset[2], "nothing is claimed that was never confirmed")
        for state in out:
            self.assertIn("free offer and pauses when idle", state[2])
            self.assertIn("does not say whether monitoring keeps it awake", state[2])

    def test_a_failed_try_is_shown_beside_what_was_confirmed(self):
        out = run_node("""
const v = view(lab({observed: 'on', checked_at: 'T3', attempted_at: 'T4', error: 'database waking'}));
console.log(JSON.stringify([v.chips, v.text]));""")
        self.assertEqual(out[0], [["performance monitoring: on", "sage"], ["last try failed", "rose"]])
        self.assertIn("Last confirmed on D(T3).", out[1])
        self.assertIn("The last try D(T4) did not work: database waking.", out[1])

    def test_nothing_is_sent_until_the_owner_has_confirmed_and_the_question_says_what_it_costs(self):
        out = run_node("""
(async () => {
  const v = view(lab({}));
  confirms = [false];
  await v.buttons[0].attrs.onclick(); await tick();
  console.log(JSON.stringify([calls, asked, v.buttons[0].disabled]));
})();""")
        self.assertEqual(out[0], [])
        self.assertEqual(len(out[1]), 1)
        for part in ("wakes the database", "2% of the month's free allowance", "Free amount remaining", "turn it off"):
            self.assertIn(part, out[1][0])
        self.assertFalse(out[2])

    def test_pressing_turn_on_posts_true_and_draws_the_answer(self):
        out = run_node("""
(async () => {
  const v = view(lab({}));
  confirms = [true];
  answers = [{ monitoring: mon({ observed: 'on', checked_at: 'T9' }) }];
  await v.buttons[0].attrs.onclick(); await tick();
  console.log(JSON.stringify([calls, text(v.el), find(v.el, (e) => e.tag === 'button').map((b) => text(b))]));
})();""")
        self.assertEqual(out[0], [["/api/studio/lab/monitoring", "POST", {"on": True}]])
        self.assertIn("Last confirmed on D(T9).", out[1])
        self.assertEqual(out[2], ["Turn off"])

    def test_pressing_turn_off_posts_false(self):
        out = run_node("""
(async () => {
  const v = view(lab({ observed: 'on', checked_at: 'T3' }));
  confirms = [true];
  answers = [{ monitoring: mon({ observed: 'off', checked_at: 'T9' }) }];
  await v.buttons[0].attrs.onclick(); await tick();
  console.log(JSON.stringify([calls, asked, text(v.el)]));
})();""")
        self.assertEqual(out[0], [["/api/studio/lab/monitoring", "POST", {"on": False}]])
        self.assertIn("What it already collected is kept", out[1][0])
        self.assertIn("Turned off, last confirmed D(T9).", out[2])

    def test_a_refusal_shows_its_reason_and_the_button_can_be_pressed_again(self):
        out = run_node("""
(async () => {
  const v = view(lab({}));
  confirms = [true];
  answers = [new Error('Waking the database, about a minute. Try again shortly.')];
  const button = v.buttons[0];
  await button.attrs.onclick(); await tick();
  console.log(JSON.stringify([text(v.el), button.disabled, text(button)]));
})();""")
        self.assertIn("Waking the database, about a minute. Try again shortly.", out[0])
        self.assertFalse(out[1], "the button is live again")
        self.assertEqual(out[2], "Turn on")


class WiringTests(unittest.TestCase):
    def test_studios_checks_reads_the_lab_and_draws_it_first(self):
        body = APP_JS.split("async function studioChecks() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('api("/api/studio/lab").catch((e) => ({ error: e.message }))', body)
        self.assertRegex(body, r"return \[labRow\(lb\),\s+pipe,")   # what the owner presses comes first (ADR 0024)

    def test_the_button_posts_to_the_one_route(self):
        self.assertEqual(APP_JS.count("/api/studio/lab/monitoring"), 1)


class NoticeAndDocsTests(unittest.TestCase):
    def test_the_adr_is_there_and_listed(self):
        adr = (ROOT / "docs" / "adr" / "0023-lab-performance-monitoring.md").read_text("utf-8")
        self.assertIn("MS_EnablePerformanceMonitoringPreview", adr)
        self.assertIn("(0023-lab-performance-monitoring.md)", (ROOT / "docs" / "adr" / "README.md").read_text("utf-8"))

    def test_members_are_told_and_asked_again_only_when_there_is_a_lab_database(self):
        self.assertGreaterEqual(team_mod.NOTICE_VERSION, 2, "the notice gained a sentence: every member is asked again")
        team = mock.Mock(owner_doc=lambda: {"display_name": "Alex"})

        def stored(labs: bool) -> str:
            sections = team_mod.notice_view(team, {}, labs=labs)["sections"]
            return next(s["text"] for s in sections if s["title"] == "Where your data is stored")

        with_labs = stored(True)
        self.assertIn("Microsoft's performance monitoring", with_labs)
        self.assertIn("not queries or table contents", with_labs)
        self.assertNotIn("monitoring", stored(False), "a Dojo with no lab database has nothing to monitor")


if __name__ == "__main__":
    unittest.main()
