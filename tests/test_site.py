"""The project site (website/, ADR 0027): it builds, every link resolves from its own address, nothing private reaches a
public page, and the wording and the look keep the rules of the app (docs/ui-principles.md, rules 11 and 12).

The site is public, so these tests are stricter than the ones for a screen behind a sign-in: the list of files it may
write is fixed, pattern checks run on every text file, and the pages may link and load only what is named here."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from tests.test_ui_rules import BANNED, NEUTRAL_LEFT, rules

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "https://tinodo.github.io/dojo/"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUILD = load("dojo_site_build", ROOT / "website" / "build.py")
LINKS = load("dojo_site_links", ROOT / "website" / "check_links.py")
SCAN = load("dojo_secret_scan", ROOT / "tools" / "secret_scan.py")

EXPECTED = sorted([
    "404.html", "index.html", "exams/index.html", "docs/index.html", "decisions/index.html", "robots.txt", "sitemap.xml",
    "assets/site.css", "assets/icon.svg", "assets/fonts.css",
    "assets/fonts/Fraunces-OFL.txt", "assets/fonts/Fraunces-italic.woff2", "assets/fonts/Fraunces-normal.woff2",
    "assets/fonts/Inter-OFL.txt", "assets/fonts/Inter-normal.woff2",
    "assets/fonts/JetBrainsMono-OFL.txt", "assets/fonts/JetBrainsMono-normal.woff2",
])
CSP = "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'"
KNOWN_HOSTS = {"github.com", "learn.microsoft.com", "docs.github.com"}
# Words from the insides of Dojo (docs/ui-principles.md, rule 6): not on a page a visitor reads.
JARGON = [r"\bgate[ds]?\b", r"\bdrift\b", r"\boverlay\b", r"\btemplates?\b", r"\btokens?\b", r"\bWER\b", r"\bheld\b", r"\bTODO\b", r"lorem ipsum"]
# Things that must never be on a public page. The repository holds the owner's real app and resource names.
PRIVATE = {
    "a GUID": r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    "an App Service address": r"azurewebsites\.net",
    "the owner's app name": r"dojo-tinodo",
    "a resource group": r"\brg-dojo",
    "an Azure AI or SQL endpoint": r"services\.ai\.azure\.com|cognitiveservices\.azure\.com|database\.windows\.net",
    "a learner key": r"\bl[0-9a-f]{31}\b",
    "an email address": r"[\w.+-]+@[\w-]+\.[\w.-]+",
}
TEXT_FILES = (".html", ".css", ".svg", ".txt", ".xml")


class Parsed(HTMLParser):
    """What a page holds: its references, ids, headings, tables and visible text."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.lang = None
        self.title = ""
        self.refs: list[tuple[str, str, str]] = []
        self.ids: set[str] = set()
        self.headings: list[tuple[int, str]] = []
        self.metas: list[dict] = []
        self.tags: list[str] = []
        self.inline_style = 0
        self.handlers = 0
        self.th_scopes: list[str | None] = []
        self.skip_first = None
        self.visible: list[str] = []
        self._in_title = False
        self._in_body = False
        self._heading: tuple[int, list[str]] | None = None
        self._first_link_seen = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "title":
            self._in_title = True
        if tag == "body":
            self._in_body = True
        if tag == "meta":
            self.metas.append(a)
        if "id" in a:
            self.ids.add(a["id"])
        if "style" in a:
            self.inline_style += 1
        self.handlers += sum(1 for k in a if k.startswith("on"))
        for name in ("href", "src"):
            if a.get(name) is not None:
                self.refs.append((tag, name, a[name]))
        if tag == "a" and not self._first_link_seen:
            self._first_link_seen = True
            self.skip_first = (a.get("class"), a.get("href"))
        if tag == "th":
            self.th_scopes.append(a.get("scope"))
        if re.fullmatch(r"h[1-6]", tag):
            self._heading = (int(tag[1]), [])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self._heading and tag == f"h{self._heading[0]}":
            self.headings.append((self._heading[0], " ".join("".join(self._heading[1]).split())))
            self._heading = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_body:
            self.visible.append(data)
        if self._heading:
            self._heading[1].append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.visible).split())

    @property
    def csp(self) -> str:
        return next((m.get("content", "") for m in self.metas if m.get("http-equiv") == "Content-Security-Policy"), "")


def page_url(rel: str) -> str:
    """The address a built file is served at. GitHub serves 404.html at whatever address was asked for."""
    if rel == "404.html":
        return SITE_URL + "a/b/c"
    return SITE_URL + (rel[: -len("index.html")] if rel.endswith("index.html") else rel)


OUT: Path
FILES: list[str]
PAGES: dict[str, Parsed]
_tmp: tempfile.TemporaryDirectory


def setUpModule():
    global OUT, FILES, PAGES, _tmp
    _tmp = tempfile.TemporaryDirectory()
    OUT = Path(_tmp.name) / "_site"
    FILES = BUILD.build(OUT, slug="tinodo/dojo", site_url=SITE_URL, sha=SHA)
    PAGES = {rel: Parsed((OUT / rel).read_text("utf-8")) for rel in FILES if rel.endswith(".html")}


def tearDownModule():
    _tmp.cleanup()


def package_files() -> list[dict]:
    return [json.loads(f.read_text("utf-8")) for f in sorted((ROOT / "app" / "packages").glob("*.json"))]


class TheBuild(unittest.TestCase):
    def test_it_writes_exactly_the_files_that_are_expected(self):
        self.assertEqual(FILES, EXPECTED)

    def test_it_can_be_repeated_and_gives_the_same_site(self):
        with tempfile.TemporaryDirectory() as other:
            again = Path(other) / "_site"
            BUILD.build(again, slug="tinodo/dojo", site_url=SITE_URL, sha=SHA)
            for rel in FILES:
                self.assertEqual((OUT / rel).read_bytes(), (again / rel).read_bytes(), rel)

    def test_it_will_not_write_into_a_folder_that_is_not_empty(self):
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / "keep.txt").write_text("x")
            with self.assertRaises(BUILD.BuildError):
                BUILD.build(Path(other), slug="tinodo/dojo", site_url=SITE_URL, sha=SHA)

    def test_a_placeholder_that_is_unknown_or_points_nowhere_fails_the_build(self):
        site = BUILD.Site("tinodo/dojo", SITE_URL, "", "")
        for text in ("{{nope}}", "{{html:nope}}", "{{page:nope}}", "{{blob:no/such/file.md}}"):
            with self.assertRaises(BUILD.BuildError, msg=text):
                BUILD.fill(text, site, "./", {}, {})

    def test_values_are_escaped_and_blob_and_page_links_are_made_right(self):
        site = BUILD.Site("tinodo/dojo", SITE_URL, "", "")
        self.assertEqual(BUILD.fill("{{x}}", site, "./", {"x": "<b>&\""}, {}), "&lt;b&gt;&amp;&quot;")
        self.assertEqual(BUILD.fill("{{blob:LICENSE}}", site, "./", {}, {}), "https://github.com/tinodo/dojo/blob/main/LICENSE")
        self.assertEqual(BUILD.fill("{{page:exams}}", site, "../", {}, {}), "../exams/")
        self.assertEqual(BUILD.fill("{{page:home}}", site, "/dojo/", {}, {}), "/dojo/")

    def test_the_numbers_it_quotes_are_read_from_the_code(self):
        c = BUILD.constants()
        text = PAGES["index.html"].text
        self.assertIn(f"up to {c['max_sources_word']} official pages", text)
        self.assertIn(f"at least {c['later_hours']} hours after you were last taught it", text)
        self.assertIn(f"Dojo reads each cited page again {c['recheck_phrase']} ({c['missing_phrase']} if a page was missing or moved)", text)
        self.assertIn(f"Python {c['python_version']} and Git", text)

    def test_facts_that_are_written_by_hand_still_hold_in_the_documents_they_came_from(self):
        ties = [("up to about USD 23 a month", "USD 23", "docs/operations.md"), ("up to 30 colleagues", "up to 30", "docs/user-guide.md")]
        text = PAGES["index.html"].text
        for on_site, in_doc, source in ties:
            self.assertIn(on_site, text)
            self.assertIn(in_doc, (ROOT / source).read_text("utf-8"), f"{source} no longer says {in_doc!r}: update the site")


class EveryPage(unittest.TestCase):
    def test_each_page_has_the_basics(self):
        for rel, page in PAGES.items():
            with self.subTest(rel):
                self.assertEqual(page.lang, "en")
                self.assertTrue(page.title.strip())
                self.assertEqual(sum(1 for lvl, _ in page.headings if lvl == 1), 1, "one H1")
                self.assertEqual(page.headings[0][0], 1, "the first heading is the H1")
                for (a, _), (b, _) in zip(page.headings, page.headings[1:]):
                    self.assertLessEqual(b, a + 1, "no heading level is skipped")
                self.assertEqual(page.tags.count("main"), 1)
                self.assertIn("main", page.ids)
                self.assertEqual(page.skip_first, ("skip", "#main"), "the first link is Skip to content")
                self.assertNotIn(None, page.th_scopes)
                self.assertEqual(page.th_scopes.count("col"), len(page.th_scopes), "table headers say they are columns")
                self.assertNotIn("{{", (OUT / rel).read_text("utf-8"))

    def test_a_page_is_found_by_its_canonical_address_and_the_404_is_not_indexed(self):
        for rel, page in PAGES.items():
            canonical = [v for t, n, v in page.refs if t == "link" and n == "href" and v.startswith(SITE_URL) and not v.endswith(".css")]
            if rel == "404.html":
                self.assertTrue(any(m.get("name") == "robots" and m.get("content") == "noindex" for m in page.metas))
            else:
                self.assertIn(page_url(rel), canonical, rel)

    def test_the_sitemap_and_robots_name_the_four_pages(self):
        sitemap = (OUT / "sitemap.xml").read_text("utf-8")
        self.assertEqual(re.findall(r"<loc>([^<]+)</loc>", sitemap), [SITE_URL, SITE_URL + "exams/", SITE_URL + "docs/", SITE_URL + "decisions/"])
        self.assertIn(f"Sitemap: {SITE_URL}sitemap.xml", (OUT / "robots.txt").read_text("utf-8"))

    def test_the_content_security_policy_is_set_and_nothing_inline_or_foreign_is_loaded(self):
        for rel, page in PAGES.items():
            with self.subTest(rel):
                self.assertEqual(page.csp, CSP)
                self.assertEqual((page.inline_style, page.handlers), (0, 0), "no style= and no on...= attribute")
                for tag in ("script", "style", "iframe", "img", "object", "embed", "form", "base"):
                    self.assertNotIn(tag, page.tags)

    def test_every_link_and_asset_resolves_from_the_address_the_page_is_served_at(self):
        for rel, page in PAGES.items():
            base = page_url(rel)
            for tag, name, value in page.refs:
                with self.subTest(f"{rel}: {tag} {name}={value}"):
                    if value.startswith("#"):
                        self.assertIn(value[1:], page.ids, "a place on this very page")
                        continue
                    url = urljoin(base, value)
                    u = urlparse(url)
                    if u.netloc != "tinodo.github.io":
                        continue
                    self.assertTrue(u.path.startswith("/dojo/"), url)
                    path = u.path[len("/dojo/"):]
                    target = path + "index.html" if path == "" or path.endswith("/") else path
                    self.assertIn(target, FILES, f"{url} is not a file of the site")
                    if u.fragment and target in PAGES:
                        self.assertIn(u.fragment, PAGES[target].ids, f"{url} has no such place")

    def test_the_404_page_uses_no_relative_address_because_it_is_served_at_any_depth(self):
        for tag, name, value in PAGES["404.html"].refs:
            u = urlparse(value)
            self.assertTrue(u.scheme == "https" or value.startswith("/dojo/") or value.startswith("#"), f"{tag} {name}={value}")

    def test_links_leave_the_site_only_for_known_places_and_files_of_the_repository_exist(self):
        repo = "/tinodo/dojo"
        for rel, page in PAGES.items():
            for tag, name, value in page.refs:
                u = urlparse(urljoin(page_url(rel), value))
                if u.netloc == "tinodo.github.io":
                    continue
                with self.subTest(f"{rel}: {value}"):
                    self.assertEqual((u.scheme, tag), ("https", "a" if tag == "a" else "never"), "nothing is loaded from another site")
                    self.assertIn(u.netloc, KNOWN_HOSTS)
                    if u.netloc == "github.com":
                        self.assertTrue(u.path.startswith(repo), value)
                        if u.path.startswith(repo + "/blob/main/"):
                            self.assertTrue((ROOT / u.path[len(repo + "/blob/main/"):]).is_file(), f"{value}: no such file")

    def test_the_visible_words_keep_the_rules_of_the_app(self):
        for rel, page in PAGES.items():
            with self.subTest(rel):
                for rx, name in BANNED:
                    self.assertIsNone(re.search(rx, page.text, re.I), f"{name!r} vouches for Dojo (docs/ui-principles.md, rule 12)")
                for rx in JARGON:
                    self.assertIsNone(re.search(rx, page.text, re.I), f"{rx}: a word from the insides of Dojo (rule 6)")

    def test_the_footer_says_which_commit_built_the_site(self):
        self.assertIn(f"Built from commit {SHA[:7]}", PAGES["index.html"].text)


class TheLook(unittest.TestCase):
    CSS = (ROOT / "website" / "assets" / "site.css").read_text("utf-8")

    def test_no_box_has_a_bar_down_its_left_side(self):
        found = []
        for selector, body in rules(self.CSS):
            for m in re.finditer(r"(border-left(?:-width|-color|-style)?|border-inline-start(?:-width|-color|-style)?)\s*:\s*([^;]+)", body):
                if not NEUTRAL_LEFT.match(m.group(2).strip()):
                    found.append(f"{selector}: {m.group(1)}: {m.group(2).strip()}")
            if re.search(r"inset\s+-?[0-9.]+px\s+0(?:px)?\s+0(?:px)?\s+(?:var\(|#|rgb)", body):
                found.append(f"{selector}: an inset shadow with only a horizontal offset")
            if re.search(r"::(?:before|after)", selector) and re.search(r"left:\s*0[;\s]", body) and re.search(r"top:\s*0[;\s]", body) and re.search(r"bottom:\s*0[;\s]", body):
                found.append(f"{selector}: a strip along the left edge")
        self.assertEqual(found, [])

    def test_the_focus_ring_and_the_text_use_colours_that_can_be_read(self):
        self.assertRegex(self.CSS, r":focus-visible\s*\{[^}]*outline:\s*2px solid var\(--amber-ink\)")
        for token in ("--ink-3", "--slate", "--ochre"):
            self.assertNotIn(token, self.CSS, f"{token} does not reach the contrast the site needs")

    def test_one_box_is_used_everywhere(self):
        self.assertRegex(self.CSS, r"\.box, \.rows, \.tablewrap\s*\{[^}]*border: 1px solid var\(--line\)[^}]*border-radius: var\(--r\)")
        self.assertRegex(self.CSS, r"\.box\s*\{[^}]*padding: 14px 16px")
        self.assertIn("--r: 8px", self.CSS)


class NothingPrivateIsPublic(unittest.TestCase):
    def test_no_text_file_holds_a_private_name_an_address_or_a_secret(self):
        for rel in FILES:
            if not rel.endswith(TEXT_FILES):
                continue
            text = (OUT / rel).read_text("utf-8")
            for what, rx in PRIVATE.items():
                if what == "an email address" and rel.startswith("assets/fonts/"):
                    continue   # the licence texts of the fonts are third-party files, copied as they are
                with self.subTest(f"{rel}: {what}"):
                    self.assertIsNone(re.search(rx, text), f"{rel} holds {what}")
            for name, rx in SCAN.PATTERNS.items():
                self.assertIsNone(rx.search(text), f"{rel} looks like a {name}")

    def test_only_named_fields_of_the_package_files_reach_the_exams_page(self):
        text = PAGES["exams/index.html"].text
        for p in package_files():
            self.assertIn(p["exam"], text)
            self.assertIn(p["title"], text)
            skills = sum(len(g["skills"]) for d in p["domains"] for g in d["groups"])
            self.assertIn(f"{skills} skills in {len(p['domains'])} domains", text)
            for d in p["domains"]:
                self.assertIn(d["title"], text)
                skill = d["groups"][0]["skills"][0]["text"]
                self.assertNotIn(skill, text, "the skill statements are © Microsoft: only the domains are listed")


class EverythingIsOnTheSite(unittest.TestCase):
    def test_every_decision_record_is_listed_with_a_link_to_it(self):
        hrefs = {v for t, n, v in PAGES["decisions/index.html"].refs}
        for f in sorted((ROOT / "docs" / "adr").glob("0*.md")):
            self.assertIn(f"https://github.com/tinodo/dojo/blob/main/docs/adr/{f.name}", hrefs)

    def test_a_decision_title_with_a_word_from_the_insides_gets_a_plainer_one(self):
        overrides = json.loads((ROOT / "website" / "content" / "decisions.json").read_text("utf-8"))
        records = {f.name[:4] for f in (ROOT / "docs" / "adr").glob("0*.md")}
        self.assertTrue(set(overrides) <= records, "decisions.json names a record that does not exist")
        _, rows = BUILD.load_decisions()
        for number, _, title, _ in rows:
            for rx in JARGON:
                self.assertIsNone(re.search(rx, title, re.I), f"ADR {number}: add a plainer title to website/content/decisions.json")

    def test_every_document_is_listed_or_left_out_on_purpose(self):
        docs = json.loads((ROOT / "website" / "content" / "docs.json").read_text("utf-8"))
        listed = {d["path"] for d in docs["items"] if "path" in d} | set(docs["not_listed"])
        for path in listed:
            self.assertTrue((ROOT / path).is_file(), f"{path} does not exist")
        on_disk = {p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").glob("*.md")} | {p.name for p in ROOT.glob("*.md")} | {"LICENSE"}
        self.assertEqual(sorted(on_disk - listed), [], "a document that is neither on the Documentation page nor in not_listed")


class TheLinkCheck(unittest.TestCase):
    def test_it_looks_at_learn_and_github_docs_addresses_only(self):
        urls = LINKS.collect(OUT)
        self.assertTrue(urls)
        self.assertTrue(any("credentials/certifications" in u for u in urls), "the exam pages and study guides")
        self.assertTrue(all(urlparse(u).hostname in LINKS.HOSTS for u in urls))

    def test_what_fails_and_what_only_says_the_other_side_is_busy(self):
        judge = LINKS.judge
        guide = "https://learn.microsoft.com/en-us/credentials/certifications/resources/study-guides/gh-300"
        self.assertEqual(judge(guide, 200, guide)[0], "ok")
        self.assertEqual(judge(guide, 200, guide + "?view=x")[0], "ok", "a query string is not another page")
        self.assertEqual(judge(guide, 200, "https://learn.microsoft.com/en-us/credentials/")[0], "fail", "replaced by an overview page")
        for status in (404, 410, 401, 400):
            self.assertEqual(judge(guide, status, guide)[0], "fail", str(status))
        for status in (403, 429, 500, 503):
            self.assertEqual(judge(guide, status, guide)[0], "inconclusive", str(status))
        self.assertEqual(judge(guide, None, None)[0], "fail", "no answer at all")


class TheWorkflows(unittest.TestCase):
    WORKFLOWS = {f.name: f.read_text("utf-8") for f in sorted((ROOT / ".github" / "workflows").glob("*.yml"))}

    def test_every_action_is_pinned_to_a_full_commit(self):
        for name, text in self.WORKFLOWS.items():
            for m in re.finditer(r"^\s*-?\s*uses:\s*(\S+)", text, re.M):
                if m.group(1).startswith("./"):
                    continue
                self.assertRegex(m.group(1), r"^[^@\s]+@[0-9a-f]{40}$", f"{name}: the repository requires a full commit hash")

    def test_a_change_to_the_site_alone_does_not_deploy_the_app_and_the_required_check_is_not_filtered(self):
        deploy, ci = self.WORKFLOWS["deploy.yml"], self.WORKFLOWS["ci.yml"]
        self.assertRegex(deploy, r"paths-ignore:\s*\n\s*- \"website/\*\*\"\s*\n\s*- \"\.github/workflows/pages\.yml\"\s*\n\s*- \"\.github/workflows/site-links\.yml\"")
        self.assertNotIn("paths", ci)

    def test_pages_may_write_only_in_its_deploy_job_and_only_after_a_push_to_main(self):
        pages = self.WORKFLOWS["pages.yml"]
        self.assertEqual(re.search(r"^permissions:\n((?:[ ]+.+\n)+)", pages, re.M).group(1), "  contents: read\n")
        cut = pages.index("\n  deploy:")
        self.assertNotIn("pages: write", pages[:cut])
        self.assertIn("pages: write", pages[cut:])
        self.assertNotIn("pull_request", pages)
        self.assertIn("environment:\n      name: github-pages", pages)
        self.assertIn("needs: build", pages)

    def test_the_weekly_link_check_can_only_read(self):
        links = self.WORKFLOWS["site-links.yml"]
        self.assertEqual(re.search(r"^permissions:\n((?:[ ]+.+\n)+)", links, re.M).group(1), "  contents: read\n")
        self.assertIn("schedule:", links)


if __name__ == "__main__":
    unittest.main()
