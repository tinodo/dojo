"""No point is met by leaving something out (ADR 0015). A rubric point phrased as a prohibition, such as
"Do not use a heap for the order table", can look met when an answer never mentions the subject. The
grader is told that silence never meets a point, a "met" still needs the learner's own words, and the item
writer must state every new point as what a correct answer states or does. Items written before stay as
they are."""
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-fair-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ai as ai_mod  # noqa: E402
from app.core import Learner, learner_key  # noqa: E402
from app.learning import GRADE_SYSTEM, ITEM_SYSTEM, rubric_prohibition  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402


class PromptTests(unittest.TestCase):
    def test_the_grader_is_told_that_silence_never_meets_a_point(self):
        self.assertIn("A point is met only by what the answer itself says. Not mentioning something never meets a point.",
                      GRADE_SYSTEM)
        self.assertIn('A point phrased as what not to do ("Do not ...", "Avoid ...") is met only when the answer states '
                      "the right choice or rejects the wrong one in its own words", GRADE_SYSTEM)

    def test_the_item_writer_is_told_to_state_every_point_positively(self):
        self.assertIn('Never write a point as a prohibition ("Do not ...", "Avoid ...", "Never ...")', ITEM_SYSTEM)


class ProhibitionTests(unittest.TestCase):
    def test_a_point_that_starts_as_a_prohibition_is_recognised(self):
        for point in ("Do not keep the order notes in the order table; if notes are kept, put them in a table of "
                      "their own.",
                      "Don't keep the Notes column in the archive.", "Don’t rely on defaults.",
                      "Does not leave the schema vague.", "Doesn't skip the key.", "Never store secrets in code.",
                      "Avoid a heap for the order table.", "Avoids a heap.", "Avoiding a heap for the base table.",
                      "Must not drop the foreign key.", "Should not use FLOAT for money.", "Not a heap.",
                      "  - do NOT use a heap."):
            self.assertTrue(rubric_prohibition(point), point)

    def test_a_positive_point_passes_even_with_a_negation_inside(self):
        for point in ("Gives every column a data type and, where it has one, a size.",
                      "States for each column whether it is NULL or NOT NULL, the keys first.",
                      "Uses a clustered index on OrderId for the order table, not a heap.",
                      "Notes that a second index stores another copy of the columns it covers.",
                      "Nonclustered index on the report columns, with the extra copy accounted for.",
                      "Noticing the extra copy.", "Doing the lookup by OrderId with an index.", "Avoidance is not enough.", ""):
            self.assertFalse(rubric_prohibition(point), point)


class ItemWriterTests(unittest.TestCase):
    """The whole item-writing path with an author whose first item states a point as a prohibition."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-fair-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.dojo = cls.app.state.dojo
        cls.me = Learner("local:dev", learner_key("local:dev"), "Local learner")
        cls.pid = "gh-300"
        pkg = cls.dojo.packages.get(cls.pid)
        cls.sids = [s for d in pkg["domains"] for g in d["groups"] for s in g["skills"] if pkg["skills"][s]["sources"]]

    def wait(self, job: dict) -> dict:
        for _ in range(900):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def post(self, path: str, body: dict) -> dict:
        r = self.c.post(path, json=body, headers=POST)
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def test_a_prohibition_goes_back_to_the_writer_with_the_reason(self):
        prompts: list[str] = []
        real = self.dojo.ai._item

        def author(user: str) -> dict:
            prompts.append(user)
            item = real(user)
            if len(prompts) == 1:
                item["rubric"][1]["point"] = "Do not leave out the second idea."
            return item
        with mock.patch.object(self.dojo.ai, "_item", author):
            item = self.wait(self.post("/api/items", {"package": self.pid, "skill": self.sids[0], "mode": "check"}))["item"]
        self.assertEqual(len(prompts), 2)
        self.assertIn('Rubric point "Do not leave out the second idea." says what not to do', prompts[1])
        self.assertIn("Write it as what a correct answer states or does.", prompts[1])
        rubric = self.dojo.item(self.me, item)["rubric"]
        self.assertEqual(len(rubric), 2)
        self.assertFalse(any(rubric_prohibition(r["point"]) for r in rubric))

    def test_a_point_marked_met_without_the_learners_words_counts_as_not_met(self):
        """A "met" with nothing quoted from the answer, as when silence seems to meet a prohibition."""
        item = self.wait(self.post("/api/items", {"package": self.pid, "skill": self.sids[1], "mode": "check"}))["item"]

        def grader(user: str) -> dict:
            ids = ai_mod._RUBRIC.findall(user)
            return {"points": [{"id": rid, "met": True, "learner_quote": "", "why": "The answer does not mention it."}
                               for rid in ids], "feedback": "Stub feedback.", "misconception": ""}
        with mock.patch.object(self.dojo.ai, "_grade", grader):
            self.wait(self.post(f"/api/items/{item}/answer", {"text": "Something else entirely, said at length.",
                                                               "elapsed_s": 30}))
        r = self.c.get(f"/api/items/{item}").json()["result"]
        self.assertEqual((r["met"], r["unverified"]), (0, r["total"]))
        self.assertTrue(all(not p["verified"] for p in r["points"]))


if __name__ == "__main__":
    unittest.main()
