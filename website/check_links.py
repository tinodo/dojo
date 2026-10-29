"""Checks that the external addresses on the built project site still answer:  python website/check_links.py _site

Run weekly by .github/workflows/site-links.yml (ADR 0027). It looks at the addresses on learn.microsoft.com and
docs.github.com only: the exam pages and study guides come from the package files, and Microsoft moves them now and then.
Links to this repository are checked against the files themselves by tests/test_site.py, not over the network.

A run fails on a connection error, on any final answer that is not 2xx (other than 403, 429 and 5xx, which are reported
as inconclusive because they only say that the other side is busy or refuses a robot), and when an address now ends up
at a different page after its redirects: another host or another path, for example a study guide replaced by an
overview. A query string, a trailing slash, capital letters and the English language prefix do not make another page.
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

HOSTS = {"learn.microsoft.com", "docs.github.com"}
AGENT = "dojo-project-site-link-check (+https://github.com/tinodo/dojo)"
BUSY = {403, 429}


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if urlparse(href).scheme == "https" and urlparse(href).hostname in HOSTS:
                self.found.append(href)


def collect(site: Path) -> list[str]:
    """Every external address of the built site that this check looks at, once each."""
    out: set[str] = set()
    for page in sorted(site.rglob("*.html")):
        parser = Links()
        parser.feed(page.read_text("utf-8"))
        out.update(parser.found)
    return sorted(out)


LOCALES = {"en", "en-us"}


def page_of(url: str) -> tuple[str, str]:
    """Host and full path of an address, without query, fragment, trailing slash, case or the English language prefix."""
    parts = urlparse(url)
    segments = [s for s in parts.path.lower().split("/") if s]
    if segments and segments[0] in LOCALES:
        segments = segments[1:]
    return (parts.hostname or "").lower(), "/".join(segments)


def judge(url: str, status: int | None, final_url: str | None) -> tuple[str, str]:
    """('ok' | 'inconclusive' | 'fail', why) for one address, from its final status and where its redirects ended."""
    if status is None:
        return "fail", "no answer"
    if status in BUSY or 500 <= status <= 599:
        return "inconclusive", f"HTTP {status}"
    if not 200 <= status <= 299:
        return "fail", f"HTTP {status}"
    if page_of(url) != page_of(final_url or url):
        return "fail", f"now ends at {final_url}, a different page"
    return "ok", ""


def fetch(url: str) -> tuple[int | None, str | None]:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.geturl()
        except urllib.error.HTTPError as e:
            if attempt == 2 or e.code < 500:
                return e.code, e.geturl()
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                return None, None
        time.sleep(2)
    return None, None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python website/check_links.py <built site folder>", file=sys.stderr)
        return 2
    urls = collect(Path(argv[1]))
    if not urls:
        print("No external addresses found: the site did not build, or its links changed shape.", file=sys.stderr)
        return 2
    failed = 0
    for url in urls:
        verdict, why = judge(url, *fetch(url))
        print(f"{verdict:12} {url}" + (f"  ({why})" if why else ""))
        failed += verdict == "fail"
    print(f"{len(urls)} addresses, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
