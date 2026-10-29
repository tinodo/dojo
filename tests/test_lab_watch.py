"""tools/lab_watch.py: the daily check of the lab database, and what it prints to a public Actions log."""
import importlib.util
import io
import os
import re
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("lab_watch", ROOT / "tools" / "lab_watch.py")
lab_watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab_watch)

GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
SUB = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
GROUP = "rg-secret-swc"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/{GROUP}"
SERVER_ID = f"{SCOPE}/providers/Microsoft.Sql/servers/sql-secret-abcdefghijklm"
DATABASE_ID = f"{SERVER_ID}/databases/sqldb-lab-secret"
ENV = {"AZURE_SUBSCRIPTION_ID": SUB, "RESOURCE_GROUP": GROUP}
NOW = datetime(2026, 10, 30, 5, 37, tzinfo=timezone.utc)
SECRETS = (SUB, GROUP, "sql-secret", "sqldb-lab", "/subscriptions/", "Bearer", "secret-token")


def database(status="Online", sku="Basic", capacity=5, size=2147483648):
    return {"name": "sqldb-lab-secret", "id": DATABASE_ID, "sku": {"name": sku, "tier": sku, "capacity": capacity},
            "properties": {"status": status, "maxSizeBytes": size}}


def points(**series):
    """A metrics answer of 24 hourly points; each keyword is a metric name with the one value every hour has, or a list of 24 values."""
    def metric(name, values):
        hours = values if isinstance(values, list) else [values] * 24
        data = [{"timeStamp": f"2026-10-29T{h:02d}:00:00Z", **hours[h]} for h in range(24)]
        return {"name": {"value": name}, "timeseries": [{"data": data}]}
    return {"value": [metric(name, values) for name, values in series.items()]}


def healthy(**changes):
    base = {"dtu_consumption_percent": {"average": 4.0, "maximum": 18.0}, "storage_percent": {"maximum": 1.2},
            "connection_successful": {"total": 12.5}, "connection_failed": {"total": 0}}
    base.update(changes)
    return points(**base)


class Azure:
    """What Resource Manager says, by URL. Records every URL it was asked for."""
    def __init__(self, db=None, metrics=None, servers=None, databases=None, fail_metrics=None):
        self.db = db or database()
        self.metrics = metrics if metrics is not None else healthy()
        self.servers = servers if servers is not None else [{"name": "sql-secret", "id": SERVER_ID}]
        self.databases = databases if databases is not None else [{"name": "master", "id": SERVER_ID + "/databases/master"}, self.db]
        self.fail_metrics = fail_metrics
        self.asked = []

    def __call__(self, url, tok, tries=3):
        self.asked.append(url)
        if "Microsoft.Insights/metrics" in url:
            if self.fail_metrics:
                raise lab_watch.Unreadable(self.fail_metrics)
            return self.metrics
        if url.split("?")[0].endswith("/databases"):
            return {"value": self.databases}
        return {"value": self.servers}


def run(azure, env=None, bicep=None):
    with mock.patch.object(lab_watch, "token", return_value="secret-token"), mock.patch.object(lab_watch, "get", azure):
        return lab_watch.run(ENV if env is None else env, NOW, bicep or ROOT / "infra" / "main.bicep")


def text(lines):
    return "\n".join(lines)


class LabWatchTests(unittest.TestCase):
    def assertNothingSecret(self, out):
        for s in SECRETS:
            self.assertNotIn(s.lower(), out.lower(), s)
        self.assertIsNone(GUID.search(out))

    def test_the_template_declares_the_tier_the_check_expects(self):
        self.assertEqual(lab_watch.declared_sku((ROOT / "infra" / "main.bicep").read_text("utf-8")), "Basic")
        self.assertIsNone(lab_watch.declared_sku("resource other 'x' = {\n  sku: { name: 'Basic' }\n}\n"))

    def test_a_healthy_database_prints_numbers_and_nothing_else(self):
        lines, status = run(Azure())
        out = text(lines)
        self.assertEqual(status, 0)
        self.assertEqual(lines, ["Lab database, last 24 hours.",
                                 "State: Online. Tier: Basic with 5 DTUs; the template declares Basic.",
                                 "Storage: at most 1.2% of the 2 GB.",
                                 "Load: 4.0% of the DTUs on average, 18.0% at most (24 hourly readings).",
                                 "Connections: 300 succeeded, 0 failed.",
                                 "All well."])
        self.assertNothingSecret(out)

    def test_the_metrics_are_asked_for_the_last_24_hours(self):
        azure = Azure()
        run(azure)
        url = next(u for u in azure.asked if "Microsoft.Insights/metrics" in u)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["metricnames"], ["dtu_consumption_percent,storage_percent,connection_successful,connection_failed"])
        self.assertEqual(query["timespan"], ["2026-10-29T05:37:00Z/2026-10-30T05:37:00Z"])
        self.assertEqual(query["interval"], ["PT1H"])
        self.assertTrue(url.startswith(f"https://management.azure.com{SCOPE.lower()}/") or url.startswith(f"https://management.azure.com{DATABASE_ID}/"))

    def test_each_finding_has_its_own_line_and_its_limit(self):
        cases = [
            ("not online", Azure(db=database(status="Paused")), "The database is Paused, not Online."),
            ("another tier", Azure(db=database(sku="GP_S_Gen5_2", capacity=2)), "The database is on the GP_S_Gen5_2 tier, but the template declares Basic."),
            ("storage over", Azure(metrics=healthy(storage_percent={"maximum": 80.1})), "Storage reached 80.1%, above 80%."),
            ("load over", Azure(metrics=healthy(dtu_consumption_percent={"average": 50.1, "maximum": 99.0})), "The load averaged 50.1%, above 50%."),
            ("failed connections", Azure(metrics=healthy(connection_failed=[{"total": 2.0}] + [{"total": 1.0}] * 23)), "25 connections failed, 25 or more."),
        ]
        for name, azure, said in cases:
            with self.subTest(name):
                lines, status = run(azure)
                self.assertEqual(status, 1)
                self.assertIn("Needs the owner: ", text(lines))
                self.assertIn(said, text(lines))
                self.assertNothingSecret(text(lines))

    def test_the_limits_themselves_are_fine(self):
        for name, metrics in (("storage", healthy(storage_percent={"maximum": 80.0})),
                              ("load", healthy(dtu_consumption_percent={"average": 50.0, "maximum": 50.0})),
                              ("failed", healthy(connection_failed={"total": 1.0}))):   # 24 failures, one short of the limit
            with self.subTest(name):
                lines, status = run(Azure(metrics=metrics))
                self.assertEqual(status, 0, text(lines))

    def test_a_missing_connection_reading_is_zero_not_a_gap(self):
        metrics = healthy()
        metrics["value"] = [m for m in metrics["value"] if not m["name"]["value"].startswith("connection")]
        lines, status = run(Azure(metrics=metrics))
        self.assertEqual(status, 0)
        self.assertIn("Connections: 0 succeeded, 0 failed.", lines)

    def test_no_reading_is_not_a_good_reading(self):
        lines, status = run(Azure(fail_metrics="Azure answered HTTP 403"))
        self.assertEqual(status, 2)
        self.assertIn("Could not judge: The storage, the load and the connections could not be read (Azure answered HTTP 403).", text(lines))
        lines, status = run(Azure(metrics={"value": []}))
        self.assertEqual(status, 2)
        self.assertIn("Azure gave no storage reading.", text(lines))
        self.assertIn("Azure gave no load reading.", text(lines))
        with mock.patch.object(lab_watch, "token", side_effect=lab_watch.Unreadable("there is no Azure token (az exit 1)")):
            lines, status = lab_watch.run(ENV, NOW)
        self.assertEqual((lines, status), (["Could not read the lab database: there is no Azure token (az exit 1)."], 2))
        for env in ({}, {"AZURE_SUBSCRIPTION_ID": "not-a-guid", "RESOURCE_GROUP": GROUP}, {"AZURE_SUBSCRIPTION_ID": SUB, "RESOURCE_GROUP": "rg/../x"}):
            self.assertEqual(lab_watch.run(env, NOW)[1], 2)

    def test_a_tier_that_cannot_be_compared_is_a_gap_not_a_pass(self):
        with mock.patch.object(lab_watch, "declared_sku", return_value=None):
            lines, status = run(Azure())
        self.assertEqual(status, 2)
        self.assertIn("The tier in infra/main.bicep could not be read.", text(lines))

    def test_a_finding_and_a_gap_are_both_said(self):
        lines, status = run(Azure(db=database(status="Paused"), fail_metrics="Azure answered HTTP 400"))
        self.assertEqual(status, 1)
        out = text(lines)
        self.assertIn("Needs the owner: The database is Paused, not Online.", out)
        self.assertIn("Also not read: The storage, the load and the connections could not be read (Azure answered HTTP 400).", out)

    def test_a_database_of_another_tier_does_not_count_its_size_in_dtus(self):
        lines, _ = run(Azure(db=database(sku="GP_S_Gen5", capacity=2)))
        self.assertIn("State: Online. Tier: GP_S_Gen5; the template declares Basic.", lines)

    def test_a_wrong_number_of_servers_or_databases_is_a_finding(self):
        lines, status = run(Azure(servers=[]))
        self.assertEqual((lines, status), (["The lab database was not found: expected one SQL server in the resource group, found 0."], 1))
        lines, status = run(Azure(databases=[{"name": "master", "id": SERVER_ID + "/databases/master"}]))
        self.assertEqual((lines, status), (["The lab database was not found: expected one lab database on the server, found 0."], 1))
        two = [database(), {**database(), "name": "other"}]
        self.assertEqual(run(Azure(databases=two))[1], 1)

    def test_a_server_outside_the_resource_group_is_refused(self):
        other = "/subscriptions/" + SUB + "/resourceGroups/rg-other/providers/Microsoft.Sql/servers/s"
        lines, status = run(Azure(servers=[{"id": other}]))
        self.assertEqual((lines, status), (["Could not read the lab database: Azure named a server outside the resource group."], 2))

    def test_only_a_state_or_a_tier_name_of_azure_is_ever_printed(self):
        hostile = database(status="Online\nsecret-token " + SUB, sku="Basic " + GROUP)
        lines, status = run(Azure(db=hostile))
        out = text(lines)
        self.assertEqual(status, 1)
        self.assertIn("State: unknown. Tier: unknown", out)
        self.assertNothingSecret(out)

    def test_main_prints_the_report_and_returns_its_status(self):
        out = io.StringIO()
        with mock.patch.object(lab_watch, "run", return_value=(["Lab database, last 24 hours.", "All well."], 0)), \
                mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(out):
            self.assertEqual(lab_watch.main(), 0)
        self.assertEqual(out.getvalue(), "Lab database, last 24 hours.\nAll well.\n", "a good run has no annotation, in Actions or outside")

    def test_a_failure_in_actions_is_also_an_annotation(self):
        lines = ["Lab database, last 24 hours.", "Needs the owner: The database is Paused, not Online."]
        out = io.StringIO()
        with mock.patch.object(lab_watch, "run", return_value=(lines, 1)), mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(out):
            self.assertEqual(lab_watch.main(), 1)
        self.assertIn("::error title=Lab database::Needs the owner: The database is Paused, not Online.", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(lab_watch, "run", return_value=(lines, 1)), mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), redirect_stdout(out):
            lab_watch.main()
        self.assertNotIn("::error", out.getvalue())

    def test_a_crash_prints_its_type_and_never_its_values(self):
        out = io.StringIO()
        with mock.patch.object(lab_watch, "run", side_effect=KeyError(GROUP + SUB)), mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), redirect_stdout(out):
            self.assertEqual(lab_watch.main(), 2)
        self.assertEqual(out.getvalue(), "The lab check failed unexpectedly (KeyError).\n")
        out = io.StringIO()
        with mock.patch.object(lab_watch, "run", side_effect=KeyError(GROUP + SUB)), mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(out):
            lab_watch.main()
        self.assertEqual(out.getvalue(), "The lab check failed unexpectedly (KeyError).\n::error title=Lab database::The lab check failed unexpectedly (KeyError).\n")
        self.assertNothingSecret(out.getvalue())


class GetTests(unittest.TestCase):
    def call(self, error, tries=3):
        with mock.patch.object(lab_watch.urllib.request, "urlopen", side_effect=error), mock.patch.object(lab_watch.time, "sleep") as sleep:
            try:
                lab_watch.get(f"https://management.azure.com{SCOPE}/x", "secret-token", tries)
            except lab_watch.Unreadable as e:
                return str(e), sleep.call_count
        self.fail("no Unreadable")

    def test_only_a_status_leaves_get_never_the_address_or_the_message(self):
        error = urllib.error.HTTPError(f"https://management.azure.com{SCOPE}/x", 404, "Not found " + SUB, {}, io.BytesIO(b'{"error":"' + GROUP.encode() + b'"}'))
        said, sleeps = self.call(error)
        self.assertEqual((said, sleeps), ("Azure answered HTTP 404", 0))
        said, _ = self.call(urllib.error.URLError("failed for " + SCOPE))
        self.assertEqual(said, "Azure did not answer")
        said, _ = self.call(TimeoutError(SUB))
        self.assertEqual(said, "Azure did not answer")

    def test_a_throttle_or_a_hiccup_is_tried_again_and_then_given_up(self):
        error = urllib.error.HTTPError("https://x", 429, "slow down", {}, io.BytesIO(b""))
        self.assertEqual(self.call(error), ("Azure answered HTTP 429", 2))

    def test_an_answer_that_is_not_json_is_unreadable(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>" + SUB.encode()
        with mock.patch.object(lab_watch.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(lab_watch.Unreadable) as raised:
                lab_watch.get("https://x", "secret-token")
        self.assertEqual(str(raised.exception), "Azure's answer could not be read")

    def test_the_numbers_of_a_series_are_what_the_points_say(self):
        n = lab_watch.numbers(points(dtu_consumption_percent={"average": 10.0, "maximum": 30.0}, storage_percent={"maximum": 2.5},
                                     connection_successful={"total": 2}, connection_failed={"total": 0}))
        self.assertEqual((n["load_average"], n["load_peak"], n["hours"], n["storage_peak"], n["ok"], n["failed"]), (10.0, 30.0, 24, 2.5, 48, 0))
        empty = lab_watch.numbers({"value": [{"name": {"value": "storage_percent"}, "timeseries": [{"data": [{"timeStamp": "x", "maximum": None}]}]}]})
        self.assertEqual((empty["storage_peak"], empty["load_average"], empty["ok"], empty["failed"]), (None, None, None, None))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.watch = (ROOT / ".github" / "workflows" / "lab-watch.yml").read_text("utf-8")
        self.deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")

    def test_it_runs_every_day_and_on_request_and_never_from_a_pull_request(self):
        self.assertRegex(self.watch, r'cron: "\d+ \d+ \* \* \*"')
        self.assertIn("workflow_dispatch:", self.watch)
        for trigger in ("pull_request", "push:"):
            self.assertNotIn(trigger, self.watch)

    def test_it_only_reads_and_signs_in_the_way_the_deploy_does(self):
        self.assertIn("permissions:\n  contents: read\n  id-token: write", self.watch)
        for action in ("actions/checkout", "azure/login"):
            ours = re.search(re.escape(action) + r"@([0-9a-f]{40}) # v[\d.]+", self.watch)
            theirs = re.search(re.escape(action) + r"@([0-9a-f]{40}) # v[\d.]+", self.deploy)
            self.assertTrue(ours and theirs)
            self.assertEqual(ours.group(0), theirs.group(0), f"{action} is pinned to the same commit as in deploy.yml")
        for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"):
            self.assertIn("${{ secrets." + name + " }}", self.watch)
        self.assertIn("run: python3 tools/lab_watch.py", self.watch)
        self.assertEqual(re.findall(r"RESOURCE_GROUP: (\S+)", self.watch), re.findall(r"RESOURCE_GROUP: (\S+)", self.deploy)[:1])

    def test_a_change_to_the_check_alone_does_not_restart_the_app(self):
        ignored = self.deploy.split("paths-ignore:", 1)[1].split("workflow_dispatch:", 1)[0]
        for path in (".github/workflows/lab-watch.yml", "tools/lab_watch.py", "tests/test_lab_watch.py"):
            self.assertIn(f'"{path}"', ignored)


if __name__ == "__main__":
    unittest.main()
