"""Unit tests: quote checks, page text, excerpts, evidence rules and the record's hash chain."""
import io
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import types  # noqa: E402

import httpx  # noqa: E402

from app.avatar import PRESENTERS  # noqa: E402
from app.avatar import media_id_for as avatar_media_id  # noqa: E402
from app.core import Store, UserError, canonical, sha256  # noqa: E402
from app.learning import Dojo, too_similar  # noqa: E402
from app.evidence import coverage, derive, suggestions  # noqa: E402
from app.packages import Packages, skill_kind  # noqa: E402
from app.sources import Sources, excerpt, html_to_text, normalize  # noqa: E402
from app.speech import MAX_AUDIO_BYTES, VOICES, Speech, build_ssml  # noqa: E402

PKG = Packages(Path(__file__).resolve().parent.parent / "app" / "packages")


class QuoteTests(unittest.TestCase):
    def test_normalize_makes_markdown_and_typography_comparable(self):
        source = "Use **content exclusion** to stop [Copilot](https://docs.github.com/x) from using “sensitive” files – always."
        self.assertIn(normalize('content exclusion to stop Copilot from using "sensitive" files - always'), normalize(source))

    def test_changed_wording_does_not_match(self):
        self.assertNotIn(normalize("stop Copilot from reading secret files"), normalize("stop Copilot from using sensitive files"))

    def test_quote_must_be_in_the_excerpt_the_models_saw(self):
        page = "First paragraph about repository settings and owners.\n\nSecond paragraph: content exclusion hides files from Copilot suggestions."
        store = Store(Path(tempfile.mkdtemp(prefix="dojo-quote-")))
        snap = {"text": page, "sha256": sha256(page)}
        seen = types.SimpleNamespace(sources=Sources(store))
        g = [{"n": 1, "snap": snap, "excerpt_norm": normalize("First paragraph about repository settings and owners.")}]
        self.assertTrue(Dojo.quote_ok(seen, g, "First paragraph about repository settings and owners", 1))
        self.assertFalse(Dojo.quote_ok(seen, g, "content exclusion hides files from Copilot suggestions", 1))
        self.assertFalse(Dojo.quote_ok(seen, g, "First paragraph about repository settings and owners", 2))
        self.assertFalse(Dojo.quote_ok(seen, g, "settings and owners", 1))


class NoveltyTests(unittest.TestCase):
    def test_light_rewording_counts_as_a_repeat(self):
        a = "Your team wants Copilot to ignore the secrets folder in the payments repository before the audit next week. What do you configure?"
        b = "Your team wants Copilot to ignore the secrets folder in the billing repository before the audit next week. What should you configure?"
        c = "A hospital asks whether nurses can dictate notes into a tablet during night shifts without internet access. What do you advise?"
        self.assertTrue(too_similar(a, b))
        self.assertFalse(too_similar(a, c))


class PageTextTests(unittest.TestCase):
    def test_main_content_only_without_scripts_and_navigation(self):
        page = ("<html><body><nav>Menu Home</nav><main><h1>Title</h1><p>First &amp; best paragraph.</p>"
                "<script>var x = 1;</script><ul><li>One</li><li>Two</li></ul></main><footer>Footer</footer></body></html>")
        text = html_to_text(page)
        self.assertIn("First & best paragraph.", text)
        self.assertIn("One", text)
        for noise in ("Menu", "var x", "Footer"):
            self.assertNotIn(noise, text)

    def test_excerpt_keeps_relevant_paragraphs_within_limit(self):
        paragraphs = [f"Filler paragraph number {i} about nothing in particular." for i in range(400)]
        paragraphs.insert(250, "Columnstore indexes store data by column and suit analytics workloads.")
        text = "\n\n".join(paragraphs)
        out = excerpt(text, "Design columnstore indexes", limit=2000)
        self.assertLessEqual(len(out), 2100)
        self.assertIn("Columnstore indexes store data by column", out)


class PackageTests(unittest.TestCase):
    def test_verbs_choose_the_item_template(self):
        self.assertEqual(skill_kind("Describe risks and limitations")[0], "explanation")
        self.assertEqual(skill_kind("Configure content exclusions")[0], "scenario")

    def test_packages_load_with_weights_summing_to_one(self):
        for pid in ("gh-300", "dp-800"):
            p = PKG.get(pid)
            self.assertAlmostEqual(sum(s["share"] for s in p["skills"].values()), 1.0, places=6)
            self.assertTrue(all(s["sources"] for s in p["skills"].values()), f"{pid} has a skill without a grounding source")


class SpeechTests(unittest.TestCase):
    def test_ssml_escapes_text_and_uses_two_voices(self):
        ssml = build_ssml([{"voice": "guide", "text": "A < B & C"}, {"voice": "coach", "text": "Why?"}])
        self.assertIn("A &lt; B &amp; C", ssml)
        self.assertIn("AvaMultilingual", ssml)
        self.assertIn("AndrewMultilingual", ssml)

    def test_a_role_is_read_by_its_own_presenter_in_listen_and_in_watch(self):
        self.assertEqual(VOICES, {role: who["voice"] for role, who in PRESENTERS.items()})
        self.assertEqual(VOICES["guide"], "en-US-AndrewMultilingualNeural")
        self.assertEqual(VOICES["coach"], "en-US-AvaMultilingualNeural")
        ssml = build_ssml([{"voice": "guide", "text": "One."}, {"voice": "coach", "text": "Two."}])
        self.assertLess(ssml.index(VOICES["guide"]), ssml.index(VOICES["coach"]))


class VideoIdTests(unittest.TestCase):
    """A video media id is the content hash of its SSML and its avatar settings. About 200 filmed
    videos are stored under these ids, so the ids below are the ones main produced and must not move.
    If this test fails, the change orphans everything already filmed."""

    turns = [["Hello there.", "Second line."], ["Third turn."]]

    def test_media_ids_are_the_ones_the_filmed_videos_are_stored_under(self):
        self.assertEqual(avatar_media_id("guide", self.turns), "v2afbe65e8bdf3568ede14cb5c5c1749a3dbb537")
        self.assertEqual(avatar_media_id("coach", self.turns), "v27622b6c7fa7e7e96286713670403d063ea4832")


SECRET = "the-learner-said-arboreal-quokka-1987"


def speech_for(reply):
    """A Speech that talks to a fake service. `reply` is called with the request."""
    store = Store(Path(tempfile.mkdtemp(prefix="dojo-stt-")))
    conf = types.SimpleNamespace(speech_endpoint="https://example-ai.cognitiveservices.azure.com", speech_region="swedencentral")
    tokens = types.SimpleNamespace(get=lambda scope: "not-a-real-token")
    return Speech(conf, tokens, store, http=httpx.Client(transport=httpx.MockTransport(reply))), store


class TranscribeTests(unittest.TestCase):
    """What the learner said is written down, given back, and kept nowhere."""

    def test_it_sends_the_audio_once_and_returns_the_words(self):
        seen = {}

        def reply(request):
            seen["url"] = str(request.url)
            seen["body"] = request.content
            return httpx.Response(200, json={"durationMilliseconds": 5200, "combinedPhrases": [{"text": SECRET}]})

        speech, store = speech_for(reply)
        heard = speech.transcribe(b"fake opus bytes", "audio/webm")
        self.assertEqual(heard, {"text": SECRET, "seconds": 5.2})
        self.assertIn("/speechtotext/transcriptions:transcribe?api-version=2024-11-15", seen["url"])
        self.assertIn(b'name="audio"; filename="answer.webm"', seen["body"])
        self.assertIn(b'{"locales":["en-US"]}', seen["body"])
        self.assertNotIn(b"phraseList", seen["body"])  # EG-17: never hand the learner words
        self.assertEqual(list(store.path("media").glob("*")) if store.path("media").exists() else [], [])

    def test_only_audio_a_browser_records_is_accepted(self):
        speech, _ = speech_for(lambda r: httpx.Response(200, json={"combinedPhrases": [{"text": "hello"}]}))
        for kind in ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav"):
            self.assertEqual(speech.transcribe(b"x", kind)["text"], "hello")
        for kind in ("text/plain", "application/json", "video/webm", ""):
            with self.assertRaises(UserError):
                speech.transcribe(b"x", kind)

    def test_a_recording_that_is_too_big_or_too_long_is_refused(self):
        speech, _ = speech_for(lambda r: httpx.Response(200, json={"durationMilliseconds": 900000, "combinedPhrases": [{"text": "long"}]}))
        with self.assertRaises(UserError):
            speech.transcribe(b"0" * (MAX_AUDIO_BYTES + 1), "audio/webm")
        with self.assertRaises(UserError) as caught:
            speech.transcribe(b"x", "audio/webm")
        self.assertIn("three minutes", str(caught.exception))

    def test_a_busy_service_says_so_in_plain_words(self):
        speech, _ = speech_for(lambda r: httpx.Response(429, json={"error": SECRET}))
        with self.assertRaises(UserError) as caught:
            speech.transcribe(b"x", "audio/webm")
        self.assertEqual(str(caught.exception), "Speech is busy. Try again in a few seconds.")

    def test_neither_the_words_nor_the_service_reply_ever_reach_the_log(self):
        """The unique phrase stands for whatever the learner said: it may come back, but it may
        never be logged, and it may never be repeated in an error."""
        good, bad = SECRET + " and more", "<html>" + SECRET + "</html>"
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        root = logging.getLogger()
        root.addHandler(handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        try:
            ok, _ = speech_for(lambda r: httpx.Response(200, json={"durationMilliseconds": 1000, "combinedPhrases": [{"text": good}]}))
            self.assertEqual(ok.transcribe(b"x", "audio/webm")["text"], good)
            for status in (400, 401, 500):
                broken, _ = speech_for(lambda r, s=status: httpx.Response(s, text=bad))
                with self.assertRaises(UserError) as caught:
                    broken.transcribe(b"x", "audio/webm")
                self.assertNotIn(SECRET, str(caught.exception))
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        self.assertNotIn(SECRET, logs.getvalue())

    def test_the_self_test_says_a_sentence_and_writes_it_down_again(self):
        said = {}

        def reply(request):
            if "tts" in str(request.url):
                said["ssml"] = request.content.decode("utf-8")
                return httpx.Response(200, content=b"O" * 4096)
            return httpx.Response(200, json={"durationMilliseconds": 2000, "combinedPhrases": [{"text": "Dojo writes down what you say."}]})

        speech, _ = speech_for(reply)
        probe = speech.probe_transcription()
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["words"], "4/4")
        self.assertIn("Dojo writes down what you say.", said["ssml"])


class ChainTests(unittest.TestCase):
    def test_chain_verifies_and_detects_tampering(self):
        store = Store(Path(tempfile.mkdtemp(prefix="dojo-chain-")))
        for i in range(3):
            store.append_event("lx", "note", {"i": i})
        self.assertEqual(store.verify_chain("lx"), {"ok": True, "events": 3})
        p = store.path("learners", "lx", "events.jsonl")
        lines = p.read_text("utf-8").splitlines()
        lines[1] = lines[1].replace('"i":1', '"i":9')
        p.write_text("\n".join(lines) + "\n", "utf-8")
        result = store.verify_chain("lx")
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"], 1)

    def test_hash_covers_the_previous_hash(self):
        body = {"id": "e", "at": "t", "type": "x", "data": {}, "prev": "0" * 64}
        self.assertNotEqual(sha256(canonical(body)), sha256(canonical(dict(body, prev="1" * 64))))


class EvidenceTests(unittest.TestCase):
    pid = "gh-300"

    def setUp(self):
        self.pkg = PKG.get(self.pid)
        self.sid = next(s["id"] for s in self.pkg["skills"].values() if s["kind"] == "scenario")
        self.t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        self.events = []

    def add(self, minutes, event_type, **data):
        at = self.t0 + timedelta(minutes=minutes)
        self.events.append({"at": at.isoformat(), "type": event_type, "data": {"package": self.pid, "skill": self.sid, **data}})

    def attempt(self, minutes, item, mode, met, total=2, hints=0, described=True, changed=True):
        self.add(minutes, "item.issued", item=item, mode=mode, kind="scenario", changed_condition=changed)
        for i in range(hints):
            self.add(minutes, "hint.shown", item=item, index=i, described=described)
        self.add(minutes + 1, "answer.judged", item=item, mode=mode, met=met, total=total, feedback_shown=True)

    def state(self, later_hours=20):
        return derive(self.events, self.pkg, later_hours)[self.sid]

    def test_learning_path_is_described_by_conditions_not_ranked(self):
        self.attempt(0, "i1", "probe", 0)
        e = self.state()
        self.assertEqual(e["state"], "not_met")
        self.assertIsNone(e["attempts"][0]["minutes_since_teaching"])
        first = suggestions(self.pkg, derive(self.events, self.pkg), self.t0 + timedelta(hours=1), 20)[0]
        self.assertEqual((first["skill"], first["action"]), (self.sid, "lesson"))

        self.add(10, "lesson.viewed", lesson="l1", modality="read")
        first = suggestions(self.pkg, derive(self.events, self.pkg), self.t0 + timedelta(hours=1), 20)[0]
        self.assertEqual(first["action"], "practice")

        self.attempt(20, "i2", "practice", 2, hints=1)
        e = self.state()
        self.assertEqual(e["state"], "met_with_help")
        self.assertFalse(e["checks"]["unaided"])
        self.assertIn("1 hint before answering", e["attempts"][-1]["conditions"])

        self.attempt(30, "i3", "check", 2)
        e = self.state()
        self.assertEqual(e["state"], "met_unaided")
        self.assertTrue(e["checks"]["unaided"])
        self.assertFalse(e["checks"]["later"])
        self.assertLess(e["attempts"][-1]["minutes_since_teaching"], 20 * 60)

        self.attempt(30 + 21 * 60, "i4", "check", 2)
        e = self.state()
        self.assertEqual(e["state"], "met_unaided_later")
        self.assertTrue(e["checks"]["later"])
        self.assertEqual(e["not_yet_shown"], [])

    def test_dispute_removes_the_claim_and_never_upgrades(self):
        self.attempt(0, "i1", "probe", 0)
        self.add(5, "dispute.filed", item="i1", reason="The rubric is wrong")
        e = self.state()
        self.assertEqual(e["state"], "untested")
        self.assertEqual(e["disputed"], 1)

        self.attempt(10, "i2", "check", 2)
        self.attempt(20, "i3", "check", 2)
        self.add(25, "dispute.filed", item="i3", reason="Too lenient")
        e = self.state()
        self.assertEqual(e["latest"]["item"], "i2")
        self.assertEqual(e["disputed"], 2)

    def test_a_disputed_miss_never_lifts_an_older_pass(self):
        self.attempt(0, "i1", "check", 2)
        self.attempt(30, "i2", "check", 0)
        self.add(35, "dispute.filed", item="i2", reason="I did cover it")
        e = self.state()
        self.assertEqual(e["state"], "not_met")
        self.assertEqual(e["latest"]["item"], "i2")
        self.assertTrue(e["checks"]["unaided"])

    def test_practice_without_opening_hints_is_still_not_unaided(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="read")
        self.attempt(5, "i1", "practice", 2)
        e = self.state()
        self.assertFalse(e["checks"]["unaided"])
        self.assertEqual(e["state"], "met_with_help")
        self.assertIn("No hints opened (hints were available)", e["attempts"][0]["conditions"])

    def test_changed_condition_right_after_teaching_is_not_credited(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="read")
        self.attempt(10, "i1", "check", 2)
        e = self.state()
        self.assertTrue(e["checks"]["unaided"])
        self.assertFalse(e["checks"]["changed"])
        self.attempt(10 + 21 * 60, "i2", "check", 2)
        self.assertTrue(self.state()["checks"]["changed"])

    def test_help_of_unknown_size_is_not_unaided(self):
        self.attempt(0, "i1", "practice", 2, hints=1, described=False)
        e = self.state()
        self.assertFalse(e["checks"]["unaided"])
        self.assertIn("Help of unknown size", e["attempts"][0]["conditions"])

    def test_multiple_choice_is_kept_apart_from_evidence(self):
        self.add(0, "mcq.answered", rehearsal="r1", index=0, correct=True)
        e = self.state()
        self.assertEqual(e["state"], "untested")
        self.assertEqual((e["rehearsal_correct"], e["rehearsal_total"]), (1, 1))

    def test_coverage_counts_weight_not_shown_without_help(self):
        cov = coverage(self.pkg, derive(self.events, self.pkg))
        self.assertEqual(cov["weight_not_shown_unaided"], 100)
        self.attempt(0, "i1", "probe", 2)
        cov = coverage(self.pkg, derive(self.events, self.pkg))
        self.assertLess(cov["weight_not_shown_unaided"], 100)

    def test_every_way_of_viewing_restarts_the_teaching_clock(self):
        for modality in ("read", "listen", "present", "watch"):
            with self.subTest(modality=modality):
                self.events = []
                self.attempt(0, "i1", "check", 2)
                self.add(21 * 60, "lesson.viewed", lesson="l1", modality=modality, repeats_within_min=30)
                self.attempt(22 * 60, "i2", "check", 2)
                e = self.state()
                self.assertFalse(e["checks"]["later"], "a check an hour after a view is not later")
                self.assertLess(e["attempts"][-1]["minutes_since_teaching"], 60)
                self.assertEqual(e["lessons_viewed"], 1)
                self.assertEqual([x["modality"] for x in e["exposures"]], [modality])
                self.assertEqual(e["exposures"][0]["extent"], "unknown")

    def test_a_view_counts_as_teaching_for_the_longest_plausible_play(self):
        # A play that starts at t0 may still be going 29 minutes later, so the teaching clock starts at
        # t0 + 30 min: a check 20 h 15 min after the view is not later, and one 20 h 31 min after it is.
        for window in (30, None):  # as views are recorded now, and as they were before they stated a window
            for judged, later in ((20 * 60 + 15, False), (20 * 60 + 31, True)):
                with self.subTest(window=window, judged=judged):
                    self.events = []
                    self.add(0, "lesson.viewed", lesson="l1", modality="watch", **({"repeats_within_min": window} if window else {}))
                    self.assertEqual(self.state()["last_teaching"], (self.t0 + timedelta(minutes=30)).isoformat(timespec="seconds"))
                    self.attempt(judged - 1, "i1", "check", 2)  # judged a minute after it is issued
                    e = self.state()
                    self.assertEqual(e["attempts"][0]["minutes_since_teaching"], judged - 30)
                    self.assertEqual(e["checks"]["later"], later)

    def test_a_longer_window_stated_by_the_event_counts_in_full(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="listen", repeats_within_min=45)
        self.attempt(20 * 60 + 30, "i1", "check", 2)
        e = self.state()
        self.assertEqual(e["attempts"][0]["minutes_since_teaching"], 20 * 60 + 31 - 45)
        self.assertFalse(e["checks"]["later"])

    def test_a_later_answer_never_moves_the_teaching_clock_back(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="listen", repeats_within_min=30)
        self.attempt(3, "i1", "check", 2)
        self.attempt(20 * 60 + 5, "i2", "check", 2)
        e = self.state()
        self.assertEqual(e["attempts"][0]["minutes_since_teaching"], 0)
        # Counted from the end of the view's window (minute 30), not from the answer at minute 4.
        self.assertEqual(e["attempts"][1]["minutes_since_teaching"], 20 * 60 + 6 - 30)
        self.assertFalse(e["checks"]["later"])

    def test_views_recorded_before_the_window_existed_are_still_read(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="present")
        e = self.state()
        self.assertEqual(e["exposures"], [{"at": self.t0.isoformat(), "modality": "present", "lesson": "l1", "extent": "unknown"}])
        self.assertEqual(e["lessons_viewed"], 1)

    def test_a_podcast_view_from_an_earlier_version_still_counts_as_teaching(self):
        # An earlier version accepted "podcast" as a way of viewing. Such a view is read like any other,
        # never in the learner's favour, and it is not something the learner declared.
        self.attempt(0, "i1", "check", 2)
        self.add(21 * 60, "lesson.viewed", lesson="l1", modality="podcast")
        self.attempt(22 * 60, "i2", "check", 2)
        e = self.state()
        self.assertFalse(e["checks"]["later"])
        self.assertLess(e["attempts"][-1]["minutes_since_teaching"], 60)
        self.assertIsNone(e["attempts"][-1]["podcast_declared"])
        self.assertEqual(e["exposures"], [{"at": (self.t0 + timedelta(hours=21)).isoformat(), "modality": "podcast",
                                           "lesson": "l1", "extent": "unknown"}])

    def answer(self, minutes, item, judged=1, **declared):
        """A check issued at `minutes`, with a podcast declaration just before sending if one is given."""
        self.add(minutes, "item.issued", item=item, mode="check", kind="scenario", changed_condition=True)
        if declared:
            self.add(minutes, "podcast.declared", item=item, lesson="l1", **declared)
        self.add(minutes + judged, "answer.judged", item=item, mode="check", met=2, total=2, feedback_shown=True)
        return self.state()

    def test_a_declared_podcast_listen_is_teaching_at_that_moment(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="read", repeats_within_min=30)
        e = self.answer(21 * 60, "i1", heard=True)
        a = e["attempts"][-1]
        self.assertEqual(a["minutes_since_teaching"], 0)
        self.assertTrue(a["podcast_declared"])
        self.assertFalse(e["checks"]["later"])
        self.assertFalse(e["checks"]["changed"])
        self.assertIn("Right after teaching", a["conditions"])
        self.assertIn("Declared: listened in a podcast app before answering", a["conditions"])
        self.assertEqual(e["exposures"][-1], {"at": (self.t0 + timedelta(hours=21)).isoformat(), "modality": "podcast",
                                              "lesson": "l1", "extent": "unknown", "declared": True})
        self.assertEqual(e["lessons_viewed"], 1, "a listen the learner declared is exposure, but not a lesson view Dojo saw")

    def test_a_declared_no_leaves_later_standing_and_says_so(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="read", repeats_within_min=30)
        e = self.answer(21 * 60, "i1", heard=False)
        a = e["attempts"][-1]
        self.assertEqual(a["minutes_since_teaching"], 21 * 60 - 30)
        self.assertIs(a["podcast_declared"], False)
        self.assertTrue(e["checks"]["later"])
        self.assertTrue(e["checks"]["changed"])
        self.assertIn("Declared: no podcast listening since the last teaching", a["conditions"])
        self.assertEqual([x["modality"] for x in e["exposures"]], ["read"])
        self.assertEqual(e["lessons_viewed"], 1)

    def test_a_declared_answer_is_measured_to_the_declaration_not_to_a_slow_judgement(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="read", repeats_within_min=30)
        # Sent 19 h 55 min after the view; judged 45 minutes later, as a queued or repeated judgement can be.
        e = self.answer(19 * 60 + 55, "i1", judged=45, heard=False)
        self.assertEqual(e["attempts"][-1]["minutes_since_teaching"], 19 * 60 + 55 - 30)
        self.assertFalse(e["checks"]["later"])
        # A "yes" judged a day later still counts as right after teaching.
        e = self.answer(30 * 60, "i2", judged=24 * 60, heard=True)
        self.assertEqual(e["attempts"][-1]["minutes_since_teaching"], 0)
        self.assertFalse(e["checks"]["later"])

    def test_anything_but_an_explicit_no_counts_as_listened(self):
        for heard in (None, "no", 0, "false"):
            with self.subTest(heard=heard):
                self.events = []
                self.add(0, "lesson.viewed", lesson="l1", modality="read", repeats_within_min=30)
                e = self.answer(21 * 60, "i1", heard=heard)
                self.assertEqual(e["attempts"][-1]["minutes_since_teaching"], 0)
                self.assertFalse(e["checks"]["later"])

    def test_a_declaration_with_no_teaching_before(self):
        e = self.answer(0, "i1", heard=False)
        a = e["attempts"][-1]
        self.assertIsNone(a["minutes_since_teaching"])
        self.assertIn("No teaching before", a["conditions"])
        self.assertIn("Declared: no podcast listening before this answer", a["conditions"])
        self.assertTrue(e["checks"]["changed"])
        self.events = []
        e = self.answer(0, "i1", heard=True)
        self.assertEqual(e["attempts"][-1]["minutes_since_teaching"], 0)
        self.assertFalse(e["checks"]["changed"])

    def test_answers_without_a_declaration_read_as_before(self):
        self.add(0, "lesson.viewed", lesson="l1", modality="read", repeats_within_min=30)
        e = self.answer(21 * 60, "i1")
        a = e["attempts"][-1]
        self.assertIsNone(a["podcast_declared"])
        self.assertEqual(a["minutes_since_teaching"], 21 * 60 + 1 - 30)
        self.assertFalse([c for c in a["conditions"] if c.startswith("Declared")])


if __name__ == "__main__":
    unittest.main()
