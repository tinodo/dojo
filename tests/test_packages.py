"""Exam packages: the schema check, and added packages kept on the data share next to the built-ins."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.core import Store, UserError
from app.packages import Packages, validate

BUILT_IN = Path(__file__).resolve().parents[1] / "app" / "packages"


def small_package(code: str = "XY-110") -> dict:
    """The smallest package the schema accepts, in the shape the built-in packages have."""
    src = {"url": "https://learn.microsoft.com/en-us/azure/widgets/overview", "title": "Widgets overview",
           "publisher": "Microsoft Learn", "license": "CC-BY-4.0", "use": "ground"}
    skill = lambda sid, text, sources: {"id": sid, "text": text, "sources": sources, "learn": []}  # noqa: E731
    return {
        "id": code.lower(), "exam": code, "title": "Contoso Widget Operations", "issuer": "Microsoft",
        "kind": "certification", "version": "skills-2026-03-03",
        "source": {"name": f"Study guide for Exam {code}: Contoso Widget Operations",
                   "url": f"https://learn.microsoft.com/en-us/credentials/certifications/resources/study-guides/{code.lower()}",
                   "skills_as_of": "2026-03-03", "retrieved": "2026-09-29", "git_commit_id": None},
        "passing_score": 700, "official_practice_assessment": None, "exam_page": None,
        "exam_format": {"duration_minutes": 100, "duration_quote": "Exam duration: 100 minutes", "question_count": None},
        "notes": ["Added by the owner on the Add an exam page."], "added": {"at": "2026-09-29T10:00:00Z"},
        "domains": [
            {"id": "d1", "title": "Plan widget deployments", "weight_min": 30, "weight_max": 35, "groups": [
                {"title": "Choose a widget model", "skills": [
                    skill("d1.g1.s1.aaaaaa", "Choose a widget model for a workload", [src]),
                    skill("d1.g1.s2.bbbbbb", "Describe widget sizes", [])]}]},
            {"id": "d2", "title": "Operate widgets", "weight_min": 40, "weight_max": 45, "groups": [
                {"title": "Configure widgets", "skills": [
                    skill("d2.g1.s1.cccccc", "Configure widget alerts by using the Contoso portal", [src])]}]},
        ],
    }


class ValidateTests(unittest.TestCase):
    def test_the_built_in_packages_pass_the_check_added_packages_must_pass(self):
        for f in BUILT_IN.glob("*.json"):
            with self.subTest(package=f.name):
                validate(json.loads(f.read_text("utf-8")))

    def test_a_small_package_passes(self):
        validate(small_package())

    def test_what_is_refused(self):
        cases = {
            "source on another host": lambda p: p["domains"][0]["groups"][0]["skills"][0]["sources"][0].update(url="https://example.com/x"),
            "weights upside down": lambda p: p["domains"][0].update(weight_min=40, weight_max=30),
            "weight not a number": lambda p: p["domains"][0].update(weight_min="30"),
            "same skill id twice": lambda p: p["domains"][1]["groups"][0]["skills"][0].update(id="d1.g1.s1.aaaaaa"),
            "id and exam differ": lambda p: p.update(id="xy-111"),
            "code shape": lambda p: p.update(exam="XY110", id="xy110"),
            "other issuer": lambda p: p.update(issuer="Contoso"),
            "too many sources": lambda p: p["domains"][0]["groups"][0]["skills"][0].update(sources=[{}] * 4),
            "source that does not ground": lambda p: p["domains"][0]["groups"][0]["skills"][0]["sources"][0].update(use="read"),
            "group without skills": lambda p: p["domains"][1]["groups"][0].update(skills=[]),
            "too few skills": lambda p: p["domains"].pop(),
            "duration out of range": lambda p: p["exam_format"].update(duration_minutes=5),
            "practice elsewhere": lambda p: p.update(official_practice_assessment={"url": "https://example.com/practice"}),
            "notes not text": lambda p: p.update(notes=[1]),
        }
        for name, change in cases.items():
            with self.subTest(name):
                raw = small_package()
                change(raw)
                with self.assertRaises(ValueError):
                    validate(raw)


class AddedPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_added_packages_load_at_startup_next_to_the_built_ins(self):
        self.store.write("packages", "xy-110", value=small_package())
        broken = small_package("XY-120")
        broken["domains"][0]["weight_min"] = 90
        self.store.write("packages", "xy-120", value=broken)
        self.store.write("packages", "gh-300", value=small_package("GH-300"))  # cannot replace a built-in
        self.store.write("packages", "xy-130", value=small_package("XY-131"))  # the file name must match
        packages = Packages(BUILT_IN, self.store)
        self.assertEqual(packages.ids(), ["dp-800", "gh-300", "xy-110"])
        self.assertTrue(packages.is_added("xy-110"))
        self.assertFalse(packages.is_added("gh-300"))
        self.assertEqual(packages.get("gh-300")["meta"]["issuer"], "GitHub")
        pkg = packages.get("xy-110")
        self.assertEqual(pkg["meta"]["added"], {"at": "2026-09-29T10:00:00Z"})
        self.assertEqual(len(pkg["skills"]), 3)
        self.assertEqual(pkg["skills"]["d1.g1.s1.aaaaaa"]["kind"], "scenario")
        self.assertEqual(pkg["skills"]["d1.g1.s2.bbbbbb"]["kind"], "explanation")
        self.assertAlmostEqual(sum(s["share"] for s in pkg["skills"].values()), 1.0)

    def test_add_and_remove(self):
        packages = Packages(BUILT_IN, self.store)
        before = packages.by_id
        packages.add(small_package())
        self.assertIn("xy-110", packages.by_id)
        self.assertNotIn("xy-110", before, "the old mapping is left alone for code that is looping over it")
        self.assertEqual(self.store.read("packages", "xy-110")["exam"], "XY-110")
        self.assertIn("xy-110", Packages(BUILT_IN, self.store).by_id, "an added exam is there after a restart")
        packages.remove("xy-110")
        self.assertNotIn("xy-110", packages.by_id)
        self.assertIsNone(self.store.read("packages", "xy-110"))
        with self.assertRaises(HTTPException):
            packages.remove("xy-110")

    def test_built_in_packages_cannot_be_replaced_or_removed(self):
        packages = Packages(BUILT_IN, self.store)
        with self.assertRaises(UserError):
            packages.add(small_package("GH-300"))
        with self.assertRaises(UserError):
            packages.remove("gh-300")
        self.assertIn("gh-300", packages.by_id)

    def test_a_package_that_fails_the_check_is_not_kept(self):
        packages = Packages(BUILT_IN, self.store)
        raw = copy.deepcopy(small_package())
        raw["domains"][0]["groups"][0]["skills"][0]["sources"][0]["url"] = "https://evil.example/page"
        with self.assertRaises(UserError):
            packages.add(raw)
        self.assertEqual(self.store.names("packages"), [])
        self.assertNotIn("xy-110", packages.by_id)


if __name__ == "__main__":
    unittest.main()
