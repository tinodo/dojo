"""Team Dojo, PR 3 (docs/adr/0009-team-dojo.md, "Money"): preventive reservations for every paid call,
bounded per call and settled after it, the learner's daily allowance and the members' daily cap, the pooled
meter under the 5-member rule, and the audio bound from samples Dojo decoded itself.

Like tests/test_team.py, apps run as in Azure (local=False, signed in through X-MS-CLIENT-PRINCIPAL) with
stub models, on their own temporary data folders. The stub reports usage, and with no price list the
budget prices it at its pretend list price (app/budget.py STUB)."""
import io
import json
import math
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import av
import httpx

from app import ai as ai_mod
from app import budget as budget_mod
from app import exam as exam_mod
from app.ai import Foundry
from app.budget import (CAP_USD, SHARED_BUSY, SHARED_REFUSAL, STUB, TOKENS_PER_MESSAGE, TOKENS_PER_REQUEST, Budget, Refused,
                        chat_input_bound, max_out, personal_refusal)
from app.core import Learner, Spend, Store, UserError, current_spend, spending, utcnow
from app.speech import MAX_AUDIO_SECONDS, NORMALIZE_RATE, WAV_HEADER, AudioRefused, normalize
from tests.test_api import POST
from tests.test_team import OWNER, OWNER_KEY
from tests.test_team2 import ID, Team2Case, snapshot

TONE = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 400 * i / 16000))) for i in range(16000))
TYPES = {"webm": "audio/webm", "ogg": "audio/ogg", "mp4": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav"}


def encode(fmt: str, codec: str, seconds: float, rate: int = 16000, video: bool = False) -> bytes:
    """A recording made with FFmpeg's own encoders: a 400 Hz tone, mono."""
    buf = io.BytesIO()
    with av.open(buf, "w", format=fmt) as c:
        s = c.add_stream(codec, rate=rate)
        s.layout = "mono"
        if codec in ("libopus", "aac", "libmp3lame"):
            s.bit_rate = 24000
        v = None
        if video:
            v = c.add_stream("mpeg4", rate=1)
            v.width, v.height, v.pix_fmt = 64, 64, "yuv420p"
        n, t = int(seconds * rate), 0
        step = s.codec_context.frame_size or 1024
        while t < n:
            k = min(step, n - t)
            f = av.AudioFrame(format="s16", layout="mono", samples=k)
            off = (t % rate) * 2
            f.planes[0].update((TONE + TONE)[off: off + k * 2])
            f.sample_rate, f.pts = rate, t
            for p in s.encode(f):
                c.mux(p)
            t += k
        for p in s.encode(None):
            c.mux(p)
        if v is not None:
            for p in v.encode(av.VideoFrame(64, 64, "yuv420p")):
                c.mux(p)
            for p in v.encode(None):
                c.mux(p)
    return buf.getvalue()


def budget_on(tmp: str, owner: str = "owner", members=None, enrolled=None) -> Budget:
    settings = SimpleNamespace(team=True, local=False, real_ai=False)
    return Budget(settings, Store(Path(tmp)), owner, None, members=members, enrolled_counts=enrolled)


# ---------------------------------------------------------------- bounds

class BoundTests(unittest.TestCase):
    def test_the_input_bound_is_at_least_one_token_per_byte_sent(self):
        text = "Grüße, 日本語 and 🎉 " * 50
        body = {"messages": [{"role": "system", "content": "TASK: grade"}, {"role": "user", "content": text}],
                "response_format": {"type": "json_object"}}
        bound = chat_input_bound(body)
        sent = json.dumps(body["messages"], ensure_ascii=False) + json.dumps(body["response_format"], ensure_ascii=False)
        self.assertEqual(bound, len(sent.encode("utf-8")) + 2 * TOKENS_PER_MESSAGE + TOKENS_PER_REQUEST)
        # A byte-level BPE token covers at least one byte: no tokenization of what is sent can exceed this.
        self.assertGreater(bound, len(text.encode("utf-8")) + len("TASK: grade".encode("utf-8")))

    def test_the_output_bound_is_the_table(self):
        self.assertEqual(max_out("author", "lesson"), 32000)
        self.assertEqual(max_out("grader", "grade"), 8000)
        self.assertEqual(max_out("gate", "anything"), 12000)

    def test_stub_prices_apply_only_without_real_models(self):
        b = budget_on(tempfile.mkdtemp(prefix="dojo-t3-"))
        self.assertEqual(b.rates("author"), (STUB["input"], STUB["output"]))
        b.settings.real_ai = True
        b._prices = (time.monotonic(), {})
        self.assertEqual(b.rates("author"), (budget_mod.CEILING["input"], budget_mod.CEILING["output"]))


# ---------------------------------------------------------------- the Foundry client, metered

class FoundryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-t3-")
        self.budget = budget_on(self.tmp)
        self.bodies: list[dict] = []
        self.replies: list = []
        settings = SimpleNamespace(ai_endpoint="https://x.openai.azure.com/openai/v1")
        tokens = SimpleNamespace(get=lambda scope: "token")
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        self.f = Foundry(settings, tokens, http=http)
        self.f.meter = self.budget
        sleep = mock.patch.object(ai_mod.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0) if self.replies else ok_reply()
        return reply

    def act(self, learner: str = "m1", amount: float = 0.0):
        return self.budget.reserve(learner, "answer", "gh-300", amount)

    def allowance(self, learner: str = "m1") -> dict:
        return self.budget._allowance(learner)["days"][budget_mod._day()]

    def test_every_call_carries_its_output_bound_and_settles_from_usage(self):
        hold = self.act()
        with spending(Spend("m1", "answer", "gh-300", hold)):
            self.assertEqual(self.f.chat("grader", "TASK: grade", "hello"), '{"ok": 1}')
        hold.release()
        self.assertEqual(self.bodies[0]["max_completion_tokens"], max_out("grader", "grade"))
        today = self.allowance()
        self.assertAlmostEqual(today["spent"], self.budget.chat_cost("grader", 100, 20), places=6)
        self.assertEqual(today["open"], {})
        cap = self.budget._cap()
        self.assertAlmostEqual(cap["spent"], today["spent"], places=6)
        self.assertEqual(cap["open"], {})

    def test_with_no_meter_the_request_is_as_today(self):
        self.f.meter = None
        self.f.chat("grader", "TASK: grade", "hello")
        self.assertNotIn("max_completion_tokens", self.bodies[0])
        self.assertNotIn("max_tokens", self.bodies[0])
        self.assertFalse((Path(self.tmp) / "team").exists())

    def test_an_empty_answer_twice_reserves_again_for_each_call(self):
        self.replies = [ok_reply(content=""), ok_reply(content=""), ok_reply()]
        hold = self.act()
        bounds = []
        real = self.budget.open_call
        with mock.patch.object(self.budget, "open_call", lambda *a, **k: bounds.append(a) or real(*a, **k)):
            with spending(Spend("m1", "answer", "gh-300", hold)):
                self.f.chat("grader", "TASK: grade", "hello")
        hold.release()
        self.assertEqual(len(bounds), 3)   # one reservation per POST, never one for the whole retry loop
        self.assertAlmostEqual(self.allowance()["spent"], 3 * self.budget.chat_cost("grader", 100, 20), places=6)

    def test_a_model_that_wants_max_tokens_gets_it(self):
        self.replies = [httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_completion_tokens'"}})]
        with spending(Spend("m1", "answer", None, self.act())):
            self.f.chat("grader", "TASK: grade", "hello")
            self.f.chat("grader", "TASK: grade", "again")
        self.assertIn("max_completion_tokens", self.bodies[0])
        self.assertEqual(self.bodies[1]["max_tokens"], max_out("grader", "grade"))
        self.assertNotIn("max_completion_tokens", self.bodies[2])

    def test_no_usage_costs_the_whole_bound(self):
        self.replies = [ok_reply(usage=None)]
        hold = self.act()
        with spending(Spend("m1", "answer", None, hold)):
            self.f.chat("grader", "TASK: grade", "hello")
        hold.release()
        self.assertGreater(self.allowance()["spent"], self.budget.chat_cost("grader", 0, max_out("grader", "grade")))

    def test_usage_above_the_bound_is_booked_and_flagged(self):
        self.replies = [ok_reply(usage={"prompt_tokens": 100000, "completion_tokens": max_out("grader", "grade") + 1})]
        hold = self.act()
        with spending(Spend("m1", "answer", None, hold)):
            self.f.chat("grader", "TASK: grade", "hello")
        hold.release()
        self.assertEqual(self.budget.flags()["kinds"], ["grader/grade"])
        self.assertAlmostEqual(self.allowance()["spent"],
                               self.budget.chat_cost("grader", 100000, max_out("grader", "grade") + 1), places=6)

    def test_reasoning_reported_outside_completion_tokens_is_booked_as_output(self):
        """grok counts its reasoning in total_tokens, not in completion_tokens; it is billed as output."""
        self.replies = [ok_reply(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 1620,
                                        "completion_tokens_details": {"reasoning_tokens": 1500}})]
        hold = self.act()
        with spending(Spend("m1", "answer", None, hold)):
            self.f.chat("grader", "TASK: grade", "hello")
        hold.release()
        self.assertAlmostEqual(self.allowance()["spent"], self.budget.chat_cost("grader", 100, 1520), places=6)

    def test_reasoning_inside_completion_tokens_is_not_booked_twice(self):
        for usage, out in (({"prompt_tokens": 100, "completion_tokens": 1520, "total_tokens": 1620,
                             "completion_tokens_details": {"reasoning_tokens": 1500}}, 1520),
                           ({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": "n/a"}, 20),
                           ({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 50}, 20)):
            with self.subTest(usage=usage):
                self.assertAlmostEqual(ai_mod._usage_cost(self.budget, "grader", {"usage": usage}),
                                       self.budget.chat_cost("grader", 100, out), places=9)

    def test_a_call_that_does_not_fit_the_cap_is_never_sent(self):
        self.budget.set_limits(None, 0.0001)
        hold = self.act()
        with spending(Spend("m1", "answer", None, hold)):
            with self.assertRaises(Refused):
                self.f.chat("grader", "TASK: grade", "hello")
        hold.release()
        self.assertEqual(self.bodies, [])

    def test_a_failed_connection_costs_nothing(self):
        def down(request):
            raise httpx.ConnectError("down")
        self.f.http = httpx.Client(transport=httpx.MockTransport(down))
        hold = self.act()
        with spending(Spend("m1", "answer", None, hold)):
            with self.assertRaises(ai_mod.AIError):
                self.f.chat("grader", "TASK: grade", "hello")
        hold.release()
        self.assertEqual(self.allowance()["spent"], 0.0)


def ok_reply(content: str = '{"ok": 1}', usage: dict | None = {"prompt_tokens": 100, "completion_tokens": 20}) -> httpx.Response:
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


# ---------------------------------------------------------------- the allowance and the cap

class CapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-t3-")
        self.members = ["m1", "m2", "m3"]
        self.enrolled = {}
        self.b = budget_on(self.tmp, members=lambda: self.members, enrolled=lambda: self.enrolled)

    def test_a_member_is_refused_at_the_allowance_and_the_owner_never(self):
        self.b.set_limits({"exam": 2}, None)
        for _ in range(2):
            self.b.reserve("m1", "exam", "gh-300", 0.1).release()
        with self.assertRaises(Refused) as e:
            self.b.reserve("m1", "exam", "gh-300", 0.1)
        self.assertEqual(str(e.exception), personal_refusal("exam"))
        self.assertEqual(self.b._allowance("m1")["days"][budget_mod._day()]["counts"]["exam"], 2)
        self.b.set_limits(None, 0.0)
        for _ in range(5):   # metered, never refused: past the allowance and past the cap
            self.b.reserve("owner", "exam", "gh-300", 1.0).release()
        self.assertEqual(self.b.left_today("owner")["actions"][6]["used"], 5)

    def test_the_cap_is_preventive_under_concurrency(self):
        amount, results = 0.6, []
        go = threading.Barrier(30)

        def one(who):
            go.wait()
            try:
                results.append(self.b.reserve(who, "answer", "gh-300", amount))
            except Refused as e:
                results.append(e)

        threads = [threading.Thread(target=one, args=(self.members[i % 3],)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        held = [r for r in results if not isinstance(r, Exception)]
        self.assertEqual(len(held), int(CAP_USD // amount))
        # Refused calmly: "busy" while reservations of work still running fill the cap (nothing is spent yet).
        self.assertTrue(all(str(r) == SHARED_BUSY for r in results if isinstance(r, Exception)))
        self.assertLessEqual(self.b._cap_total(self.b._cap()), CAP_USD + 1e-9)

    def test_a_reservation_left_by_a_crash_still_counts(self):
        self.b.reserve("m1", "answer", None, 4.5)   # never released: the process died
        after = budget_on(self.tmp, members=lambda: self.members)
        with self.assertRaises(Refused):
            after.reserve("m2", "answer", None, 1.0)
        after.reserve("m2", "answer", None, 0.4).release()

    def test_at_startup_what_a_crash_left_open_is_settled_by_what_it_drew(self):
        idle = self.b.reserve("m1", "answer", "gh-300", 2.0)          # set aside, no call yet
        busy = self.b.reserve("m2", "answer", "gh-300", 2.0)
        with spending(Spend("m2", "answer", "gh-300", busy)):
            self.b.open_call("grader/grade", 0.7)                     # sent when the process died
        drawn = {e["drawn"] for e in self.b._cap()["open"].values()}
        self.assertEqual(drawn, {0.0, 0.7})
        after = budget_on(self.tmp, members=lambda: self.members)    # the new process, before it serves
        after.recover(["owner", "m1", "m2"])
        cap = after._cap()
        self.assertEqual((cap["open"], cap["spent"]), ({}, 0.7))       # the call in flight stays spent
        for key in ("m1", "m2"):
            self.assertEqual(after._allowance(key)["days"][budget_mod._day()]["open"], {})
        after.reserve("m3", "answer", None, CAP_USD - 0.7).release()   # the rest of the cap is free again
        self.assertFalse(idle.closed)                                  # (the old process's hold, never settled)

    def test_an_entry_no_live_hold_owns_expires_and_keeps_what_it_drew(self):
        live = self.b.reserve("m1", "answer", None, 1.0)
        old = (utcnow() - timedelta(seconds=budget_mod.STALE_S + 60)).isoformat(timespec="seconds")
        cap = self.b._cap()
        cap["open"]["c-gone"] = {"amount": 2.0, "at": old, "drawn": 0.5}
        cap["open"]["c-recent"] = {"amount": 1.0, "at": utcnow().isoformat(timespec="seconds"), "drawn": 0.0}
        for cid in cap["open"]:
            if cid in self.b._live:
                cap["open"][cid]["at"] = old                            # this process's own hold: never expired
        self.b.store.write("team", "cap-today", value=cap)
        seen = self.b._cap()
        self.assertEqual(set(seen["open"]), {live.cap_id, "c-recent"})
        self.assertEqual(seen["spent"], 0.5)
        self.b.prune(["m1"])
        self.assertNotIn("c-gone", self.b.store.read("team", "cap-today")["open"])
        live.release()
        self.assertEqual(self.b._cap()["open"].keys(), {"c-recent"})

    def test_leaving_does_not_lower_todays_sum(self):
        hold = self.b.reserve("m1", "answer", None, 1.0)
        with spending(Spend("m1", "answer", None, hold)):
            self.b.settle(self.b.open_call("grader/grade", 0.5), 0.3)
        hold.release()
        before = self.b._cap()["spent"]
        shutil.rmtree(Path(self.tmp) / "learners" / "m1")   # Leave deletes the whole folder
        self.assertEqual(self.b._cap()["spent"], before)
        raw = (Path(self.tmp) / "team" / "cap-today.json").read_text("utf-8")
        self.assertNotIn("m1", raw)
        self.assertEqual(set(json.loads(raw)), {"day", "spent", "open"})

    def test_after_leave_the_spent_sum_still_refuses_others(self):
        self.b.set_limits(None, 1.0)
        hold = self.b.reserve("m1", "answer", None, 0.8)
        with spending(Spend("m1", "answer", None, hold)):
            self.b.settle(self.b.open_call("grader/grade", 0.8), 0.8)
        hold.release()
        shutil.rmtree(Path(self.tmp) / "learners" / "m1")
        with self.assertRaises(Refused) as e:
            self.b.reserve("m2", "answer", None, 0.3)
        self.assertEqual(str(e.exception), SHARED_REFUSAL)
        self.b.reserve("m2", "answer", None, 0.15).release()

    def test_a_failure_before_anything_ran_gives_the_count_back(self):
        with self.assertRaises(RuntimeError):
            with self.b.action("m1", "item", "gh-300"):
                raise RuntimeError("the route failed")
        today = self.b._allowance("m1")["days"][budget_mod._day()]
        self.assertEqual((today["counts"].get("item", 0), today["open"]), (0, {}))
        self.assertEqual(self.b._cap()["open"], {})

    def test_old_days_are_pruned_and_the_cap_starts_each_day_empty(self):
        old = (utcnow() - timedelta(days=3)).date().isoformat()
        yesterday = (utcnow() - timedelta(days=1)).date().isoformat()
        self.b.store.write("learners", "m1", "allowance", value={"days": {old: {"counts": {"ask": 3}}, yesterday: {"counts": {}}}})
        self.b.store.write("team", "cap-today", value={"day": yesterday, "spent": 4.9, "open": {"c-1": 0.1}})
        self.b.prune(["m1"])
        self.assertEqual(set(self.b.store.read("learners", "m1", "allowance")["days"]), {yesterday})
        self.assertEqual(self.b.store.read("team", "cap-today"), {"day": budget_mod._day(), "spent": 0.0, "open": {}})
        self.b.reserve("m2", "answer", None, 4.9).release()   # yesterday's sum does not count today

    def test_the_meter_keeps_pooled_sums_under_the_five_member_rule(self):
        def spend(who, pid):
            hold = self.b.reserve(who, "answer", pid, 0.0)
            with spending(Spend(who, "answer", pid, hold)):
                self.b.settle(self.b.open_call("grader/grade", 0.01), 0.01)
            hold.release()

        spend("m1", "gh-300")
        spend("owner", "gh-300")
        self.assertEqual(set(self.b.totals()["today"]), {"total"})
        self.members = [f"m{i}" for i in range(1, 6)]
        self.enrolled = {"gh-300": 5, "dp-800": 4}
        spend("m1", "gh-300")
        spend("m2", "dp-800")
        spend("owner", "dp-800")
        self.b.settle(self.b.open_call("author/lesson", 0.02), 0.02)   # no learner: shared content work
        today = self.b.totals()["today"]
        self.assertEqual(set(today), {"total", "members", "exam:gh-300", "members_other", "owner", "content"})
        raw = (Path(self.tmp) / "team" / "meter.json").read_text("utf-8")
        for key in ("m1", "m2"):
            self.assertNotIn(f'"{key}"', raw)
        self.assertNotIn("dp-800", raw)   # fewer than 5 study it: folded into members_other at write time


# ---------------------------------------------------------------- review of #46: midnight, worst cases

class MidnightTests(unittest.TestCase):
    """A hold reserved before midnight is settled in its own day, never in the new day's sum."""

    def setUp(self):
        self.b = budget_on(tempfile.mkdtemp(prefix="dojo-t3-"))
        self.b.set_limits(None, 0.15)
        self.now = datetime(2026, 3, 1, 23, 59, tzinfo=timezone.utc)
        clock = mock.patch.object(budget_mod, "utcnow", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def test_a_hold_from_yesterday_never_lands_in_todays_sum(self):
        late = self.b.reserve("m1", "answer", "gh-300", 0.14)
        with spending(Spend("m1", "answer", "gh-300", late)):
            call = self.b.open_call("grader/grade", 0.14)
        self.now += timedelta(minutes=2)                       # midnight UTC: the new day starts clean
        fresh = self.b.reserve("m2", "answer", "gh-300", 0.14)
        self.b.settle(call, 0.14)
        late.release()
        cap = self.b._cap()
        self.assertEqual((cap["day"], cap["spent"]), ("2026-03-02", 0.0))
        self.assertAlmostEqual(self.b._cap_total(cap), 0.14)   # only today's reservation
        with self.assertRaises(Refused):                       # $0.14 + $0.02 is over the $0.15 cap
            self.b.reserve("m3", "answer", "gh-300", 0.02)
        fresh.release()
        self.assertEqual(self.b._cap(), {"day": "2026-03-02", "spent": 0.0, "open": {}})

    def test_what_a_late_hold_draws_after_midnight_is_checked_and_booked_today(self):
        late = self.b.reserve("m1", "answer", "gh-300", 0.10)
        self.now += timedelta(minutes=2)
        with spending(Spend("m1", "answer", "gh-300", late)):
            call = self.b.open_call("grader/grade", 0.14)          # $0.04 more than it set aside yesterday
        self.assertAlmostEqual(self.b._cap_total(self.b._cap()), 0.04)
        self.b.settle(call, 0.14)
        late.release()
        cap = self.b._cap()
        self.assertEqual(cap["open"], {})
        self.assertAlmostEqual(cap["spent"], 0.04)                 # yesterday's $0.10 stays yesterday's
        self.b.reserve("m2", "answer", "gh-300", 0.11).release()   # 0.04 + 0.11 fits the 0.15 cap
        with self.assertRaises(Refused):
            self.b.reserve("m2", "answer", "gh-300", 0.12)


class WorstCaseTests(unittest.TestCase):
    def setUp(self):
        self.b = budget_on(tempfile.mkdtemp(prefix="dojo-t3-"))
        self.b.set_limits(None, 100.0)
        grow = mock.patch.object(self.b, "_grow", side_effect=AssertionError("topped up"))
        self.grow = grow.start()
        self.addCleanup(grow.stop)

    def bound(self, role: str, task: str) -> float:
        """The largest bound a call has with an ordinary input: the pre-input assumed at the route."""
        return self.b.chat_cost(role, budget_mod.pre_input(role, task), max_out(role, task))

    def run_calls(self, calls) -> None:
        for role, task, n in calls:
            for _ in range(n):
                t = self.b.open_call(f"{role}/{task}", self.bound(role, task))
                self.b.settle(t, t.bound)

    def test_grading_reserves_both_grader_calls_twice_and_the_gate(self):
        self.assertEqual(budget_mod.ACTION_CALLS["answer"], (("grader", "grade", 4), ("gate", "gate_judgement", 2)))
        with self.b.action("m1", "answer", "gh-300"):
            # The grader, its retry, the second ask when quotes fail and its retry; the gate and its retry.
            self.run_calls(budget_mod.ACTION_CALLS["answer"])
        self.grow.assert_not_called()

    def test_a_lesson_and_an_item_run_a_whole_first_round_from_the_route(self):
        for action, round_ in (("lesson", budget_mod.LESSON_ROUND), ("item", budget_mod.ITEM_ROUND)):
            with self.b.action("m1", action, "gh-300"):
                with self.b.claim(round_, first=True) as go:
                    self.assertTrue(go)
                    self.run_calls(round_)
        self.grow.assert_not_called()

    def test_a_second_try_never_takes_the_money_of_a_question_still_to_write(self):
        with self.b.action("m1", "check", "gh-300", batches=2) as hold:
            with self.b.claim(budget_mod.ITEM_ROUND, first=True):
                self.run_calls(budget_mod.ITEM_ROUND[:1])        # question 1's author; its gate is not needed
            with self.b.claim(budget_mod.ITEM_ROUND) as again:   # question 1's second try: question 2 keeps its share
                self.assertFalse(again)
            with self.b.claim(budget_mod.ITEM_ROUND, first=True) as go:
                self.assertTrue(go)
                self.run_calls(budget_mod.ITEM_ROUND)
            self.assertAlmostEqual(hold.owed, 0.0)
        self.grow.assert_not_called()

    def test_a_writer_batch_that_does_not_fit_is_left_out_and_never_tops_up(self):
        one = self.b.calls_amount(budget_mod.writer_batch("single"))
        with self.b.action("m1", "exam", "gh-300", amount=one):
            with self.b.batch("single") as first:
                self.assertTrue(first)
                with self.b.batch("single") as second:
                    self.assertFalse(second)
                self.run_calls(budget_mod.writer_batch("single"))
            with self.b.batch("single") as third:       # what the first batch spent is gone
                self.assertFalse(third)
        self.grow.assert_not_called()

    def test_the_owner_is_never_left_out(self):
        with self.b.action("owner", "exam", "gh-300", amount=0.0):
            with self.b.batch("case") as go:
                self.assertTrue(go)

    def test_an_action_above_the_cap_is_refused_with_its_size(self):
        self.b.set_limits(None, 1.0)
        with self.assertRaises(Refused) as e:
            self.b.reserve("m1", "exam", "gh-300", 3.2)
        self.assertEqual(str(e.exception), budget_mod.too_large(3.2, 1.0))
        self.assertIn("$3.20", str(e.exception))
        self.assertEqual(self.b._cap()["open"], {})

    def test_grading_cut_short_says_the_answer_is_saved_and_waits(self):
        from app.learning import Dojo
        dojo = Dojo.__new__(Dojo)
        for why, said in ((SHARED_REFUSAL, budget_mod.GRADE_WAITS), (SHARED_BUSY, budget_mod.GRADE_BUSY)):
            with mock.patch.object(Dojo, "_grade", side_effect=Refused(why)):
                with self.assertRaises(Refused) as e:
                    dojo.grade(lambda step: None, None, "item-1")
            self.assertEqual(str(e.exception), said)

    def test_every_action_names_the_calls_it_really_makes(self):
        """The task names in ACTION_CALLS are the TASK names of the system prompts the workflows send."""
        from app import itemwriter, learning
        from app.ai import task_of
        self.assertEqual({task_of(s) for s in (learning.ITEM_SYSTEM, learning.GATE_ITEM_SYSTEM)},
                         {t for _, t, _ in budget_mod.ACTION_CALLS["check"]})
        self.assertEqual(budget_mod.ACTION_CALLS["check"], budget_mod.ACTION_CALLS["item"])
        self.assertEqual({t for _, t, _ in budget_mod.ACTION_CALLS["answer"]},
                         {task_of(learning.GRADE_SYSTEM), task_of(learning.GATE_JUDGEMENT_SYSTEM)})
        self.assertEqual({t for _, t, _ in budget_mod.ACTION_CALLS["lesson"]},
                         {task_of(learning.LESSON_SYSTEM), task_of(learning.GATE_ELEMENTS_SYSTEM)})
        self.assertEqual({t for _, t, _ in budget_mod.ACTION_CALLS["ask"]},
                         {task_of(learning.ANSWER_SYSTEM), task_of(learning.GATE_ELEMENTS_SYSTEM)})
        writers = {task_of(learning.MCQ_SYSTEM), task_of(learning.GATE_MCQ_SYSTEM), task_of(itemwriter.GATE_ITEMS_SYSTEM)}
        writers |= {task_of(s) for s in itemwriter.SYSTEMS.values()}
        self.assertEqual({t for pair in budget_mod.WRITER_TASKS.values() for t in pair}, writers)


# ---------------------------------------------------------------- the routes

class RouteTests(Team2Case):
    def setUp(self):
        super().setUp()
        self.a = self.admit("m-a", email="a@example.com")
        self.b = self.admit("m-b", email="b@example.com")
        self.ka, self.kb = self.key("m-a"), self.key("m-b")
        self.budget = self.app.state.budget
        self.stub = self.app.state.dojo.ai
        self.sid = self.skill()
        self.ok("PUT", "/api/settings", OWNER, {"later_hours": 30})

    def today(self, key: str) -> dict:
        return self.budget._allowance(key)["days"].get(budget_mod._day()) or {}

    def item(self, h: dict) -> str:
        job = self.wait_job(self.ok("POST", "/api/items", h, {"package": "gh-300", "skill": self.sid, "mode": "practice"}), h)
        self.assertEqual(job["state"], "done", job)
        return next(iter(ID.findall(json.dumps(job["result"]))))

    def test_the_wiring(self):
        self.assertTrue(self.budget.active)
        self.assertIs(self.stub.meter, self.budget)
        self.assertIs(self.app.state.dojo.speech.meter, self.budget)
        self.assertIs(self.app.state.pool.budget, self.budget)

    def test_every_paid_route_reserves_before_anything_happens(self):
        item = self.item(self.a)
        self.ok("POST", f"/api/items/{item}/answer", self.a, {"text": "An answer of mine.", "podcast_heard": False})
        self.wait_all(self.ka)
        lesson = self.app.state.dojo.make_lesson(lambda step: None, self.app.state.preparer.owner, "gh-300", self.sid)["lesson"]
        lesson = lesson if isinstance(lesson, dict) else {"id": lesson}
        p = self.ok("GET", "/api/packages/gh-300", OWNER)
        other = next(s["id"] for d in p["domains"] for g in d["groups"] for s in g["skills"] if s["id"] != self.sid)
        routes = [
            ("/api/lessons", {"package": "gh-300", "skill": other}),
            (f"/api/lessons/{lesson['id']}/audio", None),
            ("/api/items", {"package": "gh-300", "skill": self.sid, "mode": "practice"}),
            (f"/api/items/{item}/hint", None),
            (f"/api/items/{item}/answer", {"text": "Another answer.", "podcast_heard": False}),
            (f"/api/items/{item}/grade", None),
            (f"/api/items/{item}/judge-again", None),
            ("/api/rehearsals", {"package": "gh-300", "count": 4}),
            ("/api/ask", {"package": "gh-300", "skill": self.sid, "question": "Why is that?"}),
            ("/api/checks", {"package": "gh-300"}),
            ("/api/five/start", {}),
            ("/api/exams", {"package": "gh-300", "length": "short"}),
        ]
        before, calls = snapshot(self.folder(self.ka)), len(self.stub.calls)
        asked = []

        def refuse(learner, action, *a, **k):
            asked.append(action)
            raise Refused("No more today.")

        with mock.patch.object(self.budget, "reserve", refuse):
            for path, body in routes:
                r = self.call("POST", path, self.a, body)
                self.assertEqual((r.status_code, r.json()), (429, {"detail": "No more today."}), path)
            with mock.patch.object(self.app.state.dojo.speech, "transcribe") as heard:
                r = self.c.post("/api/transcribe", content=encode("webm", "libopus", 2),
                                headers={**self.a, **POST, "Content-Type": "audio/webm"})
                self.assertEqual(r.status_code, 429, r.text)
                heard.assert_not_called()
        self.assertEqual(asked, ["lesson", "audio", "item", "hint", "answer", "answer", "answer", "rehearsal", "ask", "check",
                                 "check", "exam", "transcribe"])
        self.wait_all(self.ka)
        self.assertEqual(len(self.stub.calls), calls)
        self.assertEqual(snapshot(self.folder(self.ka)), before)

    def test_at_the_allowance_an_answer_is_refused_and_nothing_is_recorded(self):
        self.ok("PUT", "/api/team/budget", OWNER, {"allowances": {"answer": 1}})
        item = self.item(self.a)
        self.ok("POST", f"/api/items/{item}/answer", self.a, {"text": "First answer.", "podcast_heard": False})
        self.wait_all(self.ka)
        before = snapshot(self.folder(self.ka))
        r = self.call("POST", f"/api/items/{item}/answer", self.a, {"text": "Second answer.", "podcast_heard": False})
        self.assertEqual((r.status_code, r.json()["detail"]), (429, personal_refusal("answer")))
        self.assertEqual(snapshot(self.folder(self.ka)), before)
        left = self.ok("GET", "/api/me/allowance", self.a)
        self.assertEqual(next(x for x in left["actions"] if x["action"] == "answer")["left"], 0)

    def test_each_learners_calls_land_on_their_own_allowance(self):
        self.item(self.a)   # the item writer's thread pool carries A's attribution into each thread
        self.wait_all(self.ka)
        spent_a = self.today(self.ka)["spent"]
        self.assertGreater(spent_a, 0)
        self.assertEqual(self.today(self.ka)["open"], {})
        self.assertFalse((self.folder(self.kb) / "allowance.json").exists())
        self.wait_job(self.ok("POST", "/api/ask", self.b, {"package": "gh-300", "skill": self.sid, "question": "Why?"}), self.b)
        self.wait_all(self.kb)
        self.assertGreater(self.today(self.kb)["spent"], 0)
        self.assertEqual(self.today(self.ka)["spent"], spent_a)

    def test_a_pool_batch_is_reserved_for_the_learner_whose_pool_it_fills(self):
        seen = []

        def top_up(learner, pid, **kw):
            seen.append((learner.key, current_spend()))
            self.stub.chat("author", "TASK: ping", "hello")
            return {"added": 1, "state": "done", "blocked": 0}

        pool, room = self.app.state.pool, self.app.state.exams
        with mock.patch.object(room, "top_up", top_up):
            for _ in range(2):
                pool._due.clear()
                pool.step()
        mine = [s for k, s in seen if k == self.ka]
        self.assertEqual(len(mine), 1, seen)
        spend = mine[0]
        self.assertEqual((spend.learner, spend.action), (self.ka, "pool"))
        self.assertIsNotNone(spend.hold)
        today = self.today(self.ka)
        self.assertGreater(today["spent"], 0)
        self.assertEqual(today["open"], {})
        self.assertNotIn("pool", today["counts"])   # counted by the pool's own batch counter (PR 2)

    def test_a_pool_batch_that_does_not_fit_the_cap_waits(self):
        self.ok("PUT", "/api/team/budget", OWNER, {"cap": 0})
        seen = []
        pool, room = self.app.state.pool, self.app.state.exams
        with mock.patch.object(room, "top_up", lambda learner, pid, **kw: seen.append(learner.key) or {"added": 0, "state": "done", "blocked": 0}):
            for _ in range(4):
                pool._due.clear()
                pool.step()
        self.assertIn(OWNER_KEY, seen)
        self.assertNotIn(self.ka, seen)   # a member's turn is refused before any call
        self.assertNotIn(self.kb, seen)
        self.assertNotIn("pool", self.today(self.ka).get("counts", {}))

    def test_a_pool_batch_refused_for_money_leaves_the_batch_counters_alone(self):
        self.ok("PUT", "/api/team/budget", OWNER, {"cap": 0})
        pool, room = self.app.state.pool, self.app.state.exams
        member = Learner("", self.ka, "")
        with mock.patch.object(room, "top_up", lambda learner, pid, **kw: {"added": 0, "state": "done", "blocked": 0}):
            for _ in range(4):
                pool._due.clear()
                pool.step()
        self.assertEqual(pool._budget(member)["batches"], 0)
        self.assertEqual(pool._members_budget()["batches"], 0)
        self.assertEqual(self.budget._cap()["open"], {})

    def test_a_pool_batch_the_counters_refuse_gives_its_reservation_back(self):
        pool = self.app.state.pool
        member = Learner("", self.ka, "")
        with mock.patch.object(pool, "_charge", return_value="the daily limit is reached"):
            refused, hold = pool._pay(member, "gh-300", "single")
        self.assertEqual((refused, hold), ("the daily limit is reached", None))
        self.assertEqual(self.budget._cap()["open"], {})
        self.assertEqual(self.today(self.ka).get("open"), {})

    def test_a_practice_exam_reserves_its_whole_first_round_before_the_attempt_exists(self):
        room = self.app.state.exams
        member = Learner("", self.ka, "")
        first = room.first_round(member, "gh-300", "short", "single")
        wanted = exam_mod.spec(self.app.state.dojo.packages.get("gh-300"), "short")["questions"]
        batch = budget_mod.writer_batch("single")
        self.assertGreaterEqual(wanted, 15)
        self.assertEqual(len(first), len(batch) * math.ceil(wanted / 4))   # 15 questions: 4 batches, not 1
        seen = []
        real = self.budget.reserve

        def reserve(learner, action, package=None, amount=0.0, count=True):
            if action == "exam":
                seen.append((amount, list((self.folder(self.ka) / "exams").glob("*.json"))))
            return real(learner, action, package, amount, count)

        with mock.patch.object(self.budget, "reserve", reserve):
            self.ok("POST", "/api/exams", self.a, {"package": "gh-300", "length": "short"})
        self.wait_all(self.ka)
        (amount, attempts), = seen
        self.assertEqual(attempts, [])                     # reserved before the attempt was written
        self.assertAlmostEqual(amount, self.budget.calls_amount(first))
        self.assertGreaterEqual(amount, 4 * self.budget.calls_amount(batch) - 1e-6)

    def test_spend_keeps_the_exam_it_was_for(self):
        item = self.item(self.a)
        seen = []
        real = self.budget.reserve

        def reserve(learner, action, package=None, amount=0.0, count=True):
            seen.append((action, package))
            return real(learner, action, package, amount, count)

        metered = []
        real_meter = self.budget._meter
        with mock.patch.object(self.budget, "reserve", reserve), \
                mock.patch.object(self.budget, "_meter", lambda *a: metered.append(a) or real_meter(*a)):
            self.ok("POST", f"/api/items/{item}/hint", self.a)
            self.ok("POST", f"/api/items/{item}/answer", self.a, {"text": "An answer of mine.", "podcast_heard": False})
            self.wait_all(self.ka)
            self.call("POST", "/api/five/start", self.a, {})
            self.wait_all(self.ka)
        self.assertIn(("answer", "gh-300"), seen)
        self.assertIn(("hint", "gh-300"), seen)
        self.assertIn(("check", "gh-300"), seen)
        self.assertNotIn(None, {p for a, p in seen})
        self.assertTrue(metered)
        self.assertTrue(all(m[1] == "gh-300" for m in metered if m[0] == self.ka), metered)

    def test_a_job_carries_the_exam_of_its_action(self):
        member = Learner("", self.ka, "")
        with spending(Spend(self.ka, "ask", "gh-300", None)):
            job = self.app.state.dojo.jobs.start(member, "ask", lambda progress: {"spend": current_spend().package})
        self.assertEqual(self.wait_job(job, self.a)["result"], {"spend": "gh-300"})

    def test_a_job_that_fails_before_any_call_gives_the_count_back(self):
        dojo = self.app.state.dojo
        with mock.patch.object(type(dojo), "make_item", side_effect=UserError("No page to write from.")):
            job = self.wait_job(self.ok("POST", "/api/items", self.a, {"package": "gh-300", "skill": self.sid, "mode": "practice"}), self.a)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.today(self.ka)["counts"].get("item", 0), 0)
        self.assertEqual(self.today(self.ka)["open"], {})
        self.item(self.a)                                   # one that ran keeps its count
        self.assertEqual(self.today(self.ka)["counts"]["item"], 1)

    def test_a_saved_answer_says_plainly_why_judging_waits(self):
        dojo = self.app.state.dojo
        item = self.item(self.a)
        with mock.patch.object(type(dojo), "_grade", side_effect=Refused(SHARED_REFUSAL)):
            job = self.wait_job(self.ok("POST", f"/api/items/{item}/answer", self.a, {"text": "Mine.", "podcast_heard": False}), self.a)
        self.assertEqual((job["state"], job["error"]), ("failed", budget_mod.GRADE_WAITS))
        view = self.ok("GET", f"/api/items/{item}", self.a)
        self.assertIsNone(view["result"])
        self.assertIsNone(view["grade_waits"])                     # nothing holds it now: the button is open
        cap = self.budget._cap()
        cap["spent"] = CAP_USD
        self.budget.store.write("team", "cap-today", value=cap)
        self.assertEqual(self.ok("GET", f"/api/items/{item}", self.a)["grade_waits"], budget_mod.GRADE_WAITS)
        self.ok("PUT", "/api/team/budget", OWNER, {"cap": CAP_USD + 50})   # the owner raised the cap: open again
        self.assertIsNone(self.ok("GET", f"/api/items/{item}", self.a)["grade_waits"])
        # The failed judging ran no paid call, so its count came back; at an allowance of 0 it is used up.
        self.assertEqual(self.today(self.ka)["counts"].get("answer", 0), 0)
        self.ok("PUT", "/api/team/budget", OWNER, {"allowances": {"answer": 0}})
        self.assertEqual(self.ok("GET", f"/api/items/{item}", self.a)["grade_waits"], budget_mod.GRADE_WAITS_OWN)

    def test_each_action_makes_only_the_calls_it_reserved(self):
        made: dict[str, list[str]] = {}
        real = self.budget.open_call

        def open_call(kind, bound, bounded=True):
            spend = current_spend()
            hold = spend.hold if spend else None
            made.setdefault(hold.action if hold else "none", []).append(kind)
            return real(kind, bound, bounded)

        item = self.item(self.a)
        p = self.ok("GET", "/api/packages/gh-300", OWNER)
        other = next(s["id"] for d in p["domains"] for g in d["groups"] for s in g["skills"] if s["id"] != self.sid)
        with mock.patch.object(self.budget, "open_call", open_call):
            self.wait_job(self.ok("POST", "/api/lessons", self.a, {"package": "gh-300", "skill": other}), self.a)
            self.item(self.a)
            self.ok("POST", f"/api/items/{item}/answer", self.a, {"text": "An answer of mine.", "podcast_heard": False})
            self.wait_job(self.ok("POST", "/api/ask", self.a, {"package": "gh-300", "skill": self.sid, "question": "Why?"}), self.a)
            self.wait_job(self.ok("POST", "/api/rehearsals", self.a, {"package": "gh-300", "count": 4}), self.a)
            self.ok("POST", "/api/checks", self.a, {"package": "gh-300"})
            self.wait_all(self.ka)
            self.ok("POST", "/api/exams", self.a, {"package": "gh-300", "length": "short"})
            self.wait_all(self.ka)
        writers = {f"{r}/{t}" for kind in budget_mod.WRITER_TASKS for r, t, _ in budget_mod.writer_batch(kind)}
        for action in ("lesson", "item", "answer", "ask", "rehearsal", "check", "exam"):
            self.assertIn(action, made)
            allowed = writers if action in ("rehearsal", "exam") else {f"{r}/{t}" for r, t, _ in budget_mod.ACTION_CALLS[action]}
            if action in budget_mod.ACTION_CHARACTERS:   # spoken text, reserved by characters
                allowed = allowed | {"speech/tts"}
            self.assertLessEqual(set(made[action]), allowed, action)

    def test_the_cap_refuses_members_and_not_the_owner(self):
        self.ok("PUT", "/api/team/budget", OWNER, {"cap": 0})
        r = self.call("POST", "/api/ask", self.a, {"package": "gh-300", "skill": self.sid, "question": "Why?"})
        self.assertEqual((r.status_code, r.json()["detail"]), (429, SHARED_REFUSAL))
        job = self.wait_job(self.ok("POST", "/api/ask", OWNER, {"package": "gh-300", "skill": self.sid, "question": "Why?"}), OWNER)
        self.assertEqual(job["state"], "done", job)
        self.assertGreater(self.today(OWNER_KEY)["spent"], 0)

    def test_the_cap_survives_leave(self):
        self.wait_job(self.ok("POST", "/api/ask", self.a, {"package": "gh-300", "skill": self.sid, "question": "Why?"}), self.a)
        self.wait_all(self.ka)
        cap = self.budget._cap()["spent"]
        self.assertGreater(cap, 0)
        self.ok("POST", "/api/team/leave", self.a, {"confirm": "LEAVE"})
        self.assertFalse(self.folder(self.ka).exists())
        self.assertEqual(self.budget._cap()["spent"], cap)
        self.assertNotIn(self.ka, (Path(self.tmp) / "team" / "cap-today.json").read_text("utf-8"))

    def test_an_open_check_asked_for_again_is_not_counted_twice(self):
        first = self.ok("POST", "/api/checks", self.a, {"package": "gh-300"})
        again = self.ok("POST", "/api/checks", self.a, {"package": "gh-300"})
        self.assertEqual(first["id"], again["id"])
        self.wait_all(self.ka)
        self.assertEqual(self.today(self.ka)["counts"]["check"], 1)

    def test_list_prices_come_from_app_costs(self):
        budget = self.app.state.budget
        doc = budget.costs._nothing()
        doc["fetched"] = utcnow().isoformat()
        model = budget.costs.model_price(doc, "author")["model"].lower()
        doc["models"][model][budget.costs.deployment] = {"product": "p", "input": {"per_token": 2e-6},
                                                         "output": {"per_token": 9e-6}}
        doc["meters"]["voice"] = {"per_character": 1.5e-5}
        budget.store.write("content", "prices", value=doc)
        budget._prices = None
        self.assertEqual(budget.rates("author"), (2e-6, 9e-6))
        self.assertAlmostEqual(budget.chat_cost("author", 1000, 100), 1000 * 2e-6 + 100 * 9e-6)
        self.assertEqual(budget.meter_rate("voice"), 1.5e-5)
        self.assertEqual(budget.rates("grader"), (STUB["input"], STUB["output"]))   # no price: the stub's own

    def test_the_budget_routes(self):
        self.refused("GET", "/api/team/budget", self.a, "admin_only")
        self.refused("PUT", "/api/team/budget", self.a, "admin_only", {"cap": 100})
        view = self.ok("GET", "/api/team/budget", OWNER)
        self.assertEqual(view["cap_usd"], CAP_USD)
        self.assertEqual({x["action"] for x in view["allowances"]}, set(budget_mod.ACTIONS))
        self.ok("PUT", "/api/team/budget", OWNER, {"allowances": {"ask": -1}}, status=422)
        self.ok("PUT", "/api/team/budget", OWNER, {"cap": 9999}, status=422)
        self.ok("PUT", "/api/team/budget", OWNER, {"allowances": {"nope": 1}}, status=422)
        self.assertEqual(self.ok("PUT", "/api/team/budget", OWNER, {"cap": 7.5})["cap_usd"], 7.5)
        mine = self.ok("GET", "/api/me/allowance", self.a)
        self.assertEqual((mine["team"], mine["owner"]), (True, False))
        self.assertTrue(self.ok("GET", "/api/me/allowance", OWNER)["owner"])

    def test_totals_only_while_fewer_than_five(self):
        self.wait_job(self.ok("POST", "/api/ask", self.a, {"package": "gh-300", "skill": self.sid, "question": "Why?"}), self.a)
        self.wait_all(self.ka)
        totals = self.ok("GET", "/api/team/budget", OWNER)["totals"]
        self.assertEqual(set(totals["today"]), {"total"})
        self.assertEqual(set(totals["month"]), {"total"})
        self.assertIn("budget_flags", self.ok("GET", "/api/studio/costs", OWNER))


# ---------------------------------------------------------------- the audio bound

class NormalizeTests(unittest.TestCase):
    def check(self, wav: bytes, seconds: float) -> None:
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(seconds, (len(wav) - WAV_HEADER) / (NORMALIZE_RATE * 2))
        with av.open(io.BytesIO(wav), format="wav") as c:
            s = c.streams.audio[0]
            self.assertEqual((s.codec_context.sample_rate, s.codec_context.channels), (NORMALIZE_RATE, 1))

    def test_every_accepted_format_is_written_again_by_dojo(self):
        for fmt, codec in (("webm", "libopus"), ("ogg", "libopus"), ("mp4", "aac"), ("mp3", "libmp3lame"), ("wav", "pcm_s16le")):
            wav, seconds = normalize(encode(fmt, codec, 5), TYPES[fmt])
            self.check(wav, seconds)
            self.assertAlmostEqual(seconds, 5.0, delta=0.1, msg=fmt)

    def test_a_lying_wav_header_does_not_change_the_bound(self):
        w = bytearray(encode("wav", "pcm_s16le", 5))
        i = w.find(b"fmt ")
        struct.pack_into("<I", w, i + 16, 1000)   # the byte rate claims 0.006 s a second
        wav, seconds = normalize(bytes(w), "audio/wav")
        self.check(wav, seconds)
        self.assertAlmostEqual(seconds, 5.0, delta=0.01)
        # A sample rate that lies plays the samples faster; what Dojo sends is what it counted, either way.
        struct.pack_into("<I", w, i + 12, 128000)
        struct.pack_into("<I", w, i + 16, 256000)
        wav, seconds = normalize(bytes(w), "audio/wav")
        self.check(wav, seconds)

    def test_container_durations_that_lie_are_not_read(self):
        webm = bytearray(encode("webm", "libopus", 5))
        j = webm.find(b"\x44\x89\x88")
        struct.pack_into(">d", webm, j + 3, 1.0)   # Segment Duration: 1 ms
        wav, seconds = normalize(bytes(webm), "audio/webm")
        self.check(wav, seconds)
        self.assertAlmostEqual(seconds, 5.0, delta=0.1)
        mp4 = bytearray(encode("mp4", "aac", 5))
        for box in (b"mvhd", b"mdhd", b"tkhd"):
            k = mp4.find(box)
            offset = {b"mvhd": 16, b"mdhd": 16, b"tkhd": 20}[box]
            struct.pack_into(">I", mp4, k + offset, 1)
        wav, seconds = normalize(bytes(mp4), "audio/mp4")
        self.check(wav, seconds)   # MP4 durations cut what is decoded, never extend it: Dojo sends what it counted
        self.assertLessEqual(seconds, 5.1)

    def test_over_two_hundred_seconds_is_413(self):
        with self.assertRaises(AudioRefused) as e:
            normalize(encode("ogg", "libopus", MAX_AUDIO_SECONDS + 100), "audio/ogg")
        self.assertEqual(e.exception.status, 413)

    def test_long_recordings_whose_fields_say_short_are_413(self):
        out = io.BytesIO()   # 230 s of 8-bit 8 kHz PCM fits the 2 MB upload; its header says 23 s
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(8000)
            w.writeframes(bytes(128 + (i * 7) % 64 for i in range(8000)) * 230)
        lying = bytearray(out.getvalue())
        struct.pack_into("<I", lying, lying.find(b"fmt ") + 16, 80000)
        webm = bytearray(encode("webm", "libopus", MAX_AUDIO_SECONDS + 100))
        struct.pack_into(">d", webm, webm.find(b"\x44\x89\x88") + 3, 5000.0)   # Segment Duration: 5 s
        for name, data, ct in (("wav", bytes(lying), "audio/wav"), ("webm", bytes(webm), "audio/webm")):
            self.assertLess(len(data), 2 * 1024 * 1024, name)
            with self.assertRaises(AudioRefused, msg=name) as e:
                normalize(data, ct)
            self.assertEqual(e.exception.status, 413, name)

    def test_what_cannot_be_decoded_is_415(self):
        webm = encode("webm", "libopus", 3)
        cases = {
            "truncated": (webm[:64], "audio/webm"),
            "random": (os.urandom(4096), "audio/ogg"),
            "empty": (b"", "audio/webm"),
            "video": (encode("mp4", "aac", 3, video=True), "audio/mp4"),
            "flac": (encode("ogg", "flac", 3), "audio/ogg"),
            "alac": (encode("mp4", "alac", 3), "audio/mp4"),
            "other type": (webm, "audio/flac"),
        }
        for name, (data, ct) in cases.items():
            with self.assertRaises(AudioRefused, msg=name) as e:
                normalize(data, ct)
            self.assertEqual(e.exception.status, 415, name)

    def test_two_hundred_seconds_decode_quickly(self):
        data = encode("webm", "libopus", MAX_AUDIO_SECONDS - 1)
        started = time.monotonic()
        wav, seconds = normalize(data, "audio/webm")
        took = time.monotonic() - started
        self.assertAlmostEqual(seconds, MAX_AUDIO_SECONDS - 1, delta=0.1)
        self.assertLess(took, 3.0)   # about 1 s on a laptop; the B1 plan has one core


class TranscribeRouteTests(Team2Case):
    def setUp(self):
        super().setUp()
        self.a = self.admit("m-a")
        self.ka = self.key("m-a")
        self.speech = self.app.state.dojo.speech

    def post(self, data: bytes, ct: str):
        return self.c.post("/api/transcribe", content=data, headers={**self.a, **POST, "Content-Type": ct})

    def test_speech_gets_dojos_own_wav_and_the_reservation_is_for_its_length(self):
        reserved = []
        real = self.app.state.budget.reserve
        with mock.patch.object(self.speech, "transcribe", return_value={"text": "hello", "seconds": 3.0}) as heard, \
                mock.patch.object(self.app.state.budget, "reserve", lambda *a, **k: reserved.append((a, k)) or real(*a, **k)):
            r = self.post(encode("webm", "libopus", 3), "audio/webm")
        self.assertEqual(r.status_code, 200, r.text)
        (wav, ct), kw = heard.call_args
        self.assertEqual((wav[:4], ct), (b"RIFF", "audio/wav"))
        seconds = (len(wav) - WAV_HEADER) / (NORMALIZE_RATE * 2)
        self.assertEqual(kw["seconds"], seconds)
        rate = self.app.state.budget.meter_rate("transcription")
        self.assertEqual(reserved[0][0][:2], (self.ka, "transcribe"))
        self.assertAlmostEqual(reserved[0][0][3], (seconds + 1.0) * rate)

    def test_refused_recordings_reserve_nothing_and_never_reach_speech(self):
        with mock.patch.object(self.speech, "transcribe") as heard, \
                mock.patch.object(self.app.state.budget, "reserve") as reserve:
            for data, ct, status in ((encode("ogg", "libopus", 300), "audio/ogg", 413),
                                     (os.urandom(3000), "audio/webm", 415),
                                     (encode("ogg", "flac", 2), "audio/ogg", 415),
                                     (b"\x1aE\xdf\xa3" + bytes(2 * 1024 * 1024), "audio/webm", 413)):
                self.assertEqual(self.post(data, ct).status_code, status)
        heard.assert_not_called()
        reserve.assert_not_called()


# ---------------------------------------------------------------- team mode off: the owner as today

class TeamOffTests(Team2Case):
    team_on = False

    def test_nothing_is_reserved_metered_or_changed(self):
        dojo = self.app.state.dojo
        self.assertFalse(self.app.state.budget.active)
        self.assertIsNone(dojo.ai.meter)
        self.assertIsNone(dojo.speech.meter)
        self.assertIsNone(getattr(dojo.avatar, "meter", None))
        sid = self.skill()
        job = self.wait_job(self.ok("POST", "/api/items", OWNER, {"package": "gh-300", "skill": sid, "mode": "practice"}), OWNER)
        self.assertEqual(job["state"], "done", job)
        self.wait_job(self.ok("POST", "/api/ask", OWNER, {"package": "gh-300", "skill": sid, "question": "Why?"}), OWNER)
        self.ok("POST", "/api/checks", OWNER, {"package": "gh-300"})
        self.wait_all(OWNER_KEY)
        self.assertFalse((self.folder(OWNER_KEY) / "allowance.json").exists())
        for name in ("cap-today.json", "meter.json", "budget.json", "budget-flags.json"):
            self.assertFalse((Path(self.tmp) / "team" / name).exists(), name)
        self.assertNotIn("budget_flags", self.ok("GET", "/api/studio/costs", OWNER))

    def test_the_owners_recording_goes_to_speech_as_it_came(self):
        raw = b"\x1aE\xdf\xa3" + bytes(400)
        with mock.patch.object(self.app.state.dojo.speech, "transcribe", return_value={"text": "hi", "seconds": 1.0}) as heard, \
                mock.patch("app.main.normalize") as decode:
            r = self.c.post("/api/transcribe", content=raw, headers={**OWNER, **POST, "Content-Type": "audio/webm"})
        self.assertEqual(r.status_code, 200, r.text)
        heard.assert_called_once_with(raw, "audio/webm")
        decode.assert_not_called()

    def test_the_owner_has_no_allowance_view_to_change_anything(self):
        mine = self.ok("GET", "/api/me/allowance", OWNER)
        self.assertEqual((mine["team"], mine["owner"]), (False, True))
        self.assertFalse((self.folder(OWNER_KEY) / "allowance.json").exists())


if __name__ == "__main__":
    unittest.main()
