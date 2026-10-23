"""Studio for the owner (app/studio.py, docs/adr/0019-studio-overview.md): how far each course is, and what Dojo did on
its own. It only reads: these tests write lessons, audio and films the way Dojo does, and check what the two views say.
Nothing here touches the network (EG-39)."""
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

from fastapi.testclient import TestClient  # noqa: E402

import app.learning as learning_mod  # noqa: E402
import app.studio as studio_mod  # noqa: E402
from app.core import UserError, iso, new_id, parse_iso, utcnow  # noqa: E402
from tests.test_api import POST  # noqa: E402
from tests.test_newexam import Web, done, make_app, post  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"
SCRIPT = (APP_DIR / "static" / "app.js").read_text("utf-8")


def part(start: str) -> str:
    return SCRIPT.split(start, 1)[1].split("\n}\n", 1)[0]


class StudioCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-studio-")
        self.app = make_app(self.tmp, Web())
        self.c = TestClient(self.app)
        self.dojo, self.studio, self.check = self.app.state.dojo, self.app.state.studio, self.app.state.checker
        self.owner = self.app.state.drift.owner
        done(self.c, post(self.c, "/api/exam-packages", {"code": "XY-110"})["job"])
        self.pid = "xy-110"
        self.skills = [s for s in self.dojo.packages.get(self.pid)["skills"].values() if s["sources"]]
        self.assertGreaterEqual(len(self.skills), 3)

    def write(self, skill: dict, tag: str | None = None, **kw) -> str:
        """A lesson for the skill. The stand-in model writes the same words for every skill, so each lesson gets a line
        of its own: two lessons that say the same share their audio and their films, as Dojo's media ids are hashes of the words."""
        lesson = self.dojo.make_lesson(lambda step: None, self.owner, self.pid, skill["id"], **kw)["lesson"]
        doc = self.dojo.lesson(lesson)
        for slide in doc["slides"]:
            if slide.get("narration"):
                slide["narration"] = [{**line, "text": f"{line['text']} ({tag or skill['id']})"} for line in slide["narration"]]
            else:
                slide["bullets"] = list(slide.get("bullets") or []) + [f"({tag or skill['id']})"]
        self.dojo.store.write("content", "lessons", lesson, value=doc)
        self.check._plans.pop(lesson, None)
        return lesson

    def narrate(self, lesson: str) -> None:
        self.dojo.lesson_audio(lambda step: None, self.owner, lesson)

    def film(self, lesson: str) -> None:
        self.dojo.lesson_video(lambda step: None, self.owner, lesson)

    def read(self, view: str = "courses") -> dict:
        self.studio._seen.clear()
        r = self.c.get(f"/api/studio/{view}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def course(self, pid: str | None = None) -> dict:
        return next(x for x in self.read()["courses"] if x["package"] == (pid or self.pid))

    @staticmethod
    def row(course: dict, key: str) -> dict:
        return next(r for r in course["rows"] if r["key"] == key)


class CourseOverviewTests(StudioCase):
    def test_a_course_with_no_lesson_yet_says_so(self):
        course = self.course()
        self.assertFalse(course["ready"])
        lessons = self.row(course, "lessons")
        self.assertEqual((lessons["have"], lessons["of"], lessons["state"]), (0, len(self.skills), "warn"))
        self.assertEqual(self.row(course, "pages")["have"], len(self.skills))
        self.assertIn("can't be studied from a lesson yet", course["verdict"])
        self.assertEqual(course["video"], {"own": 0, "earlier": 0, "none": 0, "price": None})

    def test_every_lesson_with_its_audio_makes_a_course_ready_to_study(self):
        for s in self.skills:
            self.narrate(self.write(s))
        course = self.course()
        for key in ("lessons", "listen", "present"):
            row = self.row(course, key)
            self.assertEqual((row["have"], row["of"], row["state"]), (len(self.skills), len(self.skills), "ok"), key)
        self.assertEqual(course["ready"], len(self.skills) == course["skills"], "ready only when no skill is left without a page")
        if course["ready"]:
            self.assertEqual(course["verdict"], "Every skill has a lesson and audio.")

    def test_audio_that_is_not_there_yet_is_not_counted(self):
        first, second = self.write(self.skills[0]), self.write(self.skills[1])
        self.narrate(first)
        listen = self.row(self.course(), "listen")
        self.assertEqual((listen["have"], listen["of"], listen["state"]), (1, 2, "warn"))
        self.narrate(second)
        self.assertEqual(self.row(self.course(), "listen")["have"], 2)

    def test_a_price_that_is_not_complete_is_never_shown_as_the_cost(self):
        self.film(self.write(self.skills[0]))
        self.write(self.skills[1])
        guess = {"lessons": 1, "videos": 2, "total": 0.0, "complete": False, "currency": "USD", "lines": []}
        with mock.patch.object(self.app.state.costs, "film_estimate", lambda plans: guess):
            course = self.course()
        text = " ".join([self.row(course, "video")["note"]] + [a["text"] for a in course["attention"]])
        self.assertNotIn("$0.00", text)
        self.assertIn("The price list could not be read just now, so no price is shown.", text)

    def test_a_filmed_lesson_counts_and_the_price_of_the_rest_is_shown(self):
        a, b = self.write(self.skills[0]), self.write(self.skills[1])
        self.film(a)
        course = self.course()
        self.assertEqual((course["video"]["own"], course["video"]["none"]), (1, 1))
        watch = self.row(course, "video")
        self.assertEqual((watch["have"], watch["of"]), (1, 2))
        price = course["video"]["price"]
        self.assertEqual(price["lessons"], 1)
        self.assertGreater(price["videos"], 0)
        self.assertIn("you approve each price", watch["note"])
        self.assertFalse(any("no film of the current lesson" in a["text"] for a in course["attention"]), "the Videos row has the button")
        videos = course["make"]["videos"]
        self.assertEqual((videos["have"], videos["of"], videos["offer"]["lessons"]), (1, 2, 1))

    def test_a_rewritten_lesson_plays_the_film_of_the_earlier_version_and_says_so(self):
        old = self.write(self.skills[0])
        self.film(old)
        new = self.write(self.skills[0], tag="again", upgrade_of=old)
        doc = self.dojo.lesson(new)
        doc["created"] = iso(utcnow() + timedelta(hours=1))   # newer than the first, whatever the clock says
        self.dojo.store.write("content", "lessons", new, value=doc)
        self.dojo._lesson_meta.pop(new, None)
        course = self.course()
        self.assertEqual((course["video"]["own"], course["video"]["earlier"], course["video"]["none"]), (0, 1, 0))
        self.assertIn("play a film of an earlier version, with a note saying so", self.row(course, "video")["note"])

    def test_an_original_course_says_its_pages_have_not_had_the_two_model_check(self):
        course = self.course("gh-300")
        self.assertFalse(course["added"])
        self.assertIsNone(course["search"])
        self.assertIn("No page has been through the two-model page check", self.row(course, "pages")["note"])
        self.assertTrue(any("before Dojo's two-model page check" in a["text"] for a in course["attention"]))
        self.assertEqual(self.row(course, "exam")["state"], "ok")

    def test_an_added_course_says_how_far_the_search_for_pages_has_come(self):
        course = self.course()
        self.assertTrue(course["added"])
        self.assertEqual(set(course["search"]), {"covered", "legacy", "unfinished", "complete", "supported", "invalid", "weak", "unlooked"})
        self.assertIn("were found by Dojo's own search", self.row(course, "pages")["note"])

    def test_the_audit_of_older_pages_is_counted(self):
        raw = self.dojo.store.read("packages", self.pid)
        side = {"v": 1, "skills": {}}
        keys = [s["key"] for s in self.app.state.newexams.sourcer.skills_of(raw) if s["sources"]]
        side["skills"][keys[0]] = {"verdict": "supported", "pages": [], "at": iso()}
        side["skills"][keys[1]] = {"verdict": "invalid", "pages": [], "at": iso()}
        self.dojo.store.write("sourcing", self.pid, "side-audit", value=side)
        search = self.course()["search"]
        self.assertEqual((search["supported"], search["invalid"]), (1, 1))
        self.assertTrue(any("a page that both models refuse" in a["text"] for a in self.course()["attention"]))

    def test_pages_replaced_since_the_audit_are_not_counted_as_refused_and_the_text_says_what_happens(self):
        raw = self.dojo.store.read("packages", self.pid)
        skills = [s for s in self.app.state.newexams.sourcer.skills_of(raw) if s["sources"]]
        gone = {"verdict": "invalid", "pages": [{"url": skills[1]["sources"][0]["url"] + "-gone"}], "at": iso()}   # the skill cites other pages now
        still = {"verdict": "invalid", "pages": [{"url": skills[0]["sources"][0]["url"]}], "at": iso()}
        self.dojo.store.write("sourcing", self.pid, "side-audit", value={"v": 1, "skills": {skills[0]["key"]: still, skills[1]["key"]: gone}})
        search = self.course()["search"]
        self.assertEqual(search["invalid"], 1, "pages that were replaced are history")
        [text] = [a["text"] for a in self.course()["attention"] if "both models refuse" in a["text"]]
        self.assertIn("writes the lesson again", text)
        self.assertIn("offered for filming again on this page, at its price", text)

    def test_courses_keep_their_order_whichever_exam_is_the_active_one(self):
        orders = []
        for pid in ("gh-300", "dp-800", self.pid):
            self.dojo.save_profile(self.owner, {"active_package": pid})
            orders.append([x["package"] for x in self.read()["courses"]])
        self.assertEqual(orders[0], orders[1], "picking another exam does not move a course on Studio (ADR 0022)")
        self.assertEqual(orders[1], orders[2])
        self.assertEqual(set(orders[0]), {"gh-300", "dp-800", self.pid})
        self.assertEqual(orders[0], self.dojo.exam_order(self.owner, active_first=False))

    def test_the_work_dojo_does_still_goes_to_the_active_exam_first(self):
        self.dojo.save_profile(self.owner, {"active_package": self.pid})
        self.assertEqual(self.dojo.exam_order(self.owner)[0], self.pid)
        self.assertNotEqual(self.dojo.exam_order(self.owner, active_first=False)[0], self.pid, "an added exam comes after the built-in ones in the catalog")

    def test_what_a_reading_costs_is_nothing_but_reading(self):
        def files() -> list[str]:
            # the public price list is kept by the cost page and the estimates alike: it is the one thing a reading can leave
            return sorted(str(p.relative_to(self.tmp)) for p in Path(self.tmp).rglob("*") if p.is_file() and p.name != "prices.json")

        before = files()
        self.read("courses")
        self.read("activity")
        self.assertEqual(before, files(), "Studio's overview writes no record of its own")

    def test_both_views_are_closed_while_a_timed_exam_runs(self):
        self.app.state.exams.open_attempt = lambda learner: {"id": "exam-00000000000000000000"}
        for view in ("courses", "activity"):
            self.studio._seen.clear()
            r = self.c.get(f"/api/studio/{view}")
            self.assertEqual(r.status_code, 409, view)
            self.assertIn("timed practice exam", r.json()["detail"])


class MakeTests(StudioCase):
    """What the Courses page lets the owner make, and what it asks first (ADR 0020)."""

    def ready(self, count: int = 2) -> list[str]:
        lessons = [self.write(s) for s in self.skills[:count]]
        for lesson in lessons:
            self.narrate(lesson)
        return lessons

    def videos(self, pid: str | None = None) -> dict:
        return self.course(pid)["make"]["videos"]

    def press(self, token: str, status: int = 200, pid: str | None = None) -> dict:
        r = self.c.post(f"/api/studio/film/{pid or self.pid}", json={"confirm": "FILM", "offer": token}, headers=POST)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_a_course_offers_the_lessons_without_a_film_at_a_price_and_names_them_with_a_token(self):
        self.ready()
        videos = self.videos()
        offer = videos["offer"]
        self.assertEqual((videos["have"], videos["of"], offer["lessons"]), (0, 2, 2))
        self.assertGreater(offer["videos"], 0)
        self.assertGreater(offer["usd"], 0)
        self.assertTrue(offer["complete"])
        self.assertRegex(offer["token"], r"^[0-9a-f]{16}$")
        self.assertTrue(offer["fits"])
        self.assertEqual(offer["eta_minutes"], round(2 * (studio_mod.FILM_MINUTES + studio_mod.GAP_ADDED_MIN)))
        make = self.read()["make"]
        self.assertEqual([k["package"] for k in make["films"]["courses"]], [self.pid], "only the course with lessons to film")
        self.assertEqual((make["films"]["lessons"], make["films"]["usd"]), (2, offer["usd"]))
        self.assertTrue(make["films"]["priced"] and make["films"]["fits"])
        self.assertEqual(set(make["storage"]), {"videos", "bytes", "budget_bytes", "limit_bytes", "average_bytes", "queued_bytes", "free_bytes"})
        self.assertFalse(make["busy"], "nothing is being made")

    def test_pressing_the_button_approves_the_set_and_price_shown_and_nothing_else(self):
        self.ready()
        offer = self.videos()["offer"]
        shown = offer["usd"]
        body = self.press(offer["token"])
        videos = next(x for x in body["courses"] if x["package"] == self.pid)["make"]["videos"]
        self.assertEqual((videos["waiting"], videos["filming"], videos["offer"]), (2, 0, None))
        self.assertEqual(videos["eta_minutes"], round(2 * (studio_mod.FILM_MINUTES + studio_mod.GAP_ADDED_MIN)))
        self.assertTrue(body["make"]["busy"], "the page keeps looking while films wait")
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["waiting"], 2)
        self.assertEqual(self.app.state.newexams._doc(self.pid)["film"]["approvals"][-1]["total"], shown, "what is written down is the price the page showed")
        self.assertEqual([j for j in self.dojo.jobs.for_learner(self.owner) if j["kind"] == "video"], [], "approving files nothing by itself")
        self.assertIn("nothing new to film", self.press("0" * 16, 409)["detail"])

    def test_a_token_for_another_set_or_price_is_refused(self):
        self.ready()
        token = self.videos()["offer"]["token"]
        path = f"/api/studio/film/{self.pid}"
        self.assertEqual(self.c.post(path, json={"offer": token}, headers=POST).status_code, 422, "the word is needed")
        self.assertEqual(self.c.post(path, json={"confirm": "FILM", "offer": "not-a-token"}, headers=POST).status_code, 422)
        self.assertEqual(self.c.post(path, json={"confirm": "FILM", "offer": token}).status_code, 403, "the page's own header is needed")
        self.assertIn("changed", self.press("0" * 16, 409)["detail"])
        real = self.app.state.costs.film_estimate
        with mock.patch.object(self.app.state.costs, "film_estimate", lambda plans: {**real(plans), "total": real(plans)["total"] + 1.0}):
            self.assertIn("price changed", self.press(token, 409)["detail"], "the same lessons at another price are another offer")
        self.write(self.skills[2])
        self.press(token, 409)
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["waiting"], 0, "none of the refused presses approved anything")

    def test_a_price_that_cannot_be_worked_out_is_never_approved(self):
        self.ready()
        real = self.app.state.costs.film_estimate
        with mock.patch.object(self.app.state.costs, "film_estimate", lambda plans: {**real(plans), "complete": False}):
            self.assertIsNone(self.videos()["offer"]["token"])
            self.assertFalse(self.read()["make"]["films"]["priced"])
            self.assertIn("could not be worked out", self.press("0" * 16, 409)["detail"])

    def test_films_that_would_not_fit_are_not_approved_and_the_page_says_how_much_room_is_left(self):
        self.ready()
        full = {"videos": 100, "bytes": 5_300_000_000, "budget_bytes": 6_000_000_000}
        with mock.patch.object(self.dojo.avatar, "usage", return_value=full):
            body = self.read()
            offer = next(x for x in body["courses"] if x["package"] == self.pid)["make"]["videos"]["offer"]
            self.assertFalse(offer["fits"])
            self.assertFalse(body["make"]["films"]["fits"])
            self.assertEqual(body["make"]["storage"]["free_bytes"], 100_000_000, "filming pauses at 90% of the budget")
            self.assertIn("not room", self.press(offer["token"], 409)["detail"])
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["waiting"], 0)

    def test_films_already_approved_take_room_before_they_are_made(self):
        self.ready()
        self.press(self.videos()["offer"]["token"])
        storage = self.read()["make"]["storage"]
        self.assertEqual(storage["queued_bytes"], int(4 * storage["average_bytes"] * studio_mod.FILM_MARGIN), "two lessons of two films each, with the margin offers get")
        self.assertEqual(storage["free_bytes"], storage["limit_bytes"] - storage["bytes"] - storage["queued_bytes"])

    def test_a_queued_lesson_with_one_film_stored_takes_room_for_the_other_only(self):
        lesson = self.ready(1)[0]
        self.press(self.videos()["offer"]["token"])
        self.assertEqual(self.read()["make"]["storage"]["queued_bytes"], int(2 * studio_mod.AVG_VIDEO_BYTES * studio_mod.FILM_MARGIN))
        lines = [self.dojo._slide_lines(sl) for sl in self.dojo.lesson(lesson)["slides"]]
        self.dojo.avatar.synthesize(*self.dojo.avatar.lesson_films(lines)[0])
        storage = self.read()["make"]["storage"]
        self.assertEqual(storage["queued_bytes"], int(1 * storage["average_bytes"] * studio_mod.FILM_MARGIN), "the film that is stored takes its room already")

    def test_stop_forgets_the_films_that_have_not_started_and_leaves_the_one_being_made(self):
        first = self.ready()[0]
        self.press(self.videos()["offer"]["token"])
        job = new_id("job")
        self.dojo.store.write("jobs", job, value={"id": job, "kind": "video", "learner": self.owner.key, "ref": first, "state": "running",
                                                  "created": iso(), "heartbeat": iso()})
        self.app.state.newexams.filed(first, job)   # one of them is being made
        r = self.c.post(f"/api/studio/film/{self.pid}/stop", headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["stopped"], 1)
        videos = next(x for x in body["courses"] if x["package"] == self.pid)["make"]["videos"]
        self.assertEqual((videos["waiting"], videos["filming"], videos["offer"]["lessons"]), (0, 1, 1), "the stopped lesson can be approved again")
        self.assertEqual(self.c.post("/api/studio/film/no-such-course/stop", headers=POST).status_code, 404)

    def test_make_approves_each_course_on_its_own_and_says_what_happened_to_each(self):
        self.ready()
        token = self.videos()["offer"]["token"]
        r = self.c.post("/api/studio/make", json={"confirm": "MAKE", "decks": True, "films": [
            {"package": self.pid, "offer": token}, {"package": "gh-300", "offer": "0" * 16}]}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        results = r.json()["results"]
        self.assertEqual([(x["what"], x["ok"]) for x in results], [("films", True), ("films", False), ("diagrams", True)])
        self.assertIn("nothing new to film", results[1]["error"])
        self.assertEqual((results[0]["exam"], results[1]["exam"]), ("XY-110", "GH-300"))
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["waiting"], 2, "a course that was refused did not stop the other")
        self.assertTrue(self.app.state.preparer.deck_ahead())
        self.assertEqual(self.c.post("/api/studio/make", json={"decks": True}, headers=POST).status_code, 422)

    def test_a_lesson_a_changed_page_holds_back_is_not_offered_or_filmed_for_an_added_exam(self):
        a, b = self.ready()
        drift, films = self.app.state.drift, self.app.state.newexams
        with mock.patch.object(drift, "withheld", lambda lesson: lesson == a):
            videos = self.videos()
            self.assertEqual((videos["offer"]["lessons"], videos["later"]), (1, {"held": 1}))
        token = self.videos()["offer"]["token"]
        self.press(token)
        self.assertEqual(films.next_film(), a)
        with mock.patch.object(drift, "withheld", lambda lesson: lesson == a):
            self.assertEqual(films.next_film(), b, "approved, then held back by a changed page: it waits to be written again")
        with mock.patch.object(drift, "withheld", lambda lesson: True):
            self.assertIsNone(films.next_film())

    def rewrite(self, old: str, skill: dict) -> str:
        """The lesson written again: newer than the first, whatever the clock says."""
        new = self.write(skill, tag="again", upgrade_of=old)
        doc = self.dojo.lesson(new)
        doc["created"] = iso(utcnow() + timedelta(hours=1))
        self.dojo.store.write("content", "lessons", new, value=doc)
        self.dojo._lesson_meta.pop(new, None)
        return new

    def test_a_lesson_written_again_this_week_is_left_out_of_the_offer_with_the_day_it_joins(self):
        a, b = self.ready()
        new = self.rewrite(a, self.skills[0])
        self.narrate(new)
        videos = self.videos()
        self.assertEqual((videos["offer"]["lessons"], videos["later"]), (1, {"quiet": 1}), "only the lesson nobody rewrote is offered")
        until = parse_iso(videos["quiet_until"])
        self.assertAlmostEqual((until - utcnow()).total_seconds(), (learning_mod.QUIET_DAYS * 86400) + 3600, delta=120)
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["later"]["quiet"], {new: videos["quiet_until"]})
        later = utcnow() + timedelta(days=learning_mod.QUIET_DAYS, hours=2)
        with mock.patch.object(learning_mod, "utcnow", lambda: later):
            videos = self.videos()
        self.assertEqual((videos["offer"]["lessons"], videos["later"], videos["quiet_until"]), (2, {}, None), "a quiet week later it joins")

    def test_an_offer_says_how_much_of_it_replaces_a_video_the_owner_already_has(self):
        a, b = self.ready()
        self.film(a)
        new = self.rewrite(a, self.skills[0])
        self.narrate(new)
        later = utcnow() + timedelta(days=learning_mod.QUIET_DAYS + 1)
        with mock.patch.object(learning_mod, "utcnow", lambda: later):
            course = self.course()
            offer = course["make"]["videos"]["offer"]
        self.assertEqual(course["video"]["earlier"], 1, "Watch plays the earlier version's film of the rewritten lesson")
        self.assertEqual(offer["lessons"], 2)
        self.assertEqual((offer["replaces"]["lessons"], offer["fresh"]["lessons"]), (1, 1))
        self.assertGreater(offer["replaces"]["usd"], 0)
        self.assertAlmostEqual(offer["replaces"]["usd"] + offer["fresh"]["usd"], offer["usd"], places=2, msg="the two add up to the price on the button")

    def unreadable_books(self):
        """The writer of the deeper lessons cannot read its books: which lessons are about to be rewritten is not known."""
        books = mock.Mock()
        books.to_write_again.side_effect = RuntimeError("the books are unreadable")
        return mock.patch.object(self.dojo, "deeper", books, create=True)

    def test_when_the_lessons_about_to_be_rewritten_cannot_be_read_no_film_is_offered_or_approved(self):
        a, b = self.ready()
        token = self.videos()["offer"]["token"]
        with self.unreadable_books():
            course = self.course()
            self.assertIsNone(course["make"]["videos"], "no offer: it fails closed, and the rest of the page is still there")
            self.assertEqual(course["skills"], len(self.dojo.packages.get(self.pid)["skills"]))
            self.assertIsNone(self.app.state.newexams.next_film(), "nothing is approved, so there is nothing to choose and nothing to read")
            self.assertIn("cannot tell just now which lessons are about to be rewritten", self.press(token, 422)["detail"], "said in plain words, not as a server error")
            self.assertEqual(self.c.get(f"/api/exam-packages/{self.pid}").status_code, 200, "the exam page still draws")
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["waiting"], 0, "nothing was approved")

    def test_the_exam_page_still_says_what_was_rewritten_this_week_when_the_other_list_cannot_be_read(self):
        a, b = self.ready()
        self.narrate(self.rewrite(a, self.skills[0]))
        with self.unreadable_books():
            film = self.c.get(f"/api/exam-packages/{self.pid}").json()["film"]
        self.assertEqual(film["later"]["quiet"], 1, "the quiet week does not depend on the list that could not be read")
        self.assertEqual(film["later"]["scheduled"], 0)

    def test_an_approved_film_waits_while_the_lessons_about_to_be_rewritten_cannot_be_read(self):
        a, b = self.ready()
        self.press(self.videos()["offer"]["token"])
        films, prep = self.app.state.newexams, self.app.state.preparer
        with self.unreadable_books():
            with self.assertRaises(UserError):
                films.next_film()
            with mock.patch.object(prep, "_quiet_for", return_value=1e9):
                self.assertTrue(prep._film(), "approved films are still pending: the Preparer looks again soon")
            self.assertIn("The films wait", prep.state["film_note"])
            self.assertEqual(self.videos_jobs(), [], "nothing was filed")
        self.assertEqual(films.next_film(), a, "readable again: the film is chosen")

    def videos_jobs(self) -> list[dict]:
        return [j for j in self.dojo.jobs.for_learner(self.owner) if j.get("kind") == "video"]

    def test_one_course_that_cannot_be_approved_does_not_stop_the_diagrams(self):
        self.ready()
        token = self.videos()["offer"]["token"]
        with self.unreadable_books():
            r = self.c.post("/api/studio/make", json={"confirm": "MAKE", "decks": True, "films": [{"package": self.pid, "offer": token}]}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        films, decks = r.json()["results"]
        self.assertEqual((films["what"], films["ok"]), ("films", False))
        self.assertIn("cannot tell just now which lessons are about to be rewritten", films["error"])
        self.assertEqual((decks["what"], decks["ok"]), ("diagrams", True), "each selection starts on its own")

    def test_films_done_or_obsolete_read_nothing_when_the_rewrite_list_cannot_be_read(self):
        a, b = self.ready()
        self.press(self.videos()["offer"]["token"])
        films = self.app.state.newexams
        self.film(a)
        self.film(b)   # both approved films are made: nothing is pending
        with self.unreadable_books():
            self.assertIsNone(films.next_film(), "no film is pending, so nothing has to be known about the rewrites")
            with mock.patch.object(self.app.state.preparer, "_quiet_for", return_value=1e9):
                self.assertFalse(self.app.state.preparer._film(), "and the Preparer is not told there is work")

    def test_an_approval_for_a_lesson_written_again_since_is_forgotten_by_stop_but_not_counted(self):
        a, b = self.ready()
        self.press(self.videos()["offer"]["token"])
        self.narrate(self.rewrite(a, self.skills[0]))   # a is no longer the newest of its skill: its approval will never be filmed
        films = self.app.state.newexams
        r = self.c.post(f"/api/studio/film/{self.pid}/stop", headers=POST)
        self.assertEqual(r.json()["stopped"], 1, "only the film that was really waiting is counted")
        self.assertEqual(films._doc(self.pid)["film"]["approved"], [], "the obsolete approval is gone too")

    def test_the_sourcing_status_is_read_once_for_the_pages_row_and_the_films_offer(self):
        self.ready()
        sourcer = self.app.state.newexams.sourcer
        sourcer._summaries.clear()
        with mock.patch.object(sourcer, "status", wraps=sourcer.status) as status:
            self.read("courses")
        self.assertEqual(status.call_count, 1, "every skill's document is read to make it")

    def test_an_approved_lesson_a_changed_page_holds_back_still_waits_and_stop_forgets_it(self):
        a, b = self.ready()
        self.press(self.videos()["offer"]["token"])
        drift, films = self.app.state.drift, self.app.state.newexams
        with mock.patch.object(drift, "withheld", lambda lesson: lesson == a):
            self.assertEqual(films.film_state(self.pid)["waiting"], 2, "held back, but still approved and waiting")
            self.assertEqual(self.c.get(f"/api/exam-packages/{self.pid}").json()["film"]["later"]["held"], 1, "the exam page says why one is not offered")
            r = self.c.post(f"/api/studio/film/{self.pid}/stop", headers=POST)
            self.assertEqual(r.json()["stopped"], 2, "Stop forgets the held approval too: it would otherwise film when the page is steady again")
        self.assertEqual(films._doc(self.pid)["film"]["approved"], [])

    def test_stop_still_works_when_the_lessons_about_to_be_rewritten_cannot_be_read(self):
        self.ready()
        self.press(self.videos()["offer"]["token"])
        with self.unreadable_books():
            r = self.c.post(f"/api/studio/film/{self.pid}/stop", headers=POST)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["stopped"], 2)
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["waiting"], 0)

    def test_an_approved_film_whose_pages_are_refused_meanwhile_waits(self):
        a, b = self.ready()
        self.press(self.videos()["offer"]["token"])
        films, sourcer = self.app.state.newexams, self.app.state.newexams.sourcer
        self.assertEqual(films.next_film(), a)
        with mock.patch.object(sourcer, "unsettled", return_value={self.skills[0]["id"]: "its pages were refused and will be replaced"}):
            self.assertEqual(films.next_film(), b, "its pages may be replaced: it waits, and the next lesson is filmed")
        with mock.patch.object(sourcer, "unsettled", return_value={self.skills[0]["id"]: "x", self.skills[1]["id"]: "y"}):
            self.assertIsNone(films.next_film())

    def test_the_exam_page_says_how_many_lessons_are_left_out_and_why(self):
        a, b = self.ready()
        new = self.rewrite(a, self.skills[0])
        self.narrate(new)
        film = self.c.get(f"/api/exam-packages/{self.pid}").json()["film"]
        self.assertEqual(film["later"], {"quiet": 1, "scheduled": 0, "pages": 0, "held": 0})
        with mock.patch.object(self.dojo, "scheduled_rewrites", return_value={b}):
            self.assertEqual(self.c.get(f"/api/exam-packages/{self.pid}").json()["film"]["later"], {"quiet": 1, "scheduled": 1, "pages": 0, "held": 0})

    def test_a_first_lesson_is_offered_at_once(self):
        a, b = self.ready()
        self.assertEqual(self.videos()["offer"]["lessons"], 2)
        self.assertEqual(self.dojo.rewritten_recently(), {}, "nothing replaced anything")

    def test_a_token_from_before_a_lesson_joined_the_offer_is_refused(self):
        a, b = self.ready()
        new = self.rewrite(a, self.skills[0])
        self.narrate(new)
        token = self.videos()["offer"]["token"]
        later = utcnow() + timedelta(days=learning_mod.QUIET_DAYS, hours=2)
        with mock.patch.object(learning_mod, "utcnow", lambda: later):
            self.assertIn("changed", self.press(token, 409)["detail"])
        self.assertEqual(self.app.state.newexams.film_state(self.pid)["waiting"], 0, "nothing was approved")

    def test_a_lesson_that_will_be_written_again_on_the_deeper_template_is_not_offered(self):
        a, b = self.ready()
        with mock.patch.object(self.dojo, "scheduled_rewrites", return_value={a}):
            videos = self.videos()
            self.assertEqual((videos["offer"]["lessons"], videos["later"]), (1, {"scheduled": 1}))
        with mock.patch.object(self.dojo, "scheduled_rewrites", return_value=set()):
            token = self.videos()["offer"]["token"]
            self.press(token)
        films = self.app.state.newexams
        with mock.patch.object(self.dojo, "scheduled_rewrites", return_value={a}):
            self.assertEqual(films.next_film(), b, "approved, then scheduled to be written again: it waits for that")
            self.assertEqual(self.videos()["waiting"], 2, "it is still counted as waiting")

    def test_a_lesson_whose_pages_may_still_be_replaced_is_not_offered(self):
        a, b = self.ready()
        sourcer = self.app.state.newexams.sourcer
        with mock.patch.object(sourcer, "unsettled", return_value={self.skills[1]["id"]: "its pages are still being searched"}):
            videos = self.videos()
            self.assertEqual((videos["offer"]["lessons"], videos["later"]), (1, {"pages": 1}))
        self.assertEqual(self.videos()["offer"]["lessons"], 2)

    def test_a_film_is_chosen_started_and_written_down_under_the_lock_stop_takes(self):
        self.ready()
        self.press(self.videos()["offer"]["token"])
        films, prep = self.app.state.newexams, self.app.state.preparer
        seen = []

        def start(*args, **kwargs):
            seen.append(films._lock._is_owned())
            return {"id": new_id("job"), "state": "queued"}

        with mock.patch.object(prep, "_quiet_for", return_value=1e9), mock.patch.object(self.dojo.jobs, "start", side_effect=start):
            self.assertTrue(prep._film())
        self.assertEqual(seen, [True], "Stop waits for a film that is being filed")

    def test_films_approved_on_the_checks_page_for_an_added_exam_before_are_counted_and_stopped(self):
        first = self.ready()[0]
        drift = self.app.state.drift
        drift._set_film(first, state="approved", approved=iso(), title="t", package=self.pid, skill=self.skills[0]["id"], replaces=None,
                        job=None, tries=0, error=None)
        videos = self.videos()
        self.assertEqual((videos["waiting"], videos["offer"]), (1, None), "nothing is offered twice: the older approval is stopped or finished first")
        self.assertGreater(self.read()["make"]["storage"]["queued_bytes"], 0)
        stopped = self.c.post(f"/api/studio/film/{self.pid}/stop", headers=POST).json()
        self.assertEqual(stopped["stopped"], 1)
        self.assertEqual(drift._doc()["films"], {})
        self.assertEqual(self.videos()["offer"]["lessons"], 2)

    def test_the_diagrams_are_not_switched_on_at_a_price_that_could_not_be_worked_out(self):
        self.ready()
        unpriced = {"every_lesson": {"per_lesson": None, "complete": False, "total": 0.0, "lessons": 2}}
        with mock.patch.object(self.app.state.costs, "deck_view", return_value=unpriced):
            r = self.c.post("/api/studio/make", json={"confirm": "MAKE", "decks": True, "films": []}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        results = r.json()["results"]
        self.assertEqual([(x["what"], x["ok"]) for x in results], [("diagrams", False)])
        self.assertIn("could not be worked out", results[0]["error"])
        self.assertFalse(self.app.state.preparer.deck_ahead())

    def test_two_clicks_on_film_this_lesson_cannot_start_two_jobs(self):
        lesson = self.ready(1)[0]
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(progress, learner, lesson_id):
            release.wait(20)
            return {"lesson": lesson_id}

        with mock.patch.object(self.dojo, "lesson_video", slow):
            first = self.c.post(f"/api/lessons/{lesson}/video", headers=POST)
            self.assertEqual(first.status_code, 200, first.text)
            with mock.patch.object(self.dojo.jobs, "existing", return_value=None):   # the second click read the jobs before the first one started
                second = self.c.post(f"/api/lessons/{lesson}/video", headers=POST)
        self.assertEqual(second.status_code, 409)
        self.assertIn("already being filmed", second.json()["detail"])
        self.assertEqual(len([j for j in self.dojo.jobs.for_learner(self.owner) if j["kind"] == "video"]), 1, "one paid job")

    def test_the_lesson_page_does_not_film_a_lesson_that_is_certainly_replaced(self):
        a, b = self.ready()
        new = self.rewrite(a, self.skills[0])
        self.narrate(new)
        drift = self.app.state.drift
        started = []
        with mock.patch.object(self.dojo, "lesson_video", lambda progress, learner, lesson_id: started.append(lesson_id) or {"lesson": lesson_id}):
            old = self.c.post(f"/api/lessons/{a}/video", headers=POST)
            self.assertEqual(old.status_code, 409, "an older version of the lesson")
            self.assertIn("newer version", old.json()["detail"])
            with mock.patch.object(drift, "withheld", lambda lesson: lesson == b):
                held = self.c.post(f"/api/lessons/{b}/video", headers=POST)
            self.assertEqual(held.status_code, 409, "a lesson a changed page holds back")
            self.assertIn("writing this lesson again", held.json()["detail"])
            self.assertEqual(self.c.post(f"/api/lessons/{new}/video", headers=POST).status_code, 200, "a lesson rewritten lately may be filmed: the owner chose it, knowing")
        self.assertEqual([x for x in started if x in (a, b)], [])

    def test_the_diagrams_are_one_line_for_all_courses(self):
        self.ready()
        decks = self.read()["make"]["decks"]
        self.assertFalse(decks["ahead"])
        self.assertEqual(decks["ready"], 0)
        self.assertGreater(decks["lessons"], 0)
        self.assertEqual(decks["remaining"], decks["lessons"])
        self.assertEqual(decks["per_day"], 20)
        self.assertEqual(decks["days"], -(-decks["remaining"] // 20))
        self.assertIn("usd", decks)
        self.c.post("/api/studio/make", json={"confirm": "MAKE", "decks": True, "films": []}, headers=POST)
        after = self.read()["make"]
        self.assertTrue(after["decks"]["ahead"])
        self.assertTrue(after["busy"], "the page keeps looking while diagrams are drawn")

    def test_the_older_film_buttons_are_disabled_when_their_price_is_not_complete(self):
        watch = SCRIPT.split("const priced = !!(L.film_price && L.film_price.complete);", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if (!priced) {", watch)
        self.assertIn("btn.disabled = true;", watch.split("if (!priced) {", 1)[1])

    def test_the_routes_are_closed_while_a_timed_exam_runs(self):
        self.app.state.exams.open_attempt = lambda learner: {"id": "exam-00000000000000000000"}
        for path, body in ((f"/api/studio/film/{self.pid}", {"confirm": "FILM", "offer": "0" * 16}), (f"/api/studio/film/{self.pid}/stop", None),
                           ("/api/studio/make", {"confirm": "MAKE"})):
            r = self.c.post(path, json=body, headers=POST)
            self.assertEqual(r.status_code, 409, path)
            self.assertIn("timed practice exam", r.json()["detail"])

    def test_the_lesson_page_shows_what_filming_it_costs_without_going_to_the_network(self):
        lesson = self.ready(1)[0]
        self.dojo.store.delete("content", "prices")
        with mock.patch.object(self.app.state.costs, "_fetch", side_effect=AssertionError("a lesson went to the network")):
            self.assertEqual(self.c.get(f"/api/lessons/{lesson}").json()["film_price"], {"usd": 0.0, "complete": False, "later": None},
                             "no price list has been kept yet: no price, and no fetch")
        self.read()   # the Courses page reads the price list
        shown = self.c.get(f"/api/lessons/{lesson}").json()["film_price"]
        self.assertTrue(shown["complete"])
        self.assertGreater(shown["usd"], 0)
        self.assertLess(shown["usd"], self.videos()["offer"]["usd"] + 0.01, "one lesson of the two that are offered")
        self.film(lesson)
        self.assertIsNone(self.c.get(f"/api/lessons/{lesson}").json()["film_price"], "filmed: nothing to price")

    def test_the_lesson_page_says_when_a_lesson_is_likely_to_be_replaced(self):
        a, b = self.ready()
        self.read()   # the Courses page reads the price list
        new = self.rewrite(a, self.skills[0])
        self.narrate(new)
        later = self.c.get(f"/api/lessons/{new}").json()["film_price"]["later"]
        self.assertEqual(later["kind"], "quiet")
        self.assertEqual(later["until"], self.dojo.rewritten_recently()[new])
        self.assertIsNone(self.c.get(f"/api/lessons/{b}").json()["film_price"]["later"])
        with mock.patch.object(self.dojo, "scheduled_rewrites", return_value={b}):
            self.assertEqual(self.c.get(f"/api/lessons/{b}").json()["film_price"]["later"], {"kind": "scheduled", "until": None})

    def test_a_lesson_with_one_of_its_two_films_is_priced_for_the_other(self):
        lesson = self.ready(1)[0]
        whole = self.videos()["offer"]
        lines = [self.dojo._slide_lines(sl) for sl in self.dojo.lesson(lesson)["slides"]]
        jobs = self.dojo.avatar.lesson_films(lines)
        self.assertEqual(len(jobs), 2)
        self.dojo.avatar.synthesize(*jobs[0])
        part = self.videos()["offer"]
        self.assertEqual((whole["videos"], part["videos"]), (2, 1))
        self.assertLess(part["usd"], whole["usd"])
        self.assertEqual(part["lessons"], 1, "still a lesson to film")

    def test_the_newest_lesson_of_a_skill_is_the_one_lessons_for_puts_first(self):
        a, b = self.write(self.skills[0], tag="a"), self.write(self.skills[0], tag="b")
        for lesson in (a, b):
            doc = self.dojo.lesson(lesson)
            doc["created"] = "2026-10-16T10:00:00+00:00"   # the same second
            self.dojo.store.write("content", "lessons", lesson, value=doc)
            self.dojo._lesson_meta.pop(lesson, None)
        first = self.dojo.lessons_for(self.pid, self.skills[0]["id"])[0]["id"]
        self.film(first)
        course = self.course()
        self.assertEqual((course["video"]["own"], self.videos()["have"]), (1, 1), "the lesson the films are asked for is the one counted")

    def test_the_pace_of_a_film_is_the_middle_of_the_finished_ones_and_three_minutes_until_there_are_five(self):
        self.assertEqual(self.studio._pace(), studio_mod.FILM_MINUTES)
        start = utcnow() - timedelta(days=1)
        for minutes in (2, 2, 3, 9, 30):
            job = new_id("job")
            self.dojo.store.write("jobs", job, value={"id": job, "kind": "video", "learner": self.owner.key, "ref": "x", "state": "done",
                                                      "created": iso(start), "heartbeat": iso(start + timedelta(minutes=minutes))})
        self.studio._paced = None
        self.assertEqual(self.studio._pace(), 3.0)


class ActivityTests(StudioCase):
    def test_the_lessons_written_are_listed_newest_first(self):
        first = self.write(self.skills[0])
        second = self.write(self.skills[1])
        events = [e for e in self.read("activity")["events"] if e["kind"] == "lesson"]
        self.assertEqual({e["link"] for e in events}, {f"#/lesson/{first}", f"#/lesson/{second}"})
        self.assertEqual([e["at"] for e in events], sorted((e["at"] for e in events), reverse=True))
        self.assertEqual({e["course"] for e in events}, {"XY-110"})
        self.assertTrue(all(e["title"].startswith("Wrote the lesson") and e["tone"] == "info" for e in events))

    def test_a_lesson_written_again_says_so_whatever_made_it(self):
        old = self.write(self.skills[0])
        self.write(self.skills[0], upgrade_of=old)   # on the deeper template
        titles = [e["title"] for e in self.read("activity")["events"] if e["kind"] == "lesson"]
        self.assertEqual(sum(1 for t in titles if t.endswith(" again")), 1)

    def test_two_lessons_made_in_the_same_second_are_both_first_writes(self):
        # `created` is to the second and an id says nothing about order: a tie is not a rewrite, whichever id sorts first
        ids = [self.write(self.skills[2]), self.write(self.skills[2], tag="other")]
        for lesson in ids:
            doc = self.dojo.lesson(lesson)
            doc["created"] = "2026-10-16T10:00:00+00:00"
            self.dojo.store.write("content", "lessons", lesson, value=doc)
            self.dojo._lesson_meta.pop(lesson, None)
        with mock.patch("app.studio.utcnow", lambda: utcnow()), mock.patch("app.studio.FEED_DAYS", 3650):
            titles = [e["title"] for e in self.read("activity")["events"] if e["kind"] == "lesson" and e["link"] in {f"#/lesson/{i}" for i in ids}]
        self.assertEqual(len(titles), 2)
        self.assertFalse([t for t in titles if t.endswith(" again")])

    def test_a_lesson_written_after_a_page_changed_or_by_hand_is_again_too(self):
        # no `upgrade_of`: what the drift check and the owner's own rewrite make
        first = self.write(self.skills[1])
        second = self.write(self.skills[1], tag="rewritten")
        doc = self.dojo.lesson(second)
        doc["created"] = iso(utcnow() + timedelta(hours=1))
        self.dojo.store.write("content", "lessons", second, value=doc)
        self.dojo._lesson_meta.pop(second, None)
        again = {e["link"]: e["title"].endswith(" again") for e in self.read("activity")["events"] if e["kind"] == "lesson"}
        self.assertEqual((again[f"#/lesson/{first}"], again[f"#/lesson/{second}"]), (False, True))

    def test_what_the_search_for_pages_did_is_listed_with_the_pages_it_found(self):
        events = [e for e in self.read("activity")["events"] if e["kind"] == "pages"]
        raw = self.dojo.store.read("packages", self.pid)
        for e in events:
            self.assertTrue(e["title"].startswith("Found official pages for"))
            self.assertRegex(e["detail"], r"^\d+ pages?")
        self.assertLessEqual(len(events), len(self.app.state.newexams.sourcer.skills_of(raw)))

    def test_what_the_search_did_about_pages_that_moved_or_went_is_listed_beside_the_pages_it_found(self):
        sourcer = self.app.state.newexams.sourcer
        done = [{"at": iso(), "skill": "Compare widgets", "state": "followed", "to": "https://learn.microsoft.com/en-us/x/new"},
                {"at": iso(), "skill": "Size a widget", "state": "searching", "to": None}]
        with mock.patch.object(sourcer, "follows", return_value=done):
            events = [e for e in self.read("activity")["events"] if e["kind"] == "pages" and not e["title"].startswith("Found")]
        by = {e["title"]: e for e in events}
        took = by["Took over the new page for “Compare widgets”"]
        self.assertEqual((took["tone"], took["course"]), ("info", "XY-110"))
        self.assertIn("The lesson will be rewritten from it.", took["detail"])
        looking = by["Started looking for a new page for “Size a widget”"]
        self.assertEqual(looking["tone"], "warn")
        self.assertEqual(looking["detail"], "Every official page it was written from has moved or is gone.")

    def test_a_failed_job_of_the_owner_is_listed_and_another_learners_job_never_is(self):
        store = self.dojo.store
        now = iso()
        store.write("jobs", "job-ownerfailed0000000000", value={"id": "job-ownerfailed0000000000", "kind": "audio", "learner": self.owner.key,
                                                              "ref": None, "state": "failed", "step": "Done", "created": now, "heartbeat": now,
                                                              "result": None, "error": "Speech was down."})
        store.write("jobs", "job-somebodyelse0000000000", value={"id": "job-somebodyelse0000000000", "kind": "video", "learner": "someone-else",
                                                               "ref": "lesson-x", "state": "failed", "step": "x", "created": now, "heartbeat": now,
                                                               "result": None, "error": "private to someone else"})
        jobs = [e for e in self.read("activity")["events"] if e["kind"] == "job"]
        self.assertIn(("bad", "Speech was down."), [(e["tone"], e["detail"]) for e in jobs])
        self.assertEqual(len(jobs), 2, "the owner's failed job and the build of the exam: nothing of anyone else's")
        self.assertNotIn("someone else", json.dumps(self.read("activity")))

    def test_a_filming_job_is_named_plainly(self):
        store, now = self.dojo.store, iso()
        store.write("jobs", "job-filming00000000000000", value={"id": "job-filming00000000000000", "kind": "video", "learner": self.owner.key,
                                                              "ref": "lesson-x", "state": "done", "step": "Filmed 2 of 2 presenters",
                                                              "created": now, "heartbeat": now, "result": {}, "error": None})
        jobs = [e for e in self.read("activity")["events"] if e["kind"] == "job"]
        self.assertIn("Filming a lesson: done", [e["title"] for e in jobs])
        self.assertIn("Building a course: done", [e["title"] for e in jobs], "the build that added the exam")

    def test_what_is_running_now_is_one_line_each(self):
        now = self.read("activity")["now"]
        self.assertEqual([r["what"] for r in now[:4]], ["Writing and narrating lessons", "Writing Deeper Listens", "Drawing diagrams ahead of time",
                                                      "Finding official pages"])
        self.assertTrue(all(isinstance(r["state"], str) and r["state"] for r in now))

    def now_row(self, what: str) -> dict:   # a line of "Right now"
        return next(r for r in self.read("activity")["now"] if r["what"] == what)

    def test_a_diagram_being_drawn_is_said_as_that_and_not_as_writing_lessons(self):
        prep = self.app.state.preparer
        prep.set_deck_ahead(True)
        skill = self.skills[0]
        prep.state.update(running=True, current=f"{self.pid}/{skill['id']}", task="diagram")
        drawing = self.now_row("Drawing diagrams ahead of time")["state"]
        self.assertTrue(drawing.startswith("Drawing XY-110 · ") and drawing.endswith("(0 of 20 today)"), drawing)
        self.assertEqual(self.now_row("Writing and narrating lessons")["state"], "Waiting for work", "it is not writing a lesson")
        self.assertEqual(self.now_row("Writing Deeper Listens")["state"], "Waiting for work")
        prep.state.update(task="deeper")
        self.assertTrue(self.now_row("Writing Deeper Listens")["state"].startswith("Working on XY-110 · "))
        self.assertNotIn("Drawing XY-110", self.now_row("Drawing diagrams ahead of time")["state"])
        prep.state.update(task="lesson")
        self.assertTrue(self.now_row("Writing and narrating lessons")["state"].startswith("Working on XY-110 · "))
        prep.state.update(running=False, current=None, task=None)

    def test_the_diagrams_line_says_when_it_is_off_waiting_done_for_the_day_or_has_nothing_left(self):
        prep = self.app.state.preparer
        prep.set_deck_ahead(False)
        self.assertEqual(self.now_row("Drawing diagrams ahead of time")["state"], "Off. Switch it on from the To do page.")
        prep.set_deck_ahead(True)
        self.assertEqual(self.now_row("Drawing diagrams ahead of time")["state"], "Nothing left to draw", "no lesson is written yet")
        self.write(self.skills[0])
        self.assertEqual(self.now_row("Drawing diagrams ahead of time")["state"], "Waiting for its turn (0 of 20 today)")
        self.dojo.store.write("content", "deck-state", value={"day": iso()[:10], "today": 20, "written": 20, "ahead": True})
        done = self.now_row("Drawing diagrams ahead of time")
        self.assertEqual(done["state"], "Today's 20 are drawn. The next batch starts")
        self.assertEqual(parse_iso(done["at"]).hour, 0, "midnight UTC")
        self.assertGreater(parse_iso(done["at"]), utcnow())
        self.dojo.store.write("content", "deep-state", value={"day": iso()[:10], "today": 20, "written": 20})
        deeper = self.now_row("Writing Deeper Listens")
        self.assertEqual((deeper["state"], deeper["at"]), ("Today's 20 are written. The next batch starts", done["at"]))

    def test_the_diagrams_card_knows_what_is_drawn_now_and_when_the_next_batch_starts(self):
        prep = self.app.state.preparer
        prep.set_deck_ahead(True)
        decks = self.read()["make"]["decks"]
        self.assertEqual((decks["drawing"], decks["next_at"], decks["today"]), (None, None, 0))
        prep.state.update(running=True, current=f"{self.pid}/{self.skills[0]['id']}", task="diagram")
        self.assertTrue(self.read()["make"]["decks"]["drawing"].startswith("XY-110 · "))
        prep.state.update(running=False, current=None, task=None)
        self.dojo.store.write("content", "deck-state", value={"day": iso()[:10], "today": 20, "written": 20, "ahead": True})
        self.assertEqual(self.read()["make"]["decks"]["next_at"], self.studio._next_day())
        prep.set_deck_ahead(False)
        off = self.read()["make"]["decks"]
        self.assertEqual((off["drawing"], off["next_at"]), (None, None), "off: nothing is drawn and nothing is waiting for tomorrow")

    def test_what_is_running_now_is_read_each_time_and_not_kept_with_the_feed(self):
        prep = self.app.state.preparer
        prep.set_deck_ahead(True)
        self.assertEqual(self.read("activity")["now"][2]["state"], "Nothing left to draw", "no lesson is written yet")
        self.write(self.skills[0])
        prep.state.update(running=True, current=f"{self.pid}/{self.skills[0]['id']}", task="diagram")
        again = self.c.get("/api/studio/activity").json()   # no reset of Studio's own cache: the feed may be a minute old, the lines are not
        self.assertTrue(again["now"][2]["state"].startswith("Drawing XY-110 · "), again["now"][2])
        prep.state.update(running=False, current=None, task=None)
        self.assertTrue(self.c.get("/api/studio/activity").json()["now"][2]["state"].startswith("Waiting for its turn"))

    def test_a_drawing_that_stopped_is_not_blamed_on_its_checks(self):
        lesson = self.write(self.skills[0])
        skill = self.dojo.lesson(lesson)["skill"]
        self.dojo._deck_save(lesson, {"lesson": lesson, "package": self.pid, "skill": skill, "created": iso(), "state": "failed", "stopped": True, "attempts": 1})
        cut = next(e for e in self.read("activity")["events"] if e["kind"] == "diagram")
        self.assertTrue(cut["title"].endswith("stopped before it finished"), cut["title"])
        self.assertEqual(cut["tone"], "warn")
        self.dojo._deck_save(lesson, {"lesson": lesson, "package": self.pid, "skill": skill, "created": iso(), "state": "failed", "attempts": 1})
        held = next(e for e in self.read("activity")["events"] if e["kind"] == "diagram")
        self.assertTrue(held["title"].endswith("did not pass their checks"), held["title"])

    def test_the_diagrams_drawn_are_listed_and_one_that_did_not_pass_says_so(self):
        good, bad = self.write(self.skills[0]), self.write(self.skills[1])
        for lesson, state in ((good, "ready"), (bad, "failed")):
            skill = self.dojo.lesson(lesson)["skill"]
            self.dojo._deck_save(lesson, {"lesson": lesson, "package": self.pid, "skill": skill, "created": iso(), "state": state, "attempts": 1, "visuals": 8})
        events = {e["link"]: e for e in self.read("activity")["events"] if e["kind"] == "diagram"}
        ok, no = events[f"#/lesson/{good}/present"], events[f"#/lesson/{bad}/present"]
        self.assertEqual((ok["tone"], ok["course"], ok["detail"]), ("info", "XY-110", "8 diagrams"))
        self.assertTrue(ok["title"].startswith("Drew the diagrams for “"))
        self.assertEqual((no["tone"], no["detail"]), ("warn", "Present plays the plain deck instead."))
        self.assertTrue(no["title"].endswith("did not pass their checks"))
        self.dojo._deck_save(good, {"lesson": good, "package": self.pid, "skill": "x", "created": iso(), "state": "writing", "attempts": 1})
        self.dojo._deck_writing.add(good)   # it is being drawn at this moment
        try:
            self.assertNotIn(f"#/lesson/{good}/present", {e["link"] for e in self.read("activity")["events"] if e["kind"] == "diagram"}, "one being drawn is not an event yet")
        finally:
            self.dojo._deck_writing.discard(good)
        self.assertTrue(any(e["link"] == f"#/lesson/{good}/present" and e["tone"] == "warn" for e in self.read("activity")["events"] if e["kind"] == "diagram"),
                        "one that was cut off by a restart is read as failed")

    def test_events_older_than_the_window_are_left_out(self):
        lesson = self.write(self.skills[0])
        doc = self.dojo.lesson(lesson)
        doc["created"] = iso(utcnow() - timedelta(days=30))
        self.dojo.store.write("content", "lessons", lesson, value=doc)
        self.dojo._lesson_meta.pop(lesson, None)
        self.assertEqual([e for e in self.read("activity")["events"] if e["kind"] == "lesson"], [])

    def test_the_metered_spend_of_each_day_is_pooled_from_the_meter(self):
        budget, day = self.app.state.budget, utcnow().strftime("%Y-%m-%d")
        old = (utcnow() - timedelta(days=40)).strftime("%Y-%m-%d")
        self.dojo.store.write("team", "meter", value={"days": {
            day: {"total": 3.5}, old: {"total": 99.0},
            "2020-01-01": {"content": 1.0, "owner": 2.0, "members": 3.0, "members_other": 3.0, "exam:xy-110": 3.0}}, "months": {}})
        self.assertEqual(budget.spend_days(14), {day: 3.5})
        self.dojo.store.write("team", "meter", value={"days": {day: {"content": 1.0, "owner": 2.0, "members": 3.0, "members_other": 3.0,
                                                                       "exam:xy-110": 3.0}}, "months": {}})
        self.assertEqual(budget.spend_days(14), {day: 6.0}, "the buckets that do not overlap are added, the parts of 'members' are not")
        self.dojo.store.write("team", "meter", value={"days": {day: {"total": 2.0, "content": 1.0, "members": 0.5, "exam:xy-110": 0.5}}, "months": {}})
        self.assertEqual(budget.spend_days(14), {day: 3.5}, "a day on which the fifth member joined has both kinds of bucket")


class ReadingTests(StudioCase):
    def test_an_old_reading_is_still_what_the_visit_gets_and_a_new_one_is_made_in_the_background(self):
        made = []
        gate = threading.Event()

        def make():
            made.append(1)
            gate.wait(5)
            return {"n": len(made)}

        self.assertEqual(self.studio._cached("k", lambda: {"n": 0}), {"n": 0})
        self.studio._seen["k"] = (time.monotonic() - 3600, {"n": 0})   # an hour old
        self.assertEqual(self.studio._cached("k", make), {"n": 0}, "the visit does not wait")
        for _ in range(100):
            if made:
                break
            time.sleep(0.01)
        self.assertEqual(self.studio._cached("k", make), {"n": 0}, "and a second visit does not start a second reading")
        self.assertEqual(len(made), 1)
        gate.set()
        for _ in range(200):
            if not self.studio._busy:
                break
            time.sleep(0.01)
        self.assertEqual(self.studio._cached("k", make), {"n": 1})

    def test_a_reading_that_fails_leaves_the_last_one_in_place(self):
        self.studio._seen["k"] = (time.monotonic() - 3600, {"ok": True})

        def broken():
            raise RuntimeError("the share is slow")

        with self.assertLogs("dojo.studio", level="ERROR"):
            self.assertEqual(self.studio._cached("k", broken), {"ok": True})
            for _ in range(200):
                if not self.studio._busy:
                    break
                time.sleep(0.01)
            self.assertEqual(self.studio._cached("k", broken), {"ok": True}, "still the last reading, however often it fails")
            for _ in range(200):
                if not self.studio._busy:
                    break
                time.sleep(0.01)


class StaleReadingTests(StudioCase):
    def test_a_reading_made_on_the_spot_is_not_overwritten_by_an_older_one_that_finishes_later(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            started.set()
            gate.wait(5)
            return {"n": "older"}

        self.assertEqual(self.studio._cached("k", lambda: {"n": "first"}), {"n": "first"})
        self.studio._seen["k"] = (time.monotonic() - 3600, {"n": "first"})
        self.assertEqual(self.studio._cached("k", slow), {"n": "first"})   # a reading begins in the background
        self.assertTrue(started.wait(5))
        self.assertEqual(self.studio._cached("k", lambda: {"n": "now"}, fresh=True), {"n": "now"})   # the owner did something and read at once
        gate.set()
        for _ in range(200):
            if not self.studio._busy:
                break
            time.sleep(0.01)
        self.assertEqual(self.studio._cached("k", lambda: {"n": "never"}), {"n": "now"}, "the older reading did not take its place")


class VerdictTests(unittest.TestCase):
    def test_the_sentence_says_what_is_missing_as_what_it_is(self):
        verdict = studio_mod.Studio._verdict
        self.assertEqual(verdict(True, 73, 73, 73), "Every skill has a lesson and audio.")
        self.assertEqual(verdict(False, 94, 91, 91), "3 skills of 94 can't be studied from a lesson yet.")
        self.assertEqual(verdict(False, 94, 94, 91), "3 lessons are waiting for audio.", "not '0 skills can't be studied'")
        self.assertEqual(verdict(False, 94, 94, 93), "1 lesson is waiting for audio.")


class ToDoViewTests(StudioCase):
    """What the courses view carries for the To do page (ADR 0025): what only the owner can do, and how long a rewritten lesson is left out."""

    def test_the_view_carries_how_many_days_a_rewritten_lesson_is_left_out(self):
        self.assertEqual(self.read()["make"]["quiet_days"], learning_mod.QUIET_DAYS)

    def test_a_lab_to_look_at_names_the_page_that_changed(self):
        drift = self.app.state.drift
        flagged = [{"id": "vector", "title": "Make a vector product table", "skill": "d3.g2.s3", "date": "2026-10-12"}]
        page = {"url": "https://learn.microsoft.com/en-us/sql/t-sql/data-types/vector-data-type", "title": "Vector data type", "publisher": "Microsoft Learn"}
        with mock.patch.object(drift, "flagged_labs", return_value=flagged), \
                mock.patch.object(drift, "lab_state", return_value={"state": "recheck", "date": "2026-10-12", "pages": [page], "gone": []}):
            todo = self.read()["todo"]
        self.assertEqual(todo["labs"], [{**flagged[0], "pages": [page]}])

    def test_with_nothing_found_there_is_nothing_to_do(self):
        self.assertEqual(self.read()["todo"], {"labs": [], "stuck": [], "builds": []})

    def test_a_lab_and_a_lesson_whose_pages_are_gone_come_with_their_exam(self):
        lab = {"id": "vector", "title": "Make a vector product table", "skill": "d3.g2.s3", "date": "2026-10-12"}
        stuck = {"id": "lesson-x", "package": self.pid, "skill": self.skills[0]["id"], "title": "A lesson"}
        with mock.patch.object(self.app.state.drift, "owner_todo", return_value={"labs": [lab], "stuck": [stuck]}):
            todo = self.read()["todo"]
        self.assertEqual(todo["labs"], [lab])
        self.assertEqual(todo["stuck"], [{**stuck, "exam": "XY-110"}], "the exam is named, so the page need not look it up")

    def test_a_lesson_whose_pages_are_gone_says_what_the_search_does_about_its_skill(self):
        sourcer = self.app.state.newexams.sourcer
        a, b = self.skills[0]["id"], self.skills[1]["id"]
        stuck = lambda sid: {"id": f"lesson-{sid}", "package": self.pid, "skill": sid, "title": "A lesson", "pages": []}   # noqa: E731
        with mock.patch.object(self.app.state.drift, "owner_todo", return_value={"labs": [], "stuck": [stuck(a), stuck(b)]}), \
                mock.patch.object(sourcer, "moves", return_value={a: "checking"}):
            rows = self.read()["todo"]["stuck"]
        self.assertEqual([(r["skill"], r.get("follow")) for r in rows], [(a, "checking"), (b, None)], "no word for a skill the search does not work on")
        with mock.patch.object(self.app.state.drift, "owner_todo", return_value={"labs": [], "stuck": [stuck(a)]}), \
                mock.patch.object(sourcer, "moves", return_value={a: "stalled"}):
            self.assertEqual(self.read()["todo"]["stuck"][0]["follow"], "stalled")

    def test_what_the_search_does_about_a_lesson_that_could_not_be_read_is_said_so_and_not_taken_for_a_built_in_exam(self):
        sourcer = self.app.state.newexams.sourcer
        sid = self.skills[0]["id"]
        with mock.patch.object(self.app.state.drift, "owner_todo", return_value={"labs": [], "stuck": [{"id": "l", "package": self.pid, "skill": sid, "title": "T", "pages": []}]}), \
                mock.patch.object(sourcer, "moves", side_effect=OSError("the disk")), self.assertLogs("dojo.studio", level="ERROR"):
            rows = self.read()["todo"]["stuck"]
        self.assertEqual([r["follow"] for r in rows], ["unknown"])

    def test_the_drift_page_says_the_same_about_each_lesson_that_waits(self):
        sourcer = self.app.state.newexams.sourcer
        sid = self.skills[0]["id"]
        row = {"id": "l", "package": self.pid, "skill": sid, "title": "T", "pages": []}
        with mock.patch.object(self.app.state.drift, "summary", return_value={"stuck": [row]}), mock.patch.object(sourcer, "moves", return_value={sid: "searching"}):
            self.assertEqual(self.c.get("/api/studio/drift").json()["stuck"], [{**row, "follow": "searching"}])

    def test_a_build_that_stopped_is_there_and_an_exam_that_was_built_is_not(self):
        films = self.app.state.newexams
        films.store.write("package-builds", "zz-100", value={"package": "zz-100", "state": "stopped", "code": "ZZ-100", "started": iso()})
        films.store.write("package-builds", "zz-200", value={"package": "zz-200", "state": "failed", "code": "ZZ-200", "started": iso()})
        builds = sorted(self.read()["todo"]["builds"], key=lambda b: b["package"])
        self.assertEqual(builds, [{"package": "zz-100", "exam": "ZZ-100", "state": "stopped"}, {"package": "zz-200", "exam": "ZZ-200", "state": "failed"}],
                         "the exam that was built (XY-110) is not a thing to do")

    def test_one_part_that_cannot_be_read_never_hides_the_others(self):
        lab = {"id": "vector", "title": "T", "skill": "s", "date": "2026-10-12"}
        with mock.patch.object(self.app.state.newexams, "overview", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.app.state.drift, "owner_todo", return_value={"labs": [lab], "stuck": []}):
            todo = self.read()["todo"]
        self.assertEqual((todo["labs"], todo["builds"]), ([lab], []))


class StoreNamesTests(unittest.TestCase):
    def test_names_lists_the_json_documents_of_a_folder_and_nothing_else(self):
        tmp = tempfile.mkdtemp(prefix="dojo-names-")
        app = make_app(tmp, Web())
        store = app.state.dojo.store
        folder = Path(store.path("content", "things"))
        folder.mkdir(parents=True)
        for name in ("b.json", "a.json", ".hidden.json", "c.txt", "d.json.tmp"):
            (folder / name).write_text("{}", "utf-8")
        (folder / "folder.json").mkdir()
        self.assertEqual(store.names("content", "things"), ["a", "b"])
        self.assertEqual(store.names("content", "nothing-here"), [])


class StudioScreenTests(unittest.TestCase):
    """The page is organised by what the owner asks (ADR 0025): To do, Courses, Money, Health, Activity. These look at how it is wired; what
    each page shows is run in node by tests/test_studio_v2.py."""

    def test_the_tabs_and_their_routes(self):
        self.assertIn('[/^#\\/quality(?:\\/(todo|courses|money|health|activity|checks|costs))?$/, viewQuality],', SCRIPT)
        body = part("async function viewQuality(tab) {")
        self.assertIn("if (!isAdmin()) return viewStudioLog();", body)
        for tab in ("studioTodo", "studioCourses", "studioMoney", "studioHealth", "studioActivity"):
            self.assertIn(tab, body)
        self.assertIn('STUDIO_TABS = [["todo", "To do"], ["courses", "Courses"], ["money", "Money"], ["health", "Health"], ["activity", "Activity"]]', SCRIPT)
        self.assertIn('STUDIO_ALIAS = { checks: "health", costs: "money" }', SCRIPT, "the links of before keep working")
        nav = part("function studioNav(at) {")
        self.assertIn('"aria-current"', nav)
        self.assertNotIn("tablist", nav + body, "each tab is a link to a page: no promise of arrow keys and panels")
        self.assertNotIn('role: "tab"', nav)

    def test_the_heading_and_the_tabs_are_drawn_before_the_page_is_read(self):
        body = part("async function viewQuality(tab) {")
        self.assertIn("loadingNote()", body)
        self.assertIn("return [pageHead(", body)
        self.assertNotIn("await draw", body, "the slowest read does not hold the heading back")
        self.assertIn("state.nav === token", body, "a page that was read after the owner went elsewhere is not drawn")

    def test_each_tab_reads_its_own_views(self):
        todo = part("async function studioTodo() {")
        self.assertIn("/api/studio/courses", todo)
        self.assertIn("/api/studio/bill", todo)
        self.assertNotIn("/api/studio/drift", todo, "the labs and lessons that wait come with the courses: not the whole history")
        self.assertIn("/api/studio/courses", part("async function studioCourses() {"))
        money = part("async function studioMoney() {")
        for api in ("/api/studio/bill", "/api/studio/costs"):
            self.assertIn(api, money)
        self.assertIn("/api/studio/activity", part("async function studioActivity() {"))
        health = part("async function studioHealth(opts) {")
        for api in ("/api/quality", "/api/studio/voices", "/api/studio/drift", "/api/studio/deeper", "/api/studio/sourcing", "/api/studio/lab"):
            self.assertIn(api, health)
        self.assertNotIn("/api/studio/costs", health, "the slow price list is only read when Money is open")

    def test_the_voice_tile_says_what_it_counts_and_gives_the_kinds(self):
        self.assertNotIn("Meaning at risk", SCRIPT)
        self.assertIn('"Worth a listen"', SCRIPT)
        kinds = part("function voiceKinds(v) {")
        for word in ("name", "number", "negation"):
            self.assertIn(word, kinds)
        self.assertIn("say nothing about whether the lesson text is right", SCRIPT)

    def test_the_activity_is_grouped_by_the_readers_own_days_and_can_be_filtered(self):
        days = part("function feedDays(events) {")
        for local in ("getFullYear()", "getMonth()", "getDate()"):
            self.assertIn(local, days)
        self.assertNotIn("slice(0, 10)", days, "not the UTC day of the event")
        self.assertIn("aria-pressed", part("async function studioActivity() {"))

    def test_every_link_to_the_studio_log_opens_the_blocked_sentences(self):
        for link in ('linkButton("See the studio log", "#/quality/checks"', 'href: "#/quality/checks" }, "The studio log lists them and why."',
                     'href: "#/quality/checks" }, "See the studio log."'):
            self.assertIn(link, SCRIPT)
        self.assertIn('open: tab === "checks" ? "blocked" : null', part("async function viewQuality(tab) {"), "the old address opens that section")
        self.assertNotIn('href: "#/quality" }, "See the studio log."', SCRIPT)

    def test_every_button_that_spends_money_shows_its_price_and_asks_first(self):
        films = part("function videoRow(x, c, act) {")
        self.assertLess(films.index("moneySheet("), films.index('confirm: "FILM", offer: o.token'), "asked before anything is approved")
        self.assertIn("usd(o.usd)", films, "the price is on the button")
        self.assertIn("The price is not available just now", films, "no button without a price")
        self.assertIn("There is no room for these films", films)
        self.assertIn("Stop the rest", films)
        decks = part("function decksCard(c, act) {")
        self.assertLess(decks.index("moneySheet(deckAsk(d))"), decks.index('"/api/studio/make"'))
        self.assertIn('confirm: "MAKE"', decks)
        self.assertIn("until you press Stop", decks, "the diagrams stay on for lessons written later")
        checks = part("function pageCheckRow(x, act) {")
        self.assertLess(checks.index("moneySheet(ask)"), checks.index("/api/studio/pagecheck/"))
        every = part("function reviewAll(c, act, bill) {")
        self.assertLess(every.index("moneySheet({ pick: true"), every.index('"/api/studio/make"'))
        for spends in (films, decks, checks, every):
            self.assertNotIn("window.confirm(", spends, "a sheet with Cancel focused, not a dialog Enter can press through")
        self.assertIn("cancel.focus();", part("function moneySheet(o) {"))
        lesson = SCRIPT.split("const priced = !!(L.film_price && L.film_price.complete);", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(lesson.index("moneySheet(ask)"), lesson.index("/video"), "the lesson page asks as well")
        exam = part("function filmPanel(st, onStatus, note) {")
        self.assertNotIn("moneySheet", exam, "the exam page does not spend: videos are decided in one place")
        self.assertNotIn("/film`", exam)
        self.assertIn('linkButton("Decide on the To do page", "#/quality"', exam)

    def test_the_page_looks_again_while_something_is_made_and_stops_when_it_is_left(self):
        body = part("async function studioTodo() {")
        self.assertIn("host.isConnected", body)
        self.assertIn("c.make.busy", body)
        self.assertIn("setTimeout(poll, 30000)", body)
        self.assertIn("notice", body, "what a button said stays on the page")

    def test_a_course_says_what_it_is_made_of_in_the_owners_words(self):
        card = part("function courseCard(x, c) {")
        for label in ("Lessons", "Listen (audio)", "Deeper Listen", "Pages found", "Diagrams", "Videos"):
            self.assertIn(f'"{label}"', card)
        self.assertIn('h("summary", null, "What each number counts")', card, "the explanations are kept, out of the way")
        self.assertNotIn("act", card.split("{", 1)[0], "the Courses tab has no buttons: it reports, To do decides")

    def test_the_old_studio_checks_are_all_still_there(self):
        health = part("async function studioHealth(opts) {")
        for what in ("voicesPanel(v)", "driftRow(dr)", "deeperRow(dl)", "sourcingRow(sr)", "What was blocked, and why", "Why you can trust the gaps"):
            self.assertIn(what, health)


if __name__ == "__main__":
    unittest.main()
