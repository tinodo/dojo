"""The one switch for the AI work Dojo does by itself (docs/adr/0029-background-ai-switch.md): which services start,
what the Preparer still does, that the self-test runs once, and that the deployment passes the switch on from its
repository variable. The wiring of the lifespan itself is in tests/test_team2.py (SchedulerWiringTests)."""
import dataclasses
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

from app.core import load_settings  # noqa: E402
from app.learning import Dojo  # noqa: E402
from app.main import background_plan  # noqa: E402
from app.prepare import Preparer  # noqa: E402
from app.selftest import SelfTest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EVERYTHING = {"DOJO_PREPARE": "1", "DOJO_CHECK": "1", "DOJO_DRIFT": "1"}


def settings(**changes):
    return dataclasses.replace(load_settings(), **changes)


class TheSetting(unittest.TestCase):
    def test_only_an_exact_zero_switches_it_off(self):
        for value, on in (("0", False), (" 0 ", False), ("1", True), ("", True)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DOJO_BACKGROUND": value}):
                self.assertEqual(load_settings().background, on)
        with mock.patch.dict(os.environ):
            os.environ.pop("DOJO_BACKGROUND", None)
            self.assertTrue(load_settings().background)


class ThePlan(unittest.TestCase):
    def test_on_starts_what_it_always_started(self):
        azure = background_plan(settings(real_ai=True, in_azure=True, background=True), {})
        self.assertEqual(azure, {"prepare": True, "pool": True, "deeper": True, "check": True, "drift": True,
                                 "selftest": True, "automatic": True})
        stub = background_plan(settings(real_ai=False, in_azure=False, background=True), {})
        self.assertEqual({k for k, v in stub.items() if v}, {"automatic"}, "the stand-in models start nothing unless asked")
        asked = background_plan(settings(real_ai=True, in_azure=False, background=True), EVERYTHING)
        self.assertEqual(asked, azure)
        drift = background_plan(settings(real_ai=False, in_azure=False, background=True), {"DOJO_DRIFT": "1"})
        self.assertEqual({k for k, v in drift.items() if v}, {"drift", "automatic"}, "reading pages needs no model")

    def test_off_keeps_the_films_and_the_page_reading_and_starts_no_model_service(self):
        for env in ({}, EVERYTHING):
            with self.subTest(env=env):
                plan = background_plan(settings(real_ai=True, in_azure=True, background=False), env)
                self.assertEqual(plan, {"prepare": True, "pool": False, "deeper": False, "check": False, "drift": True,
                                        "selftest": True, "automatic": False})

    def test_off_changes_nothing_where_nothing_was_started(self):
        plan = background_plan(settings(real_ai=False, in_azure=False, background=False), {})
        self.assertEqual({k for k, v in plan.items() if v}, set())


class ThePreparer(unittest.TestCase):
    def make(self, automatic: bool) -> Preparer:
        prep = Preparer(mock.Mock(), mock.Mock())
        prep.automatic = automatic
        prep.sourcing = mock.Mock()
        prep.sourcing.step.return_value = False
        prep.sourcing.wait_s.return_value = 120
        return prep

    def test_off_it_files_the_approved_films_and_does_nothing_else(self):
        prep = self.make(False)
        prep.state["running"] = True
        with mock.patch.object(prep, "_learner_busy", return_value=False), \
                mock.patch.object(prep, "_film", return_value=True) as film, \
                mock.patch.object(prep, "_lessons_step") as lessons:
            self.assertTrue(prep.step())
        film.assert_called_once_with()
        lessons.assert_not_called()
        prep.sourcing.step.assert_not_called()
        self.assertFalse(prep.state["running"])

    def test_off_it_still_waits_for_the_owners_own_work(self):
        prep = self.make(False)
        with mock.patch.object(prep, "_learner_busy", return_value=True), mock.patch.object(prep, "_film") as film:
            self.assertTrue(prep.step())
        film.assert_not_called()

    def test_on_it_searches_and_writes_as_before(self):
        prep = self.make(True)
        with mock.patch.object(prep, "_learner_busy", return_value=False), \
                mock.patch.object(prep, "_film") as film, \
                mock.patch.object(prep, "_lessons_step", return_value=False) as lessons:
            prep.step()
        prep.sourcing.step.assert_called_once_with()
        lessons.assert_called_once_with()
        film.assert_not_called()

    def test_off_it_rests_half_an_hour_between_steps(self):
        self.assertEqual(self.make(False).wait_after(False), 1800)
        self.assertEqual(self.make(False).wait_after(True), 15)
        self.assertEqual(self.make(True).wait_after(False), 120, "on, the search for pages says when it is next due")

    def test_the_diag_says_whether_it_works_by_itself(self):
        for automatic in (True, False):
            prep = Preparer(SimpleNamespace(packages=SimpleNamespace(by_id={})), mock.Mock())
            prep.automatic = automatic
            with mock.patch.object(prep, "narration_targets", return_value=[]), mock.patch.object(prep, "rewrites", return_value=[]):
                self.assertIs(prep.status()["automatic"], automatic)


class TheSelfTest(unittest.TestCase):
    class Stop(Exception):
        pass

    def make(self, *passes: bool) -> tuple[SelfTest, mock.Mock]:
        test = SelfTest(SimpleNamespace(build="b"), None, None, None, None, None)
        results = iter(passes)

        def run():
            test.result = {"state": "done", "checks": {"data": next(results)}, "details": {}, "at": "t", "build": "b"}
            return test.result

        return test, mock.patch.object(test, "run", side_effect=run).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_off_it_runs_once_and_stops_after_a_pass(self):
        test, run = self.make(True)
        with mock.patch("app.selftest.time.sleep") as sleep:
            test._loop(False)
        run.assert_called_once_with()
        sleep.assert_not_called()

    def test_off_a_failed_run_is_repeated_until_it_passes(self):
        test, run = self.make(False, False, True)
        with mock.patch("app.selftest.time.sleep") as sleep:
            test._loop(False)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args for c in sleep.call_args_list], [(300,), (300,)])

    def test_on_it_comes_back_every_six_hours(self):
        test, run = self.make(True, True)
        with mock.patch("app.selftest.time.sleep", side_effect=self.Stop) as sleep, self.assertRaises(self.Stop):
            test._loop(True)
        sleep.assert_called_once_with(6 * 3600)
        with mock.patch("app.selftest.time.sleep", side_effect=self.Stop) as sleep, self.assertRaises(self.Stop):
            test._loop()   # on is the default
        sleep.assert_called_once_with(6 * 3600)

    def test_start_hands_the_choice_to_the_loop(self):
        for repeat in (True, False):
            with self.subTest(repeat=repeat):
                test = SelfTest(SimpleNamespace(build="b"), None, None, None, None, None)
                with mock.patch("app.selftest.threading.Thread") as thread:
                    test.start(repeat=repeat)
                self.assertEqual(thread.call_args.kwargs["args"], (repeat,))
                self.assertEqual(thread.call_args.kwargs["target"], test._loop)


class TheLessonRules(unittest.TestCase):
    @staticmethod
    def dojo(background: bool) -> SimpleNamespace:
        writer = mock.Mock()
        writer.to_write_again.return_value = {"lesson-1"}
        dojo = SimpleNamespace(settings=SimpleNamespace(background=background), deeper=writer, deep_held=lambda lesson_id: False,
                               lessons_for=lambda pid, sid: [{"id": "lesson-1"}])
        dojo._written_again = lambda lesson_id: Dojo._written_again(dojo, lesson_id)
        return dojo

    def test_nothing_waits_for_a_rewrite_while_it_is_off(self):
        """Present's first-open deck, Listen's "upgrading" and the films all wait for a lesson to be written again on the deeper template:
        while the switch is off that never happens, so none of them waits."""
        lesson = {"id": "lesson-1", "package": "p", "skill": "s"}
        for background, waits in ((True, True), (False, False)):
            with self.subTest(background=background):
                dojo = self.dojo(background)
                self.assertIs(Dojo._written_again(dojo, "lesson-1"), waits)
                self.assertEqual(Dojo.scheduled_rewrites(dojo), {"lesson-1"} if waits else set())
                self.assertEqual(Dojo.deck_wait(dojo, lesson), "upgrading" if waits else None, "None: a deck may be drawn when it is first opened")


class TheDeploy(unittest.TestCase):
    def setUp(self):
        self.deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        self.bicep = (ROOT / "infra" / "main.bicep").read_text("utf-8")
        self.step = self.deploy.split("- name: Background AI switch", 1)[1].split("- name:", 1)[0]

    def test_the_template_defaults_to_on_and_feeds_the_app_setting(self):
        self.assertRegex(self.bicep, r"param backgroundAi bool = true")
        self.assertRegex(self.bicep, r"name: 'DOJO_BACKGROUND'\s+value: backgroundAi \? '1' : '0'")

    def test_both_runs_of_the_template_get_the_switch_from_its_repository_variable(self):
        self.assertIn("id: background", self.step)
        self.assertIn("SWITCH: ${{ vars.DOJO_BACKGROUND_AI }}", self.step)
        self.assertLess(self.deploy.index("- name: Background AI switch"), self.deploy.index("- name: Preview infrastructure changes"))
        lines = [line for line in self.deploy.splitlines() if "--parameters" in line]
        self.assertEqual(len(lines), 2)   # the preview and the deployment see the same switch
        for line in lines:
            self.assertIn('backgroundAi="$BACKGROUND_AI"', line)
        self.assertEqual(self.deploy.count("BACKGROUND_AI: ${{ steps.background.outputs.on }}"), 2)

    def test_only_an_unset_on_or_true_value_leaves_it_on(self):
        bash = shutil.which("bash")
        if not bash or any(s in bash.lower() for s in ("system32", "windowsapps")):   # WSL's launcher, not a shell
            self.skipTest("bash is not installed")
        script = self.step.split("run: |\n", 1)[1]
        for value, on in ((None, "true"), ("", "true"), ("on", "true"), ("On", "true"), ("true", "true"), ("TRUE", "true"),
                          ("off", "false"), ("Off", "false"), ("OFF", "false"), ("false", "false"), ("0", "false"), ("no", "false"),
                          ("of", "false"), ("on ", "false"), ("on\n", "false"), ("\n", "false"), ("true\r\n", "false")):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "github-output"
                env = {k: v for k, v in os.environ.items() if k != "SWITCH"}
                env.update({"GITHUB_OUTPUT": output.as_posix(), **({} if value is None else {"SWITCH": value})})
                out = subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True, timeout=30)
                written = output.read_text("utf-8") if output.exists() else ""
                self.assertEqual(re.findall(r"(?m)^on=(\w+)$", written), [on], out.stderr)
                self.assertIn(f"background AI: {on}", out.stdout)


if __name__ == "__main__":
    unittest.main()
