"""Avatar video: the SSML sent, the rate limit, the subtitles that locate each turn, the storage
budget and how a video is served.

Nothing here touches the network: the batch synthesis endpoint, the signed result URL and the signed
subtitle URL are all answered by a fake transport, and the waits between calls are skipped.
"""
import dataclasses
import html
import json
import os
import re
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import avatar as avatar_mod  # noqa: E402
from app.avatar import (  # noqa: E402
    MEDIA_ID,
    RATE_LIMIT_STEP,
    Avatar,
    Pacer,
    avatar_config,
    build_ssml,
    lesson_turns,
    map_cues,
    media_id_for,
    normalise,
    parse_srt,
    safe_url,
    turns,
)
from app.core import Store, UserError  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402

ENDPOINT = "https://dojo-test.cognitiveservices.azure.com"
SIGNATURE = "?sv=2024-08-04&sig=THIS-MUST-NEVER-BE-LOGGED"
VIDEO = b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 8
TOKEN = "fake-token-for-tests"

# The narration of one slide, as a lesson really writes it: the guide explains, the coach asks.
DIALOGUE = [
    {"voice": "guide", "text": "For this exam skill, first decide the scope: repository or organization."},
    {"voice": "coach", "text": "So if the change was made on one repo, I start at that repo?"},
    {"voice": "guide", "text": "Yes. If you need broader visibility, use the organization path."},
    {"voice": "guide", "text": "Content exclusion hides files & folders."},
]
TURNS = [["Content exclusion hides files."]]

# Measured against the real service: one input of three guide turns, subtitles cut per sentence.
REAL_SRT = """1
00:00:00,050 --> 00:00:04,150
First, decide the scope: repository or organization.

2
00:00:05,750 --> 00:00:06,525
Yes.

3
00:00:06,625 --> 00:00:10,575
If you need broader visibility, use the organization path.

4
00:00:12,175 --> 00:00:16,337
Look for who last changed the content exclusion settings, and when.
"""
REAL_TURNS = [
    ["First, decide the scope: repository or organization."],
    ["Yes. If you need broader visibility, use the organization path."],
    ["Look for who last changed the content exclusion settings, and when."],
]


class FastClock:
    """Real time, no waiting: the rate limit would make these tests take many minutes."""

    sleep = staticmethod(lambda seconds: None)
    monotonic = staticmethod(time.monotonic)
    time = staticmethod(time.time)


class VirtualClock:
    """Time only moves when something sleeps, so a pacer can be measured exactly."""

    def __init__(self):
        self.now = 10_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def srt_for(ssml: str) -> str:
    """The subtitles the service would return for this SSML: one cue per sentence-sized line, a
    short gap inside a turn and a long one between turns."""
    inner = re.search(r'<voice name="[^"]+">(.*)</voice>', ssml, re.S).group(1)
    cues, at = [], 0.05
    for turn in inner.split(f'<break time="{avatar_mod.TURN_BREAK_MS}ms"/>'):
        for line in turn.split(f'<break time="{avatar_mod.LINE_BREAK_MS}ms"/>'):
            text = html.unescape(line.strip())
            if not text:
                continue
            end = at + max(1.0, len(text) / 15)
            cues.append((at, end, text))
            at = end + avatar_mod.LINE_BREAK_MS / 1000
        at += (avatar_mod.TURN_BREAK_MS - avatar_mod.LINE_BREAK_MS) / 1000
    return "\n\n".join(f"{i}\n{avatar_mod._stamp(s)} --> {avatar_mod._stamp(e)}\n{t}"
                       for i, (s, e, t) in enumerate(cues, 1)) + "\n"


class FakeService:
    """The batch avatar endpoint: PUT starts a job, GET walks it from NotStarted to Succeeded, and
    the signed URLs serve the video and its subtitles."""

    def __init__(self, fail: str = "", busy: int = 0, conflict: bool = False, subtitle: bool = True):
        self.jobs: dict[str, int] = {}
        self.srt: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.fail = fail
        self.busy = busy
        self.conflict = conflict
        self.subtitle = subtitle

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        if request.url.host == "files.example":
            if request.headers.get("authorization"):
                return httpx.Response(400, text="a signed URL must not carry a bearer token")
            name = request.url.path.rsplit("/", 1)[-1]
            sid, kind = name.rsplit(".", 1)
            if kind == "srt":
                return httpx.Response(200, text=self.srt.get(sid, ""), headers={"content-type": "text/plain"})
            return httpx.Response(200, content=VIDEO, headers={"content-type": "video/webm"})
        if not request.url.path.startswith("/avatar/batchsyntheses/"):
            return httpx.Response(404, text="wrong path")
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, text="not signed in")
        if request.url.params.get("api-version") != "2024-08-01":
            return httpx.Response(400, text="wrong api version")
        sid = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            if self.busy > 0:
                self.busy -= 1
                return httpx.Response(429, text="exceeded call rate limit of your current AIServices S0 pricing",
                                      headers={"retry-after": "2"})
            body = json.loads(request.content)
            self.bodies.append(body)
            self.srt[sid] = srt_for(body["inputs"][0]["content"])
            if self.conflict:  # a job with this id is already running from an earlier attempt
                self.jobs.setdefault(sid, 0)
                return httpx.Response(409, text="synthesis already exists")
            self.jobs[sid] = 0
            return httpx.Response(201, json={"id": sid, "status": "NotStarted"})
        if request.method == "GET":
            seen = self.jobs.get(sid)
            if seen is None:
                return httpx.Response(404, text="no such synthesis")
            self.jobs[sid] = seen + 1
            if self.fail:
                return httpx.Response(200, json={"id": sid, "status": "Failed", "properties": {"error": self.fail}})
            status = ("NotStarted", "Running", "Succeeded")[min(seen, 2)]
            body: dict = {"id": sid, "status": status}
            if status == "Succeeded":
                outputs = {"result": f"https://files.example/out/{sid}.webm{SIGNATURE}",
                           "summary": f"https://files.example/out/{sid}.json{SIGNATURE}"}
                if self.subtitle:
                    outputs["subtitle"] = f"https://files.example/out/{sid}.srt{SIGNATURE}"
                body["outputs"] = outputs
                body["properties"] = {"durationInMilliseconds": 31000, "sizeInBytes": len(VIDEO)}
            return httpx.Response(200, json=body)
        return httpx.Response(405, text="not allowed")


def avatar_for(service: FakeService, tmp: str) -> Avatar:
    conf = dataclasses.replace(settings(tmp), speech_endpoint=ENDPOINT)
    tokens = mock.Mock()
    tokens.get.return_value = TOKEN
    http = httpx.Client(transport=httpx.MockTransport(service.handle))
    return Avatar(conf, tokens, Store(conf.data_dir), http=http, pacer=Pacer(avatar_mod.CREATE_INTERVAL_S))


class NarrationTests(unittest.TestCase):
    def test_a_conversation_becomes_one_turn_per_speaker_change(self):
        self.assertEqual(turns(DIALOGUE), [
            ("guide", ["For this exam skill, first decide the scope: repository or organization."]),
            ("coach", ["So if the change was made on one repo, I start at that repo?"]),
            ("guide", ["Yes. If you need broader visibility, use the organization path.",
                       "Content exclusion hides files & folders."]),
        ], "consecutive lines by one speaker are merged, the other speaker is not")

    def test_empty_and_unknown_speakers_are_handled(self):
        self.assertEqual(turns([{"text": "One."}, {"voice": "narrator", "text": "Two."}]), [("guide", ["One.", "Two."])])
        self.assertEqual(turns([{"voice": "guide", "text": "   "}]), [])

    def test_each_speaker_has_their_own_presenter_and_voice(self):
        self.assertIn('<voice name="en-US-AndrewMultilingualNeural">', build_ssml("guide", [["Hello."]]))
        self.assertIn('<voice name="en-US-AvaMultilingualNeural">', build_ssml("coach", [["Hello."]]))
        self.assertEqual(avatar_config("guide")["talkingAvatarCharacter"], "harry")
        self.assertEqual(avatar_config("coach")["talkingAvatarCharacter"], "meg")
        for speaker in ("guide", "coach"):
            self.assertEqual(avatar_config(speaker)["talkingAvatarStyle"], "business")
            self.assertEqual(avatar_config(speaker)["videoCodec"], "vp9")
            self.assertEqual(avatar_config(speaker)["subtitleType"], "external_file")

    def test_one_job_holds_every_turn_of_one_speaker(self):
        ssml = build_ssml("guide", [["First turn.", "Still me."], ["Second turn."]])
        self.assertEqual(ssml.count("<voice"), 1, "one input, one voice")
        self.assertEqual(ssml.count('<break time="1500ms"/>'), 1, "one long break between the two turns")
        self.assertEqual(ssml.count('<break time="300ms"/>'), 1, "a short break inside the first turn")
        self.assertLess(ssml.index("First turn."), ssml.index("Second turn."), "speaking order is kept")

    def test_markup_in_the_narration_cannot_escape_into_the_ssml(self):
        ssml = build_ssml("guide", [['</voice><voice name="other">hijacked']])
        self.assertEqual(ssml.count("<voice"), 1)
        self.assertIn("&lt;/voice&gt;", ssml)

    def test_empty_lines_are_left_out(self):
        self.assertNotIn("break", build_ssml("guide", [["One.", "   "]]))

    def test_the_media_id_is_a_content_hash(self):
        first = media_id_for("guide", TURNS)
        self.assertTrue(MEDIA_ID.match(first), first)
        self.assertEqual(first, media_id_for("guide", TURNS))
        self.assertNotEqual(first, media_id_for("guide", [["Something else."]]))
        self.assertNotEqual(first, media_id_for("guide", TURNS + [["One more turn."]]),
                            "adding a turn to the lesson makes a new film")

    def test_the_same_words_by_the_other_speaker_are_a_different_film(self):
        self.assertNotEqual(media_id_for("guide", [["Same words."]]), media_id_for("coach", [["Same words."]]))

    def test_changing_the_video_settings_changes_the_media_id(self):
        before = media_id_for("guide", TURNS)
        with mock.patch.dict(avatar_mod.VIDEO_CONFIG, {"bitrateKbps": 1234}):
            self.assertNotEqual(before, media_id_for("guide", TURNS))

    def test_a_lesson_is_split_into_one_job_per_speaker(self):
        per_speaker, layout = lesson_turns([DIALOGUE, [{"voice": "coach", "text": "And after that?"}]])
        self.assertEqual(list(per_speaker), ["guide", "coach"])
        self.assertEqual(len(per_speaker["guide"]), 2)
        self.assertEqual(len(per_speaker["coach"]), 2)
        self.assertEqual(layout, [[("guide", 0), ("coach", 0), ("guide", 1)], [("coach", 1)]],
                         "every turn knows where it sits in its speaker's film")

    def test_a_signed_url_keeps_only_its_path(self):
        self.assertEqual(safe_url("https://files.example/out/clip.webm" + SIGNATURE),
                         "https://files.example/out/clip.webm")
        self.assertNotIn("sig=", safe_url("https://files.example/out/clip.webm" + SIGNATURE))


class SubtitleTests(unittest.TestCase):
    """Finding each turn inside the one video, from the subtitle file the service returns."""

    def test_the_cues_of_a_real_subtitle_file_are_read(self):
        cues = parse_srt(REAL_SRT)
        self.assertEqual(len(cues), 4)
        self.assertEqual((cues[0]["start"], cues[0]["end"]), (0.05, 4.15))
        self.assertEqual(cues[1]["text"], "Yes.")
        self.assertEqual(cues[3]["end"], 16.337)

    def test_sentence_cues_are_gathered_back_into_turns(self):
        self.assertEqual(map_cues(parse_srt(REAL_SRT), REAL_TURNS), [
            {"start": 0.05, "end": 4.15},
            {"start": 5.75, "end": 10.575},   # "Yes." and the sentence after it are one turn
            {"start": 12.175, "end": 16.337},
        ])

    def test_the_gaps_between_turns_match_the_silence_in_the_file(self):
        spans = map_cues(parse_srt(REAL_SRT), REAL_TURNS)
        gaps = [round(b["start"] - a["end"], 3) for a, b in zip(spans, spans[1:])]
        self.assertTrue(all(g >= avatar_mod.MIN_TURN_GAP_S for g in gaps), gaps)

    def test_a_subtitle_that_does_not_match_the_narration_is_refused(self):
        with self.assertRaises(UserError) as ctx:
            map_cues(parse_srt(REAL_SRT), [["Something completely different."]] + REAL_TURNS[1:])
        self.assertIn("could not follow", str(ctx.exception))

    def test_a_subtitle_that_stops_early_is_refused(self):
        with self.assertRaises(UserError) as ctx:
            map_cues(parse_srt(REAL_SRT), REAL_TURNS + [["One more turn nobody filmed."]])
        self.assertIn("whole narration", str(ctx.exception))

    def test_a_turn_that_ends_mid_sentence_is_refused(self):
        with self.assertRaises(UserError):
            map_cues(parse_srt(REAL_SRT), REAL_TURNS[:-1] + [[REAL_TURNS[-1][0] + " And one more sentence."]])

    def test_turns_that_run_together_are_refused_rather_than_guessed(self):
        tight = REAL_SRT.replace("00:00:05,750", "00:00:04,400")
        with self.assertRaises(UserError) as ctx:
            map_cues(parse_srt(tight), REAL_TURNS)
        self.assertIn("tell the presenter's turns apart", str(ctx.exception))

    def test_narration_without_words_is_refused(self):
        with self.assertRaises(UserError):
            map_cues(parse_srt(REAL_SRT), [["..."]])

    def test_punctuation_and_case_do_not_matter(self):
        self.assertEqual(normalise("Yes -- 'the' ORG path!"), "yestheorgpath")


class PacerTests(unittest.TestCase):
    """Two create calls a minute for the whole resource means one queue for the whole process."""

    def test_the_first_call_goes_out_at_once_and_the_next_one_waits(self):
        clock = VirtualClock()
        pacer = Pacer(31.0, clock=clock)
        self.assertEqual(pacer.wait(), 0.0)
        self.assertEqual(clock.slept, [])
        clock.now += 5
        self.assertEqual(pacer.wait(), 26.0)
        self.assertEqual(clock.slept, [26.0])

    def test_a_caller_that_has_to_wait_is_told_why(self):
        clock = VirtualClock()
        pacer = Pacer(31.0, clock=clock)
        notes: list[str] = []
        pacer.wait(notes.append)
        self.assertEqual(notes, [], "nobody waits for the first call")
        pacer.wait(notes.append)
        self.assertEqual(notes, [RATE_LIMIT_STEP])

    def test_a_call_long_after_the_last_one_does_not_wait(self):
        clock = VirtualClock()
        pacer = Pacer(31.0, clock=clock)
        pacer.wait()
        clock.now += 120
        self.assertEqual(pacer.wait(), 0.0)

    def test_every_avatar_shares_one_pacer_unless_told_otherwise(self):
        conf = dataclasses.replace(settings(tempfile.mkdtemp(prefix="dojo-pacer-")), speech_endpoint=ENDPOINT)
        store = Store(conf.data_dir)
        self.assertIs(Avatar(conf, None, store).pacer, avatar_mod.PACER)
        self.assertIs(Avatar(conf, None, store).pacer, Avatar(conf, None, store).pacer)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-avatar-")
        patcher = mock.patch.object(avatar_mod, "time", FastClock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_speaker_is_filmed_once_and_then_reused(self):
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        media_id = avatar.synthesize("guide", REAL_TURNS)

        self.assertEqual(media_id, media_id_for("guide", REAL_TURNS))
        self.assertTrue(avatar.exists(media_id))
        self.assertEqual(avatar.store.read_bytes("media", media_id + ".webm"), VIDEO)
        self.assertEqual(service.count("PUT"), 1, "one create for three turns")

        self.assertEqual(avatar.synthesize("guide", REAL_TURNS), media_id)
        self.assertEqual(service.count("PUT"), 1, "the same narration is never filmed twice")

    def test_the_timings_of_every_turn_are_stored_beside_the_video(self):
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        media_id = avatar.synthesize("guide", REAL_TURNS)

        spans = avatar.timings(media_id)
        self.assertEqual(len(spans), len(REAL_TURNS))
        self.assertEqual(spans, json.loads(avatar.store.path("media", media_id + ".json").read_text("utf-8")))
        for before, after in zip(spans, spans[1:]):
            self.assertLess(before["start"], before["end"])
            self.assertGreaterEqual(after["start"] - before["end"], avatar_mod.MIN_TURN_GAP_S)

    def test_a_video_without_its_timings_does_not_count_as_filmed(self):
        avatar = avatar_for(FakeService(), self.tmp)
        media_id = avatar.synthesize("guide", REAL_TURNS)
        avatar.store.delete_file("media", media_id + ".json")
        self.assertFalse(avatar.exists(media_id))
        self.assertIsNone(avatar.timings(media_id))

    def test_the_request_asks_for_a_transparent_cropped_webm_with_subtitles(self):
        service = FakeService()
        avatar_for(service, self.tmp).synthesize("guide", TURNS)
        body = service.bodies[0]
        self.assertEqual(body["inputKind"], "SSML")
        self.assertEqual(len(body["inputs"]), 1, "several inputs need a storage account we do not have")
        self.assertIn("en-US-AndrewMultilingualNeural", body["inputs"][0]["content"])
        config = body["avatarConfig"]
        self.assertEqual(config["talkingAvatarCharacter"], "harry")
        self.assertEqual(config["talkingAvatarStyle"], "business")
        self.assertEqual((config["videoFormat"], config["videoCodec"]), ("webm", "vp9"))
        self.assertEqual(config["backgroundColor"], "#00000000")
        self.assertEqual(config["subtitleType"], "external_file")
        self.assertEqual(config["videoCrop"]["topLeft"], {"x": 640, "y": 0})
        self.assertEqual(config["videoCrop"]["bottomRight"], {"x": 1280, "y": 1080})
        self.assertLessEqual(config["bitrateKbps"], 1000)
        self.assertGreater(body["properties"]["timeToLiveInHours"], 0)

    def test_the_coach_is_filmed_by_the_other_presenter(self):
        service = FakeService()
        avatar_for(service, self.tmp).synthesize("coach", [["So where do I start?"]])
        self.assertEqual(service.bodies[0]["avatarConfig"]["talkingAvatarCharacter"], "meg")
        self.assertIn("en-US-AvaMultilingualNeural", service.bodies[0]["inputs"][0]["content"])

    def test_finished_jobs_are_left_to_expire_instead_of_being_deleted(self):
        service = FakeService()
        avatar_for(service, self.tmp).synthesize("guide", TURNS)
        self.assertEqual(service.count("DELETE"), 0, "a delete may cost one of the two calls a minute")

    def test_every_create_waits_for_its_turn_at_the_pacer(self):
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        clock = VirtualClock()
        avatar.pacer = Pacer(31.0, clock=clock)
        avatar.synthesize("guide", TURNS)
        avatar.synthesize("coach", TURNS)
        self.assertEqual(service.count("PUT"), 2)
        self.assertEqual(clock.slept, [31.0], "the second create waits a full interval")

    def test_the_rate_limit_is_waited_out_and_the_learner_is_told(self):
        service = FakeService(busy=2)
        avatar = avatar_for(service, self.tmp)
        notes: list[str] = []
        self.assertTrue(avatar.exists(avatar.synthesize("guide", TURNS, notes.append)))
        self.assertEqual(service.count("PUT"), 3)
        self.assertIn(RATE_LIMIT_STEP, notes)

    def test_the_service_is_asked_only_a_few_times_before_giving_up(self):
        service = FakeService(busy=99)
        with self.assertRaises(UserError) as ctx:
            avatar_for(service, self.tmp).synthesize("guide", TURNS)
        self.assertEqual(service.count("PUT"), avatar_mod.CREATE_ATTEMPTS)
        self.assertIn("video service", str(ctx.exception))

    def test_a_job_that_is_already_running_is_joined_instead_of_started_again(self):
        service = FakeService(conflict=True)
        avatar = avatar_for(service, self.tmp)
        media_id = avatar.synthesize("guide", REAL_TURNS)
        self.assertTrue(avatar.exists(media_id))
        self.assertEqual(service.count("PUT"), 1, "409 means the film we want is already being made")
        self.assertEqual(service.count("DELETE"), 0)

    def test_the_synthesis_id_is_accepted_by_the_service_rules(self):
        service = FakeService()
        avatar_for(service, self.tmp).synthesize("guide", TURNS)
        sid = next(url for method, url in service.calls if method == "PUT").rsplit("/", 1)[-1].split("?")[0]
        self.assertRegex(sid, r"^[A-Za-z0-9][A-Za-z0-9_-]{1,62}[A-Za-z0-9]$")
        self.assertLessEqual(len(sid), 64)

    def test_a_failed_job_is_reported_and_nothing_is_stored(self):
        service = FakeService(fail="avatar character not available")
        avatar = avatar_for(service, self.tmp)
        with self.assertRaises(UserError) as ctx:
            avatar.synthesize("guide", TURNS)
        self.assertIn("avatar character not available", str(ctx.exception))
        self.assertFalse(avatar.exists(media_id_for("guide", TURNS)))
        self.assertEqual(avatar.videos(), [])

    def test_a_job_without_subtitles_is_refused_because_turns_cannot_be_found(self):
        service = FakeService(subtitle=False)
        avatar = avatar_for(service, self.tmp)
        with self.assertRaises(UserError) as ctx:
            avatar.synthesize("guide", REAL_TURNS)
        self.assertIn("when each turn is spoken", str(ctx.exception))
        self.assertEqual(avatar.videos(), [], "a video nobody can navigate is not worth keeping")

    def test_narration_that_is_far_too_long_is_refused_before_any_call(self):
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        with self.assertRaises(UserError):
            avatar.synthesize("guide", [["word " * 20000]])
        self.assertEqual(service.calls, [])

    def test_the_signed_urls_are_never_written_to_the_log(self):
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        with self.assertLogs("dojo.avatar", level="DEBUG") as logs:
            avatar.synthesize("guide", REAL_TURNS)
        written = "\n".join(logs.output)
        self.assertNotIn("sig=", written)
        self.assertNotIn("THIS-MUST-NEVER-BE-LOGGED", written)
        self.assertNotIn(TOKEN, written)

    def test_a_probe_leaves_no_video_behind(self):
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        probe = avatar.probe()
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["status"], "Succeeded")
        self.assertEqual(service.count("PUT"), 1)
        self.assertEqual(service.count("DELETE"), 0)
        self.assertEqual(avatar.videos(), [], "a probe stores nothing")
        self.assertIsNotNone(avatar.route())

    def test_a_probe_waits_at_the_same_pacer_as_a_lesson(self):
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        clock = VirtualClock()
        avatar.pacer = Pacer(31.0, clock=clock)
        avatar.synthesize("guide", TURNS)
        avatar.probe()
        self.assertEqual(clock.slept, [31.0])

    def test_a_probe_reports_a_broken_route(self):
        service = FakeService(fail="quota exceeded")
        probe = avatar_for(service, self.tmp).probe()
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["status"], "Failed")

    def test_the_two_presenters_render_side_by_side_once_they_are_started(self):
        import threading
        service = FakeService()
        avatar = avatar_for(service, self.tmp)
        inside, hold = threading.Semaphore(0), threading.Event()
        original = avatar._download

        def slow(url: str, what: str) -> bytes:
            inside.release()
            hold.wait(5)
            return original(url, what)

        avatar._download = slow
        work = [("guide", REAL_TURNS), ("coach", [["And then what?"]])]
        threads = [threading.Thread(target=avatar.synthesize, args=job) for job in work]
        for t in threads:
            t.start()
        try:
            for _ in range(2):
                self.assertTrue(inside.acquire(timeout=5), "both presenters should be in flight at once")
        finally:
            hold.set()
            for t in threads:
                t.join(10)
        self.assertEqual(service.count("PUT"), 2)
        for speaker, turn_texts in work:
            self.assertTrue(avatar.exists(media_id_for(speaker, turn_texts)))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-budget-")
        self.avatar = avatar_for(FakeService(), self.tmp)
        self.store = self.avatar.store

    def put(self, name: str, size: int, age_s: float) -> None:
        self.store.write_bytes("media", name + ".webm", data=bytes(size))
        self.store.write("media", name, value=[{"start": 0.0, "end": 1.0}])
        when = time.time() - age_s
        for part in (name + ".webm", name + ".json"):
            os.utime(self.store.path("media", part), (when, when))

    def test_the_coldest_videos_go_first_and_audio_is_never_touched(self):
        self.put("v" + "0" * 39, 400, age_s=300)   # coldest video
        self.put("v" + "1" * 39, 400, age_s=200)
        self.put("v" + "2" * 39, 400, age_s=100)   # warmest video
        self.store.write_bytes("media", "a" + "0" * 39 + ".mp3", data=bytes(900))  # older, but narration
        self.store.write("content", "lessons", "lesson-" + "0" * 20, value={"id": "keep me"})

        with mock.patch.object(avatar_mod, "BUDGET_BYTES", 1000):
            removed = self.avatar.evict()

        self.assertEqual(removed, ["v" + "0" * 39], "the least recently served video goes first")
        self.assertFalse(self.avatar.exists("v" + "0" * 39))
        self.assertTrue(self.avatar.exists("v" + "1" * 39))
        self.assertTrue(self.store.exists("media", "a" + "0" * 39 + ".mp3"), "narration must survive")
        self.assertEqual(self.store.read("content", "lessons", "lesson-" + "0" * 20)["id"], "keep me")
        self.assertLessEqual(self.avatar.usage()["bytes"], 1000)

    def test_the_timings_are_thrown_away_with_their_video(self):
        self.put("v" + "0" * 39, 900, age_s=300)
        self.assertTrue(self.store.exists("media", "v" + "0" * 39 + ".json"))
        with mock.patch.object(avatar_mod, "BUDGET_BYTES", 100):
            self.avatar.evict()
        self.assertFalse(self.store.exists("media", "v" + "0" * 39 + ".json"), "no orphan sidecars")
        self.assertFalse(self.store.exists("media", "v" + "0" * 39 + ".webm"))

    def test_room_is_made_for_the_video_that_is_coming(self):
        self.put("v" + "0" * 39, 400, age_s=300)
        self.put("v" + "1" * 39, 400, age_s=200)
        with mock.patch.object(avatar_mod, "BUDGET_BYTES", 1000):
            removed = self.avatar.evict(incoming=500)
        self.assertEqual(len(removed), 1)
        self.assertLessEqual(self.avatar.usage()["bytes"] + 500, 1000)

    def test_storing_a_new_video_evicts_the_coldest_one(self):
        self.put("v" + "0" * 39, 900, age_s=300)
        with mock.patch.object(avatar_mod, "time", FastClock), mock.patch.object(avatar_mod, "BUDGET_BYTES", 1000):
            fresh = self.avatar.synthesize("guide", TURNS)
        self.assertTrue(self.avatar.exists(fresh))
        self.assertFalse(self.avatar.exists("v" + "0" * 39), "the old video made room")

    def test_being_served_keeps_a_video_warm(self):
        self.put("v" + "0" * 39, 400, age_s=300)
        self.put("v" + "1" * 39, 400, age_s=200)
        self.avatar.touch("v" + "0" * 39)  # the learner just watched the older one
        with mock.patch.object(avatar_mod, "BUDGET_BYTES", 500):
            removed = self.avatar.evict()
        self.assertEqual(removed, ["v" + "1" * 39])

    def test_usage_reports_the_budget(self):
        self.put("v" + "0" * 39, 400, age_s=10)
        usage = self.avatar.usage()
        self.assertEqual((usage["videos"], usage["bytes"]), (1, 400))
        self.assertEqual(usage["budget_bytes"], avatar_mod.BUDGET_BYTES)


class VideoApiTests(unittest.TestCase):
    """The whole route in local mode: make a lesson, film it, then watch it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-video-api-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        pkg = cls.c.get("/api/packages/gh-300").json()
        sid = next(s["id"] for d in pkg["domains"] for g in d["groups"] for s in g["skills"] if s["kind"] == "scenario")
        job = cls.c.post("/api/lessons", json={"package": "gh-300", "skill": sid}, headers=POST).json()
        for _ in range(400):
            job = cls.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        if job["state"] != "done":
            raise AssertionError(f"the test lesson could not be written: {job.get('error')}")
        cls.lesson_id = job["result"]["lesson"]

    def lesson(self) -> dict:
        return self.c.get(f"/api/lessons/{self.lesson_id}").json()

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def wait(self, job: dict) -> dict:
        for _ in range(400):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def test_01_a_lesson_has_no_video_until_it_is_asked_for(self):
        lesson = self.lesson()
        self.assertEqual(len(lesson["video"]), len(lesson["slides"]))
        for slide, clips in zip(lesson["slides"], lesson["video"]):
            self.assertEqual(len(clips), len(turns(slide["narration"])), "one entry per speaker turn")
            self.assertTrue(all(c["speaker"] in ("guide", "coach") for c in clips))
            self.assertTrue(all(c["media"] is None and c["start"] is None and c["end"] is None for c in clips),
                            "nothing is filmed in bulk")

    def test_02_filming_needs_the_dojo_header(self):
        r = self.c.post(f"/api/lessons/{self.lesson_id}/video")
        self.assertEqual(r.status_code, 403)

    def test_03_unknown_lessons_are_refused(self):
        self.assertEqual(self.c.post("/api/lessons/not-an-id/video", headers=POST).status_code, 404)
        self.assertEqual(self.c.post("/api/lessons/lesson-" + "0" * 20 + "/video", headers=POST).status_code, 404)

    def test_04_one_film_per_speaker_covers_the_whole_lesson(self):
        result = self.wait(self.post(f"/api/lessons/{self.lesson_id}/video"))
        lesson = self.lesson()
        self.assertEqual(result["lesson"], self.lesson_id)
        self.assertEqual(result["video"], lesson["video"])
        clips = [c for slide in lesson["video"] for c in slide]
        self.assertTrue(clips)
        self.assertTrue(all(MEDIA_ID.match(c["media"]) for c in clips))
        self.assertLessEqual(len({c["media"] for c in clips}), len(avatar_mod.PRESENTERS),
                             "at most one video per speaker, however many turns there are")
        self.assertEqual(len(lesson["audio"]), len(lesson["slides"]), "audio still works the same way")

    def test_05_every_turn_says_where_to_start_and_stop(self):
        lesson = self.lesson()
        per_media: dict[str, list[tuple[float, float]]] = {}
        for clips in lesson["video"]:
            for clip in clips:
                self.assertIsInstance(clip["start"], float)
                self.assertGreater(clip["end"], clip["start"])
                per_media.setdefault(clip["media"], []).append((clip["start"], clip["end"]))
        for spans in per_media.values():
            for before, after in zip(spans, spans[1:]):
                self.assertGreaterEqual(after[0] - before[1], avatar_mod.MIN_TURN_GAP_S,
                                        "one speaker's turns follow each other in their own film")

    def test_06_the_turns_come_from_the_narration_the_lesson_shows(self):
        lesson = self.lesson()
        expected = self.app.state.dojo.avatar.lesson_clips(
            [sl["narration"] for sl in lesson["slides"]])
        self.assertEqual(lesson["video"], expected, "only checked narration is spoken")

    def test_07_a_video_is_served_with_the_right_type_and_can_be_sought(self):
        video_id = self.lesson()["video"][0][0]["media"]
        whole = self.c.get(f"/api/media/{video_id}")
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(whole.headers["content-type"], "video/webm")
        self.assertEqual(whole.headers["accept-ranges"], "bytes")

        part = self.c.get(f"/api/media/{video_id}", headers={"Range": "bytes=10-19"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, whole.content[10:20])
        self.assertEqual(part.headers["content-range"], f"bytes 10-19/{len(whole.content)}")
        self.assertEqual(part.headers["content-length"], "10")

        tail = self.c.get(f"/api/media/{video_id}", headers={"Range": "bytes=-16"})
        self.assertEqual((tail.status_code, tail.content), (206, whole.content[-16:]))
        self.assertEqual(self.c.get(f"/api/media/{video_id}", headers={"Range": "bytes=999999-"}).status_code, 416)

    def test_08_audio_is_still_served_the_old_way_and_can_be_sought_too(self):
        audio_id = self.wait(self.post(f"/api/lessons/{self.lesson_id}/audio"))["audio"][0]
        whole = self.c.get(f"/api/media/{audio_id}")
        self.assertEqual((whole.status_code, whole.headers["content-type"]), (200, "audio/mpeg"))
        part = self.c.get(f"/api/media/{audio_id}", headers={"Range": "bytes=0-2"})
        self.assertEqual((part.status_code, part.content), (206, b"ID3"))

    def test_09_media_may_be_cached_privately_but_nothing_else_is(self):
        video_id = self.lesson()["video"][0][0]["media"]
        self.assertEqual(self.c.get(f"/api/media/{video_id}").headers["cache-control"], "private, max-age=86400")
        self.assertEqual(self.c.get("/api/me").headers["cache-control"], "no-store")

    def test_10_the_timings_sidecar_is_not_served_as_media(self):
        video_id = self.lesson()["video"][0][0]["media"]
        self.assertEqual(self.c.get(f"/api/media/{video_id}").headers["content-type"], "video/webm")
        self.assertEqual(self.c.get(f"/api/media/{video_id}.json").status_code, 404)

    def test_11_a_video_id_that_is_not_here_is_not_found(self):
        self.assertEqual(self.c.get("/api/media/v" + "0" * 39).status_code, 404)
        self.assertEqual(self.c.get("/api/media/v" + "0" * 38).status_code, 404)
        self.assertEqual(self.c.get("/api/media/vnothex" + "0" * 33).status_code, 404)

    def test_12_the_owner_can_probe_the_avatar_route_on_demand(self):
        result = self.post("/api/diag/run")
        self.assertTrue(result["checks"]["avatar"])
        self.assertEqual(result["details"]["avatar"]["route"], "stub")
        self.assertIn("video_storage", result["details"])
        self.assertIn("video_storage", self.c.get("/api/diag").json())

    def test_13_the_background_self_test_does_not_film_anything(self):
        checks = self.app.state.selftest.run()["checks"]
        self.assertNotIn("avatar", checks)
        self.assertTrue(checks["stt"])  # speaking is cheap to check, so the deploy gate sees it


class ReuseTests(unittest.TestCase):
    def test_asking_twice_while_it_runs_returns_the_same_job(self):
        import threading
        app = create_app(settings(tempfile.mkdtemp(prefix="dojo-reuse-")),
                         http=httpx.Client(transport=httpx.MockTransport(web)))
        dojo, me = app.state.dojo, app.state.preparer.owner
        release = threading.Event()
        first = dojo.jobs.start(me, "video", lambda progress: release.wait(5), ref="lesson-" + "a" * 20)
        try:
            same = dojo.jobs.existing(me, "video", "lesson-" + "a" * 20)
            self.assertEqual(same["id"], first["id"])
            self.assertIsNone(dojo.jobs.existing(me, "video", "lesson-" + "b" * 20))
            self.assertIsNone(dojo.jobs.existing(me, "audio", "lesson-" + "a" * 20))
        finally:
            release.set()
        for _ in range(100):
            if dojo.jobs.get(me, first["id"])["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertIsNone(dojo.jobs.existing(me, "video", "lesson-" + "a" * 20), "a finished job is not reused")


if __name__ == "__main__":
    unittest.main()
