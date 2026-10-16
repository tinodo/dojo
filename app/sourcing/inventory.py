from __future__ import annotations

import math
import re
import threading
from collections import Counter
from datetime import timedelta
from hashlib import sha256
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit

from ..core import iso, parse_iso, utcnow
from ..pagefinder import label
from ..sources import docs_url
from .retrieve import Hit, canon_url, deny_url

_TOC_META = re.compile(r"<meta\b[^<>]*>", re.I)
_TOC_NAME = re.compile(r"""\bname\s*=\s*["']toc_rel["']""", re.I)
_TOC_CONTENT = re.compile(r"""\bcontent\s*=\s*["']([^"']*)["']""", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "and", "for", "with", "from", "your", "into", "this", "that", "are", "how", "what", "use", "using",
    "about", "between", "when", "where", "which", "can", "you", "not", "into", "than", "then", "they", "their",
}
_SYN = {
    "ha": "high availability",
    "dr": "disaster recovery",
    "hadr": "high availability disaster recovery",
    "aad": "microsoft entra",
    "rls": "row level security",
    "mi": "managed instance",
}


def toc_key(name: str, toc: str) -> str:
    return f"{name}-{sha256(toc.encode('utf-8')).hexdigest()[:12]}"


class Inventory:
    @staticmethod
    def forget(store, name: str) -> None:
        """An exam was removed: its inventory goes too, the index and every table of contents it read (they are only
        public titles, but nothing of a removed exam is kept)."""
        index = store.read("inventory", name, default=None)
        for row in (index.get("docsets") if isinstance(index, dict) else None) or []:
            if isinstance(row, dict) and row.get("toc"):
                store.delete("inventory", toc_key(name, str(row["toc"])))
        for doc in store.names("inventory"):
            if re.fullmatch(re.escape(name) + r"-[0-9a-f]{12}", doc):
                store.delete("inventory", doc)
        store.delete("inventory", name)

    def __init__(self, store, net, sources, name: str, now=utcnow):
        self.store = store
        self.net = net
        self.sources = sources
        self.name = name
        self.now = now
        self._lock = threading.RLock()
        self._index = store.read("inventory", name, default={"docsets": [], "folders": {}}) or {"docsets": [], "folders": {}}
        self._entries: dict[str, list[dict] | None] = {row["toc"]: None for row in self._index.get("docsets") or [] if isinstance(row, dict)}
        self._bm25: tuple[list[dict], dict[str, float], float] | None = None

    def seed(self, urls, max_new: int = 40) -> int:
        added = 0
        with self._lock:
            for raw in urls or []:
                url = canon_url(raw)
                if not url or urlsplit(url).hostname != "learn.microsoft.com" or deny_url(url):
                    continue
                folder = self._folder(url)
                toc = self._index["folders"].get(folder)
                if toc:
                    self._refresh_docset(toc)
                    continue
                if added >= max_new:
                    break
                remaining = 60_000 - self.size()
                if remaining <= 0:
                    break
                try:
                    final, head = self.net.get_page_head(url)
                    toc = self._toc_url(final, head)
                    if not toc:
                        continue
                    self._index["folders"][folder] = toc
                    if toc not in self._entries:
                        pages, ok, fetched = self._read_toc(toc, remaining)
                        self._entries[toc] = pages
                        self._save_docset(toc, pages, fetched, ok, new=True)
                        added += 1
                    else:
                        self._refresh_docset(toc)
                    self._save_index()
                except Exception:
                    continue
        return added

    def entries(self) -> list[dict]:
        with self._lock:
            out: list[dict] = []
            for row in self._docsets():
                out.extend(self._load_entries(row["toc"]))
            return [dict(item) for item in out]

    def search(self, queries: list[str], top: int = 30) -> list[Hit]:
        with self._lock:
            docs, idf, avg_len = self._ensure_bm25()
            scored: list[tuple[float, int, dict]] = []
            for index, doc in enumerate(docs):
                parts = [self._score(doc, query, idf, avg_len) for query in queries or []]
                parts = [score for score in parts if score > 0]
                if not parts:
                    continue
                total = max(parts) + 0.1 * (sum(parts) - max(parts))
                scored.append((total, index, doc))
            scored.sort(key=lambda row: (-row[0], row[1]))
            out: list[Hit] = []
            seen: set[str] = set()
            for _, _, doc in scored:
                if doc["url"] in seen:
                    continue
                seen.add(doc["url"])
                out.append({"url": doc["url"], "title": doc["title"], "summary": doc["trail"]})
                if len(out) >= top:
                    break
            return out

    def windows(self, size: int = 1500) -> Iterator[list[dict]]:
        rows = self.entries()
        for start in range(0, len(rows), size):
            window = []
            for offset, row in enumerate(rows[start:start + size], start=start + 1):
                window.append({"n": offset, "url": row["url"], "title": row["title"], "trail": row["trail"]})
            yield window

    def size(self) -> int:
        with self._lock:
            return sum(int(row.get("pages") or 0) for row in self._docsets())

    def docsets(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._docsets()]

    def _docsets(self) -> list[dict]:
        return [row for row in self._index.get("docsets") or [] if isinstance(row, dict)]

    def _save_index(self) -> None:
        self.store.write("inventory", self.name, value=self._index)

    def _folder(self, url: str) -> str:
        u = urlsplit(url)
        bits = [part for part in u.path.split("/") if part]
        return f"{u.scheme}://{u.netloc}/" + "/".join(bits[:-1]) if len(bits) > 1 else f"{u.scheme}://{u.netloc}"

    def _toc_key(self, toc: str) -> str:
        return toc_key(self.name, toc)

    def _load_entries(self, toc: str) -> list[dict]:
        rows = self._entries.get(toc)
        if rows is None:
            saved = self.store.read("inventory", self._toc_key(toc), default={}) or {}
            rows = list(saved.get("entries") or [])
            self._entries[toc] = rows
        return rows

    def _refresh_docset(self, toc: str) -> None:
        meta = next((row for row in self._docsets() if row.get("toc") == toc), None)
        if not meta:
            return
        age = self.now() - parse_iso(meta["fetched"])
        if meta.get("ok") and age < timedelta(hours=24):
            return
        if not meta.get("ok") and age < timedelta(hours=1):
            return
        remaining = 60_000 - (self.size() - int(meta.get("pages") or 0))
        pages, ok, fetched = self._read_toc(toc, max(0, remaining))
        if ok:
            self._entries[toc] = pages
        self._save_docset(toc, pages if ok else self._load_entries(toc), fetched, ok, new=False)

    def _save_docset(self, toc: str, pages: list[dict], fetched: str, ok: bool, new: bool) -> None:
        entry = {"toc": toc, "pages": len(pages), "fetched": fetched, "ok": ok}
        rows = self._docsets()
        at = next((i for i, row in enumerate(rows) if row.get("toc") == toc), None)
        if at is None:
            rows.append(entry)
        else:
            rows[at] = entry
        self._index["docsets"] = rows
        self.store.write("inventory", self._toc_key(toc), value={"toc": toc, "fetched": fetched, "ok": ok, "entries": list(pages)})
        self._save_index()
        self._bm25 = None

    def _read_toc(self, toc: str, remaining: int) -> tuple[list[dict], bool, str]:
        fetched = iso(self.now())
        try:
            data = self.net.get_json(toc, docs=True)
            items = data.get("items") if isinstance(data, dict) else None
            rows: list[dict] = []
            self._walk(items if isinstance(items, list) else [], [], toc, rows, min(20_000, remaining))
            return rows, True, fetched
        except Exception:
            return [], False, fetched

    def _walk(self, items: list[dict], trail: list[str], base: str, out: list[dict], cap: int) -> None:
        for item in items:
            if len(out) >= cap:
                return
            if not isinstance(item, dict):
                continue
            title = label(item.get("toc_title"))
            href = item.get("href")
            if href:
                url = canon_url(urljoin(base, str(href)))
                if url and not deny_url(url):
                    out.append({"url": url, "title": title or url, "trail": " > ".join(trail)})
                    if len(out) >= cap:
                        return
            next_trail = trail + ([title] if title else [])
            children = item.get("children")
            if isinstance(children, list):
                self._walk(children, next_trail, base, out, cap)

    def _toc_url(self, final: str, page: str) -> str | None:
        for tag in _TOC_META.findall(page or ""):
            if not _TOC_NAME.search(tag):
                continue
            found = _TOC_CONTENT.search(tag)
            if found:
                toc = urljoin(final, found.group(1))
                return toc if docs_url(toc) else None   # a table of contents outside the documentation is never read (EG-39)
        return None

    def _ensure_bm25(self) -> tuple[list[dict], dict[str, float], float]:
        if self._bm25 is not None:
            return self._bm25
        docs = []
        df: Counter[str] = Counter()
        for row in self.entries():
            tokens = self._tokens(" ".join([row["title"], row["trail"], urlsplit(row["url"]).path.replace("/", " ").replace("-", " ")]))
            bag = Counter(tokens)
            docs.append({**row, "terms": bag, "len": sum(bag.values())})
            for token in bag:
                df[token] += 1
        count = max(1, len(docs))
        avg_len = sum(doc["len"] for doc in docs) / count if docs else 0.0
        idf = {term: math.log(1 + (count - seen + 0.5) / (seen + 0.5)) for term, seen in df.items()}
        self._bm25 = (docs, idf, avg_len or 1.0)
        return self._bm25

    def _score(self, doc: dict, query: str, idf: dict[str, float], avg_len: float) -> float:
        score = 0.0
        for term in self._tokens(query):
            tf = doc["terms"].get(term, 0)
            if not tf:
                continue
            top = tf * (1.2 + 1.0)
            bottom = tf + 1.2 * (1.0 - 0.75 + 0.75 * doc["len"] / avg_len)
            score += idf.get(term, 0.0) * top / bottom
        return score

    def _tokens(self, text: str) -> list[str]:
        out: list[str] = []
        for raw in _WORD.findall((text or "").lower()):
            if raw in _SYN:
                out.extend(self._tokens(_SYN[raw]))
                continue
            if len(raw) < 3 or raw in _STOP:
                continue
            out.append(self._stem(raw))
        return out

    @staticmethod
    def _stem(word: str) -> str:
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix):
                stem = word[: -len(suffix)]
                if len(stem) >= 4:
                    return stem
        return word
