"""The App Service plan size (ADR 0031): B1, and B2 or B3 only when Azure has no B1 left for the resource group."""
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
BICEP = (ROOT / "infra" / "main.bicep").read_text("utf-8")
STEP = DEPLOY.split("- name: Deploy infrastructure", 1)[1].split("- name: ", 1)[0]
SCRIPT = textwrap.dedent(STEP.split("run: |\n", 1)[1])

# Stands in for the Azure CLI. A deployment is made with the sizes it is asked for and answers with the next exit code of CREATES; the
# list of its operations says whether the plan is what failed (PLAN_FAILED). Azure's own message goes to stderr, as the real one does.
FAKE_AZ = r"""
n=0; sizes=""; slept=0
az() {
  case "$2 $3" in
    "group create")
      n=$((n+1))
      for a in "$@"; do case "$a" in planSku=*) sizes="$sizes ${a#planSku=}";; esac; done
      echo "No available instances in /subscriptions/abc/resourceGroups/rg-secret-swc" >&2
      return "${CODES[$((n-1))]:-1}" ;;
    "operation group") echo "$PLAN_FAILED"; return 0 ;;
  esac
  return 99
}
sleep() { slept=$((slept+$1)); }
"""


def bash():
    found = shutil.which("bash")
    if not found or any(s in found.lower() for s in ("system32", "windowsapps")):   # WSL's launcher, not a shell
        raise unittest.SkipTest("bash is not installed")
    return found


class TheTemplate(unittest.TestCase):
    def test_the_size_is_one_parameter_with_three_allowed_values(self):
        self.assertRegex(BICEP, r"@allowed\(\[\s*'B1'\s*'B2'\s*'B3'\s*\]\)\s*param planSku string = 'B1'")
        plan = BICEP.split("resource plan 'Microsoft.Web/serverfarms@", 1)[1].split("\n}\n", 1)[0]
        self.assertRegex(plan, r"sku:\s*\{\s*name: planSku\s*\}")
        self.assertNotIn("'B1'", plan)


class TheWorkflow(unittest.TestCase):
    def test_only_the_deployment_asks_for_a_size_and_the_preview_takes_the_default(self):
        self.assertEqual(len([line for line in DEPLOY.splitlines() if "planSku=" in line]), 1)
        self.assertIn('planSku="$size"', STEP)
        self.assertIn("--parameters", [line for line in STEP.splitlines() if "planSku=" in line][0])

    def test_b1_is_tried_three_times_then_b2_then_b3_which_stays_last(self):
        self.assertEqual(re.search(r"for size in ([A-Z0-9 ]+); do", SCRIPT).group(1).split(), ["B1", "B1", "B1", "B2", "B3"])
        self.assertIn('[ "$size" = B3 ]', SCRIPT)

    def test_the_step_ends_with_the_public_summary_and_prints_no_azure_message(self):
        self.assertTrue(SCRIPT.rstrip().splitlines()[-1].startswith('python tools/public_log.py infra "$status" deploy.err'))
        self.assertIn("2>/dev/null", SCRIPT)   # the list of operations is read for a count only
        self.assertNotIn("cat deploy.err", SCRIPT)


class TheLoop(unittest.TestCase):
    """The step's own shell, run with a fake `az` and a `sleep` that only adds up."""

    def run_step(self, codes, plan_failed="1"):
        script = SCRIPT.rstrip().splitlines()
        self.assertTrue(script[-1].startswith("python tools/public_log.py infra"))
        body = "\n".join(script[:-1])
        text = (f'CODES=({" ".join(str(c) for c in codes)}); PLAN_FAILED="{plan_failed}"; GITHUB_RUN_NUMBER=7; RESOURCE_GROUP=rg-x\n'
                + FAKE_AZ + body + '\necho "RESULT status=$status size=$size sizes=$sizes slept=$slept"\n')
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "step.sh").write_text(text, "utf-8", newline="\n")
            out = subprocess.run([bash(), "-e", "step.sh"], cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("rg-secret-swc", out.stdout)   # Azure's message stays in deploy.err
        result = re.search(r"RESULT status=(\d+) size=(\w+) sizes=([\w ]*) slept=(\d+)", out.stdout)
        return int(result.group(1)), result.group(2), result.group(3).split(), int(result.group(4)), out.stdout

    def test_a_deployment_that_works_is_done_at_once_with_b1(self):
        status, size, sizes, slept, out = self.run_step([0])
        self.assertEqual((status, size, sizes, slept), (0, "B1", ["B1"], 0))
        self.assertNotIn("::warning::", out)

    def test_b1_is_asked_again_a_minute_later_and_stays_b1_when_it_comes_back(self):
        status, size, sizes, slept, out = self.run_step([1, 1, 0])
        self.assertEqual((status, size, sizes, slept), (0, "B1", ["B1", "B1", "B1"], 120))
        self.assertNotIn("::warning::", out)

    def test_b2_is_asked_for_when_b1_does_not_come_back_and_the_log_says_so(self):
        status, size, sizes, slept, out = self.run_step([1, 1, 1, 0])
        self.assertEqual((status, size, sizes, slept), (0, "B2", ["B1", "B1", "B1", "B2"], 180))
        self.assertIn("::warning::The App Service plan is B2, not B1", out)

    def test_b3_is_the_last_size_and_then_the_step_fails(self):
        status, size, sizes, slept, out = self.run_step([1, 1, 1, 1, 0])
        self.assertEqual((status, size, sizes), (0, "B3", ["B1", "B1", "B1", "B2", "B3"]))
        self.assertIn("::warning::The App Service plan is B3, not B1", out)
        status, size, sizes, slept, out = self.run_step([1, 1, 1, 1, 1])
        self.assertEqual((status, size, sizes, slept), (1, "B3", ["B1", "B1", "B1", "B2", "B3"], 240))
        self.assertNotIn("::warning::", out)

    def test_a_failure_that_is_not_the_plan_stops_at_once(self):
        for plan_failed in ("0", "", "None"):
            with self.subTest(plan_failed=plan_failed):
                status, size, sizes, slept, out = self.run_step([1, 0], plan_failed)
                self.assertEqual((status, size, sizes, slept), (1, "B1", ["B1"], 0))


if __name__ == "__main__":
    unittest.main()
