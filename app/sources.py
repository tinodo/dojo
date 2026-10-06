"""The official pages each package cites: allow-listed fetching, dated snapshots, relevant excerpts
and verbatim quote checks. Pages are only ever fetched from the two hosts: the pages a package lists
and, when the owner adds an exam, its study guide, its certification page and the two sites' search.

A snapshot also remembers the hash of its text in the form the quote check reads (`norm`), and the
hashes and dates of up to HISTORY earlier versions, never their text. That is what lets the drift check
(app/drift.py) tell a change of formatting from a change of words, and date a change.

A snapshot is filed under the address a package cites, and it only ever holds that page: a fetch that a
redirect took to another page is not stored under it (`same_page`, ADR 0006). Candidates follow,
citations do not: while the owner adds an exam, a candidate page that now leads to another documentation
page is replaced by that page, stored and cited under its own address (`candidate`, ADR 0005)."""
from __future__ import annotations

import email.utils
import html
import re
import threading
from datetime import timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

import httpx

from .core import Store, UserError, iso, parse_iso, sha256, utcnow

ALLOWED_HOSTS = {"docs.github.com", "learn.microsoft.com"}
MAX_BYTES = 4_000_000
FRESH_DAYS = 7
HISTORY = 12          # earlier versions remembered per page: hashes and dates only
GONE_CODES = (404, 410)
SLOW_DOWN_CODES = (403, 429)   # with every 5xx: the site is limiting or failing, not saying the page is gone
# Parts of Learn that are not documentation: training is linked, never copied (EG-39); the rest are
# credentials, profiles, questions and answers, shows and the like.
NOT_DOCS = {"training", "credentials", "users", "collections", "shows", "answers", "search", "api", "events",
            "videos", "samples", "plans", "challenges", "assessments"}
_SEGMENT = re.compile(r"[A-Za-z0-9._~!$&'()*+,=:@-]+")
GITHUB_ARTICLE = "/api/article/body"
# The first path segment when it names a language: Learn's "de-de" or "zh-hant-tw", GitHub Docs' "ja".
_LOCALE = {"learn.microsoft.com": re.compile(r"[a-z]{2}-[a-z]{2,4}(-[a-z]{2})?", re.I),
           "docs.github.com": re.compile(r"[a-z]{2}", re.I)}
_HOME = {"learn.microsoft.com": "en-us", "docs.github.com": "en"}

_TRANS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
    "\u2011": "-", "\u00a0": " ", "\u2026": "...", "\u200b": "",
})
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")
_NOISE = re.compile(r"[*_`\\>#|]+")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]{4,}")


def normalize(text: str) -> str:
    """Comparable form: markdown links reduced to their text, entities decoded, typographic quotes
    and dashes made plain, formatting characters removed, whitespace collapsed, case folded."""
    text = _LINK.sub(r"\1", text or "")
    text = html.unescape(text).translate(_TRANS)
    text = _NOISE.sub(" ", text)
    return _SPACE.sub(" ", text).strip().casefold()


def check_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS or u.username or u.password or u.port not in (None, 443):
        raise UserError(f"Not an allowed source: {url}")
    return url


def retry_after(response: httpx.Response) -> float | None:
    """The seconds a site asked us to wait (Retry-After, as seconds or as a date), or None."""
    value = (response.headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - utcnow()).total_seconds())


def docs_url(url: Any) -> str | None:
    """The canonical address of an official documentation page, or None when `url` is not one.

    Only documentation is read, stored and quoted: training is linked, never copied (EG-39). The path is
    decoded and its dot segments resolved first, as a server would, so neither `en-us/azure/../training`
    nor an encoded `%2e%2e` passes for documentation. A segment with anything unusual left in it (a
    backslash, a semicolon, a percent sign, a space, a letter outside ASCII) is refused, not guessed at."""
    try:
        u = urlsplit(check_url(str(url or "").strip()))
    except (UserError, ValueError):
        return None
    parts: list[str] = []
    for seg in unquote(u.path).split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        if not _SEGMENT.fullmatch(seg):
            return None
        parts.append(seg)
    if u.hostname == "docs.github.com":
        return "https://docs.github.com/" + "/".join(parts) if len(parts) >= 2 and parts[0] == "en" else None
    if len(parts) < 2 or parts[0] != "en-us" or parts[1].lower() in NOT_DOCS:
        return None
    view = parse_qs(u.query).get("view")
    return "https://learn.microsoft.com/" + "/".join(parts) + (f"?view={quote(view[0], safe='-._')}" if view else "")


def _article(url: str) -> str | None:
    """The documentation page a GitHub Docs article API address asks for, or None when it is not one."""
    u = urlsplit(url)
    names = parse_qs(u.query).get("pathname") or []
    if u.hostname != "docs.github.com" or u.path != GITHUB_ARTICLE or len(names) != 1 or not names[0].startswith("/"):
        return None
    return docs_url("https://docs.github.com" + names[0])


def page_address(url: Any) -> str:
    """A GitHub Docs article API address as the address of the page it asks for; any other as it is."""
    text = str(url or "")
    try:
        u = urlsplit(text)
    except ValueError:
        return text
    names = parse_qs(u.query).get("pathname") or []
    if u.hostname == "docs.github.com" and u.path == GITHUB_ARTICLE and len(names) == 1 and names[0].startswith("/"):
        return "https://docs.github.com" + names[0]
    return text


def _names(url: Any) -> tuple[str, str, str] | None:
    """What names a documentation page: its host, its path and Learn's `view`. It is read in the form
    `docs_url` gives, so a trailing slash does not count, and in the home language, so a leading language
    segment (or none) does not count either. None when `url` names no documentation page."""
    try:
        u = urlsplit(page_address(str(url or "").strip()))
    except ValueError:
        return None
    locale = _LOCALE.get(u.hostname or "")
    if not locale:
        return None
    segs = u.path.split("/")[1:]
    if segs and locale.fullmatch(segs[0]):
        segs = segs[1:]
    canon = docs_url(urlunsplit(u._replace(path="/" + "/".join([_HOME[u.hostname]] + segs))))
    if not canon:
        return None
    c = urlsplit(canon)
    return c.hostname or "", c.path, c.query


def same_page(url: Any, other: Any) -> bool:
    """Whether two addresses name the same documentation page (ADR 0006): the same host, path and Learn
    `view`. Only the language and a trailing slash may differ; anything else, letter case included, is
    another page."""
    a = _names(url)
    return a is not None and a == _names(other)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button",
            "template", "iframe", "select", "option"}
    BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "pre", "section",
             "article", "table", "blockquote", "dd", "dt", "summary", "details", "main", "hr", "figure"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK and not self.skip:
            self.out.append("\n")
        elif tag == "td" and not self.skip:
            self.out.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK and not self.skip:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)

    def text(self) -> str:
        lines = [_SPACE.sub(" ", line).strip() for line in "".join(self.out).split("\n")]
        out: list[str] = []
        for line in lines:
            if line:
                out.append(line)
            elif out and out[-1]:
                out.append("")
        return "\n".join(out).strip()


def html_to_text(page: str) -> str:
    m = re.search(r"<main\b[^>]*>(.*)</main>", page, re.S | re.I)
    parser = _TextExtractor()
    parser.feed(m.group(1) if m else page)
    parser.close()
    return parser.text()


def excerpt(text: str, query: str, limit: int = 12000) -> str:
    """The parts of a long page most related to the skill, in page order, within `limit` characters.
    The opening paragraphs are always kept."""
    if len(text) <= limit:
        return text
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    wanted = set(_WORD.findall(query.lower()))
    scored = []
    for i, p in enumerate(paragraphs):
        words = _WORD.findall(p.lower())
        hits = sum(1 for w in words if w in wanted)
        scored.append((hits / (1 + len(words)) ** 0.5, -i, i))
    keep: set[int] = set()
    total = 0
    for i in range(min(3, len(paragraphs))):
        if total + len(paragraphs[i]) <= limit // 4:
            keep.add(i)
            total += len(paragraphs[i]) + 2
    for _, _, i in sorted(scored, reverse=True):
        if i in keep:
            continue
        size = len(paragraphs[i]) + 2
        if total + size > limit:
            continue
        keep.add(i)
        total += size
    return "\n\n".join(paragraphs[i] for i in sorted(keep))


class NotDocs(UserError):
    """An address that is not, or no longer leads to, an official documentation page (EG-39)."""


def verified(snap: Any) -> bool:
    """Whether a stored snapshot was read under the documentation rule: it records the address its fetch
    ended at after redirects (`final`), and that address is documentation. A snapshot stored before Dojo
    checked redirects has no such address. It may hold a training page's text under a documentation
    address, so it is never used: not as a recent copy, and not as the last good copy either."""
    return isinstance(snap, dict) and docs_url(snap.get("final")) is not None


def own_copy(url: str, snap: Any) -> bool:
    """Whether a stored snapshot is a copy of the page at `url` that Dojo may use: read under the
    documentation rule (`verified`), and from that page itself rather than another page a redirect led to
    (ADR 0006). Anything else is kept as it is, but never read as the page."""
    return verified(snap) and same_page(url, snap.get("final"))


class Sources:
    """Fetches the official pages (two allow-listed hosts), keeps a dated text snapshot of each, and checks
    quotes word for word against it."""
    def __init__(self, store: Store, http: httpx.Client | None = None):
        self.store = store
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": "dojo/1 (personal study tool)"},
            follow_redirects=False,
        )
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._normalized: dict[str, str] = {}
        self._heads: dict[str, dict] = {}   # url -> the stored snapshot without its text; only this class writes snapshots

    @staticmethod
    def key(url: str) -> str:
        return "s" + sha256(url)[:39]

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, url: str, force: bool = False) -> dict:
        """A documentation page's text, from a snapshot under a week old or fetched now. A page that no
        longer leads to documentation is refused outright. For any other failure the last good copy is
        returned, marked stale, but only a copy that was itself read under the documentation rule, from
        this page (`own_copy`). Without one the source is unavailable, as if Dojo had never read it.
        Another page's text is never stored under this address: a page that now leads to another page is
        such a failure (ADR 0006)."""
        return self._get(url, force, follow=False)

    def candidate(self, url: str) -> dict:
        """A page Dojo is choosing as a new source, when the owner adds an exam (app/newexam.py). It is read
        like a cited page (`get`) except in one case: when the address now leads to another documentation
        page, that page is the candidate. Its text is stored under its own address, and the snapshot
        returned carries that address (`url`), so the page is cited, checked and read again there. Nothing
        is stored under the address asked for. A page the drift check found gone is not chosen from its
        stored copy; it is read again. A redirect out of the documentation is refused, as always."""
        return self._get(url, False, follow=True)

    def _get(self, url: str, force: bool, follow: bool) -> dict:
        check_url(url)
        key = self.key(url)
        with self._lock(key):
            snap = self.store.read("sources", key)
            # A snapshot stored before Dojo checked where a fetch ended is read again when the page is
            # needed. Until that succeeds it is not used for anything.
            good = snap if own_copy(url, snap) and not (follow and snap.get("gone")) else None
            if good and not force and (utcnow() - parse_iso(good["fetched_at"])).days < FRESH_DAYS:
                return good
            try:
                text, final = self._fetch(url)
            except NotDocs:
                raise
            except (httpx.HTTPError, UserError) as e:
                if good:
                    good["stale"] = True
                    return good
                raise UserError(f"Could not read the source {url} ({type(e).__name__}).") from e
            elsewhere = not same_page(url, final)
            if not elsewhere and len(text) >= 200:
                return self._keep(url, key, snap, text, final)
            if not (elsewhere and follow):
                if good:
                    good["stale"] = True
                    return good
                if elsewhere:
                    raise UserError(f"The source {url} now leads to another page, {final}.")
                raise UserError(f"The source {url} returned almost no text.")
        # A candidate that leads to another documentation page: this address's lock is released first.
        return self._follow(url, text, final)

    def _follow(self, url: str, text: str, final: str) -> dict:
        """Keep what a candidate's read found at the documentation page it ended at, under that page's own
        address. A page on the other site is read again with that site's own reader, and kept where that
        read ends."""
        if urlsplit(final).hostname != urlsplit(url).hostname:
            try:
                text, final = self._fetch(final)
            except NotDocs:
                raise
            except (httpx.HTTPError, UserError) as e:
                raise UserError(f"Could not read the page {final}, where {url} now leads ({type(e).__name__}).") from e
        if len(text) < 200:
            raise UserError(f"The page {url} now leads to {final}, which returned almost no text.")
        key = self.key(final)
        with self._lock(key):
            return self._keep(final, key, self.store.read("sources", key), text, final)

    def _keep(self, url: str, key: str, old: dict | None, text: str, final: str) -> dict:
        """Store a newly fetched text with the documentation address it was read from (`final`), which is
        this page and never another one. A new version pushes the old one's hashes and dates into the
        history, when the old one was a copy Dojo may use. A page that answers again is no longer gone."""
        if not same_page(url, final):
            raise UserError(f"The source {url} now leads to another page, {final}.")
        trusted = own_copy(url, old)
        digest, now = sha256(text), iso()
        history = list((old or {}).get("history") or []) if trusted else []
        if old and old.get("sha256") == digest:
            # When the text is the same, the date it was first seen is kept, even from a snapshot stored before the check.
            first_seen, norm = old.get("first_seen", old["fetched_at"]), self._norm(old)
        else:
            first_seen, norm = now, sha256(normalize(text))
            if trusted:
                history.append({"sha256": old["sha256"], "norm": self._norm(old),
                                "first_seen": old.get("first_seen", old["fetched_at"]), "last_seen": old["fetched_at"]})
        fresh = {"url": url, "final": final, "fetched_at": now, "sha256": digest, "norm": norm, "chars": len(text),
                 "text": text, "first_seen": first_seen}
        if history:
            fresh["history"] = history[-HISTORY:]
        self.store.write("sources", key, value=fresh)
        self._heads[url] = self._head_of(fresh)
        return fresh

    def _norm(self, snap: dict) -> str:
        return snap.get("norm") or sha256(self.normalized(snap))

    def _head_of(self, snap: dict) -> dict:
        head = {"url": snap["url"], "final": snap.get("final"), "fetched_at": snap["fetched_at"], "gone": snap.get("gone"),
                "moved": snap.get("moved"), "verified": own_copy(snap["url"], snap)}
        if not head["verified"]:
            return {**head, "sha256": None, "norm": None, "first_seen": None, "history": []}
        return {**head, "sha256": snap["sha256"], "norm": self._norm(snap),
                "first_seen": snap.get("first_seen", snap["fetched_at"]), "history": list(snap.get("history") or [])}

    def head(self, url: str) -> dict | None:
        """The stored snapshot of a page without its text: hashes, dates, earlier versions and whether the
        page is gone (and, when it moved, where to). None when Dojo has never read the page. A snapshot
        that is not a copy Dojo may use (`own_copy`) gives no version at all (`verified` False): only when
        it was read, and whether the page is gone."""
        h = self._heads.get(url)
        if h is None:
            try:
                snap = self.store.read("sources", self.key(url))
            except ValueError:
                snap = None
            if not snap:
                return None
            # setdefault: a version that _keep or mark_gone stored meanwhile wins, as they set the head
            # after writing the page.
            h = self._heads.setdefault(url, self._head_of(snap))
        return h

    def stored(self, url: str) -> dict | None:
        """The stored copy of a page as Dojo last read it, without reading it again: gone or not, and whether
        or not anything cites the page now. None unless it is a copy Dojo may use (`own_copy`). An answer is
        judged from the very pages its item was written from (Dojo.item_grounding, ADR 0008)."""
        try:
            snap = self.store.read("sources", self.key(url))
        except ValueError:
            return None
        return snap if own_copy(url, snap) else None

    def recheck(self, url: str) -> dict:
        """Fetch a page again now, for the drift check, through the same documentation rule as `get`. Never
        raises for trouble with the site; says what happened: "same" or "changed" (the snapshot is updated;
        "formatting" says whether only formatting changed), "moved" (the address now leads to another page,
        `to`: see `same_page`), "missing" (404 or 410), "limited" (the site asked us to slow down, or is
        failing) or "failed" (anything else). Only "same" and "changed" touch the stored snapshot."""
        check_url(url)
        key = self.key(url)
        with self._lock(key):
            snap = self.store.read("sources", key)
            try:
                text, final = self._fetch(url)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in GONE_CODES:
                    return {"outcome": "missing", "status": code}
                if code in SLOW_DOWN_CODES or code >= 500:
                    return {"outcome": "limited", "status": code, "retry_after": retry_after(e.response)}
                return {"outcome": "failed", "status": code, "reason": f"the site answered {code}"}
            except (httpx.HTTPError, UserError) as e:
                # A redirect Dojo would not follow: to another page, the page moved; to this page in
                # another language, it only could not be read.
                to = getattr(e, "to", None)
                if to and not same_page(url, to):
                    return {"outcome": "moved", "to": page_address(to)}
                return {"outcome": "failed", "reason": f"the page could not be read ({type(e).__name__})"}
            if not same_page(url, final):
                return {"outcome": "moved", "to": final}
            if len(text) < 200:
                return {"outcome": "failed", "reason": "the page returned almost no text"}
            old = snap if own_copy(url, snap) else None
            fresh = self._keep(url, key, snap, text, final)
        if snap and snap.get("sha256") == fresh["sha256"]:
            return {"outcome": "same", "sha256": fresh["sha256"]}
        return {"outcome": "changed", "sha256": fresh["sha256"], "before": (old or {}).get("sha256"),
                "formatting": bool(old) and self._norm(old) == fresh["norm"]}

    def mark_gone(self, url: str, since: str, to: str | None = None) -> None:
        """The page is no longer there: not found, or moved to another page (`to`). Its snapshot stays, for
        what was written from it; nothing new is. Any stored snapshot can be marked, one Dojo may use or not."""
        key = self.key(url)
        with self._lock(key):
            snap = self.store.read("sources", key)
            if not snap or snap.get("gone"):
                return
            snap["gone"] = since
            if to:
                snap["moved"] = to
            self.store.write("sources", key, value=snap)
            self._heads[url] = self._head_of(snap)

    def compare(self, url: str, sha: str | None) -> dict:
        """How the stored page differs from its version with hash `sha`: "same", "formatting" (only the
        formatting differs), "changed" (the words differ, or the version is older than Dojo remembers) or
        "gone" (moved included), with the time the page last moved away from that version's words. With
        no copy Dojo may use yet, there is nothing to compare with until the page is read again: "same"."""
        h = self.head(url)
        if h is None or not sha:
            return {"state": "same", "since": None}
        if h.get("gone"):
            return {"state": "gone", "since": h["gone"]}
        if not h["verified"] or h["sha256"] == sha:
            return {"state": "same", "since": None}
        versions = h["history"] + [{"sha256": h["sha256"], "norm": h["norm"], "first_seen": h["first_seen"]}]
        # The last time the page was at that version counts, and only its words: a page that changed and
        # came back has the words it had.
        at = next((i for i in range(len(versions) - 2, -1, -1) if versions[i]["sha256"] == sha), None)
        if at is None:
            return {"state": "changed", "since": h["first_seen"]}
        words = versions[at]["norm"]
        if h["norm"] == words:
            return {"state": "formatting", "since": versions[at + 1]["first_seen"]}
        k = len(versions) - 1   # back to the first of the latest versions whose words differ
        while k - 1 > at and versions[k - 1]["norm"] != words:
            k -= 1
        return {"state": "changed", "since": versions[k]["first_seen"]}

    def holds(self, url: str, quote_text: str) -> bool:
        """Whether a quote is still word for word on the stored page. Never on a page that is gone, and
        never on a copy Dojo may not use."""
        h = self.head(url)
        if not h or h.get("gone") or not h["verified"]:
            return False
        text = self._normalized.get(h["sha256"])
        if text is None:
            snap = self.store.read("sources", self.key(url))
            if not own_copy(url, snap):
                return False
            text = self.normalized(snap)
        return _quote_in(normalize(quote_text or ""), text)

    def _fetch(self, url: str) -> tuple[str, str]:
        """The text of a documentation page and the documentation address it was read from. The rule is
        checked on the address asked for and on every redirect before it is followed, so a redirect into
        training (or anywhere else) is never read, let alone stored as documentation."""
        canon = docs_url(url)
        if not canon:
            raise NotDocs(f"Not an official documentation page: {url}")
        if urlsplit(canon).hostname == "docs.github.com":
            read, first = _article, "https://docs.github.com" + GITHUB_ARTICLE + "?pathname=" + quote(urlsplit(canon).path, safe="/")
        else:
            read, first = docs_url, url
        response = self._request(first, read)
        final = read(str(response.url))
        if not final:
            e = NotDocs(f"The source {url} led away from the documentation, to {response.url}.")
            e.to = str(response.url)
            raise e
        return (response.text.strip() if read is _article else html_to_text(response.text)), final

    def page(self, url: str) -> tuple[str, str]:
        """A page read for its structure (a study guide or a certification page): the address it ended
        at after redirects, and its HTML. Nothing is stored. A missing page raises httpx.HTTPStatusError."""
        response = self._request(url)
        return str(response.url), response.text

    def data(self, url: str) -> Any:
        """The JSON answer of a search on one of the two hosts."""
        try:
            return self._request(url).json()
        except ValueError as e:
            raise UserError("The search answered with something Dojo cannot read.") from e

    def _request(self, url: str, docs: Callable[[str], str | None] | None = None) -> httpx.Response:
        """GET within the two hosts, following up to five redirects. With `docs`, every address,
        redirects included, must pass it before it is asked for. A redirect that is refused says where it
        led (`to` on the error)."""
        start = url
        for _ in range(5):
            try:
                check_url(url)
                if docs and not docs(url):
                    raise NotDocs(f"A redirect led away from the documentation, to {url}.")
            except UserError as e:
                if url != start:
                    e.to = url
                raise
            response = self.http.get(url)
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            if len(response.content) > MAX_BYTES:
                raise UserError("The source page is too large.")
            return response
        raise UserError("The source redirected too many times.")

    def normalized(self, snap: dict) -> str:
        key = snap["sha256"]
        value = self._normalized.get(key)
        if value is None:
            if len(self._normalized) > 300:
                self._normalized.clear()
            value = self._normalized[key] = normalize(snap["text"])
        return value

    def quote_ok(self, quote_text: str, snap: dict) -> bool:
        return _quote_in(normalize(quote_text or ""), self.normalized(snap))


def _quote_in(q: str, text: str) -> bool:
    """A quote counts only when it is long enough to mean something and is in the text word for word."""
    return len(q) >= 20 and len(q.split()) >= 4 and q in text
