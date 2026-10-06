"""In-app Help: the safe Markdown renderer (app/helpdoc.py), the guide it renders (docs/user-guide.md),
the /api/help route and its class, the deploy package, and the Help links in the front end."""
import os
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

from fastapi.routing import APIRoute  # noqa: E402

from app import helpdoc  # noqa: E402
from tests.test_team import OWNER, PUBLIC, TeamCase, classes, code, who  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "app.js").read_text("utf-8")
GUIDE_MD = (ROOT / "docs" / "user-guide.md").read_text("utf-8")

# The sections the guide promises, by their GitHub anchors.
SECTIONS = [
    "getting-started", "today", "course-and-skills", "lessons", "questions-and-answers", "spoken-checks", "recalls",
    "practice-exams", "readiness", "your-record", "plan-and-the-calendar-link", "podcast", "on-your-phone",
    "labs-dp-800", "ask-the-coach", "play", "privacy", "costs-and-daily-allowances", "limitations", "troubleshooting",
    "for-the-owner", "glossary",
]
ALLOWED_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "strong", "em", "code", "pre", "a"}


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags(html: str) -> list[tuple[str, dict]]:
    p = Tags()
    p.feed(html)
    return p.tags


def body(md: str) -> str:
    return helpdoc.render(md)["html"]


class RendererTests(unittest.TestCase):
    def test_headings_get_github_ids_and_duplicates_are_numbered(self):
        out = helpdoc.render("# Title\n\n## Labs (DP-800)\n\n### How sure were you?\n\n## Labs (DP-800)\n")
        self.assertIn('<h2 id="labs-dp-800">Labs (DP-800)</h2>', out["html"])
        self.assertIn('<h3 id="how-sure-were-you">How sure were you?</h3>', out["html"])
        self.assertIn('<h2 id="labs-dp-800-1">', out["html"])
        self.assertEqual(out["anchors"], ["title", "labs-dp-800", "how-sure-were-you", "labs-dp-800-1"])
        self.assertEqual([t["id"] for t in out["toc"]], ["labs-dp-800", "how-sure-were-you", "labs-dp-800-1"])

    def test_heading_ids_never_collide(self):
        for text, want in (
            ("## Setup\n\n## Setup\n\n## Setup-1\n", ["setup", "setup-1", "setup-1-1"]),
            ("## Setup-1\n\n## Setup\n\n## Setup\n\n## Setup\n", ["setup-1", "setup", "setup-2", "setup-3"]),
            ("## A\n\n## A-1\n\n## A\n\n## A\n", ["a", "a-1", "a-2", "a-3"]),
        ):
            anchors = helpdoc.render(text)["anchors"]
            self.assertEqual(anchors, want, text)
            self.assertEqual(len(set(anchors)), len(anchors), text)
            ids = re.findall(r'id="([^"]+)"', helpdoc.render(text)["html"])
            self.assertEqual(ids, want, text)

    def test_paragraphs_lists_and_inline_marks(self):
        out = body("One\nline.\n\n- a **b**\n- *c* and `d`\n\n1. first\n   wrapped\n2. second\n")
        self.assertIn("<p>One line.</p>", out)
        self.assertIn("<ul><li>a <strong>b</strong></li><li><em>c</em> and <code>d</code></li></ul>", out)
        self.assertIn("<ol><li>first wrapped</li><li>second</li></ol>", out)

    def test_everything_is_escaped(self):
        out = body('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n## A <b>title</b> & "quotes"\n\n'
                   '- <iframe src="https://evil.example"></iframe>\n\n`<code>` and **<em>bold</em>**\n')
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("<iframe", out)
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", out)
        self.assertIn("A &lt;b&gt;title&lt;/b&gt; &amp; &quot;quotes&quot;", out)
        self.assertIn("<code>&lt;code&gt;</code>", out)
        self.assertIn("<strong>&lt;em&gt;bold&lt;/em&gt;</strong>", out)
        self.assertEqual({t for t, _ in tags(out)} - ALLOWED_TAGS, set())

    def test_code_blocks_are_escaped_and_not_rendered(self):
        out = body("```html\n<script>x</script>\n**not bold** [x](https://a.example)\n```\n")
        self.assertEqual(out, "<pre><code>&lt;script&gt;x&lt;/script&gt;\n**not bold** [x](https://a.example)</code></pre>")

    def test_allowed_links(self):
        out = body("[Learn](https://learn.microsoft.com/en-us/credentials/?a=1&b=2) [Plan](#/plan) [Recalls](#recalls)")
        self.assertIn('<a href="https://learn.microsoft.com/en-us/credentials/?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">Learn</a>', out)
        self.assertIn('<a href="#/plan">Plan</a>', out)
        self.assertIn('<a href="#/help/recalls">Recalls</a>', out)

    def test_refused_links_are_shown_as_text(self):
        refused = [
            "javascript:alert(1)", "javascript:alert", "JavaScript:void", " javascript:x", "jav&#x61;script:x",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==", "data:image/svg+xml,x",
            "vbscript:x", "http://example.com", "//evil.example", "https:evil.example", "https://", "ftp://x.example",
            "mailto:a@example.com", "file:///etc/passwd", "/api/record/export", "help", "#Recalls", "#javascript:x",
            'https://a.example/"onmouseover="x', "https://a.example/<x>", "#/help/x\"y",
        ]
        for url in refused:
            out = body(f"[click]({url})")
            self.assertNotIn("<a", out, url)
            self.assertNotIn("href", out, url)
            self.assertEqual({t for t, _ in tags(out)}, {"p"}, url)
            self.assertIn("[click]", out, url)

    def test_raw_html_links_stay_text(self):
        out = body('<a href="javascript:alert(1)">x</a> and <a href="https://learn.microsoft.com">y</a>')
        self.assertEqual({t for t, _ in tags(out)}, {"p"})
        self.assertIn("&lt;a href=&quot;javascript:alert(1)&quot;&gt;x&lt;/a&gt;", out)

    def test_safe_href(self):
        self.assertEqual(helpdoc.safe_href("https://docs.github.com/en"), "https://docs.github.com/en")
        self.assertEqual(helpdoc.safe_href("#/help/privacy"), "#/help/privacy")
        self.assertEqual(helpdoc.safe_href("#privacy"), "#/help/privacy")
        for url in ("javascript:alert(1)", "data:,x", "http://a.example", "#/x y", "#a b", ""):
            self.assertIsNone(helpdoc.safe_href(url), url)

    def test_owner_section_is_marked(self):
        out = helpdoc.render("## Before\n\n## For the owner\n\n### Studio\n\n## Glossary\n")
        self.assertEqual([(t["id"], t["owner"]) for t in out["toc"]],
                         [("before", False), ("for-the-owner", True), ("studio", True), ("glossary", False)])
        self.assertIn('<h3 id="studio" class="owner">', out["html"])
        self.assertIn('<h2 id="glossary">', out["html"])


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.g = helpdoc.guide()

    def test_the_guide_is_the_one_in_the_repo(self):
        self.assertEqual(helpdoc.GUIDE, ROOT / "docs" / "user-guide.md")
        self.assertEqual(self.g, helpdoc.render(GUIDE_MD))

    def test_every_promised_section_is_there(self):
        level2 = [t["id"] for t in self.g["toc"] if t["level"] == 2]
        self.assertEqual(level2, SECTIONS)
        for word in ("pip", "skill", "domain", "gate", "grader", "relearned"):
            self.assertRegex(GUIDE_MD.split("## Glossary", 1)[1], rf"\*\*{word.capitalize()}\*\*", word)

    def test_only_the_owner_section_is_marked_for_the_owner(self):
        owner = [t["id"] for t in self.g["toc"] if t["owner"]]
        self.assertEqual(owner, ["for-the-owner", "studio", "add-an-exam", "members-page", "team-mode-switch"])

    def test_the_rendered_guide_has_only_safe_tags_and_links(self):
        for tag, attrs in tags(self.g["html"]):
            self.assertIn(tag, ALLOWED_TAGS)
            if tag == "a":
                self.assertRegex(attrs["href"], r"^(https://|#/)")
                self.assertLessEqual(set(attrs), {"href", "target", "rel"})
            elif tag.startswith("h"):
                self.assertLessEqual(set(attrs), {"id", "class"})
            else:
                self.assertEqual(attrs, {}, tag)

    def test_links_inside_the_guide_resolve(self):
        for anchor in re.findall(r'href="#/help/([^"]+)"', self.g["html"]):
            self.assertIn(anchor, self.g["anchors"])

    def test_every_help_link_in_the_app_exists_in_the_guide(self):
        used = set(re.findall(r'helpQ\("([^"]+)"\)', APP_JS)) | set(re.findall(r'"#/help/([a-z0-9_-]+)"', APP_JS))
        # helpQ is only ever called with a literal, so the list above is complete.
        self.assertEqual(re.findall(r"helpQ\((?!\"|anchor\))", APP_JS), [])
        for anchor in used:
            self.assertIn(anchor, self.g["anchors"], anchor)
        # The pages that must have a "?" link.
        for anchor in ("today", "lessons", "practice-exams", "your-record", "plan-and-the-calendar-link", "play",
                       "on-your-phone", "members-page"):
            self.assertIn(anchor, used)

    def test_the_app_has_the_help_page_and_its_entries(self):
        self.assertIn("[HELP_ROUTE, viewHelp]", APP_JS)
        self.assertIn('h("a", { href: "#/help" }, "Help")', APP_JS)            # the side bar
        self.assertIn('linkButton("Open Help", "#/help"', APP_JS)             # How Dojo works
        index = (ROOT / "app" / "static" / "index.html").read_text("utf-8")
        self.assertIn('href="#/help"', index)                                  # the header, also behind a gate
        self.assertIn('id="i-help"', (ROOT / "app" / "static" / "icons.svg").read_text("utf-8"))


class GuideFactsTests(unittest.TestCase):
    """The numbers and promises in the guide, tied to the code that keeps them. A change to a limit, a cap or
    a retention period fails here until the guide says the same."""

    text = " ".join(GUIDE_MD.split())

    def says(self, *parts: str):
        for part in parts:
            self.assertIn(" ".join(part.split()), self.text)

    def test_allowances_and_the_shared_cap(self):
        from app import budget
        words = {"ask": "questions to the coach", "answer": "graded answers", "hint": "hints",
                 "item": "new practice items", "check": "spoken checks", "rehearsal": "rehearsals",
                 "exam": "practice exams started", "transcribe": "transcriptions",
                 "lesson": "first lessons for a skill without one", "audio": "lesson audio", "lab": "lab runs",
                 "pool": "practice-exam question batches"}
        self.assertEqual(set(words), set(budget.ACTIONS))
        for action, (_, _, default) in budget.ACTIONS.items():
            self.says(f"- {words[action]}: {default}")
        self.says(f"by default {budget.CAP_USD:g} US dollars", f'"{budget.SHARED_REFUSAL}"')

    def test_team_membership_and_retention(self):
        from app import team
        self.says(f"A membership lasts {team.MEMBERSHIP_MONTHS} months. In the last {team.RENEW_WINDOW_DAYS} days",
                  f"is deleted {team.REQUEST_DAYS} days after you asked",
                  f"stay for {team.TOMBSTONE_DAYS} days",
                  f"you have {team.GRACE_DAYS} days to download or delete your record",
                  f"with {team.END_NOTICE_DAYS} days' notice", f"for up to {team.MAX_MEMBERS} members")
        self.assertEqual(team.AUDIT_DAYS, 365)
        self.says("keeps its entries for up to a year")
        # Backups must outlive nothing the tombstone protects.
        self.assertGreater(team.TOMBSTONE_DAYS, 30)
        self.says("backups can keep a copy for up to 30 days")

    def test_labs_off_health_check_and_region(self):
        """#48: a Dojo may have no lab database, /healthz is public but bare, and the region is not fixed."""
        from app import main
        self.says(f'the pages say "{main.LABS_OFF}"', "your plan has no lab sessions")
        self.says("shows anyone only whether Dojo's self-test passed and which build runs")
        self.assertNotIn("Sweden Central", self.text)
        self.says('The notice and the "How Dojo works" page name the region when Azure reports it.')

    def test_recalls_and_calibration(self):
        from app import know, relearn
        self.says(f"The gaps grow: {', '.join(map(str, relearn.INTERVALS[:-1]))} and {relearn.INTERVALS[-1]} days.",
                  f"after {relearn.RELEARNED_DAYS} on-time successes on different days",
                  f"at most {relearn.DAY_CAP} recalls a day",
                  f"it needs {know.MIN_ANSWERS} answers at that level")

    def test_play_points_and_social_limits(self):
        from app import play, play_board, play_duel, play_social
        self.says(f"- a study day: {play.STUDY_DAY}", f"- an open answer: {play.OPEN_ANSWER}",
                  f"- a multiple-choice answer: {play.CHOICE_ANSWER}",
                  f"- a lesson view: {play.LESSON_VIEW} (up to {play.LESSON_VIEWS} a day)",
                  f"- a checked lab: {play.LAB_CHECK} (once per lab a day, up to {play.LABS_A_WEEK} a week)",
                  f"Answers earn at most {play.ANSWER_CAP} points a day, open and multiple-choice together",
                  f"less than {play.FLOOR_S} seconds after its question earns nothing",
                  f"At most {play.DAY_CAP} points a day and {play.WEEK_CAP} a week",
                  f"a target of {play.TARGETS[0]} to {play.TARGETS[-1]} study days a week")
        self.says(f"have {play_social.CIRCLE_MIN} to {play_social.CIRCLE_MAX} people",
                  f"once at least {play_social.BAR_MIN} of its members count",
                  "a member counts from the Monday after they join", "the bar pauses until the next Monday",
                  f"Sharing works once a circle has {play_social.CIRCLE_MIN} members",
                  f"a shared moment stays for {play_social.SHARE_DAYS} days",
                  f'shown as "{play_social.OWNER_LABEL}"')
        self.says(f"needs at least {play_board.BOARD_MIN} people", f"at least {play_board.NAMES_MIN} others share it")
        for name, lo, hi in play_board.BANDS:
            self.assertRegex(self.text, rf"{name} \(({lo} to {hi}|{lo} to {hi} points a week)\)")
        self.says(f"the same {play_duel.QUESTIONS} questions for both, {play_duel.HOURS} hours to answer",
                  f"deleted {play_duel.KEEP_DAYS} days after the invitation")

    def test_no_overstated_promises(self):
        # Each of these once said more than the code delivers (PR #49 review).
        for wrong in ("Nothing from Play goes to the owner", "everything Dojo keeps about you",
                      "Nothing else is stored about you", "Nothing is invented", "Other members see only your display name",
                      "answers count up to 30 a day", "kept apart from your record"):
            self.assertNotIn(wrong.lower(), self.text.lower(), wrong)
        # The export: what it holds back, and what it does not hold.
        self.says("Some things are held back, so the export cannot be used to read answers early.",
                  "The export does not hold your membership details")
        # Play: the owner's admin view and the owner as an invited learner are two different things.
        self.says("No admin page shows anyone's Play data to the owner",
                  "If a member invites them to a circle or a duel, they take part like anyone else.")


class DeployTests(unittest.TestCase):
    def test_the_deploy_package_carries_the_guide(self):
        wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        line = next(x for x in wf.splitlines() if "zip -qr dojo.zip" in x)
        self.assertIn("docs/user-guide.md", line.split())
        # The package ships the licence and the third-party notices with the bundled fonts.
        self.assertIn("LICENSE", line.split())
        self.assertIn("THIRD-PARTY-NOTICES.md", line.split())

    def test_the_public_log_shows_no_resource_names(self):
        """Actions logs are public once the repository is. Every az step keeps its output in files, and only the
        summary of tools/public_log.py reaches the log (tests/test_public_log.py): counts, and on a failure the
        error codes. The summary is the step's last command and exits with the az command's status."""
        wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        self.assertNotIn("--result-format", wf)
        self.assertIn("--no-pretty-print --output json \\\n            > whatif.json 2> whatif.err", wf)
        runs = [s for s in wf.split("- name: ")[1:] if re.search(r"(?m)^\s+az ", s)]
        self.assertEqual(len(runs), 3)
        for body, (kind, err) in zip(runs, (("preview", "whatif"), ("infra", "deploy"), ("app", "webapp"))):
            self.assertIn(f"2> {err}.err || status=$?", body)
            self.assertTrue("> whatif.json" in body or "--output none" in body, kind)
            self.assertTrue(body.rstrip().splitlines()[-1].strip().startswith(
                f'python tools/public_log.py {kind} "$status" {err}.err'), kind)

    def test_the_app_deploy_tries_twice_and_the_smoke_test_decides(self):
        """The infrastructure step can restart the deployment service in the middle of the app's upload or its
        build (5 Oct 2026: az failed twice, and once the build stopped and the site was down until the next
        deploy). The step tries once more after a pause and may fail; the smoke test checks the exact build."""
        wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        step = wf.split("- name: Deploy the app", 1)[1].split("- name: ", 1)[0]
        self.assertIn("continue-on-error: true", step)
        self.assertEqual(step.count("az webapp deploy "), 2)
        self.assertIn('if [ "$status" -ne 0 ]; then', step)
        self.assertIn("sleep 90", step)
        smoke = wf.split("- name: Smoke test", 1)[1].split("- name: ", 1)[0]
        self.assertNotIn("if:", smoke)
        self.assertIn("s['build']==os.environ['GITHUB_SHA'] and s['status']=='ok'", smoke)


class HelpRouteTests(TeamCase):
    def test_help_is_a_learner_exit_route(self):
        route = next(r for r in self.app.routes if isinstance(r, APIRoute) and r.path == "/api/help")
        self.assertEqual(classes(route), {"learner_exit"})
        self.assertEqual(route.methods, {"GET"})
        self.assertNotIn("/api/help", PUBLIC)

    def test_members_read_help_behind_every_gate(self):
        pending = self.admit("m-1", ack=False)
        removed = self.admit("m-2")
        self.ok("POST", f"/api/team/members/{self.row('m-2')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        paused = self.admit("m-3")
        active = self.admit("m-4")
        self.ok("POST", "/api/team/pause", OWNER)
        for h in (pending, removed, paused, active, OWNER):
            g = self.ok("GET", "/api/help", h)
            self.assertEqual([t["id"] for t in g["toc"] if t["level"] == 2], SECTIONS)
            self.assertIn('<h2 id="privacy">', g["html"])

    def test_non_members_and_signed_out_people_are_refused(self):
        r = self.call("GET", "/api/help", who("stranger"))
        self.assertEqual((r.status_code, code(r)), (403, "not_member"))
        self.assertEqual(self.c.get("/api/help").status_code, 401)

    def test_a_missing_guide_says_so(self):
        with mock.patch.object(helpdoc, "GUIDE", Path(self.tmp) / "missing.md"):
            r = self.call("GET", "/api/help", OWNER)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["detail"], "The user guide is missing from this build.")


class HelpTeamOffTests(TeamCase):
    team_on = False

    def test_only_the_owner_reads_help(self):
        self.ok("GET", "/api/help", OWNER)
        r = self.call("GET", "/api/help", who("someone-else"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "This Dojo belongs to someone else.")


class PersonalModeMembersTests(unittest.TestCase):
    """Personal mode has no Members link. A typed #/members explains team mode instead of showing team controls."""

    def test_the_side_bar_links_members_only_in_team_mode(self):
        self.assertIn('isAdmin() && state.me.team ? navLink("#/members", "shield", "Members") : null', APP_JS)

    def test_a_typed_members_address_explains_team_mode(self):
        body = APP_JS.split("async function viewMembers()", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("if (!m.team && !m.suspended) return", body)
        self.assertIn("personalModePanel()", body)
        note = APP_JS.split("function personalModePanel()", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('"#/help/team-mode-switch"', note)
        self.assertIn("not a button in the app", note)


if __name__ == "__main__":
    unittest.main()
