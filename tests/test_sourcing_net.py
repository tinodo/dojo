import tempfile
import unittest
from pathlib import Path

import httpx

from app.sourcing.net import Net, SourcingError
from app.sources import NotDocs, Sources
from app.core import Store


def response(url: str, status: int, *, text: str = "", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers, request=httpx.Request("GET", url))


class FakeSources:
    def __init__(self):
        self.data_steps = {}
        self.page_steps = {}
        self.post_steps = {}
        self.calls = []

    def data(self, url: str, docs: bool = False):
        self.calls.append(("data", url))
        step = self.data_steps[url].pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def page(self, url: str, docs: bool = False):
        self.calls.append(("page", url))
        step = self.page_steps[url].pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def post(self, url: str, body, headers=None):
        self.calls.append(("post", url, body, headers or {}))
        step = self.post_steps[url].pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.now += seconds


class NetTests(unittest.TestCase):
    def test_token_bucket_waits_per_host(self):
        src = FakeSources()
        url = "https://learn.microsoft.com/en-us/sql/item"
        src.data_steps[url] = [{"n": 1}, {"n": 2}, {"n": 3}]
        clock = Clock()
        net = Net(src, per_second=2.0, tries=1, sleep=clock.sleep, clock=clock)
        self.assertEqual(net.get_json(url)["n"], 1)
        self.assertEqual(net.get_json(url)["n"], 2)
        self.assertEqual(net.get_json(url)["n"], 3)
        self.assertEqual(clock.sleeps, [0.5])

    def test_retry_after_header_is_used(self):
        src = FakeSources()
        url = "https://learn.microsoft.com/en-us/sql/item"
        err = httpx.HTTPStatusError("slow", request=httpx.Request("GET", url), response=response(url, 429, headers={"retry-after": "7"}))
        src.data_steps[url] = [err, {"ok": True}]
        clock = Clock()
        net = Net(src, tries=2, sleep=clock.sleep, clock=clock)
        self.assertEqual(net.get_json(url), {"ok": True})
        self.assertEqual(clock.sleeps, [7.0])

    def test_retries_transport_and_stops_on_permanent_error(self):
        src = FakeSources()
        url = "https://learn.microsoft.com/en-us/sql/item"
        req = httpx.Request("GET", url)
        src.data_steps[url] = [
            httpx.ConnectError("down", request=req),
            httpx.HTTPStatusError("bad", request=req, response=response(url, 500)),
            {"ok": True},
        ]
        clock = Clock()
        net = Net(src, tries=3, sleep=clock.sleep, clock=clock)
        self.assertEqual(net.get_json(url), {"ok": True})
        self.assertEqual(clock.sleeps, [2.0, 4.0])
        src.data_steps[url] = [httpx.HTTPStatusError("gone", request=req, response=response(url, 404))]
        with self.assertRaises(SourcingError) as got:
            net.get_json(url)
        self.assertEqual(got.exception.status, 404)

    def test_host_allow_list_is_enforced(self):
        net = Net(FakeSources())
        with self.assertRaises(SourcingError):
            net.get_json("https://evil.example/steal")

    def test_a_documentation_only_read_never_follows_a_redirect_into_training(self):
        seen = []
        toc = "https://learn.microsoft.com/en-us/sql/toc.json"

        def web(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/en-us/sql/toc.json":
                return httpx.Response(302, headers={"location": "/en-us/training/paths/toc.json"}, request=request)
            return httpx.Response(200, json={"items": []}, request=request)

        store = Store(Path(tempfile.mkdtemp(prefix="dojo-sourcing-net-")))
        src = Sources(store, http=httpx.Client(transport=httpx.MockTransport(web), follow_redirects=False))
        net = Net(src, per_second=1000.0, sleep=lambda s: None)
        with self.assertRaises(SourcingError):
            net.get_json(toc, docs=True)
        self.assertEqual(seen, ["/en-us/sql/toc.json"], "training was never asked for")
        with self.assertRaises(NotDocs):
            src.data(toc, docs=True)
        seen.clear()
        self.assertEqual(src.data(toc), {"items": []}, "a search is not a page: no documentation check")
        self.assertEqual(seen, ["/en-us/sql/toc.json", "/en-us/training/paths/toc.json"])

    def test_post_json_keeps_redirect_response(self):
        src = FakeSources()
        url = "https://learn.microsoft.com/api/mcp"
        src.post_steps[url] = [httpx.Response(302, text="moved", headers={"location": "/elsewhere"}, request=httpx.Request("POST", url))]
        net = Net(src, tries=1)
        text, headers = net.post_json(url, {"hello": "world"})
        self.assertEqual(text, "moved")
        self.assertEqual(headers["location"], "/elsewhere")

    def test_sources_post_does_not_follow_redirects(self):
        seen = []

        def web(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.content.decode("utf-8")))
            return httpx.Response(302, headers={"location": "/en-us/sql/next"}, request=request)

        store = Store(Path(tempfile.mkdtemp(prefix="dojo-sourcing-net-")))
        src = Sources(store, http=httpx.Client(transport=httpx.MockTransport(web), follow_redirects=False))
        reply = src.post("https://learn.microsoft.com/api/mcp", {"a": 1}, headers={"x-test": "1"})
        self.assertEqual(reply.status_code, 302)
        self.assertEqual(len(seen), 1)
        self.assertIn('"a":1', seen[0][2].replace(" ", ""))
