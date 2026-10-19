"""Studio's Checks tab is a page of a few screens, and what the owner has to press is at the top of it (ADR 0024).

The parts of app.js that draw it run in node, with a page that keeps the elements it is told to make, so the tests look at the
tree the page would get and not at text: a row behind "Show the other N" is in the tree and must not be counted as shown."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text("utf-8")
APP_CSS = (ROOT / "app" / "static" / "app.css").read_text("utf-8")
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const APP = fs.readFileSync(PATH, 'utf8');
const cut = (from, to) => { const a = APP.indexOf(from), b = APP.indexOf(to, a); if (a < 0 || b < a) throw new Error("not where it was: " + from); return APP.slice(a, b); };
const DRIFT = cut("// A long list shows its first rows", "// ---------------------------------------------------------------- Studio: official pages for every skill (ADR 0018)");
const CHECKS = cut("async function studioChecks() {", "// Costs: what building a course costs");

const flat = (list) => list.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
class El { constructor(tag, attrs) { this.tag = tag; this.attrs = attrs || {}; this.kids = []; this.textContent = ""; this.disabled = false; } }
const h = (tag, attrs, ...kids) => { const el = new El(tag, attrs); el.kids = flat(kids); return el; };
const put = (el, ...kids) => { el.kids = flat(kids); };
const cls = (el) => (typeof el === "string" ? [] : String(el.attrs.class || "").split(" "));
const text = (el) => (typeof el === "string" ? el : (el.textContent || "") + el.kids.map(text).join(" "));
// every element that matches, not looking inside a <details>: what the owner sees without pressing "Show the other N"
const outside = (el, pred) => (typeof el === "string" ? [] : [...(pred(el) ? [el] : []), ...(el.tag === "details" ? [] : el.kids.flatMap((k) => outside(k, pred)))]);
const all = (el, pred) => (typeof el === "string" ? [] : [...(pred(el) ? [el] : []), ...el.kids.flatMap((k) => all(k, pred))]);
const isCls = (name) => (e) => cls(e).includes(name);
const isTag = (name) => (e) => e.tag === name;
const mark = (name) => h("x", { marker: name });

const calls = [], asked = [];
let answers = [], confirms = [];
const context = vm.createContext({
  h, put, panel: (...kids) => h("section", { class: "panel" }, ...kids),
  chip: (label, kind) => h("span", { chip: kind }, label), eyebrow: (icon, label) => h("p", { eyebrow: icon }, label),
  icon: () => null, plural: (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`, fmtDate: (s) => `D(${s})`, fmtWhen: (s) => `W(${s})`,
  ext: (href, label) => h("a", { href }, label), button: (label, onclick, kind) => h("button", { onclick, kind }, label),
  labsOn: () => true, movedTo: () => "elsewhere", pricesAge: () => mark("age"), usd: (x) => `$${x}`,
  api: async (path, opts) => { calls.push([path, opts && opts.method, opts && opts.body]); const a = answers.shift(); if (a instanceof Error) throw a; return a; },
  window: { confirm: (message) => { asked.push(message); return confirms.length ? confirms.shift() : true; } },
});
vm.runInContext(DRIFT + "\nthis.part = { OPEN_MORE, capped, driftRow, driftChanges, driftChange, refilmPanel };", context);
const P = context.part;
// The tab on its own: every block it draws is a marker that says which it is, so the order of the page is what is looked at.
const studioChecks = async (api) => {
  const tab = vm.createContext({
    h, icon: () => null, chip: (label, kind) => h("span", { chip: kind }, label), eyebrow: (icon, label) => h("p", { eyebrow: icon }, label),
    fmtDate: (s) => `D(${s})`, PIPE: [["01", "Read", "n", "x"]], NOT_A_PREDICTION: "n", OPEN_MORE: P.OPEN_MORE, api,
    voicesPanel: () => mark("voices"), driftRow: () => mark("drift"), deeperRow: () => mark("deeper"), sourcingRow: () => mark("sourcing"),
    labRow: () => mark("lab"),
  });
  return vm.runInContext(CHECKS + "\nstudioChecks();", tab);
};
const tabApi = async (path) => (path === "/api/quality" ? [] : path === "/api/about" ? { models: { author: "a", gate: "g", grader: "r" } } : {});
const tick = () => new Promise((r) => setTimeout(r, 5));

const change = (i, extra) => ({ kind: "changed", date: `2026-10-${String(10 + (i % 15)).padStart(2, "0")}`, url: `https://learn.microsoft.com/p${i}`, title: `Page ${i}`,
  publisher: "Microsoft", removed: [], removed_total: 0, lessons: [], labs: [], items: {}, pool: {}, ...extra });
const lab = (extra) => ({ id: "vector", title: "Make a vector product table", skill: "d1.s1", ...extra });
const estimate = { usd: 1.23, minutes: 4, pace: "measured", voice_priced: true, label: "List prices." };
const lesson = (i, extra) => ({ id: `lesson-${i}`, title: `Lesson ${i}`, skill: `s${i}`, characters: 1000 + i, why: "changed", ...extra });
const film = (i, extra) => ({ id: `lesson-${i}`, title: `Film ${i}`, skill: `s${i}`, state: "done", ...extra });
const drift = (changes, extra) => ({
  pages: { cited: 9, gone: 0, moved: 0, unchecked: 0, read_today: 1, last_read: "2026-10-19T10:00:00Z", waiting: [] },
  limits: { per_day: 40, gap_minutes: 5, every_hours: 20 }, status: {}, started: true, rewrites: null, rechecking: 0, labs: [], changes, formatting: [],
  record: "Only content is written.", refilm: { lessons: [], estimate, films: [], approvals: [] }, ...extra });
"""


def run_node(script: str) -> object:
    prelude = f"const PATH = {json.dumps(str(ROOT / 'app' / 'static' / 'app.js'))};\n"
    done = subprocess.run([NODE, "-"], input=prelude + HARNESS + "\n" + script, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class SourceChangesTests(unittest.TestCase):
    def test_sixty_changes_show_four_and_keep_the_rest_behind_one_line(self):
        out = run_node("""
const host = P.driftRow(drift(Array.from({ length: 60 }, (_, i) => change(i))));
const shown = outside(host, isCls("dchange")).length, hidden = all(host, isTag("details")).flatMap((d) => all(d, isCls("dchange"))).length;
const summaries = all(host, isTag("summary")).map(text);
console.log(JSON.stringify([shown, hidden, summaries]));""")
        self.assertEqual(out, [4, 56, ["Show the other 56 earlier changes"]])

    def test_a_list_one_row_over_the_cap_shows_every_row(self):
        out = run_node("""
const sizes = [3, 4, 5, 6, 7].map((n) => {
  const host = P.driftRow(drift(Array.from({ length: n }, (_, i) => change(i))));
  return [n, outside(host, isCls("dchange")).length, all(host, isTag("details")).length];
});
console.log(JSON.stringify(sizes));""")
        self.assertEqual(out, [[3, 3, 0], [4, 4, 0], [5, 5, 0], [6, 4, 1], [7, 4, 1]], "one row is never hidden behind a line of its own")

    def test_a_lab_to_look_at_is_one_action_however_many_changes_touched_it(self):
        out = run_node("""
const flagged = lab({ now: "flagged" });
const many = Array.from({ length: 12 }, (_, i) => change(i, { labs: [flagged] }));
const buttons = (host) => outside(host, isTag("button")).map(text);
const withHistory = P.driftRow(drift(many, { labs: [lab()] }));
const evicted = P.driftRow(drift([], { labs: [lab()] }));      // the change that carried it is no longer kept
const none = P.driftRow(drift(many));                           // nothing flagged now: the history shows no button
const inside = all(withHistory, isTag("details")).flatMap((d) => all(d, isTag("button"))).length;
console.log(JSON.stringify([buttons(withHistory), inside, buttons(evicted), buttons(none)]));""")
        self.assertEqual(out, [["I looked at it"], 0, ["I looked at it"], []])

    def test_the_lab_to_look_at_comes_before_the_history(self):
        out = run_node("""
const host = P.driftRow(drift(Array.from({ length: 6 }, (_, i) => change(i)), { labs: [lab()] }));
const order = all(host, (e) => e.tag === "button" || cls(e).includes("dchange")).map((e) => e.tag === "button" ? "button" : "change");
console.log(JSON.stringify(order.slice(0, 3)));""")
        self.assertEqual(out, ["button", "change", "change"])

    def test_pressing_it_draws_the_block_again_with_the_rest_still_open(self):
        out = run_node("""
(async () => {
  const d = drift(Array.from({ length: 20 }, (_, i) => change(i)), { labs: [lab()] });
  const host = P.driftRow(d);
  const [details] = all(host, isTag("details"));
  details.attrs.ontoggle({ target: { open: true } });             // the owner opened "Show the other 16"
  answers = [drift(d.changes, { labs: [] })];
  await outside(host, isTag("button"))[0].attrs.onclick(); await tick();
  const again = all(host, isTag("details"));
  console.log(JSON.stringify([again.length, again[0].attrs.open, outside(host, isTag("button")).length]));
})();""")
        self.assertEqual(out, [1, True, 0])

    def test_a_tab_that_is_drawn_again_starts_with_the_rest_closed(self):
        out = run_node("""
(async () => {
  const d = drift(Array.from({ length: 20 }, (_, i) => change(i)));
  all(P.driftRow(d), isTag("details"))[0].attrs.ontoggle({ target: { open: true } });
  await studioChecks(tabApi);
  console.log(JSON.stringify([Object.keys(P.OPEN_MORE).length, all(P.driftRow(d), isTag("details"))[0].attrs.open || false]));
})();""")
        self.assertEqual(out, [0, False])


@unittest.skipUnless(NODE, "node is not installed")
class FilmAgainTests(unittest.TestCase):
    def test_the_button_comes_first_with_its_price_age_under_it_and_the_lessons_after(self):
        out = run_node("""
const d = drift([], { refilm: { lessons: Array.from({ length: 12 }, (_, i) => lesson(i)), estimate, films: [], approvals: [] } });
const section = P.refilmPanel(d, () => {});
const kids = section.kids.flatMap((k) => (typeof k === "string" ? [] : [k]));
const at = (pred) => kids.findIndex(pred);
const actions = at(isCls("refilm")), age = at((k) => k.attrs.marker === "age"), list = at((k) => k.tag === "div" && k.attrs.class === "top");
console.log(JSON.stringify([actions, age, list, actions < age && age < list]));""")
        self.assertEqual(out[3], True, out)
        self.assertEqual(out[1], out[0] + 1, "the price age stays right under the button")

    def test_five_lessons_show_and_the_rest_wait_but_a_failed_one_is_always_shown(self):
        out = run_node("""
const lessons = [...Array.from({ length: 11 }, (_, i) => lesson(i)), lesson(11, { error: "The film did not finish." })];   // the failed one is last
const section = P.refilmPanel(drift([], { refilm: { lessons, estimate, films: [], approvals: [] } }), () => {});
const shown = outside(section, isCls("near")).map(text), hidden = all(section, isTag("details")).flatMap((d) => all(d, isCls("near"))).length;
console.log(JSON.stringify([shown.length, shown[0], hidden, all(section, isTag("summary")).map(text)]));""")
        self.assertEqual(out[0], 6, "the failed lesson and five more")
        self.assertIn("Lesson 11", out[1])
        self.assertIn("the last try did not work", out[1])
        self.assertEqual((out[2], out[3]), (6, ["Show the other 6 more lessons"]))

    def test_what_is_approved_is_every_lesson_not_the_ones_shown(self):
        out = run_node("""
(async () => {
  const lessons = Array.from({ length: 12 }, (_, i) => lesson(i));
  const d = drift([], { refilm: { lessons, estimate, films: [], approvals: [] } });
  answers = [d];
  const section = P.refilmPanel(d, () => {});
  const button = all(section, isTag("button"))[0];
  await button.attrs.onclick(); await tick();
  console.log(JSON.stringify([calls.map((c) => [c[0], c[1], (c[2] || {}).lessons]), text(button)]));
})();""")
        self.assertEqual(out[0], [["/api/studio/drift/refilm", "POST", [f"lesson-{i}" for i in range(12)]]])
        self.assertIn("Re-film 12 changed lessons", out[1])

    def test_twelve_approved_films_show_three(self):
        out = run_node("""
const films = Array.from({ length: 12 }, (_, i) => film(i, i === 11 ? { state: "failed", error: "No room." } : {}));
const section = P.refilmPanel(drift([], { refilm: { lessons: [], estimate, films, approvals: [] } }), () => {});
console.log(JSON.stringify([outside(section, isCls("near")).length, all(section, isTag("details")).flatMap((d) => all(d, isCls("near"))).length,
  all(section, isTag("summary")).map(text)]));""")
        self.assertEqual(out, [3, 9, ["Show the other 9 earlier films"]])


@unittest.skipUnless(NODE, "node is not installed")
class ChecksTabTests(unittest.TestCase):
    def test_the_lab_switch_is_the_first_thing_on_the_tab(self):
        out = run_node("""
(async () => {
  const body = await studioChecks(tabApi);
  const first = (el) => (el.attrs.marker || (cls(el).includes("pipe") ? "pipe" : all(el, (e) => e.attrs.marker).map((e) => e.attrs.marker)[0] || cls(el).join(" ")));
  console.log(JSON.stringify(body.map(first)));
})();""")
        self.assertEqual(out, ["lab", "pipe", "voices", "drift", "deeper", "sourcing", "row stackable grow studio-log"])

    def test_the_log_is_a_box_that_scrolls_and_can_be_reached_by_keyboard(self):
        self.assertEqual(APP_JS.count('"panel grow stack scroll logbox", tabindex: "0", role: "region", "aria-label": "What was blocked, and why"'), 2,
                         "the owner's Checks tab and a member's Studio log")
        rule = next(line for line in APP_CSS.splitlines() if line.startswith(".panel.scroll.logbox"))
        self.assertIn("max-height: min(70vh, 640px)", rule)
        self.assertIn(".panel.logbox:focus-visible", APP_CSS)

    def test_nothing_else_draws_the_lab_switch(self):
        self.assertEqual(APP_JS.count("labRow(lb)"), 1)
        self.assertLess(APP_JS.index("labRow(lb), pipe,"), APP_JS.index("driftRow(dr),"))


class WordsTests(unittest.TestCase):
    def test_the_adr_and_the_guide_say_it(self):
        adr = (ROOT / "docs" / "adr" / "0024-studio-checks-short.md").read_text("utf-8")
        for word in ("Show the other", "logbox", "d.labs", "client side"):
            self.assertIn(word, adr)
        self.assertIn("(0024-studio-checks-short.md)", (ROOT / "docs" / "adr" / "README.md").read_text("utf-8"))
        guide = (ROOT / "docs" / "user-guide.md").read_text("utf-8")
        self.assertIn("is at the top of the Checks tab", guide)
        self.assertIn("Show the other", guide)


if __name__ == "__main__":
    unittest.main()
