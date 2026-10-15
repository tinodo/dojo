from __future__ import annotations

import json
import threading
from typing import Any
from urllib.parse import urlsplit

from .. import pagefinder
from ..sources import docs_url, same_page
from .net import Net, SourcingError

Hit = dict[str, str]


def deny_url(url: str) -> bool:
    text = docs_url(url) or str(url or "")
    lower = text.lower()
    if "/previous-versions/" in lower:
        return True
    bits = [part for part in urlsplit(lower).path.split("/") if part]
    return any(part in {"what-s-new", "whats-new", "release-notes", "breaking-changes"} for part in bits)


def _dedupe(hits: list[Hit]) -> list[Hit]:
    out: list[Hit] = []
    for hit in hits:
        if deny_url(hit["url"]):
            continue
        if not any(same_page(hit["url"], seen["url"]) for seen in out):
            out.append(hit)
    return out


def learn_api(net: Net, query: str, products: tuple[str, ...] = (), top: int = 20) -> list[Hit]:
    return _dedupe(pagefinder.learn_hits(net.get_json(pagefinder.learn_search(query, top, products=products))))


def github_search(net: Net, query: str, top: int = 10) -> list[Hit]:
    return _dedupe(pagefinder.github_hits(net.get_json(pagefinder.github_search(query, top))))


class McpClient:
    def __init__(self, net: Net):
        self.net = net
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._ready = False
        self._checked_tools = False

    def search(self, query: str) -> list[Hit]:
        with self._lock:
            rows = self._tool_call(query)
        out: list[Hit] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = docs_url((row.get("contentUrl") or row.get("url") or "").split("#", 1)[0])
            if not url or deny_url(url):
                continue
            old = next((hit for hit in out if same_page(hit["url"], url)), None)
            if old:
                continue
            out.append(
                {
                    "url": url,
                    "title": pagefinder.label(row.get("title")) or url,
                    "summary": pagefinder.label(row.get("content") or row.get("description"), 300),
                }
            )
        return out

    def _tool_call(self, query: str) -> list[dict]:
        for again in range(2):
            self._ensure_session()
            try:
                data = self._rpc(
                    "tools/call",
                    {"name": "microsoft_docs_search", "arguments": {"query": query}},
                    request_id=3,
                )
            except SourcingError as e:
                if again == 0 and self._session_rejected(str(e), e.status):
                    self._reset()
                    continue
                raise
            if isinstance(data, dict) and data.get("error"):
                text = json.dumps(data["error"], ensure_ascii=False)
                if again == 0 and self._session_rejected(text, None):
                    self._reset()
                    continue
                raise SourcingError(text)
            return self._rows(data)
        raise SourcingError("The MCP session was rejected.")

    def _ensure_session(self) -> None:
        if self._ready:
            return
        reply = self._rpc(
            "initialize",
            {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "dojo", "version": "1"}},
            request_id=1,
        )
        if not self._session_id:
            raise SourcingError("The MCP server did not return a session id.")
        if isinstance(reply, dict) and reply.get("error"):
            raise SourcingError(json.dumps(reply["error"], ensure_ascii=False))
        self._rpc("notifications/initialized", {})
        tools = self._rpc("tools/list", {}, request_id=2)
        names = self._tool_names(tools)
        if "microsoft_docs_search" not in names:
            raise SourcingError("The MCP server does not offer microsoft_docs_search.")
        self._checked_tools = True
        self._ready = True

    def _tool_names(self, payload: Any) -> set[str]:
        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("tools") if isinstance(result, dict) else None
        out = set()
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("name"):
                out.add(str(row["name"]))
        return out

    def _rows(self, payload: Any) -> list[dict]:
        result = payload.get("result") if isinstance(payload, dict) else None
        content = result.get("content") if isinstance(result, dict) else None
        text = "\n".join(str(row.get("text") or "") for row in content if isinstance(row, dict))
        if not text and isinstance(payload, (dict, list)):
            text = json.dumps(payload, ensure_ascii=False)
        try:
            data = json.loads(text)
        except ValueError as e:
            raise SourcingError("The MCP server answered with something Dojo cannot read.") from e
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            rows = data.get("results")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        raise SourcingError("The MCP server answered with something Dojo cannot read.")

    def _rpc(self, method: str, params: dict | None = None, request_id: int | None = None) -> Any:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if request_id is not None:
            body["id"] = request_id
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        text, reply_headers = self.net.post_json("https://learn.microsoft.com/api/mcp", body, headers=headers)
        session = reply_headers.get("mcp-session-id")
        if session:
            self._session_id = session
        return self._parse_reply(text)

    @staticmethod
    def _parse_reply(text: str) -> Any:
        stripped = str(text or "").strip()
        if not stripped:
            return {}
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(stripped)
        messages = []
        for line in stripped.splitlines():
            if not line.startswith("data:"):
                continue
            piece = line[5:].strip()
            if not piece:
                continue
            messages.append(json.loads(piece))
        if not messages:
            raise SourcingError("The MCP server answered with something Dojo cannot read.")
        return messages[-1]

    @staticmethod
    def _session_rejected(text: str, status: int | None) -> bool:
        lower = (text or "").lower()
        return (status in {400, 404, 409} and "session" in lower) or "unknown session" in lower or "invalid session" in lower

    def _reset(self) -> None:
        self._session_id = None
        self._ready = False
        self._checked_tools = False


def fuse(by_retriever: dict[str, list[Hit]], quota: dict[str, int], limit: int, deny=deny_url) -> list[dict]:
    order = list(by_retriever)
    merged: list[dict] = []
    first = 0
    for name in order:
        for pos, hit in enumerate(by_retriever.get(name) or [], start=1):
            if deny(hit["url"]):
                continue
            row = next((item for item in merged if same_page(item["url"], hit["url"])), None)
            if row is None:
                row = dict(hit, by=[], rank=pos, _first=first)
                first += 1
                merged.append(row)
            if name not in row["by"]:
                row["by"].append(name)
            row["rank"] = min(row["rank"], pos)
    picked: list[dict] = []
    seen: list[str] = []
    rounds = max([0] + [max(0, int(quota.get(name, 0))) for name in order])
    for at in range(rounds):
        for name in order:
            if at >= max(0, int(quota.get(name, 0))):
                continue
            hits = by_retriever.get(name) or []
            if at >= len(hits) or deny(hits[at]["url"]):
                continue
            row = next((item for item in merged if same_page(item["url"], hits[at]["url"])), None)
            if row and not any(same_page(row["url"], url) for url in seen):
                seen.append(row["url"])
                picked.append(row)
                if len(picked) >= limit:
                    return [_clean(row) for row in picked[:limit]]
    rest = [row for row in merged if not any(same_page(row["url"], url) for url in seen)]
    rest.sort(key=lambda row: (-len(row["by"]), row["rank"], row["_first"]))
    picked.extend(rest)
    return [_clean(row) for row in picked[:limit]]


def _clean(row: dict) -> dict:
    return {
        "url": row["url"],
        "title": row["title"],
        "summary": row["summary"],
        "by": sorted(row["by"]),
        "rank": row["rank"],
    }
