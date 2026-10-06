"""The source-drift check (ADR 0006), with a mocked web. A cited page changes only its formatting, loses
a quote, goes away, or asks Dojo to slow down, and Dojo restarts in the middle of a read. What a change no
longer supports is held back from the podcast feed, the question pool, the 3-minute checks, the plan and
the answer box, and the lesson is written again. The Record is never rewritten, and nothing is filmed
until the owner presses the button in Studio."""
import json
import os
import tempfile
import threading
import time
import types
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.drift as drift_mod  # noqa: E402
from app.core import UserError, iso, new_id, utcnow  # noqa: E402
from app.costs import PRICES_VERSION, retail_filter  # noqa: E402
from app.exam import blocker, for_export, mark_seen, plan_skills, question_key, spec, untestable  # noqa: E402
from app.labs import DISTANCE_SOURCE, JSON_SOURCE, LABS, VECTOR_SOURCE  # noqa: E402
from app.learning import rehearsal_held_note  # noqa: E402
from app.main import create_app  # noqa: E402
from app.plan import to_local  # noqa: E402
from app.prepare import Preparer  # noqa: E402
from app.readiness import new_scored, read_attempt, withdrawn_note  # noqa: E402
from app.sources import same_page  # noqa: E402
from tests.test_api import GITHUB_MD, LEARN_HTML, POST, settings, web  # noqa: E402
from tests.test_plan import BASE, Record, ahead, blocks, own, unfold, value  # noqa: E402

PID = "gh-300"
S = "d1.g1.s1"          # cites the inline-suggestions page
OTHER = "d1.g1.s2"      # cites the chat page
PAGE = "/en/copilot/responsible-use/inline-suggestions"
URL = "https://docs.github.com" + PAGE
API = "https://docs.github.com/api/article/body?pathname="   # where Dojo reads a GitHub Docs page
ELSEWHERE = "/en/copilot/responsible-use/code-completions"      # a page no skill cites
LEARN = "https://learn.microsoft.com"
# The stub author quotes whole sentences of the page it is given: lessons take the first three, item
# rubrics the first and the last, practice-exam questions the first.
Q0, Q1, Q2, Q3 = [line for line in GITHUB_MD.splitlines() if line and not line.startswith("#")]
NEW = "Only enterprise owners can now change the content exclusion settings of every repository in an enterprise."
FORMATTED = GITHUB_MD.replace("use content exclusion to", "use **content exclusion** to").replace("\n\n", "\n\n\n")
PRICES = {"version": PRICES_VERSION, "region": "swedencentral", "source": "test", "filter": retail_filter("swedencentral"),
          "meters": {"video": {"meter": "Avatar", "price": 1.0, "unit": "1 Minute", "currency": "USD", "per_second": 1 / 60},
                     "voice": {"meter": "Voice", "price": 15.0, "unit": "1M", "currency": "USD", "per_character": 15 / 1_000_000},
                     "transcription": None},
          "models": {}}
# The pages the labs cite: vector cites the first, search the second, hybrid the third and the second.
VECTOR_PATH, DISTANCE_PATH, JSON_PATH = (urlparse(u).path for u in (VECTOR_SOURCE, DISTANCE_SOURCE, JSON_SOURCE))
LEARN_CHANGED = LEARN_HTML.replace("Rowstore indexes perform best on queries that seek into the data",
                                   "Rowstore indexes now work best on queries that look up rows")
LEARN_FORMATTED = LEARN_HTML.replace("is a technology for storing", "is a technology</p>\n<p>for storing")


class Web:
    """docs.github.com and learn.microsoft.com page by page: each page can be changed, taken away or rate
    limited. Every request is written down, so a test can count the reads."""

    def __init__(self):
        self.pages: dict[str, tuple[int, str, dict]] = {}
        self.reads: list[str] = []
        self.learn: list[str] = []
        self.hosts: list[str] = []

    def set(self, path: str, text: str = "", status: int = 200, headers: dict | None = None) -> None:
        self.pages[path] = (status, text, headers or {})

    def count(self, path: str = PAGE) -> int:
        return self.reads.count(path)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        if request.url.host == "docs.github.com":
            path = request.url.params.get("pathname", "")
            self.reads.append(path)
            status, text, headers = self.pages.get(path, (200, GITHUB_MD, {}))
            return httpx.Response(status, text=text, headers={"content-type": "text/markdown", **headers})
        if request.url.host == "learn.microsoft.com":
            self.learn.append(request.url.path)
            status, text, headers = self.pages.get(request.url.path, (200, LEARN_HTML, {}))
            return httpx.Response(status, text=text, headers={"content-type": "text/html", **headers})
        return web(request)


class DriftCase(unittest.TestCase):
    labs: dict = {}   # the labs the check watches: none, except where a test is about them

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-drift-")
        self.web = Web()
        # A day after the pages were first read, so every page Dojo knows is due. At noon (UTC), so that
        # moving the clock by a few hours never starts a new day, and with it a new allowance of reads.
        due = utcnow() + timedelta(hours=25)
        noon = due.replace(hour=12, minute=0, second=0, microsecond=0)
        self.clock = noon if noon >= due else noon + timedelta(days=1)
        self.boot()
        gap = mock.patch.object(drift_mod, "FETCH_GAP_S", 0)
        gap.start()
        self.addCleanup(gap.stop)

    def boot(self) -> None:
        """Start Dojo on the same data folder, as a restart does."""
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(self.web)))
        self.c = TestClient(self.app)
        self.dojo, self.drift = self.app.state.dojo, self.app.state.drift
        self.owner = self.drift.owner
        self.drift.now = lambda: self.clock
        self.drift.labs = self.labs

    def later(self, hours: float) -> None:
        self.clock += timedelta(hours=hours)

    def read_due(self) -> int:
        """Step the check until no page is due. Returns how many pages it read."""
        reads = 0
        for _ in range(30):
            before = self.drift._doc()["read"]
            self.drift.step()
            if self.drift._doc()["read"] == before:
                return reads
            reads += 1
        self.fail("pages kept coming due")

    def lesson(self, sid: str = S) -> str:
        return self.dojo.make_lesson(lambda step: None, self.owner, PID, sid)["lesson"]

    def item(self, sid: str = S, mode: str = "practice") -> str:
        return self.dojo.make_item(lambda step: None, self.owner, PID, sid, mode)["item"]

    def record(self) -> bytes:
        return self.dojo.store.read_bytes("learners", self.owner.key, "events.jsonl") or b""

    def video_jobs(self) -> list[dict]:
        return [j for j in self.dojo.jobs.for_learner(self.owner) if j.get("kind") == "video"]

    def wait_job(self, job_id: str) -> dict:
        for _ in range(400):
            job = self.dojo.store.read("jobs", job_id) or {}
            if job.get("state") not in ("queued", "running"):
                return job
            time.sleep(0.05)
        self.fail("the job did not finish")

    def plan_row(self, sid: str) -> dict:
        return next(r for r in self.app.state.podcast._plan(PID) if r["skill"] == sid)

    def get(self, path: str) -> dict:
        r = self.c.get(path)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class SetupTests(DriftCase):
    def test_the_skills_used_here_cite_the_pages_the_tests_change(self):
        urls = lambda sid: [s["url"] for s in self.dojo.packages.skill(PID, sid)["sources"]]  # noqa: E731
        self.assertEqual(urls(S), [URL])
        self.assertNotIn(URL, urls(OTHER))

    def test_only_the_allowed_sites_are_ever_read_again(self):
        cited = self.drift.cited()
        self.assertTrue(cited)
        self.assertTrue(all(urlparse(u).hostname in ("docs.github.com", "learn.microsoft.com") for u in cited))

    def test_a_page_never_read_is_never_fetched(self):
        # Nothing was written from any page yet, so there is nothing to check again.
        self.assertEqual(self.read_due(), 0)
        self.assertEqual(self.web.reads, [])

    def test_studio_and_diag_describe_the_check_before_any_change(self):
        self.lesson()
        studio = self.get("/api/studio/drift")
        self.assertEqual(studio["changes"], [])
        self.assertEqual(studio["refilm"]["lessons"], [])
        self.assertEqual(studio["limits"]["per_day"], drift_mod.DAILY_FETCHES)
        self.assertIn("never rewritten", studio["record"])
        block = self.get("/api/diag")["drift_check"]
        self.assertFalse(block["started"])
        self.assertEqual((block["daily_limit"], block["read_today"], block["pages_known"]), (drift_mod.DAILY_FETCHES, 0, 1))
        self.assertEqual(block["lessons_rechecking"], 0)
        self.assertEqual(block["rewrites"], {"per_day": drift_mod.DAILY_REWRITES, "used_today": 0,
                                             "left_today": drift_mod.DAILY_REWRITES, "waiting": 0, "waiting_for_pages": 0,
                                             "last": None})
        self.assertEqual(studio["rewrites"], block["rewrites"])
        self.assertEqual(studio["limits"]["rewrites_per_day"], drift_mod.DAILY_REWRITES)

    def test_the_check_runs_in_azure_and_locally_only_when_asked(self):
        with TestClient(self.app):
            self.assertFalse(self.drift._started)
        with mock.patch.dict(os.environ, {"DOJO_DRIFT": "1"}):
            asked = create_app(settings(tempfile.mkdtemp(prefix="dojo-drift-on-")),
                               http=httpx.Client(transport=httpx.MockTransport(self.web)))
            with mock.patch.object(drift_mod.DriftCheck, "_loop"), TestClient(asked):
                self.assertTrue(asked.state.drift._started)


class FormattingTests(DriftCase):
    def test_a_formatting_only_change_starts_no_work(self):
        lesson, item = self.lesson(), self.item()
        before, stored = self.record(), self.dojo.sources.head(URL)["sha256"]
        self.web.set(PAGE, FORMATTED)
        self.assertEqual(self.read_due(), 1)
        self.assertNotEqual(self.dojo.sources.head(URL)["sha256"], stored, "the new version is stored: its hash differs")
        self.assertIsNone(self.drift.lesson_state(lesson))
        self.assertFalse(self.drift.withheld(lesson))
        self.assertIsNone(self.drift.item_note(self.dojo.item(self.owner, item)))
        self.assertEqual(self.drift.rechecking(PID), set())
        self.assertIsNotNone(self.plan_row(S)["lesson"], "the feed keeps the lesson")
        self.assertIsNone(self.get(f"/api/lessons/{lesson}")["drift"])
        studio = self.get("/api/studio/drift")
        self.assertEqual(studio["changes"], [])
        self.assertEqual([f["url"] for f in studio["formatting"]], [URL])
        self.assertEqual(self.record(), before)


class RemovedQuoteTests(DriftCase):
    def test_a_lesson_that_lost_a_quote_is_held_back_and_written_again(self):
        lesson, other = self.lesson(), self.lesson(OTHER)
        before = self.record()
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.assertEqual(self.read_due(), 2, "both pages are read again")
        self.assertEqual(self.record(), before, "the check writes nothing into the Record")

        # Classified: this lesson needs re-checking, the one from the other page does not.
        note = self.drift.lesson_note(lesson)
        self.assertEqual(note["state"], "recheck")
        self.assertEqual(note["removed"], [Q2])
        self.assertEqual(note["notice"], drift_mod.NOTICE.format(date=drift_mod._day(note["since"])))
        self.assertTrue(note["notice"].startswith("A source page changed on "))
        self.assertTrue(self.drift.withheld(lesson))
        self.assertFalse(self.drift.withheld(other))

        # Held back from the podcast feed, and written again first.
        self.assertIsNone(self.plan_row(S)["lesson"])
        self.assertIsNotNone(self.plan_row(OTHER)["lesson"])
        self.assertIsNone(self.app.state.podcast.episode_file(lesson))
        self.assertEqual(self.drift.rechecking(PID), {S})
        self.assertEqual(self.app.state.preparer.next_skill(), (PID, S))

        # Shown on the lesson, the course map, the skill page, in Studio and in diag.
        self.assertEqual(self.get(f"/api/lessons/{lesson}")["drift"]["state"], "recheck")
        skills = {s["id"]: s for d in self.get(f"/api/packages/{PID}")["domains"] for g in d["groups"] for s in g["skills"]}
        self.assertEqual(skills[S]["drift"]["state"], "recheck")
        self.assertEqual(skills[S]["drift"]["notice"], note["notice"])
        self.assertIsNone(skills[OTHER]["drift"])
        page = self.get(f"/api/skills/{PID}/{S}")
        self.assertEqual(page["drift"]["state"], "recheck")
        self.assertTrue(page["lessons"][0]["withheld"])
        studio = self.get("/api/studio/drift")
        change = studio["changes"][0]
        self.assertEqual((change["url"], change["kind"], change["removed"]), (URL, "changed", [Q2]))
        self.assertEqual([(x["id"], x["state"], x["now"]) for x in change["lessons"]], [(lesson, "recheck", "rechecking")])
        self.assertEqual(studio["rechecking"], 1)
        diag = self.get("/api/diag")["drift_check"]
        self.assertEqual((diag["lessons_rechecking"], diag["changes"]), (1, 1))

        # The Preparer writes it again from the page as it is now: author, gate, then narration.
        time.sleep(1.1)  # lessons are ordered by the second they were written in
        self.assertTrue(self.app.state.preparer.step())
        new = self.dojo.lessons_for(PID, S)[0]["id"]
        self.assertNotEqual(new, lesson)
        self.assertFalse(self.drift.withheld(new))
        self.assertFalse(self.dojo.lesson_audio_missing(new), "narrated, which is cheap and automatic")
        rewritten = self.drift.lesson_note(new)
        self.assertEqual((rewritten["state"], rewritten["replaces"]), ("rewritten", lesson))
        self.assertIsNone(rewritten["film"], "the old lesson had no video, so there is nothing to film")
        self.assertEqual(self.drift.lesson_note(lesson)["replaced_by"], new)
        self.assertEqual(self.plan_row(S)["lesson"]["id"], new, "the feed has the lesson again")
        self.assertEqual(self.drift.rechecking(PID), set())
        self.assertEqual(self.get("/api/studio/drift")["changes"][0]["lessons"][0]["now"], "rewritten")
        self.assertEqual(self.video_jobs(), [])

    def test_a_lesson_whose_quotes_are_all_still_there_holds(self):
        lesson = self.lesson()
        self.web.set(PAGE, GITHUB_MD.replace(Q3, NEW))   # lessons do not quote the last sentence
        self.read_due()
        note = self.drift.lesson_note(lesson)
        self.assertEqual(note["state"], "holds")
        self.assertFalse(self.drift.withheld(lesson))
        self.assertIsNotNone(self.plan_row(S)["lesson"])
        self.assertEqual(self.drift.rechecking(PID), set())
        change = self.get("/api/studio/drift")["changes"][0]
        self.assertEqual([(x["state"], x["now"]) for x in change["lessons"]], [("holds", "holds")])

    def test_a_page_that_changes_back_makes_the_lesson_hold_again(self):
        lesson = self.lesson()
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        self.assertTrue(self.drift.withheld(lesson))
        self.web.set(PAGE, GITHUB_MD)
        self.later(25)
        self.read_due()
        self.assertFalse(self.drift.withheld(lesson))

    def test_an_item_whose_quote_is_gone_is_held_back_and_the_record_stays(self):
        answered, waiting = self.item(), self.item()
        r = self.c.post(f"/api/items/{answered}/answer", json={"text": "r1 r2 everything"}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.wait_job(r.json()["id"])["state"], "done")
        lesson = self.lesson()
        self.web.set(PAGE, GITHUB_MD.replace(Q3, NEW))   # item rubrics quote the last sentence
        before = self.record()
        self.read_due()

        # Not answered yet: held back. Hints and answers are refused before anything is recorded.
        note = self.get(f"/api/items/{waiting}")["drift"]
        self.assertEqual(note["state"], "withheld")
        hint = self.c.post(f"/api/items/{waiting}/hint", headers=POST)
        self.assertEqual(hint.status_code, 409)
        self.assertEqual(hint.json()["detail"], note["notice"])
        refused = self.c.post(f"/api/items/{waiting}/answer", json={"text": "r1 r2"}, headers=POST)
        self.assertEqual(refused.status_code, 409)
        self.assertIsNone(self.dojo.item(self.owner, waiting)["answer"])

        # Answered before the change: the answer and its result stay; the skill page adds a line.
        done = self.get(f"/api/items/{answered}")
        self.assertEqual((done["drift"]["state"], done["drift"]["after_answer"]), ("changed", True))
        self.assertIsNotNone(done["result"])
        page = self.get(f"/api/skills/{PID}/{S}")
        attempt = next(a for a in page["evidence"]["attempts"] if a["item"] == answered)
        self.assertTrue(attempt["source_changed"]["after_answer"])
        self.assertIn("does not renew the proof", attempt["source_changed"]["notice"])
        self.assertEqual({i["id"]: (i["drift"] or {}).get("state") for i in page["items"]},
                         {answered: "changed", waiting: "withheld"})
        self.assertEqual(self.drift.lesson_state(lesson)["state"], "holds")

        # The Record is exactly as it was, and its chain still verifies.
        self.assertEqual(self.record(), before)
        self.assertTrue(self.get("/api/record/chain")["ok"])
        change = self.get("/api/studio/drift")["changes"][0]
        self.assertEqual(change["items"], {"holds": 0, "withheld": 1, "answered": 1})

        # A new item is written from the page as it is now, and may be answered.
        fresh = self.item()
        self.assertIsNone(self.get(f"/api/items/{fresh}")["drift"])

    def test_deleting_the_record_leaves_nothing_of_the_items_behind(self):
        waiting = self.item()
        self.web.set(PAGE, GITHUB_MD.replace(Q3, NEW))
        self.read_due()
        change = self.get("/api/studio/drift")["changes"][0]
        self.assertEqual((change["items"]["withheld"], change["removed"]), (1, [Q3]))

        r = self.c.post("/api/record/delete", json={"confirm": "DELETE"}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        change = self.get("/api/studio/drift")["changes"][0]
        self.assertEqual((change["items"], change["removed"]), ({"holds": 0, "withheld": 0, "answered": 0}, []))
        # What the check keeps is about pages and lessons only.
        kept = json.dumps(self.dojo.store.read("content", drift_mod.STATE))
        self.assertNotIn(waiting, kept)
        self.assertNotIn(Q3, kept)


class VersionTests(DriftCase):
    """How a change is dated when a page changes more than once. From the first read on, the snapshots
    take the test's clock, so every version has a time of its own."""

    def setUp(self):
        super().setUp()
        self.lesson_id = self.lesson()
        stamp = mock.patch("app.sources.iso", lambda dt=None: iso(dt or self.clock))
        stamp.start()
        self.addCleanup(stamp.stop)

    def version(self, text: str) -> str:
        self.later(1)
        self.web.set(PAGE, text)
        self.dojo.sources.recheck(URL)
        return iso(self.clock)

    def test_a_change_is_dated_from_when_the_words_last_moved_away(self):
        sources, lesson = self.dojo.sources, self.lesson_id
        written = sources.head(URL)["sha256"]
        first = self.version(GITHUB_MD.replace(Q2, NEW))
        self.assertEqual(sources.compare(URL, written), {"state": "changed", "since": first})
        self.version(FORMATTED)   # the words the lesson was written from, formatted another way
        self.assertEqual(sources.compare(URL, written)["state"], "formatting")
        self.assertIsNone(self.drift.lesson_state(lesson), "the words are as they were")
        again = self.version(GITHUB_MD.replace(Q1, NEW))
        self.assertEqual(sources.compare(URL, written), {"state": "changed", "since": again})
        self.assertEqual(self.drift.lesson_state(lesson)["since"], again, "not the first change, which was undone")
        self.version(GITHUB_MD)   # exactly the version the lesson was written from
        self.assertEqual(sources.compare(URL, written)["state"], "same")
        last = self.version(GITHUB_MD.replace(Q3, NEW))
        self.assertEqual(sources.compare(URL, written), {"state": "changed", "since": last})


class GoneTests(DriftCase):
    def test_a_page_not_found_twice_six_hours_apart_is_gone(self):
        lesson, item = self.lesson(), self.item()
        self.web.set(PAGE, "Not found", status=404)
        self.assertEqual(self.read_due(), 1)
        self.assertIsNone(self.dojo.sources.head(URL)["gone"], "one miss is not enough")
        self.assertFalse(self.drift.withheld(lesson))
        self.later(3)
        self.assertEqual(self.read_due(), 0, "a missing page is tried again after six hours, not before")
        self.later(4)
        self.assertEqual(self.read_due(), 1)
        self.assertTrue(self.dojo.sources.head(URL)["gone"])

        note = self.drift.lesson_note(lesson)
        self.assertEqual(note["state"], "recheck")
        self.assertEqual([p["url"] for p in note["gone"]], [URL])
        self.assertTrue(self.drift.withheld(lesson))
        self.assertEqual(self.drift.item_note(self.dojo.item(self.owner, item))["state"], "withheld")
        self.assertEqual(self.get("/api/studio/drift")["changes"][0]["kind"], "gone")
        with self.assertRaises(UserError):
            self.dojo.grounding(PID, S)   # nothing new is written from a page that is gone
        self.assertTrue(self.dojo.item_grounding(self.dojo.item(self.owner, item)), "an answer given earlier can still be judged")
        with self.assertRaises(UserError):
            self.lesson()

        # The page comes back as it was: nothing needs re-checking any more.
        self.web.set(PAGE, GITHUB_MD)
        self.later(25)
        self.read_due()
        self.assertFalse(self.dojo.sources.head(URL)["gone"])
        self.assertFalse(self.drift.withheld(lesson))


class MovedTests(DriftCase):
    """A cited address that now leads to another page has moved (ADR 0006). Nothing read there is stored
    as the cited page. A move counts as a page not found does: seen twice, six hours apart, the page is
    gone, and Studio says where it went. Only the language and a trailing slash may differ."""

    def test_a_page_that_leads_to_another_page_twice_six_hours_apart_has_moved(self):
        lesson, item = self.lesson(), self.item()
        sha = self.dojo.sources.head(URL)["sha256"]
        to = "https://docs.github.com" + ELSEWHERE
        self.web.set(PAGE, "", status=301, headers={"location": API + ELSEWHERE})
        self.assertEqual(self.read_due(), 1)
        page = self.drift._doc()["pages"][URL]
        self.assertEqual((page["outcome"], page["moved_to"], page["misses"]), ("moved", to, 1))
        head = self.dojo.sources.head(URL)
        self.assertEqual((head["gone"], head["sha256"], head["final"]), (None, sha, URL), "the other page is not stored as this one")
        self.assertFalse(self.drift.withheld(lesson), "one read is not enough")
        self.later(3)
        self.assertEqual(self.read_due(), 0, "a page that moved is read again after six hours, not before")
        self.later(4)
        self.assertEqual(self.read_due(), 1)
        head = self.dojo.sources.head(URL)
        self.assertEqual((head["gone"] is not None, head["moved"], head["sha256"]), (True, to, sha))

        note = self.drift.lesson_note(lesson)
        self.assertEqual(note["state"], "recheck")
        self.assertEqual([(p["url"], p["moved"]) for p in note["gone"]], [(URL, to)])
        self.assertEqual(self.get(f"/api/lessons/{lesson}")["drift"]["gone"][0]["moved"], to)
        self.assertTrue(self.drift.withheld(lesson))
        self.assertEqual(self.drift.item_note(self.dojo.item(self.owner, item))["state"], "withheld")
        studio = self.get("/api/studio/drift")
        self.assertEqual([(c["kind"], c["moved"]) for c in studio["changes"]], [("moved", to)])
        self.assertEqual((studio["pages"]["gone"], studio["pages"]["moved"]), (1, 1))
        block = self.get("/api/diag")["drift_check"]
        self.assertEqual((block["pages_gone"], block["pages_moved"]), (1, 1))
        with self.assertRaises(UserError):
            self.dojo.grounding(PID, S)   # nothing new is written from a page that moved away
        self.assertTrue(self.dojo.item_grounding(self.dojo.item(self.owner, item)), "an answer given earlier can still be judged")

        # The address leads to its own page again: it is no longer gone, and it did not move.
        self.web.set(PAGE, GITHUB_MD)
        self.later(25)
        self.assertEqual(self.read_due(), 1)
        head = self.dojo.sources.head(URL)
        self.assertEqual((head["gone"], head["moved"], head["sha256"]), (None, None, sha))
        self.assertNotIn("moved_to", self.drift._doc()["pages"][URL])
        self.assertFalse(self.drift.withheld(lesson))

    def test_a_trailing_slash_is_the_same_page(self):
        lesson = self.lesson()
        sha = self.dojo.sources.head(URL)["sha256"]
        self.web.set(PAGE, "", status=301, headers={"location": API + PAGE + "/"})
        self.assertEqual(self.read_due(), 1)
        self.assertEqual(self.web.count(PAGE + "/"), 1, "the redirect is followed")
        page = self.drift._doc()["pages"][URL]
        self.assertEqual(page["outcome"], "same")
        self.assertNotIn("misses", page)
        head = self.dojo.sources.head(URL)
        self.assertEqual((head["sha256"], head["final"], head["verified"], head["gone"]), (sha, URL, True, None))
        self.assertFalse(self.drift.withheld(lesson))

    def test_the_same_page_in_another_language_is_not_a_move(self):
        lesson = self.lesson()
        japanese = PAGE.replace("/en/", "/ja/", 1)
        self.web.set(PAGE, "", status=301, headers={"location": API + japanese})
        for _ in range(2):
            self.assertEqual(self.read_due(), 1)
            page = self.drift._doc()["pages"][URL]
            # Dojo reads documentation in English only, so the page cannot be read today. It did not move.
            self.assertEqual(page["outcome"], "failed")
            self.assertNotIn("misses", page)
            self.later(25)
        self.assertEqual(self.web.count(japanese), 0, "a page in another language is not read")
        self.assertIsNone(self.dojo.sources.head(URL)["gone"])
        self.assertFalse(self.drift.withheld(lesson))


class SamePageTests(DriftCase):
    """Where a read of a cited address may end: at that page, in any language, with or without a trailing
    slash. Anywhere else the address has moved, and nothing read there is stored under it."""

    A = "/en-us/sql/t-sql/data-types/json-data-type"

    def test_only_the_language_and_a_trailing_slash_may_differ(self):
        page, docs = LEARN + self.A, "https://docs.github.com/en/copilot/concepts/agents"
        for same in (page + "/", page.replace("/en-us/", "/de-de/"), page.replace("/en-us/", "/zh-hant-tw/"),
                     page.replace("/en-us", ""), page + "?tabs=azure-cli", docs.replace("/en/", "/ja/"),
                     API + "/en/copilot/concepts/agents", docs + "/"):
            with self.subTest(same=same):
                self.assertTrue(same_page(page if same.startswith(LEARN) else docs, same))
        for other in (page.replace("json-data-type", "JSON-data-type"), page + "?view=sql-server-ver17",
                      LEARN + "/en-us/sql/t-sql/data-types/xml-data-type", LEARN + "/en-us/training/modules/json/",
                      "https://example.com" + self.A, page.replace("https:", "http:"), "https://docs.github.com" + self.A,
                      "", None):
            with self.subTest(other=other):
                self.assertFalse(same_page(page, other))
        self.assertFalse(same_page(LEARN + "/en-us/training/x", LEARN + "/en-us/training/x"), "only documentation is a page")
        self.assertTrue(same_page(page + "?view=a", page.replace("/en-us/", "/fr-fr/") + "/?view=a&tabs=b"))

    def test_a_read_that_ends_at_another_page_stores_nothing_from_there(self):
        src, page = self.dojo.sources, LEARN + self.A
        src.get(page)
        before = src.head(page)
        self.assertEqual((before["final"], before["verified"]), (page, True))
        for to, outcome, where in (
                (LEARN + "/en-us/sql/t-sql/data-types/xml-data-type", "moved", LEARN + "/en-us/sql/t-sql/data-types/xml-data-type"),
                ("/en-us/training/modules/json/", "moved", LEARN + "/en-us/training/modules/json/"),
                ("https://example.com/json", "moved", "https://example.com/json"),
                (self.A.replace("/en-us/", "/de-de/"), "failed", None)):
            with self.subTest(to=to):
                self.web.set(self.A, "", status=301, headers={"location": to})
                got = src.recheck(page)
                self.assertEqual((got["outcome"], got.get("to")), (outcome, where))
                self.assertEqual(src.head(page), before, "nothing is stored from where it led")
        self.assertNotIn("/en-us/training/modules/json/", self.web.learn)
        self.assertNotIn(self.A.replace("/en-us/", "/de-de/"), self.web.learn)
        self.assertNotIn("example.com", self.web.hosts)
        # Asked for the page itself, Dojo gives its last copy of it, never the page it now leads to.
        self.web.set(self.A, "", status=301, headers={"location": "/en-us/sql/t-sql/data-types/xml-data-type"})
        got = src.get(page, force=True)
        self.assertEqual((got["stale"], got["final"], got["sha256"]), (True, page, before["sha256"]))
        # A trailing slash is the same page: the read counts.
        self.web.set(self.A, "", status=301, headers={"location": self.A + "/"})
        self.assertEqual(src.recheck(page)["outcome"], "same")
        self.assertEqual(src.head(page)["final"], page)

    def test_a_page_never_read_that_leads_elsewhere_is_not_stored(self):
        src, page = self.dojo.sources, LEARN + self.A
        self.web.set(self.A, "", status=302, headers={"location": "/en-us/sql/t-sql/data-types/xml-data-type"})
        with self.assertRaisesRegex(UserError, "now leads to another page"):
            src.get(page)
        self.assertIsNone(src.head(page))
        self.assertEqual(src.recheck(page), {"outcome": "moved", "to": LEARN + "/en-us/sql/t-sql/data-types/xml-data-type"})
        self.assertIsNone(src.head(page))


class PoliteTests(DriftCase):
    def test_a_site_that_asks_to_slow_down_is_left_alone_as_long_as_it_asks(self):
        lesson = self.lesson()
        self.web.set(PAGE, "Slow down", status=429, headers={"Retry-After": "7200"})
        self.assertEqual(self.read_due(), 1)
        self.assertIn("docs.github.com", self.get("/api/diag")["drift_check"]["hosts_waiting"])
        self.assertFalse(self.drift.withheld(lesson), "a refusal says nothing about the page")
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.later(1.5)
        self.assertEqual(self.read_due(), 0)
        self.assertEqual(self.web.count(), 2)
        self.later(0.6)
        self.assertEqual(self.read_due(), 1)
        self.assertTrue(self.drift.withheld(lesson))

    def test_the_wait_is_at_least_an_hour_and_at_most_a_day(self):
        now = self.clock
        for asked, hours in ((5, 1), (10**7, 24)):
            self.drift._record(URL, {"outcome": "limited", "status": 429, "retry_after": asked}, now)
            self.assertEqual(self.drift._doc()["hosts"]["docs.github.com"], iso(now + timedelta(hours=hours)))

    def test_a_failing_site_is_also_left_alone(self):
        self.lesson()
        self.web.set(PAGE, "Unavailable", status=503)
        self.read_due()
        self.assertEqual(self.drift._doc()["pages"][URL]["outcome"], "limited")
        self.assertIn("docs.github.com", self.drift._doc()["hosts"])

    def test_a_redirect_to_another_site_is_not_followed(self):
        lesson = self.lesson()
        self.web.set(PAGE, "", status=302, headers={"location": "https://example.com/elsewhere"})
        self.read_due()
        page = self.drift._doc()["pages"][URL]
        # The cited page now leads elsewhere: it moved, and one such read is not enough to hold anything.
        self.assertEqual((page["outcome"], page["moved_to"]), ("moved", "https://example.com/elsewhere"))
        self.assertNotIn("example.com", self.web.hosts)
        self.assertFalse(self.drift.withheld(lesson))

    def test_reads_are_spread_out(self):
        self.lesson(), self.lesson(OTHER)
        with mock.patch.object(drift_mod, "FETCH_GAP_S", 600):
            self.drift.step()
            self.assertEqual(self.drift._doc()["read"], 1)
            self.drift.step()
            self.assertEqual(self.drift._doc()["read"], 1, "the second read waits ten minutes")
            self.later(0.2)
            self.drift.step()
            self.assertEqual(self.drift._doc()["read"], 2)

    def test_the_daily_limit_holds_across_a_restart(self):
        self.lesson(), self.lesson(OTHER)
        with mock.patch.object(drift_mod, "DAILY_FETCHES", 1):
            self.assertEqual(self.read_due(), 1)
            self.boot()
            self.assertEqual(self.read_due(), 0, "a restart does not hand out a fresh allowance")
            self.assertEqual(len(self.web.reads), 3)   # two first reads for the lessons, one check
            self.later(24)
            self.assertEqual(self.read_due(), 1, "the next day has its own allowance")


class RestartTests(DriftCase):
    def test_a_restart_after_the_read_sorts_the_change_out_without_reading_again(self):
        lesson = self.lesson()
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        # The step is cut off after the page was read and stored, before the change was sorted out.
        self.assertTrue(self.drift._charge(URL, self.clock))
        self.assertEqual(self.dojo.sources.recheck(URL)["outcome"], "changed")
        reads = self.web.count()
        self.boot()
        self.assertEqual(self.read_due(), 0)
        self.assertEqual(self.web.count(), reads, "the page is not read again")
        self.assertEqual(self.drift._doc()["read"], 1, "and the read is not handed back")
        self.assertTrue(self.drift.withheld(lesson))
        self.assertEqual(len(self.get("/api/studio/drift")["changes"]), 1)

    def test_a_step_is_cut_off_before_the_page_is_stored(self):
        lesson = self.lesson()
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.assertTrue(self.drift._charge(URL, self.clock))
        self.boot()
        self.assertEqual(self.read_due(), 0, "the charged page waits for its next turn")
        self.assertFalse(self.drift.withheld(lesson))
        self.later(24)
        self.assertEqual(self.read_due(), 1)
        self.assertTrue(self.drift.withheld(lesson))

    def test_a_page_stored_while_its_head_is_first_read_is_not_undone(self):
        lesson = self.lesson()
        sources = self.dojo.sources
        written = sources.head(URL)["sha256"]
        sources._heads.clear()   # as just after a restart
        real = sources._head_of

        def meanwhile(snap: dict) -> dict:
            # While a request reads the page's head for the first time, the Preparer stores a new version.
            head = real(snap)
            if snap["sha256"] == written:
                self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
                self.assertEqual(sources.recheck(URL)["outcome"], "changed")
            return head

        with mock.patch.object(sources, "_head_of", side_effect=meanwhile):
            sources.head(URL)
        self.assertNotEqual(sources.head(URL)["sha256"], written, "the newer version stays")
        self.assertEqual(sources.compare(URL, written)["state"], "changed")
        self.assertTrue(self.drift.withheld(lesson))


class PoolAndCheckTests(DriftCase):
    def test_questions_whose_quote_is_gone_leave_the_pool_the_exam_and_the_check(self):
        room, checks = self.app.state.exams, self.app.state.checks
        self.dojo.store.path("learners", self.owner.key).mkdir(parents=True, exist_ok=True)
        questions, lost = self.dojo.write_mcqs(PID, [S, OTHER])
        self.assertEqual((len(questions), lost), (2, []))
        mine = next(q for q in questions if q["skill"] == S)
        room._write_pool(self.owner, PID, questions)
        pkg = self.dojo.packages.get(PID)

        def ready_exam(items: list[dict]) -> str:
            doc = room.create(self.owner, PID, "short")
            doc.update(items=items, state="ready", new_questions=len(items), minutes=spec(pkg, "short", len(items))["minutes"])
            room._save(self.owner, doc)
            return doc["id"]

        both, only_mine = ready_exam(questions), ready_exam([mine])
        check_item = self.item(mode="check")
        check = {"id": new_id("check"), "package": PID, "created": iso(), "minutes": 3, "state": "running", "ended": None,
                 "questions": [{"skill": S, "text": "Skill", "domain": "d1", "kind": "explanation", "weight": 1.0,
                                "picked": "never_tried", "why": "", "state": "ready", "item": check_item, "audio": "",
                                "note": "", "job": None}]}
        checks._save(self.owner, check)
        self.assertIsNotNone(checks.open_session(self.owner, PID))

        self.web.set(PAGE, GITHUB_MD.replace(Q0, NEW))   # every question quotes the first sentence
        self.read_due()

        self.assertEqual([q["skill"] for q in room.pool(self.owner, PID)], [OTHER])
        change = self.get("/api/studio/drift")["changes"][0]
        self.assertEqual(change["pool"], {"holds": 0, "withheld": 1})

        shown = checks.view(self.owner, check["id"])["questions"][0]
        self.assertEqual((shown["state"], shown["stem"], shown["audio"]), ("skipped", "", ""))
        self.assertTrue(shown["note"].startswith("A source page changed on "))
        self.assertIsNone(checks.open_session(self.owner, PID), "nothing is left to answer")

        brief = self.get(f"/api/exams/{both}")
        self.assertEqual((brief["questions"], brief["source_waiting"]), (2, 1), "the brief says what the start leaves out")
        refused = self.c.post(f"/api/exams/{only_mine}/start", headers=POST)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("Build a new practice exam", refused.json()["detail"])
        started = self.c.post(f"/api/exams/{both}/start", headers=POST)
        self.assertEqual(started.status_code, 200, started.text)
        exam = started.json()
        self.assertEqual((exam["questions"], exam["source_dropped"], exam["source_waiting"]), (1, 1, 0))
        export = for_export(self.dojo.store.read("learners", self.owner.key, "exams", both))
        self.assertEqual(export["source_dropped"], 1)
        self.assertIsInstance(export["withheld"], str, "the export's own note keeps its name")
        self.assertEqual(exam["minutes"], spec(pkg, "short", 1)["minutes"])
        self.assertEqual(self.c.get("/api/studio/drift").status_code, 409, "closed while a timed exam runs")


class FilmTests(DriftCase):
    def rewritten_after_filming(self) -> tuple[str, str]:
        """A filmed lesson whose page loses a quote, and the lesson written again after it."""
        self.dojo.store.write("content", "prices", value={**PRICES, "fetched": iso()})
        lesson = self.lesson()
        self.dojo.lesson_video(lambda step: None, self.owner, lesson)
        self.assertTrue(self.drift._filmed(lesson))
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        change = self.drift.summary()["changes"][0]
        self.assertEqual([(x["id"], x["filmed"]) for x in change["lessons"]], [(lesson, True)])

        stub = self.dojo.ai._lesson

        def said_differently(user: str) -> dict:
            doc = stub(user)
            doc["slides"][0]["narration"][0]["text"] = "Here is the idea, as the page puts it now."
            return doc

        self.dojo.ai._lesson = said_differently
        time.sleep(1.1)  # lessons are ordered by the second they were written in
        self.assertTrue(self.app.state.preparer.step())
        new = self.dojo.lessons_for(PID, S)[0]["id"]
        self.assertNotEqual(new, lesson)
        return lesson, new

    def test_nothing_is_filmed_until_the_owner_presses_the_button(self):
        lesson, new = self.rewritten_after_filming()
        for _ in range(3):
            self.drift.step()
            self.drift.summary()
        self.assertEqual(self.video_jobs(), [], "no film without the button")
        self.assertFalse(self.drift._filmed(new))
        self.assertIsNone(self.drift._film_thread)

        film = self.get(f"/api/lessons/{new}")["drift"]["film"]
        self.assertEqual((film["state"], film["text"]), ("waiting", drift_mod.FILM_WAITING))
        refilm = self.get("/api/studio/drift")["refilm"]
        self.assertEqual([x["id"] for x in refilm["lessons"]], [new])
        self.assertEqual(refilm["estimate"]["lessons"], 1)
        self.assertGreater(refilm["estimate"]["usd"], 0)
        self.assertGreater(refilm["estimate"]["minutes"], 0)

        self.assertEqual(self.c.post("/api/studio/drift/refilm", json={"lessons": [new]}).status_code, 403)
        self.assertEqual(self.c.post("/api/studio/drift/refilm", json={"lessons": ["not an id"]}, headers=POST).status_code, 422)
        self.assertEqual(self.c.post("/api/studio/drift/refilm", json={"lessons": [lesson]}, headers=POST).status_code, 409)
        self.assertEqual(self.video_jobs(), [])

        with mock.patch.object(drift_mod, "FILM_WAIT_S", 0.05):
            r = self.c.post("/api/studio/drift/refilm", json={"lessons": [new]}, headers=POST)
            self.assertEqual(r.status_code, 200, r.text)
            jobs = self.video_jobs()
            self.assertEqual([(j["ref"], j["learner"]) for j in jobs], [(new, self.owner.key)], "one film job, through the ordinary path")
            self.assertEqual(self.wait_job(jobs[0]["id"])["state"], "done")
            for _ in range(100):
                if (self.drift._doc()["films"].get(new) or {}).get("state") == "done":
                    break
                time.sleep(0.05)
        self.assertEqual(self.drift._doc()["films"][new]["state"], "done")
        self.assertTrue(self.drift._filmed(new))
        self.assertEqual(self.drift.lesson_note(new)["film"]["state"], "done")
        studio = self.get("/api/studio/drift")["refilm"]
        self.assertEqual(studio["lessons"], [])
        self.assertEqual(studio["approvals"][0]["lessons"], [new])
        self.assertGreater(studio["approvals"][0]["usd"], 0)
        self.assertEqual(self.c.post("/api/studio/drift/refilm", json={"lessons": [new]}, headers=POST).status_code, 409)
        self.assertEqual(len(self.video_jobs()), 1)

    def test_a_film_cut_off_by_a_restart_is_filed_once_more_then_given_up(self):
        _, new = self.rewritten_after_filming()
        cut = new_id("job")
        self.dojo.store.write("jobs", cut, value={"id": cut, "kind": "video", "learner": self.owner.key, "ref": new,
                                                  "state": "interrupted", "created": iso(), "error": "Dojo restarted."})
        self.drift._set_film(new, state="filed", job=cut, tries=1, approved=iso(), title="t", package=PID, skill=S,
                             replaces="", error=None)
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_film(progress, learner, lesson_id):
            release.wait(20)
            raise UserError("stopped by the test")

        self.dojo.lesson_video = slow_film
        self.drift._file_films()
        film = self.drift._doc()["films"][new]
        self.assertEqual((film["state"], film["tries"]), ("filed", 2))
        self.assertNotEqual(film["job"], cut)

        self.boot()   # the second film is cut off too: its job is marked interrupted
        self.drift._file_films()
        film = self.drift._doc()["films"][new]
        self.assertEqual(film["state"], "failed")
        self.assertEqual(len(self.video_jobs()), 2, "not filed a third time")
        waiting = self.drift.refilm_candidates()
        self.assertEqual([x["id"] for x in waiting], [new], "the owner can press the button again")
        self.assertTrue(waiting[0]["error"])

    def test_the_refilm_price_says_how_old_its_list_prices_are(self):
        # As every other total does (ADR 0005): when the list was read, or that it could not be refreshed.
        _, new = self.rewritten_after_filming()
        read = self.dojo.store.read("content", "prices")["fetched"]
        est = self.get("/api/studio/drift")["refilm"]["estimate"]
        self.assertEqual((est["fetched"], est["stale"]), (read, False))
        old = iso(utcnow() - timedelta(days=2))
        self.dojo.store.write("content", "prices", value={**PRICES, "fetched": old})   # and the refresh fails
        est = self.get("/api/studio/drift")["refilm"]["estimate"]
        self.assertEqual((est["fetched"], est["stale"]), (old, True))
        self.assertGreater(est["usd"], 0, "the last list still prices it, and says how old it is")
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text("utf-8")
        panel = script.split("function refilmPanel(d, paint) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('h("div", { class: "actions top refilm" }, btn), pricesAge(est),', panel, "right under the button")


class LabTests(DriftCase):
    """The labs cite official pages too, but have no quotes to look for. A change in a lab page's words, or
    the page going away, flags the lab for a person to look at until the owner says they did."""
    labs = LABS

    def flagged(self) -> list[str]:
        return [x["id"] for x in self.get("/api/studio/drift")["labs"]]

    def change(self, url: str) -> dict:
        return next(c for c in self.get("/api/studio/drift")["changes"] if c["url"] == url)

    def test_the_app_watches_the_pages_its_labs_cite(self):
        self.assertIs(drift_mod.DriftCheck(self.dojo, self.owner).labs, LABS)
        cited = self.drift.cited()
        self.assertTrue({VECTOR_SOURCE, DISTANCE_SOURCE, JSON_SOURCE} <= set(cited))

    def test_a_lab_page_is_read_once_to_start_from_and_flags_nothing(self):
        before = self.record()
        self.assertEqual(self.read_due(), 3, "each page the labs cite, once")
        self.assertEqual(sorted(self.web.learn), sorted([VECTOR_PATH, DISTANCE_PATH, JSON_PATH]))
        self.later(2)
        self.assertEqual(self.read_due(), 0, "and not again the same day")
        watched = self.drift._doc()["labs"]
        self.assertEqual(sorted(watched), ["hybrid", "search", "vector"])
        self.assertEqual(sorted(watched["hybrid"]["pages"]), sorted([DISTANCE_SOURCE, JSON_SOURCE]))
        studio = self.get("/api/studio/drift")
        self.assertEqual((studio["changes"], studio["labs"]), ([], []))
        self.assertEqual(self.get("/api/diag")["drift_check"]["labs_flagged"], 0)
        self.assertEqual(self.record(), before)
        self.assertIsNone(self.get("/api/labs/vector")["drift"])

    def test_a_change_in_words_flags_the_lab_until_the_owner_has_looked(self):
        self.read_due()
        self.web.set(VECTOR_PATH, LEARN_CHANGED)
        self.web.set(JSON_PATH, LEARN_FORMATTED)
        self.later(25)
        before = self.record()
        self.assertEqual(self.read_due(), 3)
        self.assertEqual(self.flagged(), ["vector"], "a change in formatting only flags nothing")
        change = self.change(VECTOR_SOURCE)
        self.assertEqual((change["kind"], change["title"], change["publisher"]), ("changed", "Vector data type", "Microsoft Learn"))
        self.assertEqual([(x["id"], x["now"]) for x in change["labs"]], [("vector", "flagged")])
        self.assertEqual((change["lessons"], change["removed"]), ([], []))
        self.assertEqual([f["url"] for f in self.get("/api/studio/drift")["formatting"]], [JSON_SOURCE])
        self.assertEqual(self.get("/api/diag")["drift_check"]["labs_flagged"], 1)
        self.assertEqual(self.video_jobs(), [], "a flagged lab starts no film and no other work")
        self.assertEqual(self.record(), before, "the check writes nothing to the record")
        self.boot()   # a restart keeps the flag
        self.assertEqual(self.flagged(), ["vector"])

        lab = self.get("/api/labs/vector")
        self.assertEqual(lab["drift"]["state"], "recheck")
        self.assertTrue(lab["drift"]["notice"].startswith(f'A source page changed on {lab["drift"]["date"]}. '))
        self.assertIn("flagged this lab for a look", lab["drift"]["notice"])
        self.assertEqual(([p["url"] for p in lab["drift"]["pages"]], lab["drift"]["gone"]), ([VECTOR_SOURCE], []))
        self.assertIsNone(self.get("/api/labs/hybrid")["drift"])
        run = self.c.post("/api/labs/vector/run", json={"sql": "SELECT 1"}, headers=POST)
        self.assertEqual(run.status_code, 200, "the lab stays open: it is practice and fills no pip")

        self.assertEqual(self.c.post("/api/studio/drift/labs/vector/looked").status_code, 403)
        self.assertEqual(self.c.post("/api/studio/drift/labs/nope/looked", headers=POST).status_code, 404)
        before = self.record()
        looked = self.c.post("/api/studio/drift/labs/vector/looked", headers=POST)
        self.assertEqual(looked.status_code, 200, looked.text)
        self.assertEqual(self.record(), before, "saying so writes nothing to the record")
        self.assertEqual(looked.json()["labs"], [])
        self.assertEqual([x["now"] for x in self.change(VECTOR_SOURCE)["labs"]], ["looked"])
        self.assertEqual(self.get("/api/diag")["drift_check"]["labs_flagged"], 0)
        self.assertIsNone(self.get("/api/labs/vector")["drift"])
        self.assertEqual(self.c.post("/api/studio/drift/labs/vector/looked", headers=POST).status_code, 409)

        self.web.set(VECTOR_PATH, LEARN_CHANGED.replace("columnar data format", "column-based data format"))
        self.later(25)
        self.read_due()
        self.assertEqual(self.flagged(), ["vector"], "the next change flags it again")

    def test_a_lab_page_that_is_gone_flags_every_lab_that_cites_it(self):
        self.read_due()
        self.web.set(DISTANCE_PATH, status=404)
        self.later(25)
        self.read_due()
        self.assertEqual(self.flagged(), [], "one 'not found' is not enough")
        self.later(drift_mod.GONE_AFTER_HOURS + 1)
        self.read_due()
        self.assertEqual(self.flagged(), ["search", "hybrid"])
        self.assertEqual([p["url"] for p in self.get("/api/labs/search")["drift"]["gone"]], [DISTANCE_SOURCE])
        change = self.change(DISTANCE_SOURCE)
        self.assertEqual((change["kind"], sorted(x["id"] for x in change["labs"])), ("gone", ["hybrid", "search"]))

        self.assertEqual(self.c.post("/api/studio/drift/labs/search/looked", headers=POST).status_code, 200)
        self.assertEqual(self.flagged(), ["hybrid"], "each lab is looked at on its own")
        self.later(25)
        self.read_due()
        self.assertEqual(self.flagged(), ["hybrid"], "the page is still gone, and search was looked at since")

    def test_a_lab_page_never_found_is_tried_sparingly_and_watched_once_it_is_there(self):
        self.web.set(VECTOR_PATH, status=404)
        self.read_due()
        self.later(2)
        self.read_due()
        self.later(7)
        self.read_due()
        self.later(2)
        self.read_due()
        self.assertEqual(self.web.learn.count(VECTOR_PATH), 2, "not found, tried 6 hours later, then left for a day")
        self.assertNotIn("vector", self.drift._doc()["labs"], "no version to start from")
        studio = self.get("/api/studio/drift")
        self.assertEqual((studio["changes"], studio["labs"]), ([], []), "a page never read cannot have changed")
        self.assertIsNone(self.get("/api/labs/vector")["drift"])

        self.web.set(VECTOR_PATH, LEARN_HTML)
        self.later(25)
        self.read_due()
        self.assertEqual(self.flagged(), [], "the first version found is where the lab is watched from")
        self.assertEqual(list(self.drift._doc()["labs"]["vector"]["pages"]), [VECTOR_SOURCE])
        self.web.set(VECTOR_PATH, LEARN_CHANGED)
        self.later(25)
        self.read_due()
        self.assertEqual(self.flagged(), ["vector"])


def two_questions(case: DriftCase) -> tuple[dict, dict]:
    """A practice question from the page the tests change, and one from a page that stays."""
    case.dojo.store.path("learners", case.owner.key).mkdir(parents=True, exist_ok=True)
    questions, lost = case.dojo.write_mcqs(PID, [S, OTHER])
    case.assertEqual(lost, [])
    return next(q for q in questions if q["skill"] == S), next(q for q in questions if q["skill"] == OTHER)


class QuickPracticeTests(DriftCase):
    """A quick practice saved before its page changed must not show a key the page no longer supports."""

    def test_an_unanswered_question_whose_quote_is_gone_is_held_and_shows_no_key(self):
        mine, other = two_questions(self)
        rid = new_id("reh")
        self.dojo.store.write("learners", self.owner.key, "rehearsals", rid, value={
            "id": rid, "package": PID, "created": iso(), "requested": 3, "dropped": 0, "answers": {}, "models": {},
            "items": [mine, {**mine, "stem": mine["stem"] + " (again)"}, other]})
        first = self.c.post(f"/api/rehearsals/{rid}/answer", json={"index": 0, "choice": mine["answer"]}, headers=POST)
        self.assertEqual(first.status_code, 200, first.text)

        self.web.set(PAGE, GITHUB_MD.replace(Q0, NEW))   # every question quotes the first sentence
        self.read_due()
        record = self.record()
        saved = self.dojo.store.read("learners", self.owner.key, "rehearsals", rid)

        view = self.get(f"/api/rehearsals/{rid}")
        held = view["items"][1]
        self.assertEqual(held["drift"]["state"], "withheld")
        self.assertEqual(held["drift"]["notice"], drift_mod.ITEM_NOTICE.format(date=held["drift"]["date"]))
        self.assertFalse({"answer", "rationale", "quote", "choice", "correct"} & held.keys(), "no key for a held question")
        refused = self.c.post(f"/api/rehearsals/{rid}/answer", json={"index": 1, "choice": mine["answer"]}, headers=POST)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["detail"], held["drift"]["notice"])
        self.assertEqual(self.record(), record, "nothing goes into the Record")
        self.assertEqual(self.dojo.store.read("learners", self.owner.key, "rehearsals", rid), saved, "nothing is saved")

        answered = view["items"][0]
        self.assertNotIn("drift", answered, "a question answered before the change stays as it was")
        self.assertEqual((answered["choice"], answered["answer"], answered["quote"]), (mine["answer"], mine["answer"], mine["quote"]))
        self.assertNotIn("drift", view["items"][2])
        ok = self.c.post(f"/api/rehearsals/{rid}/answer", json={"index": 2, "choice": other["answer"]}, headers=POST)
        self.assertEqual(ok.status_code, 200, "a question whose page did not change is answered as before")
        view = self.get(f"/api/rehearsals/{rid}")
        self.assertEqual((view["answered"], view["correct"], view["held"]), (2, 2, 1))
        listed = next(r for r in self.get(f"/api/rehearsals?package={PID}") if r["id"] == rid)
        self.assertEqual((listed["items"], listed["answered"], listed["held"]), (3, 2, 1), "nothing is left to answer")

        self.web.set(PAGE, GITHUB_MD)   # the page changes back, and the question holds again
        self.later(25)
        self.read_due()
        self.assertNotIn("drift", self.get(f"/api/rehearsals/{rid}")["items"][1])

    def test_the_export_leaves_out_the_key_of_a_held_question(self):
        mine, other = two_questions(self)
        rid = new_id("reh")
        self.dojo.store.write("learners", self.owner.key, "rehearsals", rid, value={
            "id": rid, "package": PID, "created": iso(), "requested": 3, "dropped": 0, "answers": {}, "models": {},
            "items": [mine, {**mine, "stem": mine["stem"] + " (again)"}, other]})
        first = self.c.post(f"/api/rehearsals/{rid}/answer", json={"index": 0, "choice": mine["answer"]}, headers=POST)
        self.assertEqual(first.status_code, 200, first.text)
        saved = self.dojo.store.read("learners", self.owner.key, "rehearsals", rid)

        def exported() -> dict:
            return next(r for r in self.get("/api/record/export")["rehearsals"] if r["id"] == rid)

        self.assertEqual(exported(), saved, "while the page holds, the export is the quick practice as saved")

        self.web.set(PAGE, GITHUB_MD.replace(Q0, NEW))   # every question of this skill quotes the first sentence
        self.read_due()
        out = exported()
        held = out["items"][1]
        self.assertFalse({"answer", "rationale", "quote"} & held.keys(), "no key for a held question")
        self.assertEqual({k: held[k] for k in ("skill", "stem", "options", "source")},
                         {k: saved["items"][1][k] for k in ("skill", "stem", "options", "source")})
        self.assertEqual(held["drift"]["notice"], drift_mod.ITEM_NOTICE.format(date=held["drift"]["date"]))
        self.assertEqual(out["items"][0], saved["items"][0], "a question answered before the change keeps everything")
        self.assertEqual(out["items"][2], saved["items"][2], "a question whose page did not change is as saved")
        self.assertEqual(out["answers"], saved["answers"])
        self.assertEqual(out["withheld"], rehearsal_held_note(1))
        self.assertTrue(out["withheld"].startswith("1 question not answered yet is held back"), out["withheld"])
        self.assertEqual(self.dojo.store.read("learners", self.owner.key, "rehearsals", rid), saved,
                         "the export changes nothing that is stored")

        self.web.set(PAGE, GITHUB_MD)   # the page changes back, and the question holds again
        self.later(25)
        self.read_due()
        self.assertEqual(exported(), saved)


class ExamWithdrawalTests(DriftCase):
    """A page changes while a practice exam runs. The exam runs on as it was. When it closes, a question
    whose quote is gone is withdrawn: not scored, not counted, and its key is not shown."""

    def start_exam(self, items: list[dict]) -> str:
        """A started exam with these questions, written by hand as an attempt built before each question
        said whether it was new."""
        room = self.app.state.exams
        doc = room.create(self.owner, PID, "short")
        doc.update(items=items, state="ready", new_questions=len(items),
                   minutes=spec(self.dojo.packages.get(PID), "short", len(items))["minutes"])
        room._save(self.owner, doc)
        started = self.c.post(f"/api/exams/{doc['id']}/start", headers=POST)
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["questions"], len(items))
        return doc["id"]

    def running_exam(self, others: int, every_domain: bool = False) -> tuple[str, list[dict]]:
        """A started exam: a question from the page that changes, then `others` from a page that stays, all
        in the first domain. With `every_domain`, five more in each other domain, so the exam can count."""
        mine, other = two_questions(self)
        items = [mine] + [{**other, "stem": f"{other['stem']} ({k + 1})"} for k in range(others)]
        items += self.other_domains() if every_domain else []
        return self.start_exam(items), items

    def other_domains(self) -> list[dict]:
        """Five questions in each domain but the first, from pages that stay. Rule 2 judges every domain of
        the blueprint, so only an exam with five scored questions in each can count."""
        pkg = self.dojo.packages.get(PID)
        first = pkg["skills"][S]["domain"]
        skills = [next(sid for g in d["groups"] for sid in g["skills"]
                       if pkg["skills"][sid]["sources"] and URL not in {s["url"] for s in pkg["skills"][sid]["sources"]})
                  for d in pkg["domains"] if d["id"] != first]
        questions, lost = self.dojo.write_mcqs(PID, skills)
        self.assertEqual((len(questions), lost), (len(skills), []))
        return [{**q, "stem": f"{q['stem']} ({q['skill']}, {k + 1})"} for q in questions for k in range(5)]

    def sit(self, aid: str, items: list[dict], page: str) -> dict:
        """The page changes before any answer; every question is still answered, right, then the exam is handed in."""
        self.web.set(PAGE, page)
        self.read_due()
        for i, it in enumerate(items):
            r = self.c.post(f"/api/exams/{aid}/answer", json={"index": i, "choice": it["answer"]}, headers=POST)
            self.assertEqual(r.status_code, 200, "no answer is refused while the exam runs")
        closed = self.c.post(f"/api/exams/{aid}/submit", json={}, headers=POST)
        self.assertEqual(closed.status_code, 200, closed.text)
        return closed.json()

    def rule_two(self) -> dict:
        advice = self.get(f"/api/readiness?package={PID}")
        return {"rule": next(r for r in advice["rules"] if r["id"] == "rule2"), "reasons": advice["reasons"]}

    def built_exam(self, seen_before: str, start: bool = True) -> tuple[str, list[dict]]:
        """A full exam built the usual way: ten questions in the first domain, one from the page that changes
        (`mine`) and nine from a page that stays, and five in each other domain. Four of the 35 were seen
        before: `mine` and three of the nine, or four of the nine. So 31 are new either way."""
        mine, other = two_questions(self)
        others = [{**other, "stem": f"{other['stem']} ({k + 1})"} for k in range(9)]
        before = [mine] + others[:3] if seen_before == "mine" else others[:4]
        mark_seen(self.dojo.store, self.owner.key, PID, [question_key(q["stem"]) for q in before])
        written = [mine] + others + self.other_domains()
        room = self.app.state.exams
        aid = room.create(self.owner, PID, "full")["id"]
        with mock.patch.object(self.dojo, "write_mcqs", lambda pid, skills, on_progress=None: (written, [])):
            built = room.build(lambda _: None, self.owner, aid)
        self.assertEqual((built["questions"], built["new_questions"]), (35, 31))
        items = room.attempt(self.owner, aid)["items"]
        seen = {q["stem"] for q in before}
        self.assertEqual({it["stem"]: it["new"] for it in items},
                         {it["stem"]: it["stem"] not in seen for it in written}, "each question says whether it was new")
        if start:
            started = self.c.post(f"/api/exams/{aid}/start", headers=POST)
            self.assertEqual(started.status_code, 200, started.text)
            items = room.attempt(self.owner, aid)["items"]
        return aid, items

    def shown_event(self, aid: str) -> dict:
        shown = self.c.post(f"/api/exams/{aid}/rationales", headers=POST)
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertTrue(shown.json()["rationales_shown"])
        events = [e["data"] for e in self.dojo.store.events(self.owner.key)
                  if e["type"] == "exam.rationales_shown" and e["data"]["attempt"] == aid]
        self.assertEqual(len(events), 1)
        return events[0]

    def last_teaching(self, sid: str) -> str | None:
        return self.get(f"/api/skills/{PID}/{sid}")["evidence"]["last_teaching"]

    def test_a_question_whose_quote_goes_during_the_exam_is_withdrawn_when_it_closes(self):
        # Five questions in the first domain, just enough to judge it, and five in each other domain.
        aid, items = self.running_exam(others=4, every_domain=True)
        n = len(items) - 1   # scored: all but the one withdrawn
        record = self.record()
        closed = self.sit(aid, items, GITHUB_MD.replace(Q0, NEW))

        self.assertEqual((closed["questions"], closed["answered"], closed["correct"], closed["withdrawn"]), (n, n, n, 1))
        self.assertEqual(closed["withdrawn_note"], "1 question was withdrawn because its official source changed during the exam.")
        self.assertEqual(closed["new_questions"], n)
        gone = closed["items"][0]
        self.assertTrue(gone["withdrawn"])
        self.assertEqual(gone["choice"], items[0]["answer"], "the answer given stays visible")
        self.assertNotIn("withdrawn", closed["items"][1])

        saved = self.dojo.store.read("learners", self.owner.key, "exams", aid)
        self.assertEqual(saved["withdrawn"], [0])
        self.assertEqual(saved["answers"]["0"]["choice"], items[0]["answer"])
        self.assertNotIn("correct", saved["answers"]["0"], "not scored")
        self.assertTrue(self.record().startswith(record), "the Record is only added to")
        mine = [e for e in self.dojo.store.events(self.owner.key) if (e.get("data") or {}).get("attempt") == aid]
        self.assertEqual([e["data"]["index"] for e in mine if e["type"] == "exam.answered"], list(range(1, n + 1)))
        done = next(e["data"] for e in mine if e["type"] == "exam.closed")
        self.assertEqual({k: done[k] for k in ("questions", "answered", "correct", "withdrawn", "new_questions")},
                         {"questions": n, "answered": n, "correct": n, "withdrawn": 1, "new_questions": n})

        shown = self.c.post(f"/api/exams/{aid}/rationales", headers=POST).json()
        gone = shown["items"][0]
        self.assertFalse({"answer", "rationale", "quote", "correct"} & gone.keys(), "its key may be stale, so it is not shown")
        self.assertEqual((gone["choice"], gone["source"]["url"]), (items[0]["answer"], URL))
        self.assertEqual(shown["items"][1]["answer"], items[1]["answer"])
        first = self.dojo.packages.get(PID)["skills"][S]["domain"]
        domain = next(d for d in shown["by_domain"] if d["id"] == first)
        self.assertEqual({k: domain[k] for k in ("questions", "correct", "withdrawn", "judged", "thinned")},
                         {"questions": 4, "correct": 4, "withdrawn": 1, "judged": False, "thinned": True})
        self.assertTrue(all(d["judged"] for d in shown["by_domain"] if d["id"] != first), "only the first domain is thinned")
        self.assertEqual((shown["correct"], shown["average"], shown["next_steps"]), (n, 100, []), "neither right nor missed")
        title = self.dojo.packages.get(PID)["skills"][S]["domain_title"]
        self.assertEqual(shown["thinned"], [title])
        self.assertIn(f"fewer than 5 questions to judge {title}, so this exam does not count", shown["thinned_note"])

        export = for_export(self.dojo.store.read("learners", self.owner.key, "exams", aid))
        self.assertFalse({"answer", "rationale", "quote"} & export["items"][0].keys())
        self.assertEqual(export["items"][0]["source"]["url"], URL)
        self.assertEqual(export["items"][1]["answer"], items[1]["answer"])
        self.assertIn("withdrawn", export["withheld"])

        listed = next(a for a in self.get(f"/api/exams?package={PID}")["attempts"] if a["id"] == aid)
        self.assertEqual((listed["questions"], listed["correct"], listed["withdrawn"]), (n, n, 1))

        # Rule 2 of "Should you book it?": the domain is left with too few questions, so the exam does not count.
        two = self.rule_two()
        attempt = two["rule"]["attempts"][0]
        self.assertEqual({k: attempt[k] for k in ("questions", "withdrawn", "thinned", "too_few", "every_domain_judged")},
                         {"questions": n, "withdrawn": 1, "thinned": [title], "too_few": [title], "every_domain_judged": False})
        self.assertEqual(two["rule"]["counted"], [])
        self.assertIn("withdrawn because its official source changed during the exam", two["rule"]["definition"])
        self.assertTrue(any("1 question withdrawn because a source changed during it left too few questions" in r
                            for r in two["reasons"]), two["reasons"])

    def test_a_withdrawal_that_leaves_enough_questions_keeps_the_exam_countable(self):
        aid, items = self.running_exam(others=5, every_domain=True)   # six in the first domain; five are left to judge it
        closed = self.sit(aid, items, GITHUB_MD.replace(Q0, NEW))
        self.assertEqual((closed["questions"], closed["withdrawn"]), (30, 1))
        attempt = self.rule_two()["rule"]["attempts"][0]
        self.assertEqual((attempt["thinned"], attempt["every_domain_judged"], attempt["exam_conditions"]), ([], True, True))
        self.assertEqual(self.rule_two()["rule"]["counted"], [aid])
        shown = self.c.post(f"/api/exams/{aid}/rationales", headers=POST).json()
        self.assertEqual((shown["thinned"], shown["thinned_note"]), ([], ""))

        # This attempt does not say which questions were new (as one built before that was kept), so the
        # withdrawn one is taken to have been new. With four of the 31 seen before, 26 of the 30 scored
        # questions are new: below the 90% rule 2 needs. Had the withdrawn one been one of the four, 27 of
        # 30 would be new, and the exam would count; the attempt does not say, so it does not.
        saved = self.dojo.store.read("learners", self.owner.key, "exams", aid)
        pkg = self.dojo.packages.get(PID)
        self.assertFalse(any("new" in it for it in saved["items"]))
        self.assertEqual(new_scored({**saved, "new_questions": 27}), 26)
        self.assertFalse(read_attempt({**saved, "new_questions": 27}, pkg)["all_new"])
        self.assertTrue(read_attempt(saved, pkg)["all_new"])

    def test_an_exam_whose_pages_still_hold_is_scored_in_full(self):
        aid, items = self.running_exam(others=4, every_domain=True)
        closed = self.sit(aid, items, FORMATTED)   # the formatting changed, not the words
        self.assertEqual((closed["questions"], closed["correct"], closed["withdrawn"], closed["withdrawn_note"]), (30, 30, 0, ""))
        self.assertEqual(self.dojo.store.read("learners", self.owner.key, "exams", aid)["withdrawn"], [])
        attempt = self.rule_two()["rule"]["attempts"][0]
        self.assertEqual((attempt["questions"], attempt["withdrawn"], attempt["every_domain_judged"]), (30, 0, True))
        self.assertEqual(self.rule_two()["rule"]["counted"], [aid])

    def test_the_note_counts_the_withdrawn_questions(self):
        self.assertEqual(withdrawn_note(1), "1 question was withdrawn because its official source changed during the exam.")
        self.assertEqual(withdrawn_note(3), "3 questions were withdrawn because their official source changed during the exam.")

    def test_when_time_ran_out_a_withdrawn_question_is_not_called_unanswered(self):
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text("utf-8")
        note = script.split("function timeUpNote(X) {", 1)[1].split("\n}\n", 1)[0]
        withdrawn = note.index("X.withdrawn")
        scored = note.index("a scored question left open counts as unanswered. A withdrawn question is not scored, so it is not a miss.")
        anything = note.index("anything left open counts as unanswered.")
        self.assertLess(withdrawn, scored, "with a withdrawn question, only scored questions left open are unanswered")
        self.assertLess(scored, anything, "without one, the sentence stays as it was")
        page = script.split("function examResultsPage(X) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('X.closed_reason === "time"', page)
        self.assertIn("timeUpNote(X)", page)
        self.assertNotIn("counts as unanswered", page)

    # ---- which withdrawn questions were new

    def test_a_withdrawn_question_seen_before_takes_nothing_off_the_new_questions(self):
        """31 of the 35 questions are new, and the withdrawn one was seen before. So 31 of the 34 scored
        questions are new (91%) and the exam counts for rule 2. Taking the withdrawn question off the new
        ones as well would leave 30 of 34 (88%), below the 90% the rule needs."""
        aid, items = self.built_exam(seen_before="mine")
        closed = self.sit(aid, items, GITHUB_MD.replace(Q0, NEW))
        self.assertEqual((closed["questions"], closed["withdrawn"], closed["new_questions"]), (34, 1, 31))
        done = next(e["data"] for e in self.dojo.store.events(self.owner.key)
                    if e["type"] == "exam.closed" and e["data"]["attempt"] == aid)
        self.assertEqual((done["questions"], done["withdrawn"], done["new_questions"]), (34, 1, 31))
        two = self.rule_two()
        attempt = two["rule"]["attempts"][0]
        self.assertEqual({k: attempt[k] for k in ("questions", "new_questions", "new_share", "all_new", "exam_conditions",
                                                  "every_domain_judged")},
                         {"questions": 34, "new_questions": 31, "new_share": 91, "all_new": True, "exam_conditions": True,
                          "every_domain_judged": True})
        self.assertEqual(two["rule"]["counted"], [aid])
        listed = next(a for a in self.get(f"/api/exams?package={PID}")["attempts"] if a["id"] == aid)
        self.assertEqual(listed["new_questions"], 31)

    def test_a_withdrawn_question_that_was_new_still_comes_off_the_new_questions(self):
        aid, items = self.built_exam(seen_before="other")
        closed = self.sit(aid, items, GITHUB_MD.replace(Q0, NEW))
        self.assertEqual((closed["questions"], closed["withdrawn"], closed["new_questions"]), (34, 1, 30))
        two = self.rule_two()
        attempt = two["rule"]["attempts"][0]
        self.assertEqual((attempt["new_questions"], attempt["new_share"], attempt["all_new"], attempt["every_domain_judged"]),
                         (30, 88, False, True))
        self.assertEqual(two["rule"]["counted"], [])
        self.assertTrue(any("not enough new questions" in r for r in two["reasons"]), two["reasons"])

    def test_only_withdrawn_questions_that_were_new_come_off_the_count(self):
        items = [{"skill": S, "stem": f"Question {i}", "new": i != 0} for i in range(10)]
        attempt = {"items": items, "new_questions": 9, "withdrawn": [0]}
        self.assertEqual(new_scored(attempt), 9, "the withdrawn question had been seen before")
        self.assertEqual(new_scored({**attempt, "withdrawn": [1]}), 8, "the withdrawn question was new")
        self.assertEqual(new_scored({**attempt, "withdrawn": [0, 1]}), 8)
        self.assertEqual(new_scored({**attempt, "withdrawn": []}), 9)
        unknown = {**attempt, "items": [{k: v for k, v in it.items() if k != "new"} for it in items]}
        self.assertEqual(new_scored(unknown), 8, "a question that does not say is taken to have been new")
        self.assertEqual(new_scored({**attempt, "new_questions": 10}), 9,
                         "never more than the scored questions not recorded as seen")
        self.assertEqual(new_scored({**attempt, "new_questions": 10, "withdrawn": [1]}), 8)

    def test_a_start_that_drops_a_question_reads_again_which_are_new(self):
        """Between the build and the start, a page changes and one of the questions is seen elsewhere. The
        start drops the question whose quote is gone, and the count and each question's record agree."""
        aid, items = self.built_exam(seen_before="other", start=False)
        seen_since = next(it for it in items if it["skill"] == OTHER and it["new"])
        mark_seen(self.dojo.store, self.owner.key, PID, [question_key(seen_since["stem"])])
        self.web.set(PAGE, GITHUB_MD.replace(Q0, NEW))
        self.read_due()
        started = self.c.post(f"/api/exams/{aid}/start", headers=POST)
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual({k: started.json()[k] for k in ("questions", "source_dropped", "new_questions")},
                         {"questions": 34, "source_dropped": 1, "new_questions": 29})
        saved = self.app.state.exams.attempt(self.owner, aid)
        self.assertEqual(sum(1 for it in saved["items"] if it["new"]), saved["new_questions"])
        self.assertFalse(next(it["new"] for it in saved["items"] if it["stem"] == seen_since["stem"]))

    # ---- opening the results

    def test_opening_the_results_teaches_only_the_skills_of_scored_questions(self):
        """A withdrawn question's key and rationale are not shown, so its skill's teaching clock does not
        start: the Record lists only the skills of the scored questions."""
        aid, items = self.running_exam(others=4)   # the withdrawn question is the only one of its skill
        self.sit(aid, items, GITHUB_MD.replace(Q0, NEW))
        shown = self.shown_event(aid)
        self.assertEqual((shown["skills"], shown["questions"]), ([OTHER], 4))
        self.assertIsNone(self.last_teaching(S), "nothing about this skill was shown")
        self.assertIsNotNone(self.last_teaching(OTHER))

    def test_a_skill_with_a_scored_question_is_still_taught(self):
        mine, other = two_questions(self)
        kept = {**mine, "stem": mine["stem"] + " (the second sentence)", "quote": Q1}   # still on the page
        items = [mine, kept] + [{**other, "stem": f"{other['stem']} ({k + 1})"} for k in range(3)]
        aid = self.start_exam(items)
        closed = self.sit(aid, items, GITHUB_MD.replace(Q0, NEW))
        self.assertEqual((closed["questions"], closed["withdrawn"]), (4, 1))
        shown = self.shown_event(aid)
        self.assertEqual((shown["skills"], shown["questions"]), (sorted([S, OTHER]), 4))
        self.assertIsNotNone(self.last_teaching(S))

    def test_an_exam_with_every_question_withdrawn_still_opens_its_results(self):
        aid, items = self.running_exam(others=0)
        closed = self.sit(aid, items, GITHUB_MD.replace(Q0, NEW))
        self.assertEqual((closed["questions"], closed["withdrawn"]), (0, 1))
        shown = self.shown_event(aid)
        self.assertEqual((shown["skills"], shown["questions"]), ([], 0), "the moment is recorded, and teaches nothing")
        self.assertIsNone(self.last_teaching(S))
        self.assertIn("withdrawn", for_export(self.dojo.store.read("learners", self.owner.key, "exams", aid))["withheld"])


class RewriteAllowanceTests(DriftCase):
    """Writing a lesson again costs author and gate calls, so a changed page gets at most DAILY_REWRITES
    rewrites a day. The rest wait in order: the active exam first, then most exam weight."""

    def setUp(self):
        super().setUp()
        cap = mock.patch.object(drift_mod, "DAILY_REWRITES", 1)
        cap.start()
        self.addCleanup(cap.stop)
        chat = next(s["url"] for s in self.dojo.packages.skill(PID, OTHER)["sources"])
        self.chat = urlparse(chat).path

    def prep(self):
        return self.app.state.preparer

    def both_rechecking(self) -> list[str]:
        """Lessons for both skills, then both pages lose a quote the lessons use. Returns the skills in the
        order the Preparer takes them."""
        self.lesson(), self.lesson(OTHER)
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.web.set(self.chat, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        self.assertEqual(self.drift.rechecking(PID), {S, OTHER})
        return [s["id"] for s in self.prep()._skills(PID) if s["id"] in (S, OTHER)]

    def test_rewrites_stop_at_the_daily_allowance_and_the_rest_wait_in_order(self):
        first, second = self.both_rechecking()
        self.assertEqual(self.prep().next_skill(), (PID, first))
        time.sleep(1.1)  # lessons are ordered by the second they were written in
        self.assertTrue(self.prep().step())
        self.assertEqual(self.drift.rechecking(PID), {second})
        self.assertEqual(self.drift.rewrites_left(), 0)
        waiting = self.prep().next_skill()
        self.assertIsNotNone(waiting, "a skill with no lesson yet is still prepared")
        self.assertNotEqual(waiting, (PID, second), "the other rewrite waits for tomorrow")

        want = {"per_day": 1, "used_today": 1, "left_today": 0, "waiting": 1}
        diag = self.get("/api/diag")["drift_check"]["rewrites"]
        self.assertEqual({k: diag[k] for k in want}, want)
        self.assertEqual(diag["last"]["skill"], f"{PID}/{first}")
        studio = self.get("/api/studio/drift")
        self.assertEqual({k: studio["rewrites"][k] for k in want}, want)
        self.assertEqual(studio["limits"]["rewrites_per_day"], 1)

        self.boot()   # a restart does not hand out a new allowance: the count is kept on the share
        self.assertEqual(self.drift.rewrites_left(), 0)
        self.assertNotEqual(self.prep().next_skill(), (PID, second))

        self.later(24)   # the next day (UTC)
        self.assertEqual(self.drift.rewrites_left(), 1)
        self.assertEqual(self.get("/api/diag")["drift_check"]["rewrites"]["used_today"], 0)
        self.assertEqual(self.prep().next_skill(), (PID, second))

    def test_the_learners_own_request_is_not_limited(self):
        first, second = self.both_rechecking()
        time.sleep(1.1)
        self.prep().step()
        self.assertEqual(self.drift.rewrites_left(), 0)
        time.sleep(1.1)
        self.lesson(second)   # what "Write the lesson" runs
        self.assertEqual(self.drift.rechecking(PID), set())
        self.assertEqual(self.drift._doc()["rewrites"], 1)

    def test_a_rewrite_counts_before_it_starts_so_a_failure_or_a_crash_gets_no_free_retry(self):
        self.lesson()
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        with mock.patch.object(self.dojo, "make_lesson", side_effect=UserError("The author did not answer.")):
            self.assertTrue(self.prep().step())
        self.assertEqual(self.drift.rewrites_left(), 0, "a rewrite that fails still counts")
        self.assertEqual(self.drift.rechecking(PID), {S})

        self.boot()
        self.later(24)
        with mock.patch.object(self.dojo, "make_lesson", side_effect=RuntimeError("Dojo stopped")):
            with self.assertRaises(RuntimeError):
                self.prep().step()
        self.boot()   # a restart in the middle of the rewrite
        self.assertEqual(self.drift.rewrites_left(), 0)
        self.assertNotEqual(self.prep().next_skill(), (PID, S))
        self.assertEqual(self.drift.rechecking(PID), {S}, "the lesson stays held back until it is written again")


class RewriteOrderTests(unittest.TestCase):
    """With more than one exam, the day's rewrites go where Studio says: the active exam first, then most
    exam weight. They do not take turns between exams, and they do not move the turn that new lessons
    take (ADR 0005). A stand-in Dojo with three exams, so the order is exact and no model is called."""

    def setUp(self):
        def exam(**shares):
            return {"skills": {sid: {"id": sid, "share": share, "sources": [{"url": "x"}]} for sid, share in shares.items()}}
        pkgs = {"gh-300": exam(g1=0.30, g2=0.20, g3=0.10), "dp-800": exam(d1=0.25, d2=0.05, d3=0.40),
                "xy-110": exam(x1=0.15, x2=0.35)}
        # Every skill has a lesson but one per exam, and a changed page holds five of those lessons back.
        self.ready = {"gh-300": {"g1", "g3"}, "dp-800": {"d1", "d2"}, "xy-110": {"x1"}}
        self.again = {pid: set(sids) for pid, sids in self.ready.items()}
        self.gone = {pid: set() for pid in pkgs}   # the skills whose every cited page is gone
        self.profile = {"active_package": "gh-300"}
        self.used, self.written = [0], []

        def charge(pid, sid):
            if self.used[0] >= 3:
                return False
            self.used[0] += 1
            return True

        def make_lesson(progress, learner, pid, sid):
            self.written.append((pid, sid, "rewrite" if sid in self.again[pid] else "new"))
            self.ready[pid].add(sid)
            self.again[pid].discard(sid)
            return {"lesson": f"{pid}/{sid}"}
        drift = types.SimpleNamespace(rechecking=lambda pid: set(self.again[pid]), rewrites_left=lambda: 3 - self.used[0],
                                      charge_rewrite=charge, gone_skills=lambda pid: set(self.gone[pid]))
        dojo = types.SimpleNamespace(packages=types.SimpleNamespace(ids=lambda: list(pkgs), by_id=pkgs),
                                     profile=lambda learner: dict(self.profile), lesson_skills=lambda pid: set(self.ready[pid]),
                                     lessons_for=lambda pid, sid: [], jobs=types.SimpleNamespace(for_learner=lambda learner: []),
                                     drift=drift, make_lesson=make_lesson, lesson_audio=lambda *a: None, quality=lambda *a: None,
                                     enrolled_counts=lambda: {},   # no members: the owner's order (ADR 0009)
                                     exam_order=lambda learner: sorted(pkgs, key=lambda p: p != self.profile["active_package"]))
        self.prep = Preparer(dojo, types.SimpleNamespace(key="owner"))

    def test_rewrites_go_to_the_active_exam_first_then_by_weight(self):
        self.assertEqual(self.prep.rewrites(), [("gh-300", "g1"), ("gh-300", "g3"), ("dp-800", "d1"), ("xy-110", "x1"),
                                                ("dp-800", "d2")])
        for _ in range(3):
            self.assertTrue(self.prep.step())
        self.assertEqual(self.written, [("gh-300", "g1", "rewrite"), ("gh-300", "g3", "rewrite"), ("dp-800", "d1", "rewrite")],
                         "the day's allowance does not alternate between exams")
        self.assertIsNone(self.prep._last_pid, "rewrites do not move the exams' turn")
        # With the allowance used, new lessons still take turns, from the active exam.
        for _ in range(3):
            self.assertTrue(self.prep.step())
        self.assertEqual(self.written[3:], [("gh-300", "g2", "new"), ("dp-800", "d3", "new"), ("xy-110", "x2", "new")])
        self.assertIsNone(self.prep.next_skill(), "the other rewrites wait for tomorrow")
        # The next day they go in the same order.
        self.used[0] = 0
        self.assertEqual(self.prep.next_skill(), ("xy-110", "x1"))
        self.prep.step()
        self.assertEqual(self.prep.next_skill(), ("dp-800", "d2"))

    def test_the_learner_s_active_exam_decides_which_goes_first(self):
        self.profile["active_package"] = "dp-800"
        self.assertEqual(self.prep.rewrites(), [("dp-800", "d1"), ("dp-800", "d2"), ("gh-300", "g1"), ("xy-110", "x1"),
                                                ("gh-300", "g3")])
        self.prep._last_pid = "dp-800"   # whatever the exams' turn, the order stays
        self.assertEqual(self.prep.next_skill(), ("dp-800", "d1"))

    def test_a_skill_whose_every_page_is_gone_is_not_tried_and_uses_no_rewrite(self):
        """Four such skills ahead in the order, more than the day's three rewrites: the one rewrite that can
        work still goes today, and nothing is written for the others (ADR 0006)."""
        self.gone.update({"gh-300": {"g1", "g3"}, "dp-800": {"d1", "d2"}})
        self.assertEqual(self.prep.rewrites(), [("xy-110", "x1")])
        self.assertTrue(self.prep.step())
        self.assertEqual((self.written, self.used[0]), ([("xy-110", "x1", "rewrite")], 1))
        self.gone["dp-800"] = set()   # a page answers again, or the exam cites another page
        self.assertEqual(self.prep.rewrites(), [("dp-800", "d1"), ("dp-800", "d2")])


class GoneRewriteTests(DriftCase):
    """A rewrite that cannot work uses no rewrite of the day (ADR 0006). Nothing can be written from a page
    that is gone, moved included, so a skill whose every cited page is gone is not tried: no model is
    called and the day's rewrites go to the lessons that can be written again. Studio says why it waits.
    Once one of its pages answers again, or its package cites another page, it is back in the queue."""

    LATE = "d4.g1.s2"   # lighter than every skill that cites only the inline-suggestions page

    def setUp(self):
        super().setUp()
        cap = mock.patch.object(drift_mod, "DAILY_REWRITES", 3)
        cap.start()
        self.addCleanup(cap.stop)
        self.prep = self.app.state.preparer
        self.late_url = next(r["url"] for r in self.dojo.packages.skill(PID, self.LATE)["sources"])

    def held(self, moved: bool = False) -> set[str]:
        """Lessons for every skill that cites only the inline-suggestions page, more of them than the day's
        rewrites and all ahead of LATE in the Preparer's order, and for LATE. That page goes; LATE's page
        loses a quote its lesson uses and stays. Returns the skills whose every page is gone."""
        pkg = self.dojo.packages.get(PID)
        gone = {sid for sid, s in pkg["skills"].items() if [r["url"] for r in s["sources"]] == [URL]}
        self.assertGreater(len(gone), drift_mod.DAILY_REWRITES)
        self.assertEqual([r["url"] for r in pkg["skills"][self.LATE]["sources"]], [self.late_url])
        order = [s["id"] for s in self.prep._skills(PID)]
        self.assertLess(max(order.index(sid) for sid in gone), order.index(self.LATE), "without the rule, they go first")
        for sid in sorted(gone | {self.LATE}):
            self.lesson(sid)
        self.web.set(urlparse(self.late_url).path, GITHUB_MD.replace(Q2, NEW))
        if moved:
            self.web.set(PAGE, "", status=301, headers={"location": API + ELSEWHERE})
        else:
            self.web.set(PAGE, "Not found", status=404)
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertTrue(self.dojo.sources.head(URL)["gone"])
        self.assertEqual(self.drift.rechecking(PID), gone | {self.LATE})
        self.assertEqual(self.drift.gone_skills(PID), gone)
        return gone

    def test_a_rewrite_that_cannot_work_uses_no_slot_and_one_that_can_goes_the_same_day(self):
        gone = self.held()
        self.assertEqual(self.prep.rewrites(), [(PID, self.LATE)])
        self.assertEqual(self.get("/api/diag")["preparation"]["rewriting"], 1)
        tried, write = [], self.dojo.make_lesson

        def make_lesson(progress, learner, pid, sid):
            tried.append((pid, sid))
            if sid != self.LATE:
                raise UserError("Not written in this test.")   # new lessons for other skills are not the point here
            return write(progress, learner, pid, sid)
        time.sleep(1.1)   # lessons are ordered by the second they were written in
        with mock.patch.object(self.dojo, "make_lesson", side_effect=make_lesson):
            for _ in range(4):
                self.assertTrue(self.prep.step())
        self.assertEqual(tried[0], (PID, self.LATE), "the rewrite that can work gets a slot the same day")
        self.assertFalse({sid for pid, sid in tried if pid == PID} & gone, "no try, and no model call, for a page that is gone")
        self.assertEqual(self.drift._doc()["rewrites"], 1, "no slot is used for them")
        self.assertEqual(self.drift._doc()["last_rewrite"]["skill"], f"{PID}/{self.LATE}")
        self.assertEqual(self.drift.rewrites_left(), 2)
        self.assertEqual(self.drift.rechecking(PID), gone, "they stay held back")
        self.assertEqual(self.prep.rewrites(), [])

        studio = self.get("/api/studio/drift")
        want = {"used_today": 1, "left_today": 2, "waiting": 0, "waiting_for_pages": len(gone)}
        self.assertEqual({k: studio["rewrites"][k] for k in want}, want)
        self.assertEqual(studio["rechecking"], len(gone))
        change = next(c for c in studio["changes"] if c["url"] == URL)
        self.assertEqual({x["skill"]: (x["now"], x.get("why")) for x in change["lessons"]},
                         {sid: ("waiting", drift_mod.PAGES_GONE) for sid in gone})
        late = next(c for c in studio["changes"] if c["url"] == self.late_url)
        self.assertEqual([(x["skill"], x["now"]) for x in late["lessons"]], [(self.LATE, "rewritten")])
        diag = self.get("/api/diag")
        self.assertEqual({k: diag["drift_check"]["rewrites"][k] for k in want}, want)
        self.assertEqual((diag["drift_check"]["lessons_rechecking"], diag["preparation"]["rewriting"]), (len(gone), 0))

    def test_a_skill_is_back_in_the_queue_once_its_package_cites_another_page_or_a_page_answers(self):
        gone = self.held(moved=True)
        order = [s["id"] for s in self.prep._skills(PID)]
        first = min(gone, key=order.index)
        pkg = self.dojo.packages.get(PID)
        other = deepcopy(pkg)
        other["skills"][first]["sources"] = deepcopy(pkg["skills"][OTHER]["sources"])
        self.dojo.packages.by_id = {**self.dojo.packages.by_id, PID: other}
        self.assertEqual(self.drift.gone_skills(PID), gone - {first})
        self.assertEqual(self.prep.rewrites(), [(PID, first), (PID, self.LATE)])
        self.assertEqual(self.prep.next_skill(), (PID, first))
        self.assertEqual(self.get("/api/studio/drift")["rewrites"]["waiting_for_pages"], len(gone) - 1)

        # Back to the page it cited, which answers again but lost a quote the lessons use.
        self.dojo.packages.by_id = {**self.dojo.packages.by_id, PID: pkg}
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.later(25)
        self.read_due()
        self.assertFalse(self.dojo.sources.head(URL)["gone"])
        self.assertEqual(self.drift.gone_skills(PID), set())
        self.assertEqual(self.prep.rewrites(), [(PID, sid) for sid in order if sid in gone | {self.LATE}])
        rewrites = self.get("/api/studio/drift")["rewrites"]
        self.assertEqual((rewrites["waiting"], rewrites["waiting_for_pages"]), (len(gone) + 1, 0))

    def one_miss(self) -> None:
        """S's lesson is held for a quote its page lost, and the page then answers "not found" once. The next
        such read, 7 hours on, makes the page gone."""
        self.lesson()
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        self.assertEqual(self.prep.rewrites(), [(PID, S)])
        self.web.set(PAGE, "Not found", status=404)
        self.later(25)
        self.read_due()
        self.assertEqual(self.drift.gone_skills(PID), set(), "one miss is not gone yet")
        self.later(7)

    def test_a_page_that_goes_after_the_skill_was_picked_uses_no_slot(self):
        """The check can mark the skill's last page gone after the Preparer picked the skill and before the
        rewrite is counted. The count looks again: no rewrite of the day is used and no model is called."""
        self.one_miss()
        pick = self.prep.next_skill

        def pick_then_gone():
            got = pick()
            self.read_due()   # meanwhile, on the check's thread
            return got
        with mock.patch.object(self.prep, "next_skill", side_effect=pick_then_gone), \
                mock.patch.object(self.dojo, "make_lesson") as write:
            self.assertTrue(self.prep.step())
        self.assertIn(S, self.drift.gone_skills(PID))
        write.assert_not_called()
        self.assertEqual(self.drift._doc()["rewrites"], 0)
        self.assertNotIn("last_rewrite", self.drift._doc())
        self.assertEqual(self.drift.rewrites_left(), drift_mod.DAILY_REWRITES)
        self.assertFalse(self.drift.charge_rewrite(PID, S))
        self.assertEqual(self.drift._doc()["rewrites"], 0)
        self.assertEqual(self.prep.rewrites(), [])
        self.assertNotEqual(self.prep.next_skill(), (PID, S), "and it is not picked again")

    def test_counting_a_rewrite_waits_while_a_page_is_being_marked_gone(self):
        """Marking a page gone and counting a rewrite share one lock, so no page can go between the count's
        look at the skill's pages and the count itself."""
        self.one_miss()
        mark, waited, counted, threads = self.dojo.sources.mark_gone, [], [], []

        def marking(*args, **kw):
            if not threads:
                t = threading.Thread(target=lambda: counted.append(self.drift.charge_rewrite(PID, S)))
                threads.append(t)
                t.start()
                t.join(0.3)
                waited.append(t.is_alive())
            return mark(*args, **kw)
        with mock.patch.object(self.dojo.sources, "mark_gone", side_effect=marking):
            self.read_due()
        threads[0].join(5)
        self.assertEqual(waited, [True], "the count waits for the mark")
        self.assertEqual(counted, [False], "and then sees the page gone")
        self.assertIn(S, self.drift.gone_skills(PID))
        self.assertEqual(self.drift._doc()["rewrites"], 0)


class UncheckedCopyTests(DriftCase):
    """A copy stored before Dojo checked where a read ended may hold another page's text under the cited
    address. It is never read as the page: it has no version, so nothing is judged from it, until the
    page is read again. From that read on, what was written from the page is judged as usual."""

    def unchecked(self) -> None:
        store, key = self.dojo.store, self.dojo.sources.key(URL)
        snap = store.read("sources", key)
        snap.pop("final")
        snap["history"] = [{"sha256": "0" * 64, "norm": "0" * 64, "first_seen": iso(), "last_seen": iso()}]
        store.write("sources", key, value=snap)
        self.dojo.sources._heads.pop(URL, None)

    def test_an_unchecked_copy_is_read_again_and_judged_from_then_on(self):
        lesson = self.lesson()
        sha = self.dojo.sources.head(URL)["sha256"]
        self.unchecked()
        src = self.dojo.sources
        head = src.head(URL)
        self.assertEqual((head["verified"], head["sha256"], head["history"]), (False, None, []))
        self.assertEqual(src.compare(URL, sha)["state"], "same")
        self.assertEqual(src.compare(URL, "f" * 64)["state"], "same", "nothing is compared with a copy Dojo may not use")
        self.assertFalse(src.holds(URL, Q0), "a quote is never found on it")
        self.assertIsNone(self.drift._mark(URL))
        self.assertIsNone(self.drift.lesson_state(lesson))
        self.assertEqual((self.get("/api/studio/drift")["pages"]["unchecked"], self.get("/api/diag")["drift_check"]["pages_unchecked"]), (1, 1))
        # The page lost a quote meanwhile. The next read is the first one Dojo may use, and it counts.
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.assertEqual(self.read_due(), 1)
        head = src.head(URL)
        self.assertEqual((head["verified"], head["final"], head["history"]), (True, URL, []), "no history is taken from the old copy")
        self.assertTrue(self.drift.withheld(lesson))
        self.assertEqual(self.drift.lesson_note(lesson)["removed"], [Q2])
        pages = self.get("/api/studio/drift")["pages"]
        self.assertEqual((pages["unchecked"], pages["known"]), (0, 1))


class HeldIsNotUnsourcedTests(DriftCase):
    """What a changed page no longer supports is held for now. The exam keeps its official sources: a hold
    never makes a built-in exam look unsourced (ADR 0005). The timed exam is not refused, every domain is
    still planned, and the blueprint's mark stays. Only nothing new is written from a page that is gone."""

    def test_a_page_that_is_gone_does_not_make_the_exam_look_unsourced(self):
        pkg = self.dojo.packages.get(PID)
        mark = pkg["meta"]["blueprint"]
        lesson = self.lesson()
        self.web.set(PAGE, "", status=301, headers={"location": API + ELSEWHERE})
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertTrue(self.dojo.sources.head(URL)["gone"])
        self.assertTrue(self.drift.withheld(lesson))

        pkg = self.dojo.packages.get(PID)
        self.assertEqual(pkg["meta"]["blueprint"], mark)
        self.assertEqual([r["url"] for r in pkg["skills"][S]["sources"]], [URL])
        self.assertEqual((untestable(pkg), blocker(pkg)), ([], None))
        planned = plan_skills(pkg, 50)
        self.assertIn(S, planned, "the held skill keeps its share of the blueprint")
        self.assertEqual({pkg["skills"][sid]["domain"] for sid in planned}, {d["id"] for d in pkg["domains"]})
        exams = self.get(f"/api/exams?package={PID}")
        self.assertIsNone(exams["untimed"])
        self.assertEqual(exams["lengths"]["full"]["questions"], spec(pkg, "full")["questions"])
        self.assertEqual(self.app.state.exams.create(self.owner, PID, "short")["blueprint"], mark)
        # Only nothing new is written from the page: its question is lost, with the reason, and the rest are written.
        questions, lost = self.dojo.write_mcqs(PID, [S, OTHER])
        self.assertEqual([q["skill"] for q in questions], [OTHER])
        self.assertEqual(len(lost), 1)
        self.assertIn("is no longer there", lost[0]["reason"])
        view = self.get(f"/api/packages/{PID}")
        skill = next(s for d in view["domains"] for g in d["groups"] for s in g["skills"] if s["id"] == S)
        self.assertEqual((skill["sourced"], skill["drift"]["state"]), (True, "recheck"))


class PlanTests(DriftCase):
    """The plan (ADR 0004) and a hold. A planned step opens the skill, where a new question or lesson is
    written from the page as it is now, so a held item never blocks one. What a hold would make lead nowhere
    is not planned while it lasts: any step for a skill whose every cited page is gone, a 3-minute check
    that would ask about it, and the Lesson or Listen of a lesson held back and being written again. Such a
    session already planned goes, even one the learner pinned or moved, and "Moved for you" says why. A lab
    stays planned: the database checks it, not a page."""

    S3 = "d1.g1.s3"   # also cites only the inline-suggestions page

    def setUp(self):
        super().setUp()
        self.planner, self.now = self.app.state.plan, ahead()
        self.rec = Record(self.dojo.store, self.owner.key)

    def planned(self, pid: str = PID) -> list[dict]:
        doc = self.planner.refresh(self.owner, self.now)
        return [s for s in doc["sessions"] if s["package"] == pid and s["state"] == "planned"]

    def steps(self, pid: str = PID) -> set[tuple[str, str | None]]:
        return {(s["kind"], s["skill"]) for s in self.planned(pid)}

    def picks(self) -> list[str]:
        return [p["skill"] for p in self.app.state.checks.plan(self.owner, PID)["picks"]]

    def gone(self) -> None:
        self.web.set(PAGE, "Not found", status=404)
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertTrue(self.dojo.sources.head(URL)["gone"])

    def only_the_page(self) -> set[str]:
        pkg = self.dojo.packages.get(PID)
        return {sid for sid, s in pkg["skills"].items() if [r["url"] for r in s["sources"]] == [URL]}

    def feed(self) -> set[str]:
        """The sessions in the calendar feed, by id."""
        body = self.planner.calendar_ics(self.owner, BASE, self.now)
        return {value(own(e)["UID"][0]).removesuffix("@dojo") for e in blocks(unfold(body), "VEVENT")}

    def removed(self) -> dict[str, tuple[str, str]]:
        """The Plan's "Moved for you" lines about sessions a source change removed, by session."""
        return {m["session"]: (m["to"], m["why"]) for m in self.planner.view(self.owner, None, self.now)["moved"]
                if m.get("removed")}

    def test_nothing_is_planned_for_a_skill_whose_every_page_is_gone_until_one_is_back(self):
        only = self.only_the_page()
        self.assertTrue({S, self.S3} < only)
        self.lesson()
        self.rec.attempt(PID, S, self.now - timedelta(days=2), "probe", 1)          # missed: the lesson is next
        self.rec.attempt(PID, self.S3, self.now - timedelta(hours=30), "probe", 3)   # met on its own: a later check
        self.rec.attempt(PID, OTHER, self.now - timedelta(days=1), "probe", 1)
        record = self.record()
        self.assertTrue({("lesson", S), ("later", self.S3), ("lesson", OTHER), ("check3", None)} <= self.steps())
        self.assertIn("d1.g2.s1", self.picks(), "an untried skill that cites only the page")

        self.gone()
        self.assertEqual(self.drift.gone_skills(PID), only)
        steps = self.steps()
        self.assertFalse({sid for _, sid in steps} & only, steps)
        self.assertTrue({("lesson", OTHER), ("check3", None)} <= steps, "the rest of the exam is planned as before")
        self.assertTrue(self.picks())
        self.assertFalse(set(self.picks()) & only, "the 3-minute check asks about skills it can write a question for")
        self.assertEqual(self.record(), record, "neither the plan nor the check's choice writes to the Record")

        # The page is back: so is every step for its skills.
        self.web.set(PAGE, GITHUB_MD)
        self.later(25)
        self.read_due()
        self.assertEqual(self.drift.gone_skills(PID), set())
        self.assertTrue({("lesson", S), ("later", self.S3)} <= self.steps())
        self.assertIn("d1.g2.s1", self.picks())

    def test_a_lab_stays_planned_for_a_skill_whose_every_page_is_gone(self):
        """The database checks a lab, not a page: only the skill's other steps wait for its page."""
        dp, sid = "dp-800", LABS["vector"]["skill"]
        self.assertEqual([urlparse(r["url"]).path for r in self.dojo.packages.get(dp)["skills"][sid]["sources"]], [VECTOR_PATH])
        self.dojo.make_lesson(lambda step: None, self.owner, dp, sid)   # reads the page, so the check watches it
        self.rec.attempt(dp, sid, self.now - timedelta(days=2), "probe", 1)   # missed: the lesson is next
        self.assertEqual({kind for kind, s in self.steps(dp) if s == sid}, {"lesson", "lab"})

        self.web.set(VECTOR_PATH, status=404)
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertIn(sid, self.drift.gone_skills(dp))
        self.assertEqual({kind for kind, s in self.steps(dp) if s == sid}, {"lab"})
        lab = next(s for s in self.planned(dp) if s["kind"] == "lab")
        self.assertEqual(self.planner.link(lab), "#/lab/vector")

    def test_no_three_minute_check_when_only_skills_whose_pages_are_gone_are_left_to_ask(self):
        """GH-300 with only the skills that cite nothing but the inline-suggestions page sourced: once that
        page is gone, a 3-minute check would have nothing to ask, so none is planned or started. DP-800
        keeps its own."""
        pkg = deepcopy(self.dojo.packages.get(PID))
        for s in pkg["skills"].values():
            if [r["url"] for r in s["sources"]] != [URL]:
                s["sources"] = []
        self.dojo.packages.by_id = {**self.dojo.packages.by_id, PID: pkg}
        self.lesson()
        self.assertEqual(self.steps(), {("check3", None)})
        self.assertTrue(self.picks())
        self.gone()
        self.assertEqual(self.steps(), set())
        self.assertEqual(self.picks(), [])
        with self.assertRaises(UserError):
            self.app.state.checks.create(self.owner, PID)
        self.assertIn(("check3", None), self.steps("dp-800"))

    def test_a_held_lesson_is_not_planned_to_study_or_hear_until_it_is_written_again(self):
        lesson = self.lesson()
        for m in self.dojo.slide_media(self.dojo.lesson(lesson)):
            self.dojo.store.write_bytes("media", m + ".mp3", data=b"ID3" + bytes(64))
        self.lesson(self.S3)
        self.rec.attempt(PID, S, self.now - timedelta(days=2), "probe", 0)                    # the lesson is next
        self.rec.attempt(PID, self.S3, self.now - timedelta(days=2), "practice", 3, hints=1)   # a check is next
        teach = [s for s in self.planned() if s["skill"] == S]
        self.assertEqual([(s["kind"], self.planner.link(s)) for s in teach], [("listen", f"#/lesson/{lesson}/listen")])

        # The page loses a quote both lessons use. Both are held back, out of the podcast feed.
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        self.assertEqual(self.drift.rechecking(PID), {S, self.S3})
        self.assertEqual(self.drift.gone_skills(PID), set())
        planned = self.planned()
        self.assertEqual([s for s in planned if s["skill"] == S], [], "no Lesson or Listen of the held lesson")
        check = [s for s in planned if s["skill"] == self.S3]
        self.assertEqual([(s["kind"], self.planner.link(s)) for s in check], [("check", f"#/skill/{PID}/{self.S3}")],
                         "practice and checks stay planned")
        self.assertTrue(self.dojo.grounding(PID, self.S3), "a new question is written from the page as it is now")

        # The Preparer writes the lesson again: the new one is planned.
        self.assertEqual(self.app.state.preparer.rewrites()[0], (PID, S))
        time.sleep(1.1)   # lessons are ordered by the second they were written in
        self.assertTrue(self.app.state.preparer.step())
        new = self.dojo.lessons_for(PID, S)[0]["id"]
        self.assertNotEqual(new, lesson)
        self.assertFalse(self.drift.withheld(new))
        teach = [s for s in self.planned() if s["skill"] == S]
        self.assertEqual([s["lesson"] for s in teach], [new])
        self.assertIn(new, self.planner.link(teach[0]))
        self.assertEqual([s["kind"] for s in self.planned() if s["skill"] == self.S3], ["check"])

    def test_a_pinned_or_moved_session_of_a_skill_whose_every_page_is_gone_leaves_the_plan_and_the_feed(self):
        """A session the learner pinned or moved keeps its time (ADR 0004), but not one that would lead
        nowhere: it goes, like a session of a removed exam, and "Moved for you" says why. What was done stays
        done. Once the page is back, the skill's steps are planned again and the line goes."""
        seen = "d1.g2.s1"
        self.assertTrue({S, self.S3, seen} <= self.only_the_page())
        lesson = self.lesson()
        self.rec.attempt(PID, S, self.now - timedelta(days=2), "probe", 1)
        self.rec.attempt(PID, seen, self.now - timedelta(days=2, hours=-1), "probe", 0)   # its lesson is next
        self.rec.attempt(PID, self.S3, self.now - timedelta(hours=30), "probe", 3)       # met on its own: a later check
        self.rec.add("lesson.viewed", self.now - timedelta(days=1), package=PID, skill=S, lesson=lesson, modality="read")
        planned = self.planned()
        practice, later, study = (next(s for s in planned if s["skill"] == sid) for sid in (S, self.S3, seen))
        self.assertEqual((practice["kind"], later["kind"], study["kind"]), ("practice", "later", "lesson"))
        self.planner.pin(self.owner, practice["id"], True, self.now)
        day = (to_local(self.now) + timedelta(days=3)).date()
        self.planner.move(self.owner, later["id"], f"{day.isoformat()}T21:00", now=self.now)
        self.rec.add("lesson.viewed", self.now, package=PID, skill=seen, lesson=new_id("lesson"), modality="read")
        self.assertTrue({practice["id"], later["id"]} <= self.feed())
        record = self.record()

        self.gone()
        doc = self.planner.refresh(self.owner, self.now)
        left = {s["id"]: s["state"] for s in doc["sessions"]}
        self.assertNotIn(practice["id"], left, "pinned")
        self.assertNotIn(later["id"], left, "moved")
        self.assertEqual(left[study["id"]], "done", "what was done stays done")
        reasons = {m["session"]: m["reason"] for m in doc["moves"] if m["why"] == "removed"}
        self.assertEqual((reasons[practice["id"]], reasons[later["id"]]), ("gone", "gone"))
        self.assertFalse({practice["id"], later["id"]} & self.feed())
        lines = self.removed()
        for s in (practice, later):
            self.assertEqual(lines[s["id"]], ("Removed", "Its official pages are gone or moved"))
        self.assertEqual(self.record(), record, "the plan never writes to the Record")

        self.web.set(PAGE, GITHUB_MD)
        self.later(25)
        self.read_due()
        self.assertEqual(self.drift.gone_skills(PID), set())
        self.assertTrue({("practice", S), ("later", self.S3)} <= self.steps(), "planned again")
        self.assertFalse({practice["id"], later["id"]} & set(self.removed()))

    def test_a_pinned_lesson_being_written_again_leaves_the_plan_until_the_new_one_is_there(self):
        lesson = self.lesson()
        self.rec.attempt(PID, S, self.now - timedelta(days=2), "probe", 0)   # the lesson is next
        study = next(s for s in self.planned() if s["skill"] == S)
        self.assertEqual((study["kind"], study["lesson"]), ("lesson", lesson))
        self.planner.pin(self.owner, study["id"], True, self.now)

        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        self.assertEqual(self.drift.rechecking(PID), {S})
        self.assertEqual(self.drift.gone_skills(PID), set())
        self.assertEqual([s for s in self.planned() if s["skill"] == S], [])
        self.assertNotIn(study["id"], self.feed())
        self.assertEqual(self.removed(), {study["id"]: ("Removed", "This lesson is being written again")})

        time.sleep(1.1)   # lessons are ordered by the second they were written in
        self.assertTrue(self.app.state.preparer.step())
        new = self.dojo.lessons_for(PID, S)[0]["id"]
        self.assertNotEqual(new, lesson)
        self.assertEqual([s["lesson"] for s in self.planned() if s["skill"] == S], [new], "to study or to hear, as it is narrated now")
        self.assertEqual(self.removed(), {})

    def test_a_pinned_lab_stays_when_every_page_its_skill_cites_is_gone(self):
        """The database checks a lab, not a page. The skill's pinned Lesson goes."""
        dp, sid = "dp-800", LABS["vector"]["skill"]
        self.dojo.make_lesson(lambda step: None, self.owner, dp, sid)
        self.rec.attempt(dp, sid, self.now - timedelta(days=2), "probe", 1)   # missed: the lesson is next
        steps = {s["kind"]: s["id"] for s in self.planned(dp) if s["skill"] == sid}
        self.assertEqual(set(steps), {"lesson", "lab"})
        for session_id in steps.values():
            self.planner.pin(self.owner, session_id, True, self.now)

        self.web.set(VECTOR_PATH, status=404)
        self.read_due()
        self.later(7)
        self.read_due()
        self.assertIn(sid, self.drift.gone_skills(dp))
        self.assertEqual({s["kind"]: s["id"] for s in self.planned(dp) if s["skill"] == sid}, {"lab": steps["lab"]})
        self.assertIn(steps["lab"], self.feed())
        self.assertNotIn(steps["lesson"], self.feed())
        lines = self.removed()
        self.assertEqual(lines[steps["lesson"]], ("Removed", "Its official pages are gone or moved"))
        self.assertNotIn(steps["lab"], lines)

    def test_a_pinned_session_that_is_only_no_longer_needed_stays(self):
        """ADR 0004 as it was: a session the learner pinned stays when it is no longer asked for, also while
        a page elsewhere is gone. Only what a source change makes lead nowhere goes."""
        self.lesson()
        self.rec.attempt(PID, OTHER, self.now - timedelta(days=2), "probe", 1)   # its lesson is next
        study = next(s for s in self.planned() if s["skill"] == OTHER)
        self.assertEqual(study["kind"], "lesson")
        self.planner.pin(self.owner, study["id"], True, self.now)
        self.rec.attempt(PID, OTHER, self.now - timedelta(minutes=10), "probe", 3)   # met on its own since
        self.gone()
        doc = self.planner.refresh(self.owner, self.now)
        needs, _, _, off = self.planner.needs(self.dojo.profile(self.owner), self.dojo.store.events(self.owner.key),
                                              self.now, doc["rules"])
        self.assertNotIn(f"teach:{PID}:{OTHER}", {n.key for n in needs}, "no longer asked for")
        self.assertNotIn((PID, OTHER), off)
        self.assertEqual(off[(PID, S)], "gone")
        kept = next(s for s in doc["sessions"] if s["id"] == study["id"])
        self.assertEqual((kept["state"], kept["pinned"]), ("planned", True))
        self.assertIn(study["id"], self.feed())
        self.assertNotIn(study["id"], self.removed())


if __name__ == "__main__":
    unittest.main()
