"""Web search through Foundry's Responses API (app/ai.py Foundry.web_search, ADR 0018, R4): the request, what is kept of
the answer (addresses only), waiting out a rate limit, one search at a time, and the money reserved and settled in team
mode. The service is a mock; no model is called."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")

import httpx  # noqa: E402

from app import ai as ai_mod  # noqa: E402
from app.ai import WEB_MAX_OUT, WEB_PRE_INPUT, WEB_SEARCH_FEE, WEB_TRIES, AIError, Foundry, StubAI  # noqa: E402
from app.budget import Budget  # noqa: E402
from app.core import Spend, Store, spending  # noqa: E402

LEARN = "https://learn.microsoft.com/en-us/"


def answer(sources=(), cited=(), searches=1, usage=None, status="completed") -> httpx.Response:
    output = []
    for n in range(searches):
        output.append({"type": "web_search_call", "status": "completed",
                       "action": {"type": "search", "queries": [f"site:learn.microsoft.com query {n}"],
                                  "sources": [{"type": "url", "url": u} for u in (sources if n == 0 else ())]}})
    output.append({"type": "message", "content": [{"type": "output_text", "text": "done",
                                                   "annotations": [{"type": "url_citation", "url": u, "title": "t"} for u in cited]}]})
    body = {"status": status, "output": output}
    if usage is not False:
        body["usage"] = usage or {"input_tokens": 10000, "output_tokens": 150, "total_tokens": 10150}
    return httpx.Response(200, json=body)


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.bodies: list[dict] = []
        self.urls: list[str] = []
        self.replies: list = []
        settings = SimpleNamespace(ai_endpoint="https://x.services.ai.azure.com/openai/v1")
        self.tokens_asked: list[str] = []

        def token(scope):
            self.tokens_asked.append(scope)
            return "token"

        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        self.f = Foundry(settings, SimpleNamespace(get=token), http=http)
        self.naps: list[float] = []
        nap = mock.patch.object(ai_mod.time, "sleep", lambda s: self.naps.append(s))
        nap.start()
        self.addCleanup(nap.stop)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.bodies.append(json.loads(request.content))
        return self.replies.pop(0) if self.replies else answer([LEARN + "azure/azure-sql/database/ledger-overview"])

    def test_the_request_asks_for_one_search_of_one_domain_and_the_sources_it_read(self):
        self.replies = [answer(sources=[LEARN + "a?view=azuresql", LEARN + "b"], cited=[LEARN + "b", LEARN + "c"], searches=2)]
        found = self.f.web_search("XY-110 exam. Skill: Configure widget alerts", domains=("learn.microsoft.com",), max_searches=2)
        body = self.bodies[0]
        self.assertEqual(self.urls, ["https://x.services.ai.azure.com/openai/v1/responses"])
        self.assertEqual((body["model"], body["tool_choice"], body["max_tool_calls"], body["max_output_tokens"]),
                         ("author", "required", 2, WEB_MAX_OUT))
        self.assertEqual(body["tools"], [{"type": "web_search", "filters": {"allowed_domains": ["learn.microsoft.com"]}}])
        self.assertEqual(body["include"], ["web_search_call.action.sources"])
        self.assertIn("Skill: Configure widget alerts", body["input"])
        self.assertIn("exactly 2 web searches", body["input"])
        self.assertEqual(set(body), {"model", "tools", "tool_choice", "include", "max_tool_calls", "max_output_tokens", "input"})
        self.assertEqual(found["urls"], [LEARN + "a?view=azuresql", LEARN + "b", LEARN + "c"], "sources first, then citations, no repeats")
        self.assertEqual(found["queries"], ["site:learn.microsoft.com query 0", "site:learn.microsoft.com query 1"])
        self.assertEqual(set(found), {"urls", "queries"}, "only addresses leave: the model's words are dropped")

    def test_without_a_domain_the_search_is_not_narrowed(self):
        self.f.web_search("a skill")
        self.assertEqual(self.bodies[0]["tools"], [{"type": "web_search"}])
        self.assertIn("exactly 1 web search,", self.bodies[0]["input"])

    def test_a_search_that_found_nothing_is_an_answer_not_an_error(self):
        self.replies = [answer()]
        self.assertEqual(self.f.web_search("a skill")["urls"], [])

    def test_a_rate_limit_is_waited_out(self):
        self.replies = [httpx.Response(429, headers={"retry-after": "17"}, json={"error": {"code": "rate_limit_exceeded"}}),
                        answer([LEARN + "a"])]
        self.assertEqual(self.f.web_search("a skill")["urls"], [LEARN + "a"])
        self.assertEqual(len(self.bodies), 2)
        self.assertEqual(self.naps, [17.0])

    def test_the_wait_has_a_ceiling_and_the_tries_end(self):
        self.replies = [httpx.Response(429, headers={"retry-after": "9999"}) for _ in range(WEB_TRIES)]
        with self.assertRaises(AIError) as raised:
            self.f.web_search("a skill")
        self.assertIn("429", str(raised.exception))
        self.assertEqual(len(self.bodies), WEB_TRIES * 1)
        self.assertTrue(all(n <= ai_mod.WEB_WAIT_S for n in self.naps))

    def test_a_server_error_is_tried_again_and_a_refusal_is_not(self):
        self.replies = [httpx.Response(503), answer([LEARN + "a"])]
        self.assertEqual(self.f.web_search("a skill")["urls"], [LEARN + "a"])
        self.replies = [httpx.Response(400, json={"error": {"message": "bad request"}})]
        before = len(self.bodies)
        with self.assertRaises(AIError) as raised:
            self.f.web_search("a skill")
        self.assertIn("refused", str(raised.exception))
        self.assertEqual(len(self.bodies), before + 1)

    def test_an_answer_that_failed_is_an_error(self):
        self.replies = [answer(status="failed")] * WEB_TRIES
        with self.assertRaises(AIError):
            self.f.web_search("a skill")

    def test_no_endpoint_is_an_error(self):
        self.f.settings = SimpleNamespace(ai_endpoint="")
        with self.assertRaises(AIError):
            self.f.web_search("a skill")

    def test_two_searches_never_run_at_once(self):
        running, most = [0], [0]
        lock = threading.Lock()

        def slow(request: httpx.Request) -> httpx.Response:
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return answer([LEARN + "a"])

        self.f.http = httpx.Client(transport=httpx.MockTransport(slow))
        threads = [threading.Thread(target=self.f.web_search, args=("a skill",)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(most[0], 1)


class MeteredWebSearchTests(unittest.TestCase):
    """Team mode: every paid call is reserved before it is sent and settled after (ADR 0009)."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="dojo-websearch-")
        self.budget = Budget(SimpleNamespace(team=True, local=False, real_ai=False), Store(Path(tmp)), "owner", None)
        self.replies: list = []
        settings = SimpleNamespace(ai_endpoint="https://x.services.ai.azure.com/openai/v1")
        http = httpx.Client(transport=httpx.MockTransport(lambda r: self.replies.pop(0)))
        self.f = Foundry(settings, SimpleNamespace(get=lambda scope: "token"), http=http)
        self.f.meter = self.budget
        nap = mock.patch.object(ai_mod.time, "sleep", lambda s: None)
        nap.start()
        self.addCleanup(nap.stop)
        self.bounds: list[float] = []
        real = self.budget.open_call

        def keep(kind, bound, *a, **k):
            self.bounds.append(bound)
            return real(kind, bound, *a, **k)

        mock.patch.object(self.budget, "open_call", keep).start()
        self.addCleanup(mock.patch.stopall)

    def spent(self) -> float:
        return self.budget._allowance("m1")["days"][__import__("app.budget", fromlist=["_day"])._day()]["spent"]

    def run_call(self, **kw):
        hold = self.budget.reserve("m1", "answer", None, 1.0)
        with spending(Spend("m1", "answer", None, hold)):
            out = self.f.web_search("a skill", **kw)
        hold.release()
        return out

    def test_the_call_reserves_its_worst_case_and_settles_from_its_usage_and_searches(self):
        self.replies = [answer([LEARN + "a"], searches=2, usage={"input_tokens": 12000, "output_tokens": 300, "total_tokens": 12300})]
        self.run_call(max_searches=2)
        self.assertAlmostEqual(self.bounds[0], self.budget.chat_cost("author", WEB_PRE_INPUT, WEB_MAX_OUT) + 2 * WEB_SEARCH_FEE, places=6)
        self.assertAlmostEqual(self.spent(), self.budget.chat_cost("author", 12000, 300) + 2 * WEB_SEARCH_FEE, places=6)

    def test_a_call_without_usage_costs_its_whole_bound(self):
        self.replies = [answer([LEARN + "a"], usage=False)]
        self.run_call()
        self.assertAlmostEqual(self.spent(), self.bounds[0], places=6)

    def test_a_rate_limited_call_costs_nothing_and_each_try_is_reserved(self):
        self.replies = [httpx.Response(429), httpx.Response(429), answer([LEARN + "a"])]
        self.run_call()
        self.assertEqual(len(self.bounds), 3)
        self.assertAlmostEqual(self.spent(), self.budget.chat_cost("author", 10000, 150) + WEB_SEARCH_FEE, places=6)

    def test_the_stand_in_reserves_and_settles_too(self):
        stub = StubAI()
        stub.meter = self.budget
        stub.search_urls = lambda q: [LEARN + "a"]
        hold = self.budget.reserve("m1", "answer", None, 1.0)
        with spending(Spend("m1", "answer", None, hold)):
            found = stub.web_search("a skill", domains=("learn.microsoft.com",))
        hold.release()
        self.assertEqual((found["urls"], stub.calls), ([LEARN + "a"], [("author", "web_search")]))
        self.assertEqual(len(self.bounds), 1)
        self.assertAlmostEqual(self.spent(), 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
