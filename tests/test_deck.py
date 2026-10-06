"""The Present deck (ADR 0014), app/deck.py: the pure part. What a deck spec may hold and how it is cut down
to what can be drawn and checked, how it is worded for the gate, how the verdicts are applied, when each
part of a slide appears, and the scenes the browser plays, for the derived deck and the visual deck.

The fixtures are Dojo's own synthetic content: a fictional lesson about syncing settings to devices (exam
XY-110), two fictional pages it quotes, and the deck spec an author model could return for it."""
import copy
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from app import deck
from app.learning import _gives_away
from app.sources import normalize

DATA = Path(__file__).parent / "data" / "present"
LESSON = json.loads((DATA / "lesson-widget-sync.json").read_text(encoding="utf-8"))
SOURCES = json.loads((DATA / "sources-widget-sync.json").read_text(encoding="utf-8"))
RAW = json.loads((DATA / "deck-spec-widget-sync.json").read_text(encoding="utf-8"))
ICONS_SVG = (Path(__file__).parent.parent / "app" / "static" / "icons.svg").read_text(encoding="utf-8")


def quote_ok(quote, n):
    """As Learning.quote_ok: at least 20 characters and 4 words, word for word in the excerpt of page n."""
    q = normalize(quote or "")
    src = SOURCES.get(str(n))
    return bool(src) and len(q) >= 20 and len(q.split()) >= 4 and q in normalize(src["excerpt"])


def code_ok(lines, n):
    src = SOURCES.get(str(n))
    return bool(src) and deck.code_found(lines, src["excerpt"])


def visual(raw):
    return deck.clean_visual(raw, quote_ok, code_ok, normalize)


def spec_of(raw=None, lesson=None):
    return deck.clean_spec(copy.deepcopy(RAW if raw is None else raw), lesson or LESSON, quote_ok, code_ok, normalize)


def raw_visual(i):
    return copy.deepcopy(next(e for e in RAW["slides"] if e["slide"] == i)["visual"])


def all_hold(spec):
    return {eid: (True, "") for eid, _ in deck.statements(spec, LESSON)}


def steps(n, cap=deck.LABEL):
    return [{"label": f"Step {k + 1}", "note": ""} for k in range(n)] if cap else []


class FixtureTests(unittest.TestCase):
    def test_the_fixtures_are_synthetic(self):
        for ref in LESSON["sources"]:
            self.assertIn("/contoso/", ref["url"])
        self.assertEqual(LESSON["package"], "xy-110")
        self.assertIn("synthetic", SOURCES["note"])

    def test_every_quote_of_the_lesson_is_on_its_page(self):
        quotes = [p for s in LESSON["sections"] for p in s["points"]] + LESSON["misconceptions"] + LESSON["self_check"]
        for q in quotes:
            self.assertTrue(quote_ok(q["quote"], q["source"]), q["quote"])

    def test_every_icon_a_diagram_may_use_is_in_the_sprite(self):
        have = set(re.findall(r'<symbol id="i-([a-z]+)"', ICONS_SVG))
        self.assertEqual(sorted(deck.ICONS - have), [])


class CleanVisualTests(unittest.TestCase):
    def test_each_of_the_ten_types_in_the_fixture_is_drawn(self):
        kinds = set()
        for entry in RAW["slides"]:
            v, why = visual(entry["visual"])
            self.assertIsNotNone(v, why)
            self.assertEqual(why, "")
            kinds.add(v["type"])
        v, why = visual(RAW["alternates"]["2"])
        self.assertIsNotNone(v, why)
        kinds.add(v["type"])
        self.assertEqual(kinds, set(deck.VISUALS))

    def test_what_is_not_a_diagram(self):
        self.assertEqual(visual("flow"), (None, "not a diagram"))
        v, why = visual({"type": "pie"})
        self.assertIsNone(v)
        self.assertIn("unknown diagram type", why)
        self.assertIsNone(visual({"type": None})[0])

    def test_flow_and_cycle_take_three_to_six_short_steps(self):
        for kind in ("flow", "cycle"):
            self.assertIsNone(visual({"type": kind, "steps": steps(2)})[0])
            self.assertIsNone(visual({"type": kind, "steps": steps(7)})[0])
            self.assertIsNotNone(visual({"type": kind, "steps": steps(3)})[0])
            self.assertIsNotNone(visual({"type": kind, "steps": steps(6)})[0])
            self.assertIsNone(visual({"type": kind, "steps": "a, b, c"})[0])

    def test_a_long_label_or_note_is_left_out_not_cut(self):
        long_label = steps(3)
        long_label[1]["label"] = "x" * (deck.LABEL + 1)
        self.assertIsNone(visual({"type": "flow", "steps": long_label})[0])
        long_note = steps(3)
        long_note[0]["note"] = "y" * (deck.NOTE + 1)
        self.assertIsNone(visual({"type": "flow", "steps": long_note})[0])
        not_dict = steps(3)
        not_dict[2] = "Step 3"
        self.assertIsNone(visual({"type": "flow", "steps": not_dict})[0])
        spaced = steps(3)
        spaced[0]["label"] = "  Collect\n  the   settings "
        v, _ = visual({"type": "flow", "steps": spaced})
        self.assertEqual(v["steps"][0]["label"], "Collect the settings")

    def test_icons_and_cues_are_cleaned(self):
        raw = steps(3)
        raw[0].update(icon="shield", cue="2")
        raw[1].update(icon="<script>", cue=-1)
        raw[2].update(icon="download", cue=True)
        v, _ = visual({"type": "flow", "steps": raw})
        self.assertEqual([s["icon"] for s in v["steps"]], ["shield", None, "download"])
        self.assertEqual([s["cue"] for s in v["steps"]], [2, None, None])

    def test_stack_takes_two_to_five_layers(self):
        self.assertIsNone(visual({"type": "stack", "layers": steps(1)})[0])
        self.assertIsNone(visual({"type": "stack", "layers": steps(6)})[0])
        v, _ = visual({"type": "stack", "layers": steps(2)})
        self.assertEqual([x["label"] for x in v["layers"]], ["Step 1", "Step 2"])

    def test_timeline_events_need_a_short_time(self):
        raw = raw_visual(8)
        self.assertEqual([e["when"] for e in visual(raw)[0]["events"]], ["Day 1", "Day 3", "Day 7"])
        raw["events"][0]["when"] = "x" * (deck.WHEN + 1)
        self.assertIsNone(visual(raw)[0])
        raw = raw_visual(8)
        del raw["events"][1]["when"]
        self.assertIsNone(visual(raw)[0])
        raw = raw_visual(8)
        raw["events"] = raw["events"][:2]
        self.assertIn("3 to 6", visual(raw)[1])
        raw = raw_visual(8)
        raw["events"][2] = "Day 7"
        self.assertIsNone(visual(raw)[0])

    def test_hub_keeps_good_spokes_and_needs_three(self):
        raw = raw_visual(6)
        raw["spokes"][1]["label"] = "z" * (deck.LABEL + 1)
        v, _ = visual(raw)
        self.assertEqual([s["label"] for s in v["spokes"]], ["Scheduler", "Verifier", "Reporter"])
        raw["spokes"][2]["note"] = "n" * (deck.NOTE + 1)
        self.assertIsNone(visual(raw)[0])
        raw = raw_visual(6)
        raw["center"] = ""
        self.assertIsNone(visual(raw)[0])
        raw = raw_visual(6)
        raw["spokes"] = [{"label": f"Spoke {k}", "note": ""} for k in range(8)]
        self.assertEqual(len(visual(raw)[0]["spokes"]), 6)
        raw["icon"] = "nope"
        self.assertIsNone(visual(raw)[0]["icon"])

    def test_compare_needs_two_or_three_columns_and_two_complete_rows(self):
        raw = raw_visual(2)
        v, _ = visual(raw)
        self.assertEqual(v["columns"], ["Push", "Pull", "Mirror"])
        self.assertEqual(len(v["rows"]), 3)
        self.assertEqual(v["rows"][2]["cells"], ["no", "yes", "yes"])
        for cols in (["Only"], ["A", "B", "C", "D"], ["A", "c" * (deck.HEAD + 1)], "A,B"):
            bad = raw_visual(2)
            bad["columns"] = cols
            self.assertIsNone(visual(bad)[0], cols)
        short = raw_visual(2)
        short["rows"][0]["cells"] = ["one", "two"]
        short["rows"][1]["cells"][2] = "c" * (deck.CELL + 1)
        self.assertIsNone(visual(short)[0])
        many = raw_visual(2)
        many["rows"] = [{"label": f"Row {k}", "cells": ["a", "b", "c"]} for k in range(7)]
        self.assertEqual(len(visual(many)[0]["rows"]), 5)

    def test_contrast_needs_two_titled_sides_with_points(self):
        raw = copy.deepcopy(RAW["alternates"]["2"])
        v, _ = visual(raw)
        self.assertEqual((v["left"]["tone"], v["right"]["tone"]), ("bad", "good"))
        self.assertEqual(len(v["right"]["points"]), 2)
        raw["left"]["tone"] = "angry"
        raw["right"]["points"] = ["p" * (deck.POINT + 1), "Kept", "a", "b", "c", "d"]
        v, _ = visual(raw)
        self.assertEqual(v["left"]["tone"], "neutral")
        self.assertEqual(v["right"]["points"], ["Kept", "a", "b"])
        raw["left"]["points"] = []
        self.assertIsNone(visual(raw)[0])
        raw = copy.deepcopy(RAW["alternates"]["2"])
        del raw["right"]
        self.assertIsNone(visual(raw)[0])
        raw = copy.deepcopy(RAW["alternates"]["2"])
        raw["left"]["title"] = "t" * (deck.HEAD + 1)
        self.assertIsNone(visual(raw)[0])

    def test_matrix_needs_two_axes_and_one_label_in_each_of_four_cells(self):
        v, _ = visual(raw_visual(3))
        self.assertEqual([(c["y"], c["x"]) for c in v["cells"]], [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.assertEqual(v["cells"][3]["label"], "Push mode")
        for change in ("drop_axis", "duplicate", "three", "outside", "no_label"):
            raw = raw_visual(3)
            if change == "drop_axis":
                raw["y"] = {"label": "Changes", "low": "later"}
            elif change == "duplicate":
                raw["cells"][1]["x"], raw["cells"][1]["y"] = 1, 1
            elif change == "three":
                raw["cells"] = raw["cells"][:3]
            elif change == "outside":
                raw["cells"][0]["x"] = 2
            else:
                raw["cells"][2]["label"] = ""
            self.assertIsNone(visual(raw)[0], change)

    def test_keynum_needs_a_number_and_an_exact_quote_that_holds_it(self):
        v, _ = visual(raw_visual(4))
        self.assertEqual((v["value"], v["unit"], v["source"]), ("15", "minutes", 1))
        for value in ("fifteen", "15 min", "123456789"):
            raw = raw_visual(4)
            raw["value"] = value
            self.assertIsNone(visual(raw)[0], value)
        raw = raw_visual(4)
        raw["value"] = "240"
        self.assertIn("contains the number", visual(raw)[1])
        raw = raw_visual(4)
        raw["quote"] = "In pull mode, each device checks the hub every 15 minutes when it likes."
        self.assertIsNone(visual(raw)[0])
        raw = raw_visual(4)
        raw["source"] = 2
        self.assertIsNone(visual(raw)[0])
        raw = raw_visual(4)
        raw["unit"] = "u" * (deck.UNIT + 1)
        self.assertIsNone(visual(raw)[0])
        for value in ("15%", "2.5k", "1,000"):
            self.assertTrue(deck._NUMBER.match(value), value)

    def test_code_must_be_the_page_own_lines(self):
        v, _ = visual(raw_visual(7))
        self.assertEqual(v["lang"], "yaml")
        self.assertEqual(len(v["lines"]), 8)
        self.assertEqual([m["line"] for m in v["marks"]], [0, 1, 2, 6])
        changed = raw_visual(7)
        changed["lines"][1] = "interval_minutes: 5"
        self.assertIn("word for word", visual(changed)[1])
        other_page = raw_visual(7)
        other_page["source"] = 1
        self.assertIsNone(visual(other_page)[0])
        swapped = raw_visual(7)
        swapped["lines"][0], swapped["lines"][1] = swapped["lines"][1], swapped["lines"][0]
        self.assertIsNone(visual(swapped)[0])
        too_long = raw_visual(7)
        too_long["lines"] = ["x"] * (deck.CODE_LINES + 1)
        self.assertIsNone(visual(too_long)[0])
        wide_line = raw_visual(7)
        wide_line["lines"][0] = "mode: pull" + " " * 10 + "#" + "x" * deck.CODE_LINE
        self.assertIsNone(visual(wide_line)[0])

    def test_code_marks_sit_on_real_lines_once_and_unknown_languages_are_text(self):
        raw = raw_visual(7)
        raw["lang"] = "brainfuck"
        raw["lines"] = ["mode: pull", "", "interval_minutes: 30"]
        raw["marks"] = [{"line": 1, "note": "blank"}, {"line": 0, "note": "first"}, {"line": 0, "note": "again"},
                        {"line": 9, "note": "past the end"}, {"line": 2, "note": "n" * (deck.NOTE + 1)}, {"line": 2, "note": "fine"}]
        v, why = visual(raw)
        self.assertIsNotNone(v, why)
        self.assertEqual(v["lang"], "text")
        self.assertEqual([(m["line"], m["note"]) for m in v["marks"]], [(0, "first")])
        raw["marks"] = [{"line": 0, "note": "a"}, {"line": 2, "note": "b"}, {"line": 0, "note": "c"}]
        self.assertEqual(len(visual(raw)[0]["marks"]), 2)
        tabbed = raw_visual(7)
        tabbed["lines"] = ["quiet_window:", '\tstart: "22:00"']
        self.assertEqual(visual(tabbed)[0]["lines"][1], '  start: "22:00"')


class CodeFoundTests(unittest.TestCase):
    PAGE = "Intro text\nalpha: 1\n# a comment\nbeta:\n  gamma: 2\nOutro"

    def test_lines_in_the_page_order_with_lines_left_out_between(self):
        self.assertTrue(deck.code_found(["alpha: 1", "beta:", "  gamma: 2"], self.PAGE))
        self.assertTrue(deck.code_found(["alpha:   1", "", "  gamma: 2  "], self.PAGE))

    def test_out_of_order_changed_or_empty_is_not_found(self):
        self.assertFalse(deck.code_found(["beta:", "alpha: 1"], self.PAGE))
        self.assertFalse(deck.code_found(["alpha: 2"], self.PAGE))
        self.assertFalse(deck.code_found(["", "  "], self.PAGE))
        self.assertFalse(deck.code_found(["alpha"], self.PAGE))
        self.assertFalse(deck.code_found(["alpha: 1"], ""))


class CleanSpecTests(unittest.TestCase):
    def test_the_fixture_spec_is_kept_whole(self):
        spec, blocked, proposed = spec_of()
        self.assertEqual(blocked, [])
        self.assertEqual(proposed, 4 + 3 + 9 + 5 + 5)
        self.assertEqual(spec["format"], deck.DECK_FORMAT)
        self.assertEqual(len(spec["outcomes"]), 4)
        self.assertEqual([s["slides"] for s in spec["sections"]], [[0, 1], [2, 3, 4], [5, 6, 7, 8]])
        self.assertEqual(sorted(spec["slides"], key=int), [str(i) for i in range(9)])
        self.assertEqual(spec["slides"]["1"]["tip"], None)
        self.assertEqual(spec["slides"]["0"]["tip"]["kind"], "trap")
        self.assertEqual(spec["slides"]["4"]["tip"]["kind"], "tip")
        self.assertEqual(len(spec["recap"]), 5)
        self.assertNotIn("alternates", spec)
        self.assertNotIn("note", spec)

    def test_what_is_not_a_dict_proposes_nothing(self):
        for raw in (None, [], "deck", {"slides": "all"}):
            spec, blocked, proposed = deck.clean_spec(raw, LESSON, quote_ok, code_ok, normalize)
            self.assertEqual((spec["outcomes"], spec["sections"], spec["slides"], spec["recap"]), ([], [], {}, []))
            self.assertEqual((blocked, proposed), ([], 0))

    def test_outcomes_are_capped_at_four_and_a_long_one_is_left_out(self):
        raw = copy.deepcopy(RAW)
        raw["outcomes"] = ["a" * (deck.OUTCOME + 1)] + [f"Outcome {k}" for k in range(5)]
        spec, blocked, proposed = spec_of(raw)
        self.assertEqual(spec["outcomes"], ["Outcome 0", "Outcome 1", "Outcome 2", "Outcome 3"])
        self.assertEqual([b["part"] for b in blocked], ["outcome 1", "outcome 6"])
        self.assertEqual(proposed, 6 + 3 + 9 + 5 + 5)

    def test_sections_must_take_every_slide_once_in_order_in_two_to_four_parts(self):
        bad = {
            "a slide missing": [[0, 1], [2, 3, 4], [5, 6, 7]],
            "out of order": [[0, 2], [1, 3, 4], [5, 6, 7, 8]],
            "twice": [[0, 1, 2], [2, 3, 4], [5, 6, 7, 8]],
            "one part": [list(range(9))],
            "five parts": [[0], [1], [2], [3], [4, 5, 6, 7, 8]],
            "an empty part": [[0, 1], [], [2, 3, 4, 5, 6, 7, 8]],
            "not numbers": [["a", "b"], list(range(9))],
        }
        for why, groups in bad.items():
            raw = copy.deepcopy(RAW)
            raw["sections"] = [{"title": f"Part {k}", "slides": g} for k, g in enumerate(groups)]
            spec, blocked, proposed = spec_of(raw)
            self.assertEqual(spec["sections"], [], why)
            self.assertEqual([b["part"] for b in blocked], ["sections"], why)
            self.assertEqual(proposed, 4 + len(groups) + 9 + 5 + 5, why)

    def test_a_section_without_a_usable_title_stays_untitled(self):
        raw = copy.deepcopy(RAW)
        raw["sections"][1]["title"] = "t" * (deck.TITLE + 1)
        raw["sections"][2] = {"slides": [5, 6, 7, 8]}
        spec, blocked, _ = spec_of(raw)
        self.assertEqual([s["title"] for s in spec["sections"]], ["How sync works", "", ""])
        self.assertEqual([b["part"] for b in blocked], ["section 2", "section 3"])

    def test_a_tip_needs_a_short_text_and_an_exact_quote(self):
        raw = copy.deepcopy(RAW)
        raw["slides"][0]["tip"]["quote"] = "A run that fails validation is retried at once."
        raw["slides"][4]["tip"]["text"] = "t" * (deck.TIP + 1)
        raw["slides"][5]["tip"] = "Retries double."
        raw["slides"][6]["tip"]["kind"] = "warning"
        spec, blocked, _ = spec_of(raw)
        self.assertEqual([b["part"] for b in blocked], ["slide 1 tip", "slide 5 tip", "slide 6 tip"])
        self.assertIsNone(spec["slides"]["0"]["tip"])
        self.assertEqual(spec["slides"]["6"]["tip"]["kind"], "tip")

    def test_a_quote_on_a_slide_is_short(self):
        long = "every device keeps its last good settings " * 6
        raw = raw_visual(4)
        raw["quote"] = f"Every 15 minutes {long}"
        self.assertIn("short exact quote", deck.clean_visual(raw, lambda q, n: True, code_ok, normalize)[1])
        raw = copy.deepcopy(RAW)
        raw["slides"][0]["tip"]["quote"] = long
        spec, blocked, _ = deck.clean_spec(raw, LESSON, lambda q, n: True, code_ok, normalize)
        self.assertIsNone(spec["slides"]["0"]["tip"])
        self.assertIn("slide 1 tip", [b["part"] for b in blocked])

    def test_an_entry_with_nothing_left_is_not_kept(self):
        raw = copy.deepcopy(RAW)
        raw["slides"][1]["visual"] = {"type": "stack", "layers": []}
        spec, blocked, _ = spec_of(raw)
        self.assertNotIn("1", spec["slides"])
        self.assertEqual(blocked[0]["part"], "slide 2 diagram")
        self.assertIn("2 to 5", blocked[0]["reason"])

    def test_entries_for_no_slide_or_a_second_time_are_offered_and_blocked(self):
        raw = copy.deepcopy(RAW)
        raw["slides"] += [{"slide": 9, "visual": raw_visual(0)}, {"slide": 0, "visual": raw_visual(1), "tip": None},
                          {"slide": -1, "tip": {"text": "x"}}, "not an entry"]
        spec, blocked, proposed = spec_of(raw)
        self.assertEqual(spec["slides"]["0"]["visual"]["type"], "flow")
        self.assertEqual([b["part"] for b in blocked], ["slide 10 diagram", "slide 1 diagram", "slide 0 tip"])
        self.assertEqual(proposed, 26 + 3)

    def test_recap_lines_are_capped(self):
        raw = copy.deepcopy(RAW)
        raw["recap"] = [f"Line {k}" for k in range(6)] + [""]
        spec, blocked, _ = spec_of(raw)
        self.assertEqual(spec["recap"], [f"Line {k}" for k in range(5)])
        self.assertEqual([b["part"] for b in blocked], ["recap 6", "recap 7"])


class StatementTests(unittest.TestCase):
    def setUp(self):
        self.spec = spec_of()[0]
        self.said = dict(deck.statements(self.spec, LESSON))

    def test_every_element_has_one_id(self):
        ids = [eid for eid, _ in deck.statements(self.spec, LESSON)]
        self.assertEqual(len(ids), len(set(ids)))
        expected = (["o0", "o1", "o2", "o3", "s0", "s1", "s2", "v0", "t0", "v1", "v2.0", "v2.1", "v2.2",
                     "v3.00", "v3.01", "v3.10", "v3.11", "v4", "t4", "v5", "t5", "v6.0", "v6.1", "v6.2", "v6.3", "t6",
                     "v7.0", "v7.1", "v7.2", "v7.6", "v8", "t8", "r0", "r1", "r2", "r3", "r4"])
        self.assertEqual(ids, expected)
        self.assertEqual([eid for eid, _ in deck.shown_texts(self.spec)], expected)

    def test_an_ordered_diagram_is_one_statement_in_its_order(self):
        flow = self.said["v0"]
        self.assertTrue(flow.startswith('On the slide "Every sync run has four stages", a diagram says: These steps, in this order:'))
        self.assertLess(flow.index("1. Collect"), flow.index("2. Validate; a failed check stops the run here"))
        self.assertLess(flow.index("3. Package"), flow.index("4. Deliver; to every enrolled device"))
        self.assertIn("then back to step 1", self.said["v5"])
        self.assertIn("from the top to the bottom: 1. Hub administrators", self.said["v1"])
        self.assertIn("in time order: 1. Day 1: One pilot site; 2. Day 3: One region; 3. Day 7: Every site.", self.said["v8"])

    def test_parts_are_worded_one_by_one(self):
        self.assertIn('about "Sync agent": Verifier; checks the package signature.', self.said["v6.2"])
        self.assertIn('on "Works if the hub link drops", Push: no; Pull: yes; Mirror: yes.', self.said["v2.2"])
        self.assertIn("where Hub connection is reliable and Changes must arrive is at once: Push mode; delivered as soon as published.",
                      self.said["v3.11"])
        self.assertIn("where Hub connection is often drops and Changes must arrive is by the next check: Mirror mode", self.said["v3.00"])
        self.assertIn('15 minutes: How often a device in pull mode checks the hub, by default (the page says: "In pull mode', self.said["v4"])
        self.assertIn("the line `max_attempts: 8`: up to 8 attempts", self.said["v7.6"])
        self.assertIn('an exam trap: A run that fails validation delivers nothing', self.said["t0"])
        self.assertIn('an exam tip: Before each stage', self.said["t8"])
        self.assertEqual(self.said["o1"], "After this lesson the learner can: Choose push, pull or mirror mode for a site")
        self.assertEqual(self.said["s1"], 'A part titled "Choosing a mode" covers the slides "Three ways to sync", '
                                          '"Pick the mode for a site", "How often a device checks".')
        self.assertTrue(self.said["r2"].startswith("In the recap: Push needs a live hub"))

    def test_contrast_points_are_worded_with_their_side(self):
        raw = copy.deepcopy(RAW)
        raw["slides"][2]["visual"] = copy.deepcopy(RAW["alternates"]["2"])
        said = dict(deck.statements(spec_of(raw)[0], LESSON))
        self.assertIn('under "Needs a live hub link": Push mode delivers only', said["v2.l0"])
        self.assertIn('under "Keeps working offline": Mirror mode serves', said["v2.r1"])

    def test_an_untitled_section_is_not_sent(self):
        spec = copy.deepcopy(self.spec)
        spec["sections"][0]["title"] = ""
        ids = [eid for eid, _ in deck.statements(spec, LESSON)]
        self.assertNotIn("s0", ids)
        self.assertIn("s1", ids)

    def test_shown_texts_are_the_words_on_the_screen(self):
        shown = dict(deck.shown_texts(self.spec))
        self.assertEqual(shown["v1"], "Hub administrators; change sync for the whole hub. Site operators; change sync for their "
                                      "own sites only. Device owners; pause sync on one device for up to 24 hours")
        self.assertEqual(shown["v8"], "Day 1; One pilot site. Day 3; One region. Day 7; Every site")
        self.assertNotIn("On the slide", shown["v0"])
        self.assertIn("15 minutes", shown["v4"])

    def test_nothing_in_the_fixture_gives_away_a_self_check_answer(self):
        for eid, text in deck.shown_texts(self.spec):
            self.assertEqual(_gives_away(text, LESSON["self_check"]), "", eid)

    def test_a_spec_text_that_asks_or_answers_the_self_check_is_caught(self):
        q = LESSON["self_check"][1]
        spec = copy.deepcopy(self.spec)
        spec["recap"][0] = q["question"]
        spec["outcomes"][0] = "Remember: " + q["answer"]
        shown = dict(deck.shown_texts(spec))
        self.assertIn("asks a self-check question", _gives_away(shown["r0"], LESSON["self_check"]))
        self.assertIn("word for word", _gives_away(shown["o0"], LESSON["self_check"]))


class ApplyVerdictsTests(unittest.TestCase):
    def setUp(self):
        self.spec = spec_of()[0]

    def apply(self, failing=(), missing=()):
        verdicts = {eid: (eid not in failing, "" if eid not in failing else f"{eid} does not hold")
                    for eid, _ in deck.statements(self.spec, LESSON) if eid not in missing}
        blocked = []
        out, held = deck.apply_verdicts(copy.deepcopy(self.spec), verdicts, blocked)
        return out, held, blocked

    def test_all_holding_keeps_everything(self):
        out, held, blocked = self.apply()
        self.assertEqual(out, self.spec)
        self.assertEqual(held, 26)
        self.assertEqual(blocked, [])

    def test_a_missing_verdict_does_not_hold(self):
        out, held, blocked = self.apply(missing={"r0"})
        self.assertEqual(len(out["recap"]), 4)
        self.assertEqual(held, 25)
        self.assertEqual(blocked, [{"part": "recap 1", "text": self.spec["recap"][0], "reason": "no verdict"}])

    def test_too_few_outcomes_or_recap_lines_fall_back(self):
        out, held, _ = self.apply(failing={"o0", "o1", "o2", "r0", "r1", "r2"})
        self.assertEqual(out["outcomes"], [])
        self.assertEqual(out["recap"], [])
        self.assertEqual(held, 26 - 6)
        out, _, _ = self.apply(failing={"o0", "o1", "r0", "r1"})
        self.assertEqual(len(out["outcomes"]), 2)
        self.assertEqual(len(out["recap"]), 3)

    def test_a_failed_section_title_keeps_the_part_untitled(self):
        out, held, blocked = self.apply(failing={"s1"})
        self.assertEqual([s["title"] for s in out["sections"]], ["How sync works", "", "Keeping sync healthy"])
        self.assertEqual(out["sections"][1]["slides"], [2, 3, 4])
        self.assertEqual(held, 25)
        self.assertEqual(blocked[0]["reason"], "s1 does not hold")

    def test_an_ordered_diagram_goes_whole_and_its_tip_stays(self):
        out, held, _ = self.apply(failing={"v0", "v5", "v8", "v1"})
        self.assertIsNone(out["slides"]["0"]["visual"])
        self.assertEqual(out["slides"]["0"]["tip"]["kind"], "trap")
        self.assertNotIn("1", out["slides"])
        self.assertEqual(held, 22)

    def test_a_hub_loses_a_spoke_and_goes_below_three(self):
        out, held, _ = self.apply(failing={"v6.1"})
        self.assertEqual([s["label"] for s in out["slides"]["6"]["visual"]["spokes"]], ["Scheduler", "Verifier", "Reporter"])
        self.assertEqual(held, 26)
        out, held, _ = self.apply(failing={"v6.1", "v6.3"})
        self.assertIsNone(out["slides"]["6"]["visual"])
        self.assertEqual(held, 25)

    def test_a_table_needs_two_rows_left(self):
        out, _, _ = self.apply(failing={"v2.1"})
        self.assertEqual([r["label"] for r in out["slides"]["2"]["visual"]["rows"]], ["Delivers", "Works if the hub link drops"])
        out, _, _ = self.apply(failing={"v2.0", "v2.2"})
        self.assertNotIn("2", out["slides"])

    def test_one_failed_cell_drops_the_matrix_and_a_keynum_goes_whole(self):
        out, held, blocked = self.apply(failing={"v3.10", "v4"})
        self.assertNotIn("3", out["slides"])
        self.assertIsNone(out["slides"]["4"]["visual"])
        self.assertIsNotNone(out["slides"]["4"]["tip"])
        self.assertEqual(held, 24)
        self.assertEqual([b["part"] for b in blocked], ["slide 4 diagram cell", "slide 5 diagram"])

    def test_code_keeps_its_lines_and_the_notes_that_held(self):
        out, held, _ = self.apply(failing={"v7.1", "v7.6"})
        v = out["slides"]["7"]["visual"]
        self.assertEqual(len(v["lines"]), 8)
        self.assertEqual([m["line"] for m in v["marks"]], [0, 2])
        self.assertEqual(held, 26)

    def test_a_contrast_with_an_empty_side_is_dropped(self):
        raw = copy.deepcopy(RAW)
        raw["slides"][2]["visual"] = copy.deepcopy(RAW["alternates"]["2"])
        spec = spec_of(raw)[0]
        ok = all_hold(spec)
        ok["v2.r1"] = (False, "no")
        out, _ = deck.apply_verdicts(spec, ok)
        self.assertEqual(out["slides"]["2"]["visual"]["right"]["points"], ["Pull mode keeps working"])
        ok["v2.l0"] = (False, "no")
        out, _ = deck.apply_verdicts(spec, ok)
        self.assertNotIn("2", out["slides"])

    def test_a_failed_tip_goes_alone(self):
        out, held, _ = self.apply(failing={"t4"})
        self.assertIsNone(out["slides"]["4"]["tip"])
        self.assertEqual(out["slides"]["4"]["visual"]["type"], "keynum")
        self.assertEqual(held, 25)


class CueTests(unittest.TestCase):
    LINES = ["The scheduler starts each check.", "The downloader fetches the package.", "The verifier checks the signature.",
             "The reporter sends the result."]

    def test_nothing_to_place(self):
        self.assertEqual(deck.cues([], self.LINES), [])
        self.assertEqual(deck.cues(["a", "b"], []), [0, 0])

    def test_a_part_comes_in_on_the_line_that_says_it(self):
        self.assertEqual(deck.cues(["Scheduler starts", "Downloader fetches", "Verifier checks", "Reporter sends"], self.LINES), [0, 1, 2, 3])

    def test_the_author_cue_wins_and_may_go_back(self):
        self.assertEqual(deck.cues(["Base", "Middle", "Top"], self.LINES, [3, 2, 0]), [3, 2, 0])
        self.assertEqual(deck.cues(["Scheduler starts", "Downloader fetches"], self.LINES, [9, None]), [0, 1])

    def test_without_shared_words_parts_are_spread_evenly_and_never_go_back(self):
        self.assertEqual(deck.cues(["x", "y", "z", "w"], self.LINES), [0, 1, 2, 3])
        self.assertEqual(deck.cues(["x", "y"], self.LINES), [0, 2])
        out = deck.cues(["reporter sends", "scheduler starts", "verifier"], self.LINES)
        self.assertEqual(out, sorted(out))

    def test_more_parts_than_lines(self):
        out = deck.cues(["a", "b", "c", "d", "e"], ["one", "two"])
        self.assertEqual(out, sorted(out))
        self.assertTrue(all(0 <= c < 2 for c in out))


class ComposeTests(unittest.TestCase):
    def kinds(self, d):
        return [s["kind"] for s in d["scenes"]]

    def test_the_derived_deck_tells_the_lesson(self):
        d = deck.compose(LESSON)
        self.assertEqual(d["level"], "derived")
        self.assertEqual(self.kinds(d), ["title", "agenda", "part"] + ["slide"] * 9 + ["part", "process", "part"]
                         + ["trap"] * 3 + ["part", "recap", "check", "end"])
        self.assertEqual([(p["title"], p["role"]) for p in d["parts"]],
                         [("The ideas", "ideas"), ("In practice", "practice"), ("Watch out", "traps"), ("Wrap-up", "wrap")])
        for k, p in enumerate(d["parts"]):
            first = d["scenes"][p["first"]]
            self.assertEqual((first["kind"], first["n"], first["of"], first["part"]), ("part", k + 1, 4, k))
        self.assertEqual([s["part"] for s in d["scenes"][:2]], [None, None])
        self.assertEqual({s["part"] for s in d["scenes"] if s["kind"] == "slide"}, {0})
        self.assertEqual(d["scenes"][-1]["part"], 3)

    def test_the_title_scene(self):
        title = deck.compose(LESSON)["scenes"][0]
        self.assertEqual(title, {"kind": "title", "title": LESSON["title"], "summary": LESSON["summary"],
                                 "skill": "Plan and monitor widget sync", "part": None})

    def test_the_derived_agenda_lists_the_parts_and_their_slide_titles(self):
        agenda = deck.compose(LESSON)["scenes"][1]
        self.assertEqual((agenda["title"], agenda["outcomes"], agenda["items"]), ("In this lesson", False, []))
        ideas, practice, traps, wrap = agenda["parts"]
        self.assertEqual(ideas["items"], [s["title"] for s in LESSON["slides"][:deck.AGENDA_SLIDES]])
        self.assertEqual(ideas["more"], 3)
        self.assertEqual(practice["items"], ["Bringing back a device marked unreachable"])
        self.assertEqual(traps["items"], ["3 common traps"])
        self.assertEqual(wrap["items"], ["Recap", "Check yourself"])

    def test_the_traps_divider_never_shows_a_wrong_belief_on_its_own(self):
        d = deck.compose(LESSON)
        divider = next(s for s in d["scenes"] if s["kind"] == "part" and s["role"] == "traps")
        self.assertEqual((divider["items"], divider["count"]), ([], 3))
        wrong = [m["wrong"] for m in LESSON["misconceptions"]]
        for s in d["scenes"]:
            if s["kind"] != "trap":
                self.assertFalse(any(w in json.dumps(s) for w in wrong), s["kind"])
        first = next(s for s in d["scenes"] if s["kind"] == "trap")
        self.assertEqual((first["n"], first["of"], first["source"]), (1, 3, 1))
        self.assertEqual(first["right"], LESSON["misconceptions"][0]["right"])

    def test_the_example_is_a_process_scene(self):
        process = next(s for s in deck.compose(LESSON)["scenes"] if s["kind"] == "process")
        self.assertEqual(process["steps"], LESSON["example"]["steps"])
        self.assertEqual(process["situation"], LESSON["example"]["situation"])

    def test_the_derived_recap_is_the_slide_titles_and_the_check_takes_three(self):
        d = deck.compose(LESSON)
        recap = next(s for s in d["scenes"] if s["kind"] == "recap")
        titles = [s["title"] for s in LESSON["slides"]]
        self.assertEqual(recap["items"], [titles[0], titles[2], titles[4], titles[6], titles[8]])
        self.assertFalse(recap["own"])
        check = next(s for s in d["scenes"] if s["kind"] == "check")
        self.assertEqual([q["question"] for q in check["items"]], [q["question"] for q in LESSON["self_check"][:3]])
        self.assertEqual(set(check["items"][0]), {"question", "answer", "quote", "source"})

    def test_slide_scenes_carry_the_bullets_and_when_they_come_in(self):
        d = deck.compose(LESSON)
        slides = [s for s in d["scenes"] if s["kind"] == "slide"]
        self.assertEqual([s["slide"] for s in slides], list(range(9)))
        for s, sl in zip(slides, LESSON["slides"]):
            self.assertEqual(s["bullets"], sl["bullets"])
            self.assertEqual(len(s["cues"]["bullets"]), len(sl["bullets"]))
            self.assertEqual(s["cues"]["bullets"], sorted(s["cues"]["bullets"]))
            self.assertTrue(all(0 <= c < len(sl["narration"]) for c in s["cues"]["bullets"]))
            self.assertIsNone(s["visual"])
            self.assertNotIn("visual", s["cues"])

    def test_the_visual_deck(self):
        spec = spec_of()[0]
        d = deck.compose(LESSON, spec)
        self.assertEqual(d["level"], "visual")
        agenda = d["scenes"][1]
        self.assertEqual((agenda["title"], agenda["items"]), ("You will be able to", spec["outcomes"]))
        self.assertEqual([p["title"] for p in d["parts"]], ["How sync works", "Choosing a mode", "Keeping sync healthy",
                                                             "In practice", "Watch out", "Wrap-up"])
        self.assertEqual([p["items"] for p in agenda["parts"][:3]],
                         [["Every sync run has four stages", "Who can change sync"],
                          ["Three ways to sync", "Pick the mode for a site", "How often a device checks"],
                          ["When delivery fails", "Inside the sync agent", "Reading a sync.yaml file", "Rolling out a big change"]])
        dividers = [s for s in d["scenes"] if s["kind"] == "part"]
        self.assertEqual([(s["n"], s["of"]) for s in dividers], [(k, 6) for k in range(1, 7)])
        slides = {s["slide"]: s for s in d["scenes"] if s["kind"] == "slide"}
        self.assertEqual(slides[0]["visual"]["type"], "flow")
        self.assertEqual(slides[0]["cues"]["visual"], [1, 1, 1, 1])
        self.assertEqual(slides[0]["tip"], {k: spec["slides"]["0"]["tip"][k] for k in ("kind", "text", "quote", "source")})
        self.assertEqual(slides[0]["cues"]["tip"], 3)
        self.assertEqual(slides[1]["tip"], None)
        self.assertEqual(slides[1]["cues"]["visual"], [1, 2, 2])
        self.assertEqual(slides[4]["cues"]["visual"], [0])
        self.assertEqual(slides[7]["cues"]["visual"], [0, 0, 1, 3])
        recap = next(s for s in d["scenes"] if s["kind"] == "recap")
        self.assertEqual((recap["items"], recap["own"]), (spec["recap"], True))
        self.assertEqual(deck.visual_count(spec), 9)

    def test_a_tip_cue_past_the_last_line_comes_in_on_the_last(self):
        spec = spec_of()[0]
        spec["slides"]["0"]["tip"]["cue"] = 12
        slide = next(s for s in deck.compose(LESSON, spec)["scenes"] if s["kind"] == "slide")
        self.assertEqual(slide["cues"]["tip"], 3)

    def test_untitled_parts_are_numbered_and_no_outcomes_means_the_plain_agenda(self):
        spec = spec_of()[0]
        spec["sections"][1]["title"] = ""
        spec["outcomes"] = []
        d = deck.compose(LESSON, spec)
        self.assertEqual([p["title"] for p in d["parts"]][:3], ["How sync works", "Part 2", "Keeping sync healthy"])
        self.assertEqual(d["scenes"][1]["title"], "In this lesson")
        self.assertEqual(d["level"], "visual")

    def test_a_spec_with_no_parts_keeps_the_single_ideas_part(self):
        spec = spec_of()[0]
        spec["sections"] = []
        d = deck.compose(LESSON, spec)
        self.assertEqual([p["title"] for p in d["parts"]], ["The ideas", "In practice", "Watch out", "Wrap-up"])

    def test_a_short_lesson(self):
        lesson = {**LESSON, "example": {"title": "", "situation": "", "steps": []}, "misconceptions": [],
                  "self_check": [], "slides": LESSON["slides"][:2]}
        d = deck.compose(lesson)
        self.assertEqual(self.kinds(d), ["title", "agenda", "part", "slide", "slide", "part", "recap", "end"])
        self.assertEqual(d["scenes"][1]["parts"][-1]["items"], ["Recap"])
        recap = next(s for s in d["scenes"] if s["kind"] == "recap")
        self.assertEqual(recap["items"], [s["title"] for s in LESSON["slides"][:2]])

    def test_a_lesson_with_no_slides_left(self):
        d = deck.compose({**LESSON, "slides": []})
        self.assertEqual(self.kinds(d)[:3], ["title", "agenda", "part"])
        self.assertEqual([p["role"] for p in d["parts"]], ["practice", "traps", "wrap"])
        self.assertEqual(next(s for s in d["scenes"] if s["kind"] == "recap")["items"], [])

    def test_misconceptions_without_a_correction_are_not_traps(self):
        lesson = {**LESSON, "misconceptions": [{"wrong": "Only wrong", "right": ""}] + LESSON["misconceptions"][:1]}
        d = deck.compose(lesson)
        self.assertEqual(len([s for s in d["scenes"] if s["kind"] == "trap"]), 1)
        self.assertEqual(d["scenes"][1]["parts"][2]["items"], ["1 common trap"])

    def test_visual_count(self):
        self.assertEqual(deck.visual_count(None), 0)
        self.assertEqual(deck.visual_count({"slides": {"0": {"visual": None, "tip": {}}}}), 0)


# ------------------------------------------------------------ drawing: the deck code of app.js, run in node

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")
APP_JS = ROOT / "app" / "static" / "app.js"
# The boxes a diagram is drawn in, in canvas pixels, as the player measures them: the whole stage, the part
# beside a bullet rail, and the tall card of a phone.
WIDE, RAIL, TALL = (1128, 364, False), (704, 364, False), (386, 400, True)

DRAW = r"""
const vm = require("vm");
const START = APP.indexOf("// deck: pure begin"), END = APP.indexOf("// deck: pure end");
if (START < 0 || END < START) throw new Error("the pure deck code is not where it was");
const ctx = vm.createContext({});
vm.runInContext(APP.slice(START, END) + "\nthis.API = { DECK_LAYOUTS, DECK_SIZES, deckScale, deckAlt, deckPlan, deckTimes, deckWrap, deckFit, deckLineAt, deckGroups, deckRail };", ctx);
const A = ctx.API;
// Each character takes a fixed share of the font size: a little wider than the real fonts on average.
const SHARE = { mono: 0.6, serif: 0.52, serifi: 0.47, bold: 0.58, label: 0.56, note: 0.53 };
const M = (t, size, font) => String(t).length * size * (SHARE[font] || 0.56);
const src = (n) => `Page ${n}`;
const lineText = (ln) => (Array.isArray(ln) ? ln.map((tk) => tk.t).join("") : ln);
function bounds(s) {
  if (s.k === "rect") return [s.x, s.y, s.x + s.w, s.y + s.h];
  if (s.k === "circle") return [s.cx - s.r, s.cy - s.r, s.cx + s.r, s.cy + s.r];
  if (s.k === "icon") return [s.x, s.y, s.x + s.s, s.y + s.s];
  if (s.k === "text") {
    const w = Math.max(0, ...s.lines.map((ln) => M(lineText(ln), s.size, s.f)));
    const x0 = s.a === "middle" ? s.x - w / 2 : s.a === "end" ? s.x - w : s.x;
    return [x0, s.y, x0 + w, s.y + s.lines.length * s.lh];
  }
  // a path: the points it goes through and its control points; an arc is taken at its end point
  const d = s.d.replace(/A[^A-Za-z]*/g, (m) => { const p = m.slice(1).trim().split(/[\s,]+/); return `L${p[5]} ${p[6]}`; });
  const xs = [], ys = [], re = /([MLHVCQZ])([^MLHVCQZ]*)/g;
  let x = 0, y = 0, m;
  while ((m = re.exec(d))) {
    const p = m[2].trim().split(/[\s,]+/).filter(Boolean).map(Number);
    if (m[1] === "Z") continue;
    if (m[1] === "H" || m[1] === "V") {
      if (m[1] === "H") x = p[0]; else y = p[0];
      xs.push(x); ys.push(y);
      continue;
    }
    for (let i = 0; i + 1 < p.length; i += 2) { x = p[i]; y = p[i + 1]; xs.push(x); ys.push(y); }
  }
  return xs.length ? [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)] : null;
}
// A diagram is drawn the way the player draws it: fitted to its box by deckScale, at a scale `s` and laid out
// 1/s as wide.
function draw(c) {
  const fit = A.deckScale(c.v, c.W, c.H, c.tall, { M, S: c.tall ? A.DECK_SIZES.tall : A.DECK_SIZES.wide, src });
  const lay = fit.lay, W = c.W / fit.s;
  const all = [...(lay.back || []), ...lay.base, ...lay.parts.flat(), ...(lay.under || []).flat()];
  const outside = [];
  for (const s of all) {
    const b = bounds(s);
    if (b && (b[0] < -0.6 || b[1] < -0.6 || b[2] > W + 0.6 || b[3] > lay.h + 0.6)) outside.push(`${s.k} ${s.c || s.f || s.name} ${b.map((n) => n.toFixed(1))}`);
  }
  const texts = all.filter((s) => s.k === "text").map((s) => ({ c: s.c, size: s.size, lines: s.lines.map(lineText) }));
  return { h: lay.h, s: fit.s, scroll: fit.scroll, parts: lay.parts.length, empty: lay.parts.filter((p) => !p.length).length, under: (lay.under || []).length,
    outside, texts, alt: A.deckAlt(c.v, src), rail: A.deckRail(c.v) };
}
const plan = (p) => {
  const r = A.deckPlan(p.sc, p.texts, p.dur);
  return { end: isFinite(r.end) ? r.end : -1, lines: r.lines, marks: r.marks };
};
console.log(JSON.stringify({
  draw: DATA.draw.map(draw),
  plan: DATA.plan.map(plan),
  times: DATA.times.map((t) => A.deckTimes(t.texts, t.dur)),
  lineAt: DATA.lineAt.map((q) => A.deckLineAt(A.deckTimes(q.texts, q.dur), q.t)),
  wrap: DATA.wrap.map((w) => A.deckWrap(w.text, w.width, w.size, "label", M)),
  fit: DATA.fit.map((f) => A.deckFit(f.text, f.width, f.sizes, "label", M, f.most)),
  groups: DATA.groups.map((D) => A.deckGroups(D)),
}));
"""


def node(data: dict) -> dict:
    prelude = f"const DATA = {json.dumps(data)};\nconst APP = require('fs').readFileSync({json.dumps(str(APP_JS))}, 'utf8');\n"
    done = subprocess.run([NODE, "-"], input=prelude + DRAW, capture_output=True, text=True, encoding="utf-8", timeout=180)
    if done.returncode != 0:
        raise AssertionError(f"node failed:\n{done.stderr}")
    return json.loads(done.stdout)


def plain(text) -> str:
    return " ".join(str(text).split()).casefold()


def drawn_texts(v: dict) -> list[str]:
    """The texts of a diagram that must be drawn, each whole. The lines of code are checked one by one."""
    kind = v["type"]
    if kind in ("flow", "cycle", "stack", "timeline", "hub"):
        items = v.get("steps") or v.get("layers") or v.get("events") or v.get("spokes")
        return [t for it in items for t in (it.get("when"), it["label"], it["note"]) if t] + ([v["center"]] if kind == "hub" else [])
    if kind == "compare":
        return v["columns"] + [t for r in v["rows"] for t in [r["label"]] + r["cells"]]
    if kind == "contrast":
        return [t for s in (v["left"], v["right"]) for t in [s["title"]] + s["points"]]
    if kind == "matrix":
        return [v[a][k] for a in ("x", "y") for k in ("label", "low", "high")] + [t for c in v["cells"] for t in (c["label"], c["note"]) if t]
    if kind == "keynum":
        return [t for t in (v["value"], v["unit"], v["label"], v["quote"]) if t]
    return [m["note"] for m in v["marks"]]


def alt_text(alt: dict) -> str:
    bits = [alt["label"]]
    for b in alt["blocks"]:
        bits += [b.get("intro") or "", b.get("text") or ""] + list(b.get("items") or []) + list(b.get("head") or [])
        bits += [c for r in b.get("rows") or [] for c in r]
    return plain(" ".join(bits))


WORDS = ("settings reach every device through the hub before the quiet window ends while the relay keeps the "
         "last good copy and each agent checks in on time after a failed run").split()


def words(cap: int, k: int = 0) -> str:
    """Ordinary words, as many as fit in `cap` characters: the longest text a diagram may hold."""
    out = []
    while len(" ".join(out + [WORDS[k % len(WORDS)]])) <= cap:
        out.append(WORDS[k % len(WORDS)])
        k += 1
    return " ".join(out).capitalize()


def item(k: int, icon: str | None = "check") -> dict:
    return {"label": words(deck.LABEL, k), "note": words(deck.NOTE, k + 7), "icon": icon, "cue": None}


def largest() -> list[dict]:
    """Each kind of diagram at the most parts and the longest texts clean_visual lets through."""
    icons = ["check", "shield", "download", "share", "clock", "flag"]
    side = lambda k, tone: {"title": words(deck.HEAD, k), "tone": tone, "icon": "link", "points": [words(deck.POINT, k + j) for j in range(4)]}
    return [
        {"type": "flow", "steps": [item(k, icons[k]) for k in range(6)]},
        {"type": "cycle", "steps": [item(k, icons[k]) for k in range(6)]},
        {"type": "stack", "layers": [item(k, icons[k]) for k in range(5)]},
        {"type": "timeline", "events": [{**item(k), "when": f"Week {k + 10}, day {k + 1}"} for k in range(6)]},
        {"type": "hub", "center": words(deck.LABEL, 3), "icon": "people", "spokes": [item(k, icons[k]) for k in range(6)]},
        {"type": "compare", "columns": [words(deck.HEAD, k) for k in range(3)],
         "rows": [{"label": words(deck.HEAD, k + 3), "cells": [words(deck.CELL, k + j) for j in range(3)], "cue": None} for k in range(5)]},
        {"type": "compare", "columns": ["Push", "Pull", "Mirror"],
         "rows": [{"label": words(deck.HEAD, k), "cells": ["yes", "no", "Yes"], "cue": None} for k in range(5)]},
        {"type": "contrast", "left": side(0, "bad"), "right": side(5, "good")},
        {"type": "matrix", "x": {"label": words(deck.HEAD, 1), "low": words(deck.HEAD, 2), "high": words(deck.HEAD, 3)},
         "y": {"label": words(deck.HEAD, 4), "low": words(deck.HEAD, 5), "high": words(deck.HEAD, 6)},
         "cells": [{"x": x, "y": y, "label": words(deck.LABEL, 2 * y + x), "note": words(deck.NOTE, 2 * y + x + 4), "cue": None}
                   for y in (0, 1) for x in (0, 1)]},
        {"type": "keynum", "value": "1,000.50", "unit": words(deck.UNIT, 2), "label": words(deck.KEYLABEL, 1),
         "quote": "1,000.50 " + words(deck.QUOTE - 9, 4), "source": 1},
        {"type": "code", "lang": "yaml", "source": 1,
         "lines": [("  " * (k % 3)) + f"key_{k}: " + words(deck.CODE_LINE - 8 - 2 * (k % 3), k).lower() for k in range(deck.CODE_LINES)],
         "marks": [{"line": k * 3, "note": words(deck.NOTE, k), "cue": None} for k in range(deck.MARKS)]},
    ]


def boxes(v: dict) -> list[tuple]:
    """Where a diagram can be drawn: beside the bullets only when it is narrow enough (deckRail)."""
    railed = v["type"] in ("stack", "keynum") or (v["type"] == "flow" and len(v["steps"]) <= 3) or \
        (v["type"] == "timeline" and len(v["events"]) <= 3)
    return [WIDE, TALL] + ([RAIL] if railed else [])


@unittest.skipUnless(NODE, "node is not installed")
class DrawingTests(unittest.TestCase):
    """The deck code of app.js that has no page: the ten diagrams, the timing of a scene, line wrapping and
    the parts of the progress bar. Text is measured with a fixed width a character, so what is checked is
    the geometry: every shape inside its box, every word drawn and in the text alternative, and one build
    for every cue app/deck.py gave."""

    @classmethod
    def setUpClass(cls):
        spec = spec_of()[0]
        cls.fixture = [e["visual"] for e in spec["slides"].values() if e.get("visual")] + [visual(RAW["alternates"]["2"])[0]]
        cls.largest = largest()
        cls.cases = [(v, b) for v in cls.fixture + cls.largest for b in boxes(v)]
        cls.decks = [deck.compose(LESSON), deck.compose(LESSON, spec)]
        cls.plans = [(d, s, dur) for d in cls.decks for s in d["scenes"] for dur in (0, 61000)]
        texts = [ln["text"] for ln in LESSON["slides"][0]["narration"]]
        cls.times = [(texts, 0), (texts, 20000), (["One line only."], 5000), ([], 0)]
        cls.line_at = [(texts, 20000, t) for t in (-1, 0, 4000, 19999, 20000, 20749, 20751, 99999)]
        cls.wraps = [(words(400, k), width, size) for k in range(3) for width in (90, 200, 376, 1128) for size in (16, 24)]
        cls.wraps.append(("Supercalifragilisticexpialidocious-configuration-file-name and more", 120, 20))
        # the medium text takes four lines at 24 and 22 and three at 20: the fit is 20
        cls.fits = [("Short", 300, [24, 20], 1), (words(120), 450, [24, 22, 20, 18], 3), (words(400), 100, [24, 20], 2)]
        cls.wraps += [(cls.fits[1][0], cls.fits[1][1], size) for size in cls.fits[1][2]]
        cls.out = node({
            "draw": [{"v": v, "W": b[0], "H": b[1], "tall": b[2]} for v, b in cls.cases],
            "plan": [{"sc": s, "texts": [ln.get("text", "") for ln in LESSON["slides"][s["slide"]]["narration"]] if s["kind"] == "slide" else [],
                      "dur": dur} for _, s, dur in cls.plans],
            "times": [{"texts": t, "dur": dur} for t, dur in cls.times],
            "lineAt": [{"texts": t, "dur": dur, "t": at} for t, dur, at in cls.line_at],
            "wrap": [{"text": t, "width": w, "size": s} for t, w, s in cls.wraps],
            "fit": [{"text": t, "width": w, "sizes": s, "most": m} for t, w, s, m in cls.fits],
            "groups": cls.decks,
        })

    def test_the_fixture_has_every_kind_of_diagram(self):
        self.assertEqual(sorted(v["type"] for v in self.fixture), sorted(deck.VISUALS))
        self.assertEqual(sorted({v["type"] for v in self.largest}), sorted(deck.VISUALS))

    def each(self):
        return [(v, b, got, f"{v['type']} {'largest' if v in self.largest else 'fixture'} {b[0]}x{b[1]}")
                for (v, b), got in zip(self.cases, self.out["draw"])]

    def test_every_shape_stays_inside_its_box(self):
        for v, b, got, name in self.each():
            with self.subTest(name):
                self.assertEqual(got["outside"], [])
                self.assertGreater(got["h"], 0)

    def test_every_text_is_drawn_whole(self):
        for v, b, got, name in self.each():
            with self.subTest(name):
                shown = [plain(" ".join(t["lines"])) for t in got["texts"]]
                for text in drawn_texts(v):
                    self.assertTrue(any(plain(text) in s for s in shown), text)

    def test_the_code_is_drawn_line_for_line(self):
        for v, b, got, name in self.each():
            with self.subTest(name):
                if v["type"] != "code":
                    continue
                numbers = next(t for t in got["texts"] if "dkv-lnum" in t["c"])["lines"]
                rows = next(t for t in got["texts"] if "dkv-ctext" in t["c"])["lines"]
                self.assertEqual([n for n in numbers if n], [str(k + 1) for k in range(len(v["lines"]))])
                lines = []
                for n, row in zip(numbers, rows):
                    if n:
                        lines.append(row)
                    else:
                        lines[-1] += " " + row
                self.assertEqual([" ".join(x.split()) for x in lines], [" ".join(x.split()) for x in v["lines"]])
                self.assertEqual([r[:len(r) - len(r.lstrip())] for r, n in zip(rows, numbers) if n],
                                 [x[:len(x) - len(x.lstrip())] for x in v["lines"]])

    def test_the_text_alternative_says_everything_the_picture_does(self):
        for v, b, got, name in self.each():
            with self.subTest(name):
                alt = alt_text(got["alt"])
                self.assertTrue(got["alt"]["label"])
                for text in drawn_texts(v):
                    self.assertIn(plain(text), alt)
                if v["type"] == "code":
                    self.assertEqual(got["alt"]["blocks"][0], {"kind": "pre", "text": "\n".join(v["lines"])})

    def test_one_build_for_every_cue(self):
        for v, b, got, name in self.each():
            with self.subTest(name):
                parts, _ = deck._visual_parts(v)
                self.assertEqual(got["parts"], len(parts))
                self.assertEqual(got["empty"], 0)
                self.assertEqual(got["under"], len(v["marks"]) if v["type"] == "code" else 0)

    def test_the_text_stays_readable(self):
        """The player fits a diagram to its box with deckScale: smaller text first, or scaled down and laid out
        that much wider, never below DECK_LEAST. Even at the most a diagram may hold, its smallest text stays
        at least 12 canvas pixels on the stage and 11 on a phone (the canvas is scaled to the page). On the
        stage every diagram fits its box, and the fixture's at full size; on a phone a long one scrolls."""
        for v, b, got, name in self.each():
            with self.subTest(name):
                smallest = min(t["size"] for t in got["texts"] if "dkv-lnum" not in t["c"]) * got["s"]
                self.assertGreaterEqual(smallest, 11 if b[2] else 12, f"s={got['s']} h={got['h']}")
                if not got["scroll"]:
                    self.assertLessEqual(got["h"] * got["s"], b[1] + 0.5)
                if not b[2]:
                    self.assertFalse(got["scroll"])
                    if v in self.fixture:
                        self.assertEqual(got["s"], 1)

    def test_the_rail_is_for_narrow_diagrams(self):
        for v, b, got, name in self.each():
            with self.subTest(name):
                self.assertEqual(got["rail"], RAIL in boxes(v))

    def test_a_scene_plan_stays_within_the_scene(self):
        for (d, s, dur), got in zip(self.plans, self.out["plan"]):
            with self.subTest(kind=s["kind"], level=d["level"], dur=dur):
                if s["kind"] in ("check", "end"):
                    self.assertEqual(got["end"], -1)    # they wait for the learner
                    continue
                self.assertGreater(got["end"], 0)
                self.assertLessEqual(got["end"], 90000)
                for key, m in got["marks"].items():
                    self.assertTrue(0 <= m["at"] <= m["till"] <= got["end"], (key, m))

    def test_a_slide_builds_on_its_narration(self):
        for (d, s, dur), got in zip(self.plans, self.out["plan"]):
            if s["kind"] != "slide":
                continue
            with self.subTest(slide=s["slide"], level=d["level"], dur=dur):
                lines, marks, cues = got["lines"], got["marks"], s["cues"]
                self.assertEqual(len(lines), len(LESSON["slides"][s["slide"]]["narration"]))
                self.assertAlmostEqual(got["end"], lines[-1]["t1"] + 1400)
                keys = [f"b{j}" for j in range(len(s["bullets"]))] + [f"v{j}" for j in range(len(cues.get("visual", [])))]
                self.assertEqual(sorted(marks), sorted(keys + (["tip"] if s["tip"] else [])))
                for prefix, cs in (("b", cues["bullets"]), ("v", cues.get("visual", []))):
                    for j, c in enumerate(cs):
                        m = marks[f"{prefix}{j}"]
                        self.assertTrue(lines[c]["t0"] <= m["at"] < lines[c]["t1"], (prefix, j, c))
                if s["tip"]:
                    self.assertEqual(marks["tip"]["at"], lines[cues["tip"]]["t0"])

    def test_the_lines_share_out_the_audio_or_follow_a_clock(self):
        texts = self.times[0][0]
        clock, audio, one, none = self.out["times"]
        self.assertEqual(none, [])
        self.assertEqual(one, [{"t0": 0, "t1": 5000}])
        self.assertEqual(audio[0]["t0"], 0)
        self.assertAlmostEqual(audio[-1]["t1"], 20000)
        for a, b in zip(audio, audio[1:]):
            self.assertAlmostEqual(a["t1"], b["t0"])
        longest = max(range(len(texts)), key=lambda k: len(texts[k]))
        spans = [x["t1"] - x["t0"] for x in audio]
        self.assertEqual(max(range(len(spans)), key=lambda k: spans[k]), longest)
        for x, text in zip(clock, texts):
            self.assertAlmostEqual(x["t1"] - x["t0"], max(900, 1000 * len(text) / 14))
        for a, b in zip(clock, clock[1:]):
            self.assertAlmostEqual(b["t0"] - a["t1"], 350)

    def test_the_line_being_spoken(self):
        audio = self.out["times"][1]
        got = dict(zip([at for _, _, at in self.line_at], self.out["lineAt"]))
        self.assertEqual(got[-1], -1)
        self.assertEqual(got[0], 0)
        self.assertEqual(got[4000], next(k for k, x in enumerate(audio) if x["t0"] <= 4000 < x["t1"]))
        self.assertEqual(got[19999], len(audio) - 1)
        self.assertEqual(got[20749], len(audio) - 1)    # kept up through the pause after it
        self.assertEqual(got[20751], -1)
        self.assertEqual(got[99999], -1)

    def test_wrapping_never_drops_a_word(self):
        for (text, width, size), lines in zip(self.wraps, self.out["wrap"]):
            with self.subTest(width=width, size=size):
                long_words = [w for w in text.split() if len(w) * size * 0.56 > width]
                if not long_words:
                    self.assertEqual(" ".join(lines).split(), text.split())
                    for ln in lines:
                        self.assertLessEqual(len(ln) * size * 0.56, width + 1e-6)
                else:
                    self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))
                    for ln in lines:
                        self.assertLessEqual(len(ln) * size * 0.56, width + 1e-6)

    def test_fitting_takes_the_largest_size_that_fits(self):
        short, medium, long = self.out["fit"]
        self.assertEqual((short["size"], short["ok"], short["lines"]), (24, True, ["Short"]))
        text, width, sizes, most = self.fits[1]
        counts = {size: len(lines) for (t, w, size), lines in zip(self.wraps, self.out["wrap"]) if (t, w) == (text, width)}
        self.assertEqual(medium["size"], next(size for size in sizes if counts[size] <= most))
        self.assertGreater(counts[sizes[0]], most)
        self.assertTrue(medium["ok"])
        self.assertEqual((long["size"], long["ok"]), (20, False))
        self.assertEqual(" ".join(long["lines"]).split(), self.fits[2][0].split())

    def test_the_progress_bar_parts_cover_every_scene_once(self):
        for d, groups in zip(self.decks, self.out["groups"]):
            with self.subTest(level=d["level"]):
                self.assertEqual(groups[0], {"label": "Start", "role": "intro", "from": 0, "to": d["parts"][0]["first"]})
                self.assertEqual([g["label"] for g in groups[1:]], [p["title"] for p in d["parts"]])
                self.assertEqual(groups[-1]["to"], len(d["scenes"]))
                for a, b in zip(groups, groups[1:]):
                    self.assertEqual(a["to"], b["from"])
                    self.assertLess(a["from"], a["to"])


class PlayerWiringTests(unittest.TestCase):
    """How the player is wired into the page, which the drawing tests above do not reach."""

    @classmethod
    def setUpClass(cls):
        js = APP_JS.read_text("utf-8")
        cls.player = js.split("function deckPlayer(L) {", 1)[1].split("\nfunction lessonPresent(", 1)[0]
        cls.stop = js.split("function stopClock() {", 1)[1].split("\n}", 1)[0]

    def test_exposure_is_recorded_only_when_a_scene_plays(self):
        play = self.player.split("function play() {", 1)[1].split("\n  function ", 1)[0]
        self.assertEqual(play.count('markViewed(L.id, "present")'), 1)
        # Play on the self-check moves to the closing scene, which does not play: no exposure for that.
        held = play.index('kind === "end") playing = false;')
        self.assertGreater(play.index('markViewed(L.id, "present")'), held)
        self.assertIn("else {", play[held:play.index('markViewed(L.id, "present")')])
        self.assertEqual(self.player.count("markViewed("), 1)

    def test_leaving_the_page_takes_the_watchers_down(self):
        detach = self.player.split("function detach() {", 1)[1].split("\n  }", 1)[0]
        for part in ("ro.disconnect()", 'window.removeEventListener("resize", onResize)',
                     'phone.removeEventListener("change", onResize)', 'document.removeEventListener("keydown", onKey)',
                     "cancelAnimationFrame(raf)", "audio.pause()", "state.deckOff = null"):
            self.assertIn(part, detach)
        boot = self.player.split("async function boot() {", 1)[1]
        self.assertLess(boot.index("if (state.deckOff) state.deckOff();"), boot.index("state.deckOff = detach;"))
        self.assertIn("ro = new ResizeObserver(onResize)", boot)
        self.assertIn("if (state.deckOff) state.deckOff();", self.stop)   # every route calls stopClock first

    def test_only_a_player_on_the_page_listens_to_keys(self):
        """A lesson answer that arrives after the learner moved on still builds a player, which never boots.
        It must not take the keys of the page the learner is on now."""
        self.assertEqual(self.player.count('document.addEventListener("keydown", onKey)'), 1)
        boot = self.player.split("async function boot() {", 1)[1]
        mounted = boot.index("if (state.nav !== nav) return;")
        self.assertGreater(boot.index('document.addEventListener("keydown", onKey)'), mounted)
        self.assertGreater(boot.index("state.keys = onKey;"), boot.index("if (!root.isConnected) return;"))


if __name__ == "__main__":
    unittest.main()
