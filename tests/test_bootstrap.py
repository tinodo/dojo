"""The one-time bootstrap and the deploy workflow (docs/operations.md): the region you pass is the region
you get, and the deployment identifiers live in repository secrets that are never printed."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SECRETS = ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "DOJO_AUTH_CLIENT_ID", "DOJO_OWNER_OID")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / "infra" / "bootstrap" / "bootstrap.ps1").read_text("utf-8")
        self.template = (ROOT / "infra" / "bootstrap" / "main.bicep").read_text("utf-8")
        self.app = (ROOT / "infra" / "main.bicep").read_text("utf-8")
        self.deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")

    def test_location_reaches_the_template_and_the_app_follows_its_group(self):
        self.assertIn("param location string", self.template)
        self.assertRegex(self.script, r"parameters = @\{[^\n]*\blocation = @\{ value = \$Location \}")
        self.assertIn("param location string = resourceGroup().location", self.app)

    def test_tenant_and_subscription_are_required_with_no_defaults(self):
        params = self.script.split("param(", 1)[1].split("\n)", 1)[0]
        self.assertRegex(params, r"\[Parameter\(Mandatory\)\]\[string\]\$Tenant,")
        self.assertRegex(params, r"\[Parameter\(Mandatory\)\]\[string\]\$Subscription,")
        self.assertEqual(set(GUID.findall(self.script)), {"00000000-0000-0000-0000-000000000000"},
                         "only the default app role id; no tenant, subscription or object id")

    def test_identifiers_are_secrets_and_never_printed(self):
        self.assertNotIn("gh variable set", self.script)
        self.assertIn("| & gh secret set $k --repo $Repo", self.script, "the value goes in on standard input")
        for name in SECRETS:
            self.assertIn(f"{name} ", self.script)
            self.assertIn(f"secrets.{name} }}}}", self.deploy)
        # One repository variable, a switch and not an identifier: team mode (docs/operations.md).
        self.assertEqual(re.findall(r"vars\.(\w+)", self.deploy), ["DOJO_TEAM_MODE"])
        for line in self.script.splitlines():
            if "Write-Host" in line:
                self.assertNotRegex(line, r"\$(Tenant|Subscription|ownerOid|appId|deployClientId|appIdentityClientId)\b", line)
                self.assertNotIn("userPrincipalName", line)

    def test_the_deploy_trust_comes_from_the_repository_not_a_default(self):
        """A default subject would carry one repository's numeric ids into every fork or re-created repository."""
        self.assertRegex(self.template, r"(?m)^param githubSubject string$")
        self.assertNotRegex(self.template, r"repo:[^'\s]*@\d+")
        # GitHub says which subject format the repository uses: immutable ids, or names for older repositories.
        self.assertIn('gh api "repos/$Repo/actions/oidc/customization/sub"', self.script)
        self.assertIn("if (-not $oidc.use_default) { throw", self.script)
        self.assertIn("$subjectPrefix = $oidc.sub_claim_prefix", self.script)
        self.assertIn('if ($oidc.use_immutable_subject) { "repo:$($repoInfo.owner.login)@$($repoInfo.owner.id)/$($repoInfo.name)@$($repoInfo.id)" } else { "repo:$($repoInfo.full_name)" }', self.script)
        self.assertIn('$githubSubject = "$($subjectPrefix):ref:refs/heads/main"', self.script)
        self.assertRegex(self.script, r"parameters = @\{[^\n]*\bgithubSubject = @\{ value = \$githubSubject \}")


if __name__ == "__main__":
    unittest.main()
