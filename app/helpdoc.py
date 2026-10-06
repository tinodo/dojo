"""The in-app Help: a small, safe Markdown renderer for docs/user-guide.md.

The guide is one file that reads well in the repo and is the Help page in the app. This renderer knows only
what the guide needs: headings (with ids), paragraphs, bullet and numbered lists, bold, italic, inline code,
fenced code blocks and links. Every piece of text is HTML-escaped. A link is kept only if it goes to an
https:// address or to a page in the app (#/...); a fragment such as #recalls becomes #/help/recalls. Any
other link, raw HTML included, is shown as the text it is. Heading ids follow GitHub's rule, so an anchor
works the same in the repo and in the app.
"""
from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "user-guide.md"
# Every heading under this level-2 section is labelled "For the owner" in the app.
OWNER_SECTION = "for-the-owner"

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_BULLET = re.compile(r"^[ ]{0,3}[-*+][ \t]+(.*)$")
_NUMBER = re.compile(r"^[ ]{0,3}\d{1,9}[.)][ \t]+(.*)$")
_FENCE = re.compile(r"^[ ]{0,3}(```+|~~~+)")
_INLINE = re.compile(r"`([^`\n]+)`|\[([^\]\n]+)\]\(([^()\s]*)\)")
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_EM = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_HTTPS = re.compile(r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?(?:[/?#][^\s<>\"'`\\]*)?$")
_APP = re.compile(r"^#/[A-Za-z0-9/._-]*$")
_FRAGMENT = re.compile(r"^#([a-z0-9_-]+)$")


def slug(text: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces become hyphens."""
    plain = re.sub(r"[`*]", "", text).strip().lower()
    return re.sub(r"[^\w\- ]", "", plain).replace(" ", "-")


def safe_href(url: str) -> str | None:
    """The address a link may have, or None: then the link is shown as plain text."""
    if _HTTPS.match(url):
        return url
    if _APP.match(url):
        return url
    m = _FRAGMENT.match(url)
    return f"#/help/{m.group(1)}" if m else None


def _emphasis(escaped: str) -> str:
    return _EM.sub(r"<em>\1</em>", _STRONG.sub(r"<strong>\1</strong>", escaped))


def inline(text: str) -> str:
    """One line of Markdown as escaped HTML with only the inline marks the guide uses."""
    out, at = [], 0
    for m in _INLINE.finditer(text):
        out.append(_emphasis(html.escape(text[at:m.start()])))
        if m.group(1) is not None:
            out.append(f"<code>{html.escape(m.group(1))}</code>")
        else:
            href = safe_href(m.group(3))
            if href is None:
                out.append(html.escape(m.group(0)))
            else:
                outside = ' target="_blank" rel="noopener noreferrer"' if href.startswith("https://") else ""
                out.append(f'<a href="{html.escape(href, quote=True)}"{outside}>{_emphasis(html.escape(m.group(2)))}</a>')
        at = m.end()
    out.append(_emphasis(html.escape(text[at:])))
    return "".join(out)


def render(markdown: str, owner_section: str | None = OWNER_SECTION) -> dict:
    """The guide as {"html", "toc", "anchors"}: toc lists the level-2 and level-3 headings in order, and
    anchors every heading id. A heading under owner_section is marked class="owner"."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    toc: list[dict] = []
    seen: dict[str, int] = {}
    used: set[str] = set()
    anchors: list[str] = []
    para: list[str] = []
    items: list[list[str]] = []
    kind = ""
    owner = False

    def flush() -> None:
        nonlocal kind
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{inline(' '.join(i))}</li>" for i in items) + f"</{kind}>")
            items.clear()
        kind = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush()
            mark, body = fence.group(1), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(mark):
                body.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(body))}</code></pre>")
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level, text = len(heading.group(1)), heading.group(2)
            base = slug(text) or "section"
            # Like GitHub: a repeated heading gets -1, -2, ...; an id already taken is skipped, so ids never collide.
            anchor, n = base, seen.get(base, 0)
            if anchor in used:
                n = max(n, 1)
                while f"{base}-{n}" in used:
                    n += 1
                anchor = f"{base}-{n}"
            seen[base] = n + 1
            used.add(anchor)
            anchors.append(anchor)
            if level <= 2:
                owner = bool(owner_section) and level == 2 and anchor == owner_section
            cls = ' class="owner"' if owner else ""
            out.append(f'<h{level} id="{anchor}"{cls}>{inline(text)}</h{level}>')
            if level in (2, 3):
                toc.append({"id": anchor, "title": re.sub(r"[`*]", "", text), "level": level, "owner": owner})
        elif not line.strip():
            flush()
        elif _BULLET.match(line) or _NUMBER.match(line):
            bullet = _BULLET.match(line)
            want = "ul" if bullet else "ol"
            if para or (kind and kind != want):
                flush()
            kind = want
            items.append([(bullet or _NUMBER.match(line)).group(1).strip()])
        elif items:
            items[-1].append(line.strip())     # a wrapped list item goes on until a blank line
        else:
            para.append(line.strip())
        i += 1
    flush()
    return {"html": "\n".join(out), "toc": toc, "anchors": anchors}


@lru_cache(maxsize=4)
def _cached(path: str, mtime: float) -> dict:
    return render(Path(path).read_text("utf-8"))


def guide(path: Path | None = None) -> dict:
    """The rendered user guide, read again only when the file changes."""
    path = path or GUIDE
    return _cached(str(path), path.stat().st_mtime)
