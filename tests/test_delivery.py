"""The delivered-meaning check (EG-19): one lesson per step, a daily budget counted before the work,
yielding to the learner, a day's wait after a failure, per-turn comparison of the presenter videos,
and nothing written into a learner's record or the application log."""
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import delivery  # noqa: E402
from app.core import UserError, iso, utcnow  # noqa: E402
from app.delivery import DeliveryCheck  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import settings, web  # noqa: E402

EXCLUSION = [
    [("guide", "Content exclusion settings may take up to 30 minutes to take effect."), ("coach", "So it is not instant?")],
    [("guide", "No. GitHub Copilot won't use excluded files, but your IDE can still pass indirect information.")],
]
SCOPE = [
    [("guide", "First, decide the scope: repository or organization."), ("coach", "Copilot will never read excluded files?")],
    [("guide", "Yes. If you need broader visibility, use the organization path.")],
]


def add_lesson(dojo, n: int, slides: list, created: str, audio: bool = True) -> str:
    lesson_id = f"lesson-{n:020x}"
    doc = {"id": lesson_id, "package": "gh-300", "skill": "d1.s1", "title": f"Lesson {n}", "created": created,
           "slides": [{"title": f"Slide {k + 1}", "bullets": [], "narration": [{"voice": v, "text": t} for v, t in lines]}
                      for k, lines in enumerate(slides)]}
    dojo.store.write("content", "lessons", lesson_id, value=doc)
    for slide in doc["slides"] if audio else []:
        media_id = dojo._slide_media(slide)
        dojo.store.write_bytes("media", media_id + ".mp3", data=b"ID3" + media_id.encode())
    return lesson_id


def film(dojo, check: DeliveryCheck, lesson_id: str, speaker: str) -> tuple[str, list[dict]]:
    """A presenter video on the share: 10 seconds per turn, the words in the first 8."""
    plan = check.plan(lesson_id)
    media_id = plan["films"][speaker]
    spans = [{"start": 10.0 * k + 0.5, "end": 10.0 * k + 8.0} for k in range(len(plan["turns"][speaker]))]
    dojo.store.write_bytes("media", media_id + ".webm", data=b"WEBM" + media_id.encode())
    dojo.store.write("media", media_id, value=spans)
    return media_id, spans


class Ears:
    """Stands in for Speech fast transcription: hears what it is told for each file, and counts the calls."""

    def __init__(self):
        self.heard: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}
        self.before = None

    def __call__(self, audio: bytes, content_type: str) -> dict:
        media_id = (audio[3:] if content_type == "audio/mpeg" else audio[4:]).decode()
        self.calls.append((media_id, content_type))
        if self.before:
            self.before(len(self.calls))
        if media_id in self.fail:
            raise self.fail[media_id]
        return self.heard[media_id]

    def as_approved(self, check: DeliveryCheck, lesson_id: str, **changed: str) -> None:
        """Every slide heard exactly as approved, except the slides given as s1="..."."""
        plan = check.plan(lesson_id)
        for k, (media_id, text) in enumerate(zip(plan["audio"], plan["approved"])):
            self.heard[media_id] = {"text": changed.get(f"s{k + 1}", text), "seconds": 10.0, "words": []}

    def video(self, media_id: str, spans: list[dict], turns: list[str], extra: list[dict] | None = None) -> None:
        words = []
        for span, text in zip(spans, turns):
            parts = text.split()
            step = (span["end"] - span["start"]) / max(1, len(parts))
            words += [{"text": w, "start": span["start"] + i * step, "end": span["start"] + (i + 0.8) * step}
                      for i, w in enumerate(parts)]
        self.heard[media_id] = {"text": " ".join(turns), "seconds": spans[-1]["end"] + 1, "words": words + (extra or [])}


class DeliveryCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-check-")
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.dojo, self.check = self.app.state.dojo, self.app.state.checker
        self.ears = Ears()
        self.dojo.speech.transcribe_words = self.ears

    def state(self) -> dict:
        return self.dojo.store.read("content", "delivery-state")

    def today(self) -> dict:
        return (self.state() or {}).get("today") or {"listen": 0, "watch": 0}

    def busy(self) -> None:
        job = {"id": "job-00000000000000000000", "kind": "item", "learner": self.check.owner.key, "ref": None,
               "state": "running", "step": "", "created": iso(), "heartbeat": iso(), "result": None, "error": None}
        self.dojo.store.write("jobs", job["id"], value=job)

    def test_checks_one_lesson_per_step_and_compares_every_slide(self):
        older = add_lesson(self.dojo, 1, SCOPE, "2026-09-01T00:00:00+00:00")
        newer = add_lesson(self.dojo, 2, EXCLUSION, "2026-09-02T00:00:00+00:00")
        partial = add_lesson(self.dojo, 3, [[("guide", "A slide nobody has narrated yet.")]], "2026-09-03T00:00:00+00:00", audio=False)
        self.ears.as_approved(self.check, older)
        self.ears.as_approved(self.check, newer, s1="Content exclusion settings may take up to 13 minutes to take effect. So, it is not instant?")
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        self.assertEqual(set(self.check.results()), {newer}, "the newest lesson first, one lesson per step")
        listen = self.check.results()[newer]["listen"]
        self.assertEqual([s["slide"] for s in listen["slides"]], [1, 2])
        first, second = listen["slides"]
        self.assertEqual((first["errors"], first["flags"]), (1, ["number"]))
        self.assertEqual(first["diffs"][0], {"approved": "30", "heard": "13", "flags": ["number"]})
        self.assertEqual((second["wer"], second["errors"]), (0.0, 0), "punctuation and 'won't' are not errors")
        self.assertEqual((listen["words"], listen["errors"], listen["seconds"]), (first["words"] + second["words"], 1, 20.0))
        self.assertEqual(self.today(), {"listen": 1, "watch": 0, "deep": 0})
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        self.assertEqual(set(self.check.results()), {newer, older})
        self.assertEqual(self.check.step(), delivery.IDLE_WAIT_S, "a lesson without all its audio is not checked")
        self.assertEqual(self.check.state["note"], "nothing new to check")
        self.assertNotIn(partial, self.check.results())
        self.assertEqual(len(self.ears.calls), 4)
        self.assertTrue(all(kind == "audio/mpeg" for _, kind in self.ears.calls))
        # The results are on the share: a restarted app reads them back and checks nothing twice.
        again = DeliveryCheck(self.dojo, self.check.owner)
        self.assertEqual(set(again.results()), {newer, older})
        self.assertEqual(again.step(), delivery.IDLE_WAIT_S)
        self.assertEqual(len(self.ears.calls), 4)

    def test_the_daily_budget_is_counted_before_the_work_and_survives_a_restart(self):
        self.assertEqual(delivery.DAILY, {"listen": 10, "watch": 1, "deep": 10})
        a = add_lesson(self.dojo, 1, SCOPE, "2026-09-01T00:00:00+00:00")
        b = add_lesson(self.dojo, 2, EXCLUSION, "2026-09-02T00:00:00+00:00")
        self.ears.as_approved(self.check, a)
        self.ears.as_approved(self.check, b)
        with mock.patch.dict(delivery.DAILY, {"listen": 1}):
            self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
            self.assertEqual(self.check.step(), delivery.IDLE_WAIT_S)
            self.assertEqual(self.check.state["note"], "Listen: done for today; Watch: nothing new; Deeper Listen: nothing new")
            self.assertEqual(len(self.check.results()), 1)
            restarted = DeliveryCheck(self.dojo, self.check.owner)
            self.assertEqual(restarted.step(), delivery.IDLE_WAIT_S, "a restart does not hand out a new allowance")
            self.assertEqual(len(self.ears.calls), 2)
            doc = self.state()
            doc["day"] = (utcnow() - timedelta(days=1)).date().isoformat()
            self.dojo.store.write("content", "delivery-state", value=doc)
            self.assertEqual(restarted.step(), delivery.WORK_WAIT_S, "a new day, a new allowance")
            self.assertEqual(len(restarted.results()), 2)
            self.assertEqual(self.state()["seconds"]["listen"], 40.0, "the transcription used is kept across days")

    def test_yields_to_the_learner_before_and_between_slides(self):
        lesson = add_lesson(self.dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00")
        self.ears.as_approved(self.check, lesson)
        self.busy()
        self.assertEqual(self.check.step(), delivery.BUSY_WAIT_S)
        self.assertEqual((self.ears.calls, self.check.state["note"]), ([], "your own work comes first"))
        self.assertEqual(self.today()["listen"], 0, "nothing is counted when nothing starts")
        self.dojo.store.delete("jobs", "job-00000000000000000000")
        self.ears.before = lambda n: self.busy()  # the learner starts something while slide 1 is written down
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        self.assertEqual(len(self.ears.calls), 1, "stops before the next slide")
        self.assertEqual(self.check.results(), {}, "half a lesson is not a result")
        self.assertEqual(self.check.state["note"], "stopped for your own work")
        self.assertEqual(self.today()["listen"], 1, "what was spent stays counted")

    def test_waits_for_a_free_job_slot(self):
        lesson = add_lesson(self.dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00")
        self.ears.as_approved(self.check, lesson)
        slots = self.dojo.jobs.slots
        taken = [slots.acquire() for _ in range(3)]
        try:
            with mock.patch.object(delivery, "SLOT_WAIT_S", 0.01):
                self.assertEqual(self.check.step(), delivery.BUSY_WAIT_S)
        finally:
            for _ in taken:
                slots.release()
        self.assertEqual((self.ears.calls, self.today()["listen"]), ([], 0))
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        self.assertEqual(len(self.ears.calls), 2)

    def test_a_failure_goes_to_the_quality_log_and_is_tried_again_after_a_day(self):
        lesson = add_lesson(self.dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00")
        self.ears.as_approved(self.check, lesson)
        first_audio = self.check.plan(lesson)["audio"][0]
        self.ears.fail[first_audio] = UserError("Speech could not write this down.")
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        log = self.dojo.store.read_lines("content", "quality.jsonl")
        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual((entry["kind"], entry["part"], entry["lesson"], entry["text"]), ("delivery", "Listen", lesson, ""))
        self.assertIn("Speech could not write this down", entry["reason"])
        self.assertIn("tried again after 24 hours", entry["reason"])
        self.assertIn(entry, self.dojo.quality_log(self.check.owner), "Studio's log shows it")
        self.assertEqual(self.today()["listen"], 1, "counted before the work")
        self.assertEqual(self.check.step(), delivery.IDLE_WAIT_S, "the failed lesson waits a day")
        self.assertEqual(len(self.ears.calls), 1)
        self.assertEqual(self.check.status()["waiting_after_failure"], 1)
        doc = self.state()
        doc["failed"][f"listen:{lesson}"] = iso(utcnow() - timedelta(hours=delivery.RETRY_HOURS + 1))
        self.dojo.store.write("content", "delivery-state", value=doc)
        del self.ears.fail[first_audio]
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        self.assertIn(lesson, self.check.results())
        self.assertEqual(self.state()["failed"], {}, "a success clears the failure")
        # A bug in the check itself is a failure like any other, and never ends the loop.
        other = add_lesson(self.dojo, 2, SCOPE, "2026-09-02T00:00:00+00:00")
        self.ears.as_approved(self.check, other)
        self.ears.fail[self.check.plan(other)["audio"][0]] = RuntimeError("boom")
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        self.assertIn("the check itself failed (RuntimeError)", self.dojo.store.read_lines("content", "quality.jsonl")[-1]["reason"])

    def test_watch_compares_each_turn_by_its_stored_timing(self):
        lesson = add_lesson(self.dojo, 1, SCOPE, "2026-09-01T00:00:00+00:00")
        self.ears.as_approved(self.check, lesson)
        plan = self.check.plan(lesson)
        self.assertEqual(plan["layout"], [[("guide", 0), ("coach", 0)], [("guide", 1)]])
        guide, guide_spans = film(self.dojo, self.check, lesson, "guide")
        coach, coach_spans = film(self.dojo, self.check, lesson, "coach")
        self.ears.video(guide, guide_spans, ["First, decide the scope, repository, or organization.",
                                             "Yes, if you need broader visibility, use the organization path."],
                        extra=[{"text": "Bye.", "start": 30.0, "end": 30.4}])
        self.ears.video(coach, coach_spans, ["Copilot will ever read excluded files?"])
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
        watch = self.check.results()[lesson]["watch"]
        self.assertEqual([(t["slide"], t["speaker"], t["turn"]) for t in watch["turns"]],
                         [(1, "guide", 1), (1, "coach", 1), (2, "guide", 2)])
        self.assertEqual([t["errors"] for t in watch["turns"]], [0, 1, 0])
        self.assertEqual(watch["turns"][1]["flags"], ["negation"])
        self.assertEqual(watch["turns"][1]["diffs"], [{"approved": "never", "heard": "ever", "flags": ["negation"]}])
        self.assertEqual(watch["presenters"]["guide"]["words_outside_turns"], 1)
        self.assertEqual(watch["media"], {"guide": guide, "coach": coach})
        self.assertEqual([kind for _, kind in self.ears.calls], ["audio/webm", "audio/webm"])
        self.assertEqual(self.today(), {"listen": 0, "watch": 1, "deep": 0})
        self.assertEqual(self.check.step(), delivery.WORK_WAIT_S, "the Watch sample is spent; Listen goes on")
        self.assertIn("listen", self.check.results()[lesson])
        summary = self.check.summary()
        self.assertEqual((summary["watch"]["lessons"], summary["watch"]["turns"], summary["watch"]["flagged"]), (1, 3, 1))
        self.assertEqual(summary["worst"][0]["kind"], "watch")
        self.assertEqual(summary["worst"][0]["speaker"], "coach")

    def test_a_video_that_cannot_be_checked_never_spends_the_watch_sample(self):
        """A video without its turn timing, or too large to send, would fail every time, spend the day's one
        Watch check each time, and come back first after the retry wait. It is passed over; a large one is
        counted, so Studio can say so."""
        untimed = add_lesson(self.dojo, 3, SCOPE, "2026-09-03T00:00:00+00:00", audio=False)
        large = add_lesson(self.dojo, 2, SCOPE, "2026-09-02T00:00:00+00:00", audio=False)
        mixed = add_lesson(self.dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00", audio=False)
        bare = self.check.plan(untimed)["films"]["guide"]
        self.dojo.store.write_bytes("media", bare + ".webm", data=b"WEBM" + bare.encode())
        big, _ = film(self.dojo, self.check, large, "guide")
        self.dojo.store.write_bytes("media", big + ".webm", data=b"WEBM" + big.encode() + bytes(1000))
        guide, spans = film(self.dojo, self.check, mixed, "guide")
        coach, _ = film(self.dojo, self.check, mixed, "coach")
        self.dojo.store.write_bytes("media", coach + ".webm", data=b"WEBM" + coach.encode() + bytes(1000))
        self.ears.video(guide, spans, [" ".join(t for v, t in slide if v == "guide") for slide in EXCLUSION])
        with mock.patch.object(delivery, "MAX_CHECK_BYTES", 500), mock.patch.dict(delivery.DAILY, {"watch": 5}):
            self.assertEqual(self.check.step(), delivery.WORK_WAIT_S)
            self.assertEqual(self.ears.calls, [(guide, "audio/webm")], "only the video that can be checked is sent")
            self.assertEqual(self.check.results()[mixed]["watch"]["media"], {"guide": guide})
            self.assertEqual(self.check.step(), delivery.IDLE_WAIT_S, "and with budget left, nothing is tried again")
            self.assertEqual((len(self.ears.calls), self.today()["watch"], self.state()["failed"]), (1, 1, {}))
            self.assertEqual((self.check.summary()["watch"]["too_large"], self.check.status()["videos_too_large"]), (2, 2))
        self.assertEqual(set(self.check.results()), {mixed})

    def test_media_is_read_outside_the_store_lock_and_its_size_is_checked_first(self):
        """A presenter video is tens of megabytes. Read under the store's lock, it would hold up every other
        read and write in Dojo for as long as the share takes, so it is read as playback reads it."""
        lesson = add_lesson(self.dojo, 1, SCOPE, "2026-09-01T00:00:00+00:00")
        name = self.check.plan(lesson)["audio"][0] + ".mp3"
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with self.dojo.store.lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(held.wait(5))
        got: list = []
        reader = threading.Thread(target=lambda: got.append(self.check._media(name)))
        reader.start()
        reader.join(2)
        blocked, read_while_held = reader.is_alive(), list(got)
        release.set()
        holder.join(5)
        reader.join(5)
        self.assertFalse(blocked, "a media read never waits for the store")
        self.assertEqual(read_while_held, [b"ID3" + name[:-4].encode()], "read while another thread holds the store")
        self.assertIsNone(self.check._media("a" + "0" * 39 + ".mp3"))
        with mock.patch.object(delivery, "MAX_CHECK_BYTES", 10), self.assertRaises(UserError):
            self.check._media(name)

    def test_nothing_is_written_into_a_learners_record_and_nothing_heard_is_logged(self):
        learners = Path(self.tmp, "learners")
        self.app.state.dojo.profile(self.check.owner)
        before = sorted(str(p) for p in learners.rglob("*")) if learners.exists() else []
        lesson = add_lesson(self.dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00")
        failing = add_lesson(self.dojo, 2, SCOPE, "2026-09-02T00:00:00+00:00")
        self.ears.as_approved(self.check, lesson, s2="No zebra Copilot will use excluded files but your idea can pass it")
        self.ears.as_approved(self.check, failing)
        self.ears.fail[self.check.plan(failing)["audio"][1]] = UserError("Speech could not write this down.")
        with self.assertLogs("dojo.delivery", level="DEBUG") as logs:
            self.check.step()
            self.check.step()
        after = sorted(str(p) for p in learners.rglob("*")) if learners.exists() else []
        self.assertEqual(before, after)
        text = "\n".join(logs.output)
        self.assertIn(lesson, text)
        for word in ("zebra", "idea", "excluded", "Content exclusion", "organization"):
            self.assertNotIn(word, text)
        written = {str(p.relative_to(self.tmp)).replace("\\", "/") for p in Path(self.tmp).rglob("*") if p.is_file()}
        self.assertIn(f"content/delivery/{lesson}.json", written)
        self.assertIn("content/delivery-state.json", written)
        self.assertFalse(any(p.startswith("learners/") for p in written - set(before)))


class StudioVoicesApiTests(unittest.TestCase):
    def test_the_studio_panel_and_diag_have_the_checker(self):
        tmp = tempfile.mkdtemp(prefix="dojo-voices-")
        app = create_app(settings(tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        c = TestClient(app)
        dojo, check = app.state.dojo, app.state.checker
        ears = Ears()
        dojo.speech.transcribe_words = ears
        lesson = add_lesson(dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00")
        ears.as_approved(check, lesson, s1="Content exclusion settings may take up to 13 minutes to take effect. So it is not instant?")
        empty = c.get("/api/studio/voices").json()
        self.assertEqual((empty["listen"]["lessons"], empty["worst"], empty["listen"]["median_wer"]), (0, [], None))
        check.step()
        v = c.get("/api/studio/voices").json()
        self.assertEqual(set(v), {"lessons", "listen", "watch", "deep", "worst", "daily", "today", "retry_hours", "note", "status"})
        self.assertEqual(set(v["listen"]), {"lessons", "slides", "median_wer", "wer", "flagged", "minutes"})
        self.assertEqual(set(v["deep"]), {"lessons", "parts", "median_wer", "wer", "flagged", "minutes"})
        self.assertEqual(set(v["watch"]), {"lessons", "turns", "median_wer", "wer", "flagged", "minutes", "too_large"})
        self.assertEqual(set(v["status"]), {"started", "running", "current", "last", "note"})
        self.assertFalse(v["status"]["started"], "only started where Speech is real")
        self.assertEqual((v["lessons"], v["listen"]["lessons"], v["listen"]["slides"], v["listen"]["flagged"]), (1, 1, 2, 1))
        self.assertEqual(v["listen"]["median_wer"], round((v["worst"][0]["wer"] + 0) / 2, 4))
        worst = v["worst"][0]
        self.assertEqual((worst["kind"], worst["lesson"], worst["slide"], worst["flags"]), ("listen", lesson, 1, ["number"]))
        self.assertIn(["30", 2], worst["approved"])
        self.assertIn(["13", 2], worst["heard"])
        self.assertIn("Transcription itself can be wrong", v["note"])
        self.assertEqual((v["daily"], v["today"]), ({"listen": 10, "watch": 1, "deep": 10}, {"listen": 1, "watch": 0, "deep": 0}))
        diag = c.get("/api/diag").json()["delivery_check"]
        self.assertEqual(diag["daily_limit"], {"listen": 10, "watch": 1, "deep": 10})
        self.assertEqual((diag["checked_today"]["listen"], diag["lessons_checked"]["listen"], diag["retry_hours"]), (1, 1, 24))
        self.assertIn("transcribed_minutes", diag)
        self.assertEqual(diag["videos_too_large"], 0)
        self.assertIs(diag["started"], False)
        # The worst slides quote lesson words, which teach: closed while a timed exam runs.
        app.state.exams.open_attempt = lambda learner: {"id": "exam-00000000000000000000"}
        closed = c.get("/api/studio/voices")
        self.assertEqual(closed.status_code, 409)
        self.assertIn("timed practice exam", closed.json()["detail"])


if __name__ == "__main__":
    unittest.main()
