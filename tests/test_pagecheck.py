"""The two-model check of the pages a course cites (app/pagecheck.py, docs/adr/0019-studio-overview.md): a report with a
verdict for every skill, started by the owner, that changes nothing. The pages are those of an added exam on the mock of
Learn (the original courses' own pages are on the real site); the models are the stand-ins. Nothing touches the network (EG-39)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.pagecheck as pagecheck_mod  # noqa: E402
from app.core import UserError  # noqa: E402
from tests.test_api import POST  # noqa: E402
from tests.test_newexam import DOCS_AT, LEARN, Web, done, make_app, post  # noqa: E402
from tests.test_sourcing_ladder import Hooked  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"
SCRIPT = (APP_DIR / "static" / "app.js").read_text("utf-8")
PID = "xy-110"


def part(start: str) -> str:
    return SCRIPT.split(start, 1)[1].split("\n}\n", 1)[0]


class FlakyWeb(Web):
    """A page that answers 503 the first times it is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.flaky: dict[str, int] = {}
        self.redirect: dict[str, str] = {}   # a word of a page's path -> the documentation page it now leads to

    def learn(self, path: str, params: httpx.QueryParams) -> httpx.Response:
        for word, target in self.redirect.items():
            if word in path:
                return httpx.Response(302, headers={"location": target})
        for word, left in list(self.flaky.items()):
            if left > 0 and word in path:
                self.flaky[word] -= 1
                return httpx.Response(503)
        return super().learn(path, params)


class PageCheckCase(unittest.TestCase):
    def setUp(self):
        self.web, self.ai = FlakyWeb(), Hooked()
        self.tmp = tempfile.mkdtemp(prefix="dojo-pagecheck-")
        self.app = make_app(self.tmp, self.web, self.ai)
        self.c = TestClient(self.app)
        self.dojo, self.pc = self.app.state.dojo, self.app.state.pagecheck
        self.owner = self.app.state.drift.owner
        done(self.c, post(self.c, "/api/exam-packages", {"code": "XY-110"})["job"])
        self.raw = self.dojo.store.read("packages", PID)
        self.skills = [s for d in self.raw["domains"] for g in d["groups"] for s in g["skills"] if s["sources"]]
        self.assertGreaterEqual(len(self.skills), 4)
        # what a course's pages are made of, for the three verdicts: one page that teaches, the hub that teaches nothing,
        # and a page one model accepts and the other refuses
        self.good, self.hub, self.split = (self.skills[0], self.skills[1], self.skills[2])
        for s, title in ((self.hub, ""), (self.split, "split")):
            s["sources"] = [self.page(s, "widget-documentation" if not title else "split-page")]
        self.dojo.packages.add(self.raw)
        self.ai.grades = lambda role, title, text: (
            "no" if "documentation" in title.lower() else ("teaches" if role == "gate" else "no") if "split" in title.lower() else "teaches")

    @staticmethod
    def page(skill: dict, slug: str) -> dict:
        src = dict(skill["sources"][0])
        src.update(url=f"{LEARN}{DOCS_AT}{slug}" if slug != "widget-documentation" else LEARN + DOCS_AT, title=slug.replace("-", " ").capitalize())
        return src

    def run_check(self) -> dict:
        return self.pc.run(lambda step: None, PID)

    def view(self) -> dict:
        return self.pc.view(PID)


class VerdictTests(PageCheckCase):
    def test_every_skill_gets_the_verdict_both_models_give_its_pages(self):
        self.run_check()
        v = self.view()
        self.assertEqual((v["state"], v["checked"], v["unchecked"]), ("done", len(self.skills), 0))
        self.assertEqual((v["invalid"], v["weak"]), (1, 1), "the hub is refused by both, the split page by one")
        self.assertEqual(v["supported"], len(self.skills) - 2)
        listed = {x["id"]: x["verdict"] for x in v["not_supported"]}
        self.assertEqual(listed, {self.hub["id"]: "invalid", self.split["id"]: "weak"})
        self.assertEqual([x["verdict"] for x in v["not_supported"]], ["invalid", "weak"], "the refused first")
        self.assertTrue(v["finished"])

    def test_nothing_in_the_course_changes(self):
        lessons = self.dojo.store.names("content", "lessons")
        package = json.dumps(self.dojo.store.read("packages", PID), sort_keys=True)
        sources = {s["id"]: [r["url"] for r in self.dojo.packages.get(PID)["skills"][s["id"]]["sources"]] for s in self.skills}
        self.run_check()
        self.assertEqual(self.dojo.store.names("content", "lessons"), lessons)
        self.assertEqual(json.dumps(self.dojo.store.read("packages", PID), sort_keys=True), package)
        self.assertEqual({s["id"]: [r["url"] for r in self.dojo.packages.get(PID)["skills"][s["id"]]["sources"]] for s in self.skills}, sources)

    def test_a_page_that_cannot_be_read_is_not_judged_and_is_tried_on_the_next_run(self):
        slug = self.page(self.good, "flaky-widget")["url"].rsplit("/", 1)[-1]
        self.good["sources"] = [self.page(self.good, slug)]
        self.dojo.packages.add(self.raw)
        self.web.flaky["flaky-widget"] = 1
        out = self.run_check()
        self.assertEqual(out["unread"], 1)
        v = self.view()
        self.assertEqual((v["state"], v["unchecked"]), ("partial", 1))
        self.assertNotIn(self.good["id"], [x["id"] for x in v["not_supported"]], "an unread page says nothing about the skill")
        self.assertEqual(self.run_check()["checked"], 1)
        self.assertEqual((self.view()["state"], self.view()["unchecked"]), ("done", 0))

    def test_a_run_that_stops_keeps_what_it_judged_and_goes_on_where_it_stopped(self):
        asked: list[str] = []
        real = pagecheck_mod.Verifier.grade_pages

        def counting(verifier, ctx, pages, **kw):
            asked.append(ctx["text"])
            return real(verifier, ctx, pages, **kw)

        with mock.patch.object(pagecheck_mod.Verifier, "grade_pages", counting), mock.patch.object(pagecheck_mod, "MAX_CALLS", 3):
            with self.assertRaises(UserError) as stopped:
                self.run_check()
        self.assertIn("start it again to go on", str(stopped.exception))
        judged = self.view()["checked"]
        self.assertTrue(0 < judged < len(self.skills), judged)
        asked.clear()
        with mock.patch.object(pagecheck_mod.Verifier, "grade_pages", counting):
            self.run_check()
        self.assertEqual(len(asked), len(self.skills) - judged, "only the skills without a verdict are asked again")
        self.assertEqual(self.view()["state"], "done")

    def test_a_page_that_now_leads_elsewhere_is_not_credited_to_the_address_the_course_cites(self):
        self.good["sources"] = [self.page(self.good, "old-home")]
        self.dojo.packages.add(self.raw)
        self.web.redirect["old-home"] = f"{DOCS_AT}new-home"   # a page that teaches
        self.run_check()
        entry = next(x for x in self.view()["not_supported"] if x["id"] == self.good["id"])
        self.assertEqual(entry["verdict"], "weak", "the page it moved to teaches, but that is not the page the course cites")
        self.assertTrue(entry["pages"][0]["moved_to"].endswith("/new-home"), entry)

    def test_a_verdict_belongs_to_the_pages_it_was_made_for(self):
        self.run_check()
        self.assertEqual((self.view()["checked"], self.view()["invalid"]), (len(self.skills), 1))
        self.hub["sources"] = self.hub["sources"] + [self.page(self.hub, "widget-backups")]   # a page added to the skill since
        self.dojo.packages.add(self.raw)
        v = self.view()
        self.assertEqual((v["state"], v["unchecked"], v["checked"]), ("partial", 1, len(self.skills) - 1))
        self.run_check()
        v = self.view()
        self.assertEqual((v["state"], v["invalid"], v["unchecked"]), ("done", 0, 0), "the added page teaches the skill")

    def test_a_verdict_belongs_to_the_context_the_models_were_given_too(self):
        self.run_check()
        self.assertEqual(self.view()["unchecked"], 0)
        self.good["sources"][0]["title"] = "The same page under a better title"      # the title the models read
        self.dojo.packages.add(self.raw)
        self.assertEqual(self.view()["unchecked"], 1)
        self.run_check()
        raw = self.dojo.store.read("packages", PID)   # a group renamed in the package
        for d in raw["domains"]:
            for g in d["groups"]:
                if any(s["id"] == self.good["id"] for s in g["skills"]):
                    g["title"] = g["title"] + " (renamed)"
        self.dojo.packages.add(raw)
        self.assertGreaterEqual(self.view()["unchecked"], 1, "the models are told the group of the skill")

    def test_a_course_whose_every_skill_has_a_verdict_is_done_even_if_the_run_stopped_before_it_said_so(self):
        self.run_check()
        doc = self.dojo.store.read("page-check", PID)
        doc["finished"] = None
        self.dojo.store.write("page-check", PID, value=doc)
        v = self.view()
        self.assertEqual((v["state"], v["price"], v["unchecked"]), ("done", None, 0))

    def test_a_verdict_of_an_older_checker_counts_as_not_made(self):
        self.run_check()
        doc = self.dojo.store.read("page-check", PID)
        doc["v"] = doc["v"] - 1
        self.dojo.store.write("page-check", PID, value=doc)
        v = self.view()
        self.assertEqual((v["state"], v["checked"]), ("none", 0))


class OwnerAndPriceTests(PageCheckCase):
    def test_an_added_exam_is_not_checked_here(self):
        r = self.c.post(f"/api/studio/pagecheck/{PID}", json={}, headers=POST)
        self.assertNotEqual(r.status_code, 200)
        self.assertIn("added exam", json.dumps(r.json()))

    def test_the_price_is_that_of_the_pages_still_to_check(self):
        pages = sum(len(s["sources"]) for s in self.skills)
        price = self.view()["price"]
        self.assertEqual(price["pages"], pages)
        self.run_check()
        self.assertIsNone(self.view()["price"], "nothing is left to check")
        est = self.app.state.costs.pagecheck_estimate(100)
        self.assertEqual([(x["role"], x["input_tokens"], x["output_tokens"]) for x in est["lines"]],
                         [("author", 250000, 50000), ("gate", 250000, 45000)])

    def test_a_check_stops_a_second_start_while_it_runs(self):
        gate = {"go": False}
        real = pagecheck_mod.Verifier.grade_pages

        def slow(verifier, ctx, pages, **kw):
            for _ in range(200):
                if gate["go"]:
                    break
                time.sleep(0.02)
            return real(verifier, ctx, pages, **kw)

        raw_pkg = self.dojo.packages
        with mock.patch.object(pagecheck_mod.Verifier, "grade_pages", slow), mock.patch.object(raw_pkg, "is_added", lambda pid: False):
            first = self.pc.start(self.owner, PID)
            again = self.pc.start(self.owner, PID)
            self.assertEqual(first["id"], again["id"], "asking again returns the same job")
            self.assertEqual(self.view()["state"], "running")
            gate["go"] = True
            for _ in range(300):
                if self.view()["state"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(self.view()["state"], "done")


class StudioShowsItTests(PageCheckCase):
    def test_the_course_card_carries_the_check_only_for_a_course_that_was_not_found_by_the_search(self):
        courses = {x["package"]: x for x in self.c.get("/api/studio/courses", params={"fresh": "true"}).json()["courses"]}
        self.assertIsNone(courses[PID]["pagecheck"], "an added course has the search and its audit")
        self.assertEqual(courses["gh-300"]["pagecheck"]["state"], "none")
        self.assertEqual(courses["gh-300"]["pagecheck"]["skills"], 41)
        self.assertEqual(courses["gh-300"]["pagecheck"]["price"]["pages"],
                         sum(len(s["sources"]) for s in self.dojo.packages.get("gh-300")["skills"].values()))

    def test_a_finished_check_is_said_on_the_pages_row_and_in_what_to_know(self):
        # the same code for an original course, read as if XY-110 were one
        with mock.patch.object(self.dojo.packages, "is_added", lambda pid: False):
            self.run_check()
            self.app.state.studio._seen.clear()
            card = next(x for x in self.c.get("/api/studio/courses", params={"fresh": "true"}).json()["courses"] if x["package"] == PID)
        row = next(r for r in card["rows"] if r["key"] == "pages")
        self.assertIn("Two model families read them", row["note"])
        self.assertIn("1 only partly, 1 refused", row["note"])
        texts = [a["text"] for a in card["attention"]]
        self.assertTrue(any("only pages that both models refuse" in t for t in texts))
        self.assertTrue(any("cover it only partly" in t for t in texts))
        self.assertFalse(any("from before Dojo's two-model page check" in t for t in texts), "it has had the check now")

    def test_the_activity_names_the_job(self):
        now = pagecheck_mod.iso()
        self.dojo.store.write("jobs", "job-pagecheck000000000000", value={"id": "job-pagecheck000000000000", "kind": "pagecheck", "learner": self.owner.key,
                                                                            "ref": "gh-300", "state": "done", "step": "Done", "created": now,
                                                                            "heartbeat": now, "result": {}, "error": None})
        self.app.state.studio._seen.clear()
        titles = [e["title"] for e in self.c.get("/api/studio/activity").json()["events"] if e["kind"] == "job"]
        self.assertIn("Checking a course's pages: done", titles)


class PageCheckScreenTests(unittest.TestCase):
    def test_the_button_says_the_price_asks_first_and_changes_nothing(self):
        block = part("function pageCheckRow(x, act) {")
        self.assertIn("moneySheet(ask)", block)
        self.assertIn("It changes nothing", block)
        self.assertIn("/api/studio/pagecheck/${x.package}", block)
        self.assertIn("The price is not available just now", block)
        self.assertIn("runJob(", block)
        self.assertNotIn("window.confirm(", block)

    def test_the_to_do_page_offers_the_check_and_the_course_shows_what_it_found(self):
        self.assertIn("pageChecksCard(c, act)", part("async function studioTodo() {"))
        self.assertIn("pageCheckList(x)", part("function courseCard(x, c) {"))
        self.assertIn("pagesLine(x, pages)", part("function courseCard(x, c) {"))
        self.assertIn("/api/studio/courses?fresh=true", part("function pageCheckRow(x, act) {"))
        self.assertIn("whose pages were not confirmed", part("function pageCheckList(x) {"))


if __name__ == "__main__":
    unittest.main()
