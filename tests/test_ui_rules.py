"""The look and the voice of Dojo's screens: no accent bars, no self-vouching words (rules 11 and 12 of docs/ui-principles.md).

The owner judges these two things first: a bar down the left side of a box, and a screen that tells him how honest it is.
These tests read the stylesheet, the strings of the front end and the user guide, so neither can come back unnoticed."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app" / "static" / "app.css").read_text("utf-8")
JS = (ROOT / "app" / "static" / "app.js").read_text("utf-8")
GUIDE = (ROOT / "docs" / "user-guide.md").read_text("utf-8")
README = (ROOT / "README.md").read_text("utf-8")

# A 1px divider between columns is a line, not an accent.
NEUTRAL_LEFT = re.compile(r"^(0|1px solid var\(--line(-2)?\))$")
# Words a screen uses to vouch for itself. They are fine in a model's instructions (not read by people), so only the
# strings of the front end and the user guide are looked at.
BANNED = [
    (r"\bhonest(ly)?\b", "honest"), (r"\btruth(ful(ly)?)?\b", "truth"), (r"\bgenuine(ly)?\b", "genuine"),
    (r"\btrust(ed|worthy)?\b", "trust"), (r"\binvent(ed|s|ing)?\b", "invent"), (r"\bchecks out\b", "checks out"),
    (r"\bwill not do\b", "will not do"), (r"\brules it keeps\b", "rules it keeps"), (r"\breally\b", "really"),
    (r"\bactually\b", "actually"),
]


def rules(css: str):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        yield m.group(1).strip(), m.group(2)


def by_selector(css: str) -> dict[str, str]:
    """Every declaration of a selector together: a selector can have a rule for narrow screens after its main one."""
    out: dict[str, str] = {}
    for selector, body in rules(css):
        out[selector] = out.get(selector, "") + " " + body
    return out


def js_strings(src: str):
    """The text of every string literal of a JavaScript file with the line it starts on, comments left out.

    A template literal can hold `${...}` and that code can hold strings and templates of its own, so the scan follows the
    nesting: the pieces of text of a template are returned one by one, and what is inside `${...}` is read as code."""
    out: list[tuple[int, str]] = []
    n = len(src)
    pos = [0, 1]   # index, line

    def regex_may_start(i: int) -> bool:
        k = i - 1
        while k >= 0 and src[k] in " \t":
            k -= 1
        return k < 0 or src[k] in "(,=:[!&|?{};" or src[max(0, k - 5):k + 1] == "return"

    def quoted(q: str) -> None:
        i, line, buf = pos[0] + 1, pos[1], []
        start = line
        while i < n and src[i] != q:
            if src[i] == "\\" and i + 1 < n:
                buf.append(src[i + 1])
                i += 2
                continue
            if src[i] == "\n":
                break
            buf.append(src[i])
            i += 1
        out.append((start, "".join(buf)))
        pos[0] = i + 1

    def template() -> None:
        i, buf, start = pos[0] + 1, [], pos[1]
        while i < n:
            c = src[i]
            if c == "\\" and i + 1 < n:
                buf.append(src[i + 1])
                i += 2
            elif c == "`":
                out.append((start, "".join(buf)))
                pos[0] = i + 1
                return
            elif src.startswith("${", i):
                out.append((start, "".join(buf)))
                buf = []
                pos[0] = i + 2
                code(until_brace=True)
                i, start = pos[0], pos[1]
            else:
                if c == "\n":
                    pos[1] += 1
                buf.append(c)
                i += 1
        pos[0] = i

    def code(until_brace: bool = False) -> None:
        depth = 0
        while pos[0] < n:
            i = pos[0]
            c = src[i]
            if c == "\n":
                pos[1] += 1
                pos[0] += 1
            elif src.startswith("//", i) and (i == 0 or src[i - 1] != ":"):
                while pos[0] < n and src[pos[0]] != "\n":
                    pos[0] += 1
            elif src.startswith("/*", i):
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                pos[1] += src.count("\n", i, j)
                pos[0] = j
            elif c in "\"'":
                quoted(c)
            elif c == "`":
                template()
            elif c == "/" and i + 1 < n and src[i + 1] not in "/*" and regex_may_start(i):
                j = i + 1
                while j < n and src[j] != "\n":
                    if src[j] == "\\":
                        j += 2
                        continue
                    if src[j] == "/":
                        break
                    j += 1
                pos[0] = j + 1
            elif c == "{":
                depth += 1
                pos[0] += 1
            elif c == "}":
                pos[0] += 1
                if until_brace and depth == 0:
                    return
                depth = max(0, depth - 1)
            else:
                pos[0] += 1

    code()
    return out


class TheScannerReadsStrings(unittest.TestCase):
    """The wording guard is only as good as its reading of the source: these are the shapes it must get right."""

    def texts(self, src: str) -> list[str]:
        return [text for _, text in js_strings(src)]

    def test_a_template_inside_a_template_is_read(self):
        found = self.texts("const label = `Before ${flag ? `an honest record` : \"\"} after`;")
        self.assertIn("an honest record", found)
        self.assertIn("Before ", found)
        self.assertIn(" after", found)

    def test_a_string_inside_an_interpolation_is_read(self):
        self.assertIn("event", self.texts('x = `${n} ${plural(n, "event")}.`;'))

    def test_comments_and_regular_expressions_are_not_strings(self):
        found = self.texts("// an honest comment\nconst a = /`honest`/.test(s); /* trust */ const b = 'ok';")
        self.assertEqual(found, ["ok"])

    def test_an_escaped_quote_does_not_end_a_string(self):
        self.assertEqual(self.texts("const s = 'it\\'s honest';"), ["it's honest"])

    def test_braces_in_code_do_not_end_an_interpolation_early(self):
        found = self.texts("x = `a ${ {k: `honest`}.k } b`;")
        self.assertIn("honest", found)
        self.assertIn(" b", found)

    def test_the_front_end_is_read_in_full(self):
        found = js_strings(JS)
        self.assertGreater(len(found), 2000, "the strings of app.js")
        self.assertTrue(any("Record check passed" in text for _, text in found))


class NoAccentBars(unittest.TestCase):
    def test_no_box_has_a_bar_down_its_left_side(self):
        found = []
        for selector, body in rules(CSS):
            for m in re.finditer(r"(border-left(?:-width|-color|-style)?|border-inline-start(?:-width|-color|-style)?)\s*:\s*([^;]+)", body):
                if not NEUTRAL_LEFT.match(m.group(2).strip()):
                    found.append(f"{selector}: {m.group(1)}: {m.group(2).strip()}")
        self.assertEqual(found, [], "a coloured or thick left border is an accent bar; use a tint and a full border")

    def test_no_shadow_draws_a_bar_on_one_side(self):
        found = [f"{sel}: {m.group(0)}" for sel, body in rules(CSS)
                 for m in re.finditer(r"inset\s+-?[0-9.]+px\s+0(?:px)?\s+0(?:px)?\s+(?:var\(|#|rgb)", body)]
        self.assertEqual(found, [], "an inset shadow with only a horizontal offset is a bar in disguise")

    def test_no_pseudo_element_is_a_strip_along_the_left_edge(self):
        found = [sel for sel, body in rules(CSS)
                 if re.search(r"::(?:before|after)", sel) and re.search(r"left:\s*0[;\s]", body)
                 and re.search(r"top:\s*0[;\s]", body) and re.search(r"bottom:\s*0[;\s]", body)]
        self.assertEqual(found, [])

    def test_a_selected_row_is_a_ring_and_says_so(self):
        css = by_selector(CSS)
        self.assertIn("inset 0 0 0 1px", css[".rec.on"])
        self.assertIn("inset 0 0 0 1px", css[".skill.selected"])
        self.assertIn("font-weight: 600", css[".skill.selected .s-name"])
        self.assertGreaterEqual(JS.count('setAttribute("aria-current", "true")'), 4, "the selected skill is told to a screen reader too")


class BoxesOfOneSize(unittest.TestCase):
    def test_the_inset_boxes_share_one_padding_and_radius(self):
        css = by_selector(CSS)
        for sel in (".rec", ".skill", ".status-sage", ".status-slate", ".drift-note", ".team-banner", ".pl-shared"):
            self.assertIn("padding: 9px 12px", css[sel], sel)
        for sel in (".rec", ".skill", ".status-sage", ".status-slate", ".drift-note", ".team-banner", ".pl-shared", "blockquote.quote"):
            self.assertRegex(css[sel], r"border-radius:\s*8px", sel)
        self.assertIn("font-size: 13px", css[".rec .rn"])
        self.assertIn("font-size: 13px", css[".status-sage"])

    def test_a_card_heading_has_the_same_gap_whatever_follows_it(self):
        self.assertRegex(CSS, r"\.panel > \.eyebrow \+ :is\([^)]*\.rec-list[^)]*div:not\(\[class\]\)[^)]*\)\s*\{\s*margin-top:\s*7px")


class NoSelfVouching(unittest.TestCase):
    def test_no_screen_says_how_honest_or_trustworthy_dojo_is(self):
        found = []
        for line, text in js_strings(JS):
            for rx, name in BANNED:
                if re.search(rx, text, re.I):
                    found.append(f"app.js:{line}: {name}: {text.strip()[:110]}")
        self.assertEqual(found, [], "say what a thing does, once, where it matters; do not vouch for it (docs/ui-principles.md, rule 12)")

    def test_the_user_guide_does_not_either(self):
        found = []
        for n, text in enumerate(GUIDE.splitlines(), 1):
            for rx, name in BANNED:
                if re.search(rx, text, re.I):
                    found.append(f"user-guide.md:{n}: {name}: {text.strip()[:110]}")
        self.assertEqual(found, [])

    def test_the_readme_does_not_either(self):
        found = []
        for n, text in enumerate(README.splitlines(), 1):
            for rx, name in BANNED:
                if re.search(rx, text, re.I):
                    found.append(f"README.md:{n}: {name}: {text.strip()[:110]}")
        self.assertEqual(found, [])

    def test_the_prediction_disclaimer_is_said_where_a_score_or_advice_is_shown(self):
        # The constant, Today's coverage bars and the practice-exam setup: it used to be on 21 screens, the sidebar included.
        self.assertLessEqual(JS.count("NOT_A_PREDICTION"), 3)
        self.assertNotIn('"Not a pass prediction."', JS, "not in the sidebar footer, which is on every page")

    def test_the_builder_has_no_card_of_rules_it_keeps(self):
        self.assertNotIn("rulesPanel", JS)
        self.assertNotIn("The rules it keeps", JS)


if __name__ == "__main__":
    unittest.main()
