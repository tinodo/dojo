"""What the lab page tells the owner after he presses a button (docs/ui-principles.md, rules 5 and 10).

The page runs in node with a page that keeps the elements it is told to make, so these tests look at the tree the owner would get: the status line
under the buttons, the Results area and the Checker, which block is scrolled into view and where the focus is. Nothing here touches the network."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const APP = fs.readFileSync(PATH, 'utf8');
const cut = (from, to) => { const a = APP.indexOf(from), b = APP.indexOf(to, a); if (a < 0 || b < a) throw new Error("not where it was: " + from); return APP.slice(a, b); };
const LABS = cut("// ---------------------------------------------------------------- hands-on Azure SQL labs", "// ---------------------------------------------------------------- Play (ADR 0010)");

const flat = (list) => list.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
const text = (el) => (typeof el === "string" ? el : el.kids.map(text).join(" "));
const all = (el, pred) => (typeof el === "string" ? [] : [...(pred(el) ? [el] : []), ...el.kids.flatMap((k) => all(k, pred))]);
class El {
  constructor(tag, attrs) { this.tag = tag; this.attrs = attrs || {}; this.kids = []; this.className = this.attrs.class || ""; this.disabled = false; this.listeners = {};
    this.scrolled = 0; this.focused = 0; this.value = ""; this.rows = this.attrs.rows; }
  get textContent() { return text(this); }
  set textContent(v) { this.kids = [String(v)]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  press() { (this.listeners.click || []).forEach((fn) => fn({ currentTarget: this })); }
  type(value) { this.value = value; (this.listeners.input || []).forEach((fn) => fn({ target: this })); }
  querySelectorAll(tag) { return all(this, (e) => e.tag === tag); }
  scrollIntoView() { this.scrolled += 1; }
  focus() { this.focused += 1; }
  remove() { this.removed = true; }
  append(...kids) { this.kids.push(...flat(kids)); }
}
const h = (tag, attrs, ...kids) => {
  const el = new El(tag, attrs);
  for (const [k, v] of Object.entries(attrs || {})) if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
  el.kids = flat(kids);
  if (tag === "textarea") el.value = text(el);
  return el;
};
const put = (el, ...kids) => { el.kids = flat(kids); };
const cls = (el) => String(el.className || "").split(" ");
const isCls = (name) => (e) => cls(e).includes(name);

let answers = [], confirms = [], seen = [];
const calls = [];
const state = { pid: "dp-800" };
let confirmAnswer = true;
const windowStub = { confirm: (m) => { confirms.push(m); return confirmAnswer; } };
const context = vm.createContext({
  h, put, state, console, window: windowStub, Date, setTimeout,
  plural: (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`,
  button: (label, onclick, kind) => h("button", { onclick, kind }, label),
  panel: (...kids) => h("section", { class: "panel" }, ...kids), eyebrow: (icon, label) => h("p", { eyebrow: icon }, label),
  pageHead: (title, sub) => h("div", { class: "pageh" }, title, sub), helpQ: () => null, driftNote: () => null, pageChange: () => null,
  ext: (href, label) => h("a", { href }, label), $: () => ({}), renderHeader: () => {}, renderSide: () => {},
  sleep: async () => { seen.push({ status: statusLine().textContent.trim(), results: words(output()), checker: words(checker()) }); },
  api: async (path, opts) => { calls.push([path, opts && opts.method, opts && opts.body]); const a = answers.shift(); if (typeof a === "function") return a(); if (a instanceof Error) throw a; return a; },
});
vm.runInContext(LABS + "\nthis.part = { LAB_FAILED, LAB_AGAIN, LAB_EXISTS, LAB_SIMPLER, labFailure, labOutcome, labRows, viewLab };", context);
const P = context.part;

const detail = (extra) => ({ id: "vector", title: "Make a vector product table", goal: "Create a table.", why: "Because.", steps: ["One", "Two"],
  starter: "CREATE TABLE Products (Id int PRIMARY KEY);", hint_count: 2, sources: [{ url: "https://learn.microsoft.com/x", title: "Docs" }], limits: { rows: 200, seconds: 20, sql_bytes: 8000 },
  local: false, shared: false, schema: "lab_vector", skill: "d3.g2.s3", drift: null, ...extra });
const failure = (message, status, extra) => Object.assign(new Error(message), status === undefined ? {} : { status }, extra || {});
const refusal = (message, number) => failure(message, 422, { kind: "sql", number });
const rows = (n) => ({ columns: ["Id", "Name"], rows: Array.from({ length: n }, (_, i) => [String(i + 1), `p${i + 1}`]), truncated: false, message: "Results from the database." });
const noResultSet = { columns: [], rows: [], truncated: false, message: "SQL completed." };
const passed = { passed: true, message: "The table has two products.", conditions: { runs: 2, hints_shown: 0, coach_answers: 0 } };

let view;
const statusLine = () => all(view, isCls("lab-message"))[0];
const feedback = () => all(view, isCls("lab-feedback"))[0];
const output = () => all(view, isCls("lab-output"))[0];
const checker = () => all(view, isCls("lab-check"))[0];
const editor = () => all(view, (e) => e.tag === "textarea")[0];
const button = (label) => all(view, (e) => e.tag === "button" && text(e).trim() === label)[0];
const open = async (extra) => { answers = [detail(extra)]; calls.length = 0; confirms.length = 0; seen = []; confirmAnswer = true; view = h("div", null, await P.viewLab("vector")); return view; };
const tick = () => new Promise((r) => setTimeout(r, 5));
const press = async (label) => { button(label).press(); await tick(); await tick(); };
const words = (el) => text(el).replace(/\s+/g, " ").trim();
const snapshot = () => ({
  status: statusLine().textContent.trim(), statusClass: statusLine().className, results: words(output()), checker: words(checker()),
  tables: all(output(), (e) => e.tag === "table").length, dataRows: all(output(), (e) => e.tag === "tr").length - (all(output(), (e) => e.tag === "table").length ? 1 : 0),
  notices: all(output(), isCls("lab-notice")).length, alerts: all(output(), (e) => e.attrs.role === "alert").length + all(checker(), (e) => e.attrs.role === "alert").length,
  advice: all(output(), isCls("advice")).concat(all(checker(), isCls("advice"))).map((e) => text(e).trim()),
  feedbackScrolled: feedback().scrolled, checkerScrolled: checker().scrolled, statusScrolled: statusLine().scrolled, outputScrolled: output().scrolled,
  disabled: all(view, (e) => e.tag === "button").map((b) => b.disabled),
});
"""


def run_node(script: str) -> object:
    prelude = f"const PATH = {json.dumps(str(ROOT / 'app' / 'static' / 'app.js'))};\n"
    done = subprocess.run([NODE, "-"], input=prelude + HARNESS + "\n" + script, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


DRIVER_NOISE = ("Driver Error", "DDBC Error", "[Microsoft]", "SQLExecDirectW", "SQL error", "SQL completed", "This SQL did not run")
START = "Dojo checks the database, not a screenshot. No lab fills a pip."
AGAIN = "Read the message, then change the SQL or try again, or press Reset this lab to start from the beginning."
EXISTS = "It is already there. Check it, then go on, or press Reset this lab to start over."
SIMPLER = "Look at your tables, then try a smaller or simpler query."


@unittest.skipUnless(NODE, "node is not installed")
class LabPageTests(unittest.TestCase):
    def test_the_editor_is_as_tall_as_its_text_and_grows_while_he_types(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  got.start = editor().attrs.rows;
  editor().type("a\\nb\\nc\\nd\\ne\\nf\\ng\\nh");
  got.eight = editor().rows;
  editor().type(Array.from({ length: 40 }, () => "x").join("\\n"));
  got.forty = editor().rows;
  editor().type("");
  got.empty = editor().rows;
  await open({ starter: Array.from({ length: 30 }, (_, i) => "-- line " + i).join("\\n") });
  got.longStarter = editor().attrs.rows;
  console.log(JSON.stringify(got));
})();
""")
        self.assertEqual(out, {"start": 5, "eight": 11, "forty": 16, "empty": 5, "longStarter": 16})

    def test_a_long_line_that_wraps_gets_the_rows_that_the_width_needs(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  const ed = editor();
  Object.defineProperty(ed, "clientHeight", { get() { return ed.rows * 20; } });   // a page that lays the box out: 20 px a row
  ed.scrollHeight = 9 * 20;                    // what one long unbroken line needs at this width
  ed.type("x".repeat(4000));
  got.wrapped = ed.rows;
  ed.scrollHeight = 40 * 20;
  ed.type("y".repeat(9000));
  got.veryLong = ed.rows;
  ed.scrollHeight = 2 * 20;
  ed.type("short");
  got.short = ed.rows;
  console.log(JSON.stringify(got));
})();
""")
        self.assertEqual(out, {"wrapped": 9, "veryLong": 16, "short": 5})

    def test_rows_that_came_back_say_how_many_and_the_block_with_the_answer_is_scrolled_into_view(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  got.before = snapshot();
  answers.push(rows(3)); await press("Run SQL");
  got.three = snapshot();
  answers.push(rows(1)); await press("Run SQL");
  got.one = snapshot();
  got.calls = calls.map((c) => [c[0], c[1], c[2] && c[2].confirm_destructive]);
  console.log(JSON.stringify(got));
})();
""")
        self.assertEqual(out["before"]["status"], "Authorized live-write sandbox. Only lab schema objects may be changed.")
        self.assertEqual(out["before"]["results"], "Run a query to see results.")
        for key, said, n in (("three", "Done. 3 rows.", 3), ("one", "Done. 1 row.", 1)):
            got = out[key]
            self.assertEqual((got["status"], got["statusClass"], got["tables"], got["dataRows"], got["notices"]), (said, "lab-message", 1, n, 0), key)
        self.assertEqual((out["one"]["feedbackScrolled"], out["one"]["statusScrolled"], out["one"]["outputScrolled"]), (2, 0, 0),
                         "the block that holds the summary and the results is scrolled to, not one part of it")
        self.assertEqual(out["calls"][0], ["/api/labs/vector", None, None])
        self.assertEqual(out["calls"][1], ["/api/labs/vector/run", "POST", False])

    def test_a_query_that_returned_no_rows_says_so_under_its_column_names(self):
        out = run_node("""
(async () => {
  await open();
  answers.push(rows(0)); await press("Run SQL");
  console.log(JSON.stringify(snapshot()));
})();
""")
        self.assertEqual((out["status"], out["tables"], out["dataRows"], out["notices"]), ("Done. 0 rows.", 1, 0, 0))
        self.assertEqual(out["results"], "Id Name No rows were returned.")

    def test_more_rows_than_the_limit_are_said_in_the_status_line(self):
        out = run_node("""
(async () => {
  await open();
  answers.push({ ...rows(2), truncated: true }); await press("Run SQL");
  console.log(JSON.stringify(snapshot()));
})();
""")
        self.assertEqual(out["status"], "Done. Showing the first 200 rows; there are more.")
        self.assertNotIn("Showing only", out["results"])
        self.assertEqual(out["tables"], 1)

    def test_a_statement_that_returns_no_rows_says_it_ran_without_errors(self):
        out = run_node("""
(async () => {
  await open();
  answers.push(noResultSet); await press("Run SQL");
  console.log(JSON.stringify(snapshot()));
})();
""")
        self.assertEqual((out["status"], out["statusClass"]), ("Done.", "lab-message"))
        self.assertEqual(out["results"], "Ran without errors. No result set came back.")
        self.assertEqual((out["tables"], out["notices"]), (0, 0))

    def test_a_refusal_by_the_database_replaces_the_old_result_and_says_what_the_database_said(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  answers.push(rows(3)); await press("Run SQL");
  got.good = snapshot();
  answers.push(refusal("There is already an object named 'Products' in the database. (error 2714)", 2714)); await press("Run SQL");
  got.exists = snapshot();
  answers.push(refusal("Incorrect syntax near 'FORM'. (error 102)", 102)); await press("Run SQL");
  got.syntax = snapshot();
  answers.push(refusal("The database refused it.", null)); await press("Run SQL");
  got.noNumber = snapshot();
  answers.push(rows(2)); await press("Run SQL");
  got.recovered = snapshot();
  console.log(JSON.stringify(got));
})();
""")
        self.assertEqual(out["good"]["tables"], 1)
        exists = out["exists"]
        self.assertEqual((exists["status"], exists["statusClass"]), ("That did not work.", "lab-message bad"))
        self.assertEqual((exists["tables"], exists["notices"], exists["alerts"]), (0, 1, 1), "no earlier result stays beside a failure")
        self.assertEqual(exists["results"], "The database said: There is already an object named 'Products' in the database. (error 2714) " + EXISTS)
        self.assertEqual(out["syntax"]["results"], "The database said: Incorrect syntax near 'FORM'. (error 102) " + AGAIN)
        self.assertEqual(out["noNumber"]["advice"], [AGAIN])
        for key in ("exists", "syntax", "noNumber"):
            for noise in DRIVER_NOISE:
                self.assertNotIn(noise, out[key]["results"])
        self.assertEqual((out["recovered"]["status"], out["recovered"]["statusClass"], out["recovered"]["notices"]), ("Done. 2 rows.", "lab-message", 0))

    def test_a_query_stopped_after_20_seconds_is_a_failure_not_a_completed_run(self):
        out = run_node("""
(async () => {
  await open();
  answers.push({ columns: [], rows: [], truncated: false, message: "Your query ran longer than 20 seconds.", timed_out: true }); await press("Run SQL");
  console.log(JSON.stringify(snapshot()));
})();
""")
        self.assertEqual(out["status"], "That did not work.")
        self.assertEqual(out["results"], "Your query ran longer than 20 seconds. " + SIMPLER)
        self.assertNotIn("Ran without errors", out["results"])

    def test_a_failure_that_is_not_a_fault_in_the_sql_does_not_tell_him_to_change_the_sql(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  for (const [name, e] of [
    ["unavailable", failure("The database is unavailable. Try again.", 503)],
    ["network", failure("Dojo could not be reached. If you were away for a while, your sign-in may have expired: reload the page.")],
    ["tooLong", failure("Enter SQL up to 8 KB.", 422)],
    ["empty", failure("Enter some SQL to run.", 422)],
    ["busy", failure("A lab query is already running. Wait for it to finish.", 422)],
    ["collision", failure("The fixture name is used by a different object. Press Reset this lab.", 422)],
    ["noMessage", failure("The run could not be completed. No message came back from the database.", 422)],
    ["none", failure("", 500)],
  ]) { answers.push(e); await press("Run SQL"); got[name] = snapshot(); }
  console.log(JSON.stringify(got));
})();
""")
        for key, out_ in out.items():
            self.assertEqual((out_["status"], out_["statusClass"], out_["notices"], out_["advice"], out_["tables"]), ("That did not work.", "lab-message bad", 1, [], 0), key)
            self.assertNotIn("The database said", out_["results"], key)
        self.assertEqual(out["tooLong"]["results"], "Enter SQL up to 8 KB.")
        self.assertEqual(out["collision"]["results"], "The fixture name is used by a different object. Press Reset this lab.")
        self.assertEqual(out["none"]["results"], "No reason came back. Try again.")

    def test_a_database_that_does_not_answer_is_asked_again_while_the_results_wait_and_then_the_result_shows(self):
        out = run_node("""
(async () => {
  await open();
  answers.push(rows(3)); await press("Run SQL");
  answers.push(failure("The lab database did not answer. Try again in a minute.", 503), rows(1));
  await press("Run SQL");
  console.log(JSON.stringify({ snap: snapshot(), seen, runs: calls.filter((c) => c[0].endsWith("/run")).length }));
})();
""")
        self.assertEqual(out["seen"], [{"status": "The lab database did not answer. Trying again…", "results": "Waiting for the database…", "checker": START}],
                         "the old table is gone while the new run waits")
        self.assertEqual(out["runs"], 3)
        self.assertEqual((out["snap"]["status"], out["snap"]["notices"], out["snap"]["tables"]), ("Done. 1 row.", 0, 1))

    def test_a_failure_of_a_run_or_a_reset_is_not_the_news_any_more_when_a_check_starts(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  answers.push(failure("The database is unavailable. Try again.", 503)); await press("Run SQL");
  got.failedRun = snapshot();
  answers.push(passed); await press("Check my work");
  got.afterCheck = snapshot();
  answers.push(rows(2)); await press("Run SQL");
  answers.push(failure("Reset failed: Cannot drop a system-versioned table (error 13552)", 422)); await press("Reset this lab");
  got.failedReset = snapshot();
  answers.push({ passed: false, message: "Not yet. Check your table or saved results." }); await press("Check my work");
  got.afterCheck2 = snapshot();
  console.log(JSON.stringify(got));
})();
""")
        idle = "Authorized live-write sandbox. Only lab schema objects may be changed."
        self.assertEqual((out["failedRun"]["status"], out["failedRun"]["notices"]), ("That did not work.", 1))
        for key in ("afterCheck", "afterCheck2"):
            got = out[key]
            self.assertEqual((got["status"], got["statusClass"], got["results"], got["notices"], got["tables"]), (idle, "lab-message", "Run a query to see results.", 0, 0), key)
        self.assertIn("Verified in Azure SQL", out["afterCheck"]["checker"])
        self.assertIn("Not yet", out["afterCheck2"]["checker"])
        self.assertEqual(out["failedReset"]["notices"], 1)

    def test_a_check_leaves_the_results_of_a_run_that_worked(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  got.live = [checker().attrs.role, checker().attrs["aria-live"]];
  answers.push(rows(2)); await press("Run SQL");
  got.run = snapshot();
  answers.push(passed); await press("Check my work");
  got.passed = snapshot();
  answers.push({ passed: false, message: "Not yet. Check your table or saved results." }); await press("Check my work");
  got.notYet = snapshot();
  answers.push(failure("The check could not run: Error converting data type nvarchar to float. (error 8114)", 422)); await press("Check my work");
  got.refused = snapshot();
  answers.push(failure("The database is unavailable. Try again.", 503)); await press("Check my work");
  got.unavailable = snapshot();
  console.log(JSON.stringify(got));
})();
""")
        self.assertEqual(out["live"], ["status", "polite"], "what a Check says is announced where it is said")
        for key in ("passed", "notYet", "refused", "unavailable"):
            got = out[key]
            self.assertEqual((got["status"], got["statusClass"], got["tables"]), ("Done. 2 rows.", "lab-message", 1), f"{key}: the Run's line and results stay")
        self.assertIn("Verified in Azure SQL", out["passed"]["checker"])
        self.assertEqual((out["passed"]["checkerScrolled"], out["passed"]["feedbackScrolled"] - out["run"]["feedbackScrolled"]), (2, 0),
                         "a Check scrolls to the Checker, at its start and at its end, and never to the Run's block")
        self.assertIn("Not yet", out["notYet"]["checker"])
        refused = out["refused"]
        self.assertEqual((refused["alerts"], refused["checker"]), (1, "The check could not run: Error converting data type nvarchar to float. (error 8114)"))
        self.assertEqual(out["unavailable"]["checker"], "The database is unavailable. Try again.")

    def test_a_run_or_a_reset_clears_a_check_that_no_longer_describes_the_lab(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  answers.push(passed); await press("Check my work");
  got.verified = snapshot().checker;
  answers.push(rows(1)); await press("Run SQL");
  got.afterRun = snapshot().checker;
  answers.push(passed); await press("Check my work");
  answers.push({ message: "Only this lab was reset to its starting state." }); await press("Reset this lab");
  got.afterReset = snapshot();
  answers.push(rows(1)); await press("Run SQL");
  got.untouched = snapshot().checker;
  console.log(JSON.stringify(got));
})();
""")
        self.assertIn("Verified in Azure SQL", out["verified"])
        self.assertEqual(out["afterRun"], START, "a run may have changed the lab: the check before it is gone")
        self.assertEqual(out["afterReset"]["checker"], START)
        self.assertEqual(out["afterReset"]["status"], "Only this lab was reset to its starting state.")
        self.assertEqual(out["afterReset"]["results"], "This lab is back at its starting state. You can run SQL again.")
        self.assertEqual(out["untouched"], START)

    def test_a_check_that_waits_or_asks_again_says_so_in_the_checker(self):
        out = run_node("""
(async () => {
  await open();
  answers.push(rows(1)); await press("Run SQL");
  answers.push(failure("The lab database did not answer. Try again in a minute.", 503), passed);
  await press("Check my work");
  console.log(JSON.stringify({ seen, snap: snapshot() }));
})();
""")
        self.assertEqual(out["seen"], [{"status": "Done. 1 row.", "results": "Id Name 1 p1", "checker": "The lab database did not answer. Trying again…"}])
        self.assertIn("Verified in Azure SQL", out["snap"]["checker"])
        self.assertEqual(out["snap"]["status"], "Done. 1 row.")

    def test_a_reset_that_failed_says_so_where_its_success_would_have_been_said(self):
        out = run_node("""
(async () => {
  await open();
  answers.push(rows(2)); await press("Run SQL");
  answers.push(failure("Reset failed: Cannot drop a system-versioned table (error 13552)", 422)); await press("Reset this lab");
  const failed = snapshot();
  console.log(JSON.stringify({ failed, confirms: confirms.length, scrolled: feedback().scrolled }));
})();
""")
        failed = out["failed"]
        self.assertEqual((failed["status"], failed["statusClass"], failed["tables"], failed["notices"]), ("That did not work.", "lab-message bad", 0, 1))
        self.assertEqual(failed["results"], "Reset failed: Cannot drop a system-versioned table (error 13552)")
        self.assertEqual((out["confirms"], out["scrolled"]), (1, 2))

    def test_a_hint_that_finishes_does_not_release_the_buttons_of_a_run_that_has_not(self):
        out = run_node("""
(async () => {
  const got = {};
  await open();
  const controls = () => ["Run SQL", "Check my work", "Reset this lab"].map((label) => button(label).disabled);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  answers.push(() => gate.then(() => rows(3)));      // the Run, with its answer still on the way
  button("Run SQL").press(); await tick();
  got.running = controls();
  answers.push({ hint: "Think about the type." });
  button("Show hint 1").press(); await tick(); await tick();
  got.afterHint = controls();
  got.hintShown = words(all(view, isCls("lab-hints"))[0]);
  got.statusWhileRunning = statusLine().textContent.trim();
  release(); await tick(); await tick();
  got.finished = controls();
  got.final = snapshot();
  answers.push({ hint: "Another." });
  button("Show hint 2").press(); await tick(); await tick();
  got.afterSecondHint = controls();
  console.log(JSON.stringify(got));
})();
""")
        self.assertEqual(out["running"], [True, True, True])
        self.assertEqual(out["afterHint"], [True, True, True], "the Run has not answered: its buttons stay held, or a second press is answered by 'already running'")
        self.assertEqual(out["hintShown"], "Think about the type.")
        self.assertEqual(out["finished"], [False, False, False])
        self.assertEqual((out["final"]["status"], out["final"]["tables"], out["final"]["notices"]), ("Done. 3 rows.", 1, 0))
        self.assertEqual(out["afterSecondHint"], [False, False, False], "with nothing running, a hint leaves the buttons free")

    def test_the_button_gets_its_focus_back_and_nothing_stays_disabled(self):
        out = run_node("""
(async () => {
  await open();
  answers.push(rows(1));
  button("Run SQL").press(); await tick(); await tick();
  console.log(JSON.stringify({ focused: button("Run SQL").focused, snap: snapshot() }));
})();
""")
        self.assertEqual(out["focused"], 1)
        self.assertEqual(out["snap"]["disabled"], [False, False, False, False, False])

    def test_dangerous_sql_still_asks_first_and_nothing_is_sent_when_he_says_no(self):
        out = run_node("""
(async () => {
  await open();
  answers.push(rows(2)); await press("Run SQL");
  editor().type("DROP TABLE Products;");
  confirmAnswer = false;
  await press("Run SQL");
  console.log(JSON.stringify({ confirms: confirms.length, runs: calls.filter((c) => c[0].endsWith("/run")).length, snap: snapshot() }));
})();
""")
        self.assertEqual((out["confirms"], out["runs"]), (1, 1))
        self.assertEqual((out["snap"]["status"], out["snap"]["tables"]), ("Done. 2 rows.", 1), "saying no leaves the last answer alone")

    def test_the_local_demo_keeps_its_own_words(self):
        out = run_node("""
(async () => {
  const got = {};
  await open({ local: true });
  got.before = snapshot();
  answers.push({ columns: ["mode", "SQL bytes"], rows: [["Local demo, no database", "40"]], truncated: false, message: "Demo only. No SQL was executed." });
  button("Run SQL").press();
  got.during = { status: statusLine().textContent.trim(), results: words(output()) };
  await tick(); await tick();
  got.after = snapshot();
  answers.push({ columns: [], rows: [], truncated: false, message: "Demo only. No SQL was executed." }); await press("Run SQL");
  got.noRows = snapshot();
  console.log(JSON.stringify(got));
})();
""")
        self.assertEqual(out["before"]["status"], "Local demo: no database. Checks here cannot verify work.")
        self.assertEqual(out["during"], {"status": "Running the local demo…", "results": "Running the local demo…"})
        self.assertEqual((out["after"]["status"], out["after"]["tables"], out["after"]["dataRows"]), ("Demo only. No SQL was executed.", 1, 1))
        self.assertEqual(out["noRows"]["status"], "Demo only. No SQL was executed.")

    def test_the_words_of_a_failure_are_fixed_here_and_never_say_what_is_not_true(self):
        out = run_node("""
console.log(JSON.stringify({ failed: P.LAB_FAILED, again: P.LAB_AGAIN, exists: P.LAB_EXISTS, simpler: P.LAB_SIMPLER,
  timedOut: P.labFailure({ message: "m", timedOut: true }), sql: P.labFailure({ message: "m", kind: "sql", number: 2714 }), plain: P.labFailure({ message: "m", status: 422 }) }));
""")
        self.assertEqual((out["failed"], out["again"], out["exists"], out["simpler"]), ("That did not work.", AGAIN, EXISTS, SIMPLER))
        self.assertEqual(out["timedOut"]["advice"], SIMPLER)
        self.assertEqual((out["sql"]["problem"], out["sql"]["advice"]), ("The database said: m", EXISTS))
        self.assertEqual((out["plain"]["problem"], out["plain"]["advice"]), ("m", None))
        for said in (out["failed"], out["again"], out["exists"], out["simpler"]):
            self.assertNotIn("did not run", said, "a query that timed out, or a batch that failed half way, did run")
        for said in (out["exists"], out["simpler"]):
            self.assertNotIn("Leave out", said, "that the object that is there is the right one is not known")


if __name__ == "__main__":
    unittest.main()
