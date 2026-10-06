"""Deeper Listen (ADR 0007): a two-voice conversation that teaches the whole lesson, written from the checked
lesson and checked line by line like the slide narration. Listen and the podcast play it once it is ready;
Watch and Present keep the slide narration. The Preparer writes it in the background, at most DEEP_PER_DAY
a day."""
import dataclasses
import json
import os
import re
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import delivery, learning, prepare  # noqa: E402
from app.ai import AIError  # noqa: E402
from app.core import UserError, utcnow  # noqa: E402
from app.learning import _gives_away  # noqa: E402
from app.main import create_app  # noqa: E402
from app.podcast import episode_key, join_mp3  # noqa: E402
from app.prepare import Preparer  # noqa: E402
from app.speech import media_id_for  # noqa: E402
from tests.test_api import GITHUB_MD, POST, settings, web  # noqa: E402
from tests.test_costs import LIVE, MODELS, SAMPLE, PriceDesk  # noqa: E402
from tests.test_delivery import Ears  # noqa: E402
from tests.test_drift import NEW, OTHER, PAGE, PID, Q2, S, DriftCase  # noqa: E402
from tests.test_podcast import mp3, narrated_lesson  # noqa: E402

PARTS = ["Opening", "Key idea", "Details", "Worked example", "Misconceptions", "Exam traps", "Summary"]
QUESTION = "Who can manage content exclusion settings for a repository?"
ANSWER = "Repository administrators and organization owners manage the exclusion settings."
ASKS = "So who can manage content exclusion settings for a repository?"
TELLS = "Here is the answer: repository administrators and organization owners manage the exclusion settings."
# A self-check with a short answer (review round 1): caught only next to its question.
SHORT_Q = "Which GitHub Copilot feature should you use to explain a block of code?"
SHORT_A = "Use GitHub Copilot Chat."
SHORT_SAYS = "The exam may ask which GitHub Copilot feature you should use to explain a block of code."
TEACHES = "Use GitHub Copilot Chat when the code is new to you."
NEXT_TO = "It gives a self-check answer next to its question. Deeper Listen leaves the self-check to Dojo."
# A self-check question too short for the near-duplicate test (review, fix check): it counts said whole.
TINY_Q = "Who can enroll?"
TINY_A = "Only administrators."
TINY_TEACHES = "Only administrators can change the policy, so find one early."
ASKS_IT = "It asks a self-check question. Deeper Listen leaves the self-check to Dojo."


class Models:
    """The stub models, with a hand on them: the gate can say that the lines of some parts do not hold,
    lines can be added to what the author writes, and the lesson's self-check can be set. It keeps the
    author's Deeper Listen prompts and what the gate was asked, per writing round."""

    def __init__(self, dojo):
        self.dojo, self.real = dojo, dojo.ai.chat
        self.round = 0
        self.fail: dict[int, set[str]] = {}          # round -> parts whose lines do not hold
        self.always: set[str] = set()                # parts whose lines never hold
        self.extra: dict[int, dict[str, list]] = {}  # round -> part -> lines the author adds
        self.self_check: list[dict] | None = None
        self.prompts: list[str] = []
        self.gated: list[tuple[int, str]] = []

    def __call__(self, role, system, user, json_mode=True):
        out = self.real(role, system, user, json_mode)
        task = re.search(r"TASK: (\w+)", system).group(1)
        if task == "lesson" and self.self_check is not None:
            data = json.loads(out)
            first = data["self_check"][0]
            data["self_check"] = [{**q, "quote": first["quote"], "source": first["source"]} for q in self.self_check]
            out = json.dumps(data)
        elif task == "deep_narration":
            self.round += 1
            self.prompts.append(user)
            data = json.loads(out)
            for seg in data["segments"]:
                seg["lines"] += self.extra.get(self.round, {}).get(seg["part"], [])
            out = json.dumps(data)
        elif task == "gate_elements" and "Deeper Listen part" in user:
            self.gated.append((self.round, user))
            data = json.loads(out)
            failing = self.always | self.fail.get(self.round, set())
            for v in data["verdicts"]:
                if v["id"].split(".")[0] in failing:
                    v.update(holds=False, reason="The sources do not say this.")
            out = json.dumps(data)
        return out

    def ids(self, round_no: int) -> list[str]:
        return [eid for r, user in self.gated if r == round_no for eid in re.findall(r"^\[(P\d+\.\d+)\]", user, re.M)]

    def __enter__(self):
        self._patch = mock.patch.object(self.dojo.ai, "chat", side_effect=self)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def new_app(prefix: str, **changes):
    return create_app(dataclasses.replace(settings(tempfile.mkdtemp(prefix=prefix)), **changes),
                      http=httpx.Client(transport=httpx.MockTransport(web)))


def lesson_for(app, sid: str, pid: str = "gh-300", created: str | None = None) -> str:
    """A stub lesson for this skill, optionally dated earlier so a later one is clearly the newest."""
    dojo = app.state.dojo
    lesson = dojo.make_lesson(lambda step: None, app.state.preparer.owner, pid, sid)["lesson"]
    if created:
        doc = dojo.store.read("content", "lessons", lesson)
        doc["created"] = created
        dojo.store.write("content", "lessons", lesson, value=doc)
        dojo._lesson_meta.clear()
    return lesson


def at(when):
    """Dojo's clock set to this moment: what it stamps and what the Preparer and the listen record read."""
    return mock.patch("app.core.utcnow", return_value=when), mock.patch("app.prepare.utcnow", return_value=when), \
        mock.patch("app.learning.utcnow", return_value=when)


class Clock:
    def __init__(self, when):
        self.patches = at(when)

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def quality(dojo, kind: str) -> list[dict]:
    return [e for e in dojo.store.read_lines("content", "quality.jsonl") if e.get("kind") == kind]


class DeepNarrationTests(unittest.TestCase):
    def setUp(self):
        self.app = new_app("dojo-deep-")
        self.dojo, self.prep = self.app.state.dojo, self.app.state.preparer
        self.c = TestClient(self.app)
        self.sid = self.prep._skills("gh-300")[0]["id"]

    def ready(self) -> str:
        lesson = lesson_for(self.app, self.sid)
        self.dojo.make_deep(lambda step: None, lesson)
        self.assertTrue(self.dojo.deep_audio(lambda step: None, lesson))
        self.assertTrue(self.dojo.deep_ready(lesson))
        return lesson

    def test_it_teaches_the_whole_lesson_in_two_voices_from_the_checked_lesson(self):
        lesson = lesson_for(self.app, self.sid)
        stored = self.dojo.store.read("content", "lessons", lesson)
        with Models(self.dojo) as models:
            out = self.dojo.make_deep(lambda step: None, lesson)
        d = self.dojo.deep(lesson)
        self.assertEqual(out, {"lesson": lesson, "state": "ready", "words": d["words"], "minutes": d["minutes"]})
        self.assertEqual([s["title"] for s in d["deep_narration"]], PARTS)
        self.assertEqual([s["kind"] for s in d["deep_narration"]],
                         ["opening", "section", "section", "example", "misconceptions", "traps", "summary"])
        lines = [line for s in d["deep_narration"] for line in s["lines"]]
        self.assertEqual({line["voice"] for line in lines}, {"guide", "coach"}, "the two voices of the slide narration")
        self.assertEqual(d["words"], sum(len(line["text"].split()) for line in lines))
        self.assertTrue(800 <= d["words"] <= 1100, d["words"])
        self.assertEqual(d["minutes"], round(d["words"] / learning.WORDS_PER_MINUTE, 1))
        self.assertTrue(5.3 <= d["minutes"] <= 7.4, d["minutes"])
        self.assertEqual((d["state"], d["rounds"], d["kept_share"], d["missing"], d["blocked"]), ("ready", 1, 1.0, [], []))
        self.assertIn("self-check in Dojo", lines[-1]["text"])
        self.assertEqual(d["models"], self.dojo.models("author", "gate"))
        self.assertTrue(d["sources"])
        # Written from the checked lesson: every section, point, step and misconception; not the self-check.
        [prompt] = models.prompts
        doc = self.dojo.lesson(lesson)
        for sec in doc["sections"]:
            self.assertIn(f'Section "{sec["heading"]}"', prompt)
            for p in sec["points"]:
                self.assertIn(p["text"], prompt)
        for m in doc["misconceptions"]:
            self.assertIn(m["wrong"], prompt)
            self.assertIn(m["right"], prompt)
        for text in [doc["example"]["situation"], *doc["example"]["steps"]]:
            self.assertIn(text, prompt)
        for q in doc["self_check"]:
            self.assertNotIn(q["question"], prompt)
        self.assertNotIn("Invented point.", prompt, "what the lesson's own check removed is not in it")
        # Every line went to the gate model.
        self.assertEqual(sorted(models.ids(1)), sorted(f"{s['part']}.{j}" for s in d["deep_narration"] for j in range(len(s["lines"]))))
        self.assertEqual(self.dojo.store.read("content", "lessons", lesson), stored, "the lesson itself never changes")

    def test_a_part_that_does_not_hold_twice_is_left_out_and_logged(self):
        lesson = lesson_for(self.app, self.sid)
        with Models(self.dojo) as models:
            models.fail = {1: {"P3"}, 2: {"P3"}}
            out = self.dojo.make_deep(lambda step: None, lesson)
        d = self.dojo.deep(lesson)
        self.assertEqual((out["state"], d["rounds"], d["spent_rounds"], d["missing"]), ("ready", 2, 2, ["Details"]))
        self.assertEqual([s["title"] for s in d["deep_narration"]], [p for p in PARTS if p != "Details"])
        self.assertIn("Write ONLY these parts again: P3.", models.prompts[1])
        self.assertIn("The sources do not say this.", models.prompts[1], "the rewrite is told what went wrong")
        self.assertEqual({eid.split(".")[0] for eid in models.ids(2)}, {"P3"}, "only that part is checked again")
        self.assertTrue(d["blocked"])
        self.assertTrue(all(b["part"] == "Details" and b["reason"] == "The sources do not say this." for b in d["blocked"]))
        self.assertGreaterEqual(d["kept_share"], learning.KEPT_SHARE)
        self.assertGreaterEqual(d["words"], learning.DEEP_MIN_WORDS)
        logged = quality(self.dojo, "deep narration")
        self.assertEqual([(e["lesson"], e["part"], e["text"]) for e in logged], [(lesson, b["part"], b["text"]) for b in d["blocked"]])

    def test_a_part_that_holds_when_written_again_is_kept(self):
        lesson = lesson_for(self.app, self.sid)
        with Models(self.dojo) as models:
            models.fail = {1: {"P2"}}
            self.dojo.make_deep(lambda step: None, lesson)
        d = self.dojo.deep(lesson)
        self.assertEqual((d["state"], d["rounds"], d["missing"], d["kept_share"], d["blocked"]), ("ready", 2, [], 1.0, []))
        self.assertEqual([s["title"] for s in d["deep_narration"]], PARTS)
        self.assertIn("Write ONLY these parts again: P2.", models.prompts[1])

    def test_self_check_answers_are_never_spoken(self):
        with Models(self.dojo) as models:
            models.self_check = [{"question": QUESTION, "answer": ANSWER}]
            lesson = lesson_for(self.app, self.sid)
            self.assertEqual([(q["question"], q["answer"]) for q in self.dojo.lesson(lesson)["self_check"]], [(QUESTION, ANSWER)])
            # The coach asks the self-check's question once; the summary says its answer in both rounds.
            models.extra = {1: {"P2": [{"voice": "coach", "text": ASKS}], "P7": [{"voice": "guide", "text": TELLS}]},
                            2: {"P7": [{"voice": "guide", "text": TELLS}]}}
            out = self.dojo.make_deep(lambda step: None, lesson)
        d = self.dojo.deep(lesson)
        self.assertEqual((out["state"], d["rounds"], d["missing"]), ("ready", 2, ["Summary"]))
        self.assertNotIn(QUESTION, models.prompts[0], "the author never sees the self-check")
        self.assertNotIn(ANSWER, models.prompts[0])
        self.assertIn("Write ONLY these parts again: P2, P7.", models.prompts[1])
        self.assertIn("It asks a self-check question.", models.prompts[1])
        gated = "\n".join(user for _, user in models.gated)
        self.assertNotIn(ASKS, gated, "caught before the gate model")
        self.assertNotIn(TELLS, gated)
        self.assertEqual([(b["part"], b["text"], b["reason"]) for b in d["blocked"]],
                         [("Summary", TELLS, "It says a self-check answer word for word. Deeper Listen leaves the self-check to Dojo.")])
        self.assertEqual([(e["part"], e["text"]) for e in quality(self.dojo, "deep narration")], [("Summary", TELLS)])
        lines = [line["text"] for s in d["deep_narration"] for line in s["lines"]]
        self.assertNotIn(ASKS, lines)
        self.assertNotIn(" ".join(learning._words(ANSWER)), " ".join(learning._words(" ".join(lines))))
        self.assertEqual([_gives_away(t, self.dojo.lesson(lesson)["self_check"]) for t in lines], [""] * len(lines))
        self.assertEqual(lines[-1], learning.DEEP_CLOSE, "Dojo's own last line sends the listener to the self-check")

    def test_only_a_question_or_an_answer_of_the_self_check_gives_it_away(self):
        check = [{"question": QUESTION, "answer": ANSWER}, {"question": "What is it?", "answer": "Up to 30 minutes."}]
        self.assertIn("asks a self-check question", _gives_away(ASKS, check))
        self.assertIn("says a self-check answer", _gives_away(TELLS, check))
        self.assertEqual(_gives_away("Repository administrators and organization owners can manage content exclusion "
                                     "settings for their repositories and organizations.", check), "",
                         "teaching the facts an answer rests on is the lesson itself")
        self.assertEqual(_gives_away("It can take up to 30 minutes.", check), "", "a short answer is a fact the lesson teaches")
        self.assertEqual(_gives_away("What is it?", check), ASKS_IT, "a question of three words is matched whole")
        self.assertEqual(_gives_away("What is its limit?", check), "", "whole words only")

    def test_a_short_answer_gives_it_away_only_next_to_its_question(self):
        check = [{"question": SHORT_Q, "answer": SHORT_A}]
        self.assertEqual(_gives_away(SHORT_A, check, SHORT_Q), NEXT_TO, "the coach asks it and the guide answers")
        self.assertEqual(_gives_away(SHORT_A, check, SHORT_SAYS), NEXT_TO, "the question put as a statement")
        self.assertEqual(_gives_away(f"The feature you should use to explain a block of code: {SHORT_A}", check), NEXT_TO,
                         "both in one line")
        self.assertEqual(_gives_away(TEACHES, check, SHORT_Q), NEXT_TO, "an answer is an answer, whatever follows it")
        # Ordinary teaching: the same words without the question.
        self.assertEqual(_gives_away(SHORT_A, check), "", "said on its own, it is a fact the lesson teaches")
        self.assertEqual(_gives_away(SHORT_A, check, "What should I hold on to here?"), "")
        self.assertEqual(_gives_away(TEACHES, check, "Can you say that another way?"), "")
        self.assertEqual(_gives_away("GitHub Copilot Chat can explain a block of code, so use GitHub Copilot Chat when you "
                                     "inherit code.", check), "", "teaching around the question is not restating it")
        self.assertEqual(_gives_away(SHORT_SAYS, check), "", "the question as a statement, without its answer")
        self.assertEqual(_gives_away("Use GitHub Copilot Chatbots.", check, SHORT_Q), "", "the answer only as a whole phrase")
        self.assertEqual(_gives_away(SHORT_A, [{"question": "Which one?", "answer": SHORT_A}], "Which one?"), NEXT_TO,
                         "a question of two words is matched whole")

    def test_a_short_question_counts_when_it_is_said_whole(self):
        check = [{"question": TINY_Q, "answer": TINY_A}]
        # Asked, it gives the self-check away with or without its answer, as a longer question does.
        self.assertEqual(_gives_away(TINY_Q, check), ASKS_IT, "asked without its answer")
        self.assertEqual(_gives_away("So, WHO can enroll?", check), ASKS_IT, "case and punctuation do not count")
        self.assertEqual(_gives_away("And who can enroll in the pilot?", check), ASKS_IT, "the whole question inside a longer one")
        # Answered next to it.
        self.assertEqual(_gives_away(TINY_A, check, TINY_Q), NEXT_TO, "the coach asks it and the guide answers")
        self.assertEqual(_gives_away(TINY_A, check, "The exam asks who can enroll."), NEXT_TO, "the question in a statement")
        self.assertEqual(_gives_away("Who can enroll comes down to this: only administrators.", check), NEXT_TO, "both in one line")
        # Ordinary teaching: the same words without the whole question.
        self.assertEqual(_gives_away(TINY_TEACHES, check), "")
        self.assertEqual(_gives_away(TINY_A, check, "Who can change the policy?"), "", "the answer to another question")
        self.assertEqual(_gives_away("Only administrators can enroll new members.", check), "", "the fact itself is the lesson")
        self.assertEqual(_gives_away("Who can enroll is set by the enterprise.", check), "", "a statement without its answer")
        self.assertEqual(_gives_away("Who can unenroll?", check), "", "another question: whole words only")

    def test_a_short_question_and_its_answer_are_left_out_and_teaching_is_kept(self):
        with Models(self.dojo) as models:
            models.self_check = [{"question": TINY_Q, "answer": TINY_A}]
            lesson = lesson_for(self.app, self.sid)
            check = self.dojo.lesson(lesson)["self_check"]
            self.assertEqual([(q["question"], q["answer"]) for q in check], [(TINY_Q, TINY_A)])
            # "Key idea": the coach asks the question and the guide answers it, in both rounds. "Details" uses
            # the answer's words to teach. "Worked example": the coach asks the question alone, in round 1.
            pair = [{"voice": "coach", "text": TINY_Q}, {"voice": "guide", "text": TINY_A}]
            models.extra = {1: {"P2": pair, "P3": [{"voice": "guide", "text": TINY_TEACHES}],
                                "P4": [{"voice": "coach", "text": TINY_Q}]}, 2: {"P2": pair}}
            out = self.dojo.make_deep(lambda step: None, lesson)
        d = self.dojo.deep(lesson)
        self.assertEqual((out["state"], d["rounds"], d["missing"]), ("ready", 2, ["Key idea"]))
        self.assertIn("Write ONLY these parts again: P2, P4.", models.prompts[1])
        self.assertIn(ASKS_IT, models.prompts[1], "the rewrite is told why")
        self.assertIn(NEXT_TO, models.prompts[1])
        blocked = [("Key idea", TINY_Q, ASKS_IT), ("Key idea", TINY_A, NEXT_TO)]
        self.assertEqual([(b["part"], b["text"], b["reason"]) for b in d["blocked"]], blocked)
        self.assertEqual([(e["part"], e["text"], e["reason"]) for e in quality(self.dojo, "deep narration")], blocked)
        gated = "\n".join(user for _, user in models.gated)
        self.assertNotIn(f"): {TINY_Q}", gated, "caught before the gate model")
        self.assertNotRegex(gated, rf"(?m)\): {re.escape(TINY_A)}$")
        self.assertIn(f"(guide): {TINY_TEACHES}", gated)
        lines = [line["text"] for s in d["deep_narration"] for line in s["lines"]]
        self.assertIn(TINY_TEACHES, lines, "ordinary teaching is kept")
        self.assertNotIn(TINY_Q, lines)
        self.assertNotIn(TINY_A, lines)
        for seg in d["deep_narration"]:
            texts = [line["text"] for line in seg["lines"]]
            self.assertEqual([_gives_away(t, check, texts[j - 1] if j else "") for j, t in enumerate(texts)], [""] * len(texts))

    def test_a_short_answer_next_to_its_question_is_left_out_and_teaching_is_kept(self):
        with Models(self.dojo) as models:
            models.self_check = [{"question": SHORT_Q, "answer": SHORT_A}]
            lesson = lesson_for(self.app, self.sid)
            check = self.dojo.lesson(lesson)["self_check"]
            self.assertEqual([(q["question"], q["answer"]) for q in check], [(SHORT_Q, SHORT_A)])
            # Round 1: the coach asks the question in "Key idea" and the guide answers it; "Details" uses the
            # same words to teach. "Summary" puts the question as a statement and answers it, in both rounds.
            asked = [{"voice": "coach", "text": SHORT_Q}, {"voice": "guide", "text": SHORT_A}]
            said = [{"voice": "coach", "text": SHORT_SAYS}, {"voice": "guide", "text": SHORT_A}]
            models.extra = {1: {"P2": asked, "P3": [{"voice": "guide", "text": TEACHES}], "P7": said}, 2: {"P7": said}}
            out = self.dojo.make_deep(lambda step: None, lesson)
        d = self.dojo.deep(lesson)
        self.assertEqual((out["state"], d["rounds"], d["missing"]), ("ready", 2, ["Summary"]))
        self.assertIn("Write ONLY these parts again: P2, P7.", models.prompts[1])
        self.assertIn(NEXT_TO, models.prompts[1], "the rewrite is told why")
        self.assertEqual([(b["part"], b["text"], b["reason"]) for b in d["blocked"]], [("Summary", SHORT_A, NEXT_TO)])
        self.assertEqual([(e["part"], e["text"], e["reason"]) for e in quality(self.dojo, "deep narration")],
                         [("Summary", SHORT_A, NEXT_TO)])
        gated = "\n".join(user for _, user in models.gated)
        self.assertNotRegex(gated, rf"(?m)\): {re.escape(SHORT_A)}$", "caught before the gate model")
        self.assertIn(f"(guide): {TEACHES}", gated)
        self.assertIn(f"(coach): {SHORT_SAYS}", gated, "the question as a statement gives nothing away")
        lines = [line["text"] for s in d["deep_narration"] for line in s["lines"]]
        self.assertIn(TEACHES, lines, "ordinary teaching is kept")
        self.assertNotIn(SHORT_A, lines)
        self.assertNotIn(SHORT_Q, lines)
        for seg in d["deep_narration"]:
            texts = [line["text"] for line in seg["lines"]]
            self.assertEqual([_gives_away(t, check, texts[j - 1] if j else "") for j, t in enumerate(texts)], [""] * len(texts))

    def test_with_too_little_holding_listen_keeps_the_slide_narration(self):
        lesson = lesson_for(self.app, self.sid)
        self.dojo.lesson_audio(lambda step: None, self.prep.owner, lesson)
        before = self.c.get(f"/api/lessons/{lesson}").json()
        with Models(self.dojo) as models:
            models.always = {"P2", "P3", "P4"}
            out = self.dojo.make_deep(lambda step: None, lesson)
        d = self.dojo.deep(lesson)
        self.assertEqual((out["state"], d["state"], d["missing"]), ("failed", "failed", ["Key idea", "Details", "Worked example"]))
        self.assertLess(d["kept_share"], learning.KEPT_SHARE)
        self.assertIn("Listen keeps the slide narration", d["reason"])
        whole = [e for e in quality(self.dojo, "deep narration") if e["part"] == "whole"]
        self.assertEqual([e["reason"] for e in whole], [d["reason"]])
        self.assertEqual(self.dojo.deep_view(lesson), {"state": "failed", "reason": d["reason"]})
        self.assertFalse(self.dojo.deep_ready(lesson))
        with self.assertRaises(UserError):
            self.dojo.deep_audio(lambda step: None, lesson)
        self.assertFalse(any(self.dojo.speech.exists(m) for m in self.dojo._deep_media(d)), "nothing spoken")
        after = self.c.get(f"/api/lessons/{lesson}").json()
        self.assertEqual(after["deep"]["state"], "failed")
        self.assertEqual({k: after[k] for k in ("slides", "audio", "video")}, {k: before[k] for k in ("slides", "audio", "video")})
        self.assertTrue(all(after["audio"]), "Listen plays the slide narration")
        self.assertNotIn("deep", self.app.state.podcast._lesson(lesson), "and so does the podcast")
        # A listen of the slides keeps the slides' window. If the page says it played a Deeper Listen,
        # the longer window is recorded again: it is never in the learner's favour.
        self.assertTrue(self.c.post(f"/api/lessons/{lesson}/viewed", json={"modality": "listen"}, headers=POST).json()["recorded"])
        self.assertTrue(self.c.post(f"/api/lessons/{lesson}/viewed", json={"modality": "listen", "narration": "deep"},
                                    headers=POST).json()["recorded"])
        viewed = [e["data"] for e in self.dojo.store.events(self.prep.owner.key) if e["type"] == "lesson.viewed"]
        self.assertEqual([(v["narration"], v["repeats_within_min"]) for v in viewed],
                         [("slides", learning.VIEW_REPEAT_MINUTES), ("deep", learning.DEEP_VIEW_MINUTES)])

    def test_listen_gets_it_once_it_is_spoken_and_watch_and_present_keep_theirs(self):
        check = self.app.state.checker
        lesson = lesson_for(self.app, self.sid)
        self.dojo.lesson_audio(lambda step: None, self.prep.owner, lesson)
        before = self.c.get(f"/api/lessons/{lesson}").json()
        films = check.plan(lesson)["films"]
        stored = self.dojo.store.read("content", "lessons", lesson)
        self.assertEqual(before["deep"], {"state": "missing"})
        self.dojo.make_deep(lambda step: None, lesson)
        self.assertEqual(self.c.get(f"/api/lessons/{lesson}").json()["deep"], {"state": "speaking"})
        self.assertTrue(self.dojo.deep_audio(lambda step: None, lesson))
        after = self.c.get(f"/api/lessons/{lesson}").json()
        deep = after["deep"]
        self.assertEqual((deep["state"], [p["title"] for p in deep["parts"]]), ("ready", PARTS))
        self.assertEqual((deep["words"], deep["minutes"], deep["missing"]), (self.dojo.deep(lesson)["words"], self.dojo.deep(lesson)["minutes"], []))
        media = [p["media"] for p in deep["parts"]]
        self.assertEqual(media, [media_id_for(p["lines"]) for p in deep["parts"]], "ids hash the lines and voices, like the slides'")
        for m in media:
            r = self.c.get(f"/api/media/{m}")
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "audio/mpeg"))
        # Watch and Present: the same slides, slide audio and videos, and the same presenter media ids.
        self.assertEqual({k: after[k] for k in ("slides", "audio", "video")}, {k: before[k] for k in ("slides", "audio", "video")})
        check._plans.clear()
        self.assertEqual(check.plan(lesson)["films"], films)
        self.assertFalse(set(media) & (set(films.values()) | set(after["audio"])))
        self.assertEqual(self.dojo.store.read("content", "lessons", lesson), stored, "the lesson itself never changes")

    def test_a_listen_opens_a_window_long_enough_for_it_and_is_never_evidence(self):
        lesson = self.ready()
        start = (utcnow() - timedelta(hours=3)).replace(microsecond=0)

        def listen(minutes: int, narration: str) -> bool:
            with Clock(start + timedelta(minutes=minutes)):
                r = self.c.post(f"/api/lessons/{lesson}/viewed", json={"modality": "listen", "narration": narration}, headers=POST)
            self.assertEqual(r.status_code, 200)
            return r.json()["recorded"]

        self.assertEqual(learning.DEEP_VIEW_MINUTES, 45, "about six minutes of audio, paused and resumed")
        self.assertTrue(listen(0, "deep"))
        self.assertFalse(listen(44, "deep"), "a pause and a resume inside the window")
        self.assertFalse(listen(44, "slides"), "the page cannot shorten the window of a lesson whose Deeper Listen plays")
        self.assertTrue(listen(46, "deep"))
        viewed = [e["data"] for e in self.dojo.store.events(self.prep.owner.key) if e["type"] == "lesson.viewed"]
        self.assertEqual([(v["modality"], v["narration"], v["repeats_within_min"]) for v in viewed], [("listen", "deep", 45)] * 2)
        ev = self.c.get(f"/api/skills/gh-300/{self.sid}").json()["evidence"]
        self.assertEqual(ev["last_teaching"], (start + timedelta(minutes=46 + 45)).isoformat(timespec="seconds"),
                         "teaching counts until the window of the last listen ends")
        self.assertEqual([x["narration"] for x in ev["exposures"]], ["deep", "deep"])
        self.assertEqual((ev["state"], ev["attempts"], ev["checks"]["unaided"]), ("untested", [], False), "exposure is never evidence")

    def test_a_lesson_a_changed_source_holds_back_does_not_play_it(self):
        lesson = self.ready()
        held = SimpleNamespace(withheld=lambda lesson_id: lesson_id == lesson)
        with mock.patch.object(self.dojo, "drift", held, create=True):
            self.assertEqual(self.dojo.deep_view(lesson), {"state": "held"})
            self.assertFalse(self.dojo.deep_ready(lesson))
            self.assertIsNone(self.prep._deep_due(lesson, utcnow()), "its rewrite gets one instead")
            self.assertEqual(self.dojo.lesson_viewed(self.prep.owner, lesson, "listen"), {"ok": True, "recorded": True})
        self.assertEqual(self.dojo.store.events(self.prep.owner.key)[-1]["data"]["narration"], "slides")
        self.assertTrue(self.dojo.deep_ready(lesson), "it plays again once nothing holds the lesson back")


class DeepPodcastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-deep-feed-")
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.dojo, self.podcast = self.app.state.dojo, self.app.state.podcast
        self.podcast.prune_after_s, self.podcast.unused_grace_s = 3600, 600   # this test runs every prune itself
        self.addCleanup(self.podcast.close)
        pkg = self.dojo.packages.get("gh-300")
        self.sid = next(s for d in pkg["domains"] for g in d["groups"] for s in g["skills"])
        self.lesson = narrated_lesson(self.app, self.sid, fill=10, created="2026-03-01T10:00:00+00:00")

    def files(self) -> set[str]:
        return {p.name for p in (Path(self.tmp) / "podcast").iterdir() if not p.name.startswith(".")}

    def test_the_feed_plays_it_once_ready_and_the_slides_file_goes_after_the_grace_time(self):
        c, token, lesson = TestClient(self.app), self.podcast.links.create(), self.lesson
        self.assertEqual(c.get(f"/feed/{token}/ep/{lesson}.mp3").status_code, 200)
        slides = episode_key(self.podcast._lesson(lesson)["media"])
        self.assertIn(slides + ".mp3", self.files())
        self.dojo.make_deep(lambda step: None, lesson)
        media = self.dojo._deep_media(self.dojo.deep(lesson))
        self.assertEqual(c.get(f"/feed/{token}/ep/{lesson}-deep.mp3").status_code, 404, "not before it is spoken")
        parts = [mp3(20 + i, fill=100 + i) for i in range(len(media))]
        for m, audio in zip(media, parts):
            self.dojo.store.write_bytes("media", m + ".mp3", data=audio)
        info = self.podcast._lesson(lesson)
        self.assertEqual((info["deep"], info["media"], info["titles"]), (True, media, PARTS))
        deep = episode_key(media)
        self.assertNotEqual(deep, slides, "a new episode key")
        [item] = ET.fromstring(c.get(f"/feed/{token}/gh-300.xml").content).findall("channel/item")
        self.assertEqual(item.findtext("guid"), f"{lesson}-deep", "a new guid, so a podcast app fetches it again")
        self.assertTrue(item.find("enclosure").get("url").endswith(f"/feed/{token}/ep/{lesson}-deep.mp3"))
        notes = item.findtext("description")
        self.assertIn("had a second AI model check every line against them. The self-check is in Dojo.", notes)
        self.assertIn("<li>Worked example</li>", notes)
        r = c.get(f"/feed/{token}/ep/{lesson}-deep.mp3")
        self.assertEqual((r.status_code, r.content), (200, join_mp3(parts)[0]))
        self.assertEqual(c.get(f"/feed/{token}/ep/{lesson}.mp3").status_code, 404, "the slides episode is no longer served")
        for seconds, there in ((0, True), (599, True), (600, False)):
            with mock.patch("app.podcast.time.monotonic", return_value=5000.0 + seconds):
                self.podcast.prune()
            self.assertEqual(slides + ".mp3" in self.files(), there, f"{seconds} s after it was first found unused")
        self.assertIn(deep + ".mp3", self.files(), "the current episode is never removed")
        self.assertEqual(list(self.dojo.store.read("learners", self.dojo.owner_key, "aired")["gh-300"][self.sid]), [lesson], "noted per lesson, once")


class DeepVoiceCheckTests(unittest.TestCase):
    def test_the_voice_check_compares_every_part_with_its_approved_words(self):
        app = new_app("dojo-deep-check-")
        dojo, check = app.state.dojo, app.state.checker
        ears = Ears()
        dojo.speech.transcribe_words = ears
        lesson = lesson_for(app, app.state.preparer._skills("gh-300")[0]["id"])
        dojo.make_deep(lambda step: None, lesson)
        plan = check.deep_plan(lesson)
        self.assertEqual(plan["titles"], PARTS)
        for m in plan["audio"]:
            dojo.store.write_bytes("media", m + ".mp3", data=b"ID3" + m.encode())
        self.assertTrue(dojo.deep_ready(lesson))
        for k, (m, text) in enumerate(zip(plan["audio"], plan["approved"])):
            ears.heard[m] = {"text": text.rsplit(" ", 1)[0] if k == 1 else text, "seconds": 50.0, "words": []}
        self.assertEqual(check.step(), delivery.WORK_WAIT_S)
        deep = check.results()[lesson]["deep"]
        self.assertEqual(deep["audio"], plan["audio"])
        self.assertEqual([(p["part"], p["title"]) for p in deep["parts"]], list(enumerate(PARTS, start=1)))
        self.assertEqual([p["errors"] for p in deep["parts"]], [0, 1, 0, 0, 0, 0, 0])
        self.assertEqual((deep["seconds"], deep["errors"]), (350.0, 1))
        self.assertEqual(sorted(m for m, _ in ears.calls), sorted(plan["audio"]))
        v = check.summary()
        self.assertEqual((v["deep"]["lessons"], v["deep"]["parts"], v["deep"]["minutes"], v["today"]["deep"]), (1, 7, 5.8, 1))
        worst = v["worst"][0]
        self.assertEqual((worst["kind"], worst["lesson"], worst["part"], worst["part_title"], worst["errors"]),
                         ("deep", lesson, 2, "Key idea", 1))
        self.assertEqual(check.step(), delivery.IDLE_WAIT_S, "checked once for this version of its audio")


class DeepPreparerTests(unittest.TestCase):
    def setUp(self):
        self.app = new_app("dojo-deep-prep-")
        self.dojo, self.prep = self.app.state.dojo, self.app.state.preparer

    def narrated(self, k: int, pid: str = "gh-300") -> str:
        lesson = lesson_for(self.app, self.prep._skills(pid)[k]["id"], pid)
        self.dojo.lesson_audio(lambda step: None, self.prep.owner, lesson)
        return lesson

    def test_ordinary_work_comes_first_then_the_active_exam_heaviest_first(self):
        gh_sid, dp_sid = self.prep._skills("gh-300")[0]["id"], self.prep._skills("dp-800")[0]["id"]
        gh, dp = lesson_for(self.app, gh_sid), lesson_for(self.app, dp_sid, "dp-800")
        lessons = len(self.dojo.lesson_index())
        self.assertTrue(self.prep.step())
        self.assertEqual(len(self.dojo.lesson_index()), lessons + 1, "a missing lesson is written first")
        self.assertEqual(self.dojo.deep_docs(), [])
        self.prep.next_skill = lambda: None   # every skill has a lesson now
        while self.prep.next_narration():
            self.assertTrue(self.prep.step())
        self.assertEqual(self.dojo.deep_docs(), [], "and every lesson is narrated before any Deeper Listen")
        self.dojo.save_profile(self.prep.owner, {"active_package": "dp-800"})
        self.assertEqual(self.prep.next_deep(), ("write", "dp-800", dp_sid, dp))
        self.assertTrue(self.prep.step())
        self.assertTrue(self.dojo.deep_ready(dp))
        self.assertEqual(self.prep.next_deep(), ("write", "gh-300", gh_sid, gh), "then the other exam, most weight first")
        self.assertEqual((self.prep.state["deepened"], self.prep.deep_status()["ready"]), (1, 1))

    def test_at_most_twenty_a_day_counted_on_the_share_across_restarts(self):
        self.assertEqual(prepare.DEEP_PER_DAY, 20)
        lessons = [self.narrated(k) for k in range(3)]
        self.prep.next_skill = lambda: None
        with mock.patch.object(prepare, "DEEP_PER_DAY", 2):
            self.assertTrue(self.prep.step())
            self.assertTrue(self.prep.step())
            self.assertFalse(self.prep.step(), "the day's allowance is spent")
            self.assertEqual([bool(self.dojo.deep(lesson)) for lesson in lessons], [True, True, False])
            day = self.dojo.store.read("content", "deep-state")
            self.assertEqual((day["day"], day["today"], day["written"]), (utcnow().date().isoformat(), 2, 2))
            again = Preparer(self.dojo, self.prep.owner)   # a restart
            again.next_skill = lambda: None
            self.assertEqual(again.deep_left(), 0)
            self.assertFalse(again.step(), "a restart gets no fresh allowance")
            self.assertEqual((again.deep_status()["today"], again.deep_status()["per_day"]), (2, 2))
            with Clock(utcnow() + timedelta(days=1)):
                self.assertEqual(again.deep_left(), 2, "a new UTC day")
                self.assertTrue(again.step())
                self.assertFalse(again.step(), "nothing left to write")
                day = self.dojo.store.read("content", "deep-state")
                self.assertEqual((day["today"], day["written"]), (1, 3))
        self.assertTrue(all(self.dojo.deep_ready(lesson) for lesson in lessons))

    def test_it_waits_for_the_learner_and_speaks_what_is_written_for_free(self):
        lesson = self.narrated(0)
        sid = self.prep._skills("gh-300")[0]["id"]
        self.prep.next_skill = lambda: None
        job = {"id": "job-00000000000000000000", "kind": "item", "learner": self.prep.owner.key, "ref": None, "state": "running",
               "step": "", "created": "2026-09-28T00:00:00+00:00", "heartbeat": "2026-09-28T00:00:00+00:00", "result": None, "error": None}
        self.dojo.store.write("jobs", job["id"], value=job)
        self.assertTrue(self.prep.step())
        self.assertIsNone(self.dojo.deep(lesson), "nothing while the learner has work running")
        self.assertIsNone(self.dojo.store.read("content", "deep-state", default=None))
        self.dojo.store.delete("jobs", job["id"])
        answers = iter([False, True])   # free when the step starts, busy once the text is written
        with mock.patch.object(self.prep, "_learner_busy", side_effect=lambda: next(answers, True)):
            self.assertTrue(self.prep.step())
        self.assertEqual((self.dojo.deep_view(lesson), self.prep.state["deepened"]), ({"state": "speaking"}, 0))
        self.assertEqual(self.prep.next_deep(), ("speak", "gh-300", sid, lesson))
        self.assertTrue(self.prep.step())
        self.assertTrue(self.dojo.deep_ready(lesson))
        self.assertEqual((self.prep.state["deepened"], self.prep.deep_left()), (1, prepare.DEEP_PER_DAY - 1), "speaking is not counted")

    def test_one_that_did_not_pass_is_tried_again_after_a_week_at_most_twice(self):
        lesson = self.narrated(0)
        self.prep.next_skill = lambda: None
        with Models(self.dojo) as models:
            models.always = {"P2", "P3", "P4"}
            self.assertTrue(self.prep.step())
            self.assertEqual((self.dojo.deep(lesson)["state"], self.dojo.deep(lesson)["attempts"]), ("failed", 1))
            self.assertIsNone(self.prep.next_deep(), "not again within a week")
            week = utcnow() + prepare.DEEP_RETRY + timedelta(minutes=1)
            with Clock(week):
                self.assertEqual(self.prep.next_deep()[0::3], ("write", lesson))
                self.assertTrue(self.prep.step())
            d = self.dojo.deep(lesson)
            self.assertEqual((d["state"], d["attempts"], d["spent_rounds"]), ("failed", 2, 4))
            with Clock(week + prepare.DEEP_RETRY + timedelta(minutes=1)):
                self.assertIsNone(self.prep.next_deep(), "at most twice")
        self.assertEqual(self.prep.deep_status()["failed"], 1)

    def test_a_rewrite_of_the_lesson_replaces_its_deeper_listen(self):
        sid = self.prep._skills("gh-300")[0]["id"]
        old = lesson_for(self.app, sid, created="2026-03-01T10:00:00+00:00")
        self.dojo.lesson_audio(lambda step: None, self.prep.owner, old)
        self.prep.next_skill = lambda: None
        self.assertTrue(self.prep.step())
        self.assertTrue(self.dojo.deep_ready(old))
        held = SimpleNamespace(withheld=lambda lesson_id: lesson_id == old)
        with mock.patch.object(self.dojo, "drift", held, create=True):
            new = self.dojo.make_lesson(lambda step: None, self.prep.owner, "gh-300", sid)["lesson"]   # the rewrite
            d = self.dojo.deep(old)
            self.assertEqual(d["state"], "invalidated")
            self.assertIn(new, d["reason"])
            self.assertEqual(self.dojo.deep_view(old)["state"], "invalidated")
            while self.prep.next_narration():   # the new lesson is narrated first, if its slides need new audio
                self.assertTrue(self.prep.step())
            self.assertIsNone(self.dojo.deep(new))
            self.assertEqual(self.prep.next_deep(), ("write", "gh-300", sid, new), "the rewrite gets its own")
            self.assertTrue(self.prep.step())
            self.assertTrue(self.dojo.deep_ready(new))
        self.assertEqual(self.dojo.deep(old)["state"], "invalidated", "for good")
        self.assertEqual(self.app.state.costs.deep_view()["so_far"]["conversations"], 2, "what the old one cost stays counted")


class Crash(BaseException):
    """Dojo stops in the middle of the work, as when it restarts: nothing it can catch."""


class DeepAttemptTests(unittest.TestCase):
    """Review round 1: an attempt is kept on the share before its first model call. One that stops on an
    error, or that a restart cuts off, waits its week, counts toward the attempts and stays in what
    Deeper Listen has cost so far."""

    def setUp(self):
        self.app = self.start(settings(tempfile.mkdtemp(prefix="dojo-deep-attempt-")))
        self.dojo, self.prep = self.app.state.dojo, self.app.state.preparer
        self.sid = self.prep._skills("gh-300")[0]["id"]
        self.lesson = lesson_for(self.app, self.sid)
        self.dojo.lesson_audio(lambda step: None, self.prep.owner, self.lesson)
        self.seen: list[dict] = []

    @staticmethod
    def start(cfg):
        """Dojo over this data share, with list prices to read."""
        desk = PriceDesk()
        desk.body = {**SAMPLE, "Items": SAMPLE["Items"] + MODELS["Items"], "Count": 15}
        app = create_app(dataclasses.replace(cfg, models=LIVE), http=httpx.Client(transport=httpx.MockTransport(desk)))
        app.state.preparer.next_skill = lambda: None   # the other skills are not part of these tests
        return app

    def author_fails(self, dojo, error: BaseException):
        """The author model fails on the Deeper Listen. What the share holds at that moment is kept."""
        real = dojo.ai.chat

        def chat(role, system, user, json_mode=True):
            if "TASK: deep_narration" in system:
                self.seen.append(dojo.store.read("content", "deep", self.lesson))
                raise error
            return real(role, system, user, json_mode)
        return mock.patch.object(dojo.ai, "chat", side_effect=chat)

    def test_an_error_in_the_author_call_counts_as_an_attempt_across_a_restart(self):
        with self.author_fails(self.dojo, AIError("The author model did not answer")):
            self.assertTrue(self.prep.step())
        [kept] = self.seen
        self.assertEqual({k: kept.get(k) for k in ("lesson", "state", "attempts", "rounds", "spent_rounds")},
                         {"lesson": self.lesson, "state": "writing", "attempts": 1, "rounds": 1, "spent_rounds": 1},
                         "the attempt is on the share before the model is called")
        d = self.dojo.deep(self.lesson)
        self.assertEqual((d["state"], d["stopped"], d["attempts"], d["spent_rounds"], d["created"]),
                         ("failed", True, 1, 1, kept["created"]))
        self.assertEqual(d["reason"], "It stopped on an error: The author model did not answer. Listen keeps the slide narration.")
        self.assertEqual(self.dojo.deep_view(self.lesson), {"state": "failed", "reason": d["reason"], "stopped": True})
        self.assertIn("The author model did not answer", quality(self.dojo, "preparation")[-1]["reason"])
        # A restart: a new Dojo and Preparer over the same share.
        again = self.start(self.app.state.settings)
        dojo, prep, c = again.state.dojo, again.state.preparer, TestClient(again)
        self.assertIsNot(dojo, self.dojo)
        self.assertEqual(dojo.deep(self.lesson), d)
        self.assertIsNone(prep.next_deep(), "it waits its week")
        status = prep.deep_status()
        self.assertEqual((status["ready"], status["failed"], status["stopped"], status["today"]), (0, 1, 1, 1))
        so_far = c.get("/api/studio/costs").json()["deep"]["so_far"]
        self.assertEqual((so_far["rounds"], so_far["conversations"], so_far["complete"]), (1, 1, True))
        self.assertGreater(so_far["total"], 0, "the round it started is spent")
        diag = c.get("/api/diag").json()["deeper_listen"]
        self.assertEqual((diag["failed"], diag["stopped"], diag["rounds"], diag["so_far"]), (1, 1, 1, so_far["total"]))
        self.assertEqual(c.get(f"/api/lessons/{self.lesson}").json()["deep"], {"state": "failed", "reason": d["reason"], "stopped": True})
        # A week later it is written again: the second attempt, and the last.
        week = utcnow() + prepare.DEEP_RETRY + timedelta(minutes=1)
        with Clock(week):
            self.assertEqual(prep.next_deep(), ("write", "gh-300", self.sid, self.lesson))
            with self.author_fails(dojo, AIError("Timed out")):
                self.assertTrue(prep.step())
        d = dojo.deep(self.lesson)
        self.assertEqual((d["state"], d["stopped"], d["attempts"], d["spent_rounds"]), ("failed", True, 2, 2))
        self.assertEqual(self.seen[1]["attempts"], 2)
        with Clock(week + prepare.DEEP_RETRY + timedelta(minutes=1)):
            self.assertIsNone(prep.next_deep(), "at most twice")
        so_far = c.get("/api/studio/costs").json()["deep"]["so_far"]
        self.assertEqual((so_far["rounds"], so_far["conversations"]), (2, 1))

    def test_an_attempt_a_restart_cuts_off_is_failed_and_counted(self):
        with self.author_fails(self.dojo, Crash()), self.assertRaises(Crash):
            self.prep.step()
        self.assertEqual(self.dojo.store.read("content", "deep", self.lesson)["state"], "writing", "what the share holds")
        again = self.start(self.app.state.settings)
        dojo, prep = again.state.dojo, again.state.preparer
        d = dojo.deep(self.lesson)
        self.assertEqual((d["state"], d["stopped"], d["attempts"], d["spent_rounds"], d["reason"]),
                         ("failed", True, 1, 1, learning.DEEP_CUT_OFF))
        self.assertEqual(dojo.deep_view(self.lesson), {"state": "failed", "reason": learning.DEEP_CUT_OFF, "stopped": True})
        self.assertIsNone(prep.next_deep(), "it waits its week")
        self.assertEqual((prep.deep_status()["failed"], prep.deep_status()["stopped"]), (1, 1))
        self.assertEqual(again.state.costs.deep_view()["so_far"]["rounds"], 1)
        with Clock(utcnow() + prepare.DEEP_RETRY + timedelta(minutes=1)):
            self.assertTrue(prep.step())
        d = dojo.deep(self.lesson)
        self.assertEqual((d["state"], d["attempts"], d["rounds"], d["spent_rounds"]), ("ready", 2, 1, 2), "the second attempt writes it")
        self.assertNotIn("stopped", d)
        self.assertTrue(dojo.deep_ready(self.lesson))
        self.assertEqual(again.state.costs.deep_view()["so_far"]["rounds"], 2)

    def test_while_it_is_written_listen_says_so(self):
        views = []
        real = self.dojo.ai.chat

        def chat(role, system, user, json_mode=True):
            if "TASK: deep_narration" in system:
                views.append(self.dojo.deep_view(self.lesson))
                self.assertIsNone(self.prep.next_deep(), "not written twice at once")
            return real(role, system, user, json_mode)
        with mock.patch.object(self.dojo.ai, "chat", side_effect=chat):
            self.assertTrue(self.prep.step())
        self.assertEqual(views, [{"state": "writing", "reason": ""}])
        self.assertTrue(self.dojo.deep_ready(self.lesson))


class DeepCostTests(unittest.TestCase):
    def test_studio_and_diag_show_the_progress_and_the_cost_at_list_price(self):
        desk = PriceDesk()
        desk.body = {**SAMPLE, "Items": SAMPLE["Items"] + MODELS["Items"], "Count": 15}
        app = create_app(dataclasses.replace(settings(tempfile.mkdtemp(prefix="dojo-deep-cost-")), models=LIVE),
                         http=httpx.Client(transport=httpx.MockTransport(desk)))
        c, dojo, prep = TestClient(app), app.state.dojo, app.state.preparer
        lesson = lesson_for(app, prep._skills("gh-300")[0]["id"])
        lesson_for(app, prep._skills("gh-300")[1]["id"])
        dojo.make_deep(lambda step: None, lesson)
        dojo.deep_audio(lambda step: None, lesson)
        body = c.get("/api/studio/costs").json()
        deep = body["deep"]
        self.assertEqual((deep["ready"], deep["lessons"], deep["today"], deep["per_day"]), (1, 2, 0, 20))
        spoken = sum(len(line["text"]) for s in dojo.deep(lesson)["deep_narration"] for line in s["lines"])
        so_far = {line["key"]: line for line in deep["so_far"]["lines"]}
        self.assertEqual((deep["so_far"]["rounds"], deep["so_far"]["conversations"]), (1, 1))
        self.assertEqual((so_far["deep_voice"]["quantity"], so_far["deep_voice"]["cost"]), (spoken, round(spoken * 15e-6, 2)))
        self.assertEqual({line["key"]: line["quantity"] for line in body["lines"]}["deep_voice"], spoken, "in the Speech total too")
        every = deep["every_lesson"]
        self.assertTrue(deep["so_far"]["complete"] and every["complete"])
        self.assertEqual((every["lessons"], every["per_lesson"]), (2, round(every["total"] / 2, 2)))
        self.assertTrue(0.2 < every["per_lesson"] < 1.0, every["per_lesson"])
        self.assertTrue(any("gpt-5.5" in a and "grok-4-1-fast-reasoning" in a for a in deep["assumptions"]))
        diag = c.get("/api/diag").json()["deeper_listen"]
        self.assertEqual((diag["ready"], diag["lessons"], diag["per_day"], diag["today"]), (1, 2, 20, 0))
        self.assertEqual((diag["so_far"], diag["every_lesson"], diag["per_lesson"]),
                         (deep["so_far"]["total"], every["total"], every["per_lesson"]))


class DeepDriftTests(DriftCase):
    """With the source-drift check itself (ADR 0006): the rewrite of a lesson a changed page no longer
    supports comes before any Deeper Listen work, the old lesson's Deeper Listen stops for good, and the
    new lesson gets its own, from the lesson as it is now."""

    def test_the_rewrite_comes_first_and_the_new_lesson_gets_its_own(self):
        prep, podcast = self.app.state.preparer, self.app.state.podcast
        lesson, other = self.lesson(), self.lesson(OTHER)
        for x in (lesson, other):
            self.dojo.lesson_audio(lambda step: None, self.owner, x)
        self.dojo.make_deep(lambda step: None, lesson)
        self.dojo.deep_audio(lambda step: None, lesson)
        self.assertTrue(self.plan_row(S)["lesson"]["deep"])
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        self.assertTrue(self.drift.withheld(lesson))
        self.assertEqual(self.dojo.deep_view(lesson), {"state": "held"})
        self.assertIsNone(self.plan_row(S)["lesson"], "out of the feed, Deeper Listen too")
        self.assertEqual(prep.next_deep(), ("write", PID, OTHER, other), "Deeper Listen work is waiting")
        time.sleep(1.1)  # lessons are ordered by the second they were written in
        self.assertTrue(prep.step())
        new = self.dojo.lessons_for(PID, S)[0]["id"]
        self.assertNotEqual(new, lesson, "the rewrite came first")
        self.assertIsNone(self.dojo.deep(other))
        self.assertEqual(self.dojo.deep(lesson)["state"], "invalidated")
        self.assertIn(new, self.dojo.deep(lesson)["reason"])
        prep.next_skill = lambda: None   # the other skills are not part of this test
        while prep.next_narration() or prep.next_deep():
            self.assertTrue(prep.step())
        self.assertTrue(self.dojo.deep_ready(new) and self.dojo.deep_ready(other))
        self.assertFalse(self.dojo.deep_ready(lesson), "for good")
        row = self.plan_row(S)["lesson"]
        self.assertEqual((row["id"], row["deep"]), (new, True), "the feed plays the new lesson's Deeper Listen")
        self.assertEqual(self.app.state.costs.deep_view()["so_far"]["conversations"], 3, "what the old one cost stays counted")


if __name__ == "__main__":
    unittest.main()
