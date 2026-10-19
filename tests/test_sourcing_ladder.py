"""The search for the official pages of an added exam (app/sourcing/ladder.py, ADR 0018), end to end on a mock of
Learn and GitHub Docs with the stand-in models: what the build keeps, what the background steps add, what two models
must agree on, what a restart repeats (nothing), what a failure never turns into, and what is never changed.

Nothing here touches the network. The mock serves small pages written for these tests (EG-39)."""
import json
import os
import re
import tempfile
import threading
import unittest
from datetime import timedelta
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.sourcing.ladder as ladder  # noqa: E402
from app import readiness  # noqa: E402
from app.ai import AIError, StubAI  # noqa: E402
from app.core import iso, utcnow, current_spend  # noqa: E402
from app.learning import LESSON_TEMPLATE  # noqa: E402
from app.prepare import Preparer  # noqa: E402
from app.sourcing.inventory import Inventory  # noqa: E402
from app.sourcing.state import LeaseLost, new_skill, skill_key  # noqa: E402
from app.sourcing.verify import Verifier  # noqa: E402
from tests.test_api import POST  # noqa: E402
from tests.test_newexam import DOCS_AT, LEARN, Web, done, html, make_app, post, skills_of  # noqa: E402

UNSOURCED = "Compare widget sizes for a workload"   # the mock's search finds nothing for a skill with "sizes" in it


def word_in(word: str, path: str) -> bool:
    return path.startswith(DOCS_AT) and word in path


class TocWeb(Web):
    """The mock site, with a table of contents that holds a page no search finds."""

    def __init__(self) -> None:
        super().__init__()
        self.toc = [("Dimensioning guide", "dimensioning-guide")]
        self.versioned: set[str] = set()   # pages with one of these words now redirect to the old versions of the documentation
        self.flaky: dict[str, int] = {}    # a word of a page's address -> how many times it answers 503 before it answers

    def learn(self, path: str, params: httpx.QueryParams) -> httpx.Response:
        for word in [w for w, left in self.flaky.items() if left > 0 and word_in(w, path)]:
            self.flaky[word] -= 1
            return httpx.Response(503)
        if path.startswith(DOCS_AT) and any(word in path for word in self.versioned):
            return httpx.Response(302, headers={"location": "/en-us/previous-versions/contoso/widgets/" + path.rsplit("/", 1)[-1]})
        if path.startswith("/en-us/previous-versions/"):
            what = path.rsplit("/", 1)[-1].replace("-", " ").capitalize()
            return html(f"<html><body><main><h1>An old version</h1><p>{what} was done differently in the old versions of the Contoso widget documentation. "
                        "This page describes a version of the service that is no longer supported, with the settings it had at that time and the steps "
                        "that operators followed to change them in the portal of those years.</p></main></body></html>")
        if path == DOCS_AT + "toc.json":
            return httpx.Response(200, json={"items": [{"toc_title": "Reference",
                                                        "children": [{"toc_title": t, "href": h} for t, h in self.toc]}]})
        r = super().learn(path, params)
        if path.startswith(DOCS_AT) and r.status_code == 200 and "<head>" in r.text:
            return html(r.text.replace("</head>", '<meta name="toc_rel" content="toc.json" /></head>', 1))
        return r


class Hooked(StubAI):
    """The stand-in models with every prompt kept and ways to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[tuple[str, str, str, str]] = []
        self.fail: dict[str, int] = {}       # task -> the next calls of it that fail
        self.fail_web = False
        self.web_calls: list[tuple] = []
        self.spends: list = []               # what each model call was attributed to (app.core.current_spend)
        self.scan_for = "Dimensioning"       # the title scan names the pages whose title has this word
        self.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "teaches"   # a hub teaches nothing
        self._keep = threading.Lock()

    def chat(self, role: str, system: str, user: str, json_mode: bool = True) -> str:
        task = re.search(r"TASK: (\w+)", system).group(1)
        with self._keep:
            self.prompts.append((role, task, system, user))
            self.spends.append(current_spend())
            if self.fail.get(task):
                self.fail[task] -= 1
                raise AIError(f"the {role} model is down")
        return super().chat(role, system, user, json_mode)

    def web_search(self, query, domains=(), max_searches=1):
        with self._keep:
            self.web_calls.append((query, domains, max_searches))
            self.spends.append(current_spend())
        if self.fail_web:
            raise AIError("web search is down")
        return super().web_search(query, domains, max_searches)

    def calls_of(self, task: str, role: str | None = None) -> int:
        return sum(1 for r, t, _, _ in self.prompts if t == task and role in (None, r))

    def _scan_titles(self, user: str) -> dict:
        skills = re.findall(r"^\[(\d+)\] ", user.split("<skills>")[-1].split("</skills>")[0], re.M)
        pages = [int(n) for n, line in re.findall(r"^\[(\d+)\] (.+)$", user.split("<pages>")[-1].split("</pages>")[0], re.M) if self.scan_for in line]
        return {"matches": [{"skill": int(k), "pages": pages[:2]} for k in skills]}


class SourcingCase(unittest.TestCase):
    def build(self, web: Web | None = None, ai: Hooked | None = None, code: str = "xy-110", r3: bool = False) -> dict:
        """Add the exam. The mock's pages all sit under /widgets/, a word of every skill, so the inventory's own
        search would find any of them: unless a test is about it (`r3`), it finds nothing, and only the title scan can."""
        if not r3:
            patch = mock.patch.object(Inventory, "search", lambda self, queries, top=30: [])
            patch.start()
            self.addCleanup(patch.stop)
        self.pid = code
        self.tmp = tempfile.mkdtemp(prefix="dojo-sourcing-")
        self.web, self.ai = web or TocWeb(), ai or Hooked()
        self.app = make_app(self.tmp, self.web, self.ai)
        self.c = TestClient(self.app)
        self.dojo = self.app.state.dojo
        self.store = self.dojo.store
        self.nx = self.app.state.newexams
        self.sourcer = self.nx.sourcer
        self.clock = [utcnow()]
        self.sourcer.now = lambda: self.clock[0]
        self.result = done(self.c, post(self.c, "/api/exam-packages", {"code": code})["job"])
        return self.result

    def raw(self) -> dict:
        return self.store.read("packages", self.pid)

    def skill(self, text: str) -> dict:
        return next(s for s in skills_of(self.raw()) if s["text"] == text)

    def book(self, text: str) -> dict:
        raw = self.raw()
        for d in raw["domains"]:
            for g in d["groups"]:
                for s in g["skills"]:
                    if s["text"] == text:
                        return self.store.read("sourcing", self.pid, skill_key(d["title"], g["title"], text), default=None)
        raise AssertionError(text)

    def steps(self, limit: int = 40) -> int:
        n = 0
        while self.sourcer.step():
            n += 1
            self.assertLess(n, limit, "the search never ends")
        return n

    def later(self, **delta) -> None:
        self.clock[0] = self.clock[0] + timedelta(**delta)


class BestTests(unittest.TestCase):
    """Which accepted pages a skill cites (ladder.best)."""

    @staticmethod
    def url(name: str) -> str:
        return LEARN + DOCS_AT + name

    def doc(self, direct, gate_drops):
        pages = [{"url": self.url(n), "title": n, "grade": "teaches", "by": ["r1u"], "rank": 1} for n in ("p", "q", "s")]
        return {"accepted": pages,
                "ranked": {"order": [self.url(n) for n in ("p", "q", "s")], "drop": [], "direct": [self.url(n) for n in direct],
                           "models": {"gate": {"drop": [self.url(n) for n in gate_drops]}, "author": {"drop": []}}}}

    def names(self, doc):
        return [p["url"].rsplit("/", 1)[-1] for p in ladder.best(doc)]

    def test_the_page_both_models_ranked_first_stays_though_one_of_them_dropped_it(self):
        self.assertEqual(self.names(self.doc(direct=["s"], gate_drops=["p"])), ["s", "p", "q"],
                         "the combined best page is never left out: only both models dropping a page leaves it out")

    def test_a_page_only_one_model_dropped_does_not_fill_a_place_beside_a_direct_page(self):
        self.assertEqual(self.names(self.doc(direct=["s"], gate_drops=["q"])), ["s", "p"])

    def test_without_a_direct_page_the_order_of_the_models_decides(self):
        self.assertEqual(self.names(self.doc(direct=[], gate_drops=["q"])), ["p", "q", "s"])


class BuildTests(SourcingCase):
    def test_the_build_keeps_what_two_models_accepted_and_goes_on_searching_for_the_rest(self):
        self.build()
        self.assertEqual(self.result, {"package": "xy-110", "skills": 7, "sourced": 6})
        gate, author = self.dojo.models("gate")["gate"], self.dojo.models("author")["author"]
        for s in skills_of(self.raw()):
            for ref in s["sources"]:
                self.assertEqual(ref["verified"]["by"], f"{gate}; {author}", "both families are named")
                self.assertTrue(self.dojo.sources.quote_ok(ref["verified"]["quote"], self.dojo.sources.get(ref["url"])))
        book = self.book(UNSOURCED)
        self.assertEqual((book["state"], book["round"], book["accepted"]), ("searching", 2, []))
        self.assertTrue(book["pool"] == {} or all(e["verdict"] != "accepted" for e in book["pool"].values()))
        notes = " ".join(self.raw().get("notes") or [])
        self.assertNotIn("no official page passed the check", notes, "the exam page says how far the search is, live")

    def test_the_exam_page_says_what_is_missing_and_there_is_no_repair_button(self):
        self.build()
        st = self.c.get("/api/exam-packages/xy-110").json()
        self.assertEqual(st["sourcing"], {"missing": 1, "incomplete": 1, "complete": False})
        for gone in ("repair", "repairing", "last_repair"):
            self.assertNotIn(gone, st)
        r = self.c.post("/api/exam-packages/xy-110/repair-sources", headers=POST)
        self.assertIn(r.status_code, (404, 405))
        with open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn("repair-sources", js)
        self.assertNotIn("Find sources for missing skills", js)

    def test_web_search_finds_a_page_the_other_searches_miss_and_sends_public_words_only(self):
        ai = Hooked()
        ai.search_urls = lambda q: [LEARN + DOCS_AT + "compare-widget-sizes?view=widgets"] if "sizes" in q else []
        self.build(ai=ai)
        self.assertEqual(self.result["sourced"], 7)
        [ref] = self.skill(UNSOURCED)["sources"]
        self.assertEqual(ref["url"], LEARN + DOCS_AT + "compare-widget-sizes", "no `view` in the address Dojo keeps")
        self.assertIn("r4", self.book(UNSOURCED)["accepted"][0]["by"])
        self.assertTrue(ai.web_calls)
        for query, domains, searches in ai.web_calls:
            self.assertEqual(domains, ("learn.microsoft.com",))
            self.assertIn("XY-110", query)
            self.assertLessEqual(len(query), 600)
            self.assertNotRegex(query, r"local:dev|learner|owner|token|key", "no learner data can reach the search")

    def test_web_search_asks_with_the_planners_phrase_for_every_product(self):
        ai = Hooked()
        planned = ai._plan_queries

        def five(user):
            out = planned(user)
            for s in out["skills"]:
                base = s["queries"][0]
                s["queries"] = [base, f"{base} how to", f"{base} title", f"{base} on Contoso gadgets", f"{base} on Contoso cogs"]
            return out

        ai._plan_queries = five
        self.build(ai=ai)
        mine = [q for q, _, _ in ai.web_calls if "Skill: Implement widget backups" in q]
        self.assertTrue(any(q.endswith("Skill: Implement widget backups") for q in mine))
        self.assertTrue(any(q.endswith("Skill: Implement widget backups on Contoso gadgets") for q in mine), "and its phrase for a product")
        self.assertTrue(any(q.endswith("Skill: Implement widget backups on Contoso cogs") for q in mine), "and for the other product")
        self.assertFalse(any(q.endswith("how to") or q.endswith("title") for q in mine), "not for every phrase of the planner")

    def test_every_model_call_of_the_build_is_attributed_to_the_job_that_asked(self):
        self.build()
        self.assertTrue(self.ai.spends)
        learners = {s.learner if s else None for s in self.ai.spends}
        self.assertNotIn(None, learners, "the calls of the threads carry the learner and the action of the job")
        self.assertEqual(len(learners), 1, learners)

    def test_the_inventory_search_finds_a_page_by_its_title(self):
        self.build(r3=True)
        self.assertEqual(self.result["sourced"], 7)
        [ref] = self.skill(UNSOURCED)["sources"]
        self.assertEqual(ref["url"], LEARN + DOCS_AT + "dimensioning-guide")
        self.assertIn("r3", self.book(UNSOURCED)["accepted"][0]["by"])

    def test_a_github_exam_searches_github_docs_and_the_web_for_github_docs(self):
        ai = Hooked()
        self.build(ai=ai, code="gh-990")
        self.assertTrue(ai.web_calls)
        for _, domains, _ in ai.web_calls:
            self.assertEqual(domains, ("docs.github.com",))

    def test_a_page_one_model_refuses_is_never_a_source(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if (role == "author" and "alerts" in title) or "documentation" in title.lower() else "teaches"
        self.build(ai=ai)
        alerts = self.skill("Configure widget alerts by using the Contoso portal")
        self.assertEqual(alerts["sources"], [])
        book = self.book("Configure widget alerts by using the Contoso portal")
        self.assertTrue(book["pool"])
        self.assertTrue(all(e["verdict"] == "rejected" and e["why"] == "split" for e in book["pool"].values()
                            if "alerts" in e["title"].lower()))

    def test_a_skill_with_only_partly_covering_pages_is_written_when_the_search_is_done_with_it(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "partly"
        self.build(ai=ai)
        self.assertEqual(self.result["sourced"], 0, "the build writes the skills that are covered, and only those")
        book = self.book("Implement widget backups")
        self.assertEqual(book["state"], "searching")
        self.assertTrue(book["partial"])
        self.assertFalse(book.get("activated"))
        self.assertEqual(self.skill("Implement widget backups")["sources"], [])
        self.steps()
        book = self.book("Implement widget backups")
        self.assertEqual((book["state"], book["round"]), ("partial", 4))
        self.assertTrue(book["activated"]["partial"], "pages that cover part of a skill still teach what they cover")
        self.assertTrue(self.skill("Implement widget backups")["sources"])
        st = self.c.get("/api/studio/sourcing").json()["exams"][0]
        self.assertEqual(st["complete"], False)
        self.assertEqual(st["counts"].get("covered", 0), 0)

    def test_every_page_of_the_round_is_judged_and_the_two_models_choose_the_pages_cited(self):
        ai = Hooked()
        extra = [LEARN + DOCS_AT + f"widget-backup-{w}" for w in ("alpha", "beta", "gamma", "delta")]
        ai.search_urls = lambda q: extra if "backups" in q else []

        def ranking(role, titles):
            numbers = range(1, len(titles) + 1)
            return (sorted(numbers, key=lambda i: (0 if "gamma" in titles[i - 1] else 1, i)),
                    [i for i in numbers if "alpha" in titles[i - 1] or "beta" in titles[i - 1]])

        ai.ranking = ranking
        self.build(ai=ai)
        book = self.book("Implement widget backups")
        self.assertGreaterEqual(len(book["accepted"]), 5, "judging did not stop at three accepted pages")
        self.assertTrue(all(e["verdict"] != "new" for e in book["pool"].values()), "the whole pool was judged")
        cited = [r["url"].rsplit("/", 1)[-1] for r in self.skill("Implement widget backups")["sources"]]
        self.assertEqual(cited[0], "widget-backup-gamma")
        self.assertFalse({"widget-backup-alpha", "widget-backup-beta"} & set(cited), "both models dropped them")
        self.assertEqual(len(cited), 3)
        self.assertEqual(sorted(book["ranked"]["drop"]), sorted(LEARN + DOCS_AT + f"widget-backup-{w}" for w in ("alpha", "beta")))
        self.assertEqual(ai.calls_of("rank_sources", "gate"), ai.calls_of("rank_sources", "author"))
        [row] = [d for d in self.sourcer.status("xy-110")["details"] if d["text"] == "Implement widget backups"]
        self.assertEqual(sum(1 for a in row["accepted"] if a["cited"]), 3, "Studio shows which accepted pages the lesson cites")

    def test_a_page_that_moved_to_the_old_versions_reaches_no_model_when_the_accepted_pages_are_compared(self):
        ai = Hooked()
        ai.search_urls = lambda q: [LEARN + DOCS_AT + f"widget-backup-{w}" for w in ("alpha", "beta")] if "backups" in q else []
        self.build(ai=ai)
        text = "Implement widget backups"
        doc = self.book(text)
        self.assertTrue(any(a["url"].endswith("widget-backup-alpha") for a in doc["accepted"]))
        self.web.versioned.add("alpha")
        key = self.dojo.sources.key(next(a["url"] for a in doc["accepted"] if a["url"].endswith("widget-backup-alpha")))
        stored = self.store.read("sources", key)
        stored["fetched_at"] = iso(utcnow() - timedelta(days=10))   # its stored copy is old: the page is read again
        self.store.write("sources", key, value=stored)
        doc["ranked"], doc["steps"] = None, {}   # the comparison made at the build is forgotten
        run = ladder.Run(self.sourcer, self.pid, {"code": "XY-110", "title": "x"})
        self.assertTrue(run.lease.acquire())
        w = {"doc": doc, "key": doc["key"], "ctx": {"code": "XY-110", "title": "x", "domain": "d", "group": "g", "text": text}}
        seen = len(ai.prompts)
        try:
            self.sourcer._rank(run, w)
        finally:
            run.lease.release()
        self.assertFalse(any(a["url"].endswith("widget-backup-alpha") for a in doc["accepted"]), "no longer accepted")
        self.assertEqual(doc["pool"][LEARN + DOCS_AT + "widget-backup-alpha"]["why"], "not-docs")
        for _, task, _, user in ai.prompts[seen:]:
            self.assertNotIn("widget-backup-alpha", user.lower().replace(" ", "-"), task)
            self.assertNotIn("old versions", user)

    def test_a_skill_is_covered_only_by_pages_its_package_cites(self):
        self.build()
        text = "Implement widget backups"
        doc = self.book(text)
        self.assertEqual(doc["state"], "covered")
        raw = self.raw()
        donor = next(s for s in skills_of(raw) if s["text"] != text and s["sources"])
        mine = next(s for s in skills_of(raw) if s["text"] == text)
        mine["sources"] = json.loads(json.dumps((donor["sources"] * 3)[:3]))   # a partial skill written earlier: three pages that are not these
        self.dojo.packages.add(raw)
        doc["activated"] = {"at": iso()}
        run = ladder.Run(self.sourcer, self.pid, {"code": "XY-110", "title": "x"})
        self.assertTrue(run.lease.acquire())
        try:
            self.assertFalse(self.sourcer._activate(run, {"doc": doc, "key": doc["key"], "id": mine["id"]}),
                             "its package keeps three other pages: it is not covered by what it cites")
        finally:
            run.lease.release()
        self.assertEqual([r["url"] for r in self.skill(text)["sources"]], [r["url"] for r in mine["sources"]], "and nothing was changed")

    def test_a_partial_skill_that_cites_three_pages_is_left_as_it_is(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "partly"
        ai.search_urls = lambda q: [LEARN + DOCS_AT + f"widget-backup-{w}" for w in ("alpha", "beta", "gamma", "delta")] if "backups" in q else []
        self.build(ai=ai)
        self.steps()
        text = "Implement widget backups"
        book = self.book(text)
        self.assertEqual((book["state"], book["cited"], book["stuck"]), ("partial", 3, True))
        self.assertEqual(len(self.skill(text)["sources"]), 3)
        cycles = book["cycles"]
        self.later(days=3)
        self.steps(limit=200)
        self.assertEqual(self.book(text)["cycles"], cycles, "nothing it could find could be written: it is not searched again")
        [row] = [d for d in self.sourcer.status("xy-110")["details"] if d["text"] == text]
        self.assertEqual(row["state"], "partial")
        self.assertEqual(sum(1 for a in row["accepted"] if a["cited"]), 3, "Studio shows what the package cites")

    def test_a_skill_with_no_page_that_explains_it_itself_is_not_covered_and_the_search_goes_on(self):
        ai = Hooked()
        text = "Implement widget backups"
        base = [LEARN + DOCS_AT + "widget-backup-alpha", LEARN + DOCS_AT + "widget-backup-beta"]
        later_round = lambda q: "Skill: Contoso" in q   # noqa: E731 - round 2 searches with the wording the planner chose after round 1
        ai.search_urls = lambda q: ([LEARN + DOCS_AT + "widget-backup-gamma"] if later_round(q) else base) if "backups" in q else []
        ai.set_answer = lambda role: "partly"
        ai.ranking = lambda role, titles: (list(range(1, len(titles) + 1)), [], [n for n, t in enumerate(titles, 1) if "gamma" in t])
        self.build(ai=ai)
        book = self.book(text)
        self.assertEqual((book["state"], book["round"]), ("covered", 2), "round 1 found no page that explains it itself, round 2 did")
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in self.skill(text)["sources"]][0], "widget-backup-gamma", "the direct page comes first")
        hints = [user for _, task, _, user in ai.prompts if task == "plan_queries" and "(only the same area)" in user]
        self.assertTrue(hints, "round 2's planner was told which accepted pages were only the same area")

    def test_a_page_one_model_dropped_does_not_fill_a_place_beside_a_direct_page(self):
        text = "Implement widget backups"
        for direct, cited_alpha in ((True, False), (False, True)):
            ai = Hooked()
            ai.search_urls = lambda q: [LEARN + DOCS_AT + f"widget-backup-{w}" for w in ("alpha", "gamma")] if "backups" in q else []

            def ranking(role, titles, direct=direct):
                numbers = list(range(1, len(titles) + 1))
                alpha = [n for n in numbers if "alpha" in titles[n - 1]]
                gamma = [n for n in numbers if "gamma" in titles[n - 1]]
                return numbers, (alpha if role == "gate" else []), (gamma if direct else [])

            ai.ranking = ranking
            self.build(ai=ai, code="xy-110")
            cited = [r["url"].rsplit("/", 1)[-1] for r in self.skill(text)["sources"]]
            self.assertEqual("widget-backup-alpha" in cited, cited_alpha, (direct, cited))
            if direct:
                self.assertEqual(cited[0], "widget-backup-gamma")

    def test_pages_both_models_drop_never_leave_a_skill_without_a_page(self):
        ai = Hooked()
        ai.search_urls = lambda q: [LEARN + DOCS_AT + "widget-backup-alpha"] if "backups" in q else []
        ai.ranking = lambda role, titles: (list(range(1, len(titles) + 1)), list(range(1, len(titles) + 1)))
        self.build(ai=ai)
        self.assertEqual(self.result["sourced"], 6)
        self.assertEqual(len(self.skill("Implement widget backups")["sources"]), 1, "only the best page stays")

    def test_the_exam_is_added_when_every_skill_is_only_partly_covered(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "partly"
        self.build(ai=ai)
        st = self.c.get("/api/exam-packages/xy-110").json()
        self.assertEqual(st["state"], "added")
        self.assertEqual(st["matching"]["sourced"], 0)

    def test_a_split_set_of_pages_is_one_look_more_not_a_yes(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "yes" if role == "gate" else "partly"
        self.build(ai=ai)
        self.assertEqual(ai.calls_of("complete_set", "gate"), ai.calls_of("complete_set", "author"))
        self.assertGreaterEqual(ai.calls_of("complete_set", "gate"), 12, "a split is asked again with more of each page")


class BackgroundTests(SourcingCase):
    def test_the_title_scan_finds_a_page_no_search_finds(self):
        self.build()
        self.assertEqual(self.skill(UNSOURCED)["sources"], [])
        self.steps()
        [ref] = self.skill(UNSOURCED)["sources"]
        self.assertEqual(ref["url"], LEARN + DOCS_AT + "dimensioning-guide")
        book = self.book(UNSOURCED)
        self.assertEqual((book["state"], book["round"]), ("covered", 3))
        self.assertIn("r7", book["accepted"][0]["by"])
        self.assertEqual(book["activated"]["generation"], 1)
        pkg = self.dojo.packages.get("xy-110")
        self.assertTrue(pkg["skills"][self.skill(UNSOURCED)["id"]]["sources"], "the package in use has it too")
        st = self.c.get("/api/exam-packages/xy-110").json()
        self.assertEqual(st["sourcing"], {"missing": 0, "incomplete": 0, "complete": True})

    def test_exhausted_means_every_step_ran_and_nothing_was_accepted(self):
        self.build()
        self.ai.scan_for = "no title has this word"
        self.steps()
        book = self.book(UNSOURCED)
        self.assertEqual((book["state"], book["round"], book["accepted"]), ("exhausted", 4, []))
        self.assertEqual(book["cycles"], 1)
        self.assertGreater(parse_next(book), self.clock[0], "it comes back tomorrow")
        self.assertEqual(self.skill(UNSOURCED)["sources"], [])
        st = self.sourcer.status("xy-110")
        self.assertEqual(st["counts"]["exhausted"], 1)
        self.assertFalse(st["complete"])
        self.assertEqual(self.steps(), 0, "nothing is due before tomorrow")

    def test_an_exhausted_skill_is_searched_again_after_a_day_with_new_words(self):
        self.build()
        self.ai.scan_for = "no title has this word"
        self.steps()
        first = self.book(UNSOURCED)["plan"]
        self.ai.scan_for = "Dimensioning"
        self.later(days=1, minutes=1)
        self.steps()
        book = self.book(UNSOURCED)
        self.assertEqual(book["state"], "covered")
        self.assertEqual(book["cycle"], 2)
        self.assertEqual(set(first) - {"1", "2"}, set())

    def test_a_failed_step_waits_and_repeats_nothing_that_was_paid_for(self):
        self.build()
        scans_before = self.ai.calls_of("scan_titles")
        self.ai.fail["verify_graded"] = 1   # the first judging after the title scan fails
        self.sourcer.step()
        book = self.book(UNSOURCED)
        self.assertEqual((book["state"], book["round"]), ("searching", 2))
        self.assertIn("is down", book["failure"])
        scans = self.ai.calls_of("scan_titles") - scans_before
        self.assertGreaterEqual(scans, 1)
        self.assertFalse(self.sourcer.step() and self.book(UNSOURCED)["round"] > 2, "it waits for its pause")
        self.later(minutes=6)
        self.steps()
        self.assertEqual(self.ai.calls_of("scan_titles") - scans_before, scans, "the windows already read are not read again")
        self.assertEqual(self.book(UNSOURCED)["state"], "covered")

    def test_a_step_that_failed_three_times_blocks_the_skill_and_never_exhausts_it(self):
        self.build()
        self.ai.scan_for = "no title has this word"
        self.ai.fail_web = True
        for _ in range(8):
            self.sourcer.step()
            self.later(hours=1)
        book = self.book(UNSOURCED)
        self.assertEqual(book["state"], "blocked")
        self.assertIn("web search", book["blocked"]["error"])
        self.assertNotEqual(book["state"], "exhausted")
        st = self.sourcer.status("xy-110")
        [row] = [d for d in st["details"] if d["text"] == UNSOURCED]
        self.assertEqual(row["state"], "blocked")
        self.assertTrue(row["blocked"]["step"])
        self.ai.fail_web = False
        self.later(days=1)
        self.steps()
        book = self.book(UNSOURCED)
        self.assertEqual((book["state"], book["cycle"]), ("exhausted", 2), "retried the next day with fresh tries; now every step ran")

    def test_the_days_model_calls_pause_the_work_and_end_no_skill(self):
        self.build()
        with mock.patch.object(ladder, "DAY_CALLS", 1):
            self.assertFalse(self.sourcer.step())
        self.assertIn("paused until tomorrow", self.sourcer.diag()["note"])
        self.assertEqual(self.book(UNSOURCED)["state"], "searching")

    def test_a_skill_that_has_sources_is_never_changed(self):
        self.build()
        before = json.dumps([s["sources"] for s in skills_of(self.raw()) if s["text"] != UNSOURCED], sort_keys=True)
        self.steps()
        self.later(days=40)
        self.steps()
        after = json.dumps([s["sources"] for s in skills_of(self.raw()) if s["text"] != UNSOURCED], sort_keys=True)
        self.assertEqual(before, after)

    def test_an_old_note_in_the_package_goes(self):
        self.build()
        raw = self.raw()
        raw["notes"] = list(raw["notes"]) + ["For 1 of 7 skills no official page passed the check, so Dojo does not teach them or ask about them."]
        self.dojo.packages.add(raw)
        self.steps()
        self.assertFalse([n for n in self.raw()["notes"] if "no official page passed the check" in n])

    def test_the_lease_keeps_two_processes_apart(self):
        self.build()
        other = ladder.Sourcer(self.dojo, self.nx)
        other.now = self.sourcer.now
        run = ladder.Run(self.sourcer, "xy-110", {"code": "XY-110", "title": "x"})
        self.assertTrue(run.lease.acquire())
        run2 = ladder.Run(other, "xy-110", {"code": "XY-110", "title": "x"})
        self.assertFalse(run2.lease.acquire(), "the first process holds it")
        self.assertFalse(other.step(), "so the second does nothing")
        self.assertEqual(self.book(UNSOURCED)["round"], 2)
        run.lease.release()

    def test_a_step_still_running_when_the_exam_is_removed_writes_nothing_more(self):
        self.build()
        run = ladder.Run(self.sourcer, "xy-110", {"code": "XY-110", "title": "x"})
        self.assertTrue(run.lease.acquire())
        key = skill_key("d", "g", "t")
        post(self.c, "/api/exam-packages/xy-110/remove", {"confirm": "REMOVE"})
        with self.assertRaises(LeaseLost):
            run.books.save_skill(new_skill(key))
        with self.assertRaises(LeaseLost):   # and a skill is never written back into the removed package
            self.sourcer._activate(run, {"doc": new_skill(key), "key": key})
        self.assertEqual(self.store.names("sourcing", "xy-110"), [])
        self.assertIsNone(self.store.read("packages", "xy-110", default=None))

    def test_the_package_lock_is_the_one_the_removal_holds(self):
        self.build()
        self.assertIs(self.nx.sourcer.package_lock, self.sourcer._package)

    def test_the_pause_is_as_long_as_the_next_due_step_needs(self):
        self.build()
        self.ai.scan_for = "no title has this word"
        self.assertEqual(self.sourcer.wait_s(), 15.0, "a skill is due now")
        self.ai.fail["scan_titles"] = 1
        self.sourcer.step()   # the title scan fails: the skill waits five minutes
        self.assertIn("is down", self.book(UNSOURCED)["failure"])
        self.assertTrue(15 <= self.sourcer.wait_s() <= 300, self.sourcer.wait_s())
        self.later(minutes=6)
        self.steps()
        self.assertEqual(self.book(UNSOURCED)["state"], "exhausted")
        self.assertEqual(self.sourcer.wait_s(), 1800.0, "nothing is due before tomorrow")

    def test_the_pause_is_long_when_the_days_calls_are_used(self):
        self.build()
        with mock.patch.object(ladder, "DAY_CALLS", 1):
            self.assertEqual(self.sourcer.wait_s(), 1800.0)

    def test_the_last_round_tries_again_a_page_that_failed_in_it(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "partly"
        late = LEARN + DOCS_AT + "widget-backup-late"
        ai.search_urls = lambda q: [late] if "backups" in q and q.endswith("official documentation") else []   # only the last round's web search finds it
        web = TocWeb()
        web.flaky["widget-backup-late"] = 1   # the page cannot be read the first time it is asked for
        self.build(web=web, ai=ai)
        self.steps()
        e = self.book("Implement widget backups")["pool"][late]
        self.assertIn(e["verdict"], ("accepted", "rejected"), "judged, not left as unread")
        self.assertEqual(e["verdict"], "accepted", e)
        self.assertEqual(self.book("Implement widget backups")["round"], 4)

    def test_a_restart_in_the_second_pass_of_the_last_round_judges_the_page_it_was_retrying(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "partly"
        late = LEARN + DOCS_AT + "widget-backup-late"
        ai.search_urls = lambda q: [late] if "backups" in q and q.endswith("official documentation") else []
        web = TocWeb()
        web.flaky["widget-backup-late"] = 1   # unread in the first pass of the last round
        asked = []
        real = Verifier.grade_pages

        def grade(verifier, ctx, cands, **kw):
            if any(c["url"] == late for c in cands):
                asked.append(1)
                if len(asked) == 2:
                    raise AIError("the models are down while the page is tried again")   # the second pass stops half way
            return real(verifier, ctx, cands, **kw)

        with mock.patch.object(Verifier, "grade_pages", grade):
            self.build(web=web, ai=ai)
            for _ in range(5):
                self.steps()
                self.later(minutes=6)
        e = self.book("Implement widget backups")["pool"][late]
        self.assertEqual(e["verdict"], "accepted", e)
        self.assertGreaterEqual(len(asked), 3, "read once and not read, stopped in the retry, judged after the restart")
        self.assertNotEqual(e.get("why"), "repeat", "what was done before the restart is not mistaken for the retry that stopped")

    def test_the_last_round_judges_every_page_that_is_left(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "partly"
        ai.search_urls = lambda q: [LEARN + DOCS_AT + f"widget-backup-{n:02d}" for n in range(14)] if "backups" in q else []
        with mock.patch.object(ladder, "VERIFY_LIMIT", ladder.SLICE):   # one batch of five a round: most of the pool is left
            self.build(ai=ai)
            self.assertTrue(any(e["verdict"] == "new" for e in self.book("Implement widget backups")["pool"].values()),
                            "the first rounds leave pages unjudged")
            self.steps()
        book = self.book("Implement widget backups")
        self.assertEqual(book["round"], 4)
        self.assertFalse([u for u, e in book["pool"].items() if e["verdict"] == "new"], "the last round of the cycle judged them all")

    def test_a_judging_that_keeps_failing_blocks_the_skill_instead_of_trying_for_ever(self):
        self.build()
        self.ai.fail["verify_graded"] = 10_000   # the models cannot judge a page now
        for _ in range(12):
            self.sourcer.step()
            self.later(hours=2)   # past every pause
        book = self.book(UNSOURCED)
        self.assertEqual(book["state"], "blocked")
        self.assertIn(".verify.", book["blocked"]["step"])
        tries = [s["tries"] for k, s in book["steps"].items() if ".verify." in k and not s["done"]]
        self.assertEqual(tries, [3], "the same batch, tried three times: a repeat is the same step, not a new one")

    def test_a_changed_source_set_makes_the_lesson_be_written_again(self):
        self.build()
        deeper = self.app.state.deeper
        skill = next(s for s in skills_of(self.raw()) if s["sources"])
        live = self.dojo.packages.get("xy-110")["skills"][skill["id"]]
        meta = {"template": LESSON_TEMPLATE, "offered": [r["url"] for r in skill["sources"]]}
        self.assertIsNone(deeper.why(live, meta), "a lesson written from the pages the package cites is left alone")
        meta["offered"] = [LEARN + DOCS_AT + "the-page-an-earlier-lesson-was-written-from"]
        self.assertEqual(deeper.why(live, meta), "pages", "the preparer writes it again (ADR 0008); the old lesson stays until the new one holds")

    def test_removing_the_exam_forgets_what_the_search_kept(self):
        self.build()
        self.steps()
        self.assertTrue(self.store.names("inventory"), "the exam's table of contents were read")
        self.assertTrue(self.store.names("sourcing", "xy-110"))
        post(self.c, "/api/exam-packages/xy-110/remove", {"confirm": "REMOVE"})
        self.assertEqual(self.store.names("sourcing", "xy-110"), [])
        self.assertIsNone(self.store.read("sourcing", "xy-110", default=None))
        self.assertFalse(self.store.path("sourcing", "xy-110.lease").exists(), "a re-added exam is not kept waiting for a lease")
        self.assertEqual(self.store.names("inventory"), [], "the inventory of the removed exam goes with it")


def parse_next(book: dict):
    from app.core import parse_iso
    return parse_iso(book["next"])


class AuditTests(SourcingCase):
    """Pages cited before this search existed: judged page by page, a report; a skill whose pages both models
    refuse is searched again and gets new pages, whether or not its lesson has a film (ADR 0021)."""

    def legacy(self) -> str:
        """Give the skill without sources the pages another skill cites, as an older Dojo would have kept them."""
        raw = self.raw()
        donor = next(s for s in skills_of(raw) if s["sources"])
        target = next(s for s in skills_of(raw) if s["text"] == UNSOURCED)
        target["sources"] = json.loads(json.dumps(donor["sources"][:1]))
        self.dojo.packages.add(raw)
        return donor["sources"][0]["url"].rsplit("/", 1)[-1]

    def test_supported_pages_are_reported_and_left_alone(self):
        self.build()
        slug = self.legacy()
        before = self.skill(UNSOURCED)["sources"]
        self.steps()
        report = self.sourcer.status("xy-110")["details"]
        [row] = [d for d in report if d["text"] == UNSOURCED]
        self.assertEqual(row["audit"]["verdict"], "supported")
        self.assertEqual(self.skill(UNSOURCED)["sources"], before)
        self.assertIn(slug, json.dumps(row["audit"]))

    def test_pages_both_models_refuse_are_replaced_when_there_is_no_film(self):
        self.build()
        slug = self.legacy()
        before = self.skill(UNSOURCED)["sources"]
        self.ai.grades = lambda role, title, text: "no" if slug.replace("-", " ") == title.lower() else "teaches"
        self.steps()
        row = next(d for d in self.sourcer.status("xy-110")["details"] if d["text"] == UNSOURCED)
        self.assertEqual(row["audit"]["verdict"], "invalid")
        self.assertFalse(row["audit"].get("confirmed"), "refused once: nothing is changed on one look")
        self.assertEqual(self.skill(UNSOURCED)["sources"], before)
        self.later(hours=21)
        self.steps()   # a day later the same refusal confirms it, and the skill goes back to the search
        urls = [s["url"] for s in self.skill(UNSOURCED)["sources"]]
        self.assertTrue(urls and not any(u.endswith("/" + slug) for u in urls), urls)
        self.assertEqual(self.book(UNSOURCED)["state"], "covered")

    def test_a_page_one_model_still_accepts_never_makes_the_audit_call_it_invalid(self):
        self.build()
        slug = self.legacy()
        before = self.skill(UNSOURCED)["sources"]
        same = lambda title: slug.replace("-", " ") == title.lower()   # noqa: E731
        self.ai.grades = lambda role, title, text: ("teaches" if role == "gate" else "no") if same(title) else "teaches"
        self.steps()
        self.later(hours=21)
        self.steps()
        row = next(d for d in self.sourcer.status("xy-110")["details"] if d["text"] == UNSOURCED)
        self.assertEqual(row["audit"]["verdict"], "weak", "a split is not a refusal")
        self.assertEqual(self.skill(UNSOURCED)["sources"], before)

    def test_a_check_that_keeps_failing_is_given_up_after_three_tries(self):
        self.build()
        self.legacy()
        self.ai.fail["verify_graded"] = 1000
        with self.assertLogs("dojo.sourcing", level="ERROR"):
            for _ in range(6):
                self.sourcer.step()
        row = next(d for d in self.sourcer.status("xy-110")["details"] if d["text"] == UNSOURCED)
        self.assertEqual(row["audit"]["verdict"], "unchecked")
        paid = self.ai.calls_of("verify_graded")
        self.assertFalse(self.sourcer.step())
        self.assertEqual(self.ai.calls_of("verify_graded"), paid, "nothing more is paid for")

    def test_pages_both_models_refuse_are_replaced_though_the_lesson_has_a_film(self):
        self.build()
        slug = self.legacy()
        self.ai.grades = lambda role, title, text: "no" if slug.replace("-", " ") == title.lower() else "teaches"
        with mock.patch.object(self.sourcer, "_filmed", lambda pid, sid: True):
            self.steps()
            self.later(hours=21)
            self.steps()
        urls = [s["url"] for s in self.skill(UNSOURCED)["sources"]]
        self.assertTrue(urls and not any(u.endswith("/" + slug) for u in urls), "a film does not keep pages both models refuse")
        book = self.book(UNSOURCED)
        self.assertEqual(book["state"], "covered")
        self.assertIsNone(book["replacing"])
        self.assertNotIn("replacement_waits", book)

    def held_back(self) -> tuple[list, list]:
        """The state a filmed skill was left in before ADR 0021: its refused page cited, the replacement waiting in the
        book (`partial`, `stuck`). Returns the refused pages and the pages the search accepted for it."""
        self.build()
        slug = self.legacy()
        before = self.skill(UNSOURCED)["sources"]
        self.ai.grades = lambda role, title, text: "no" if slug.replace("-", " ") == title.lower() else "teaches"
        self.steps()
        self.later(hours=21)
        self.steps()
        replaced = self.skill(UNSOURCED)["sources"]
        self.assertNotEqual(replaced, before)
        self.put_pages(before)
        book = self.book(UNSOURCED)
        book.update(state="partial", stuck=True, replacing=[r["url"] for r in before], replacement_waits=True)
        self.store.write("sourcing", self.pid, book["key"], value=book)
        return before, replaced

    def put_pages(self, pages: list) -> None:
        raw = self.raw()
        next(s for s in skills_of(raw) if s["text"] == UNSOURCED)["sources"] = pages
        self.dojo.packages.add(raw)

    def generation(self) -> int:
        return self.sourcer.status("xy-110")["generation"]

    def test_a_replacement_the_earlier_rule_held_back_for_a_film_is_written_by_the_next_step_without_a_model_call(self):
        before, replaced = self.held_back()
        generation, asked = self.generation(), len(self.ai.prompts)
        self.assertTrue(self.sourcer.step(), "writing the held pages is the step's work")
        self.assertEqual(self.skill(UNSOURCED)["sources"], replaced)
        book = self.book(UNSOURCED)
        self.assertIsNone(book["replacing"])
        self.assertNotIn("replacement_waits", book)
        self.assertEqual(book["state"], "covered", "what the package cites now is what covers the skill")
        self.steps()
        self.assertEqual(self.generation(), generation + 1, "written once, and nothing else was")
        self.assertEqual(len(self.ai.prompts), asked, "nothing is searched and no model is asked")

    def test_a_held_replacement_stays_partial_when_the_new_pages_do_not_make_the_skill_covered(self):
        self.held_back()
        with mock.patch.object(ladder.Sourcer, "_justified", staticmethod(lambda doc, cited: False)):
            self.assertTrue(self.sourcer.step())
        book = self.book(UNSOURCED)
        self.assertEqual((book["state"], book["stuck"], book["replacing"]), ("partial", True, None), "the search is done with it, as before")

    def given_up(self) -> tuple:
        """What a failed upgrade of the skill's lesson left behind in the deeper lessons' state."""
        deeper = self.nx.preparer.deeper
        key = f"{self.pid}/{self.skill(UNSOURCED)['id']}"
        record = deeper._doc()
        record["skills"][key] = {"lesson": "lesson-0", "tries": 3, "last": iso(), "outcome": "failed", "given_up": iso(), "error": "The author did not answer."}
        deeper._save(record)
        return deeper, key

    def test_a_held_replacement_is_not_written_again_when_the_package_already_cites_the_new_pages(self):
        before, replaced = self.held_back()
        self.put_pages(replaced)   # written by a step that stopped before it saved the book
        deeper, key = self.given_up()   # and before it cleared what failed for the lesson
        generation = self.generation()
        self.assertTrue(self.sourcer.step())
        self.assertEqual(self.skill(UNSOURCED)["sources"], replaced)
        self.assertEqual(self.generation(), generation, "nothing was written, so nothing is written again")
        self.assertNotIn("replacement_waits", self.book(UNSOURCED))
        self.assertNotIn(key, deeper._doc()["skills"], "the attempts at the old lesson are cleared all the same")

    def test_a_failure_to_clear_the_attempts_is_tried_again_by_the_next_step(self):
        before, replaced = self.held_back()
        deeper, key = self.given_up()
        with mock.patch.object(type(deeper), "start_again", side_effect=OSError("the disk is full")), self.assertLogs("dojo.sourcing", level="ERROR"):
            self.sourcer.step()
        self.assertTrue(self.book(UNSOURCED)["replacement_waits"], "the book was not saved as settled")
        self.assertIn(key, deeper._doc()["skills"])
        self.assertTrue(self.sourcer.step(), "the next step settles it")
        self.assertNotIn(key, deeper._doc()["skills"])
        self.assertNotIn("replacement_waits", self.book(UNSOURCED))

    def test_a_held_replacement_never_overwrites_pages_that_changed_since(self):
        before, replaced = self.held_back()
        taken = [r["url"] for r in before + replaced]
        mine = [next(r for s in skills_of(self.raw()) if s["text"] != UNSOURCED for r in s["sources"]
                     if not any(ladder.same_page(r["url"], u) for u in taken))]
        self.put_pages(mine)   # none of the refused pages is cited any more, and not the accepted ones either
        generation = self.generation()
        self.assertTrue(self.sourcer.step())
        self.assertEqual(self.skill(UNSOURCED)["sources"], mine, "what the package cites stays")
        self.assertEqual(self.generation(), generation)
        book = self.book(UNSOURCED)
        self.assertIsNone(book["replacing"])
        self.assertNotIn("replacement_waits", book)

    def test_a_held_replacement_waits_while_the_refusal_is_not_on_record(self):
        before, replaced = self.held_back()
        side = self.store.read("sourcing", self.pid, "side-audit")
        side["skills"][self.book(UNSOURCED)["key"]].pop("confirmed")
        self.store.write("sourcing", self.pid, "side-audit", value=side)
        self.assertFalse(self.sourcer.step())
        self.assertEqual(self.skill(UNSOURCED)["sources"], before)
        self.assertTrue(self.book(UNSOURCED)["replacement_waits"])

    def test_replacing_pages_clears_what_failed_before_for_the_lesson(self):
        self.build()
        slug = self.legacy()
        deeper, key = self.given_up()
        self.ai.grades = lambda role, title, text: "no" if slug.replace("-", " ") == title.lower() else "teaches"
        self.steps()
        self.assertIn(key, deeper._doc()["skills"], "while its pages stand, the attempts at its lesson stand")
        self.later(hours=21)
        self.steps()
        self.assertNotIn(key, deeper._doc()["skills"], "the new pages start the lesson afresh")

    def test_a_skill_whose_pages_were_replaced_no_longer_shows_the_old_verdict_as_current(self):
        self.build()
        slug = self.legacy()
        self.ai.grades = lambda role, title, text: "no" if slug.replace("-", " ") == title.lower() else "teaches"
        self.steps()
        row = next(d for d in self.sourcer.status("xy-110")["details"] if d["text"] == UNSOURCED)
        self.assertTrue(row["audit"]["current"], "it still cites the pages that were judged")
        self.later(hours=21)
        self.steps()
        row = next(d for d in self.sourcer.status("xy-110")["details"] if d["text"] == UNSOURCED)
        self.assertEqual(row["audit"]["verdict"], "invalid", "what was judged stays on record")
        self.assertFalse(row["audit"]["current"], "but the skill cites other pages now")

    def test_only_a_few_skills_a_day_go_back_to_the_search(self):
        self.build()
        slug = self.legacy()
        before = self.skill(UNSOURCED)["sources"]
        self.ai.grades = lambda role, title, text: "no" if slug.replace("-", " ") == title.lower() else "teaches"
        with mock.patch.object(ladder, "REPLACE_PER_DAY", 0):
            self.steps()
            self.later(hours=21)
            self.steps()
            row = next(d for d in self.sourcer.status("xy-110")["details"] if d["text"] == UNSOURCED)
            self.assertFalse(row["audit"].get("confirmed"))
            self.assertEqual(self.skill(UNSOURCED)["sources"], before)
        self.later(hours=21)
        self.steps()
        self.assertNotEqual(self.skill(UNSOURCED)["sources"], before, "the next day's allowance puts it back")


class ReadinessTests(unittest.TestCase):
    def rules(self, met: bool = True) -> list[dict]:
        return [{"id": "rule1", "met": met}, {"id": "rule2", "met": met}, {"id": "rule3", "met": met, "measured": True},
                {"id": "rule4", "met": met, "available": True, "value": "taken"}]

    def test_never_book_while_a_skill_has_no_page(self):
        sure = {"level": "high"}
        self.assertEqual(readiness.verdict(self.rules(), sure)["say"], "book")
        out = readiness.verdict(self.rules(), sure, untaught=3)
        self.assertEqual(out["say"], "close")
        self.assertIn("3 of the exam's skills", out["why"])
        self.assertEqual(readiness.verdict(self.rules(False), sure, untaught=3)["say"], "not-yet")


class PreparerTurnsTests(unittest.TestCase):
    """The search for pages takes every other turn while it has work, so lessons for what it found are written between."""

    def preparer(self, sourcing_steps: list[bool], lessons: list[bool]):
        order: list[str] = []
        p = Preparer.__new__(Preparer)
        p.sourcing = mock.Mock()
        p.sourcing.step.side_effect = lambda: (order.append("S"), sourcing_steps.pop(0))[1]
        p._after_sourcing = False
        p._learner_busy = lambda: False
        p._lessons_step = lambda: (order.append("L"), lessons.pop(0))[1]
        return p, order

    def test_the_two_take_turns(self):
        p, order = self.preparer([True, True, True, False], [True, True, True, True])
        self.assertEqual([p.step() for _ in range(6)], [True] * 6)
        self.assertEqual("".join(order), "SLSLSLSL"[:len(order)])
        self.assertEqual(order[:6], ["S", "L", "S", "L", "S", "L"])

    def test_with_no_lessons_to_write_the_search_still_goes_on(self):
        p, order = self.preparer([True, True, False], [False, False, False])
        self.assertTrue(p.step())    # S: worked
        self.assertTrue(p.step())    # L: nothing to write, but the search has more: come back soon
        self.assertTrue(p.step())    # S: worked
        self.assertTrue(p.step())    # L again, same
        self.assertFalse(p.step())   # S: nothing left; L: nothing to write: sleep
        self.assertEqual("".join(order), "SLSLSL")

    def test_the_pause_is_short_after_work_and_never_longer_than_the_search_needs(self):
        p = Preparer.__new__(Preparer)
        p.sourcing = mock.Mock()
        p.sourcing.wait_s.return_value = 240.0
        self.assertEqual(p.wait_after(True), 15)
        self.assertEqual(p.wait_after(False), 240.0)
        p.sourcing.wait_s.return_value = 5
        self.assertEqual(p.wait_after(False), 15.0)
        p.sourcing.wait_s.return_value = 99999
        self.assertEqual(p.wait_after(False), 1800.0)
        p.sourcing.wait_s.side_effect = RuntimeError("no")
        self.assertEqual(p.wait_after(False), 1800)
        p.sourcing = None
        self.assertEqual(p.wait_after(False), 1800)

    def test_a_learner_at_work_stops_both(self):
        p, order = self.preparer([True], [True])
        p._learner_busy = lambda: True
        self.assertTrue(p.step())
        self.assertEqual(order, [])


class StudioTests(SourcingCase):
    def test_studio_shows_every_skill_with_its_state_and_pages(self):
        self.build()
        data = self.c.get("/api/studio/sourcing").json()
        self.assertEqual(set(data), {"exams", "web_search", "search"})
        [exam] = data["exams"]
        self.assertEqual((exam["exam"], exam["skills"], exam["incomplete"]), ("XY-110", 7, 1))
        self.assertEqual(exam["counts"], {"covered": 6, "searching": 1})
        covered = [d for d in exam["details"] if d["state"] == "covered"]
        self.assertTrue(all(d["accepted"] and d["accepted"][0]["grade"] == "teaches" for d in covered))
        one = self.c.get("/api/exam-packages/xy-110/sourcing").json()
        self.assertEqual(one["counts"], exam["counts"])
        self.assertEqual(self.c.get("/api/exam-packages/nope-1/sourcing").status_code, 404)

    def test_the_diag_page_says_whether_web_search_works(self):
        ai = Hooked()
        ai.search_urls = lambda q: [LEARN + DOCS_AT + "a-page"]
        self.build(ai=ai)
        self.assertIsNone(self.c.get("/api/studio/sourcing").json()["web_search"], "nothing is searched just to ask")
        self.sourcer.step()
        self.assertTrue(self.c.get("/api/studio/sourcing").json()["web_search"]["ok"])
        self.assertEqual(self.c.get("/api/diag").json()["source_search"]["web_search"]["ok"], True)


if __name__ == "__main__":
    unittest.main()
