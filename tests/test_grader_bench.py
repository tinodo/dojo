"""The grader bench (tools/grader_bench.py, ADR 0016) without the models: its case files are well formed, and its
comparison counts agreement and both kinds of disagreement the way the app counts a point (met and verified)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import grader_bench as bench  # noqa: E402


class BenchCases(unittest.TestCase):
    def test_every_case_file_is_well_formed_and_balanced(self):
        cases = bench.load_cases()
        self.assertGreaterEqual(len(cases), 6)
        expected = [v for c in cases for a in c["answers"] for v in a["expect"].values()]
        self.assertGreaterEqual(len(expected), 140)
        self.assertGreater(sum(expected), len(expected) // 3, "enough points that should be met")
        self.assertGreater(len(expected) - sum(expected), len(expected) // 3, "enough points that should not be")
        for c in cases:
            kinds = {a["id"] for a in c["answers"]}
            self.assertIn("strong-prose", kinds, c["file"])
            self.assertIn("injection", kinds, c["file"])
            self.assertTrue(c["item"]["sources"] and all(s["url"].startswith("https://") for s in c["item"]["sources"]))
            self.assertTrue(all(r["point"] for r in c["item"]["rubric"]))

    def test_a_case_whose_expectations_miss_a_point_is_refused(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            case = {"item": {"rubric": [{"id": "r1", "point": "x"}, {"id": "r2", "point": "y"}], "sources": []},
                    "answers": [{"id": "a", "text": "t", "expect": {"r1": True}}]}
            Path(d, "x.json").write_text(json.dumps(case), "utf-8")
            with self.assertRaises(ValueError):
                bench.load_cases(Path(d))


class Compare(unittest.TestCase):
    RESULT = {"points": [{"id": "r1", "met": True, "verified": True}, {"id": "r2", "met": True, "verified": False},
                         {"id": "r3", "met": False, "verified": True}, {"id": "r4", "met": True, "verified": True}]}

    def test_a_point_counts_as_met_only_when_met_and_verified(self):
        rows = bench.compare({"r1": True, "r2": True, "r3": False, "r4": False}, self.RESULT)
        self.assertEqual([r["verdict"] for r in rows], ["agree", "too_harsh", "agree", "too_generous"])

    def test_the_summary_counts_agreement_referee_and_stability(self):
        judged = [{"id": "r1", "met": True, "verified": True, "refereed": {"grader_met": False}},
                  {"id": "r4", "met": True, "verified": True, "refereed": {"grader_met": True}}]
        first = {"case": "c", "answer": "a", "points": bench.compare({"r1": True, "r2": True, "r3": False, "r4": False}, self.RESULT),
                 "judged": judged}
        again = {"case": "c", "answer": "a", "points": bench.compare({"r1": True, "r2": True, "r3": False, "r4": False},
                                                                     {"points": self.RESULT["points"][:3]}), "judged": []}
        s = bench.summarize([first, again, {"case": "c", "answer": "b", "error": "AIError: down"}])
        self.assertEqual((s["answers"], s["failed"], s["points"]), (2, 1, 8))
        self.assertEqual((s["agree"], s["too_generous"], s["too_harsh"]), (5, 1, 2))
        self.assertEqual(s["agreement"], round(5 / 8, 3))
        self.assertEqual((s["refereed"], s["referee_changed"], s["unstable"]), (2, 1, 1))


if __name__ == "__main__":
    unittest.main()
