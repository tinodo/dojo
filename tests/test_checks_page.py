"""Studio's source-change history and its Health tab are pages of a few screens, and what the owner has to press is never behind a closed
section (ADR 0024, ADR 0025).

The parts of app.js that draw them run in node, with a page that keeps the elements it is told to make, so the tests look at the
tree the page would get and not at text: a row behind "Show the other N" is in the tree and must not be counted as shown."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text("utf-8")
APP_CSS = (ROOT / "app" / "static" / "app.css").read_text("utf-8")
NODE = shutil.which("node")


def contrast(a: str, b: str) -> float:
    """The WCAG contrast ratio of two #RRGGBB colours."""
    def luminance(hexcolor: str) -> float:
        channels = [int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const APP = fs.readFileSync(PATH, 'utf8');
const cut = (from, to) => { const a = APP.indexOf(from), b = APP.indexOf(to, a); if (a < 0 || b < a) throw new Error("not where it was: " + from); return APP.slice(a, b); };
const DRIFT = cut("// A long list shows its first rows", "// ---------------------------------------------------------------- Studio: official pages for every skill (ADR 0018)");
const HEALTH = cut("// ---------------------------------------------------------------- Health", "// What Dojo's own voices actually said (EG-19)");

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
vm.runInContext(DRIFT + "\nthis.part = { OPEN_MORE, capped, driftRow, driftChanges, driftChange, filmHistory };", context);
const P = context.part;
// The tab on its own: every block it draws is a marker that says which it is, so the order of the page is what is looked at.
const studioHealth = async (api, opts) => {
  const tab = vm.createContext({
    h, icon: () => null, chip: (label, kind) => h("span", { chip: kind }, label), eyebrow: (icon, label) => h("p", { eyebrow: icon }, label),
    panel: (...kids) => h("section", { class: "panel" }, ...kids), plural: (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`,
    fmtDate: (s) => `D(${s})`, PIPE: [["01", "Read", "n", "x"]], NOT_A_PREDICTION: "n", OPEN_MORE: P.OPEN_MORE, api,
    voicesPanel: () => mark("voices"), driftRow: () => mark("drift"), deeperRow: () => mark("deeper"), sourcingRow: () => mark("sourcing"),
    labRow: () => mark("lab"), document: { getElementById: () => null }, Date, fmtDay: (s) => `Day(${s})`,
  });
  return vm.runInContext(HEALTH + `\nstudioHealth(${JSON.stringify(opts || {})});`, tab);
};
const voices = (flagged) => ({ lessons: 9, listen: { lessons: 9, flagged }, watch: { flagged: 0 }, deep: { flagged: 0 } });
const deeperView = (failures) => ({ today: { used: 3 }, limits: { per_day: 20 }, failures: failures || [] });
const sources = (ok) => ({ exams: [], web_search: { ok } });
const tabApi = async (path, over) => {
  const o = Object.assign({}, tabApiOver);
  if (path in o) { const v = o[path]; if (v instanceof Error) throw v; return v; }
  return path === "/api/quality" ? [] : path === "/api/about" ? { models: { author: "a", gate: "g", grader: "r" } } : path === "/api/studio/voices" ? voices(0)
    : path === "/api/studio/drift" ? drift([]) : path === "/api/studio/deeper" ? deeperView() : path === "/api/studio/sourcing" ? sources(true)
    : path === "/api/studio/courses" ? { courses: [], todo: { labs: [], stuck: [], builds: [] } } : { on: true };
};
let tabApiOver = {};
const tick = () => new Promise((r) => setTimeout(r, 5));

const change = (i, extra) => ({ kind: "changed", date: `2026-10-${String(10 + (i % 15)).padStart(2, "0")}`, url: `https://learn.microsoft.com/p${i}`, title: `Page ${i}`,
  publisher: "Microsoft", removed: [], removed_total: 0, lessons: [], labs: [], items: {}, pool: {}, ...extra });
const lab = (extra) => ({ id: "vector", title: "Make a vector product table", skill: "d1.s1", ...extra });
const estimate = { usd: 1.23, minutes: 4, pace: "measured", voice_priced: true, label: "List prices." };
const lesson = (i, extra) => ({ id: `lesson-${i}`, title: `Lesson ${i}`, skill: `s${i}`, characters: 1000 + i, why: "changed", ...extra });
const film = (i, extra) => ({ id: `lesson-${i}`, title: `Film ${i}`, skill: `s${i}`, state: "done", ...extra });
const drift = (changes, extra) => ({
  pages: { cited: 9, gone: 0, moved: 0, unchecked: 0, read_today: 1, last_read: "2026-10-19T10:00:00Z", waiting: [] },
  limits: { per_day: 40, gap_minutes: 5, every_hours: 20 }, status: {}, started: true, rewrites: null, rechecking: 0, labs: [], stuck: [], changes, formatting: [],
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

    def test_a_lesson_that_waits_for_a_new_source_is_named_above_the_history_and_not_lost_in_it(self):
        out = run_node("""
const stuck = (n) => Array.from({ length: n }, (_, i) => ({ id: `lesson-w${i}`, package: "gh-300", skill: `d1.g1.s${i}`, title: `Waiting lesson ${i}` }));
const waiting = (e) => cls(e).includes("near") && text(e).includes("Waiting lesson");
const run = (n, changes) => {
  const host = P.driftRow(drift(changes, { stuck: stuck(n) }));
  const everything = all(host, () => true);
  return [outside(host, waiting).length, all(host, isTag("summary")).map(text),
          changes.length ? everything.findIndex(waiting) < everything.findIndex(isCls("dchange")) : null];
};
const many = Array.from({ length: 30 }, (_, i) => change(i));
console.log(JSON.stringify([run(3, []), run(6, []), run(8, []), run(8, many)]));""")
        self.assertEqual(out, [[3, [], None], [6, [], None], [5, ["Show the other 3 lessons waiting for a source"], None],
                               [5, ["Show the other 3 lessons waiting for a source", "Show the other 26 earlier changes"], True]],
                         "named without any change to carry them, five shown, and above the history")

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
  await studioHealth(tabApi);
  console.log(JSON.stringify([Object.keys(P.OPEN_MORE).length, all(P.driftRow(d), isTag("details"))[0].attrs.open || false]));
})();""")
        self.assertEqual(out, [0, False])


@unittest.skipUnless(NODE, "node is not installed")
class FilmHistoryTests(unittest.TestCase):
    """Approving a film is on the To do page (ADR 0025); what became of the films that were approved stays with the source changes."""

    def test_twelve_approved_films_show_three_and_say_what_became_of_each(self):
        out = run_node("""
const films = Array.from({ length: 12 }, (_, i) => film(i, i === 11 ? { state: "failed", error: "No room." } : {}));
const section = P.filmHistory(drift([], { refilm: { lessons: [], estimate, films, approvals: [] } }));
console.log(JSON.stringify([outside(section, isCls("near")).length, all(section, isTag("details")).flatMap((d) => all(d, isCls("near"))).length,
  all(section, isTag("summary")).map(text), all(section, isTag("button")).length]));""")
        self.assertEqual(out, [3, 9, ["Show the other 9 earlier films"], 0], "no button here: a film is approved on the To do page")

    def test_with_nothing_approved_it_points_to_the_page_where_videos_are_decided(self):
        out = run_node("""
const approved = P.filmHistory(drift([], { refilm: { lessons: [], estimate, films: [], approvals: [{ at: "2026-10-19T10:00:00Z", lessons: ["a", "b"], usd: 3.5 }] } }));
const none = P.filmHistory(drift([], { refilm: { lessons: [], estimate, films: [], approvals: [] } }));
console.log(JSON.stringify([text(approved), text(none)]));""")
        self.assertIn("D(2026-10-19T10:00:00Z), 2 lessons, about $3.5", out[0])
        self.assertIn("Videos are decided on the To do page", out[1])


@unittest.skipUnless(NODE, "node is not installed")
class HealthTabTests(unittest.TestCase):
    def test_the_summary_comes_first_then_the_lab_switch_then_the_closed_sections(self):
        out = run_node("""
(async () => {
  const body = await studioHealth(tabApi);
  const names = (el) => (el.tag === "details" ? el.attrs.id : el.attrs.marker || (cls(el).includes("panel") ? "summary" : "?"));
  console.log(JSON.stringify([body.flat(Infinity).map(names), all({ tag: "x", attrs: {}, kids: body.flat(Infinity) }, isTag("details")).map((d) => !!d.attrs.open)]));
})();""")
        self.assertEqual(out[0], ["summary", "lab", "health-changes", "health-voices", "health-rewrites", "health-sources", "health-blocked"])
        self.assertEqual(out[1], [False] * 5, "every section starts closed")

    def test_the_old_blocked_text_link_opens_its_section(self):
        out = run_node("""
(async () => {
  const body = await studioHealth(tabApi, { open: "blocked" });
  const open = all({ tag: "x", attrs: {}, kids: body.flat(Infinity) }, isTag("details")).filter((d) => d.attrs.open).map((d) => d.attrs.id);
  console.log(JSON.stringify(open));
})();""")
        self.assertEqual(out, ["health-blocked"])

    def test_the_summary_is_honest_about_what_it_could_and_could_not_read(self):
        out = run_node("""
(async () => {
  const sentence = async (over) => { tabApiOver = over; const body = await studioHealth(tabApi); return text(body.flat(Infinity)[0]); };
  const fine = await sentence({});
  const labText = await sentence({ "/api/studio/drift": drift([], { labs: [lab()] }) });
  const unread = await sentence({ "/api/studio/voices": new Error("boom"), "/api/studio/sourcing": new Error("down") });
  const both = await sentence({ "/api/studio/drift": drift([], { labs: [lab()], stuck: [{ id: "l", package: "p", skill: "s", title: "T" }] }), "/api/studio/voices": new Error("boom") });
  const stale = await sentence({ "/api/studio/drift": drift([], { pages: { cited: 9, gone: 0, moved: 0, unchecked: 0, read_today: 0, last_read: new Date(Date.now() - 3 * 86400000).toISOString(), waiting: [] } }) });
  const web = await sentence({ "/api/studio/sourcing": sources(false) });
  console.log(JSON.stringify([fine, labText, unread, both, stale, web]));
})();""")
        fine, lab_text, unread, both, stale, web = out
        self.assertIn("Everything Dojo checked is working.", fine)
        self.assertNotIn("could not be read", fine)
        self.assertIn("1 lab to look at.", lab_text)
        self.assertNotIn("working", lab_text)
        self.assertIn("Everything Dojo could read is working.", unread, "a check that could not be read is not a thing to look at")
        self.assertIn("2 of 7 checks could not be read: voices, lesson sources.", unread)
        self.assertIn("2 things to look at.", both)
        self.assertIn("1 of 7 checks could not be read: voices.", both)
        self.assertIn("The daily read of the cited pages has not read a page for 3 days.", stale, "a check that stopped is not 'fine'")
        self.assertIn("Web search for finding pages does not work.", web)

    def test_slides_worth_a_listen_are_information_not_a_thing_to_look_at(self):
        out = run_node("""
(async () => {
  tabApiOver = { "/api/studio/voices": voices(171) };
  const body = await studioHealth(tabApi);
  const sections = all({ tag: "x", attrs: {}, kids: body.flat(Infinity) }, isTag("details"));
  const voicesChip = all(sections.find((d) => d.attrs.id === "health-voices"), (e) => e.attrs && e.attrs.chip !== undefined).map((e) => [text(e), e.attrs.chip]);
  console.log(JSON.stringify([text(body.flat(Infinity)[0]), voicesChip, text(sections.find((d) => d.attrs.id === "health-voices").kids[0])]));
})();""")
        self.assertIn("Everything Dojo checked is working.", out[0])
        self.assertEqual(out[1], [["for information", "line"]])
        self.assertIn("171 passages were read a little differently from the text", out[2])

    def test_the_voice_line_says_out_of_how_many(self):
        out = run_node("""
(async () => {
  tabApiOver = { "/api/studio/voices": { lessons: 9, listen: { lessons: 9, flagged: 120, slides: 1180 }, watch: { flagged: 20, turns: 60 }, deep: { flagged: 31, parts: 700 } } };
  const body = await studioHealth(tabApi);
  console.log(JSON.stringify(text(all({ tag: "x", attrs: {}, kids: body.flat(Infinity) }, isTag("details")).find((d) => d.attrs.id === "health-voices").kids[0])));
})();""")
        self.assertIn("9 lessons heard again; 171 of 1940 passages were read a little differently from the text.", out)

    def test_what_needs_the_owner_on_the_to_do_page_is_not_called_fine_here(self):
        out = run_node("""
(async () => {
  const sentence = async (over) => { tabApiOver = over; const body = await studioHealth(tabApi); return text(body.flat(Infinity)[0]); };
  const course = (failed) => ({ package: "p", exam: "XY-110", make: { videos: { failed } } });
  const build = await sentence({ "/api/studio/courses": { courses: [], todo: { labs: [], stuck: [], builds: [{ package: "xy", exam: "XY-110", state: "stopped" }] } } });
  const films = await sentence({ "/api/studio/courses": { courses: [course(2)], todo: { labs: [], stuck: [], builds: [] } } });
  const monitoring = await sentence({ "/api/studio/lab": { on: true, demo: false, monitoring: { observed: "on", error: "database waking" } } });
  const clear = await sentence({});
  console.log(JSON.stringify([build, films, monitoring, clear]));
})();""")
        build, films, monitoring, clear = out
        self.assertIn("The build of XY-110 stopped.", build)
        self.assertIn("2 films did not work last time.", films)
        self.assertIn("The last try to change the lab database's performance monitoring did not work.", monitoring)
        self.assertIn("Everything Dojo checked is working.", clear)
        self.assertRegex(clear, r"Opened \d{1,2}:\d\d(?:\s[AP]M)?\. Checked: source changes, voices, rewrites from more pages, lesson sources, blocked sentences, lab database, courses\.", "it says when the page was opened and what it checked")

    def test_the_counts_say_what_is_kept_not_the_cap_as_a_fact(self):
        out = run_node("""
(async () => {
  tabApiOver = { "/api/quality": Array.from({ length: 400 }, (_, i) => ({ kind: "lesson", part: "p", at: "2026-10-01T00:00:00Z", text: `t${i}`, reason: "r" })) };
  const body = await studioHealth(tabApi);
  const blocked = all({ tag: "x", attrs: {}, kids: body.flat(Infinity) }, isTag("details")).find((d) => d.attrs.id === "health-blocked");
  console.log(JSON.stringify(text(blocked.kids[0])));
})();""")
        self.assertIn("The last 400 are kept", out)
        self.assertIn("400 or more sentences were stopped before reaching you; the latest on Day(2026-10-01).", out)
        self.assertNotIn("400 entries", out)

    def test_the_blocked_line_says_how_many_were_stopped_and_when_or_that_none_were(self):
        out = run_node("""
(async () => {
  const line = async (log) => { tabApiOver = { "/api/quality": log }; const body = await studioHealth(tabApi);
    return text(all({ tag: "x", attrs: {}, kids: body.flat(Infinity) }, isTag("details")).find((d) => d.attrs.id === "health-blocked").kids[0]); };
  const some = [3, 1, 2].map((d) => ({ kind: "lesson", part: "p", at: `2026-10-0${d}T10:00:00Z`, text: `t${d}`, reason: "r" }));
  console.log(JSON.stringify([await line(some), await line([some[0]]), await line([])]));
})();""")
        some, one, none = out
        self.assertIn("3 sentences were stopped before reaching you; the latest on Day(2026-10-03).", some, "the latest, whatever the order of the log")
        self.assertIn("1 sentence was stopped before reaching you; the latest on Day(2026-10-03).", one, "the one entry given")
        self.assertIn("Nothing was stopped lately.", none)

    def test_a_web_search_that_was_never_tested_is_not_called_fine(self):
        out = run_node("""
(async () => {
  tabApiOver = { "/api/studio/sourcing": { exams: [{ exam: "DP-300" }], web_search: null } };
  const body = await studioHealth(tabApi);
  const sec = all({ tag: "x", attrs: {}, kids: body.flat(Infinity) }, isTag("details")).find((d) => d.attrs.id === "health-sources");
  console.log(JSON.stringify([text(sec.kids[0]), all(sec.kids[0], (e) => e.attrs && e.attrs.chip !== undefined).map((e) => e.attrs.chip)]));
})();""")
        self.assertIn("web search has not been tested yet", out[0])
        self.assertEqual(out[1], ["line"], "for information, not 'fine'")

    def test_the_log_is_a_box_that_scrolls_and_can_be_reached_by_keyboard(self):
        self.assertEqual(APP_JS.count('"panel grow stack scroll logbox", tabindex: "0", role: "region", "aria-label": "What was blocked, and why"'), 2,
                         "the owner's Health tab and a member's Studio log")
        rule = next(line for line in APP_CSS.splitlines() if line.startswith(".panel.scroll.logbox"))
        self.assertIn("max-height: min(70vh, 640px)", rule)
        ring = next(line for line in APP_CSS.splitlines() if line.startswith(".panel.logbox:focus-visible"))
        self.assertIn("outline: 2px solid var(--amber-ink)", ring)
        tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", APP_CSS[:APP_CSS.index("}")]))
        for ground in ("canvas", "card"):
            self.assertGreaterEqual(contrast(tokens["amber-ink"], tokens[ground]), 3.0, f"the ring on the {ground}: 3:1 is what a focus ring needs")
        self.assertLess(contrast(tokens["amber"], tokens["canvas"]), 3.0, "the check can tell a ring that is too light (the first version used amber)")

    def test_nothing_else_draws_the_lab_switch(self):
        self.assertEqual(APP_JS.count("labRow(lb)"), 1)
        self.assertIn("return [healthTop(parts, look), labRow(lb), sections];", APP_JS)


class WordsTests(unittest.TestCase):
    def test_the_adr_and_the_guide_say_it(self):
        adr = (ROOT / "docs" / "adr" / "0024-studio-checks-short.md").read_text("utf-8")
        for word in ("Show the other", "logbox", "d.labs", "client side", "stuck"):
            self.assertIn(word, adr)
        self.assertIn("(0024-studio-checks-short.md)", (ROOT / "docs" / "adr" / "README.md").read_text("utf-8"))
        guide = (ROOT / "docs" / "user-guide.md").read_text("utf-8")
        self.assertIn("is on the Health tab, above its sections", guide, "the switch moved with the Checks tab (ADR 0025)")
        self.assertIn("Show the other", guide)


if __name__ == "__main__":
    unittest.main()
