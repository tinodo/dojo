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
from app.core import iso, utcnow  # noqa: E402
from app.prepare import Preparer  # noqa: E402
from app.sourcing.inventory import Inventory  # noqa: E402
from app.sourcing.state import skill_key  # noqa: E402
from tests.test_api import POST  # noqa: E402
from tests.test_newexam import DOCS_AT, LEARN, Web, done, html, make_app, post, skills_of  # noqa: E402

UNSOURCED = "Compare widget sizes for a workload"   # the mock's search finds nothing for a skill with "sizes" in it


class TocWeb(Web):
    """The mock site, with a table of contents that holds a page no search finds."""

    def __init__(self) -> None:
        super().__init__()
        self.toc = [("Dimensioning guide", "dimensioning-guide")]

    def learn(self, path: str, params: httpx.QueryParams) -> httpx.Response:
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
        self.scan_for = "Dimensioning"       # the title scan names the pages whose title has this word
        self.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "teaches"   # a hub teaches nothing
        self._keep = threading.Lock()

    def chat(self, role: str, system: str, user: str, json_mode: bool = True) -> str:
        task = re.search(r"TASK: (\w+)", system).group(1)
        with self._keep:
            self.prompts.append((role, task, system, user))
            if self.fail.get(task):
                self.fail[task] -= 1
                raise AIError(f"the {role} model is down")
        return super().chat(role, system, user, json_mode)

    def web_search(self, query, domains=(), max_searches=1):
        with self._keep:
            self.web_calls.append((query, domains, max_searches))
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

    def test_a_skill_with_only_partly_covering_pages_is_written_and_keeps_searching(self):
        ai = Hooked()
        ai.grades = lambda role, title, text: "no" if "documentation" in title.lower() else "covers"
        ai.set_answer = lambda role: "partly"
        self.build(ai=ai)
        self.assertEqual(self.result["sourced"], 6, "pages that cover part of a skill still teach what they cover")
        book = self.book("Implement widget backups")
        self.assertEqual(book["state"], "searching")
        self.assertTrue(book["partial"] and book["activated"]["partial"] and book["growing"])
        st = self.c.get("/api/studio/sourcing").json()["exams"][0]
        self.assertEqual(st["complete"], False)
        self.assertEqual(st["counts"].get("covered", 0), 0)

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

    def test_removing_the_exam_forgets_what_the_search_kept(self):
        self.build()
        self.steps()
        self.assertTrue(self.store.names("sourcing", "xy-110"))
        post(self.c, "/api/exam-packages/xy-110/remove", {"confirm": "REMOVE"})
        self.assertEqual(self.store.names("sourcing", "xy-110"), [])
        self.assertIsNone(self.store.read("sourcing", "xy-110", default=None))


def parse_next(book: dict):
    from app.core import parse_iso
    return parse_iso(book["next"])


class AuditTests(SourcingCase):
    """Pages cited before this search existed: judged page by page, a report; a skill whose pages both models
    refuse is searched again and gets new pages only while its lesson has no film."""

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

    def test_pages_both_models_refuse_are_kept_while_the_lesson_has_a_film(self):
        self.build()
        slug = self.legacy()
        before = self.skill(UNSOURCED)["sources"]
        self.ai.grades = lambda role, title, text: "no" if slug.replace("-", " ") == title.lower() else "teaches"
        with mock.patch.object(self.sourcer, "_filmed", lambda pid, sid: True):
            self.steps()
            self.later(hours=21)
            self.steps()
        self.assertEqual(self.skill(UNSOURCED)["sources"], before)
        self.assertTrue(self.book(UNSOURCED)["replacement_waits"])

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
