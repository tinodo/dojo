"""Links Dojo hands out to be opened later (ADR 0004 and ADR 0013, amended 5 October 2026).

Easy Auth loses everything after the "#" when it sends someone to sign in, and keeps the path and the query. So a
calendar event, a nudge and an episode's notes link to /?go=<route>, and the app turns that into /#/<route> when it
starts. These tests hold the one rule for a route (app/deeplink.py, app.js and sw.js carry it), the links the server
builds, and the two places a browser acts on one: it never goes to an address, so it never leaves the app."""
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-deeplink-import-"))

from fastapi.testclient import TestClient  # noqa: E402

from app import deeplink, helpdoc, push  # noqa: E402
from app import plan as planmod  # noqa: E402
from app.main import create_app  # noqa: E402
from app.packages import Packages  # noqa: E402
from tests.test_api import settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
APP_JS = (STATIC / "app.js").read_text("utf-8")
SW_JS = (STATIC / "sw.js").read_text("utf-8")
NODE = shutil.which("node")
LESSON = "lesson-0123456789abcdef0123"

GOOD = [
    "today", "five", "phone", "help", "plan/2026-10-05", "help/pick-an-account-keeps-coming-back",
    f"{LESSON}", f"lesson/{LESSON}", f"lesson/{LESSON}/listen", f"lesson/{LESSON}/read/s3",
    "skill/gh-300/d1.g1.s1", "check/for/dp-800", "lab/vector",
    "a", "0", "a_b", "a-b", "a.b", "a.b-c_d", "a/b/c/d/e/f", "a" * 120,
]

# What a route is not, by the way it goes wrong. These are the values the app sees after the query is decoded once.
BAD = {
    "traversal": ["..", "../", "../..", "a/..", "a/../b", "a/../../b", "a/./b", ".", "./a", "a/.", "...", ".a", "a.", "a..b",
                  "a./b", "a/.b", "..\\.."],
    "address": ["https://evil.example", "http://evil.example/x", "javascript:alert(1)", "data:text/html,x",
                "mailto:a@b.example", "//evil.example", "///evil.example", "/\\evil.example", "\\\\evil.example",
                "evil.example:80", "ftp://x", "a:b"],
    "encoded": ["a%2Fb", "a%2fb", "%2F%2Fevil.example", "%252F", "%252e%252e", "%2e%2e", "%5Cevil", "a%00b", "%0A",
                "a%20b", "a%", "%", "%41"],
    "backslash": ["a\\b", "\\a", "a\\", "..\\..\\x", "a/..\\b"],
    "size": ["a" * 121, "a" * 500, "/".join(["a"] * 7), "a/" * 61 + "a"],
    "unicode": ["caf\u00e9", "\u202eevil", "a\u202eb", "\uff48elp", "\u0131", "\u0130", "\u212a", "\u0430", "\u2215",
                "\uff0f", "a\u2024b", "\u00ad", "a\u200bb", "\U0001f600", "\u0660"],
    "control": ["", "a\nb", "a\n", "today\n", "\n", "a\r", "a\tb", "a\x00", " a", "a ", "a b", "a+b"],
    "character": ["a;b", "a,b", "a@b", "a?b", "a#b", "a=b", "a&b", "a~b", "a!b", "a*b", "a'b", 'a"b', "a<b", "a>b",
                  "a(b)", "a|b", "a$b", "a[b]", "a{b}", "a^b", "a`b"],
    "case": ["Today", "A", "Lesson/x", "help/Pick"],
    "separator": ["-a", "_a", "a-", "a_", "a/", "/a", "a//b", "a--b", "a__b", "a-_b", "a._b", "/", "//", "-", "_"],
}

# The same, as they arrive in the address bar: /?go=<value>.
CONVERTED = [
    ("?go=today", "/#/today"),
    (f"?go=lesson/{LESSON}/present", f"/#/lesson/{LESSON}/present"),
    ("?go=skill/gh-300/d1.g1.s1", "/#/skill/gh-300/d1.g1.s1"),
    ("?go=help/pick-an-account-keeps-coming-back", "/#/help/pick-an-account-keeps-coming-back"),
    (f"?go=lesson/{LESSON}/read/s3", f"/#/lesson/{LESSON}/read/s3"),
    ("?go=today&utm_source=mail", "/#/today"),
    ("?utm_source=mail&go=today", "/#/today"),
    ("?go=a%2Fb", "/#/a/b"),   # decoded once, then judged as the route it has become
    ("?go=a%2fb", "/#/a/b"),
    ("?go=" + "a" * 120, "/#/" + "a" * 120),
    ("?go=a/b/c/d/e/f", "/#/a/b/c/d/e/f"),
]
DROPPED = [
    "?go=", "?go", "?go=&go=today", "?go=today&go=", "?go=today&go=five",
    "?go=..", "?go=../..", "?go=a/../b", "?go=%2e%2e", "?go=a%2F..%2Fb", "?go=%2e%2e%2f%2e%2e%2fetc",
    "?go=https://evil.example", "?go=https%3A%2F%2Fevil.example", "?go=//evil.example", "?go=%2F%2Fevil.example",
    "?go=a%2F%2Fb", "?go=%252F", "?go=%252e%252e", "?go=\\evil.example", "?go=%5Cevil.example", "?go=a%5Cb",
    "?go=javascript:alert(1)", "?go=javascript%3Aalert(1)", "?go=data:text/html,x",
    "?go=" + "a" * 121, "?go=" + "/".join(["a"] * 7),
    "?go=caf%C3%A9", "?go=caf\u00e9", "?go=%E2%80%AEevil", "?go=%EF%BD%88elp", "?go=%C4%B1", "?go=%E2%84%AA",
    "?go=%D0%B0", "?go=%E2%88%95", "?go=%EF%BC%8F",
    "?go=%00", "?go=a%0Ab", "?go=%09", "?go=a%20b", "?go=a+b", "?go=Today", "?go=-a", "?go=a-", "?go=a/", "?go=/a",
    "?go=a//b", "?go=%41", "?go=a%", "?go=%zz",
]
UNTOUCHED = ["", "?", "?x=1", "?GO=today", "?goto=today", "?go2=today", "?ago=today", "?x=go%3Dtoday"]

# The sign-in banner's Reload: the hash the page is on, and where Reload goes.
RELOAD = [
    ("#/today", "/?go=today"),
    (f"#/lesson/{LESSON}/listen", f"/?go=lesson/{LESSON}/listen"),
    ("#/skill/gh-300/d1.g1.s1", "/?go=skill/gh-300/d1.g1.s1"),
    ("#/help/pick-an-account-keeps-coming-back", "/?go=help/pick-an-account-keeps-coming-back"),
    ("#/plan/2026-10-05", "/?go=plan/2026-10-05"),
    ("", "/"), ("#", "/"), ("#/", "/"), ("#today", "/"), ("#/Today", "/"), ("#/..", "/"), ("#/a/../b", "/"),
    ("#/a%2Fb", "/"), ("#//evil.example", "/"), ("#/https://evil.example", "/"), ("#/javascript:alert(1)", "/"),
    ("#/a?x=1", "/"), ("#/a b", "/"), ("#/a\\b", "/"), ("#/" + "a" * 121, "/"), ("#/caf\u00e9", "/"),
]

ALPHABET = list("abcxyz019AZ./\\%:@?#&=+-_~;,!*() \t\n\x00") + [
    "\u00e9", "\u0131", "\u0130", "\u212a", "\uff48", "\u0430", "\u202e", "\u2215", "\uff0f", "\u2024", "\u00ad", "\u200b",
    "\U0001f600", "\u0660"]


def fuzz(n: int = 3000, seed: int = 20261005) -> list[str]:
    """Random text, and routes that are right but for one thing: where a rule that is a little off would show."""
    rnd = random.Random(seed)
    values = ["".join(rnd.choice(ALPHABET) for _ in range(rnd.randint(0, 24))) for _ in range(n)]
    for _ in range(n):
        base = rnd.choice(GOOD[:-1])
        i = rnd.randrange(len(base) + 1)
        change = rnd.choice(("insert", "replace", "delete"))
        extra = rnd.choice(ALPHABET)
        values.append(base[:i] + (extra if change != "delete" else "") + base[i + (change != "insert"):])
    return values


class Bare(planmod.Planner):
    """A planner with nothing behind it: a session's route and link read nothing of it."""

    def __init__(self):
        pass


PLANNER = Bare()


def planner_routes() -> list[str]:
    """Every route the Planner gives, for each kind of session, each exam and each skill Dojo ships."""
    route = PLANNER.route
    routes = [route({"kind": "lesson", "lesson": LESSON, "package": "gh-300", "skill": "d1.g1.s1"}),
              route({"kind": "listen", "lesson": LESSON, "package": "gh-300", "skill": "d1.g1.s1"})]
    for pid, package in Packages(ROOT / "app" / "packages").by_id.items():
        routes += [route({"kind": kind, "package": pid}) for kind in ("check3", "recall", "exam")]
        routes += [route({"kind": kind, "package": pid, "skill": sid})
                   for sid in package["skills"] for kind in ("practice", "check", "later")]
    routes += [route({"kind": "lab", "lab": lab, "package": "dp-800"}) for lab in sorted(planmod.LABS)]
    return routes


def run_node(script: str, data: object) -> object:
    """Runs a script in node. It sees the data as DATA, the app as APP and the worker as SW, and prints one JSON value."""
    prelude = "\n".join([
        "const fs = require('fs'), vm = require('vm');",
        f"const DATA = {json.dumps(data)};",
        f"const APP = fs.readFileSync({json.dumps(str(STATIC / 'app.js'))}, 'utf8');",
        f"const SW = fs.readFileSync({json.dumps(str(STATIC / 'sw.js'))}, 'utf8');",
    ])
    done = subprocess.run([NODE, "-"], input=prelude + "\n" + script, capture_output=True, text=True, encoding="utf-8", timeout=180)
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


# The part of app.js that reads a link and the sign-in banner, run with a page that records what it is told to do.
APP_HARNESS = r"""
const START = APP.indexOf("const GO_ROUTE");
const END = APP.indexOf("async function api(");
if (START < 0 || END < START) throw new Error("the deep link code is not where it was");
const page = { search: "", hash: "" };
const calls = [];
const made = [];
const buttons = {};
const context = vm.createContext({
  URLSearchParams,
  location: {
    get search() { return page.search; },
    get hash() { return page.hash; },
    replace(u) { calls.push(["replace", u]); },
    assign(u) { calls.push(["assign", u]); },
    reload() { calls.push(["reload"]); },
    set href(u) { calls.push(["href", u]); },
  },
  history: {
    replaceState(state, title, u) { calls.push(["replaceState", state, title, u]); },
    pushState(state, title, u) { calls.push(["pushState", state, title, u]); },
  },
  window: { open() { calls.push(["open"]); } },
  hideNewBuild() {},
  icon: () => null,
  h: (...parts) => parts,
  button: (label, onClick) => { made.push(label); buttons[label] = onClick; return null; },
  document: { body: { append() {} } },
});
vm.runInContext(APP.slice(START, END) + "\nthis.found = { goRoute, openDeepLink, reloadToSignIn, signInEnded };", context);
const { goRoute, openDeepLink, reloadToSignIn, signInEnded } = context.found;
const start = (search) => { calls.length = 0; page.search = search; openDeepLink(); return calls.slice(); };
"""

# sw.js, loaded as the browser would with a fake worker scope, and a way to tap a nudge and to receive one.
WORKER_HARNESS = r"""
const ORIGIN = "https://dojo.example";
function load(windows) {
  const log = [], handlers = {};
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting() {},
    clients: {
      claim: async () => {},
      matchAll: async () => windows.map((url) => ({
        url,
        focus: async () => { log.push(["focus", url]); },
        navigate: async (to) => { log.push(["navigate", to]); },
      })),
      openWindow: async (url) => { log.push(["openWindow", url]); },
    },
    registration: { showNotification: async (title, options) => { log.push(["show", title, options.data.url]); } },
  };
  const context = vm.createContext({ self, URL });
  vm.runInContext(SW, context);
  return { log, handlers, safePath: vm.runInContext("safePath", context) };
}
async function tap(data, windows) {
  const worker = load(windows);
  let work;
  worker.handlers.notificationclick({ notification: { data, close() {} }, waitUntil: (p) => { work = p; } });
  await work;
  return worker.log;
}
async function receive(payload) {
  const worker = load([]);
  let work;
  worker.handlers.push({ data: { json: () => payload }, waitUntil: (p) => { work = p; } });
  await work;
  return worker.log;
}
"""


class RuleTests(unittest.TestCase):
    def test_a_route_is_a_few_plain_parts(self):
        for route in GOOD:
            self.assertTrue(deeplink.route_ok(route), route)

    def test_nothing_else_is_a_route(self):
        for kind, values in BAD.items():
            for value in values:
                self.assertFalse(deeplink.route_ok(value), f"{kind}: {value!r}")

    def test_the_limits_are_exact(self):
        self.assertTrue(deeplink.route_ok("a" * deeplink.MAX_LENGTH))
        self.assertFalse(deeplink.route_ok("a" * (deeplink.MAX_LENGTH + 1)))
        self.assertTrue(deeplink.route_ok("/".join(["a"] * 6)))
        self.assertFalse(deeplink.route_ok("/".join(["a"] * 7)))
        self.assertFalse(deeplink.route_ok("today\n"), "a trailing newline is not part of a route")

    def test_only_text_can_be_a_route(self):
        for value in (None, 5, 1.5, True, b"today", ["today"], {"today": 1}):
            self.assertFalse(deeplink.route_ok(value), repr(value))

    def test_a_link_is_the_start_page_with_the_route_in_the_query(self):
        self.assertEqual(deeplink.go_link("today"), "/?go=today")
        self.assertEqual(deeplink.go_link(f"lesson/{LESSON}/listen", "https://dojo.example"),
                         f"https://dojo.example/?go=lesson/{LESSON}/listen")
        for route in GOOD:
            link = deeplink.go_link(route, "https://dojo.example")
            parts = urlsplit(link)
            self.assertEqual((parts.scheme, parts.netloc, parts.path, parts.fragment), ("https", "dojo.example", "/", ""), link)
            self.assertEqual(parse_qs(parts.query), {"go": [route]}, "it needs no encoding and survives a URL parser")

    def test_a_route_that_fails_the_rule_gives_the_start_page_and_never_fails_a_feed(self):
        for kind, values in BAD.items():
            for value in values:
                self.assertEqual(deeplink.go_link(value, "https://dojo.example"), "https://dojo.example/", f"{kind}: {value!r}")
                self.assertEqual(deeplink.go_link(value), "/")

    def test_the_three_copies_of_the_rule_are_one(self):
        want = "^" + deeplink.SOURCE.replace("/", "\\/") + "$"
        self.assertEqual(re.search(r"^const GO_ROUTE = /(.*)/;$", APP_JS, re.M).group(1), want)
        self.assertEqual(re.search(r"^const ROUTE = /(.*)/;$", SW_JS, re.M).group(1), want)
        self.assertIn(f"value.length <= {deeplink.MAX_LENGTH} &&", APP_JS)
        self.assertIn(f"m[1].length <= {deeplink.MAX_LENGTH} &&", SW_JS)


class LinkTests(unittest.TestCase):
    def test_every_route_the_planner_gives_is_a_route(self):
        routes = planner_routes()
        self.assertGreater(len(routes), 100)
        for route in routes:
            self.assertTrue(deeplink.route_ok(route), route)
            self.assertEqual(deeplink.go_link(route, "https://dojo.example"), f"https://dojo.example/?go={route}")

    def test_a_session_has_a_route_and_the_plan_page_still_links_with_a_hash(self):
        session = {"kind": "listen", "lesson": LESSON, "package": "gh-300", "skill": "d1.g1.s1"}
        self.assertEqual(PLANNER.route(session), f"lesson/{LESSON}/listen")
        self.assertEqual(PLANNER.link(session), f"#/lesson/{LESSON}/listen")

    def test_a_nudge_opens_dojo_with_a_query_and_not_a_hash(self):
        urls = {"phone": push.TEST_MESSAGE["url"], "five": push.recall_message(1, 5)["url"], "today": push.plan_message(2, 30)["url"]}
        self.assertEqual(urls, {"phone": "/?go=phone", "five": "/?go=five", "today": "/?go=today"})
        for url in urls.values():
            self.assertNotIn("#", url)

    def test_every_help_page_has_a_route_so_a_reload_on_it_comes_back(self):
        anchors = helpdoc.guide()["anchors"]
        self.assertGreater(len(anchors), 30)
        for anchor in anchors:
            self.assertTrue(deeplink.route_ok(f"help/{anchor}"), f"a heading that makes {anchor!r} is a page a reload would not return to")

    def test_every_page_the_app_links_to_is_a_route(self):
        literals = set(re.findall(r"""["'`]#/([^"'`\s]*)["'`]""", APP_JS))
        self.assertGreater(len(literals), 40)
        for literal in literals:
            route = re.sub(r"\$\{[^}]*\}", "x1", literal).rstrip("/")   # "#/item/" + id is a route with its id left off
            self.assertTrue(route == "" or deeplink.route_ok(route), f"#/{literal}")

    def test_links_are_built_in_one_place(self):
        for path in sorted((ROOT / "app").glob("*.py")):
            if path.name != "deeplink.py":
                self.assertNotIn("/#/", path.read_text("utf-8"), f"{path.name}: build a link with deeplink.go_link")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(create_app(settings(tempfile.mkdtemp(prefix="dojo-deeplink-"))))

    def test_the_server_answers_the_start_page_whatever_go_says(self):
        plain = self.c.get("/", follow_redirects=False)
        self.assertEqual(plain.status_code, 200)
        for query in ("?go=today", "?go=skill/gh-300/d1.g1.s1", "?go=https://evil.example", "?go=%2e%2e%2f", "?go=" + "a" * 300, "?go="):
            r = self.c.get("/" + query, follow_redirects=False)
            self.assertEqual(r.status_code, 200, query)
            self.assertEqual(r.text, plain.text, query)
            self.assertNotIn("location", r.headers, query)
            self.assertEqual(r.headers["cache-control"], "no-store", query)
            self.assertEqual(r.headers["content-security-policy"], plain.headers["content-security-policy"], query)


@unittest.skipUnless(NODE, "node is not installed")
class AppStartTests(unittest.TestCase):
    def test_a_link_becomes_the_page_route_and_the_query_goes(self):
        out = run_node(APP_HARNESS + "console.log(JSON.stringify(DATA.map(start)));", [s for s, _ in CONVERTED])
        for (search, want), calls in zip(CONVERTED, out):
            self.assertEqual(calls, [["replaceState", None, "", want]], search)

    def test_anything_that_is_not_a_route_is_dropped_and_the_start_page_opens(self):
        out = run_node(APP_HARNESS + "console.log(JSON.stringify(DATA.map(start)));", DROPPED)
        for search, calls in zip(DROPPED, out):
            self.assertEqual(calls, [["replaceState", None, "", "/"]], search)

    def test_an_address_without_go_is_left_alone(self):
        out = run_node(APP_HARNESS + "console.log(JSON.stringify(DATA.map(start)));", UNTOUCHED)
        self.assertEqual(out, [[]] * len(UNTOUCHED))

    def test_decoding_happens_once_and_the_result_is_judged(self):
        values = fuzz()
        searches = ["?go=" + quote(v, safe="") for v in values]
        out = run_node(APP_HARNESS + "console.log(JSON.stringify(DATA.map(start)));", searches)
        wrong = [(v, calls) for v, calls in zip(values, out)
                 if calls != [["replaceState", None, "", f"/#/{v}" if deeplink.route_ok(v) else "/"]]]
        self.assertEqual(wrong[:5], [], f"{len(wrong)} of {len(values)} differ from deeplink.route_ok")

    def test_nothing_a_visitor_sends_makes_the_page_leave_the_app(self):
        values = fuzz(seed=7)
        searches = ["?go=" + v for v in values] + ["?go=" + quote(quote(v, safe=""), safe="") for v in values]
        out = run_node(APP_HARNESS + "console.log(JSON.stringify(DATA.map(start)));", searches)
        for search, calls in zip(searches, out):
            self.assertEqual(len(calls), 1, search)
            kind, state, title, url = calls[0]
            self.assertEqual((kind, state, title), ("replaceState", None, ""), search)
            self.assertTrue(url == "/" or (url.startswith("/#/") and deeplink.route_ok(url[3:])), f"{search!r} gave {url!r}")

    def test_the_app_and_python_agree_on_what_a_route_is(self):
        values = fuzz(seed=11) + [v for values in BAD.values() for v in values] + GOOD
        out = run_node(APP_HARNESS + "console.log(JSON.stringify(DATA.map(goRoute)));", values)
        wrong = [(v, got) for v, got in zip(values, out) if got != (v if deeplink.route_ok(v) else None)]
        self.assertEqual(wrong[:5], [])

    def test_the_banner_reload_goes_to_the_page_you_were_on(self):
        out = run_node(APP_HARNESS + "console.log(JSON.stringify(DATA.map((hash) => { calls.length = 0; page.hash = hash; reloadToSignIn(); return calls.slice(); })));",
                       [h for h, _ in RELOAD])
        for (hash, want), calls in zip(RELOAD, out):
            self.assertEqual(calls, [["replace", want]], hash)

    def test_the_reload_button_of_the_banner_does_it(self):
        out = run_node(APP_HARNESS + r"""
page.hash = DATA;
const error = signInEnded();
signInEnded();
buttons.Reload();
console.log(JSON.stringify({ calls, made, status: error.status, signedOut: error.signedOut }));
""", f"#/lesson/{LESSON}/listen")
        self.assertEqual(out["made"], ["Reload"], "a second failed call does not stack a second banner")
        self.assertEqual(out["calls"], [["replace", f"/?go=lesson/{LESSON}/listen"]], "no plain reload, which would lose the page")
        self.assertEqual((out["status"], out["signedOut"]), (401, True))

    def test_the_router_has_a_page_for_every_link_the_server_builds(self):
        routes = sorted(set(planner_routes() + ["today", "five", "phone"] + [f"help/{a}" for a in helpdoc.guide()["anchors"]]))
        out = run_node(r"""
const table = APP.slice(APP.indexOf("const routes = ["), APP.indexOf("async function route()"));
const patterns = [...table.matchAll(/^\s*\[\/(\^#.*?)\/,\s/gm)].map((m) => new RegExp(m[1]));
patterns.push(new RegExp(/const HELP_ROUTE = \/(.*)\/;/.exec(APP)[1]));
console.log(JSON.stringify({ patterns: patterns.length, missing: DATA.filter((r) => !patterns.some((re) => re.test("#/" + r))) }));
""", routes)
        self.assertGreater(out["patterns"], 25)
        self.assertEqual(out["missing"], [], "a link to a page the app does not have opens Today")

    def test_the_start_reads_the_link_before_anything_else(self):
        start = APP_JS[APP_JS.index("async function init() {"):]
        self.assertEqual(start.splitlines()[1].strip(), "openDeepLink();")
        self.assertLess(start.index("openDeepLink();"), start.index("route()"))
        self.assertLess(start.index("openDeepLink();"), start.index('api("/api/me")'))

    def test_the_link_is_only_ever_turned_into_a_hash_route(self):
        code = APP_JS[APP_JS.index("function openDeepLink()"):APP_JS.index("// ---------------------------------------------------------------- server calls")]
        self.assertIn("history.replaceState(", code)
        for navigation in ("location.assign", "location.replace", "location.href", "location.hash", "window.open", "location ="):
            self.assertNotIn(navigation, code)

    def test_the_banner_never_reloads_the_page_as_it_is(self):
        code = APP_JS[APP_JS.index("function reloadToSignIn()"):APP_JS.index("async function api(")]
        self.assertIn("location.replace(", code)
        self.assertNotIn("location.reload()", code)
        self.assertNotIn("/.auth/login", APP_JS)


@unittest.skipUnless(NODE, "node is not installed")
class WorkerTests(unittest.TestCase):
    OPEN = "https://dojo.example"

    def paths(self, values: list) -> list:
        return run_node(WORKER_HARNESS + "const { safePath } = load([]);\nconsole.log(JSON.stringify(DATA.map(safePath)));", values)

    def test_a_nudge_opens_the_route_it_names_on_this_site(self):
        cases = ["/?go=five", "/?go=today", "/?go=phone", f"/?go=lesson/{LESSON}/listen", "/?go=skill/gh-300/d1.g1.s1", "/?go=" + "a" * 120]
        self.assertEqual(self.paths(cases), cases)

    def test_a_nudge_sent_before_this_build_opens_the_same_route(self):
        old = ["/#/five", "/#/today", "/#/phone", f"/#/lesson/{LESSON}", "/#/skill/gh-300/d1.g1.s1"]
        self.assertEqual(self.paths(old), ["/?go=" + p[3:] for p in old])

    def test_anything_else_opens_today(self):
        hostile = [
            "https://evil.example/", "https://evil.example/?go=five", "//evil.example/?go=five", "/\\evil.example",
            "/?go=//evil.example", "/?go=https://evil.example", "/?go=javascript:alert(1)", "/?go=../x", "/?go=a/../b",
            "/?go=%2e%2e", "/?go=a%2Fb", "/?go=%252F", "/?go=a\\b", "/?go=a&x=1", "/?go=a?b", "/?go=a#b", "/?go=five\n",
            "/?go=" + "a" * 121, "/?go=caf\u00e9", "/?go=Five", "/?go=", "/?go", "/", "", "/#/", "/#/../x", "/#//evil",
            "/#/https://evil.example", "#/five", "five", "/static/app.js", "/api/me", "/?x=1&go=five", "/?go=five&go=six",
            "/?go=five#/six", " /?go=five", "/?go=five ", None, 5, {}, ["/?go=five"], True,
        ]
        self.assertEqual(self.paths(hostile), ["/?go=today"] * len(hostile))

    def test_the_worker_and_python_agree_on_what_a_route_is(self):
        values = fuzz(seed=13) + [v for values in BAD.values() for v in values] + GOOD
        for form, prefix in (("query", "/?go="), ("old", "/#/")):
            got = self.paths([prefix + v for v in values])
            wrong = [(v, g) for v, g in zip(values, got) if g != (f"/?go={v}" if deeplink.route_ok(v) else "/?go=today")]
            self.assertEqual(wrong[:5], [], form)

    def test_whatever_a_payload_says_the_worker_opens_this_site_and_nothing_else(self):
        values = fuzz(seed=17)
        for path in self.paths(["/?go=" + v for v in values] + ["/#/" + v for v in values] + values):
            url = urlsplit(self.OPEN + path)
            self.assertEqual((url.scheme, url.netloc, url.path, url.fragment), ("https", "dojo.example", "/", ""), path)
            self.assertTrue(url.query.startswith("go=") and deeplink.route_ok(url.query[3:]), path)

    def test_a_tap_opens_the_route_in_a_window_of_this_site_or_a_new_one(self):
        far = "https://evil.example/"
        cases = [
            ({"url": "/?go=five"}, [], [["openWindow", f"{self.OPEN}/?go=five"]]),
            ({"url": "/?go=five"}, [f"{self.OPEN}/#/today"], [["focus", f"{self.OPEN}/#/today"], ["navigate", f"{self.OPEN}/?go=five"]]),
            ({"url": "/?go=five"}, [far, f"{self.OPEN}/#/five"], [["focus", f"{self.OPEN}/#/five"], ["navigate", f"{self.OPEN}/?go=five"]]),
            ({"url": "/?go=five"}, [far], [["openWindow", f"{self.OPEN}/?go=five"]]),
            ({"url": "/#/five"}, [], [["openWindow", f"{self.OPEN}/?go=five"]]),
            ({"url": "https://evil.example/?go=five"}, [], [["openWindow", f"{self.OPEN}/?go=today"]]),
            ({"url": "//evil.example"}, [f"{self.OPEN}/"], [["focus", f"{self.OPEN}/"], ["navigate", f"{self.OPEN}/?go=today"]]),
            ({"url": "/?go=https://evil.example"}, [], [["openWindow", f"{self.OPEN}/?go=today"]]),
            ({"url": 5}, [], [["openWindow", f"{self.OPEN}/?go=today"]]),
            ({}, [], [["openWindow", f"{self.OPEN}/?go=today"]]),
            (None, [], [["openWindow", f"{self.OPEN}/?go=today"]]),
        ]
        out = run_node(WORKER_HARNESS + "(async () => { console.log(JSON.stringify(await Promise.all(DATA.map(([data, windows]) => tap(data, windows))))); })();",
                       [[data, windows] for data, windows, _ in cases])
        for (data, windows, want), log in zip(cases, out):
            self.assertEqual(log, want, f"{data!r} with {windows!r}")

    def test_a_payload_url_is_checked_when_the_nudge_is_shown(self):
        urls = [push.TEST_MESSAGE["url"], push.recall_message(1, 5)["url"], push.plan_message(2, 30)["url"],
                "/#/five", "https://evil.example/", "/?go=a%2Fb", None]
        out = run_node(WORKER_HARNESS + "(async () => { console.log(JSON.stringify(await Promise.all(DATA.map((url) => receive({ title: 'Dojo', body: 'x', url }))))); })();", urls)
        self.assertEqual([log[0][2] for log in out],
                         ["/?go=phone", "/?go=five", "/?go=today", "/?go=five", "/?go=today", "/?go=today", "/?go=today"])


if __name__ == "__main__":
    unittest.main()
