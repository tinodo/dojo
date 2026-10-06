"""Deeper lessons (ADR 0008): more official pages for the skills of the built-in exams, found by Dojo itself
and kept as an overlay of the package; a deeper lesson template that uses every page; and a capped daily
pass, written down before any model is called, that writes the existing lessons again on it.

The documentation sites, their search and the list prices are mocks: nothing here touches the network.
The Record never changes. Reading or hearing a lesson is exposure, never evidence."""
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from typing import Callable
from unittest import mock
from urllib.parse import urlparse

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.deeper as deeper_mod  # noqa: E402
import app.drift as drift_mod  # noqa: E402
import app.learning as learning_mod  # noqa: E402
import tests.test_drift as td  # noqa: E402
from app.ai import AIError, StubAI, _quotes  # noqa: E402
from app.core import Store, UserError, iso, new_id, parse_iso, utcnow  # noqa: E402
from app.costs import DEEP_CHARS, DEEP_ROUNDS, SPEAKING_CPS  # noqa: E402
from app.learning import GROUNDING_CHARS, LESSON_TEMPLATE  # noqa: E402
from app.main import create_app  # noqa: E402
from app.packages import ADDED_PUBLISHERS, MAX_SOURCES, OVERLAY, Packages, blueprint, merge_sources  # noqa: E402
from app.pagefinder import KNOWN_RULE  # noqa: E402
from app.readiness import rule_two  # noqa: E402
from app.sources import Sources  # noqa: E402
from tests.test_api import GITHUB_MD, LEARN_HTML, POST, settings  # noqa: E402
from tests.test_newexam import Scripted, html  # noqa: E402
from tests.test_newexam import Web as Sites  # noqa: E402
from tests.test_packages import BUILT_IN, small_package  # noqa: E402

PID, S, OTHER = td.PID, td.S, td.OTHER   # S cites the inline-suggestions page, OTHER the chat page
THIRD, FOURTH = "d1.g1.s3", "d1.g2.s1"   # both cite the inline-suggestions page as well
URL, PAGE, DP = td.URL, td.PAGE, "dp-800"
GH = "https://docs.github.com"
ADDED = GH + td.ELSEWHERE                 # a page no skill cites
ADDED_2 = GH + "/en/copilot/get-started/what-is-github-copilot"
ADDED_3 = GH + "/en/copilot/concepts/prompting/prompt-engineering"
QUOTE = "Copilot code completion suggests whole lines of code while you type in a supported editor."
ELSE_MD = ("# Code completions\n\n" + QUOTE + "\n\n"
           "Suggestions are based on the code around the cursor and the files that are open in the editor.\n\n"
           "You can accept a suggestion with the Tab key, or keep typing to ignore it completely.\n")
CHANGED = "Copilot now suggests code in more places than it did before, including the terminal and the chat."


def ref(url: str, quote: str = QUOTE, **change) -> dict:
    """A page as a search keeps it for a skill."""
    out = {"url": url, "title": "A page found", "publisher": ADDED_PUBLISHERS.get(urlparse(url).hostname, "Someone"),
           "license": "CC-BY-4.0", "use": "ground", "verified_at": "2026-09-30T10:00:00Z", "reason": "It teaches the skill.",
           "verified": {"quote": quote, "by": "grok-4-1-fast-reasoning", "on": "2026-09-30"}}
    out.update(change)
    return out


def skill_texts(packages: Packages, pid: str = PID) -> dict[str, str]:
    return {s["id"]: s["text"] for d in packages.builtin_raw(pid)["domains"] for g in d["groups"] for s in g["skills"]}


def found(packages: Packages, pages: dict[str, list], pid: str = PID, when: str = "2026-09-30T10:00:00Z") -> dict:
    """An overlay as the search writes it: for each skill, its words then, the pages found and when."""
    words = skill_texts(packages, pid)
    return {"package": pid, "version": 1, "skills": {
        sid: {"skill_text": words[sid], "sources": list(refs), "tried": when, "searched": when, "found": len(refs), "error": None}
        for sid, refs in pages.items()}}


def urls(pkg: dict, sid: str) -> list[str]:
    return [r["url"] for r in pkg["skills"][sid]["sources"]]


def practice_exam(pkg: dict, days: int) -> dict:
    """A practice exam taken under exam conditions some days ago: ten new questions in every domain, all right."""
    items = []
    for d in pkg["domains"]:
        sids = [sid for g in d["groups"] for sid in g["skills"]]
        items += [{"skill": sids[k % len(sids)], "answer": 0, "new": True} for k in range(10)]
    at = iso(utcnow() - timedelta(days=days))
    return {"id": f"attempt-{days}", "created": at, "closed_at": at, "conditions": "exam", "items": items,
            "answers": {str(i): {"choice": 0} for i in range(len(items))}, "new_questions": len(items),
            "blueprint": pkg["meta"]["blueprint"]}


def from_every_page(out: dict, user: str) -> dict:
    """A section with one point from each page the author was given, quoting the page's first sentence."""
    first: dict[int, str] = {}
    for n, sentence in _quotes(user):
        first.setdefault(int(n), sentence)
    out["sections"].append({"heading": "From every page", "points": [
        {"text": f"What page {n} adds.", "quote": q, "source": n} for n, q in sorted(first.items())]})
    return out


class Restart(BaseException):
    """Dojo stops in the middle of something, as a restart does: nothing after it runs."""


class Crash(Exception):
    """Something nobody expected, in the middle of writing a lesson."""


class NothingTeaches(Scripted):
    """The checker finds that no candidate teaches the skill."""

    def _verify_sources(self, user: str) -> dict:
        out = super()._verify_sources(user)
        for v in out["sources"]:
            v["teaches"] = False
        return out


class CheckerDown(Scripted):
    def _verify_sources(self, user: str) -> dict:
        raise AIError("The checker did not answer.")


class EveryPage(Scripted):
    def _lesson(self, user: str) -> dict:
        return from_every_page(super()._lesson(user), user)


class Author(StubAI):
    """The stand-in author, with switches: it fails or crashes, writes what the checker rejects or fewer
    points that hold, quotes every page, says its first line differently, or runs `during` as it writes."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = self.crash = self.reject = self.fewer = self.every_page = False
        self.say: str | None = None
        self.during: Callable[[], None] | None = None

    def _lesson(self, user: str) -> dict:
        if self.during:
            self.during()
        if self.crash:
            raise Crash("the author fell over")
        if self.fail:
            raise AIError("The author did not answer.")
        out = super()._lesson(user)
        if self.fewer:
            out["sections"][1]["points"].pop(0)   # the third point that holds: two of three are left
        if self.every_page:
            out = from_every_page(out, user)
        if self.say:
            out["slides"][0]["narration"][0]["text"] = self.say
        return out

    def _gate_elements(self, user: str) -> dict:
        out = super()._gate_elements(user)
        if self.reject:
            for v in out["verdicts"]:
                v.update(holds=False, reason="Not what the sources say.")
        return out


# ---------------------------------------------------------------------------------------------- the overlay


class OverlayTests(unittest.TestCase):
    """The pages found are kept apart from the repository's package file and merged into the built-in
    package when Dojo loads it and whenever they change. A bad overlay is logged and ignored."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="dojo-overlay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.store = Store(Path(tmp))

    def load(self) -> Packages:
        return Packages(BUILT_IN, self.store)

    def keep(self, doc) -> None:
        self.store.write("content", OVERLAY, PID, value=doc)

    def test_the_pages_found_come_after_the_package_s_own_page(self):
        plain = self.load()
        before = plain.get(PID)
        self.keep(found(plain, {S: [ref(ADDED), ref(ADDED_2)]}))
        packages = self.load()
        pkg = packages.get(PID)
        self.assertEqual(urls(pkg, S), [URL, ADDED, ADDED_2])
        self.assertEqual(pkg["skills"][S]["sources"][0], before["skills"][S]["sources"][0], "the package's own page stays first")
        added = pkg["skills"][S]["sources"][1]
        self.assertEqual((added["publisher"], added["license"], added["use"], added["verified"]["quote"], added["verified_at"]),
                         ("GitHub Docs", "CC-BY-4.0", "ground", QUOTE, "2026-09-30T10:00:00Z"))
        self.assertEqual(pkg["skills"][OTHER]["sources"], before["skills"][OTHER]["sources"])
        own = next(s for d in packages.builtin_raw(PID)["domains"] for g in d["groups"] for s in g["skills"] if s["id"] == S)
        self.assertEqual([r["url"] for r in own["sources"]], [URL], "the repository's package is not changed")
        # What the exam measures is not changed by the pages that teach it.
        self.assertEqual(pkg["meta"]["blueprint"], before["meta"]["blueprint"])
        self.assertEqual(pkg["meta"]["blueprint"], blueprint(packages.builtin_raw(PID)))
        self.assertEqual({k: s["share"] for k, s in pkg["skills"].items()}, {k: s["share"] for k, s in before["skills"].items()})
        self.assertEqual(packages.get(DP), plain.get(DP))

    def test_an_overlay_that_cannot_be_used_is_logged_and_ignored(self):
        plain = self.load().get(PID)
        good = found(self.load(), {S: [ref(ADDED)]})
        cases = {"not JSON": b"{ not json", "another exam's": json.dumps({**good, "package": DP}).encode(),
                 "skills not an object": json.dumps({"package": PID, "skills": []}).encode(),
                 "not an object": json.dumps(["pages"]).encode()}
        for what, data in cases.items():
            with self.subTest(what):
                self.store.write_bytes("content", OVERLAY, PID + ".json", data=data)
                with self.assertLogs("dojo.packages", "WARNING") as logs:
                    packages = self.load()
                self.assertEqual(packages.get(PID), plain, "Dojo starts with the package as the repository has it")
                self.assertIn("ignoring the pages found for the exam package gh-300", logs.output[0])

    def test_pages_that_fail_the_checks_are_left_out_and_said(self):
        packages = self.load()
        bad = [ref(ADDED, use="read"), ref("https://evil.example/copilot/code-completions"),
               ref("https://learn.microsoft.com/en-us/training/modules/introduction-to-github-copilot/"),
               ref(ADDED, license="All rights reserved"), ref(ADDED, publisher="Someone else"),
               ref(ADDED, verified={"quote": "  "}), ADDED]
        doc = found(packages, {S: bad + [ref(URL), ref(ADDED), ref(ADDED_2), ref(ADDED_3)], OTHER: [ref(ADDED)], THIRD: []})
        doc["skills"][OTHER]["skill_text"] = "Describe a skill the study guide no longer has"
        doc["skills"][THIRD]["sources"] = {"url": ADDED}
        doc["skills"]["d9.g9.s9"] = {"skill_text": "Gone", "sources": [ref(ADDED)]}
        merged, skipped = merge_sources(packages.builtin_raw(PID), doc)
        self.assertEqual(len(skipped), 12, skipped)
        said = " | ".join(skipped)
        for part in ("must ground the skill", "is not an official documentation page", "the CC-BY-4.0 license",
                     "no sentence the checker copied", f"{URL} is cited already", f"at most {MAX_SOURCES} pages",
                     "the skill's words changed", "its pages are not a list", "d9.g9.s9: there is no such skill"):
            self.assertIn(part, said)
        self.keep(doc)
        with self.assertLogs("dojo.packages", "WARNING") as logs:
            pkg = self.load().get(PID)
        self.assertIn("left out 12 of the pages found for the exam package gh-300", logs.output[0])
        self.assertEqual(urls(pkg, S), [URL, ADDED, ADDED_2], "what passes is kept, the package's own page first")
        mine = next(s for d in merged["domains"] for g in d["groups"] for s in g["skills"] if s["id"] == S)
        self.assertEqual([r["url"] for r in mine["sources"]], urls(pkg, S))
        self.assertEqual(len(pkg["skills"][OTHER]["sources"]), 1)
        self.assertEqual(len(pkg["skills"][THIRD]["sources"]), 1)

    def test_set_overlay_replaces_the_packages_and_keeps_the_file(self):
        packages = self.load()
        before = packages.by_id
        indexed = packages.set_overlay(PID, found(packages, {S: [ref(ADDED)]}))
        self.assertIsNot(packages.by_id, before, "replaced, never changed in place")
        self.assertEqual(len(before[PID]["skills"][S]["sources"]), 1, "code holding the old one still sees it whole")
        self.assertIs(packages.get(PID), indexed)
        self.assertIs(packages.get(DP), before[DP])
        self.assertEqual(urls(indexed, S), [URL, ADDED])
        self.assertEqual(self.load().get(PID)["skills"][S]["sources"], indexed["skills"][S]["sources"], "kept for the next start")
        self.assertEqual(packages.overlay(PID)["skills"][S]["sources"][0]["url"], ADDED)
        # One for another exam is refused before anything is written.
        with self.assertRaises(ValueError):
            packages.set_overlay(PID, {"package": DP, "skills": {}})
        self.assertEqual(packages.overlay(PID)["skills"][S]["sources"][0]["url"], ADDED)
        self.assertIs(packages.get(PID), indexed)
        # An added exam got its pages when it was added; and without a store there is nowhere to keep them.
        added = small_package()
        packages.add(added)
        with self.assertRaises(UserError):
            packages.set_overlay(added["id"], {"package": added["id"], "skills": {}})
        with self.assertRaises(UserError):
            Packages(BUILT_IN).set_overlay(PID, found(packages, {}))
        self.assertIsNone(Packages(BUILT_IN).overlay(PID))

    def test_readiness_credit_survives_the_pages_found(self):
        packages = self.load()
        pkg = packages.get(PID)
        attempts = [practice_exam(pkg, days) for days in (1, 2)]
        now = utcnow()
        self.assertTrue(rule_two(pkg, attempts, now)["met"])
        packages.set_overlay(PID, found(packages, {sid: [ref(ADDED)] for sid in pkg["skills"]}))
        after = packages.get(PID)
        self.assertNotEqual(after["skills"][S]["sources"], pkg["skills"][S]["sources"])
        self.assertEqual(after["meta"]["blueprint"], pkg["meta"]["blueprint"])
        self.assertTrue(rule_two(after, attempts, now)["met"], "the practice exams still count")
        self.assertTrue(rule_two(self.load().get(PID), attempts, now)["met"], "and after a restart")


# ---------------------------------------------------------------------------------------------- the search


class SiteWeb(Sites):
    """Add an exam's two documentation sites, which also serve the pages the built-in exams cite."""

    def learn(self, path: str, params: httpx.QueryParams) -> httpx.Response:
        r = super().learn(path, params)
        return html(LEARN_HTML) if r.status_code == 404 and path != "/api/search" else r

    def github(self, path: str, params: httpx.QueryParams) -> httpx.Response:
        r = super().github(path, params)
        if r.status_code == 404 and path == "/api/article/body":
            return httpx.Response(200, text=GITHUB_MD, headers={"content-type": "text/markdown"})
        return r


class SearchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-deeper-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.web = SiteWeb()
        self.ai = self.make_ai()
        self.clock = utcnow().replace(microsecond=0)
        self.boot()

    def make_ai(self) -> Scripted:
        return Scripted()

    def boot(self) -> None:
        """Start Dojo on the same data folder, as a restart does."""
        self.app = create_app(settings(self.tmp), ai=self.ai, http=httpx.Client(transport=httpx.MockTransport(self.web)))
        self.c = TestClient(self.app)
        self.dojo, self.deeper, self.preparer = self.app.state.dojo, self.app.state.deeper, self.app.state.preparer
        self.owner = self.deeper.owner
        self.deeper.now = lambda: self.clock

    def later(self, **delta) -> None:
        self.clock += timedelta(**delta)

    def group(self, sid: str = S) -> tuple[dict, dict]:
        pkg = self.dojo.packages.get(PID)
        return next((d, g) for d in pkg["domains"] for g in d["groups"] if sid in g["skills"])

    def search(self, sid: str = S) -> dict:
        d, g = self.group(sid)
        return self.deeper.search_group(PID, d, g, list(g["skills"]))

    def sources(self, sid: str = S) -> list[str]:
        return urls(self.dojo.packages.get(PID), sid)

    def entry(self, sid: str = S) -> dict:
        return self.deeper.overlay(PID)["skills"][sid]

    def due(self, pid: str = PID) -> set[str]:
        return {sid for p, _, _, sids in self.deeper.search_targets() if p == pid for sid in sids}

    def picks(self) -> int:
        return len(self.ai.of("pick_sources"))


class SearchTests(SearchCase):
    def test_a_search_adds_up_to_two_checked_pages_after_the_package_s_own(self):
        own = {sid: urls(self.dojo.packages.get(PID), sid) for sid in (S, OTHER, THIRD)}
        self.assertEqual(own[S], [URL])
        self.search()
        stamp = iso(self.clock)
        for sid in (S, OTHER, THIRD):
            refs = self.dojo.packages.get(PID)["skills"][sid]["sources"]
            self.assertEqual(len(refs), MAX_SOURCES, sid)
            self.assertEqual(refs[0]["url"], own[sid][0], "the package's own page stays first")
            for r in refs[1:]:
                host = urlparse(r["url"]).hostname
                self.assertIn(host, ("docs.github.com", "learn.microsoft.com"))
                self.assertNotIn("/training/", r["url"])
                self.assertEqual((r["publisher"], r["license"], r["use"], r["verified_at"]),
                                 (ADDED_PUBLISHERS[host], "CC-BY-4.0", "ground", stamp))
                self.assertTrue(r["verified"]["quote"])
                self.assertTrue(r["reason"])
                self.assertTrue(self.dojo.sources.head(r["url"])["verified"], "read under the documentation rule")
                self.assertIn(r["url"], self.dojo.drift.cited(), "the drift check reads it like any cited page")
            self.assertEqual(len({r["url"] for r in refs}), MAX_SOURCES, "no page twice")
            e = self.entry(sid)
            self.assertEqual((e["searched"], e["tried"], e["error"], e["found"]), (stamp, stamp, None, 2))
        self.assertFalse([u for u in self.web.seen if "/training/" in u or "evil.example" in u], "never read")
        prompt = self.ai.of("pick_sources")[0]
        self.assertIn("already cited: ", prompt)
        self.assertIn(URL, prompt)
        self.assertIn(KNOWN_RULE, prompt)
        self.boot()
        self.assertEqual(len(self.sources()), MAX_SOURCES, "kept for the next start")
        self.later(days=31)
        self.assertFalse(self.due() & {S, OTHER, THIRD}, "a skill with three pages is not searched again")
        self.assertEqual(self.deeper._doc()["spent"][PID]["searched"], 3)

    def test_a_search_is_written_down_before_any_call(self):
        seen = {}

        def cut(*args, **kwargs):
            seen["overlay"] = json.loads(json.dumps(self.deeper.overlay(PID)))
            seen["state"] = self.deeper._doc()
            raise Restart()
        d, g = self.group()
        with mock.patch.object(self.deeper.finder, "match_group", side_effect=cut):
            with self.assertRaises(Restart):
                self.deeper.search_group(PID, d, g, list(g["skills"]))
        stamp = iso(self.clock)
        for sid in g["skills"]:
            e = seen["overlay"]["skills"][sid]
            self.assertEqual((e["tried"], e.get("searched")), (stamp, None))
        self.assertEqual((seen["state"]["spent"][PID]["searched"], seen["state"]["last_search"]), (len(g["skills"]), stamp))
        self.assertEqual(self.ai.calls, [], "written down before any model is called")
        self.assertFalse(self.web.queries, "and before any search")
        self.boot()
        self.assertFalse(self.due() & set(g["skills"]), "a restart gets no free retry")
        self.later(hours=deeper_mod.SEARCH_RETRY_H - 1)
        self.assertFalse(self.due() & set(g["skills"]))
        self.later(hours=1)
        self.assertTrue(set(g["skills"]) <= self.due())

    def test_a_search_that_finds_nothing_new_is_tried_again_after_thirty_days(self):
        self.ai = NothingTeaches()
        self.boot()
        self.search()
        self.assertEqual(self.sources(), [URL], "the skill keeps its one page")
        e = self.entry()
        self.assertEqual((e["sources"], e["found"], e["error"], e["searched"]), ([], 0, None, iso(self.clock)))
        self.assertEqual(self.deeper._doc()["failures"], [], "finding nothing is no failure")
        self.assertNotIn(S, self.due())
        self.later(days=deeper_mod.SEARCH_AGAIN_DAYS, hours=-1)
        self.assertNotIn(S, self.due())
        self.later(hours=1)
        self.assertIn(S, self.due())

    def test_a_failed_search_is_shown_and_tried_again_a_day_later(self):
        self.ai = CheckerDown()
        self.boot()
        self.search()
        d, g = self.group()
        e = self.entry()
        self.assertEqual((e["error"], e.get("searched"), e["sources"]), ("The checker did not answer.", None, []))
        failures = self.c.get("/api/studio/deeper").json()["failures"]
        self.assertEqual({(f["skill"], f["what"]) for f in failures}, {(sid, "search") for sid in g["skills"]})
        self.assertNotIn(S, self.due())
        self.later(hours=deeper_mod.SEARCH_RETRY_H - 1)
        self.assertNotIn(S, self.due())
        self.later(hours=1)
        self.assertIn(S, self.due())

    def test_the_search_waits_for_the_learner_and_keeps_its_pace(self):
        self.assertEqual(self.deeper.busy, self.preparer._learner_busy, "the Preparer's rule")
        job = new_id("job")
        running = {"id": job, "kind": "lesson", "learner": self.owner.key, "state": "running", "created": iso()}
        self.dojo.store.write("jobs", job, value=running)
        self.assertTrue(self.deeper.busy())
        self.assertEqual(self.deeper.search_step(), deeper_mod.BUSY_WAIT_S)
        self.assertEqual(self.ai.calls, [])
        self.dojo.store.write("jobs", job, value={**running, "state": "done"})
        self.assertEqual(self.deeper.search_step(), deeper_mod.SEARCH_GAP_S)
        self.assertEqual(self.picks(), 1)
        self.later(minutes=4)
        self.assertEqual(self.deeper.search_step(), deeper_mod.SEARCH_GAP_S - 240)
        self.assertEqual(self.picks(), 1)
        self.boot()
        self.assertEqual(self.deeper.search_step(), deeper_mod.SEARCH_GAP_S - 240, "a restart keeps the pace")
        self.later(minutes=6)
        self.assertEqual(self.deeper.search_step(), deeper_mod.SEARCH_GAP_S)
        self.assertEqual(self.picks(), 2)

    def test_the_active_exam_is_searched_first_then_the_heaviest_groups(self):
        targets = self.deeper.search_targets()
        pids = [t[0] for t in targets]
        self.assertEqual(pids[0], PID)
        self.assertEqual(pids, sorted(pids, key=lambda p: p != PID))
        self.assertEqual(set(pids), {PID, DP}, "the built-in exams")
        pkg = self.dojo.packages.get(PID)
        weights = [max(pkg["skills"][sid]["share"] for sid in t[3]) for t in targets if t[0] == PID]
        self.assertEqual(weights, sorted(weights, reverse=True))
        self.dojo.save_profile(self.owner, {"active_package": DP})
        self.assertEqual(self.deeper.search_targets()[0][0], DP)
        added = small_package()
        self.dojo.packages.add(added)
        self.assertNotIn(added["id"], {t[0] for t in self.deeper.search_targets()}, "an added exam got its pages when it was added")


class ReplacementTests(SearchCase):
    """A page a search added that the drift check then finds gone (ADR 0006) gets one search for a page to
    take its place, without the 30-day wait (ADR 0008, review round 1)."""

    def three_pages(self) -> list[str]:
        self.search()
        pages = self.sources()
        self.assertEqual(len(pages), MAX_SOURCES)
        return pages

    def search_s(self) -> dict:
        """Search for S alone, and say what the search was told."""
        seen = {}
        match = self.deeper.finder.match_group

        def spy(*args, **kwargs):
            seen.update(known=[k["url"] for k in kwargs["known"][S]], limit=kwargs["limits"][S])
            return match(*args, **kwargs)
        d, g = self.group()
        with mock.patch.object(self.deeper.finder, "match_group", side_effect=spy):
            self.deeper.search_group(PID, d, g, [S])
        return seen

    def test_a_page_found_that_goes_is_replaced_by_one_search_without_the_wait(self):
        own, first, second = self.three_pages()
        cached = self.dojo.drift._doc()
        cached["pages"][second] = {"classified": "earlier"}
        cached["changes"].append({"id": "c-gone", "url": second, "kind": "gone", "lessons": []})
        self.dojo.drift._save(cached)
        self.dojo.sources.mark_gone(second, iso(self.clock))
        self.assertNotIn(S, self.due(), "not within a day of its last search")
        self.later(hours=deeper_mod.SEARCH_RETRY_H)
        self.assertIn(S, self.due(), "a day later, not thirty days later")
        seen = self.search_s()
        self.assertEqual(seen["limit"], 1, "the package's page and the page still there keep their places")
        self.assertIn(second, seen["known"], "the page that went is not found again")
        pages = self.sources()
        self.assertEqual(pages[:2], [own, first])
        self.assertEqual(len(pages), MAX_SOURCES)
        self.assertNotIn(second, pages, "the page that went leaves the package")
        self.assertNotIn(second, [r["url"] for r in self.entry()["sources"]])
        self.assertEqual(self.entry()["gone_searched"], [])
        doc = self.dojo.drift._doc()
        self.assertNotIn(second, self.dojo.drift.cited())
        self.assertNotIn(second, doc["pages"], "what the drift check wrote down for it goes, as for a removed page")
        self.assertFalse([c for c in doc["changes"] if c["url"] == second])
        self.assertEqual(doc["replaced"], {})
        self.assertIsNotNone(self.dojo.sources.stored(second), "its copy stays, for the answers to items written from it")
        self.later(days=deeper_mod.SEARCH_AGAIN_DAYS + 1)
        self.assertNotIn(S, self.due(), "three pages again")

    def test_a_gone_page_nothing_replaces_stays_listed_and_waits_thirty_days(self):
        own, first, second = self.three_pages()
        self.dojo.sources.mark_gone(second, iso(self.clock))
        self.later(hours=deeper_mod.SEARCH_RETRY_H)
        self.ai = NothingTeaches()
        self.boot()
        self.assertIn(S, self.due())
        self.search_s()
        self.assertEqual(self.sources(), [own, first, second], "it stays listed: the drift check still reads it, it may come back")
        self.assertEqual(self.entry()["gone_searched"], [second])
        self.assertEqual(self.entry()["searched"], iso(self.clock))
        self.later(hours=deeper_mod.SEARCH_RETRY_H)
        self.assertNotIn(S, self.due(), "one search for a replacement, not one a day")
        row = next(r for r in self.c.get("/api/studio/deeper").json()["exams"] if r["package"] == PID)
        self.assertEqual(row["replacing"], 0)
        self.later(days=deeper_mod.SEARCH_AGAIN_DAYS, hours=-deeper_mod.SEARCH_RETRY_H - 1)
        self.assertNotIn(S, self.due())
        self.later(hours=1)
        self.assertIn(S, self.due(), "then every thirty days, as for a skill where nothing new was found")

    def test_two_pages_that_went_give_two_places_the_longest_gone_first(self):
        own, first, second = self.three_pages()
        self.dojo.sources.mark_gone(first, iso(self.clock - timedelta(hours=1)))
        self.dojo.sources.mark_gone(second, iso(self.clock))
        self.later(hours=deeper_mod.SEARCH_RETRY_H)
        row = next(r for r in self.c.get("/api/studio/deeper").json()["exams"] if r["package"] == PID)
        self.assertGreaterEqual(row["replacing"], 1)
        diag = next(r for r in self.c.get("/api/diag").json()["deeper_lessons"]["exams"] if r["package"] == PID)
        self.assertEqual(diag["replacing"], row["replacing"])
        seen = self.search_s()
        self.assertEqual(seen["limit"], 2, "both places are free")
        self.assertEqual(self.entry()["found"], 1, "the search world here has one more page for the skill")
        new = next(u for u in self.sources() if u not in (own, first, second))
        self.assertEqual(self.sources(), [own, new, second], "the page found takes the place of the page gone longest")
        self.assertEqual(self.entry()["gone_searched"], [second])


class TemplateTests(SearchCase):
    def make_ai(self) -> Scripted:
        return EveryPage()

    def test_a_new_lesson_uses_every_page_on_the_deeper_template(self):
        self.search()
        pages = self.sources()
        self.assertEqual(len(pages), 3)
        sizes = []
        real = learning_mod.excerpt

        def spy(text, query, size, *args, **kwargs):
            sizes.append(size)
            return real(text, query, size, *args, **kwargs)
        with mock.patch.object(learning_mod, "excerpt", spy):
            lid = self.dojo.make_lesson(lambda step: None, self.owner, PID, S)["lesson"]
        self.assertEqual(sizes, [GROUNDING_CHARS[3]] * 3, "three pages of 10,000 characters each")
        prompt = self.ai.of("lesson")[-1]
        for part in ("There are 3 SOURCES", "cite every SOURCE that covers the skill at least once",
                     "4-6 sections of 3-5 points", "2-4 misconceptions", "4-5 self-check questions", "6-9 slides",
                     "each step an instruction the learner could follow"):
            self.assertIn(part, prompt)
        doc = self.dojo.lesson(lid)
        self.assertEqual((doc["template"], doc["offered"], doc["cited"]), (LESSON_TEMPLATE, pages, pages))
        self.assertEqual([r["url"] for r in doc["sources"]], pages)
        self.assertEqual(self.dojo.lessons_for(PID, S)[0]["template"], LESSON_TEMPLATE)
        skill = self.c.get(f"/api/skills/{PID}/{S}").json()["skill"]
        self.assertEqual([r["url"] for r in skill["sources"]], pages, "the skill page lists every page it cites")
        self.assertEqual([bool(r.get("verified_at")) for r in skill["sources"]], [False, True, True])


class CostTests(unittest.TestCase):
    def test_the_deeper_estimate_at_list_price(self):
        tmp = tempfile.mkdtemp(prefix="dojo-deeper-cost-")
        self.addCleanup(shutil.rmtree, tmp, True)
        app = create_app(settings(tmp), http=httpx.Client(transport=httpx.MockTransport(SiteWeb())))
        est = app.state.costs.deeper_estimate(114, 114)
        lines = {line["key"]: line for line in est["lines"]}
        # 114 skills searched (picking 900 in and 800 out, checking 3500 and 1000) and 114 lessons written
        # again 1.5 times on average (10,000 and 10,000; their check 31,000 and 9,000), with 3000 characters
        # of narration each: gpt-5.5 at $5 and $30 per million tokens, Grok 4.1 Fast at $0.20 and $0.50.
        expected = {"pick": (102_600, 91_200, 3.25), "verify": (399_000, 114_000, 0.14),
                    "write": (1_710_000, 1_710_000, 59.85), "check": (5_301_000, 1_539_000, 1.83)}
        for key, (tokens_in, tokens_out, cost) in expected.items():
            self.assertEqual((lines[key]["input_tokens"], lines[key]["output_tokens"], lines[key]["cost"]),
                             (tokens_in, tokens_out, cost), key)
        self.assertEqual((lines["narration"]["quantity"], lines["narration"]["cost"]), (342_000, 5.13))
        # The voice check transcribes those 342,000 characters once, at 14 a second: 407 minutes at $0.36 an hour.
        self.assertEqual((lines["narration_check"]["quantity"], lines["narration_check"]["unit"], lines["narration_check"]["cost"]),
                         (407.1, "minutes", 2.44))
        self.assertEqual((est["total"], est["complete"], est["searches"], est["lessons"]), (72.64, True, 114, 114))
        said = " ".join(est["assumptions"])
        for part in ("114 skills searched", "Tokens for each skill and call", "writing the lessons 10,000 input and 10,000 output",
                     "never filmed again on its own", "no such lesson is waiting now",
                     "transcribes each new lesson's narration once, about 3.6 minutes a lesson"):
            self.assertIn(part, said)
        self.assertNotIn("deep_again", lines)

    def test_a_lesson_that_plays_a_deeper_listen_needs_it_written_again(self):
        tmp = tempfile.mkdtemp(prefix="dojo-deeper-cost-")
        self.addCleanup(shutil.rmtree, tmp, True)
        costs = create_app(settings(tmp), http=httpx.Client(transport=httpx.MockTransport(SiteWeb()))).state.costs
        est, without = costs.deeper_estimate(0, 2, deep_again=2), costs.deeper_estimate(0, 2)
        line = next(line for line in est["lines"] if line["key"] == "deep_again")
        # Two of ADR 0007's lessons: 1.5 rounds of writing and checking each, and 5,800 characters spoken.
        each = costs._deep_lines(costs.prices(), DEEP_ROUNDS * 2, DEEP_CHARS * 2, DEEP_CHARS / SPEAKING_CPS * 2)
        self.assertEqual((line["quantity"], line["unit"], line["cost"]), (2, "lessons", round(sum(x["cost"] for x in each), 2)))
        self.assertTrue(0.4 < line["cost"] / 2 < 0.6, line["cost"])
        self.assertTrue(est["complete"])
        self.assertAlmostEqual(est["total"], without["total"] + line["cost"], places=2)
        self.assertEqual(est["deep_again"], 2)
        self.assertIn("2 such lessons", " ".join(est["assumptions"]))
        one = next(line for line in costs.deeper_estimate(0, 1, deep_again=1)["lines"] if line["key"] == "deep_again")
        self.assertEqual((one["quantity"], one["unit"]), (1, "lesson"))


class StudioTests(SearchCase):
    def test_the_card_and_the_diagnostics_say_where_it_stands(self):
        lid = self.dojo.make_lesson(lambda step: None, self.owner, PID, S)["lesson"]
        doc = self.dojo.lesson(lid)
        doc.pop("template")
        self.dojo.store.write("content", "lessons", lid, value=doc)
        self.dojo._lesson_meta.pop(lid, None)
        self.search()
        view = self.c.get("/api/studio/deeper").json()
        row = next(r for r in view["exams"] if r["package"] == PID)
        n = len(self.dojo.packages.get(PID)["skills"])
        self.assertEqual((row["skills"], row["deep"], row["searched"], row["to_search"]), (n, 3, 3, n - 3))
        self.assertEqual((row["lessons"], row["deeper"], row["behind"], row["waiting"], row["given_up"]), (1, 0, 1, 1, 0))
        self.assertEqual(row["spent"], {"searched": 3, "attempts": 0, "written": 0})
        self.assertTrue(row["builtin"])
        self.assertTrue(row["to_finish"]["complete"])
        dp = next(r for r in view["exams"] if r["package"] == DP)
        self.assertEqual((view["estimate"]["searches"], view["estimate"]["lessons"]), (n - 3 + dp["to_search"], 1))
        self.assertTrue(view["estimate"]["complete"])
        self.assertTrue(view["estimate"]["fetched"], "the age of the prices is shown")
        self.assertIn("Tokens for each skill and call", " ".join(view["estimate"]["assumptions"]))
        self.assertEqual((view["spent"]["searches"], view["spent"]["lessons"]), (3, 0))
        self.assertEqual(view["limits"], {"per_day": 20, "tries": 3, "wait_hours": 24, "search_gap_minutes": 10,
                                          "search_again_days": 30, "pages_per_skill": 3})
        self.assertEqual((view["today"]["used"], view["today"]["left"]), (0, 20))
        self.assertEqual(view["template"], LESSON_TEMPLATE)
        self.assertIn("never fills a pip", view["record"])
        diag = self.c.get("/api/diag").json()["deeper_lessons"]
        self.assertEqual(set(diag), {"started", "template", "per_day", "used_today", "left_today", "last_search", "exams",
                                     "estimate_usd", "spent_usd", "failures"})
        self.assertEqual((diag["started"], diag["estimate_usd"], diag["last_search"]), (False, view["estimate"]["total"], iso(self.clock)))
        self.assertEqual(next(r for r in diag["exams"] if r["package"] == PID)["behind"], 1)


# ---------------------------------------------------------------------------------------------- upgrades


class UpgradeCase(td.DriftCase):
    """The drift check's world (a mocked web, a clock a day ahead, at noon), with an author whose
    behaviour each test can change, and the upgrade pass."""

    def setUp(self):
        self.ai = Author()
        super().setUp()

    def boot(self) -> None:
        self.app = create_app(settings(self.tmp), ai=self.ai, http=httpx.Client(transport=httpx.MockTransport(self.web)))
        self.c = TestClient(self.app)
        self.dojo, self.drift = self.app.state.dojo, self.app.state.drift
        self.owner = self.drift.owner
        self.drift.now = lambda: self.clock
        self.drift.labs = self.labs
        self.preparer, self.deeper = self.app.state.preparer, self.app.state.deeper
        self.deeper.now = lambda: self.clock

    def old_lesson(self, sid: str, pid: str = PID) -> str:
        """A lesson written a month ago on the earlier template, from the pages the skill cited then."""
        lid = self.dojo.make_lesson(lambda step: None, self.owner, pid, sid)["lesson"]
        doc = self.dojo.lesson(lid)
        for key in ("template", "cited", "offered"):
            doc.pop(key, None)
        doc["created"] = iso(utcnow() - timedelta(days=30))
        self.dojo.store.write("content", "lessons", lid, value=doc)
        self.dojo._lesson_meta.pop(lid, None)
        return lid

    def searched(self, *sids: str, pages: list | None = None, pid: str = PID) -> None:
        """A search for these skills finished now and found these pages (none by default)."""
        overlay = self.deeper.overlay(pid)
        words = skill_texts(self.dojo.packages, pid)
        stamp = iso(self.clock)
        for sid in sids:
            overlay["skills"][sid] = {"skill_text": words[sid], "sources": [dict(r) for r in pages or []], "tried": stamp,
                                      "searched": stamp, "found": len(pages or []), "error": None}
        self.dojo.packages.set_overlay(pid, overlay)

    def fill(self, keep: tuple = ()) -> None:
        """Every other skill of the built-in exams gets a lesson on the current template, two months old,
        written from every page it cites now and with nothing to narrate: nothing else is due. `keep` names
        skills of the GH-300 package that stay without a lesson."""
        when = iso(utcnow() - timedelta(days=60))
        for pid in (PID, DP):
            ready = self.dojo.lesson_skills(pid)
            for sid, s in self.dojo.packages.get(pid)["skills"].items():
                if not s["sources"] or sid in ready or (pid == PID and sid in keep):
                    continue
                lid = new_id("lesson")
                self.dojo.store.write("content", "lessons", lid, value={
                    "id": lid, "package": pid, "skill": sid, "skill_text": s["text"], "created": when, "template": LESSON_TEMPLATE,
                    "title": "Written earlier", "summary": "", "sections": [], "misconceptions": [], "self_check": [],
                    "example": {"title": "", "situation": "", "steps": []}, "slides": [], "sources": [], "cited": [],
                    "offered": [r["url"] for r in s["sources"]], "blocked": []})

    def newest(self, sid: str, pid: str = PID) -> dict:
        return self.dojo.lessons_for(pid, sid)[0]

    def attempt(self, sid: str, pid: str = PID) -> dict:
        return self.deeper._doc()["skills"][f"{pid}/{sid}"]

    def written(self) -> int:
        return sum(1 for _, task in self.ai.calls if task == "lesson")


class UpgradeTests(UpgradeCase):
    def test_rewrites_then_missing_lessons_then_upgrades_then_narration(self):
        old = self.old_lesson(OTHER)
        held = self.lesson(S)
        self.web.set(PAGE, GITHUB_MD.replace(td.Q2, td.NEW))
        self.read_due()
        self.assertIn(S, self.drift.rechecking(PID))
        narrate = self.lesson(FOURTH)   # written from the page as it is now, never narrated
        doc = self.dojo.lesson(narrate)   # slides of its own: narration is stored by what it says
        doc["slides"] = [{"title": "Only this lesson", "bullets": ["A slide no other lesson has."]}]
        self.dojo.store.write("content", "lessons", narrate, value=doc)
        self.searched(S, OTHER, THIRD, FOURTH)
        self.fill(keep=(THIRD,))
        record, rewrites = self.record(), self.drift.rewrites_left()
        self.assertEqual(self.deeper.upgrades(), [(PID, OTHER, old)])

        time.sleep(1.1)   # lessons are ordered by the second they were written in
        self.assertTrue(self.preparer.step())   # 1. the drift check's rewrite
        self.assertNotEqual(self.newest(S)["id"], held)
        self.assertEqual((self.drift.rewrites_left(), self.deeper.upgrades_left()), (rewrites - 1, deeper_mod.DAILY_UPGRADES))
        self.assertEqual(self.dojo.lessons_for(PID, THIRD), [])
        self.assertTrue(self.preparer.step())   # 2. the missing lesson
        self.assertEqual(self.newest(THIRD)["template"], LESSON_TEMPLATE)
        self.assertEqual(self.newest(OTHER)["id"], old)
        self.assertEqual(self.deeper.upgrades_left(), deeper_mod.DAILY_UPGRADES)
        with mock.patch.object(self.dojo, "lesson_audio", wraps=self.dojo.lesson_audio) as audio:
            self.assertTrue(self.preparer.step())   # 3. the upgrade, from its own allowance
        new = self.newest(OTHER)
        self.assertNotEqual(new["id"], old)
        self.assertEqual(new["template"], LESSON_TEMPLATE)
        self.assertEqual(self.dojo.lesson(new["id"])["upgrade_of"], old)
        self.assertEqual((self.drift.rewrites_left(), self.deeper.upgrades_left()), (rewrites - 1, deeper_mod.DAILY_UPGRADES - 1),
                         "never one of the drift check's rewrites")
        self.assertEqual([c.args[2] for c in audio.call_args_list], [new["id"]], "narrated as it is written")
        self.assertFalse(self.dojo.lesson_audio_missing(new["id"]))
        self.assertTrue(self.dojo.lesson_audio_missing(narrate))
        self.assertTrue(self.preparer.step())   # 4. narration
        self.assertFalse(self.dojo.lesson_audio_missing(narrate))
        self.assertEqual(self.deeper.upgrades(), [])
        self.assertEqual(self.record(), record, "nothing in the Record changes")
        status = self.get("/api/diag")["preparation"]
        self.assertEqual((status["upgraded"], status["upgrading"]), (1, 0))
        a = self.attempt(OTHER)
        self.assertEqual((a["lesson"], a["outcome"], a["replaced"], a["tries_needed"]), (new["id"], "done", old, 1))
        self.assertEqual(self.deeper._doc()["spent"][PID], {"searched": 0, "attempts": 1, "written": 1})

    def test_the_allowance_is_its_own_and_survives_a_restart(self):
        with mock.patch.object(deeper_mod, "DAILY_UPGRADES", 2):
            olds = {sid: self.old_lesson(sid) for sid in (S, OTHER, THIRD)}
            self.searched(S, OTHER, THIRD)
            self.fill()
            rewrites = self.drift.rewrites_left()
            self.assertEqual(len(self.deeper.upgrades()), 3)
            self.assertTrue(self.preparer.step())
            self.assertTrue(self.preparer.step())
            self.assertEqual((self.deeper.upgrades_left(), self.drift.rewrites_left()), (0, rewrites))
            waiting = [sid for sid in olds if self.newest(sid)["id"] == olds[sid]]
            self.assertEqual(len(waiting), 1)
            self.assertIsNone(self.preparer.next_upgrade(), "the day's allowance is used up")
            self.boot()
            self.assertEqual(self.deeper.upgrades_left(), 0, "a restart does not reset it")
            self.assertIsNone(self.preparer.next_upgrade())
            self.assertEqual([q[1] for q in self.deeper.upgrades()], waiting)
            self.later(24)   # the next UTC day
            self.assertEqual(self.deeper.upgrades_left(), 2)
            self.assertTrue(self.preparer.step())
            self.assertNotEqual(self.newest(waiting[0])["id"], olds[waiting[0]])
            self.assertEqual(self.deeper.upgrades_left(), 1)

    def test_the_active_exam_comes_first_then_exam_weight(self):
        dp_skills = [sid for sid, s in self.dojo.packages.get(DP)["skills"].items() if s["sources"]][:2]
        olds = {(PID, sid): self.old_lesson(sid) for sid in (S, OTHER)}
        olds.update({(DP, sid): self.old_lesson(sid, DP) for sid in dp_skills})
        self.searched(S, OTHER)
        self.searched(*dp_skills, pid=DP)
        self.fill()
        queue = self.deeper.upgrades()
        self.assertEqual(sorted(queue), sorted((pid, sid, lid) for (pid, sid), lid in olds.items()))
        self.assertEqual([pid for pid, _, _ in queue], [PID, PID, DP, DP])
        for pid in (PID, DP):
            shares = [self.dojo.packages.get(pid)["skills"][sid]["share"] for p, sid, _ in queue if p == pid]
            self.assertEqual(shares, sorted(shares, reverse=True))
        self.dojo.save_profile(self.owner, {"active_package": DP})
        self.assertEqual([pid for pid, _, _ in self.deeper.upgrades()], [DP, DP, PID, PID])

    def test_an_attempt_is_written_down_before_the_author_is_called(self):
        old = self.old_lesson(OTHER)
        self.searched(OTHER)
        self.fill()
        seen = []
        self.ai.during = lambda: seen.append(json.loads(json.dumps(self.dojo.store.read("content", deeper_mod.STATE))))
        self.assertTrue(self.preparer.step())
        doc = seen[0]
        self.assertEqual(doc["upgrades"], 1)
        self.assertEqual(doc["skills"][f"{PID}/{OTHER}"]["lesson"], old)
        self.assertEqual((doc["skills"][f"{PID}/{OTHER}"]["tries"], doc["skills"][f"{PID}/{OTHER}"]["outcome"]), (1, "running"))
        self.assertEqual(doc["spent"][PID]["attempts"], 1)

    def test_a_crash_or_a_restart_gets_no_free_retry(self):
        old = self.old_lesson(OTHER)
        self.searched(OTHER)
        self.fill()
        self.ai.crash = True
        for tries in range(1, deeper_mod.UPGRADE_TRIES + 1):
            with self.assertRaises(Crash):
                self.preparer.step()
            self.assertEqual((self.attempt(OTHER)["tries"], self.attempt(OTHER)["outcome"]), (tries, "running"))
            self.boot()
            self.assertIsNone(self.preparer.next_upgrade(), "it waits a day, restart or not")
            self.later(deeper_mod.UPGRADE_WAIT_H)
        written = self.written()
        self.assertTrue(self.preparer.step())
        self.assertEqual(self.written(), written, "the attempt after the last one calls no model")
        a = self.attempt(OTHER)
        self.assertTrue(a["given_up"])
        self.assertEqual((a["tries"], a["outcome"]), (deeper_mod.UPGRADE_TRIES, "failed"))
        self.assertIn("stopped before they finished", a["error"])
        self.assertEqual(self.deeper.upgrades(), [])
        self.assertEqual(self.deeper.upgrades_left(), deeper_mod.DAILY_UPGRADES, "giving up uses none of the allowance")
        self.assertEqual(self.newest(OTHER)["id"], old)

    def test_after_three_failures_it_is_given_up_and_studio_says_so(self):
        old = self.old_lesson(OTHER)
        self.searched(OTHER)
        self.fill()
        self.ai.fail = True
        for tries in range(1, deeper_mod.UPGRADE_TRIES + 1):
            self.assertTrue(self.preparer.step())
            a = self.attempt(OTHER)
            self.assertEqual((a["tries"], a["outcome"], a["error"]), (tries, "failed", "The author did not answer."))
            self.assertEqual(bool(a.get("given_up")), tries == deeper_mod.UPGRADE_TRIES)
            self.assertIsNone(self.preparer.next_upgrade(), "a failed upgrade waits at least a day")
            self.later(deeper_mod.UPGRADE_WAIT_H - 1)
            self.assertIsNone(self.preparer.next_upgrade())
            self.later(1)
        self.assertIsNone(self.preparer.next_upgrade(), "given up")
        self.assertEqual(self.newest(OTHER)["id"], old)
        studio = self.get("/api/studio/deeper")
        row = next(r for r in studio["exams"] if r["package"] == PID)
        self.assertEqual((row["behind"], row["given_up"], row["waiting"]), (1, 1, 0))
        self.assertEqual([(f["skill"], f["what"]) for f in studio["failures"]], [(OTHER, "upgrade")] * 3)
        self.assertEqual(studio["spent"]["lessons"], 3, "the attempts that failed cost money too")
        self.assertEqual(self.get("/api/diag")["deeper_lessons"]["failures"][0]["reason"], "The author did not answer.")

    def test_the_earlier_lesson_stays_until_the_new_one_passes(self):
        old = self.old_lesson(OTHER)
        self.searched(OTHER)
        self.fill()
        self.ai.reject = True
        self.assertTrue(self.preparer.step())
        self.assertEqual([x["id"] for x in self.dojo.lessons_for(PID, OTHER)], [old], "nothing is written")
        self.assertIn("No part of this lesson could be verified", self.attempt(OTHER)["error"])
        self.assertEqual(self.get(f"/api/lessons/{old}")["id"], old, "and the earlier lesson is still read")
        self.later(deeper_mod.UPGRADE_WAIT_H)
        self.ai.reject, self.ai.fewer = False, True
        self.assertTrue(self.preparer.step())
        self.assertEqual([x["id"] for x in self.dojo.lessons_for(PID, OTHER)], [old])
        self.assertIn("kept 2 checked points (67% of what was written) and the lesson it would replace has 3, so the earlier lesson stays",
                      self.attempt(OTHER)["error"])
        self.later(deeper_mod.UPGRADE_WAIT_H)
        self.ai.fewer = False
        self.assertTrue(self.preparer.step())
        new = self.newest(OTHER)
        self.assertNotEqual(new["id"], old)
        self.assertEqual(self.dojo.lesson(new["id"])["upgrade_of"], old)
        self.assertEqual((self.attempt(OTHER)["outcome"], self.attempt(OTHER)["tries_needed"]), ("done", 3))
        self.assertEqual(self.get(f"/api/lessons/{old}")["id"], old, "the earlier lesson stays readable")
        self.assertEqual(self.deeper.upgrades(), [])

    def test_a_lesson_the_drift_check_holds_is_left_to_it(self):
        old = self.old_lesson(S)
        self.searched(S)
        self.fill()
        self.assertEqual(self.deeper.upgrades(), [(PID, S, old)])
        self.web.set(PAGE, GITHUB_MD.replace(td.Q2, td.NEW))
        self.read_due()
        self.assertIn(S, self.drift.rechecking(PID))
        self.assertEqual(self.deeper.upgrades(), [])
        self.assertFalse(self.deeper.charge_upgrade(PID, S, old), "held by now: nothing is counted")
        self.assertEqual((self.deeper._doc()["upgrades"], self.deeper._doc()["skills"]), (0, {}))
        rewrites = self.drift.rewrites_left()
        self.assertTrue(self.preparer.step())
        new = self.newest(S)
        self.assertNotEqual(new["id"], old)
        self.assertEqual(new["template"], LESSON_TEMPLATE, "the drift check's rewrite is on the deeper template too")
        self.assertEqual((self.drift.rewrites_left(), self.deeper.upgrades_left()), (rewrites - 1, deeper_mod.DAILY_UPGRADES))
        self.assertEqual(self.deeper.upgrades(), [])

    def test_a_skill_whose_pages_are_all_gone_is_left_to_the_drift_check(self):
        old = self.old_lesson(S)
        self.searched(S)
        self.fill()
        self.web.set(PAGE, "Not found", status=404)
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertIn(S, self.drift.gone_skills(PID))
        self.assertNotIn((PID, S, old), self.deeper.upgrades())
        self.assertFalse(self.deeper.charge_upgrade(PID, S, old))
        self.assertEqual((self.deeper._doc()["upgrades"], self.deeper._doc()["skills"]), (0, {}))

    def test_a_built_in_skill_waits_for_its_search_and_an_added_exam_does_not(self):
        old = self.old_lesson(OTHER)
        self.fill()
        self.assertEqual(self.deeper.upgrades(), [], "written once, from all its pages, after the search")
        row = next(r for r in self.get("/api/studio/deeper")["exams"] if r["package"] == PID)
        self.assertEqual((row["behind"], row["searching_first"], row["waiting"]), (1, 1, 0))
        self.searched(OTHER)
        self.assertEqual(self.deeper.upgrades(), [(PID, OTHER, old)])
        added = small_package()
        self.dojo.packages.add(added)
        sid = added["domains"][0]["groups"][0]["skills"][0]["id"]
        mine = self.old_lesson(sid, added["id"])
        self.assertIn((added["id"], sid, mine), self.deeper.upgrades(), "an added exam is not searched")

    def test_a_lesson_not_written_from_a_page_found_later_is_written_again(self):
        self.searched(OTHER)
        lid = self.lesson(OTHER)
        self.fill()
        self.assertEqual(self.deeper.upgrades(), [])
        self.searched(OTHER, pages=[ref(ADDED)])
        self.assertEqual(self.deeper.upgrades(), [(PID, OTHER, lid)])
        meta = next(m for m in self.dojo.lesson_index() if m["id"] == lid)
        self.assertEqual(self.deeper.why(self.dojo.packages.get(PID)["skills"][OTHER], meta), "pages")
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.ai.every_page = True
        time.sleep(1.1)   # lessons are ordered by the second they were written in
        self.assertTrue(self.preparer.step())
        new = self.dojo.lesson(self.newest(OTHER)["id"])
        self.assertEqual((new["upgrade_of"], new["offered"], new["cited"]), (lid, urls(self.dojo.packages.get(PID), OTHER), [
            urls(self.dojo.packages.get(PID), OTHER)[0], ADDED]))
        self.assertEqual(self.deeper.upgrades(), [])

    def test_a_page_that_cannot_be_read_now_is_waited_for(self):
        self.searched(OTHER)
        lid = self.lesson(OTHER)
        self.fill()
        self.searched(OTHER, pages=[ref(ADDED)])
        self.web.set(td.ELSEWHERE, "Busy", status=500)
        written = self.written()
        self.assertTrue(self.preparer.step())
        self.assertEqual(self.written(), written, "no model is called")
        self.assertEqual(self.newest(OTHER)["id"], lid)
        a = self.attempt(OTHER)
        self.assertEqual((a["tries"], a["outcome"]), (1, "failed"))
        self.assertIn(f"The page {ADDED} could not be read just now, so the deeper lesson waits for it.", a["error"])


class FoundPageDriftTests(UpgradeCase):
    """The drift check reads a page Dojo found exactly as it reads a page the package cites."""

    def test_a_page_found_that_loses_a_quote_holds_the_lesson_and_a_page_found_that_goes_is_gone(self):
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.searched(S, pages=[ref(ADDED)])
        self.fill()
        self.assertIn(ADDED, self.drift.cited())
        self.ai.every_page = True
        lesson = self.lesson(S)
        self.assertEqual(self.dojo.lesson(lesson)["cited"], [URL, ADDED])
        self.assertTrue(self.dojo.sources.head(ADDED)["verified"])
        self.web.set(td.ELSEWHERE, ELSE_MD.replace(QUOTE, CHANGED))
        self.read_due()
        self.assertTrue(self.drift.withheld(lesson))
        self.assertIn(S, self.drift.rechecking(PID))
        self.assertEqual(self.get(f"/api/lessons/{lesson}")["drift"]["removed"], [QUOTE])
        self.assertEqual(self.preparer.rewrites(), [(PID, S)])
        self.assertEqual(self.deeper.upgrades(), [], "the drift check writes it again, not the upgrade pass")
        self.web.set(td.ELSEWHERE, "Not found", status=404)
        self.later(25)
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertTrue(self.dojo.sources.head(ADDED)["gone"])
        self.assertNotIn(S, self.drift.gone_skills(PID), "the skill still has the package's own page")
        self.ai.every_page = False
        time.sleep(1.1)   # lessons are ordered by the second they were written in
        self.assertTrue(self.preparer.step())
        new = self.dojo.lesson(self.newest(S)["id"])
        self.assertNotEqual(new["id"], lesson)
        self.assertEqual([r["url"] for r in new["sources"]], [URL], "nothing new is written from a page that is gone")
        self.assertEqual(self.deeper.upgrades(), [], "and the page that went is no reason to write it once more")

    def test_a_page_found_that_moves_twice_is_gone(self):
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.searched(S, pages=[ref(ADDED)])
        self.ai.every_page = True
        lesson = self.lesson(S)
        self.web.set(td.ELSEWHERE, "", status=301, headers={"location": td.API + "/en/copilot/elsewhere"})
        self.read_due()
        self.assertFalse(self.drift.withheld(lesson), "one read is not enough")
        self.later(7)
        self.read_due()
        head = self.dojo.sources.head(ADDED)
        self.assertEqual((head["gone"] is not None, head["moved"]), (True, GH + "/en/copilot/elsewhere"))
        self.assertTrue(self.drift.withheld(lesson))
        self.assertNotIn(S, self.drift.gone_skills(PID))


class ReplacedPageTests(UpgradeCase):
    """A page Dojo found goes, a search finds one to take its place, and the page that went leaves the
    package (ADR 0008, review round 1)."""

    def search_finds(self, *pages: str) -> dict:
        """The search for S, its result given: these pages, checked. Says what the search was told."""
        seen = {}

        def match(*args, **kwargs):
            seen.update(known=[k["url"] for k in kwargs["known"][S]], limit=kwargs["limits"][S])
            return {S: {"sources": [ref(p) for p in pages], "candidates": len(pages)}}
        pkg = self.dojo.packages.get(PID)
        d, g = next((d, g) for d in pkg["domains"] for g in d["groups"] if S in g["skills"])
        with mock.patch.object(self.deeper.finder, "match_group", side_effect=match):
            self.deeper.search_group(PID, d, g, [S])
        return seen

    def due(self) -> set[str]:
        return {sid for p, _, _, sids in self.deeper.search_targets() if p == PID for sid in sids}

    def test_an_upgrade_waits_for_the_search_for_a_page_to_replace_one_that_went(self):
        old = self.old_lesson(S)   # from the package's own page, on the earlier template
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.dojo.sources.get(ADDED)
        self.searched(S, pages=[ref(ADDED), ref(ADDED_2)])
        self.assertIn((PID, S, old), self.deeper.upgrades())
        self.dojo.sources.mark_gone(ADDED, iso(self.clock))
        self.assertNotIn(S, [sid for _, sid, _ in self.deeper.upgrades()], "it waits for the search for a page to take its place")
        self.assertIn(old, self.deeper.to_write_again(), "and its Deeper Listen waits with it")
        row = next(r for r in self.get("/api/studio/deeper")["exams"] if r["package"] == PID)
        self.assertEqual((row["replacing"] >= 1, row["searching_first"] >= 1), (True, True))
        self.later(deeper_mod.SEARCH_RETRY_H)
        self.assertIn(S, self.due())
        seen = self.search_finds(ADDED_3)
        self.assertEqual(seen["limit"], 1)
        self.assertIn(ADDED, seen["known"])
        self.assertEqual(urls(self.dojo.packages.get(PID), S), [URL, ADDED_2, ADDED_3])
        self.assertIn((PID, S, old), self.deeper.upgrades(), "then it is written once, from the three pages")

    def test_a_page_that_went_is_forgotten_once_nothing_cites_it_and_answers_are_judged_from_its_copy(self):
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.searched(S, pages=[ref(ADDED), ref(ADDED_2)])
        self.fill()
        self.ai.every_page = True
        lesson = self.lesson(S)
        self.assertIn(ADDED, self.dojo.lesson(lesson)["cited"])
        item = self.item(S)
        self.assertEqual([r["url"] for r in self.dojo.item(self.owner, item)["sources"]], [URL, ADDED, ADDED_2])
        chat = self.ai.chat

        def grader_down(role: str, system: str, user: str, json_mode: bool = True) -> str:
            if "TASK: grade" in system:
                raise AIError("The grader did not answer.")
            return chat(role, system, user, json_mode)
        with mock.patch.object(self.ai, "chat", side_effect=grader_down):
            r = self.c.post(f"/api/items/{item}/answer", json={"text": "r1 r2 everything"}, headers=POST)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.wait_job(r.json()["id"])["state"], "failed")   # answered, not judged yet

        self.web.set(td.ELSEWHERE, "Not found", status=404)
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertTrue(self.dojo.sources.head(ADDED)["gone"])
        self.assertTrue(self.drift.withheld(lesson))
        self.assertIn(ADDED, self.drift._doc()["pages"])
        self.later(deeper_mod.SEARCH_RETRY_H)
        self.assertIn(S, self.due(), "a page Dojo found went: one search for a page to take its place")
        self.search_finds(ADDED_3)
        self.assertEqual(urls(self.dojo.packages.get(PID), S), [URL, ADDED_2, ADDED_3])
        doc = self.drift._doc()
        self.assertIn(ADDED, doc["replaced"], "the newest lesson still cites it: it is still held and written again")
        self.assertIn(ADDED, doc["pages"])
        self.assertTrue(self.drift.withheld(lesson))
        self.assertEqual(self.preparer.rewrites(), [(PID, S)])

        time.sleep(1.1)   # lessons are ordered by the second they were written in
        self.assertTrue(self.preparer.step())
        new = self.dojo.lesson(self.newest(S)["id"])
        self.assertNotEqual(new["id"], lesson)
        self.assertEqual([r["url"] for r in new["sources"]], [URL, ADDED_2, ADDED_3])
        self.drift.step()
        doc = self.drift._doc()
        self.assertNotIn(ADDED, self.drift.cited())
        self.assertEqual(doc["replaced"], {}, "nothing cites it now: what the check wrote down for it goes")
        self.assertNotIn(ADDED, doc["pages"])
        self.assertFalse([c for c in doc["changes"] if c.get("url") == ADDED])
        self.assertIsNotNone(self.dojo.sources.stored(ADDED), "its copy stays")
        self.assertEqual(self.deeper.upgrades(), [])

        prompts: list[str] = []

        def grader(role: str, system: str, user: str, json_mode: bool = True) -> str:
            if "TASK: grade" in system:
                prompts.append(user)
            return chat(role, system, user, json_mode)
        with mock.patch.object(self.ai, "chat", side_effect=grader):
            r = self.c.post(f"/api/items/{item}/grade", headers=POST)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.wait_job(r.json()["id"])["state"], "done")
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertIn(f'<source n="2" url="{ADDED}"', prompt, "judged from the copy of the page it was written from")
            self.assertIn(QUOTE, prompt)
            self.assertNotIn(ADDED_3, prompt, "never from the page that took its place")


class GradingTests(UpgradeCase):
    """An answer is judged against the pages its item was written from, at the size it was written with:
    pages found later change nothing for an item written before them."""

    def test_an_item_is_judged_from_the_pages_it_was_written_from(self):
        self.web.set(td.ELSEWHERE, ELSE_MD)
        earlier = self.item(S)
        self.searched(S, pages=[ref(ADDED)])
        later = self.item(S)
        self.assertEqual([r["url"] for r in self.dojo.item(self.owner, earlier)["sources"]], [URL])
        self.assertEqual([r["url"] for r in self.dojo.item(self.owner, later)["sources"]], [URL, ADDED])
        prompts: list[list[str]] = []
        sizes: list[list[int]] = []
        chat, cut = self.ai.chat, learning_mod.excerpt

        def grader(role: str, system: str, user: str, json_mode: bool = True) -> str:
            if "TASK: grade" in system:
                prompts[-1].append(user)
            return chat(role, system, user, json_mode)

        def excerpt(text: str, query: str, size: int) -> str:
            sizes[-1].append(size)
            return cut(text, query, size)

        with mock.patch.object(self.ai, "chat", side_effect=grader), \
                mock.patch.object(learning_mod, "excerpt", side_effect=excerpt):
            for item in (earlier, later):
                prompts.append([])
                sizes.append([])
                r = self.c.post(f"/api/items/{item}/answer", json={"text": "r1 r2 everything"}, headers=POST)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.wait_job(r.json()["id"])["state"], "done")
        first, second = (p[0] for p in prompts)
        self.assertIn('<source n="1"', first)
        self.assertNotIn('<source n="2"', first, "the page found later is not part of the earlier item's rubric")
        self.assertIn('<source n="2"', second)
        self.assertEqual(sizes, [[GROUNDING_CHARS[1]], [GROUNDING_CHARS[2]] * 2])
        self.assertIsNotNone(self.dojo.item(self.owner, earlier)["result"])

    def test_an_item_is_judged_at_the_size_it_was_written_with(self):
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.searched(S, pages=[ref(ADDED), ref(ADDED_2)])
        it = self.dojo.item(self.owner, self.item(S))
        self.assertEqual([r["url"] for r in it["sources"]], [URL, ADDED, ADDED_2])
        self.assertEqual([r["excerpt_chars"] for r in it["sources"]], [GROUNDING_CHARS[3]] * 3, "each page records its size")
        sizes: list[int] = []
        cut = learning_mod.excerpt

        def excerpt(text: str, query: str, size: int) -> str:
            sizes.append(size)
            return cut(text, query, size)

        def unrecorded(refs: list[dict]) -> dict:
            return it | {"sources": [{k: v for k, v in r.items() if k != "excerpt_chars"} for r in refs]}

        with mock.patch.object(learning_mod, "excerpt", side_effect=excerpt):
            self.dojo.item_grounding(it)
            # An item written before ADR 0008 records no size: Dojo cut 18,000 characters of one page then,
            # and 12,000 of each page when there were more (not 10,000 of three, as now).
            self.dojo.item_grounding(unrecorded(it["sources"]))
            self.dojo.item_grounding(unrecorded(it["sources"][:1]))
        self.assertEqual(sizes, [GROUNDING_CHARS[3]] * 3 + [12000] * 3 + [18000])

    def grading(self) -> tuple[list[str], list[str], Callable]:
        """What the grader is shown, and every page read from the web, while the context lasts."""
        prompts: list[str] = []
        fetched: list[str] = []
        chat, get = self.ai.chat, self.dojo.sources.get

        def grader(role: str, system: str, user: str, json_mode: bool = True) -> str:
            if "TASK: grade" in system:
                prompts.append(user)
            return chat(role, system, user, json_mode)

        def read(url: str, *args, **kwargs) -> dict:
            fetched.append(url)
            return get(url, *args, **kwargs)

        def watch():
            stack = ExitStack()
            stack.enter_context(mock.patch.object(self.ai, "chat", side_effect=grader))
            stack.enter_context(mock.patch.object(self.dojo.sources, "get", side_effect=read))
            return stack
        return prompts, fetched, watch

    def answer(self, item: str) -> dict:
        r = self.c.post(f"/api/items/{item}/answer", json={"text": "r1 r2 everything"}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        return self.wait_job(r.json()["id"])

    def test_an_item_is_judged_from_a_page_the_package_no_longer_cites(self):
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.searched(S, pages=[ref(ADDED)])
        item = self.item(S)
        self.assertEqual([r["url"] for r in self.dojo.item(self.owner, item)["sources"]], [URL, ADDED])
        self.searched(S, pages=[ref(ADDED_2)])   # the page found leaves the package; another one is there now
        self.assertEqual(urls(self.dojo.packages.get(PID), S), [URL, ADDED_2])
        prompts, fetched, watch = self.grading()
        with watch():
            self.assertEqual(self.answer(item)["state"], "done")
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertIn(f'<source n="1" url="{URL}"', prompt)
            self.assertIn(f'<source n="2" url="{ADDED}"', prompt, "the page the rubric was written from")
            self.assertIn(QUOTE, prompt, "as Dojo stores it")
            self.assertNotIn(ADDED_2, prompt, "a page the skill cites now never stands in for it")
        self.assertEqual(fetched, [], "the stored copies are used: nothing is read again")
        self.assertIsNotNone(self.dojo.item(self.owner, item)["result"])

    def test_without_a_copy_of_its_own_page_an_answer_waits_and_no_other_page_is_used(self):
        self.web.set(td.ELSEWHERE, ELSE_MD)
        self.searched(S, pages=[ref(ADDED)])
        item = self.item(S)
        it = self.dojo.item(self.owner, item)
        it["sources"] = [r for r in it["sources"] if r["url"] == ADDED]   # written from the page found alone
        self.dojo._save_item(self.owner, it)
        key = Sources.key(ADDED)
        snap = self.dojo.store.read("sources", key)
        final = snap.pop("final")   # a copy not read under the documentation rule is never used
        self.dojo.store.write("sources", key, value=snap)
        prompts, fetched, watch = self.grading()
        with watch():
            job = self.answer(item)
        self.assertEqual(job["state"], "failed")
        self.assertIn("holds no copy it may use of the official page this item was written from", job["error"])
        self.assertIn(ADDED, job["error"])
        self.assertEqual(prompts, [], "not judged against the package's own page, or any other")
        self.assertEqual(fetched, [])
        self.assertIsNone(self.dojo.item(self.owner, item).get("result"), "the answer waits")
        self.dojo.store.write("sources", key, value={**snap, "final": final})
        with watch():
            r = self.c.post(f"/api/items/{item}/grade", headers=POST)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.wait_job(r.json()["id"])["state"], "done")
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertIn(f'<source n="2" url="{ADDED}"', prompt, "its own page, with its own number")
            self.assertNotIn(f'url="{URL}"', prompt)
        self.assertIsNotNone(self.dojo.item(self.owner, item)["result"])


class EarlierFilmTests(UpgradeCase):
    """Watch keeps the presenter video of the lesson an upgrade replaced and says plainly that it was filmed
    from an earlier version. Filming again happens only through Studio's button, which shows the price."""

    def upgraded_after_filming(self, sid: str) -> tuple[str, str]:
        self.dojo.store.write("content", "prices", value={**td.PRICES, "fetched": iso()})
        old = self.old_lesson(sid)
        self.dojo.lesson_video(lambda step: None, self.owner, old)
        self.assertTrue(self.drift._filmed(old))
        self.searched(sid)
        self.fill()
        self.ai.say = "Here is the idea, now from every page the skill cites."
        self.assertTrue(self.preparer.step())
        new = self.newest(sid)["id"]
        self.assertNotEqual(new, old)
        self.assertFalse(self.drift._filmed(new))
        return old, new

    def test_watch_shows_the_earlier_film_until_the_owner_films_again(self):
        old, new = self.upgraded_after_filming(OTHER)
        view = self.get(f"/api/lessons/{new}")
        film = view["earlier_film"]
        self.assertEqual((film["id"], film["remaking"]), (old, False))
        written = parse_iso(view["created"])
        self.assertEqual(film["note"], learning_mod.EARLIER_FILM.format(date=f"{written.day} {written:%b %Y}"))
        self.assertNotIn(view["created"][:10], film["note"], "the date reads as people write it")
        self.assertEqual(film["slides"], self.dojo.lesson(old)["slides"])
        self.assertTrue(film["video"])
        self.assertIsNone(self.get(f"/api/lessons/{old}")["earlier_film"], "a filmed lesson shows its own film")
        for _ in range(3):
            self.preparer.step()
        self.assertEqual(self.video_jobs(), [], "nothing is filmed on its own")
        refilm = self.get("/api/studio/drift")["refilm"]
        self.assertEqual([(x["id"], x["replaces"], x["why"]) for x in refilm["lessons"]], [(new, old, "deeper")])
        self.assertGreater(refilm["estimate"]["usd"], 0, "the button shows the price")
        self.drift._set_film(new, state="approved")
        self.assertTrue(self.get(f"/api/lessons/{new}")["earlier_film"]["remaking"])
        self.drift._set_film(new, state=None)
        with mock.patch.object(drift_mod, "FILM_WAIT_S", 0.05):
            r = self.c.post("/api/studio/drift/refilm", json={"lessons": [new]}, headers=POST)
            self.assertEqual(r.status_code, 200, r.text)
            jobs = self.video_jobs()
            self.assertEqual([j["ref"] for j in jobs], [new], "one film, through the ordinary path")
            self.assertEqual(self.wait_job(jobs[0]["id"])["state"], "done")
        self.assertTrue(self.drift._filmed(new))
        self.assertIsNone(self.get(f"/api/lessons/{new}")["earlier_film"])
        self.assertTrue(self.drift._filmed(old), "the earlier film is not removed")

    def test_an_earlier_film_a_page_change_no_longer_supports_is_not_shown(self):
        old, new = self.upgraded_after_filming(S)
        self.assertEqual(self.get(f"/api/lessons/{new}")["earlier_film"]["id"], old)
        self.web.set(PAGE, GITHUB_MD.replace(td.Q2, td.NEW))
        self.read_due()
        self.assertTrue(self.drift.withheld(old))
        self.assertIsNone(self.get(f"/api/lessons/{new}")["earlier_film"])


class DeeperListenTests(UpgradeCase):
    """Deeper Listen (ADR 0007) waits for a lesson that will be written again on the deeper template, so it is
    written, and paid for, once: for the new lesson. An upgrade retires the Deeper Listen of the lesson it
    replaces, as a rewrite after a page change does."""

    def only(self, sid: str) -> None:
        """Deeper Listen looks at this skill of GH-300 only: the lessons `fill` writes are not part of these tests."""
        targets = self.preparer.deep_targets
        self.preparer.deep_targets = lambda: [t for t in targets() if t[:2] == (PID, sid)]

    def test_deeper_listen_waits_for_the_lesson_written_again(self):
        old = self.old_lesson(OTHER)
        self.only(OTHER)
        self.assertIsNone(self.preparer.next_deep(), "not even while its skill waits for its search")
        self.assertEqual(self.preparer.deep_status()["waiting_upgrade"], 1)
        self.assertEqual(self.get(f"/api/lessons/{old}")["deep"], {"state": "upgrading"}, "Listen says why")
        self.searched(OTHER)
        self.fill()
        self.assertEqual(self.deeper.upgrades(), [(PID, OTHER, old)])
        self.assertIsNone(self.preparer.next_deep())
        record = self.record()
        self.assertTrue(self.preparer.step())   # the upgrade
        new = self.newest(OTHER)["id"]
        self.assertNotEqual(new, old)
        self.assertIsNone(self.dojo.deep(old), "the lesson replaced never got one")
        self.assertEqual(self.get(f"/api/lessons/{new}")["deep"], {"state": "missing"})
        self.assertEqual(self.preparer.next_deep(), ("write", PID, OTHER, new))
        self.assertEqual(self.preparer.deep_status()["waiting_upgrade"], 0)
        self.assertTrue(self.preparer.step())   # its Deeper Listen
        self.assertTrue(self.dojo.deep_ready(new))
        self.assertEqual(self.get(f"/api/lessons/{new}")["deep"]["state"], "ready")
        self.assertEqual(self.dojo.store.read("content", "deep-state")["written"], 1, "written once")
        self.assertEqual(self.record(), record, "nothing in the Record changes")

    def test_an_upgrade_given_up_lets_the_earlier_lesson_have_one(self):
        old = self.old_lesson(OTHER)
        self.dojo.lesson_audio(lambda step: None, self.owner, old)   # its slides are narrated already
        self.only(OTHER)
        self.searched(OTHER)
        self.fill()
        self.ai.fail = True
        with mock.patch.object(deeper_mod, "UPGRADE_TRIES", 1):
            self.assertTrue(self.preparer.step())
            self.assertTrue(self.attempt(OTHER)["given_up"])
            self.assertEqual(self.preparer.next_deep(), ("write", PID, OTHER, old), "it is not written again")
            self.assertEqual(self.get(f"/api/lessons/{old}")["deep"], {"state": "missing"})
            self.assertTrue(self.preparer.step())
        self.assertTrue(self.dojo.deep_ready(old))

    def test_an_upgrade_retires_the_deeper_listen_of_the_lesson_it_replaces(self):
        self.dojo.store.write("content", "prices", value={**td.PRICES, "fetched": iso()})
        old = self.old_lesson(OTHER)
        self.only(OTHER)
        self.dojo.make_deep(lambda step: None, old)   # written before this feature reached the lesson
        self.assertTrue(self.dojo.deep_audio(lambda step: None, old))
        self.assertEqual(self.get(f"/api/lessons/{old}")["deep"]["state"], "ready", "it plays until the new lesson passes")
        self.searched(OTHER)
        self.fill()
        self.assertIsNone(self.preparer.next_deep())
        self.assertEqual(self.preparer.deep_status()["waiting_upgrade"], 0, "it plays: nothing waits")
        card = self.get("/api/studio/deeper")
        row = next(r for r in card["exams"] if r["package"] == PID)
        self.assertEqual((row["behind"], row["deep_again"]), (1, 1))
        lines = {line["key"]: line for line in card["estimate"]["lines"]}
        self.assertEqual((card["estimate"]["deep_again"], lines["deep_again"]["quantity"]), (1, 1))
        self.assertEqual(next(r for r in self.get("/api/diag")["deeper_lessons"]["exams"] if r["package"] == PID)["deep_again"], 1)
        self.assertTrue(self.preparer.step())   # the upgrade
        new = self.newest(OTHER)["id"]
        self.assertNotEqual(new, old)
        d = self.dojo.deep(old)
        self.assertEqual(d["state"], "invalidated")
        self.assertIn(f"written again on the deeper template; {new} was written in its place", d["reason"])
        self.assertEqual(self.get(f"/api/lessons/{old}")["deep"]["state"], "invalidated")
        self.assertIsNone(self.dojo.deep(new))
        self.assertEqual(self.preparer.next_deep(), ("write", PID, OTHER, new), "the new lesson gets its own")
        self.assertTrue(self.preparer.step())
        self.assertTrue(self.dojo.deep_ready(new))
        self.assertFalse(self.dojo.deep_ready(old), "for good")
        self.assertEqual(len(self.dojo.deep_docs()), 2, "the old one stays, with what it cost")
        self.assertEqual(next(r for r in self.get("/api/studio/deeper")["exams"] if r["package"] == PID)["deep_again"], 0)


if __name__ == "__main__":
    unittest.main()
