"""Present's visual deck (ADR 0014, level 2). ONE author-model call plans the deck of a checked lesson; what
cannot be drawn, or quotes a page wrongly, is left out before the check, and the gate model checks every other
element, worded as plain statements. A deck that keeps less than KEPT_SHARE of what was proposed is kept as
failed, and Present plays the deck derived from the lesson. The Preparer draws decks after Deeper Listen, at
most DECK_PER_DAY a day, a failed one again after a week and at most twice. A learner's first open in Present
may have one drawn sooner: in team mode, as one of the member's visual decks for the day."""
import dataclasses
import json
import os
import re
import tempfile
import threading
import time
import unittest
from concurrent import futures
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import deck as decks, prepare  # noqa: E402
from app.ai import AIError  # noqa: E402
from app.budget import personal_refusal  # noqa: E402
from app.core import APP_DIR, UserError, iso, utcnow  # noqa: E402
from app.learning import DECK_BUSY, DECK_CUT_OFF, DECK_DRAWN, DECK_KEEPS  # noqa: E402
from app.main import create_app  # noqa: E402
from app.prepare import Preparer  # noqa: E402
from tests.test_api import GITHUB_MD, settings  # noqa: E402
from tests.test_costs import LIVE, MODELS, SAMPLE, PriceDesk  # noqa: E402
from tests.test_deep import ANSWER, QUESTION, Clock, Models, lesson_for, new_app, quality  # noqa: E402
from tests.test_drift import NEW, PAGE, PID, Q2, S, DriftCase  # noqa: E402
from tests.test_team import OWNER  # noqa: E402
from tests.test_team2 import Team2Case, snapshot  # noqa: E402

GIVES_AWAY = "It says a self-check answer word for word. Present leaves the self-check to its own scene."
REFUSED = "The sources do not say this."


def noop(step):
    return None


class Crash(BaseException):
    """What a crash or a restart does to a deck being drawn: nothing after it runs."""


class Hand:
    """The stub models, with a hand on them: the author's deck can be changed before Dojo reads it, the gate
    can say that elements of a deck do not hold, and the author call can fail. It keeps the author's prompts,
    the deck documents on the share when the author was called, and the element ids the gate was asked about."""

    def __init__(self, dojo):
        self.dojo, self.real = dojo, dojo.ai.chat
        self.edit = None
        self.refuse: set[str] = set()
        self.error: BaseException | None = None
        self.prompts: list[str] = []
        self.saw: list[dict] = []
        self.asked: list[str] = []

    def __call__(self, role, system, user, json_mode=True):
        task = re.search(r"TASK: (\w+)", system).group(1)
        if task == "deck":
            self.prompts.append(user)
            self.saw.append({n: self.dojo.store.read("content", "decks", n) for n in self.dojo.store.names("content", "decks")})
            if self.error is not None:
                raise self.error
        out = self.real(role, system, user, json_mode)
        if task == "deck" and self.edit is not None:
            data = json.loads(out)
            self.edit(data)
            out = json.dumps(data)
        elif task == "gate_elements" and "Slide deck for the skill" in user:
            self.asked += re.findall(r"^\[([a-z]\d+(?:\.\w+)?)\]", user, re.M)
            data = json.loads(out)
            for v in data["verdicts"]:
                if v["id"] in self.refuse:
                    v.update(holds=False, reason=REFUSED)
            out = json.dumps(data)
        return out

    def __enter__(self):
        self._patch = mock.patch.object(self.dojo.ai, "chat", side_effect=self)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class DeckCase(unittest.TestCase):
    def setUp(self):
        self.app = new_app("dojo-deck-")
        self.dojo, self.prep = self.app.state.dojo, self.app.state.preparer
        self.sid = self.prep._skills("gh-300")[0]["id"]

    def view(self, lesson: str) -> dict:
        return self.dojo.deck_view(self.dojo.lesson(lesson))

    def scene(self, lesson: str, kind: str) -> dict:
        return next(sc for sc in self.view(lesson)["scenes"] if sc["kind"] == kind)


# ---------------------------------------------------------------- drawing one deck

class DrawTests(DeckCase):
    def test_the_attempt_is_saved_before_the_model_call_and_a_deck_that_holds_plays(self):
        with Models(self.dojo) as m:
            m.self_check = [{"question": QUESTION, "answer": ANSWER}]
            lesson = lesson_for(self.app, self.sid)
        self.assertEqual((self.view(lesson)["level"], self.view(lesson)["state"]), ("derived", "missing"))
        drawing = []
        with Hand(self.dojo) as hand:
            hand.edit = lambda d: drawing.append(self.view(lesson)["state"])
            res = self.dojo.make_deck(noop, lesson)
        self.assertEqual(res, {"lesson": lesson, "state": "ready", "kept_share": 1.0, "visuals": 2})
        saved = hand.saw[0][lesson]
        self.assertEqual((saved["state"], saved["attempts"], saved["spent_rounds"]), ("writing", 1, 1), "saved before the call")
        self.assertEqual(drawing, ["writing"])
        d = self.dojo.deck(lesson)
        self.assertEqual((d["state"], d["proposed"], d["held"], d["blocked"], d["format"]), ("ready", 11, 11, [], decks.DECK_FORMAT))
        self.assertEqual(set(d["models"]), {"author", "gate"})
        self.assertTrue(d["sources"])
        doc = self.dojo.lesson(lesson)
        self.assertEqual(hand.asked, [eid for eid, _ in decks.statements(d["spec"], doc)], "the gate saw every element")
        view = self.view(lesson)
        self.assertEqual((view["level"], view["state"], view["models"], view["created"]), ("visual", "ready", d["models"], d["created"]))
        self.assertEqual([sc["visual"]["type"] for sc in view["scenes"] if sc["kind"] == "slide" and sc["visual"]], ["flow", "contrast"])
        self.assertEqual([p["title"] for p in view["parts"]][:2], ["The idea", "In practice"])
        self.assertTrue(self.scene(lesson, "agenda")["outcomes"])
        prompt = hand.prompts[0]
        self.assertIn("<lesson>", prompt)
        for i, sl in enumerate(doc["slides"]):
            self.assertIn(f'SLIDE {i}: "{sl["title"]}"', prompt)
        self.assertEqual(doc["self_check"][0]["question"], QUESTION)
        self.assertNotIn(QUESTION, prompt, "the author never sees the self-check")
        self.assertNotIn(ANSWER, prompt)
        self.assertEqual(quality(self.dojo, "visual deck"), [])

    def test_what_cannot_be_drawn_or_quotes_a_page_wrongly_is_left_out_before_the_check(self):
        lesson = lesson_for(self.app, self.sid)

        def edit(d):
            d["slides"][0]["visual"]["steps"] = d["slides"][0]["visual"]["steps"][:2]
            d["slides"][0]["tip"]["quote"] = "Words that no page says."
            d["slides"][1]["visual"] = {"type": "chart", "bars": [1, 2]}
        with Hand(self.dojo) as hand:
            hand.edit = edit
            res = self.dojo.make_deck(noop, lesson)
        d = self.dojo.deck(lesson)
        self.assertEqual({b["part"]: b["reason"] for b in d["blocked"]}, {
            "slide 1 diagram": "a flow needs 3 to 6 short steps",
            "slide 1 tip": "a tip needs a short text and a short exact quote from the page",
            "slide 2 diagram": "unknown diagram type 'chart'"})
        self.assertFalse({"v0", "t0", "v1.l0", "v1.r0"} & set(hand.asked), "none of it reached the gate")
        # Measured against everything proposed: 8 of 11 is under three quarters.
        self.assertEqual((d["proposed"], d["held"], d["kept_share"], res["state"]), (11, 8, 0.73, "failed"))
        self.assertEqual(d["reason"], f"73% of it held, and the visual deck needs 75%. {DECK_KEEPS}")
        view = self.view(lesson)
        self.assertEqual((view["level"], view["state"]), ("derived", "failed"))
        self.assertNotIn("stopped", view)
        self.assertEqual([(e["part"], e["reason"]) for e in quality(self.dojo, "visual deck")],
                         [(b["part"], b["reason"]) for b in d["blocked"]] + [("whole", d["reason"])])

    def test_what_the_gate_refuses_is_left_out_and_a_list_left_too_short_goes(self):
        lesson = lesson_for(self.app, self.sid)
        with Hand(self.dojo) as hand:
            hand.refuse = {"o0", "o1"}
            res = self.dojo.make_deck(noop, lesson)
        self.assertEqual((res["state"], res["kept_share"]), ("ready", 0.82))
        d = self.dojo.deck(lesson)
        self.assertEqual([(b["part"], b["reason"]) for b in d["blocked"]], [("outcome 1", REFUSED), ("outcome 2", REFUSED)])
        self.assertEqual(d["spec"]["outcomes"], [], "one outcome is no list")
        agenda = self.scene(lesson, "agenda")
        self.assertEqual((agenda["outcomes"], agenda["title"]), (False, "In this lesson"))
        self.assertEqual(self.view(lesson)["level"], "visual", "the rest of the deck plays")

    def test_with_less_than_three_quarters_held_present_keeps_the_derived_deck(self):
        lesson = lesson_for(self.app, self.sid)
        with Hand(self.dojo) as hand:
            hand.refuse = {"o0", "o1", "o2"}
            res = self.dojo.make_deck(noop, lesson)
        self.assertEqual((res["state"], res["kept_share"], res["visuals"]), ("failed", 0.73, 2))
        d = self.dojo.deck(lesson)
        self.assertEqual(d["reason"], "73% of it held, and the visual deck needs 75%. Present keeps the deck built from the lesson.")
        view = self.view(lesson)
        self.assertEqual((view["level"], view["state"]), ("derived", "failed"))
        self.assertNotIn("models", view)
        self.assertFalse(self.dojo.deck_due(self.dojo.lesson(lesson)), "a failed deck is not drawn again on an open")
        whole = [e for e in quality(self.dojo, "visual deck") if e["part"] == "whole"]
        self.assertEqual([(e["lesson"], e["reason"]) for e in whole], [(lesson, d["reason"])])

    def test_a_line_that_gives_away_a_self_check_answer_never_reaches_the_gate(self):
        with Models(self.dojo) as m:
            m.self_check = [{"question": QUESTION, "answer": ANSWER}]
            lesson = lesson_for(self.app, self.sid)

        def edit(d):
            d["recap"][0] = ANSWER
        with Hand(self.dojo) as hand:
            hand.edit = edit
            res = self.dojo.make_deck(noop, lesson)
        self.assertIn("r1", hand.asked)
        self.assertNotIn("r0", hand.asked)
        d = self.dojo.deck(lesson)
        self.assertEqual(d["blocked"], [{"part": "recap 1", "text": ANSWER, "reason": GIVES_AWAY}])
        self.assertEqual((res["state"], d["held"]), ("ready", 10))
        recap = self.scene(lesson, "recap")
        titles = [sl["title"] for sl in self.dojo.lesson(lesson)["slides"]]
        self.assertEqual((recap["own"], recap["items"]), (False, titles), "two lines are no recap: the slide titles are")
        self.assertEqual(self.scene(lesson, "check")["items"][0]["answer"], ANSWER, "the self-check keeps its own scene")

    def test_a_diagram_whose_own_words_give_an_answer_away_never_reaches_the_gate(self):
        with Models(self.dojo) as m:
            m.self_check = [{"question": "Which mode keeps working when the hub link drops?",
                             "answer": "Use the hub link when it drops again."}]
            lesson = lesson_for(self.app, self.sid)

        def edit(d):
            d["slides"][1]["visual"] = {"type": "compare", "columns": ["Use the hub link", "when it drops", "again"],
                                        "rows": [{"label": "One", "cells": ["a", "b", "c"]}, {"label": "Two", "cells": ["d", "e", "f"]}]}
        with Hand(self.dojo) as hand:
            hand.edit = edit
            res = self.dojo.make_deck(noop, lesson)
        d = self.dojo.deck(lesson)
        self.assertEqual(d["blocked"], [{"part": "slide 2 diagram", "text": "compare", "reason": GIVES_AWAY}])
        self.assertFalse([eid for eid in hand.asked if eid.startswith("v1")], "none of it reached the gate")
        self.assertEqual((res["state"], d["proposed"], d["held"], d["visuals"]), ("ready", 11, 10, 1))
        self.assertEqual([sc["visual"]["type"] for sc in self.view(lesson)["scenes"] if sc["kind"] == "slide" and sc["visual"]], ["flow"])

    def test_a_deck_withdrawn_while_it_is_drawn_stays_withdrawn_and_keeps_what_it_cost(self):
        lesson = lesson_for(self.app, self.sid)
        with Hand(self.dojo) as hand:
            hand.edit = lambda d: self.assertTrue(self.dojo.invalidate_deck(lesson, "Withdrawn by a test."))
            res = self.dojo.make_deck(noop, lesson)
        self.assertEqual(res["state"], "invalidated")
        d = self.dojo.deck(lesson)
        self.assertEqual((d["state"], d["reason"], d["attempts"], d["spent_rounds"]), ("invalidated", "Withdrawn by a test.", 1, 1))
        self.assertNotIn("spec", d, "what the late answer made is not kept")
        self.assertEqual((self.view(lesson)["level"], self.view(lesson)["state"]), ("derived", "invalidated"))
        self.assertEqual(quality(self.dojo, "visual deck"), [])
        self.assertEqual(self.app.state.costs.deck_view()["so_far"]["rounds"], 1, "the attempt stays spent")
        self.assertEqual(self.dojo.make_deck(noop, lesson, True)["state"], "ready", "a withdrawn deck may be drawn again")

    def test_an_error_after_the_deck_was_withdrawn_does_not_bring_it_back(self):
        lesson = lesson_for(self.app, self.sid)

        def edit(d):
            self.dojo.invalidate_deck(lesson, "Withdrawn by a test.")
            raise AIError("The author model did not answer.")
        with Hand(self.dojo) as hand:
            hand.edit = edit
            with self.assertRaises(AIError):
                self.dojo.make_deck(noop, lesson)
        self.assertEqual(self.dojo.deck(lesson)["state"], "invalidated")

    def test_a_deck_deleted_while_it_is_drawn_is_not_brought_back(self):
        lesson = lesson_for(self.app, self.sid)
        with Hand(self.dojo) as hand:
            hand.edit = lambda d: self.dojo.drop_deck(lesson)
            res = self.dojo.make_deck(noop, lesson)
        self.assertEqual(res["state"], "missing")
        self.assertIsNone(self.dojo.deck(lesson))
        self.assertEqual(self.dojo.store.names("content", "decks"), [])
        self.assertEqual(quality(self.dojo, "visual deck"), [])

    def test_a_deck_withdrawn_or_deleted_before_its_attempt_is_saved_starts_nothing(self):
        lesson = lesson_for(self.app, self.sid)
        with Hand(self.dojo) as hand:
            self.assertEqual(self.dojo.make_deck(noop, lesson)["state"], "ready")
            real = self.dojo.grounding

            def withdrawn(pid, sid):
                self.dojo.invalidate_deck(lesson, "Withdrawn by a test.")
                return real(pid, sid)
            with mock.patch.object(self.dojo, "grounding", withdrawn):
                self.assertEqual(self.dojo.make_deck(noop, lesson)["state"], "invalidated")
            d = self.dojo.deck(lesson)
            self.assertEqual((d["state"], d["attempts"], d["spent_rounds"]), ("invalidated", 1, 1), "no second attempt was started")

            def deleted(pid, sid):
                self.dojo.drop_deck(lesson)
                return real(pid, sid)
            self.dojo.make_deck(noop, lesson)   # a withdrawn deck may be drawn again
            with mock.patch.object(self.dojo, "grounding", deleted):
                self.assertEqual(self.dojo.make_deck(noop, lesson)["state"], "missing")
            self.assertIsNone(self.dojo.deck(lesson))
        self.assertEqual(len(hand.prompts), 2, "the author was asked twice, never after a withdrawal or a deletion")

    def test_a_first_attempt_whose_deck_is_withdrawn_or_deleted_meanwhile_starts_nothing(self):
        lesson = lesson_for(self.app, self.sid)   # no deck document at all
        real = self.dojo.grounding
        for how in (self.dojo.drop_deck, lambda lesson_id: self.dojo.invalidate_deck(lesson_id, "Withdrawn by a test.")):
            def gone(pid, sid, how=how):
                how(lesson)
                return real(pid, sid)
            with Hand(self.dojo) as hand, mock.patch.object(self.dojo, "grounding", gone):
                self.assertEqual(self.dojo.make_deck(noop, lesson)["state"], "missing")
            self.assertIsNone(self.dojo.deck(lesson), "nothing was brought back")
            self.assertEqual(hand.prompts, [], "the author was never asked")
        self.assertEqual(self.dojo.make_deck(noop, lesson)["state"], "ready", "a later attempt is not held back")

    def test_one_valid_outcome_is_not_a_visual_deck(self):
        lesson = lesson_for(self.app, self.sid)

        def edit(d):
            d["outcomes"], d["sections"], d["slides"], d["recap"] = ["Explain the key idea of this skill."], [], [], []
        with Hand(self.dojo) as hand:
            hand.edit = edit
            res = self.dojo.make_deck(noop, lesson)
        d = self.dojo.deck(lesson)
        self.assertEqual((res["state"], d["proposed"], d["held"], d["kept_share"]), ("failed", 1, 1, 1.0))
        self.assertEqual(d["reason"], f"Nothing was left to draw once the checks were done. {DECK_KEEPS}")
        self.assertEqual((self.view(lesson)["level"], self.view(lesson)["state"]), ("derived", "failed"))
        self.assertFalse(self.dojo.deck_due(self.dojo.lesson(lesson)), "waits its week like any failed deck")

    def test_an_error_or_a_restart_leaves_a_failed_attempt_that_stays_spent(self):
        lesson = lesson_for(self.app, self.sid)
        with Hand(self.dojo) as hand:
            hand.error = AIError("The author model did not answer.")
            with self.assertRaises(AIError):
                self.dojo.make_deck(noop, lesson)
        d = self.dojo.deck(lesson)
        self.assertEqual((d["state"], d["stopped"], d["attempts"], d["spent_rounds"]), ("failed", True, 1, 1))
        self.assertEqual(d["reason"], f"It stopped on an error: The author model did not answer. {DECK_KEEPS}")
        view = self.view(lesson)
        self.assertEqual((view["level"], view["state"], view["stopped"]), ("derived", "failed", True))
        self.assertFalse(self.dojo.deck_drawing(lesson))
        self.assertFalse(self.prep._deck_due(lesson, utcnow()), "it waits its week")

        other = lesson_for(self.app, self.prep._skills("gh-300")[1]["id"])
        with Hand(self.dojo) as hand:
            hand.error = Crash()
            with self.assertRaises(Crash):
                self.dojo.make_deck(noop, other)
        self.assertEqual(self.dojo.store.read("content", "decks", other)["state"], "writing", "as a crash leaves it")
        d = self.dojo.deck(other)
        self.assertEqual((d["state"], d["stopped"], d["reason"], d["spent_rounds"]), ("failed", True, DECK_CUT_OFF, 1))
        self.assertEqual(self.view(other)["state"], "failed")
        self.assertFalse(self.dojo.deck_due(self.dojo.lesson(other)), "not drawn again on an open")
        self.assertFalse(self.prep._deck_due(other, utcnow()))
        self.assertTrue(self.prep._deck_due(other, utcnow() + prepare.DECK_RETRY + timedelta(minutes=1)), "but after a week")
        self.assertEqual(self.app.state.costs.deck_view()["so_far"]["rounds"], 2, "both attempts stay spent")

    def test_one_drawing_at_a_time_and_a_first_open_draws_nothing_drawn_meanwhile(self):
        lesson = lesson_for(self.app, self.sid)
        seen = []

        def again(d):
            with self.assertRaises(UserError) as e:
                self.dojo.make_deck(noop, lesson)
            seen.append(str(e.exception))
        with Hand(self.dojo) as hand:
            hand.edit = again
            self.dojo.make_deck(noop, lesson)
        self.assertEqual(seen, [DECK_BUSY])
        with self.assertRaises(UserError) as e:
            self.dojo.make_deck(noop, lesson, True)
        self.assertEqual(str(e.exception), DECK_DRAWN)
        self.assertEqual(self.dojo.deck(lesson)["attempts"], 1)
        self.assertTrue(self.dojo.invalidate_deck(lesson, "Withdrawn by a test."))
        self.assertEqual(self.view(lesson)["state"], "invalidated")
        self.assertEqual(self.dojo.make_deck(noop, lesson, True)["state"], "ready", "a withdrawn deck may be drawn again")
        self.assertEqual((self.dojo.deck(lesson)["attempts"], self.dojo.deck(lesson)["spent_rounds"]), (2, 2))

    def test_a_deleted_lesson_takes_its_deck_with_it(self):
        lesson = lesson_for(self.app, self.sid)
        self.dojo.make_deck(noop, lesson)
        self.dojo.drop_lesson(lesson)
        self.assertIsNone(self.dojo.deck(lesson))
        self.assertEqual(self.dojo.store.names("content", "decks"), [])


# ---------------------------------------------------------------- the Preparer

class PreparerTests(DeckCase):
    def setUp(self):
        super().setUp()
        # Present alone: Deeper Listen, which the Preparer writes first, has tests of its own (tests/test_deep.py).
        no_deep = mock.patch.object(prepare, "DEEP_PER_DAY", 0)
        no_deep.start()
        self.addCleanup(no_deep.stop)
        self.prep.next_skill = lambda: None
        self.prep.set_deck_ahead(True)   # drawing ahead of time is the owner's switch: off until it is turned on

    def narrated(self, k: int) -> str:
        lesson = lesson_for(self.app, self.prep._skills("gh-300")[k]["id"])
        self.dojo.lesson_audio(noop, self.prep.owner, lesson)
        return lesson

    def test_nothing_is_drawn_ahead_of_time_until_the_owner_turns_it_on(self):
        lesson = self.narrated(0)
        self.prep.set_deck_ahead(False)
        calls = len(self.dojo.ai.calls)
        self.assertEqual((self.prep.deck_ahead(), self.prep.deck_status()["ahead"]), (False, False))
        self.assertIsNone(self.prep.next_deck())
        self.assertFalse(self.prep.step(), "nothing to do: no deck, no film")
        self.assertEqual((self.dojo.deck(lesson), len(self.dojo.ai.calls)), (None, calls), "no model was called")
        self.assertTrue(self.prep.set_deck_ahead(True))
        self.assertEqual(self.prep.next_deck(), ("gh-300", self.sid, lesson))
        self.assertTrue(self.prep.step())
        self.assertEqual(self.dojo.deck(lesson)["state"], "ready")
        self.assertTrue(self.prep.deck_status()["ahead"])
        self.assertFalse(self.prep.set_deck_ahead(False))
        self.assertFalse(Preparer(self.dojo, self.prep.owner).deck_ahead(), "kept in the data share, across a restart")

    def test_it_stays_on_across_a_restart_and_a_new_day(self):
        again = Preparer(self.dojo, self.prep.owner)
        self.assertTrue(again.deck_ahead(), "a restart keeps it")
        with Clock(utcnow() + timedelta(days=1)):
            self.assertTrue(again.deck_ahead(), "and so does a new UTC day")
            self.assertEqual(again.deck_left(), prepare.DECK_PER_DAY)

    def test_at_most_twenty_a_day_counted_on_the_share_across_restarts(self):
        self.assertEqual((prepare.DECK_PER_DAY, prepare.DECK_RETRY, prepare.DECK_ATTEMPTS), (20, timedelta(days=7), 2))
        lessons = [self.narrated(k) for k in range(3)]
        with mock.patch.object(prepare, "DECK_PER_DAY", 2):
            self.assertTrue(self.prep.step())
            self.assertTrue(self.prep.step())
            self.assertFalse(self.prep.step(), "the day's allowance is spent")
            self.assertEqual([bool(self.dojo.deck(x)) for x in lessons], [True, True, False])
            day = self.dojo.store.read("content", "deck-state")
            self.assertEqual((day["day"], day["today"], day["written"]), (utcnow().date().isoformat(), 2, 2))
            again = Preparer(self.dojo, self.prep.owner)   # a restart
            again.next_skill = lambda: None
            self.assertEqual(again.deck_left(), 0)
            self.assertFalse(again.step(), "a restart gets no fresh allowance")
            with Clock(utcnow() + timedelta(days=1)):
                self.assertEqual(again.deck_left(), 2, "a new UTC day")
                self.assertTrue(again.step())
                self.assertFalse(again.step(), "nothing left to draw")
        self.assertEqual([self.dojo.deck(x)["state"] for x in lessons], ["ready"] * 3)
        self.assertEqual(self.prep.state["decked"], 2)
        status = self.prep.deck_status()
        self.assertEqual((status["ready"], status["lessons"], status["failed"], status["drawing"]), (3, 3, 0, 0))

    def test_a_deck_that_did_not_pass_is_drawn_again_after_a_week_at_most_twice(self):
        lesson = self.narrated(0)
        with Hand(self.dojo) as hand:
            hand.refuse = {"o0", "o1", "o2", "r0", "r1", "r2"}
            self.assertTrue(self.prep.step())
            self.assertEqual((self.dojo.deck(lesson)["state"], self.dojo.deck(lesson)["attempts"]), ("failed", 1))
            self.assertIsNone(self.prep.next_deck(), "not again within the week")
            week = utcnow() + prepare.DECK_RETRY + timedelta(minutes=1)
            with Clock(week):
                self.assertEqual(self.prep.next_deck(), ("gh-300", self.sid, lesson))
                self.assertTrue(self.prep.step())
            self.assertEqual((self.dojo.deck(lesson)["state"], self.dojo.deck(lesson)["attempts"]), ("failed", 2))
            with Clock(week + prepare.DECK_RETRY + timedelta(minutes=1)):
                self.assertIsNone(self.prep.next_deck(), "at most twice")
        self.assertEqual((self.prep.deck_status()["failed"], self.prep.deck_status()["ready"]), (1, 0))
        self.assertEqual(self.app.state.costs.deck_view()["so_far"]["rounds"], 2)

    def test_none_for_a_lesson_held_back_written_again_or_older(self):
        old = lesson_for(self.app, self.sid, created="2026-03-01T10:00:00+00:00")
        self.dojo.lesson_audio(noop, self.prep.owner, old)
        self.assertEqual(self.prep.next_deck(), ("gh-300", self.sid, old))
        held = SimpleNamespace(withheld=lambda lesson_id: lesson_id == old)
        with mock.patch.object(self.dojo, "drift", held, create=True):
            self.assertIsNone(self.prep.next_deck(), "a changed page holds it back: its rewrite gets the deck")
            self.assertEqual(self.view(old)["state"], "held")
            self.assertFalse(self.dojo.deck_due(self.dojo.lesson(old)))
        with mock.patch.object(self.app.state.deeper, "to_write_again", return_value={old}):
            self.assertIsNone(self.prep.next_deck(), "its new lesson on the deeper template gets the deck")
            self.assertEqual(self.prep.deck_status()["waiting_upgrade"], 1)
            self.assertEqual(self.view(old)["state"], "upgrading")
            self.assertFalse(self.dojo.deck_due(self.dojo.lesson(old)))
        new = self.narrated(0)
        self.assertEqual(self.view(old)["state"], "older")
        self.assertFalse(self.dojo.deck_due(self.dojo.lesson(old)))
        self.assertEqual(self.prep.next_deck(), ("gh-300", self.sid, new))

    def test_a_ready_deck_of_a_held_lesson_stops_playing_and_its_rewrite_withdraws_it(self):
        old = lesson_for(self.app, self.sid, created="2026-03-01T10:00:00+00:00")
        self.dojo.make_deck(noop, old)
        held = SimpleNamespace(withheld=lambda lesson_id: lesson_id == old)
        with mock.patch.object(self.dojo, "drift", held, create=True):
            view = self.view(old)
            self.assertEqual((view["state"], view["level"]), ("held", "derived"))
            self.assertEqual(self.dojo.deck(old)["state"], "ready", "held, not withdrawn")
            self.assertEqual(self.prep.deck_status()["ready"], 0)
            new = self.dojo.make_lesson(noop, self.prep.owner, "gh-300", self.sid)["lesson"]   # its rewrite
        d = self.dojo.deck(old)
        self.assertEqual(d["state"], "invalidated")
        self.assertEqual(d["reason"], f"A changed source page no longer supports this lesson; {new} was written in its place.")
        self.assertTrue(self.dojo.deck_due(self.dojo.lesson(new)))
        self.dojo.make_deck(noop, new)
        upgraded = self.dojo.make_lesson(noop, self.prep.owner, "gh-300", self.sid, upgrade_of=new)["lesson"]
        self.assertIn(f"written again on the deeper template; {upgraded} was written", self.dojo.deck(new)["reason"])
        self.assertEqual(self.dojo.deck(new)["state"], "invalidated")
        so_far = self.app.state.costs.deck_view()["so_far"]
        self.assertEqual((so_far["rounds"], so_far["decks"]), (2, 2), "what the withdrawn decks cost stays counted")


class DeckDriftTests(DriftCase):
    """With the source-drift check itself (ADR 0006): a changed page holds the deck back, the rewrite comes
    first, and the new lesson gets its own deck."""

    def test_the_rewrite_comes_first_and_the_new_lesson_gets_its_own_deck(self):
        no_deep = mock.patch.object(prepare, "DEEP_PER_DAY", 0)
        no_deep.start()
        self.addCleanup(no_deep.stop)
        prep = self.app.state.preparer
        prep.set_deck_ahead(True)
        lesson = self.lesson()
        self.dojo.make_deck(noop, lesson)
        self.assertEqual(self.dojo.deck_view(self.dojo.lesson(lesson))["level"], "visual")
        self.web.set(PAGE, GITHUB_MD.replace(Q2, NEW))
        self.read_due()
        self.assertTrue(self.drift.withheld(lesson))
        view = self.dojo.deck_view(self.dojo.lesson(lesson))
        self.assertEqual((view["state"], view["level"]), ("held", "derived"))
        time.sleep(1.1)  # lessons are ordered by the second they were written in
        self.assertTrue(prep.step())
        new = self.dojo.lessons_for(PID, S)[0]["id"]
        self.assertNotEqual(new, lesson, "the rewrite came first")
        self.assertEqual(self.dojo.deck(lesson)["state"], "invalidated")
        self.assertIn(new, self.dojo.deck(lesson)["reason"])
        prep.next_skill = lambda: None   # the other skills are not part of this test
        while prep.next_narration() or prep.next_deck():
            self.assertTrue(prep.step())
        self.assertEqual(self.dojo.deck_view(self.dojo.lesson(new))["level"], "visual")
        self.assertEqual(self.dojo.deck(lesson)["state"], "invalidated", "for good")


# ---------------------------------------------------------------- a learner's first open, in team mode

class RouteTests(Team2Case):
    def setUp(self):
        super().setUp()
        self.a = self.admit("m-a")
        self.ka = self.key("m-a")
        self.budget, self.dojo = self.app.state.budget, self.app.state.dojo
        self.stub = self.dojo.ai
        skills = self.app.state.preparer._skills("gh-300")
        self.sid = skills[0]["id"]
        self.lesson = lesson_for(self.app, self.sid)
        self.other = lesson_for(self.app, skills[1]["id"])

    def path(self, lesson: str) -> str:
        return f"/api/lessons/{lesson}/deck"

    def record(self) -> bytes:
        return self.store.read_bytes("learners", self.ka, "events.jsonl") or b""

    def left(self) -> int:
        return next(x for x in self.ok("GET", "/api/me/allowance", self.a)["actions"] if x["action"] == "deck")["left"]

    def test_a_first_open_draws_the_deck_as_one_of_the_members_visual_decks_for_the_day(self):
        self.assertEqual(self.ok("GET", f"/api/lessons/{self.lesson}", self.a)["deck"]["state"], "missing")
        before, record, reserved = snapshot(self.folder(self.ka)), self.record(), []
        reserve, chat, go = self.budget.reserve, self.stub.chat, threading.Event()

        def slow(role, system, user, json_mode=True):
            if "TASK: deck" in system:
                go.wait(10)
            return chat(role, system, user, json_mode)
        with mock.patch.object(self.budget, "reserve", lambda *a, **k: reserved.append(a) or reserve(*a, **k)), \
                mock.patch.object(self.stub, "chat", side_effect=slow):
            job = self.ok("POST", self.path(self.lesson), self.a)
            self.assertEqual(job["kind"], "deck")
            self.assertEqual(self.ok("POST", self.path(self.lesson), self.a)["id"], job["id"], "asking again: the same job")
            for _ in range(200):
                if self.dojo.deck_drawing(self.lesson):
                    break
                time.sleep(0.02)
            self.assertEqual(self.ok("POST", self.path(self.lesson), OWNER), {"started": False, "state": "writing"})
            go.set()
            done = self.wait_job(job, self.a)
        self.assertEqual((done["state"], done["result"]["state"]), ("done", "ready"))
        self.assertEqual([a[:2] for a in reserved], [(self.ka, "deck")], "one reservation, the member's")
        self.assertEqual(reserved[0][3], self.budget.action_amount("deck"))
        self.wait_all(self.ka)
        after = snapshot(self.folder(self.ka))
        self.assertEqual({k for k in before.keys() | after.keys() if before.get(k) != after.get(k)} - {"allowance.json"}, set())
        self.assertEqual(self.record(), record, "nothing in the learner's Record")
        deck = self.ok("GET", f"/api/lessons/{self.lesson}", self.a)["deck"]
        self.assertEqual((deck["level"], deck["state"]), ("visual", "ready"))
        calls = len(self.stub.calls)
        self.assertEqual(self.ok("POST", self.path(self.lesson), self.a), {"started": False, "state": "ready"})
        self.assertEqual(self.ok("POST", self.path(self.lesson), OWNER), {"started": False, "state": "ready"})
        self.assertEqual(len(self.stub.calls), calls)
        self.assertEqual(self.left(), 2)

    def test_when_it_is_not_due_nothing_starts_and_nothing_is_reserved(self):
        old = lesson_for(self.app, self.sid, created="2026-03-01T10:00:00+00:00")
        self.dojo._deck_save(self.other, {"lesson": self.other, "state": "failed", "attempts": 1, "spent_rounds": 1,
                                          "created": iso(), "reason": "Did not pass."})
        calls = len(self.stub.calls)
        with mock.patch.object(self.budget, "reserve") as reserve:
            self.assertEqual(self.ok("POST", self.path(old), self.a), {"started": False, "state": "older"})
            self.assertEqual(self.ok("POST", self.path(self.other), self.a), {"started": False, "state": "failed"})
            held = SimpleNamespace(withheld=lambda lesson_id: lesson_id == self.lesson)
            with mock.patch.object(self.dojo, "drift", held, create=True):
                self.assertEqual(self.ok("POST", self.path(self.lesson), self.a), {"started": False, "state": "held"})
            self.dojo._deck_writing.add(self.lesson)
            try:
                self.assertEqual(self.ok("POST", self.path(self.lesson), self.a), {"started": False, "state": "writing"})
            finally:
                self.dojo._deck_writing.discard(self.lesson)
        reserve.assert_not_called()
        self.assertEqual(len(self.stub.calls), calls)
        self.assertEqual(self.left(), 3)

    def test_two_opens_at_once_reserve_and_start_once(self):
        reserve, start, reserved = self.budget.reserve, self.dojo.jobs.start, []

        def slow_start(*a, **k):
            time.sleep(0.5)   # the second open arrives while the first is between its checks and its start
            return start(*a, **k)
        with mock.patch.object(self.budget, "reserve", lambda *a, **k: reserved.append(a) or reserve(*a, **k)), \
                mock.patch.object(self.dojo.jobs, "start", slow_start), \
                futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.call, "POST", self.path(self.lesson), self.a)
            time.sleep(0.15)
            second = pool.submit(self.call, "POST", self.path(self.lesson), self.a)
            a, b = first.result(30), second.result(30)
        self.assertEqual((a.status_code, b.status_code), (200, 200), (a.text, b.text))
        self.assertEqual(a.json()["id"], b.json()["id"], "the same job")
        self.assertEqual([x[:2] for x in reserved], [(self.ka, "deck")], "one reservation")
        done = self.wait_job(a.json(), self.a)
        self.assertEqual((done["state"], done["result"]["state"]), ("done", "ready"))
        self.wait_all(self.ka)
        self.assertEqual(len([c for c in self.stub.calls if c[1] == "deck"]), 1, "one author call")
        self.assertEqual(self.left(), 2, "one deck drawn and counted")

    def test_two_learners_opening_the_same_lesson_at_once_reserve_once(self):
        b_headers = self.admit("m-b")
        reserve, start, reserved = self.budget.reserve, self.dojo.jobs.start, []

        def slow_start(*a, **k):
            time.sleep(0.5)
            return start(*a, **k)
        with mock.patch.object(self.budget, "reserve", lambda *a, **k: reserved.append(a) or reserve(*a, **k)), \
                mock.patch.object(self.dojo.jobs, "start", slow_start), \
                futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.call, "POST", self.path(self.lesson), self.a)
            time.sleep(0.15)
            second = pool.submit(self.call, "POST", self.path(self.lesson), b_headers)
            a, b = first.result(30), second.result(30)
        self.assertEqual((a.status_code, b.status_code), (200, 200), (a.text, b.text))
        self.assertEqual(b.json(), {"started": False, "state": "writing"}, "the second learner's open starts nothing")
        self.assertEqual([x[:2] for x in reserved], [(self.ka, "deck")], "one reservation, the first learner's")
        self.wait_job(a.json(), self.a)
        self.wait_all(self.ka)
        self.assertEqual(len([c for c in self.stub.calls if c[1] == "deck"]), 1)

    def test_at_the_allowance_a_member_is_refused_and_nothing_starts(self):
        self.ok("PUT", "/api/team/budget", OWNER, {"allowances": {"deck": 1}})
        self.assertEqual(self.wait_job(self.ok("POST", self.path(self.lesson), self.a), self.a)["state"], "done")
        self.wait_all(self.ka)
        before, calls = snapshot(self.folder(self.ka)), len(self.stub.calls)
        r = self.call("POST", self.path(self.other), self.a)
        self.assertEqual((r.status_code, r.json()["detail"]), (429, personal_refusal("deck")))
        self.assertEqual(personal_refusal("deck"), "You have used today's allowance for visual decks. It starts again at midnight UTC.")
        self.assertEqual((len(self.stub.calls), snapshot(self.folder(self.ka))), (calls, before))
        self.assertIsNone(self.dojo.deck(self.other))
        self.assertEqual(self.ok("GET", f"/api/lessons/{self.other}", self.a)["deck"]["state"], "missing",
                         "the Preparer may still draw it")
        self.assertEqual(self.left(), 0)

    def test_only_the_owner_turns_drawing_ahead_on_and_it_is_off_until_then(self):
        prep = self.app.state.preparer
        self.assertFalse(prep.deck_ahead(), "off by default: it spends money nobody asked for")
        self.assertIsNone(prep.next_deck())
        self.assertEqual(self.call("POST", "/api/studio/decks/ahead", self.a, {"on": True}).status_code, 403)
        self.assertFalse(prep.deck_ahead(), "a member cannot turn it on")
        self.assertEqual(self.ok("GET", "/api/studio/costs", OWNER)["deck"]["ahead"], False)
        self.assertEqual(self.ok("POST", "/api/studio/decks/ahead", OWNER, {"on": True}), {"ahead": True})
        self.assertEqual((prep.deck_ahead(), prep.next_deck()[2]), (True, self.lesson))
        self.assertEqual(self.ok("GET", "/api/studio/costs", OWNER)["deck"]["ahead"], True)
        self.assertEqual(self.call("POST", "/api/studio/decks/ahead", OWNER, {"on": "yes please"}).status_code, 422)
        self.assertEqual(self.ok("POST", "/api/studio/decks/ahead", OWNER, {"on": False}), {"ahead": False})
        self.assertIsNone(prep.next_deck())
        # A first open draws one deck whether it is on or off.
        self.assertEqual(self.wait_job(self.ok("POST", self.path(self.lesson), self.a), self.a)["state"], "done")

    def test_the_owner_is_never_refused_and_the_background_reserves_nothing(self):
        self.ok("PUT", "/api/team/budget", OWNER, {"allowances": {"deck": 0}})
        r = self.call("POST", self.path(self.lesson), self.a)
        self.assertEqual((r.status_code, r.json()["detail"]), (429, personal_refusal("deck")))
        self.assertEqual(self.wait_job(self.ok("POST", self.path(self.lesson), OWNER), OWNER)["state"], "done")
        self.assertEqual(self.dojo.deck(self.lesson)["state"], "ready")
        prep = self.app.state.preparer
        prep.set_deck_ahead(True)
        with mock.patch.object(self.budget, "reserve") as reserve:
            self.assertEqual(prep.next_deck()[2], self.other)
            self.assertTrue(prep._deck())
        reserve.assert_not_called()
        self.assertEqual(self.dojo.deck(self.other)["state"], "ready")


class StudioSwitchScreenTests(unittest.TestCase):
    """Drawing visual decks ahead of time spends money nobody asked for, so Studio turns it on only after the
    price for every lesson was shown and the owner said yes; turning it off asks nothing."""

    SCRIPT = (APP_DIR / "static" / "app.js").read_text("utf-8")

    def test_turning_it_on_shows_the_price_for_every_lesson_first(self):
        card = self.SCRIPT.split("function deckCosts(first) {", 1)[1].split("\n}\n", 1)[0]
        ask = card.index("if (!d.ahead && !window.confirm(`Draw the visual decks ahead of time?")
        post = card.index('"/api/studio/decks/ahead"')
        self.assertLess(ask, post, "asked before anything is switched on")
        self.assertIn('${all.complete ? "about" : "at least"} ${usd(all.total)} at list price in all', card)
        self.assertIn("A lesson you open in Present gets its deck drawn right away, whether this is on or not.", card)
        self.assertIn("body: { on: !d.ahead }", card)


# ---------------------------------------------------------------- what the decks cost

class DeckCostTests(unittest.TestCase):
    def test_studio_and_diag_show_the_decks_and_their_cost_at_list_price(self):
        desk = PriceDesk()
        desk.body = {**SAMPLE, "Items": SAMPLE["Items"] + MODELS["Items"], "Count": 15}
        app = create_app(dataclasses.replace(settings(tempfile.mkdtemp(prefix="dojo-deck-cost-")), models=LIVE),
                         http=httpx.Client(transport=httpx.MockTransport(desk)))
        c, dojo, prep = TestClient(app), app.state.dojo, app.state.preparer
        lesson = lesson_for(app, prep._skills("gh-300")[0]["id"])
        other = lesson_for(app, prep._skills("gh-300")[1]["id"])
        dojo.make_deck(noop, lesson)
        with Hand(dojo) as hand:
            hand.refuse = {"o0", "o1", "o2", "r0", "r1", "r2"}
            dojo.make_deck(noop, other)
        deck = c.get("/api/studio/costs").json()["deck"]
        self.assertEqual((deck["ready"], deck["lessons"], deck["failed"], deck["today"], deck["per_day"]), (1, 2, 1, 0, 20))
        self.assertIs(deck["ahead"], False)
        so_far, every = deck["so_far"], deck["every_lesson"]
        self.assertEqual((so_far["rounds"], so_far["decks"]), (2, 2), "the deck that did not pass is counted too")
        self.assertTrue(so_far["complete"] and every["complete"])
        self.assertEqual([line["key"] for line in so_far["lines"]], ["deck_write", "deck_gate"])
        self.assertEqual((every["lessons"], every["per_lesson"]), (2, round(every["total"] / 2, 2)))
        self.assertAlmostEqual(every["total"], so_far["total"] * 1.2, delta=0.03)
        self.assertTrue(0.05 < every["per_lesson"] < 0.6, every["per_lesson"])
        self.assertTrue(any("gpt-5.5" in a and "grok-4-1-fast-reasoning" in a for a in deck["assumptions"]))
        diag = c.get("/api/diag").json()["present_deck"]
        self.assertEqual((diag["ready"], diag["lessons"], diag["per_day"], diag["today"]), (1, 2, 20, 0))
        self.assertEqual((diag["so_far"], diag["every_lesson"], diag["per_lesson"], diag["rounds"], diag["complete"]),
                         (so_far["total"], every["total"], every["per_lesson"], 2, True))


if __name__ == "__main__":
    unittest.main()
