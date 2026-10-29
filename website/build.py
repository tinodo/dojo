"""Builds the project site (GitHub Pages) from the repository:  python website/build.py --out _site

Standard library only. The site is a pure function of the repository: the templates, the stylesheet and the
hand-written lists under website/, the exam package files, docs/adr, the app's fonts and icon, four constants
read from the code, and the commit. It never reads learner data, the live app or the network.
What the site holds and never holds, and how it is kept true: docs/adr/0027-project-site.md.

Placeholders in a template:
  {{name}}        a text value, HTML-escaped
  {{html:name}}   HTML this program built (every value inside it was escaped)
  {{blob:path}}   the address of a file of this repository on GitHub; the build fails when the file does not exist
  {{page:name}}   a link to a page of the site, right from the page it is written on
An unknown placeholder, or one left unfilled, fails the build.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "website"
DEFAULT_REPO = "tinodo/dojo"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}

PAGES = {
    "home": {"dir": "", "template": "index.html", "nav": None,
             "title": "Dojo: an exam coach that teaches from the official documentation",
             "description": "Dojo writes a lesson for each exam skill from the official pages that teach it, asks you open questions, judges your "
                            "answers point by point, and keeps a record of what you have shown. Open source, for Microsoft and GitHub certification exams."},
    "exams": {"dir": "exams/", "template": "exams.html", "nav": "Exams",
              "title": "Exams | Dojo", "description": "The exams that come with Dojo, with the domains and weights of their official study guides."},
    "docs": {"dir": "docs/", "template": "docs.html", "nav": "Documentation",
             "title": "Documentation | Dojo", "description": "The user guide, architecture, hosting and privacy documents of Dojo, and where to read them."},
    "decisions": {"dir": "decisions/", "template": "decisions.html", "nav": "Decisions",
                  "title": "Decision records | Dojo", "description": "The decisions that shape Dojo, each with its status."},
}
NOT_FOUND = {"template": "404.html", "title": "Page not found | Dojo"}
TOKEN = re.compile(r"\{\{(?:(html|blob|page):)?([A-Za-z0-9_./#-]+)\}\}")


class BuildError(Exception):
    pass


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def inline(text: str) -> str:
    """A line of Markdown with only `code` in it, as escaped HTML."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


def plural(n: int, word_: str) -> str:
    return f"{n} {word_}" + ("" if n == 1 else "s")


def word(n: int) -> str:
    return WORDS.get(n, str(n))


def read(path: str, pattern: str) -> str:
    m = re.search(pattern, (ROOT / path).read_text("utf-8"), re.M)
    if not m:
        raise BuildError(f"cannot read {pattern!r} from {path}: the code changed, so website/build.py must change with it")
    return m.group(1)


# ---------------------------------------------------------------------------------------------- what the site is told

class Site:
    def __init__(self, slug: str, site_url: str, sha: str, built_on: str):
        self.slug = slug
        self.repo_url = f"https://github.com/{slug}"
        self.site_url = site_url if site_url.endswith("/") else site_url + "/"
        self.base = urlparse(self.site_url).path or "/"
        self.sha = sha
        self.built_on = built_on

    def blob(self, spec: str) -> str:
        """The address on GitHub of a file of this repository. The path must be spelled the way the repository spells it (no
        '..', no backslash, the same capitals): a file that only the local disk finds would be a dead link on GitHub."""
        path, _, anchor = spec.partition("#")
        target = (ROOT / path).resolve() if path else ROOT
        try:
            spelled = target.relative_to(ROOT).as_posix()
        except ValueError:
            spelled = ""
        if not path or spelled != path or not target.is_file():
            raise BuildError(f"{{{{blob:{spec}}}}}: not a file of the repository, spelled as the repository spells it")
        return f"{self.repo_url}/blob/main/{spelled}" + (f"#{anchor}" if anchor else "")


def commit_facts(sha: str | None) -> tuple[str, str]:
    """The commit this site is built from and its date (a date, not 'now', so a build can be repeated)."""
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    sha = sha or git("rev-parse", "HEAD")
    if not sha:
        return "", ""
    day = git("show", "-s", "--format=%cI", sha)[:10]
    try:
        d = date.fromisoformat(day)
        return sha, f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    except ValueError:
        return sha, ""


def constants() -> dict[str, str]:
    """Numbers the site quotes, read from where the code keeps them."""
    max_sources = int(read("app/packages.py", r"^MAX_SOURCES\s*=\s*(\d+)"))
    later = int(read("app/evidence.py", r"^def derive\(.*later_hours: int = (\d+)"))
    recheck = int(read("app/drift.py", r"^RECHECK_HOURS\s*=\s*(\d+)"))
    missing = int(read("app/drift.py", r"^MISSING_RETRY_HOURS\s*=\s*(\d+)"))
    return {
        "max_sources_word": word(max_sources),
        "later_hours": str(later),
        "recheck_phrase": "about once a day" if recheck == 24 else f"about every {recheck} hours",
        "missing_phrase": f"after {word(missing)} hours",
        "python_version": read(".github/workflows/ci.yml", r'python-version:\s*"([\d.]+)"'),
    }


def long_date(iso: str | None) -> str:
    try:
        d = date.fromisoformat(iso or "")
    except ValueError:
        return ""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def weight(lo: int | None, hi: int | None) -> str:
    if lo is None or hi is None:
        return "not stated"
    return f"{lo}%" if lo == hi else f"{lo}-{hi}%"


def load_exams() -> list[dict]:
    """Named fields only: nothing else of a package file reaches the public site."""
    exams = []
    for f in sorted((ROOT / "app" / "packages").glob("*.json")):
        p = json.loads(f.read_text("utf-8"))
        domains = [{"title": d["title"], "weight": weight(d.get("weight_min"), d.get("weight_max")),
                    "skills": sum(len(g["skills"]) for g in d["groups"])} for d in p["domains"]]
        src = p.get("source") or {}
        exams.append({"id": p["id"], "code": p["exam"], "title": p["title"], "domains": domains,
                      "skills": sum(d["skills"] for d in domains), "guide": src.get("url"), "as_of": src.get("skills_as_of"),
                      "exam_page": p.get("exam_page")})
    return sorted(exams, key=lambda e: e["code"])


def adr_title(path: Path) -> str:
    first = path.read_text("utf-8").splitlines()[0]
    m = re.match(r"#\s*ADR\s+\d{4}\s*[-:]\s*(.+)$", first)
    if not m:
        raise BuildError(f"{path.name}: the first line is not '# ADR NNNN - title'")
    return m.group(1).strip()


def load_decisions() -> tuple[list[str], list[tuple[str, str, str, str]]]:
    """The README's opening paragraphs, and (number, file, title, status) for every record."""
    adr = ROOT / "docs" / "adr"
    overrides = json.loads((SRC / "content" / "decisions.json").read_text("utf-8"))
    lines = (adr / "README.md").read_text("utf-8").splitlines()
    head: list[str] = []
    for line in lines[1:]:
        if line.startswith("|"):
            break
        head.append(line)
    intro = [" ".join(p.split()) for p in re.split(r"\n\s*\n", "\n".join(head).strip()) if p.strip() and not p.lstrip().startswith("#")]
    rows = []
    for line in lines:
        if not line.startswith("| ["):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.match(r"\[(\d{4})\]\(([^)]+)\)$", cells[0])
        if not m or len(cells) < 3:
            raise BuildError(f"cannot read this row of docs/adr/README.md: {line[:70]}")
        number, name = m.groups()
        rows.append((number, name, overrides.get(number) or adr_title(adr / name), cells[-1]))
    if sorted(r[1] for r in rows) != sorted(p.name for p in adr.glob("0*.md")):
        raise BuildError("docs/adr/README.md and the ADR files do not list the same records")
    return intro, rows


def load_docs() -> dict:
    return json.loads((SRC / "content" / "docs.json").read_text("utf-8"))


# ---------------------------------------------------------------------------------------------- pieces of HTML

def builtin_line(exams: list[dict]) -> str:
    names = [f"{e['code']} {e['title']}" for e in exams]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return esc(f"Built in: {joined}. Any other Microsoft or GitHub exam with an official study guide can be added.")


def exam_list(exams: list[dict], prefix: str) -> str:
    rows = []
    for e in exams:
        meta = f"{plural(e['skills'], 'skill')} in {plural(len(e['domains']), 'domain')}"
        rows.append(f'<li><span class="code">{esc(e["code"])}</span><span class="etitle">{esc(e["title"])}</span>'
                    f'<span class="emeta">{esc(meta)} &middot; <a href="{esc(prefix)}exams/#{esc(e["id"])}">Domains and weights</a></span></li>')
    return '<ul class="rows examlist">' + "".join(rows) + "</ul>"


def exam_sections(exams: list[dict]) -> str:
    out = []
    for e in exams:
        links = []
        if e["exam_page"]:
            links.append(f'<a href="{esc(e["exam_page"])}">Official exam page</a>')
        if e["guide"]:
            links.append(f'<a href="{esc(e["guide"])}">Official study guide</a>')
        rows = "".join(f'<tr><td>{esc(d["title"])}</td><td class="num">{esc(d["weight"])}</td><td class="num">{d["skills"]}</td></tr>'
                       for d in e["domains"])
        as_of = f"Study guide skills as of {long_date(e['as_of'])}. " if long_date(e["as_of"]) else ""
        out.append(
            f'<section class="exam" id="{esc(e["id"])}"><h2>{esc(e["code"])} &middot; {esc(e["title"])}</h2>'
            f'<p class="meta">{esc(as_of)}{esc(plural(e["skills"], "skill"))} in {esc(plural(len(e["domains"]), "domain"))}.</p>'
            f'<p class="links">{" ".join(links)}</p>'
            f'<div class="tablewrap"><table><caption class="sr">Domains of {esc(e["code"])} with their weights</caption>'
            f'<thead><tr><th scope="col">Domain</th><th scope="col" class="num">Weight</th><th scope="col" class="num">Skills</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></section>')
    return "".join(out)


def docs_list(docs: dict, site: Site, prefix: str) -> str:
    items = []
    for d in docs["items"]:
        href = f"{prefix}{PAGES[d['page']]['dir']}" if "page" in d else site.blob(d["path"])
        items.append(f'<li><a href="{esc(href)}">{esc(d["label"])}</a><p>{esc(d["text"])}</p></li>')
    return '<ul class="doclist">' + "".join(items) + "</ul>"


def decisions_intro(paragraphs: list[str]) -> str:
    return "".join(f"<p>{inline(p)}</p>" for p in paragraphs)


def decisions_table(rows: list[tuple[str, str, str, str]], site: Site) -> str:
    body = "".join(f'<tr><td class="num"><a href="{esc(site.blob("docs/adr/" + name))}">{esc(number)}</a></td>'
                   f'<td>{esc(title)}</td><td>{esc(status)}</td></tr>' for number, name, title, status in rows)
    return ('<div class="tablewrap"><table><caption class="sr">Decision records with their status</caption>'
            '<thead><tr><th scope="col" class="num">Record</th><th scope="col">Decision</th><th scope="col">Status</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def nav(site: Site, prefix: str, current: str | None) -> str:
    items = []
    for key, page in PAGES.items():
        if page["nav"]:
            cur = ' aria-current="page"' if key == current else ""
            items.append(f'<li><a href="{esc(prefix + page["dir"])}"{cur}>{esc(page["nav"])}</a></li>')
    items.append(f'<li><a href="{esc(site.repo_url)}">GitHub</a></li>')
    return "<ul>" + "".join(items) + "</ul>"


# ---------------------------------------------------------------------------------------------- the build

def fill(text: str, site: Site, prefix: str, values: dict[str, str], fragments: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        kind, name = m.groups()
        if kind == "html":
            if name not in fragments:
                raise BuildError(f"unknown placeholder {{{{html:{name}}}}}")
            return fragments[name]
        if kind == "blob":
            return esc(site.blob(name))
        if kind == "page":
            if name not in PAGES:
                raise BuildError(f"unknown placeholder {{{{page:{name}}}}}")
            return esc(prefix + PAGES[name]["dir"])
        if name not in values:
            raise BuildError(f"unknown placeholder {{{{{name}}}}}")
        return esc(values[name])
    return TOKEN.sub(sub, text)


def template(name: str) -> str:
    return (SRC / "templates" / name).read_text("utf-8")


def render_page(site: Site, key: str | None, prefix: str, data: dict, title: str, description: str, tpl: str) -> str:
    fragments = {
        "builtin_line": builtin_line(data["exams"]),
        "exam_list": exam_list(data["exams"], prefix),
        "exam_sections": exam_sections(data["exams"]),
        "docs_list": docs_list(data["docs"], site, prefix),
        "adr_intro": decisions_intro(data["adr_intro"]),
        "adr_table": decisions_table(data["adr_rows"], site),
    }
    values = {**data["constants"], "repo_url": site.repo_url, "exam_count_word": word(len(data["exams"]))}
    main = fill(template(tpl), site, prefix, values, fragments)
    if key:
        canonical = site.site_url + PAGES[key]["dir"]
        head_extra = (f'<link rel="canonical" href="{esc(canonical)}">'
                      f'<meta property="og:type" content="website"><meta property="og:title" content="{esc(title)}">'
                      f'<meta property="og:description" content="{esc(description)}"><meta property="og:url" content="{esc(canonical)}">')
    else:
        head_extra = '<meta name="robots" content="noindex">'
    built = (f"Built from commit <code>{esc(site.sha[:7])}</code>" + (f" on {esc(site.built_on)}" if site.built_on else "") + "."
             if site.sha else "Built locally, not from a commit.")
    layout_fragments = {"nav": nav(site, prefix, key), "main": main, "head_extra": head_extra, "built_line": built}
    layout_values = {"title": title, "description": description, "prefix": prefix, "repo_url": site.repo_url}
    page = fill(template("layout.html"), site, prefix, layout_values, layout_fragments)
    if "{{" in page:
        raise BuildError(f"{title}: a placeholder was left unfilled")
    return page


def sitemap(site: Site) -> str:
    urls = [site.site_url] + [site.site_url + PAGES[k]["dir"] for k in ("exams", "docs", "decisions")]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <url><loc>{esc(u)}</loc></url>\n" for u in urls) + "</urlset>\n")


def copy_file(src: Path, dst: Path) -> None:
    if src.is_symlink() or not src.is_file():
        raise BuildError(f"{src}: only regular files are copied")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def build(out: Path, slug: str | None = None, site_url: str | None = None, sha: str | None = None, clean: bool = False) -> list[str]:
    slug = slug or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    owner, _, name = slug.partition("/")
    site_url = site_url or os.environ.get("SITE_URL") or f"https://{owner}.github.io/{name}/"
    sha, built_on = commit_facts(sha or os.environ.get("GITHUB_SHA"))
    site = Site(slug, site_url, sha, built_on)
    out = out.resolve()
    if out.exists() and any(out.iterdir()):
        if not (clean and out == ROOT / "_site"):
            raise BuildError(f"{out} is not empty; remove it first (--clean empties only {ROOT / '_site'})")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    intro, rows = load_decisions()
    data = {"exams": load_exams(), "docs": load_docs(), "constants": constants(), "adr_intro": intro, "adr_rows": rows}

    for key, page in PAGES.items():
        prefix = "./" if not page["dir"] else "../"
        text = render_page(site, key, prefix, data, page["title"], page["description"], page["template"])
        target = out / page["dir"] / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8", newline="\n")
    # GitHub serves 404.html at whatever address was asked for, so its links start at the base of the site.
    (out / "404.html").write_text(
        render_page(site, None, site.base, data, NOT_FOUND["title"], "This page is not on the Dojo project site.", NOT_FOUND["template"]),
        "utf-8", newline="\n")
    (out / "sitemap.xml").write_text(sitemap(site), "utf-8", newline="\n")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {site.site_url}sitemap.xml\n", "utf-8", newline="\n")

    copy_file(SRC / "assets" / "site.css", out / "assets" / "site.css")
    copy_file(ROOT / "app" / "static" / "icon.svg", out / "assets" / "icon.svg")
    copy_file(ROOT / "app" / "static" / "fonts.css", out / "assets" / "fonts.css")
    for f in sorted((ROOT / "app" / "static" / "fonts").iterdir()):
        copy_file(f, out / "assets" / "fonts" / f.name)
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Dojo project site.")
    ap.add_argument("--out", default="_site", help="folder to write (default _site)")
    ap.add_argument("--clean", action="store_true", help="empty the repository's own _site folder first")
    args = ap.parse_args()
    try:
        files = build(Path(args.out), clean=args.clean)
    except BuildError as e:
        print(f"website/build.py: {e}", file=sys.stderr)
        return 1
    print(f"Built {len(files)} files into {args.out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
