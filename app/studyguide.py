"""Reading an official study guide and certification page (DESIGN section 1 and screen 12; EG-45).

A certification target is configuration. Its study guide on Microsoft Learn states the blueprint: the
domains with their weight ranges, the groups and skills under them, the date the skills are measured
as of, and a change log. This module reads such pages without a model and without the network.
The pages are untrusted input (EG-37): their words are only ever copied, and every copied title and
skill is checked word for word against the page's own text before anything is built from it."""
from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

from .core import UserError, sha256
from .sources import check_url, html_to_text, normalize

LEARN = "https://learn.microsoft.com"
GUIDE_URL = LEARN + "/en-us/credentials/certifications/resources/study-guides/{code}"
EXAM_URL = LEARN + "/en-us/credentials/certifications/exams/{code}/"
CODE_HELP = "An exam code is two letters and three digits, such as AZ-104 or GH-300."
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
MAX_SKILLS = 400
_M = "|".join(MONTHS)
_CODE = re.compile(r"^([A-Z]{2})(\d{3})$")
_DATE = re.compile(rf"\b({_M})\s*(?:(\d{{1,2}})\s*,\s*|,?\s*)(\d{{4}})\b", re.I)
_FULL_DATE = rf"(?:{_M})\s*\d{{1,2}}\s*,\s*\d{{4}}"
_TITLE = re.compile(r"^study guide for exam ([a-z]{2}-\d{3})\s*:\s*(.+)$", re.I)
_WEIGHT = re.compile(r"^(.*\S)\s*\(\s*(\d{1,3})\s*(?:[-\u2013\u2014]\s*(\d{1,3})\s*)?%\s*\)$")
_END = re.compile(r"^(study resources|change log|additional resources)$", re.I)
_ASIDE = re.compile(r"^(note|important|tip|warning|caution|audience profile|in this article)\b", re.I)
_VERSION = re.compile(r"^skills (measured|at a glance)\b", re.I)
_RETIRED = re.compile(rf"\bthis exam (?:was|will be|has been|is scheduled to be) retired on ({_FULL_DATE})", re.I)
_PASS = re.compile(r"\bA score of (\d{3}) or greater is required to pass\b", re.I)
_DURATION = re.compile(r"\bYou will have (\d{2,3}) minutes to complete this assessment\.", re.I)
_UPDATE = re.compile(rf"\bwill be updated on ({_FULL_DATE})", re.I)
_MD_LINK = re.compile(r"\[([^\]]{3,80})\]\s*\((/[^)\s]+)\)")
_CERT_PATH = re.compile(r"^/en-us/credentials/certifications/([a-z0-9-]{3,80})/?$")


def normalize_code(raw: str) -> str:
    """'az104', 'AZ 104' and 'Exam AZ-104' are all 'AZ-104'."""
    text = str(raw or "").strip()
    compact = re.sub(r"[\s\-\u2013\u2014_]+", "", text[:40]).upper()
    m = _CODE.match(re.sub(r"^EXAM", "", compact))
    if len(text) > 40 or not m:
        raise UserError(CODE_HELP)
    return f"{m.group(1)}-{m.group(2)}"


def issuer(code: str) -> str:
    return "GitHub" if code.startswith("GH-") else "Microsoft"


def guide_url(code: str) -> str:
    return GUIDE_URL.format(code=code.lower())


def exam_url(code: str) -> str:
    return EXAM_URL.format(code=code.lower())


def parse_date(text: str) -> str | None:
    """'October 27, 2026' is '2026-10-27'. 'July 2026' and 'January, 2026' name only a month: '2026-07'."""
    m = _DATE.search(text or "")
    if not m:
        return None
    month = [x.lower() for x in MONTHS].index(m.group(1).lower()) + 1
    year = int(m.group(3))
    if not m.group(2):
        return f"{year:04d}-{month:02d}"
    try:
        return date(year, month, int(m.group(2))).isoformat()
    except ValueError:
        return None


def after(value: str | None, today: date) -> bool:
    """Whether a date from a page ('2026-10-27', or the month '2026-10') lies after today."""
    if not value:
        return False
    parts = [int(x) for x in value.split("-")]
    if len(parts) == 2:
        return (parts[0], parts[1]) > (today.year, today.month)
    return date(*parts) > today


def resolve(href: str, base: str) -> str | None:
    """An absolute link on an allowed host, or None. Learn paths without a locale get /en-us."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "javascript:")):
        return None
    url = urljoin(base, href)
    u = urlparse(url)
    if u.hostname == "learn.microsoft.com" and u.path.startswith("/credentials/"):
        url = u._replace(path="/en-us" + u.path).geturl()
    try:
        return check_url(url.split("#")[0])
    except UserError:
        return None


def _main(page: str) -> str:
    m = re.search(r"<main\b[^>]*>(.*)</main>", page, re.S | re.I)
    return m.group(1) if m else page


def _clean(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


class _Blocks(HTMLParser):
    """A page as a flat list of blocks in reading order: headings, list items, paragraphs and table
    rows, each with its text and links. It reads what `sources.html_to_text` reads, so a text taken
    from a block can be found again in the page's text."""
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button",
            "template", "iframe", "select", "option"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.links: list[tuple[str, str]] = []   # every link in the article: (href, text)
        self.skip = 0
        self.lists = 0
        self.sinks: list[dict] = []
        self.row: dict | None = None
        self.cell: dict | None = None
        self.link: dict | None = None
        self.strong = 0

    def _open(self, block: dict, listed: bool = True) -> None:
        if self.sinks and self.sinks[-1]["kind"] == "p":
            self._close("p")
        block.update(parts=[], links=[])
        if listed:
            self.blocks.append(block)
        self.sinks.append(block)

    def _close(self, kind: str) -> None:
        for i in range(len(self.sinks) - 1, -1, -1):
            if self.sinks[i]["kind"] == kind:
                block = self.sinks.pop(i)
                block["text"] = _clean(block.pop("parts"))
                return

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in self.HEADINGS:
            self._open({"kind": "h", "level": int(tag[1])})
        elif tag in ("ul", "ol"):
            self.lists += 1
        elif tag == "li":
            self._open({"kind": "li", "depth": self.lists, "in_table": self.cell is not None}, listed=self.cell is None)
        elif tag == "p" and not self.sinks and self.cell is None:
            self._open({"kind": "p"})
        elif tag == "tr":
            self.row = {"kind": "tr", "cells": []}
            self.blocks.append(self.row)
        elif tag in ("td", "th"):
            self.cell = {"head": tag == "th", "parts": [], "strong_parts": [], "links": []}
            if self.row is not None:
                self.row["cells"].append(self.cell)
        elif tag == "a":
            self.link = {"href": dict(attrs).get("href") or "", "parts": []}
        elif tag in ("strong", "b"):
            self.strong += 1
        elif tag == "br":
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in self.HEADINGS:
            self._close("h")
        elif tag in ("ul", "ol"):
            self.lists = max(0, self.lists - 1)
        elif tag in ("li", "p"):
            self._close(tag)
        elif tag in ("td", "th") and self.cell is not None:
            cell = self.cell
            cell["text"] = _clean(cell.pop("parts"))
            strong = _clean(cell.pop("strong_parts"))
            cell["strong"] = bool(strong) and strong == cell["text"]
            self.cell = None
        elif tag == "tr":
            self.row = None
        elif tag == "a" and self.link is not None:
            link = (self.link["href"], _clean(self.link["parts"]))
            self.links.append(link)
            if self.sinks:
                self.sinks[-1]["links"].append(link)
            if self.cell is not None:
                self.cell["links"].append(link)
            self.link = None
        elif tag in ("strong", "b"):
            self.strong = max(0, self.strong - 1)

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.sinks:
            self.sinks[-1]["parts"].append(data)
        if self.cell is not None:
            self.cell["parts"].append(data)
            if self.strong:
                self.cell["strong_parts"].append(data)
        if self.link is not None:
            self.link["parts"].append(data)

    def result(self) -> list[dict]:
        while self.sinks:
            self._close(self.sinks[-1]["kind"])
        for b in self.blocks:
            if b["kind"] == "tr":
                for cell in b["cells"]:
                    if "parts" in cell:
                        cell["text"] = _clean(cell.pop("parts"))
                        cell.pop("strong_parts", None)
                        cell["strong"] = False
            else:
                b.setdefault("text", "")
        return self.blocks


class _Meta(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "meta" and a.get("name") in ("ms.date", "updated_at", "git_commit_id") and a.get("content"):
            self.meta[a["name"]] = str(a["content"])[:80]


def read(page: str) -> tuple[list[dict], list[tuple[str, str]]]:
    """The article's blocks and every link in it."""
    parser = _Blocks()
    parser.feed(_main(page))
    parser.close()
    return parser.result(), parser.links


def page_meta(page: str) -> dict:
    parser = _Meta()
    m = re.search(r"<main\b", page, re.I)
    parser.feed(page[:m.start()] if m else page[:200_000])
    parser.close()
    return parser.meta


def _row_links(row: dict, base: str) -> list[tuple[str, str]]:
    """(url, label) for every allowed link in a table row, including a link Learn failed to render
    and shows as markdown text, such as '[How to earn the certification] (/credentials/...)'."""
    out = []
    for cell in row["cells"]:
        for href, label in cell["links"]:
            url = resolve(href, base)
            if url:
                out.append((url, label))
        for label, path in _MD_LINK.findall(cell.get("text", "")):
            url = resolve(path, base)
            if url:
                out.append((url, label.strip()))
    return out


def cert_slug(url: str) -> str | None:
    m = _CERT_PATH.match(urlparse(url).path)
    if not m or m.group(1) in ("resources", "exams", "renew-your-microsoft-certification", "exam-scoring-reports",
                               "request-accommodations", "accommodations", "support", "browse", "exam-duration-exam-experience"):
        return None
    return m.group(1)


def parse_guide(page: str, code: str, url: str, today: date) -> dict:
    """The blueprint on a study guide page. `problems` lists what makes the page unusable: when it is
    not empty, nothing from the page may be built into a package."""
    items, _ = read(page)
    text = html_to_text(page)
    flat = normalize(text)
    out: dict = {
        "code": code, "url": url, "name": None, "title": None, "as_of": None, "as_of_text": None, "domains": [],
        "skills": 0, "groups": 0, "glance": [], "notes": [], "problems": [], "retirement": None, "passing_score": None,
        "cert_links": [], "practice_links": [], "resources": [], "change_log": None, "meta": page_meta(page),
        "text_sha256": sha256(text),
    }
    problems, notes = out["problems"], out["notes"]
    h1 = next((b for b in items if b["kind"] == "h" and b["level"] == 1), None)
    m = _TITLE.match(h1["text"]) if h1 else None
    if not m:
        problems.append("The page has no title of the form 'Study guide for Exam XX-000: name'.")
        return out
    if m.group(1).upper() != code:
        problems.append(f"The page is the study guide for {m.group(1).upper()}, not {code}.")
        return out
    out["name"], out["title"] = h1["text"], m.group(2).strip()

    retired = _RETIRED.search(text)
    if retired:
        when = parse_date(retired.group(1))
        out["retirement"] = {"date": when, "text": retired.group(0), "past": bool(when) and not after(when, today)}
    passing = _PASS.search(text)
    out["passing_score"] = int(passing.group(1)) if passing else None

    domains: list[dict] = []
    glance: list[str] = []
    section, group, aside, glancing, nested = "intro", None, False, False, 0
    change: dict = {"prior": None, "as_of": None, "rows": [], "text": []}
    for b in items[items.index(h1) + 1:]:
        kind = b["kind"]
        if kind == "h":
            t = b["text"]
            if _END.match(t):
                section = {"study resources": "resources", "change log": "changes"}.get(t.lower(), "after")
                continue
            if section not in ("intro", "skills"):
                continue
            w = _WEIGHT.match(t)
            if w:
                section, group, aside, glancing = "skills", None, False, False
                lo, hi = int(w.group(2)), int(w.group(3) or w.group(2))
                domains.append({"title": w.group(1).strip(), "heading": t, "weight_min": lo, "weight_max": hi, "groups": []})
            elif section == "intro":
                if _VERSION.match(t):
                    when = parse_date(t)
                    if when and (out["as_of"] is None or t.lower().startswith("skills measured")):
                        out["as_of"], out["as_of_text"] = when, t
                glancing = bool(re.match(r"^skills at a glance\b", t, re.I))
            elif _VERSION.match(t):
                notes.append(f"The page lists a second version of the skills under '{t}'; Dojo used the first.")
                section = "after"
            elif _ASIDE.match(t):
                group, aside = None, True
            else:
                group, aside = {"title": t, "skills": []}, False
                domains[-1]["groups"].append(group)
        elif kind == "li":
            if section == "intro" and glancing and b["depth"] == 1:
                glance.append(b["text"])
            elif section == "skills" and not aside and b["text"]:
                if b["depth"] > 1:
                    nested += 1
                    continue
                if group is None:
                    group = {"title": domains[-1]["title"], "skills": []}
                    domains[-1]["groups"].append(group)
                    notes.append(f"'{domains[-1]['title']}' lists skills without a group heading; Dojo grouped them under the domain's title.")
                group["skills"].append(b["text"])
        elif kind == "tr":
            if section == "intro":
                for link, label in _row_links(b, url):
                    low = label.lower()
                    if "how to earn" in low and link not in out["cert_links"]:
                        out["cert_links"].append(link)
                    elif "practice assessment" in low and link not in out["practice_links"]:
                        out["practice_links"].append(link)
            elif section == "resources":
                links = [{"url": link, "title": label or link} for link, label in _row_links(b, url)]
                if links and b["cells"] and not b["cells"][0]["head"]:
                    out["resources"].append({"label": b["cells"][0]["text"][:80], "links": links[:12]})
            elif section == "changes":
                cells = b["cells"]
                if cells and all(c["head"] for c in cells):
                    for c in cells:
                        low = c["text"].lower()
                        if "prior to" in low:
                            change["prior"] = parse_date(c["text"])
                        elif "as of" in low:
                            change["as_of"] = parse_date(c["text"])
                elif len(cells) >= 3 and len(change["rows"]) < 300:
                    change["rows"].append({"prior": cells[0]["text"][:300], "current": cells[1]["text"][:300],
                                           "change": cells[2]["text"][:80], "area": cells[0]["strong"] or cells[1]["strong"]})
        elif kind == "p" and section == "changes" and b["text"] and len(change["text"]) < 6:
            change["text"].append(b["text"][:600])
    if nested:
        notes.append(f"Dojo did not take {nested} indented bullet point{'s' if nested > 1 else ''} under the skills "
                     "as skills of their own.")

    groups = [g for d in domains for g in d["groups"]]
    skills = [s for g in groups for s in g["skills"]]
    out.update(domains=domains, glance=glance, groups=len(groups), skills=len(skills))
    changed_on = change["as_of"] or next((parse_date(p) for p in change["text"] if parse_date(p)), None)
    if change["rows"] or change["text"]:
        out["change_log"] = {**change, "changed": changed_on}

    if not domains:
        problems.append("Dojo found no skills measured on this page: no heading with a weight such as (20\u201325%).")
        return out
    for d in domains:
        if not any(g["skills"] for g in d["groups"]):
            problems.append(f"The section '{d['title']}' lists no skills.")
        if not 0 <= d["weight_min"] <= d["weight_max"] <= 100:
            problems.append(f"The weight of '{d['title']}' is not a range between 0 and 100%.")
        d["groups"] = [g for g in d["groups"] if g["skills"]]
    low, high = sum(d["weight_min"] for d in domains), sum(d["weight_max"] for d in domains)
    if not low <= 100 <= high:
        problems.append(f"The weights on the page do not add up to 100% (they range from {low}% to {high}%).")
    titles = [normalize(d["title"]) for d in domains]
    if len(set(titles)) != len(titles):
        problems.append("Two sections on the page have the same title.")
    if not 3 <= len(skills) <= MAX_SKILLS:
        problems.append(f"The page lists {len(skills)} skills; Dojo expects between 3 and {MAX_SKILLS}.")
    for t in [d["heading"] for d in domains] + [g["title"] for g in groups] + skills:
        if len(t) < 3 or len(t) > 600 or normalize(t) not in flat:
            problems.append(f"'{t[:80]}' was not found word for word on the page.")
            break
    _cross_check(domains, glance, notes)
    return out


def _cross_check(domains: list[dict], glance: list[str], notes: list[str]) -> None:
    """The 'Skills at a glance' list repeats the sections with their weights. The sections win; any
    difference is noted, never silently repaired."""
    by_title = {normalize(d["title"]): d for d in domains}
    listed: set[str] = set()
    for line in glance:
        w = _WEIGHT.match(line)
        if not w:
            continue
        key = normalize(w.group(1))
        lo, hi = int(w.group(2)), int(w.group(3) or w.group(2))
        d = by_title.get(key)
        if d is None:
            notes.append(f"Skills at a glance lists '{line}', which has no section of its own on the page; "
                         f"Dojo uses the {len(domains)} sections.")
        elif (lo, hi) != (d["weight_min"], d["weight_max"]):
            notes.append(f"Skills at a glance gives '{line}', but the section heading says '{d['heading']}'; Dojo uses the section heading.")
        listed.add(key)
    if glance:
        for key, d in by_title.items():
            if key not in listed:
                notes.append(f"Skills at a glance does not list '{d['title']}'.")


def parse_cert_page(page: str, url: str, code: str) -> dict:
    """What a certification page states about one exam: its name, the time allowed and the official
    practice assessment. `mentions_code` says whether the page names the exam at all."""
    items, links = read(page)
    text = html_to_text(page)
    h1 = next((b["text"] for b in items if b["kind"] == "h" and b["level"] == 1), "")
    out: dict = {"url": url, "slug": cert_slug(url), "name": h1[:200] or None, "duration_minutes": None, "duration_quote": None,
                 "practice": None, "update": None, "update_quote": None, "level": None, "level_quote": None,
                 "mentions_code": re.search(rf"(?<![A-Za-z0-9-]){re.escape(code)}(?![0-9])", text) is not None}
    # The level decides the open-book rule (ADR 0012): from the certification's name, else from its badge.
    lv = re.search(r"\b(Fundamentals|Associate|Expert)\s*$", h1.strip())
    badge = re.search(r"microsoft-certified-(fundamentals|associate|expert)-badge", page, re.I)
    if lv:
        out["level"], out["level_quote"] = lv.group(1).lower(), h1.strip()[:200]
    elif badge:
        out["level"], out["level_quote"] = badge.group(1).lower(), badge.group(0)
    d = _DURATION.search(text)
    if d and 15 <= int(d.group(1)) <= 600:
        out["duration_minutes"], out["duration_quote"] = int(d.group(1)), d.group(0)
    u = _UPDATE.search(text)
    if u:
        out["update"], out["update_quote"] = parse_date(u.group(1)), u.group(0)
    for href, label in links:
        if "practice/assessment" in href and "assessment-type=practice" in href:
            link = resolve(href, url)
            if link:
                ids = parse_qs(urlparse(link).query).get("assessmentId") or [""]
                out["practice"] = {"url": link, "assessment_id": re.sub(r"[^0-9A-Za-z-]", "", ids[0])[:20], "label": label[:120]}
                break
    return out


def practice_for(link: str, code: str, slug: str | None) -> bool:
    """A practice assessment link belongs to this exam only when its path names the exam or the
    certification the exam page confirmed. The DP-800 study guide once linked the AZ-104 assessment."""
    path = urlparse(link).path.lower()
    if "/practice/assessment" not in path:
        return False
    return f"/exams/{code.lower()}/" in path or bool(slug and f"/certifications/{slug}/" in path)
