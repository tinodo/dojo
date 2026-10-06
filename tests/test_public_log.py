"""tools/public_log.py: what a deploy step prints to a public Actions log."""
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("public_log", ROOT / "tools" / "public_log.py")
public_log = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(public_log)

GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
SUB = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
PRINCIPAL = "9A8B7C6D-5E4F-4A3B-8C2D-1E0F9A8B7C6D"

ARM_ERROR = (
    'ERROR: {"status":"Failed","error":{"code":"DeploymentFailed","target":"/subscriptions/' + SUB
    + '/resourceGroups/rg-secret-swc/providers/Microsoft.Resources/deployments/dojo-7","message":"At least one'
    ' resource deployment operation failed.","details":[{"code": "RequestDisallowedByPolicy","target":'
    '"sql-secret-abcdefghijklm","message":"Resource \'sql-secret-abcdefghijklm\' was disallowed by policy.'
    ' Policy identifiers: \'[{\\"policyAssignment\\":{\\"name\\":\\"Deny-Secret-Things\\"}}]\'."},'
    '{"code":"Conflict","message":"Principal ' + PRINCIPAL + ' in tenant tenant-secret.example."}]}}\n'
)
CORE_ERROR = (
    "ERROR: (AuthorizationFailed) The client '" + PRINCIPAL + "' with object id '" + PRINCIPAL + "' does not have"
    " authorization to perform action 'Microsoft.Resources/deployments/validate/action' over scope"
    " '/subscriptions/" + SUB + "/resourcegroups/rg-secret-swc'.\n"
    "Code: AuthorizationFailed\n"
    "Message: The client '" + PRINCIPAL + "' with object id '" + PRINCIPAL + "' does not have authorization.\n"
)
BICEP_ERROR = ("ERROR: /home/runner/work/x/x/infra/main.bicep(12,3) : Error BCP035: The specified \"object\""
               " declaration is missing the following required properties: \"name\".\n")
SECRETS = (SUB, PRINCIPAL, "rg-secret-swc", "sql-secret", "Deny-Secret", "tenant-secret", "/subscriptions/",
           "was disallowed", "does not have", "/home/runner")


class PublicLogTests(unittest.TestCase):
    def run_main(self, *argv, files=None):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (files or {}).items():
                (Path(tmp) / name).write_text(text, "utf-8")
            names = [str(Path(tmp) / a) if a in (files or {}) else a for a in argv]
            out = io.StringIO()
            with redirect_stdout(out):
                code = public_log.main(names)
            return code, out.getvalue()

    def assertNothingSecret(self, text):
        for s in SECRETS:
            self.assertNotIn(s.lower(), text.lower(), s)
        self.assertIsNone(GUID.search(text))

    def test_a_failed_deployment_prints_only_its_error_codes(self):
        with mock.patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "7"}):
            code, out = self.run_main("infra", "1", "deploy.err", files={"deploy.err": ARM_ERROR})
        self.assertEqual(code, 1)
        self.assertIn("Error codes: DeploymentFailed, RequestDisallowedByPolicy, Conflict.", out)
        self.assertIn("dojo-7", out)
        self.assertNothingSecret(out)

    def test_azure_core_errors_and_bicep_diagnostics_give_their_codes(self):
        code, out = self.run_main("preview", "3", "whatif.err", "whatif.json",
                                  files={"whatif.err": CORE_ERROR + BICEP_ERROR, "whatif.json": ""})
        self.assertEqual(code, 3)
        self.assertIn("Error codes: AuthorizationFailed, BCP035.", out)
        self.assertNothingSecret(out)

    def test_an_error_without_a_code_prints_no_message(self):
        code, out = self.run_main("app", "1", "webapp.err",
                                  files={"webapp.err": "ERROR: Deployment to 'sql-secret-x' failed for " + SUB + "\n"})
        self.assertEqual(code, 1)
        self.assertIn("Error codes: none found.", out)
        self.assertNothingSecret(out)

    def test_a_missing_stderr_file_still_fails_the_step(self):
        code, out = self.run_main("infra", "2", "nowhere.err")
        self.assertEqual(code, 2)
        self.assertIn("Error codes: none found.", out)

    def test_a_guid_or_a_resource_name_never_counts_as_a_code(self):
        text = '"code": "' + SUB + '" "code":"sql-secret-abc" (rg-secret-swc) x\nCode: ' + PRINCIPAL + "\n"
        self.assertEqual(public_log.error_codes(text), [])

    def test_a_good_preview_prints_counts_and_hides_warnings(self):
        changes = {"changes": [{"changeType": "Modify", "resourceId": "/subscriptions/" + SUB + "/x"},
                               {"changeType": "NoChange", "resourceId": "/subscriptions/" + SUB + "/y"},
                               {"changeType": "NoChange", "resourceId": "/subscriptions/" + SUB + "/z"}]}
        warnings = ("WARNING: /home/runner/work/x/x/infra/main.bicep(58,3) : Warning use-stable-resource-identifiers:"
                    " sql-secret-abc\n\nWARNING: something about " + SUB + "\n")
        code, out = self.run_main("preview", "0", "whatif.err", "whatif.json",
                                  files={"whatif.err": warnings, "whatif.json": json.dumps(changes)})
        self.assertEqual(code, 0)
        self.assertIn("What-if: Modify 1, NoChange 2", out)
        self.assertIn("2 line(s) of warnings, not printed", out)
        self.assertNothingSecret(out)

    def test_a_good_step_without_warnings_prints_nothing(self):
        self.assertEqual(self.run_main("infra", "0", "deploy.err", files={"deploy.err": ""}), (0, "\n"))
        code, out = self.run_main("preview", "0", "whatif.err", "whatif.json",
                                  files={"whatif.err": "", "whatif.json": '{"changes": []}'})
        self.assertEqual((code, out.strip()), (0, "What-if: no changes"))

    def test_an_unreadable_preview_fails_without_printing_it(self):
        code, out = self.run_main("preview", "0", "whatif.err", "whatif.json",
                                  files={"whatif.err": "", "whatif.json": "not json " + SUB})
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "What-if: the result could not be read.")
        self.assertEqual(self.run_main("preview", "0", "whatif.err", "missing.json", files={"whatif.err": ""})[0], 1)

    def test_bad_arguments_are_refused(self):
        self.assertEqual(self.run_main("deploy", "0", "x.err")[0], 2)
        self.assertEqual(self.run_main("infra", "-1", "x.err")[0], 2)

    def test_the_deployment_name_matches_the_workflow(self):
        wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        self.assertIn('--name "dojo-${GITHUB_RUN_NUMBER}"', wf)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "42"}):
            self.assertEqual(public_log.deployment_name(), "dojo-42")


if __name__ == "__main__":
    unittest.main()
