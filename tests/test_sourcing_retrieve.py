import json
import unittest

from app.sourcing.net import SourcingError
from app.sourcing.retrieve import McpClient, deny_url, fuse, github_search, learn_api


class FakeNet:
    def __init__(self):
        self.answers = {}
        self.posts = []
        self.replies = []

    def get_json(self, url: str):
        return self.answers[url]

    def post_json(self, url: str, body: dict, headers: dict | None = None):
        self.posts.append((url, body, dict(headers or {})))
        step = self.replies.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class RetrieveTests(unittest.TestCase):
    def test_learn_and_github_drop_noise_and_dedupe(self):
        net = FakeNet()
        net.answers["https://learn.microsoft.com/api/search?search=deploy&locale=en-us&$top=20&$filter=category%20eq%20%27Documentation%27"] = {
            "results": [
                {"url": "https://learn.microsoft.com/en-us/sql/deploy", "title": "Deploy", "description": "How to deploy"},
                {"url": "https://learn.microsoft.com/en-us/sql/deploy#part", "title": "Deploy again", "description": "Dup"},
                {"url": "https://learn.microsoft.com/en-us/previous-versions/sql/deploy", "title": "Old", "description": "Old"},
            ]
        }
        hits = learn_api(net, "deploy")
        self.assertEqual([row["url"] for row in hits], ["https://learn.microsoft.com/en-us/sql/deploy"])
        net.answers["https://docs.github.com/api/search/v1?version=free-pro-team%40latest&language=en&query=actions&size=10"] = {
            "hits": [
                {"url": "/en/actions/learn", "title": "<b>Learn</b>", "intro": "One\nTwo"},
                {"url": "/en/actions/learn#part", "title": "Learn 2", "intro": "dup"},
                {"url": "/en/actions/release-notes", "title": "Notes", "intro": "noise"},
            ]
        }
        hits = github_search(net, "actions")
        self.assertEqual(hits, [{"url": "https://docs.github.com/en/actions/learn", "title": "Learn", "summary": "One Two"}])

    def test_mcp_client_handles_sse_and_plain_json(self):
        rows = [
            {"contentUrl": "https://learn.microsoft.com/sql/deploy#one", "title": "Deploy hybrid SQL", "content": "Do the deployment."},
            {"url": "https://learn.microsoft.com/en-us/sql/deploy#two", "title": "Later chunk", "description": "More text."},
            {"url": "https://learn.microsoft.com/en-us/sql/release-notes", "title": "Notes", "content": "Skip me."},
        ]
        net = FakeNet()
        net.replies = [
            ('data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}', {"mcp-session-id": "sid-1"}),
            ("", {}),
            ('{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"microsoft_docs_search"}]}}', {}),
            (f'data: {json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps(rows)}]}})}', {}),
        ]
        client = McpClient(net)
        hits = client.search("Deploy hybrid SQL Server solutions")
        self.assertEqual(hits, [{"url": "https://learn.microsoft.com/en-us/sql/deploy", "title": "Deploy hybrid SQL", "summary": "Do the deployment."}])
        self.assertEqual(net.posts[1][2]["Mcp-Session-Id"], "sid-1")
        self.assertEqual(net.posts[2][2]["Mcp-Session-Id"], "sid-1")

    def test_mcp_reinitialises_once_when_session_is_rejected(self):
        rows = [{"url": "https://learn.microsoft.com/en-us/sql/jobs", "title": "Jobs", "description": "Troubleshoot jobs"}]
        net = FakeNet()
        net.replies = [
            ('{"jsonrpc":"2.0","id":1,"result":{}}', {"mcp-session-id": "sid-a"}),
            ("", {}),
            ('{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"microsoft_docs_search"}]}}', {}),
            ('{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"[]"}]}}', {}),
        ]
        client = McpClient(net)
        self.assertEqual(client.search("one"), [])
        net.replies = [
            SourcingError("HTTP 404: unknown session", status=404),
            ('{"jsonrpc":"2.0","id":1,"result":{}}', {"mcp-session-id": "sid-b"}),
            ("", {}),
            ('{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"microsoft_docs_search"}]}}', {}),
            (json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"results": rows})}]}}), {}),
        ]
        hits = client.search("two")
        self.assertEqual(hits[0]["url"], "https://learn.microsoft.com/en-us/sql/jobs")
        self.assertEqual([call[1]["method"] for call in net.posts].count("initialize"), 2)

    def test_mcp_missing_tool_raises(self):
        net = FakeNet()
        net.replies = [
            ('{"jsonrpc":"2.0","id":1,"result":{}}', {"mcp-session-id": "sid-1"}),
            ("", {}),
            ('{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"other"}]}}', {}),
        ]
        with self.assertRaises(SourcingError):
            McpClient(net).search("hello")

    def test_fuse_uses_quota_then_pool_order(self):
        by = {
            "r1s": [
                {"url": "https://learn.microsoft.com/en-us/sql/a", "title": "A", "summary": "A"},
                {"url": "https://learn.microsoft.com/en-us/sql/b", "title": "B", "summary": "B"},
                {"url": "https://learn.microsoft.com/en-us/sql/c", "title": "C", "summary": "C"},
            ],
            "r2": [
                {"url": "https://learn.microsoft.com/en-us/sql/d", "title": "D", "summary": "D"},
                {"url": "https://learn.microsoft.com/en-us/sql/a#chunk", "title": "A2", "summary": "A2"},
                {"url": "https://learn.microsoft.com/en-us/sql/e", "title": "E", "summary": "E"},
            ],
            "r3": [
                {"url": "https://learn.microsoft.com/en-us/sql/f", "title": "F", "summary": "F"},
                {"url": "https://learn.microsoft.com/en-us/sql/b#part", "title": "B2", "summary": "B2"},
                {"url": "https://learn.microsoft.com/en-us/sql/previous-versions/old", "title": "Old", "summary": "Old"},
            ],
        }
        rows = fuse(by, {"r1s": 1, "r2": 1, "r3": 1}, 6)
        self.assertEqual([row["url"] for row in rows], [
            "https://learn.microsoft.com/en-us/sql/a",
            "https://learn.microsoft.com/en-us/sql/d",
            "https://learn.microsoft.com/en-us/sql/f",
            "https://learn.microsoft.com/en-us/sql/b",
            "https://learn.microsoft.com/en-us/sql/c",
            "https://learn.microsoft.com/en-us/sql/e",
        ])
        self.assertEqual(rows[3]["by"], ["r1s", "r3"])

    def test_deny_url_only_blocks_the_named_patterns(self):
        self.assertTrue(deny_url("https://learn.microsoft.com/en-us/sql/previous-versions/item"))
        self.assertTrue(deny_url("https://learn.microsoft.com/en-us/sql/release-notes"))
        self.assertFalse(deny_url("https://learn.microsoft.com/en-us/sql/overview"))
