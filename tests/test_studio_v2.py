"""What Studio's pages show the owner (docs/adr/0025-studio-v2.md): the words, the buttons, the order and what is left out.

The parts of app.js that draw them run in node, with a page that keeps the elements it is told to make, so these tests look at the tree
the owner would get. Nothing here touches the network."""
import json
import re
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
const STUDIO = cut("// ---------------------------------------------------------------- Studio (ADR 0019, ADR 0025)", "// ---------------------------------------------------------------- Health");
const CAPPED = cut("const OPEN_MORE = {};", "const LESSON_NOW");

const flat = (list) => list.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
class El {
  constructor(tag, attrs) { this.tag = tag; this.attrs = attrs || {}; this.kids = []; this.textContent = ""; this.disabled = !!(attrs && attrs.disabled); this.listeners = {}; this.focused = false; this.isConnected = true; this.checkedNow = undefined; this.style = { setProperty: () => {} }; }
  get checked() { return this.checkedNow === undefined ? !!this.attrs.checked : this.checkedNow; }
  set checked(v) { this.checkedNow = !!v; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, ev) { (this.listeners[type] || []).forEach((fn) => fn(ev || { target: this })); }
  replaceChildren(...kids) { this.kids = flat(kids); }
  append(...kids) { this.kids.push(...flat(kids)); }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire("close"); }
  remove() { this.removed = true; }
  focus() { this.focused = true; }
}
const h = (tag, attrs, ...kids) => { const el = new El(tag, attrs); el.kids = flat(kids); return el; };
const put = (el, ...kids) => { el.kids = flat(kids); };
const cls = (el) => (typeof el === "string" ? [] : String(el.attrs.class || "").split(" "));
const text = (el) => (typeof el === "string" ? el : (el.textContent || "") + el.kids.map(text).join(" "));
// every element that matches; `outside` does not look inside a closed <details>: what the owner sees without pressing anything
const all = (el, pred) => (typeof el === "string" ? [] : [...(pred(el) ? [el] : []), ...el.kids.flatMap((k) => all(k, pred))]);
const outside = (el, pred) => (typeof el === "string" ? [] : [...(pred(el) ? [el] : []), ...(el.tag === "details" && !el.attrs.open ? [] : el.kids.flatMap((k) => outside(k, pred)))]);
const isCls = (name) => (e) => cls(e).includes(name);
const isTag = (name) => (e) => e.tag === name;
// the words a page shows without anything being opened: a closed <details> shows only its summary
const visible = (el) => {
  if (typeof el === "string") return el;
  if (el.tag === "details" && !el.attrs.open) return el.kids.filter((k) => typeof k !== "string" && k.tag === "summary").map(text).join(" ");
  return (el.textContent || "") + el.kids.map(visible).join(" ");
};
const buttons = (el) => outside(el, (e) => e.tag === "button").map((b) => text(b).replace(/\s+/g, " ").trim());
const links = (el) => outside(el, (e) => e.tag === "a").map((a) => [text(a).replace(/\s+/g, " ").trim(), a.attrs.href]);

const calls = [], sheets = [];
let answers = [], sheetAnswers = [];
const state = { nav: 1, me: { role: "owner" } };
const parseDay = (s) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || "")); return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null; };
const documentStub = { body: new El("body"), getElementById: () => null };
const windowStub = { listeners: {}, addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }, removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }, confirm: () => { throw new Error("window.confirm must not be used to spend money"); } };
const context = vm.createContext({
  h, put, state, parseDay, console, setTimeout, clearTimeout, Intl,
  window: windowStub, document: documentStub,
  panel: (...kids) => h("section", { class: "panel" }, ...kids),
  chip: (label, kind) => h("span", { chip: kind }, label), eyebrow: (icon, label) => h("p", { eyebrow: icon }, label), icon: () => null,
  plural: (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`,
  button: (label, onclick, kind) => h("button", { onclick, kind }, label), linkButton: (label, href, kind) => h("a", { class: "btn", href, kind }, label),
  usd: (x) => (x === null || x === undefined ? "–" : `$${Number(x).toFixed(2)}`), gb: (b) => `${((b || 0) / 1e9).toFixed(1)} GB`,
  fmtDay: (s) => `Day(${s})`, fmtDate: (s) => `D(${s})`, wide: (el) => el, ext: (href, label) => h("a", { href }, label),
  labsOn: () => true, isAdmin: () => true, viewStudioLog: async () => ["log"], pageHead: (title, sub) => h("div", { class: "pageh" }, title, sub), helpQ: () => null,
  NOT_A_PREDICTION: "Not a prediction.", costsPanel: () => h("section", { class: "estimates" }), runJob: async (p) => p,
  api: async (path, opts) => { calls.push([path, opts && opts.method, opts && opts.body]); const a = answers.shift(); if (a instanceof Error) throw a; return typeof a === "function" ? a(path, opts) : a; },
});
vm.runInContext(CAPPED + "\nthis.capped = capped; this.OPEN_MORE = OPEN_MORE;", context);
vm.runInContext(STUDIO + "\nthis.part = { STUDIO_TABS, STUDIO_ALIAS, STUDIO_LEAD, money, dayOf, studioNav, viewQuality, studioTodo, studioCourses, studioMoney, todoStrip, labCard, stuckCard, buildCard, videosCard, videoRow, decksCard, pageChecksCard, reviewAll, courseCard, billPanels, groupRow, dayBars, filmLeftOut };", context);
const P = context.part;
const usd = context.usd;
// Studio's own sheet is tested on its own below: here a press on a money button is answered by the test, and what was asked is kept
context.moneySheet = async (o) => { sheets.push(o); const a = sheetAnswers.shift(); return a === undefined ? o.lines : a; };
context.studioHealth = async () => ["health"];   // the Health tab is in tests/test_checks_page.py
// moneySheet itself runs in a context of its own: a page that keeps what it is told to make
const sheetContext = vm.createContext({ h, put, usd: context.usd, window: windowStub, document: documentStub });
vm.runInContext(cut("function moneySheet(o) {", "function studioNav(at) {") + "\nthis.moneySheet = moneySheet;", sheetContext);
const realSheet = sheetContext.moneySheet;
const tick = () => new Promise((r) => setTimeout(r, 5));
const reset = () => { calls.length = 0; sheets.length = 0; answers = []; sheetAnswers = []; };

const offer = (lessons, usd, extra) => ({ lessons, videos: 2 * lessons, usd, complete: true, token: `t${lessons}`, bytes: lessons * 17e6, eta_minutes: lessons * 7, fits: true,
  replaces: { lessons: Math.floor(lessons / 2), usd: Math.round(usd * 50) / 100 }, fresh: { lessons: lessons - Math.floor(lessons / 2), usd: Math.round(usd * 50) / 100 }, ...extra });
const videos = (extra) => ({ have: 0, of: 73, waiting: 0, filming: 0, failed: 0, later: {}, quiet_until: null, quiet_last: null, offer: null, queued_bytes: 0, eta_minutes: null, ...extra });
const course = (pid, exam, extra) => ({ package: pid, exam, title: `${exam} title`, added: false, skills: 73, ready: true, rechecking: 0, verdict: "Every skill has a lesson and audio.",
  rows: [{ key: "pages", label: "Official pages", have: 73, of: 73, state: "ok", note: "n" }, { key: "lessons", label: "Lessons", have: 73, of: 73, state: "ok", note: "n" },
    { key: "listen", label: "Listen", have: 73, of: 73, state: "ok", note: "n" }, { key: "deeper", label: "Deeper Listen", have: 73, of: 73, state: "ok", note: "n" }],
  video: { own: 0, earlier: 41, none: 32, price: null }, search: null, pagecheck: null, diagrams: { ready: 4, of: 73 }, attention: [],
  missing: { deeper: 0, diagrams: 69, videos: 73, deeper_days: 0 }, make: { videos: videos() }, ...extra });
const withVideos = (c, v) => { c.make = { videos: videos(v) }; return c; };
const check = (state, pages, total) => ({ state, supported: 0, weak: 0, invalid: 0, checked: 0, unchecked: pages, not_supported: [], price: total === null ? { pages, total: null, complete: false } : { pages, total, complete: true } });
const decks = (extra) => ({ ready: 4, lessons: 208, remaining: 204, ahead: false, per_day: 20, today: 0, usd: 57.12, complete: true, days: 11, ...extra });
const data = (courses, extra) => ({ at: "2026-10-19T16:05:00+00:00", courses, todo: { labs: [], stuck: [], builds: [] },
  make: { quiet_days: 7, storage: { videos: 46, bytes: 3.2e9, budget_bytes: 6e9, limit_bytes: 5.1e9, average_bytes: 7e6, queued_bytes: 0, free_bytes: 1.9e9 }, films: {}, decks: decks({ remaining: 0, ahead: false, ready: 208 }), busy: false }, ...extra });
const bill = (extra) => ({ state: "ok", reason: null, read_at: "2026-10-19T15:50:00+00:00", stale: false, currency: "USD",
  month: { id: "2026-10", from: "2026-10-01", to: "2026-10-19", days: 19, total: 337.86 },
  groups: [{ key: "ai", label: "AI models", note: "n", total: 141.8, kind: "use", parts: [{ label: "gpt-5.5", total: 134.93, kind: "use" }, { label: "Other models", total: 6.87, kind: "use" }] },
    { key: "defender", label: "Security scanning", note: "n", total: 40.37, kind: "mixed", parts: [{ label: "Billed by how much text goes to the AI", total: 28.12, kind: "use" }, { label: "Billed per server", total: 12.25, kind: "always" }] },
    { key: "web", label: "The web app", note: "n", total: 7.9, kind: "always", parts: [] }, { key: "database", label: "Database", note: "n", total: 0, kind: "free", parts: [] }],
  fixed: { total: 24.56, per_day: 1.29, per_month: 40 }, quiet_day: { day: "2026-10-08", total: 2, per_month: 62 }, busiest: [{ day: "2026-10-15", total: 87 }],
  previous: { month: "2026-09", total: 212.4, currency: "USD" }, link: "https://portal.azure.com/#view/x", budget_link: "https://portal.azure.com/#view/b", month_floor: 353.5, current_month: "2026-10",
  days: Array.from({ length: 19 }, (_, i) => ({ day: `2026-10-${String(i + 1).padStart(2, "0")}`, total: 5 + i, partial: i === 18 })), ...extra });
"""


def run_node(script: str) -> object:
    prelude = f"const PATH = {json.dumps(str(ROOT / 'app' / 'static' / 'app.js'))};\n"
    done = subprocess.run([NODE, "-"], input=prelude + HARNESS + "\n" + script, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


# what a page may not say on its face: the words of Dojo's insides. They may be behind a closed "details".
BANNED = re.compile(r"\b(held|withheld|template|overlay|WER|token|verifier|drift|quarantine|ADR)\b", re.I)


@unittest.skipUnless(NODE, "node is not installed")
class ToDoTests(unittest.TestCase):
    def test_with_nothing_to_decide_it_says_so_and_has_no_button(self):
        out = run_node("""
(async () => {
  reset();
  answers = [data([course("dp-800", "DP-800")]), bill()];
  const host = await P.studioTodo();
  console.log(JSON.stringify([visible(host), buttons(host), outside(host, isTag("h2")).map(text)]));
})();""")
        self.assertIn("Nothing needs you right now.", out[0])
        self.assertEqual(out[1], [])
        self.assertEqual(out[2], [], "no heading over nothing")

    def test_what_needs_the_owner_comes_first_and_costs_nothing_what_costs_money_comes_after_with_its_price(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56) });
  const gh = withVideos(course("gh-300", "GH-300", { pagecheck: check("none", 98, 2.15) }), { offer: offer(31, 59.52) });
  const c = data([dp, gh], { todo: { labs: [{ id: "vector", title: "Make a vector product table", skill: "d3.g2.s3", date: "2026-10-12" }],
    stuck: [{ id: "lesson-w0", package: "gh-300", exam: "GH-300", skill: "d1.g1.s0", title: "A waiting lesson" }], builds: [{ package: "xy-110", exam: "XY-110", state: "stopped" }] } });
  c.make.decks = decks();
  answers = [c, bill()];
  const host = await P.studioTodo();
  const heads = outside(host, isTag("h2")).map(text);
  const cards = outside(host, isCls("todo-card"));
  const kinds = cards.map((card) => [text(all(card, isTag("h3"))[0]), buttons(card)]);
  console.log(JSON.stringify([heads, kinds, buttons(host), P.STUDIO_LEAD.todo]));
})();""")
        heads, kinds, order, face = out
        self.assertEqual(heads, ["Needs you", "Optional · costs money"])
        needs, optional = kinds[:3], kinds[3:]
        self.assertEqual([k[0] for k in needs], ["The build of XY-110 stopped", "Look at the lab “ Make a vector product table ”", "1 lesson can't be updated"])
        for _, buttons in needs:
            self.assertFalse(any("$" in b for b in buttons), "what needs him costs nothing")
        self.assertEqual([k[0] for k in optional], ["Videos", "Diagrams", "Page check"])
        for _, buttons in optional:
            self.assertTrue(buttons and all("about $" in b or b.startswith("Stop") for b in buttons), "every button that spends carries its price")
        everything = next(i for i, b in enumerate(order) if re.match(r"^Review all 4 · about \$249\.35$", b))
        self.assertLess(everything, order.index("Film 68 lessons · about $130.56"), "the one button for everything is the first thing in the optional group")
        self.assertGreater(everything, order.index("I looked at it"), "after what needs him, which costs nothing")
        self.assertIn("A gold button costs money and starts only when you press it, with its price on it.", face)
        self.assertIn("Lessons, audio and rewrites run on their own and cost money as they go: see Money.", face)

    def test_there_is_one_row_for_each_course_with_its_button_and_what_was_left_out(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56), later: { quiet: 4, held: 1 }, quiet_until: "2026-10-21T12:20:00+00:00", quiet_last: "2026-10-25T07:05:00+00:00" });
  const done = withVideos(course("gh-300", "GH-300"), { have: 73, of: 73 });
  answers = [data([dp, done]), bill()];
  const host = await P.studioTodo();
  const rows = outside(host, isCls("vrow"));
  console.log(JSON.stringify([rows.length, text(rows[0]), buttons(rows[0])]));
})();""")
        self.assertEqual(out[0], 1, "a course with nothing to say has no row")
        row = out[1]
        self.assertIn("DP-800", row)
        self.assertIn("68 lessons to film now", row)
        self.assertIn("None of these 68 was rewritten in the last 7 days.", row)
        self.assertIn("Left out for now: 4 lessons rewritten in the last 7 days", row)
        self.assertIn("unless rewritten again", row)
        self.assertIn("1 lesson waiting for a rewrite after a source page changed", row)
        self.assertIn("34 (about $65.28) replace the video Watch plays now, an earlier version of the lesson shown with a note; 34 (about $65.28) have no video yet.", row,
                      "how much of the money replaces a video he already has")
        self.assertEqual(out[2], ["Film 68 lessons · about $130.56"])

    def test_a_course_whose_films_could_not_be_read_says_so_and_offers_nothing(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56) });
  const gh = course("gh-300", "GH-300", { make: { videos: null } });
  answers = [data([dp, gh]), bill()];
  const host = await P.studioTodo();
  const rows = outside(host, isCls("vrow"));
  const card = outside(host, isCls("todo-card")).find((c) => text(c).includes("Videos"));
  answers = [data([gh]), bill()];
  const alone = await P.studioTodo();
  console.log(JSON.stringify([rows.length, text(card), outside(alone, isCls("todo-card")).map(text)]));
})();""")
        self.assertEqual(out[0], 1, "the course that was read has its row")
        self.assertIn("The films of GH-300 could not be read just now, so none is offered.", out[1])
        self.assertTrue(any("The films of GH-300 could not be read just now" in t for t in out[2]), "on its own, it is a card, not a blank page")

    def test_nothing_is_approved_unless_the_owner_says_yes_to_the_sheet_and_the_sheet_says_what_it_asks(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56, { eta_minutes: 476 }), later: { quiet: 4 }, quiet_until: "2026-10-21T12:20:00+00:00", quiet_last: "2026-10-21T12:20:00+00:00" });
  answers = [data([dp]), bill()];
  const host = await P.studioTodo();
  const go = all(host, isTag("button")).find((b) => text(b).includes("Film 68"));
  sheetAnswers = [null];                                   // Cancel
  await go.attrs.onclick(); await tick();
  const refused = [calls.filter((c) => c[1] === "POST").length, go.disabled];
  sheetAnswers = [undefined];                              // yes
  answers = [data([dp]), data([dp])];
  await go.attrs.onclick(); await tick();
  const ask = sheets[0];
  console.log(JSON.stringify([refused, calls.filter((c) => c[1] === "POST"), ask.title(1, 130.56), ask.confirm(1), ask.notes]));
})();""")
        refused, posts, title, confirm, notes = out
        self.assertEqual(refused, [0, False], "Cancel approves nothing and leaves the button live")
        self.assertEqual(posts, [["/api/studio/film/dp-800", "POST", {"confirm": "FILM", "offer": "t68"}]])
        self.assertEqual(title, "Film 68 lessons of DP-800? About $130.56")
        self.assertEqual(confirm, "Film 68 lessons")
        joined = " ".join(notes)
        self.assertIn("An estimate at Azure's published prices; the billed amount may differ.", joined)
        self.assertIn("34 (about $65.28) replace the video Watch plays now", joined)
        self.assertIn("Left out: 4 lessons rewritten in the last 7 days", joined)
        self.assertIn("If a lesson is rewritten after you film it, Watch plays the old video with a note", joined)
        self.assertIn("You see the price first.", joined)
        self.assertIn("of room is left before filming pauses", joined, "the room the videos need, and that it is the room before filming pauses")
        self.assertIn("a film that has started finishes and is charged", joined)

    def test_no_button_without_a_price_or_without_room(self):
        out = run_node("""
(async () => {
  reset();
  const noPrice = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56, { token: null }) });
  const noRoom = withVideos(course("gh-300", "GH-300"), { offer: offer(31, 59.52, { fits: false }) });
  answers = [data([noPrice, noRoom]), bill()];
  const host = await P.studioTodo();
  console.log(JSON.stringify([buttons(host), visible(host)]));
})();""")
        self.assertEqual(out[0], [])
        self.assertIn("The price is not available just now, so nothing can be approved.", out[1])
        self.assertIn("There is no room for these films: they need 0.5 GB and only 1.9 GB of room is left before filming pauses.", out[1])

    def test_films_being_made_show_how_many_are_left_and_can_be_stopped(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { waiting: 60, filming: 1, eta_minutes: 427, failed: 2, offer: offer(2, 3.84) });
  answers = [data([dp]), bill()];
  const host = await P.studioTodo();
  const row = outside(host, isCls("vrow"))[0];
  const stop = all(row, isTag("button")).find((b) => text(b) === "Stop the rest");
  answers = [{ ...data([dp]), stopped: 60 }];
  await stop.attrs.onclick(); await tick();
  console.log(JSON.stringify([text(row), buttons(row), calls.filter((c) => c[1] === "POST")]));
})();""")
        self.assertIn("Filming: 61 lessons to go, about 7 hours.", out[0])
        self.assertIn("2 films did not work last time. Pressing the button tries them again.", out[0])
        self.assertEqual(out[1], ["Film 2 lessons · about $3.84", "Stop the rest"])
        self.assertEqual(out[2], [["/api/studio/film/dp-800/stop", "POST", None]])

    def test_a_failed_film_does_not_point_to_a_button_that_is_not_there(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { waiting: 3, eta_minutes: 20, failed: 2 });
  answers = [data([dp]), bill()];
  const row = outside(await P.studioTodo(), isCls("vrow"))[0];
  console.log(JSON.stringify([text(row), buttons(row)]));
})();""")
        self.assertIn("2 films did not work last time.", out[0])
        self.assertNotIn("Pressing the button", out[0])
        self.assertEqual(out[1], ["Stop the rest"])

    def test_the_diagrams_card_asks_with_the_price_and_says_they_stay_on_for_later_lessons(self):
        out = run_node("""
(async () => {
  reset();
  const c = data([course("dp-800", "DP-800")]); c.make.decks = decks();
  answers = [c, bill()];
  const host = await P.studioTodo();
  const card = outside(host, isCls("todo-card"))[0];
  const go = all(card, isTag("button"))[0];
  answers = [{ results: [{ ok: true, what: "diagrams" }], ...c }];
  await go.attrs.onclick(); await tick();
  const ask = sheets[0];
  const on = data([course("dp-800", "DP-800")]); on.make.decks = decks({ ahead: true, ready: 30, remaining: 178 });
  answers = [on, bill()];
  const hostOn = await P.studioTodo();
  const unpriced = data([course("dp-800", "DP-800")]); unpriced.make.decks = decks({ usd: null, complete: false });
  answers = [unpriced, bill()];
  const hostNo = await P.studioTodo();
  console.log(JSON.stringify([text(card), buttons(card), calls.filter((c) => c[1] === "POST").map((c) => [c[0], c[2]]), ask.title(1, 57.12), ask.notes.join(" "),
    text(outside(hostOn, isCls("todo-card"))[0]), buttons(hostOn), buttons(hostNo)]));
})();""")
        card, btns, posts, title, notes, on_text, on_buttons, no_buttons = out
        self.assertEqual(btns, ["Draw 204 diagrams · about $57.12"])
        self.assertIn("up to 20 a day (about 11 days), then about $0.28 for each lesson written later, until you press Stop.", card)
        self.assertEqual(posts, [["/api/studio/make", {"confirm": "MAKE", "decks": True, "films": []}]])
        self.assertEqual(title, "Draw the diagrams ahead of time? About $57.12")
        self.assertIn("A lesson you open in Present gets its diagram at once", notes)
        self.assertIn("until you press Stop here.", notes)
        self.assertIn("Being drawn ahead of time: 30 of 208 done, up to 20 a day.", on_text)
        self.assertEqual(on_buttons, ["Stop drawing ahead of time"], "turning it off asks nothing")
        self.assertEqual(no_buttons, [], "no price, no button")

    def test_the_page_check_is_offered_for_a_course_not_checked_and_running_shows_no_button(self):
        out = run_node("""
(async () => {
  reset();
  const none = course("gh-300", "GH-300", { pagecheck: check("none", 98, 2.15) });
  const part = course("dp-800", "DP-800", { pagecheck: check("partial", 12, 0.31) });
  const running = course("xx", "XX-100", { pagecheck: check("running", 10, 0.3) });
  const done = course("yy", "YY-100", { pagecheck: { ...check("done", 0, null), price: null } });
  answers = [data([none, part, running, done]), bill()];
  const host = await P.studioTodo();
  const card = outside(host, isCls("todo-card"))[0];
  console.log(JSON.stringify([buttons(card), text(card), outside(card, isCls("vrow")).length]));
})();""")
        self.assertEqual(out[0], ["Check pages · about $2.15", "Check the rest · about $0.31"])
        self.assertIn("running now", out[1])
        self.assertEqual(out[2], 3, "a course whose check is done has no row")
        self.assertIn("Nothing changes: no page, lesson or video. The result shows on Courses.", out[1])
        self.assertIn("Two AI models read the pages a built-in course points to", out[1])

    def test_review_all_lists_every_line_and_approves_only_the_ones_left_ticked(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800", { pagecheck: check("none", 98, 2.15) }), { offer: offer(68, 130.56) });
  const gh = withVideos(course("gh-300", "GH-300"), { offer: offer(31, 59.52) });
  const c = data([dp, gh]); c.make.decks = decks();
  answers = [c, bill()];
  const host = await P.studioTodo();
  const go = all(host, isTag("button")).find((b) => text(b).startsWith("Review all"));
  sheetAnswers = [[]];   // nothing ticked is nothing approved
  await go.attrs.onclick(); await tick();
  const none = calls.filter((x) => x[1] === "POST").length;
  const ask = sheets[0];
  const lines = ask.lines.map((l) => [l.label, l.usd, l.kind]);
  sheetAnswers = [[ask.lines[0], ask.lines[2]]];   // the DP-800 films and the diagrams
  answers = [{ ...c, results: [{ ok: true, what: "films", exam: "DP-800" }, { ok: true, what: "diagrams" }] }];
  await go.attrs.onclick(); await tick();
  const posts = calls.filter((x) => x[1] === "POST");
  console.log(JSON.stringify([text(go), none, lines, ask.pick, ask.title(2, 187.68), ask.confirm(2), ask.notes.join(" "), posts.map((p) => [p[0], p[2]])]));
})();""")
        label, none, lines, pick, title, confirm, notes, posts = out
        self.assertEqual(label, "Review all 4 · about $249.35")
        self.assertEqual(none, 0)
        self.assertEqual([l[0] for l in lines], ["Film 68 lessons of DP-800 (34 replace older videos)", "Film 31 lessons of GH-300 (15 replace older videos)",
                                                 "Draw 204 diagrams, up to 20 a day", "Check the 98 pages of DP-800"])
        self.assertTrue(pick, "a box for each line")
        self.assertEqual((title, confirm), ("Spend about $187.68 on these 2?", "Start 2"))
        self.assertIn("The diagrams stay on for lessons written later, about $0.28 each and at most 20 a day, until you press “Stop drawing ahead of time” on the To do page.", notes)
        self.assertIn("Each selection starts on its own. If one is refused, the others still start and are charged.", notes)
        self.assertIn("Room for videos before filming pauses: 1.9 GB.", notes)
        self.assertEqual(posts, [["/api/studio/make", {"confirm": "MAKE", "decks": True, "films": [{"package": "dp-800", "offer": "t68"}]}]])

    def test_the_sheet_says_when_the_films_ticked_need_more_room_than_there_is_and_not_when_one_is_unticked(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56, { bytes: 8e9 }) });
  const gh = withVideos(course("gh-300", "GH-300"), { offer: offer(31, 59.52, { bytes: 5e9 }) });
  const c = data([dp, gh]); c.make.storage.free_bytes = 10e9;
  answers = [c, bill()];
  const host = await P.studioTodo();
  sheetAnswers = [null];
  await all(host, isTag("button")).find((b) => text(b).startsWith("Review all")).attrs.onclick(); await tick();
  const ask = sheets[0];
  console.log(JSON.stringify([ask.lines.map((l) => l.off), ask.warn([ask.lines[0], ask.lines[1]]), ask.warn([ask.lines[1]]), ask.warn([])]));
})();""")
        off, both, one, none = out
        self.assertEqual(off, [False, False], "each fits alone: neither is ruled out for the other")
        self.assertIn("The films ticked need about 13.0 GB and 10.0 GB of room is left before filming pauses", both)
        self.assertIsNone(one)
        self.assertIsNone(none)

    def test_review_all_compares_with_the_month_only_in_the_same_month_and_currency(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56) });
  const gh = withVideos(course("gh-300", "GH-300"), { offer: offer(31, 59.52) });
  const notes = async (b) => { reset(); answers = [data([dp, gh]), b]; const host = await P.studioTodo(); sheetAnswers = [null];
    await all(host, isTag("button")).find((x) => text(x).startsWith("Review all")).attrs.onclick(); await tick(); return sheets[0].notes.join(" "); };
  console.log(JSON.stringify([await notes(bill()), await notes(bill({ current_month: "2026-11" })), await notes(bill({ currency: "EUR" }))]));
})();""")
        same, old, euro = out
        self.assertIn("For comparison: Azure has charged $337.86 this month so far.", same)
        self.assertNotIn("For comparison", old, "an old reading is not this month")
        self.assertNotIn("For comparison", euro, "the estimates are in dollars: they are not compared with euros")

    def test_review_all_is_not_there_for_one_thing(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800"), { offer: offer(68, 130.56) });
  answers = [data([dp]), bill()];
  console.log(JSON.stringify(buttons(await P.studioTodo())));
})();""")
        self.assertEqual(out, ["Film 68 lessons · about $130.56"])

    def test_the_first_screen_says_where_the_courses_stand_and_what_the_month_cost(self):
        out = run_node("""
(async () => {
  reset();
  const ok = data([course("a", "A"), course("b", "B", { ready: false })]);
  const strip = (b) => links(P.todoStrip(ok, b)).map((l) => l.join(" -> "));
  console.log(JSON.stringify([strip(bill()), strip({ state: "not_connected" }), strip({ state: "empty" }), strip({ state: "error" }), strip(null)]));
})();""")
        self.assertEqual(out[0], ["Courses: 1 of 2 courses have every lesson and audio -> #/quality/courses", "Money: $337.86 so far this month -> #/quality/money"])
        self.assertIn("Money: not connected to Azure yet", out[1][1])
        self.assertIn("Money: nothing reported yet this month", out[2][1])
        self.assertIn("Money: not read just now", out[3][1])
        self.assertIn("Money: not read just now", out[4][1], "the first screen never waits for the bill")

    def test_the_page_says_nothing_in_the_words_of_dojos_insides(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800", { pagecheck: check("none", 98, 2.15) }), { offer: offer(68, 130.56), later: { quiet: 4, held: 1, scheduled: 2, pages: 3, silent: 1, other: 1 },
    quiet_until: "2026-10-21T12:20:00+00:00", quiet_last: "2026-10-25T07:05:00+00:00", failed: 1 });
  const c = data([dp, course("gh-300", "GH-300")], { todo: { labs: [{ id: "v", title: "A lab", skill: "s", date: "2026-10-12" }], stuck: [{ id: "l", package: "p", exam: "GH-300", skill: "s", title: "T" }],
    builds: [{ package: "xy-110", exam: "XY-110", state: "failed" }] } });
  c.make.decks = decks();
  answers = [c, bill()];
  const host = await P.studioTodo();
  console.log(JSON.stringify([visible(host), outside(host, () => true).filter((e) => typeof e !== "string" && e.tag === "button").length]));
})();""")
        words = BANNED.findall(out[0])
        self.assertEqual(words, [], f"words of the insides on the face of the page: {words}")
        self.assertGreater(out[1], 4)


@unittest.skipUnless(NODE, "node is not installed")
class CoursesTests(unittest.TestCase):
    def test_the_tab_reports_and_has_no_button_but_links_to_the_page_that_decides(self):
        out = run_node("""
(async () => {
  reset();
  const dp = withVideos(course("dp-800", "DP-800", { pagecheck: { ...check("done", 0, null), supported: 70, weak: 2, invalid: 1, checked: 73, unchecked: 0 } }), { offer: offer(68, 130.56), later: { quiet: 4 } });
  const added = course("dp-300", "DP-300", { added: true, ready: false, skills: 94, verdict: "3 skills of 94 can't be studied from a lesson yet.", search: { covered: 0, supported: 77, weak: 1, invalid: 0, unlooked: 0 },
    rows: [{ key: "pages", label: "Official pages", have: 94, of: 94, state: "ok", note: "n" }, { key: "lessons", label: "Lessons", have: 91, of: 94, state: "warn", note: "n" },
      { key: "listen", label: "Listen", have: 91, of: 91, state: "ok", note: "n" }, { key: "deeper", label: "Deeper Listen", have: 41, of: 91, state: "todo", note: "n" }], missing: { deeper: 50, diagrams: 91, videos: 91, deeper_days: 3 } });
  answers = [data([dp, added])];
  const body = await P.studioCourses();
  const cards = body.flat(Infinity).filter((e) => cls(e).includes("course"));
  console.log(JSON.stringify([cards.length, cards.map((c) => buttons(c)), cards.map((c) => visible(c)), cards.map((c) => links(c)), text(body.flat(Infinity)[0])]));
})();""")
        count, buttons, faces, links, head = out
        self.assertEqual(count, 2)
        self.assertEqual(buttons, [[], []], "the Courses tab reports; To do decides")
        self.assertIn("Ready to study", faces[0])
        self.assertIn("Pages found", faces[0])
        self.assertIn("Every skill cites an official page.", faces[0])
        self.assertIn("Pages confirmed by two models", faces[0])
        self.assertIn("70 of 73", faces[0], "found and confirmed are two rows: found is every skill, confirmed is what two models said")
        self.assertIn("2 only partly, 1 refused", faces[0])
        self.assertIn("68 lessons can be filmed now; 41 play an earlier version's video.", faces[0])
        self.assertIn("Ready to study means the material exists. It does not predict that you will pass.", head)
        self.assertIn("Not complete yet", faces[1])
        self.assertIn("3 skills of 94 can't be studied from a lesson yet.", faces[1])
        self.assertIn("Dojo writes them by itself, about 3 days", faces[1])
        self.assertIn("77 of 94", faces[1], "an added exam's pages: the search's covered skills and the audited ones")
        self.assertIn("1 only partly", faces[1])
        deeper = faces[1].index("Deeper Listen")
        self.assertGreater(deeper, faces[1].index("Optional extras"), "Deeper Listen is an extra: a course is ready without it")
        self.assertIn(["Decide in To do", "#/quality"], links[0])
        self.assertEqual(BANNED.findall(" ".join(faces)), [])

    def test_confirmed_pages_are_never_counted_past_the_skills(self):
        out = run_node("""
(async () => {
  reset();
  answers = [data([course("dp-300", "DP-300", { added: true, skills: 94, search: { covered: 40, supported: 77, weak: 0, invalid: 0, unlooked: 0 } })])];
  const card = (await P.studioCourses()).flat(Infinity).find((e) => cls(e).includes("course"));
  console.log(JSON.stringify(visible(card)));
})();""")
        self.assertIn("94 of 94", out)
        self.assertNotIn("117 of 94", out)

    def test_a_lesson_being_rewritten_is_said_apart_from_the_course_being_ready(self):
        out = run_node("""
(async () => {
  reset();
  answers = [data([course("dp-800", "DP-800", { rechecking: 2 })])];
  const card = (await P.studioCourses()).flat(Infinity).find((e) => cls(e).includes("course"));
  console.log(JSON.stringify(visible(card)));
})();""")
        self.assertIn("Ready to study", out)
        self.assertIn("2 lessons are being rewritten after a source page changed.", out)


@unittest.skipUnless(NODE, "node is not installed")
class MoneyTests(unittest.TestCase):
    def test_a_reading_says_what_it_counts_how_old_it_is_and_what_each_kind_of_cost_means(self):
        out = run_node("""
const b = bill();
const [top, groups] = P.billPanels(b);
console.log(JSON.stringify([visible(top), visible(groups), outside(groups, isCls("bg-row")).length, all(top, (e) => cls(e).includes("partial")).length,
  all(top, isCls("bill-bars"))[0].attrs["aria-label"], links(groups)]));""")
        top, groups, rows, partial, label, links = out
        top, groups = (re.sub(r"\s+", " ", s) for s in (top, groups))
        self.assertIn("$337.86", top)
        self.assertIn("up to Mon, Oct 19 (that day is still arriving)", top)
        self.assertIn("$24.56 is always on: charged even if nobody uses Dojo, about $40 a month.", top)
        self.assertIn("$313.30 follows what Dojo writes, films and checks.", top)
        self.assertIn("The lowest full day so far was $2.00 (Thu, Oct 8): about $62 a month at that rate.", top, "a projection has no cents")
        self.assertIn("At least $353 by the end of October, because the always-on lines keep running.", top, "rounded down: it is a floor")
        self.assertIn("Each bar is a day, Thu, Oct 1 to Mon, Oct 19; the hatched one is still arriving.", top)
        self.assertIn("Last month: $212.40.", top)
        self.assertIn("Busiest days: Thu, Oct 15 $87.00.", top)
        self.assertEqual(partial, 1, "the last day is drawn as still arriving")
        self.assertIn("Cost of each day shown", label)
        self.assertEqual(rows, 4)
        for word in ("Pay per use", "Always on", "Partly always on", "No charge so far"):
            self.assertIn(word, groups)
        self.assertIn("Always on: charged for as long as it exists, even if nobody uses Dojo.", groups)
        self.assertIn("Azure bills the whole resource group, so this cannot be split per course or per person.", groups)
        self.assertIn("Dojo grouped the lines by the names Azure gives them", groups)
        self.assertIn("charges Azure does not assign to it are not included", groups)
        self.assertIn("Azure sends no alert unless you set a budget.", groups)
        self.assertIn("Costs about $13 a month.", groups, "an always-on line says what it costs a month: $7.90 in 19 days")
        self.assertIn("Billed per server Always on about $20 a month", groups)
        self.assertEqual(groups.count("a month."), 1, "only the always-on lines are projected: what follows use has no month figure")
        self.assertEqual([l[1] for l in links[-2:]], ["https://portal.azure.com/#view/x", "https://portal.azure.com/#view/b"])

    def test_a_reading_of_last_month_says_so_until_the_new_month_is_read(self):
        out = run_node("""
const [now] = P.billPanels(bill());
const [old] = P.billPanels(bill({ current_month: "2026-11" }));
console.log(JSON.stringify([now, old].map((p) => visible(p).replace(/\\s+/g, " "))));""")
        now, old = out
        self.assertIn("This month so far", now)
        self.assertIn("Last month: $212.40.", now)
        self.assertIn("Last month, until this month is read", old)
        self.assertIn("The month before: $212.40.", old, "not 'last month' twice, meaning two different months")

    def test_the_first_screen_does_not_call_an_old_reading_this_month(self):
        out = run_node("""
const ok = data([course("a", "A")]);
console.log(JSON.stringify([links(P.todoStrip(ok, bill({ current_month: "2026-11" })))[1][0], links(P.todoStrip(ok, bill()))[1][0]]));""")
        self.assertEqual(out[0], "Money: $337.86 last month; this month is not read yet")
        self.assertEqual(out[1], "Money: $337.86 so far this month")

    def test_a_group_whose_parts_are_all_the_same_kind_shows_the_kind_once(self):
        out = run_node("""
const b = bill();
const rows = outside(P.billPanels(b)[1], isCls("bg-row"));
const chips = (row) => all(row, (e) => e.attrs && e.attrs.chip !== undefined).map(text);
console.log(JSON.stringify(rows.map(chips)));""")
        self.assertEqual(out[0], ["Pay per use"], "AI models: one chip, not one for each model")
        self.assertEqual(out[1], ["Partly always on", "Pay per use", "Always on"], "a mixed group says which part is which")

    def test_a_credit_is_a_credit_not_a_bar(self):
        out = run_node("""
const b = bill({ groups: [{ key: "ai", label: "AI models", note: "n", total: -5, kind: "use", parts: [] }] });
const row = outside(P.billPanels(b)[1], isCls("bg-row"))[0];
console.log(JSON.stringify([text(row), all(row, isCls("bar")).length]));""")
        self.assertIn("credit", out[0])
        self.assertEqual(out[1], 0)

    def test_money_is_shown_in_the_currency_azure_reported(self):
        out = run_node("""console.log(JSON.stringify([P.money(1234.5, "EUR"), P.money(2, "USD"), P.money(2, "ZZZZ")]));""")
        self.assertEqual(out[0], "€1,234.50")
        self.assertEqual(out[1], "$2.00")
        self.assertIn("2.00", out[2], "a currency the browser does not know is still shown with its code")

    def test_a_reading_that_is_old_says_why_and_since_when(self):
        out = run_node("""
const [top] = P.billPanels(bill({ stale: true, stale_reason: "Azure asked Dojo to wait before asking again." }));
console.log(JSON.stringify(visible(top)));""")
        self.assertIn("as of", out)
        self.assertIn("Azure asked Dojo to wait before asking again.", out)

    def test_without_the_role_it_says_what_is_missing_and_the_estimates_open_instead(self):
        out = run_node("""
(async () => {
  reset();
  const denied = { state: "not_connected", reason: "Dojo has no read access to Azure Cost Management yet.", detail: "The app's identity needs the built-in role “Cost Management Reader” on the resource group rg-dojo-swc." };
  answers = [denied, { lines: [] }];
  const flat = (await P.studioMoney()).flat(Infinity);
  const notConnected = [visible(flat[0]), text(flat[0]), flat.filter((e) => e.tag === "details").map((d) => !!d.attrs.open), all(flat[0], isTag("details")).map((d) => !!d.attrs.open)];
  reset();
  answers = [bill(), { lines: [] }];
  const ok = (await P.studioMoney()).flat(Infinity).filter((e) => e.tag === "details").map((d) => !!d.attrs.open);
  console.log(JSON.stringify([notConnected, ok]));
})();""")
        denied, ok = out
        self.assertIn("Dojo cannot read the Azure bill yet", denied[0])
        self.assertIn("Dojo has no read access to Azure Cost Management yet.", denied[0])
        self.assertIn("Cost Management Reader", denied[1], "what is missing is behind one line, for whoever grants it")
        self.assertIn("They are not the bill.", denied[0])
        self.assertEqual(denied[2], [False], "the estimates are Dojo's own, closed under a plain summary")
        self.assertEqual(denied[3], [], "what is missing is said in the open, not behind a line")
        self.assertIn("Cost Management Reader", denied[0])
        self.assertEqual(ok, [False], "with a bill the estimates are closed")

    def test_the_empty_first_day_is_not_a_zero(self):
        out = run_node("""
const empty = P.billPanels({ state: "empty", reason: "Azure has not reported any cost for this month yet.", previous: { month: "2026-09", total: 480, currency: "USD" }, currency: "USD" });
console.log(JSON.stringify(visible(empty)));""")
        self.assertIn("No cost reported yet this month", out)
        self.assertIn("Last month: $480.00.", out)
        self.assertNotIn("$0.00", out)


@unittest.skipUnless(NODE, "node is not installed")
class ExamPageTests(unittest.TestCase):
    def test_the_film_panel_says_what_is_left_out_and_never_that_all_is_done_when_it_is_not(self):
        out = run_node("""
vm.runInContext(cut("function filmPanel(st, onStatus, note) {", "function removedPage(res) {") + "\\nthis.filmPanel = filmPanel;", context);
const st = (film, lessons) => ({ film: { offer: null, waiting: 0, filming: 0, failed: [], approvals: [], storage: { bytes: 1e9, budget_bytes: 6e9 }, later: {}, ...film },
  lessons: { filmed: 0, ready: 3, ...lessons } });
const said = (s) => visible(context.filmPanel(s)).replace(/\\s+/g, " ");
const withOffer = st({ offer: { lessons: 2 }, later: { scheduled: 1 } });
console.log(JSON.stringify([said(st({ later: { quiet: 1, scheduled: 0, pages: 0 } }, { filmed: 2 })), said(st({ later: { quiet: 2, scheduled: 1, pages: 1 } })),
  said(st({}, { filmed: 3 })), said(withOffer), links(context.filmPanel(withOffer)), said(st({}, { filmed: 2 })), said(st({ later: { held: 1 } }, { filmed: 2 }))]));""")
        one, many, all_done, offered, links, partly, held = out
        self.assertIn("Nothing is offered for filming right now.", partly, "two of three are filmed and nothing is left to offer: not 'every lesson is filmed'")
        self.assertIn("1 written lesson is not offered for filming yet: 1 waiting for a rewrite after a source page changed.", held)
        self.assertIn("1 written lesson is not offered for filming yet: 1 rewritten less than a week ago. It joins the To do page when that is over.", one)
        self.assertNotIn("Every written lesson is filmed or approved.", one, "it is not true while one is left out")
        self.assertIn("4 written lessons are not offered for filming yet: 2 rewritten less than a week ago, 1 waiting to be rewritten as a deeper lesson, "
                      "1 whose pages may still be replaced. They join the To do page when that is over.", many)
        self.assertIn("Every written lesson is filmed.", all_done)
        self.assertNotIn("filmed or approved", all_done)
        self.assertIn("2 written lessons not filmed yet.", offered)
        self.assertIn("1 written lesson is not offered for filming yet: 1 waiting to be rewritten as a deeper lesson.", offered)
        self.assertEqual(links, [["Decide on the To do page", "#/quality"]], "the page decides nothing and spends nothing: it points to where it is decided")


@unittest.skipUnless(NODE, "node is not installed")
class NavigationTests(unittest.TestCase):
    def test_the_tabs_are_links_with_the_current_page_marked_and_none_is_a_tab_role(self):
        out = run_node("""
const nav = P.studioNav("money");
console.log(JSON.stringify([nav.tag, nav.attrs["aria-label"], nav.kids.map((a) => [text(a), a.attrs.href, a.attrs["aria-current"] || null, a.attrs.role || null])]));""")
        self.assertEqual(out[0], "nav")
        self.assertEqual(out[1], "Studio")
        self.assertEqual(out[2], [["To do", "#/quality", None, None], ["Courses", "#/quality/courses", None, None], ["Money", "#/quality/money", "page", None],
                                  ["Health", "#/quality/health", None, None], ["Activity", "#/quality/activity", None, None]])

    def test_the_old_addresses_open_the_new_pages_and_the_log_link_opens_its_section(self):
        out = run_node("""
(async () => {
  const opened = [];
  const note = (name) => async (opts) => { opened.push([name, opts]); return [name]; };
  for (const name of ["studioTodo", "studioCourses", "studioMoney", "studioHealth", "studioActivity"]) context[name] = note(name);
  const shown = async (tab) => { const [head, nav, body] = await P.viewQuality(tab); await tick(); return [text(nav.kids.find((a) => a.attrs["aria-current"])), text(body)]; };
  const rows = [];
  for (const tab of [undefined, "todo", "courses", "money", "health", "activity", "checks", "costs", "nonsense"]) rows.push([tab || "(none)", ...(await shown(tab))]);
  console.log(JSON.stringify([rows, opened.filter((o) => o[0] === "studioHealth").map((o) => o[1])]));
})();""")
        rows, health = out
        self.assertEqual([r[:2] for r in rows], [["(none)", "To do"], ["todo", "To do"], ["courses", "Courses"], ["money", "Money"], ["health", "Health"],
                                                 ["activity", "Activity"], ["checks", "Health"], ["costs", "Money"], ["nonsense", "To do"]])
        self.assertEqual(health, [{"open": None}, {"open": "blocked"}], "only the old log address opens the blocked sentences")

    def test_the_heading_is_drawn_before_the_page_is_read_and_a_failure_says_so(self):
        out = run_node("""
(async () => {
  let release;
  context.studioTodo = () => new Promise((resolve) => { release = resolve; });
  const [head, nav, body] = await P.viewQuality("todo");
  const first = text(body);
  release(["the page"]);
  await tick();
  const second = text(body);
  context.studioMoney = async () => { throw new Error("Azure did not answer"); };
  const [, , failed] = await P.viewQuality("money");
  await tick();
  state.nav = 2;
  context.studioCourses = () => new Promise((resolve) => { release = resolve; });
  const [, , gone] = await P.viewQuality("courses");
  state.nav = 3;   // the owner went elsewhere
  release(["late"]);
  await tick();
  console.log(JSON.stringify([text(head), first, second, text(failed), text(gone)]));
})();""")
        self.assertIn("What needs you", out[0])
        self.assertEqual([out[1], out[2]], ["Loading…", "the page"])
        self.assertIn("This page could not be read: Azure did not answer", out[3])
        self.assertEqual(out[4], "Loading…", "a page that was read after the owner left is not drawn")

    def test_a_member_still_gets_the_studio_log(self):
        out = run_node("""
(async () => {
  context.isAdmin = () => false;
  console.log(JSON.stringify(await P.viewQuality("todo")));
})();""")
        self.assertEqual(out, ["log"])


@unittest.skipUnless(NODE, "node is not installed")
class SheetTests(unittest.TestCase):
    """The question before money is spent."""

    def test_cancel_has_the_focus_and_escape_or_cancel_answers_nothing(self):
        out = run_node("""
(async () => {
  const ask = { title: (n, t) => `Film? About ${usd(t)}`, confirm: () => "Film", lines: [{ label: "68 lessons", usd: 130.56 }], notes: ["a note"] };
  const answers = [];
  for (const how of ["escape", "cancel"]) {
    const p = realSheet(ask);
    const dlg = documentStub.body.kids[documentStub.body.kids.length - 1];
    const cancel = all(dlg, isTag("button")).find((b) => text(b) === "Cancel");
    const focused = cancel.focused;
    if (how === "cancel") cancel.attrs.onclick(); else dlg.close();
    answers.push([focused, await p, dlg.removed]);
  }
  console.log(JSON.stringify(answers));
})();""")
        self.assertEqual(out, [[True, None, True], [True, None, True]])

    def test_the_confirm_button_carries_the_price_and_answers_the_lines(self):
        out = run_node("""
(async () => {
  const lines = [{ label: "68 lessons of DP-800", usd: 130.56 }];
  const p = realSheet({ title: (n, t) => `Film 68 lessons of DP-800? About ${usd(t)}`, confirm: () => "Film 68 lessons", lines, notes: [] });
  const dlg = documentStub.body.kids[documentStub.body.kids.length - 1];
  const go = all(dlg, isTag("button")).find((b) => text(b).startsWith("Film"));
  const title = text(all(dlg, isTag("h2"))[0]);
  go.fire("click");
  const answer = await p;
  console.log(JSON.stringify([title, text(go), answer === lines || JSON.stringify(answer) === JSON.stringify(lines), all(dlg, isTag("li")).map(text)]));
})();""")
        self.assertEqual(out[0], "Film 68 lessons of DP-800? About $130.56")
        self.assertEqual(out[1], "Film 68 lessons · about $130.56")
        self.assertTrue(out[2])
        self.assertEqual(out[3], ["68 lessons of DP-800 about $130.56"])

    def test_with_boxes_the_price_follows_the_ticks_and_nothing_ticked_cannot_be_confirmed(self):
        out = run_node("""
(async () => {
  const lines = [{ label: "Films", usd: 100 }, { label: "Diagrams", usd: 50 }, { label: "No room", usd: 30, off: true }];
  const p = realSheet({ pick: true, title: (n, t) => `Start ${n}? About ${usd(t)}`, confirm: (n) => `Start ${n}`, lines });
  const dlg = documentStub.body.kids[documentStub.body.kids.length - 1];
  const boxes = all(dlg, (e) => e.tag === "input");
  const go = all(dlg, isTag("button")).find((b) => text(b).startsWith("Start"));
  const states = [[boxes.map((b) => b.checked), text(all(dlg, isTag("h2"))[0]), text(go), go.disabled]];
  boxes[1].checked = false; dlg.fire("change");
  states.push([text(all(dlg, isTag("h2"))[0]), text(go), go.disabled]);
  boxes[0].checked = false; dlg.fire("change");
  states.push([text(all(dlg, isTag("h2"))[0]), text(go), go.disabled]);
  boxes[1].checked = true; dlg.fire("change");
  go.fire("click");
  const answer = await p;
  console.log(JSON.stringify([states, answer.map((l) => l.label), boxes[2].attrs.disabled || false]));
})();""")
        states, answer, off_disabled = out
        self.assertEqual(states[0], [[True, True, False], "Start 2? About $150.00", "Start 2 · about $150.00", False])
        self.assertEqual(states[1], ["Start 1? About $100.00", "Start 1 · about $100.00", False])
        self.assertEqual(states[2], ["Start 0? About $0.00", "Start 0 · about $0.00", True])
        self.assertEqual(answer, ["Diagrams"])
        self.assertTrue(off_disabled, "a line that cannot be done is shown, unticked and not tickable")

    def test_it_closes_when_the_owner_goes_to_another_page(self):
        out = run_node("""
(async () => {
  const p = realSheet({ title: () => "t", confirm: () => "c", lines: [{ label: "l", usd: 1 }] });
  const dlg = documentStub.body.kids[documentStub.body.kids.length - 1];
  (windowStub.listeners.hashchange || []).forEach((fn) => fn());
  console.log(JSON.stringify([await p, (windowStub.listeners.hashchange || []).length]));
})();""")
        self.assertEqual(out, [None, 0])


if __name__ == "__main__":
    unittest.main()
