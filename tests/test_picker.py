"""The course picker says what a page is about (ADR 0022): the one course it is about, or "All courses" on Studio, Plan and Play.

The shell code of app.js (the header, the picker, the side bar and the switch of a course) runs in node with a page that
records what it is told to do. What Studio shows, and in which order, is held by tests/test_studio.py."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text("utf-8")
NODE = shutil.which("node")

# The pages about every course, and some that are about one course or none: the exact edge of the rule.
ALL = ["#/quality", "#/quality/courses", "#/quality/activity", "#/quality/checks", "#/quality/costs", "#/plan", "#/plan/2026-10-19", "#/play"]
ONE = ["", "#/today", "#/skills", "#/rehearsal", "#/record", "#/ask", "#/check", "#/five", "#/lesson/lesson-0123456789abcdef0123/read",
       "#/skill/gh-300/d1.g1.s1", "#/add-exam", "#/members", "#/help", "#/about", "#/phone", "#/qualityx", "#/quality-log", "#/planning",
       "#/plan-b", "#/playing", "#/x/quality", "quality", "#/PLAN"]

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const APP = fs.readFileSync(PATH, 'utf8');
const START = APP.indexOf("function examWhen(");
const END = APP.indexOf("// ---------------------------------------------------------------- Today");
if (START < 0 || END < START) throw new Error("the shell code is not where it was");

// A page with only what the shell touches: elements that keep their attributes, children and text.
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.value = ""; this.classes = new Set(); this.text = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  get classList() { const c = this.classes; return { toggle: (n, on) => { if (on) c.add(n); else c.delete(n); }, contains: (n) => c.has(n) }; }
  set textContent(v) { this.text = String(v); this.kids = []; }
  get textContent() { return this.text + this.kids.map((k) => (typeof k === "string" ? k : k.textContent)).join(""); }
  all() { return [this, ...this.kids.flatMap((k) => (typeof k === "string" ? [] : k.all()))]; }
}
const reg = {};
for (const id of ["exam", "exam-pill", "exam-code", "exam-title", "exam-when", "me-initials", "side"]) reg[id] = new El(id);
const page = { hash: "#/today" };
const calls = [];
let apiImpl = async () => ({});
const h = (tag, attrs, ...kids) => {
  const el = new El(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.attrs.class = v; else if (k.startsWith("on") && typeof v === "function") el.on = v; else el.attrs[k] = String(v);
  }
  el.kids = kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false).map((k) => (k instanceof El ? k : String(k)));
  return el;
};
const put = (el, ...kids) => { el.kids = kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false); };
const withClass = (root, name) => root.all().filter((e) => (e.attrs.class || "").split(" ").includes(name));
const context = vm.createContext({
  state: null, h, put, icon: () => null, weeksTo: () => null, fmtDay: (d) => d,
  $: (id) => reg[id],
  api: (path, opts) => { calls.push(["api", opts && opts.method || "GET", path, opts && opts.body]); return apiImpl(path, opts); },
  route: () => { calls.push(["route"]); },
  location: { get hash() { return page.hash; }, set hash(v) { calls.push(["hash", v]); page.hash = v; } },
  document: { querySelectorAll: (sel) => withClass(reg.side, sel.split(".").pop()) },
});
vm.runInContext(APP.slice(START, END)
  + "\nthis.shell = { onAllCourses, renderHeader, renderSide, fillExamSelect, markNav, markScope, switchPackage, examChosen, followExam, choosePackage };", context);
const S = context.shell;

const PACKS = [
  { id: "dp-800", exam: "DP-800", title: "Developing AI-enabled database solutions" },
  { id: "gh-300", exam: "GH-300", title: "GitHub Copilot" },
  { id: "dp-300", exam: "DP-300", title: "Administering Azure SQL", added: true },
];
function start(role, enrolled) {
  context.state = { me: { name: "Tino Donders", role, enrolled, packages: PACKS, profile: { exam_dates: {}, active_package: "gh-300" } },
    pid: "gh-300", pkg: null, pkgPid: null, pickedSkill: null };
  apiImpl = async () => ({});
  calls.length = 0;
  page.hash = "#/today";
  S.fillExamSelect(); S.renderHeader(); S.renderSide();
}
// What route() does at every page change, after the hash is set.
function go(hash) { page.hash = hash; S.markNav(); S.markScope(); }
function seen() {
  const ex = withClass(reg.side, "ex");
  const name = (b) => b.kids[0].textContent;
  return {
    code: reg["exam-code"].textContent, title: reg["exam-title"].textContent, when: reg["exam-when"].textContent,
    all: reg["exam-pill"].classes.has("all"), tip: reg["exam-pill"].attrs.title, label: reg.exam.attrs["aria-label"],
    options: reg.exam.kids.map((o) => [o.attrs.value, o.textContent]), selected: reg.exam.value,
    exams: ex.map(name), current: ex.filter((b) => b.attrs["aria-current"] === "true").map(name),
  };
}
const tick = () => new Promise((r) => setTimeout(r, 5));
"""


def run_node(script: str, data: object = None) -> object:
    """Runs a script in node. It sees the data as DATA, and prints one JSON value."""
    prelude = f"const PATH = {json.dumps(str(ROOT / 'app' / 'static' / 'app.js'))};\nconst DATA = {json.dumps(data)};\n"
    done = subprocess.run([NODE, "-"], input=prelude + HARNESS + "\n" + script, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


PUT = lambda pid: ["api", "PUT", "/api/settings", {"active_package": pid}]   # noqa: E731 - what keeping an exam sends


@unittest.skipUnless(NODE, "node is not installed")
class ScopeTests(unittest.TestCase):
    def test_exactly_studio_plan_and_play_are_about_every_course(self):
        out = run_node("console.log(JSON.stringify(DATA.map((hash) => { page.hash = hash; return S.onAllCourses(); })));", ALL + ONE)
        self.assertEqual(out, [True] * len(ALL) + [False] * len(ONE), dict(zip(ALL + ONE, out)))

    def test_a_course_page_says_its_course_and_the_side_bar_marks_it(self):
        out = run_node("start('owner'); console.log(JSON.stringify(seen()));")
        self.assertEqual((out["code"], out["title"], out["when"], out["all"]), ("GH-300", "GitHub Copilot", "no date set", False))
        self.assertEqual((out["tip"], out["label"]), ("GH-300 GitHub Copilot", "Exam package"))
        self.assertEqual([v for v, _ in out["options"]], ["dp-800", "gh-300", "dp-300", "+add"])
        self.assertEqual(out["options"][1][1], "GH-300 · GitHub Copilot")
        self.assertEqual((out["selected"], out["exams"], out["current"]), ("gh-300", ["DP-800", "GH-300", "DP-300"], ["GH-300"]))

    def test_a_page_about_every_course_says_so_and_each_course_is_one_to_open(self):
        out = run_node("start('owner'); console.log(JSON.stringify(DATA.map((hash) => { go(hash); return seen(); })));", ALL)
        for hash, got in zip(ALL, out):
            self.assertEqual((got["code"], got["title"], got["when"], got["all"]), ("ALL", "All courses", "not tied to one course", True), hash)
            self.assertIn("Choose a course to open it", got["tip"], hash)
            self.assertIn("Choose a course to open it", got["label"], "the picker is described for a screen reader, not only by a tooltip")
            self.assertEqual([v for v, _ in got["options"]], ["+all", "dp-800", "gh-300", "dp-300", "+add"], hash)
            self.assertEqual([t for _, t in got["options"]][:2], ["All courses", "Open DP-800 · Developing AI-enabled database solutions"], hash)
            self.assertEqual(got["selected"], "+all", hash)
            # the exam picked stays marked in the side bar: it is the one Dojo prepares first, and Studio says so
            self.assertEqual((got["exams"], got["current"]), (["DP-800", "GH-300", "DP-300"], ["GH-300"]), hash)

    def test_the_picker_follows_the_page_both_ways(self):
        hashes = ["#/quality", "#/today", "#/plan", "#/skills", "#/play", "#/rehearsal", "#/quality/costs", "#/record"]
        out = run_node("start('owner'); console.log(JSON.stringify(DATA.map((hash) => { go(hash); const s = seen(); return [s.code, s.selected, s.label === 'Exam package']; })));", hashes)
        self.assertEqual(out, [["ALL", "+all", False], ["GH-300", "gh-300", True]] * 4)

    def test_a_member_sees_only_the_exams_chosen_and_no_way_to_add_one(self):
        out = run_node("start('learner', ['gh-300', 'dp-300']); go('#/plan'); const a = seen(); go('#/today'); console.log(JSON.stringify([a, seen()]));")
        self.assertEqual([v for v, _ in out[0]["options"]], ["+all", "gh-300", "dp-300"])
        self.assertEqual([v for v, _ in out[1]["options"]], ["gh-300", "dp-300"])
        self.assertEqual(out[0]["exams"], ["GH-300", "DP-300"])


@unittest.skipUnless(NODE, "node is not installed")
class ChoiceTests(unittest.TestCase):
    def test_choosing_a_course_on_a_page_about_one_course_switches_in_place(self):
        out = run_node("""
start('owner');
(async () => {
  const log = [];
  await S.switchPackage('gh-300'); log.push(calls.splice(0));                       // the course already chosen: nothing
  await S.switchPackage('dp-800'); log.push(calls.splice(0), context.state.pid);
  go('#/lesson/lesson-0123456789abcdef0123'); await S.switchPackage('dp-300'); log.push(calls.splice(0));   // a lesson of the old course is gone
  console.log(JSON.stringify(log));
})();""")
        self.assertEqual(out, [[], [PUT("dp-800"), ["route"]], "dp-800", [PUT("dp-300"), ["hash", "#/today"]]])

    def test_choosing_a_course_on_studio_plan_or_play_opens_it(self):
        out = run_node("""
(async () => {
  const log = [];
  for (const hash of DATA) {
    start('owner'); go(hash); await S.switchPackage('dp-800'); log.push(calls.splice(0));   // another course: it becomes the course, and opens
    start('owner'); go(hash); await S.switchPackage('gh-300'); log.push(calls.splice(0));   // the one already chosen opens too
  }
  console.log(JSON.stringify(log));
})();""", ["#/quality", "#/quality/activity", "#/plan", "#/plan/2026-10-19", "#/play"])
        for i in range(0, len(out), 2):
            self.assertEqual(out[i], [PUT("dp-800"), ["hash", "#/today"]])
            self.assertEqual(out[i + 1], [["hash", "#/today"]], "no call to the server when the course does not change")

    def test_what_the_picker_does_with_each_choice(self):
        out = run_node("""
(async () => {
  const log = [];
  const choose = async (hash, value) => {
    start('owner'); go(hash); const sel = reg.exam; sel.value = value;
    S.examChosen(sel); const now = sel.value;
    await tick(); const sent = calls.splice(0);
    go(page.hash);                              // route() draws the page the hash now names
    return [sent, now, sel.value];
  };
  log.push(await choose('#/plan', '+all'));      // what is chosen already: nothing happens
  log.push(await choose('#/plan', 'dp-800'));
  log.push(await choose('#/plan', '+add'));      // the wizard; the picker keeps saying what the page was
  log.push(await choose('#/today', '+add'));
  log.push(await choose('#/today', 'dp-800'));
  console.log(JSON.stringify(log));
})();""")
        self.assertEqual(out, [[[], "+all", "+all"],
                               [[PUT("dp-800"), ["hash", "#/today"]], "+all", "dp-800"],
                               [[["hash", "#/add-exam"]], "+all", "gh-300"],
                               [[["hash", "#/add-exam"]], "gh-300", "gh-300"],
                               [[PUT("dp-800"), ["route"]], "dp-800", "dp-800"]])

    def test_a_choice_made_on_studio_is_drawn_for_the_page_it_opens(self):
        out = run_node("""
(async () => {
  start('owner'); go('#/quality');
  await S.switchPackage('dp-800');         // the header is drawn while the hash still says Studio
  const during = seen();
  go('#/today');                            // and route() draws it again for the page the hash now names
  console.log(JSON.stringify([during, seen()]));
})();""")
        self.assertEqual((out[0]["code"], out[0]["selected"], out[0]["current"]), ("ALL", "+all", ["DP-800"]))
        self.assertEqual((out[1]["code"], out[1]["selected"], out[1]["current"]), ("DP-800", "dp-800", ["DP-800"]))

    def test_two_quick_choices_are_kept_in_the_order_they_were_made(self):
        out = run_node("""
(async () => {
  start('owner'); go('#/today');
  const pending = [];
  apiImpl = (path, opts) => new Promise((resolve) => pending.push({ pid: opts.body.active_package, resolve }));
  const a = S.switchPackage('dp-800'), b = S.switchPackage('dp-300');
  await tick();
  const first = pending.map((p) => p.pid);        // the second write waits for the first
  pending[0].resolve({}); await tick();
  const second = pending.map((p) => p.pid);
  pending[1].resolve({}); await Promise.all([a, b]);
  console.log(JSON.stringify([first, second, context.state.pid]));
})();""")
        self.assertEqual(out, [["dp-800"], ["dp-800", "dp-300"], "dp-300"])

    def test_a_late_answer_does_not_take_the_owner_off_the_page_he_went_to(self):
        out = run_node("""
(async () => {
  start('owner'); go('#/quality');
  let release;
  apiImpl = () => new Promise((resolve) => { release = resolve; });
  const done = S.switchPackage('dp-800');
  await tick();
  go('#/skills');                              // he went on while the choice was being kept
  release({}); await done;
  console.log(JSON.stringify([calls.filter((c) => c[0] !== 'api'), context.state.pid, seen().code]));
})();""")
        self.assertEqual(out, [[], "dp-800", "DP-800"], "no jump to Today, no second drawing of the page; the choice is made all the same")

    def test_a_page_about_one_of_the_exams_moves_the_picker_to_it(self):
        out = run_node("""
(async () => {
  start('owner'); go('#/skills');
  const log = [];
  S.followExam('gh-300'); await tick(); log.push(calls.splice(0));            // the exam already chosen
  S.followExam('xx-999'); await tick(); log.push(calls.splice(0));            // none of the owner's exams
  S.followExam(undefined); await tick(); log.push(calls.splice(0));
  S.followExam('dp-300'); await tick(); log.push(calls.splice(0), context.state.pid, seen().code, seen().current);
  start('learner', ['gh-300']); go('#/skills');
  S.followExam('dp-800'); await tick(); log.push(calls.splice(0), context.state.pid);   // a member does not study it
  console.log(JSON.stringify(log));
})();""")
        self.assertEqual(out, [[], [], [], [PUT("dp-300")], "dp-300", "DP-300", ["DP-300"], [], "gh-300"])


class WiringTests(unittest.TestCase):
    """The parts of app.js outside the shell slice: where the picker is drawn, read and followed."""

    def test_every_page_change_draws_the_picker_after_the_side_bar(self):
        route = APP_JS.split("async function route() {", 1)[1].split("\n}\n", 1)[0]
        self.assertRegex(route, r"markNav\(\);\s+markScope\(\);")

    def test_the_pickers_change_runs_the_one_rule(self):
        self.assertIn('sel.addEventListener("change", () => examChosen(sel));', APP_JS)
        self.assertEqual(APP_JS.count("switchPackage(sel.value)"), 1, "the one place a choice in the picker is read")

    def test_a_skill_and_a_lesson_move_the_picker_to_their_exam(self):
        skill = APP_JS.split("async function viewSkill(pid, sid) {", 1)[1].split("\n", 3)
        self.assertEqual(skill[1].strip(), "followExam(pid);")
        lesson = APP_JS.split("async function viewLesson(id, tab, section) {", 1)[1].split("\n}\n", 1)[0]
        self.assertRegex(lesson, r"const L = await api\(`/api/lessons/\$\{id\}`\);\s+followExam\(L\.package\);")

    def test_the_exam_is_kept_in_one_place(self):
        self.assertEqual(APP_JS.count('body: { active_package: pid }'), 1, "every choice goes through keepExam, which writes in order")


if __name__ == "__main__":
    unittest.main()
