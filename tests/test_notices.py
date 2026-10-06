"""THIRD-PARTY-NOTICES.md names every package requirements.txt installs, with its license.

The versions live only in requirements.txt, so a dependency bump cannot leave the notices out of date.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pinned() -> list[str]:
    names = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(re.split(r"[=<>!~\[; ]", line, maxsplit=1)[0].lower())
    return names


def package_rows() -> dict[str, str]:
    text = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    section = text.split("## Python packages", 1)[1].split("\n## ", 1)[0]
    rows = re.finditer(r"^\| \[([^\]]+)\]\(https://pypi\.org/project/[^)]+\)[^|]*\| ([^|]*?) \|", section, re.M)
    return {m.group(1).lower(): m.group(2).strip() for m in rows}


class NoticesTests(unittest.TestCase):
    def test_every_installed_package_is_listed_with_a_license(self):
        names, rows = pinned(), package_rows()
        self.assertGreater(len(names), 5)
        for name in names:
            self.assertIn(name, rows, f"{name} is in requirements.txt but not in THIRD-PARTY-NOTICES.md")
            self.assertTrue(rows[name], f"{name} has no license in THIRD-PARTY-NOTICES.md")

    def test_no_package_is_listed_that_is_not_installed(self):
        self.assertEqual(sorted(package_rows()), sorted(set(pinned())))

    def test_versions_live_only_in_requirements(self):
        text = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        self.assertNotIn("| Version |", text)
        self.assertIn("pinned in [`requirements.txt`](requirements.txt)", text)


if __name__ == "__main__":
    unittest.main()