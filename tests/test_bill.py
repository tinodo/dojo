"""The Money tab's reading of Azure's cost (app/bill.py, docs/adr/0025-studio-v2.md). Nothing here touches the network: Azure's answers
are made up here, in the shape Cost Management gives them."""
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.bill as bill_mod  # noqa: E402
from app.bill import Bill, classify, summarize  # noqa: E402
from app.core import Store, iso, utcnow  # noqa: E402

SUB = "6d47a6cb-a818-49b8-af51-5f508aa5786c"
RG = "rg-dojo-swc"
RESOURCE = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.CognitiveServices/accounts/dojo-tinodo-ai"
COLUMNS = [{"name": n, "type": t} for n, t in (("Cost", "Number"), ("UsageDate", "Number"), ("ServiceName", "String"), ("Meter", "String"), ("Currency", "String"))]
DAYS = [15.2, 12.8, 11.7, 11.7, 12.3, 20.7, 6.9, 2.0, 2.0, 2.6, 2.0, 2.0, 2.0, 56.6, 87.0, 62.7, 13.6, 7.6, 6.5]


def line(day: int, service: str, meter: str, cost: float, month: str = "202610", currency: str = "USD") -> list:
    return [cost, int(f"{month}{day:02d}"), service, meter, currency]


def october() -> list[list]:
    """Nineteen days of an Azure resource group, in the owner's real proportions: AI models and voices follow what is built, the web
    app, the private endpoint and the Defender servers cost the same every day."""
    rows = []
    for d, total in enumerate(DAYS, start=1):
        always = 0.42 + 0.23 + 0.64
        use = max(0.0, total - always)
        rows += [line(d, "Foundry Models", "5.5 ShortCo opt Gl 1M Tokens", round(use * 0.45, 4)),
                 line(d, "Foundry Models", "Grok 4.1 Inp Glbl Tokens", round(use * 0.03, 4)),
                 line(d, "Foundry Tools", "TTS Standard Avatar Batch Speech", round(use * 0.27, 4)),
                 line(d, "Foundry Tools", "S1 Neural Text To Speech Characters", round(use * 0.14, 4)),
                 line(d, "Microsoft Defender for Cloud", "Standard Tokens", round(use * 0.11, 4)),
                 line(d, "Microsoft Defender for Cloud", "Standard Node", 0.64),
                 line(d, "Azure App Service", "B1", 0.42),
                 line(d, "Virtual Network", "Standard Private Endpoint", 0.23),
                 line(d, "SQL Database", "1 vCore - Free", 0.0),
                 line(d, "SQL Database", "General Purpose Data Stored - Free", 0.0)]
    return rows


class Tokens:
    def __init__(self):
        self.scopes = []

    def get(self, scope: str) -> str:
        self.scopes.append(scope)
        return "a-token"


class Azure:
    """A made-up Cost Management: what it answers, and every request it got."""

    def __init__(self, *, month=None, last=None, status=200, headers=None, pages=None, last_status=200):
        self.month, self.last = october() if month is None else month, [[1234.56, "USD"]] if last is None else last
        self.status, self.headers, self.pages, self.last_status = status, headers or {}, pages, last_status
        self.requests: list[httpx.Request] = []
        self.gate: threading.Event | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gate is not None:
            self.gate.wait(10)
        if self.status != 200:
            return httpx.Response(self.status, headers=self.headers, json={"error": {"code": "Nope", "message": f"/subscriptions/{SUB} is not allowed"}})
        body = json.loads(request.content)
        if body["timeframe"] == "TheLastMonth":
            if self.last_status != 200:
                return httpx.Response(self.last_status, json={"error": {"code": "Nope"}})
            return httpx.Response(200, json={"properties": {"columns": [{"name": "Cost"}, {"name": "Currency"}], "rows": self.last, "nextLink": None}})
        if self.pages is not None:
            return self.pages(request)
        return httpx.Response(200, json={"properties": {"columns": COLUMNS, "rows": self.month, "nextLink": None}})


class BillCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-bill-")
        self.store = Store(Path(self.tmp))
        self.tokens = Tokens()
        self.now = utcnow().replace(year=2026, month=10, day=19, hour=16, minute=0, second=0, microsecond=0)
        clock = mock.patch.object(bill_mod, "utcnow", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def bill(self, azure: Azure, tokens=True) -> Bill:
        self.azure = azure
        return Bill(self.store, self.tokens if tokens else None, RESOURCE, httpx.Client(transport=httpx.MockTransport(azure)))


class ReadingTests(unittest.TestCase):
    def test_each_line_of_the_bill_has_a_group_a_part_and_a_kind(self):
        self.assertEqual(classify("Foundry Models", "5.5 ShortCo opt Gl 1M Tokens"), ("ai", "gpt-5.5", "use"))
        self.assertEqual(classify("Foundry Models", "V4 Pro Inp glbl Tokens"), ("ai", "DeepSeek", "use"))
        self.assertEqual(classify("Foundry Models", "Something New"), ("ai", "Other models", "use"))
        self.assertEqual(classify("Foundry Tools", "TTS Standard Avatar Batch Speech"), ("voices", "Presenter videos", "use"))
        self.assertEqual(classify("Foundry Tools", "S1 Neural Text To Speech Characters"), ("voices", "Narration voices", "use"))
        self.assertEqual(classify("Foundry Tools", "Fast Transcription Speech To Text"), ("voices", "Checking what was said", "use"))
        self.assertEqual(classify("Microsoft Defender for Cloud", "Standard Tokens")[0::2], ("defender", "use"))
        self.assertEqual(classify("Microsoft Defender for Cloud", "Standard Node")[0::2], ("defender", "always"))
        self.assertEqual(classify("Azure App Service", "B1"), ("web", None, "always"))
        self.assertEqual(classify("Virtual Network", "Standard Private Endpoint"), ("network", None, "always"))
        self.assertEqual(classify("MS Bing Services", "Grounding"), ("search", None, "use"))
        self.assertEqual(classify("SQL Database", "B DTU"), ("database", "Basic tier", "always"), "Basic, charged per day")
        self.assertEqual(classify("SQL Database", "B DTUs"), ("database", "Basic tier", "always"))
        for meter in ("1 vCore - Free", "General Purpose Data Stored - Free", "vCore", "General Purpose Data Stored"):
            self.assertEqual(classify("SQL Database", meter), ("database", "Before the move to Basic", "use"), meter)
        self.assertEqual(classify("SQL Database", "Backup LRS Data Stored"), ("database", "Other database charges", "unknown"),
                         "a line of the database that no rule names is not called pay per use, and not always on")
        self.assertEqual(classify("SQL Database", "Free DTU"), ("database", "Other database charges", "unknown"))
        self.assertEqual(classify("Bandwidth", "Data Transfer Out"), ("other", None, "use"), "a line nobody named still counts in the total")

    def rows(self) -> list[dict]:
        return [{"day": f"2026-10-{r[1] % 100:02d}", "service": r[2], "meter": r[3], "cost": r[0], "currency": r[4]} for r in october()]

    def test_the_groups_always_add_up_to_the_total_and_say_what_is_always_on(self):
        s = summarize(self.rows(), {"month": "2026-09", "total": 120.0, "currency": "USD"}, "https://portal/x")
        self.assertEqual(s["state"], "ok")
        self.assertEqual((s["month"]["from"], s["month"]["to"], s["month"]["days"]), ("2026-10-01", "2026-10-19", 19))
        self.assertAlmostEqual(sum(g["total"] for g in s["groups"]), s["month"]["total"], delta=0.05)
        self.assertAlmostEqual(s["month"]["total"], sum(DAYS), delta=0.1)
        self.assertEqual(s["groups"][-1]["key"], "database", "a group that costs nothing is last")
        self.assertEqual(s["groups"][0]["key"], "ai", "the biggest group first")
        kinds = {g["key"]: g["kind"] for g in s["groups"]}
        self.assertEqual((kinds["ai"], kinds["voices"], kinds["web"], kinds["network"], kinds["database"]), ("use", "use", "always", "always", "free"))
        self.assertEqual(kinds["defender"], "mixed", "its servers are always on, its AI scanning follows use")
        defender = next(g for g in s["groups"] if g["key"] == "defender")
        self.assertEqual({p["label"]: p["kind"] for p in defender["parts"]}, {"Billed by how much text goes to the AI": "use", "Billed per server": "always"})
        self.assertEqual(s["fixed"]["per_day"], 1.29)
        self.assertEqual(s["fixed"]["total"], round(1.29 * 19, 2))
        self.assertEqual(s["fixed"]["per_month"], round(1.29 * 31, 2))

    def test_the_last_day_is_marked_as_still_arriving_and_the_quietest_and_busiest_days_are_complete_ones(self):
        s = summarize(self.rows())
        self.assertEqual([d["day"] for d in s["days"] if d["partial"]], ["2026-10-19"])
        self.assertEqual(len(s["days"]), 19)
        self.assertEqual(s["quiet_day"]["day"], "2026-10-08", "the lowest complete day, not a day that was only part reported")
        self.assertAlmostEqual(s["quiet_day"]["total"], 2.0, delta=0.05)
        self.assertEqual([d["day"] for d in s["busiest"]], ["2026-10-15", "2026-10-16", "2026-10-14"])

    def test_a_month_with_few_complete_days_has_no_quietest_day(self):
        s = summarize([r for r in self.rows() if r["day"] <= "2026-10-04"])
        self.assertIsNone(s["quiet_day"])
        self.assertEqual(len(s["days"]), 4)

    def test_a_day_without_a_line_is_a_zero_day_in_the_series(self):
        rows = [r for r in self.rows() if r["day"] != "2026-10-05"]
        s = summarize(rows)
        self.assertEqual(next(d for d in s["days"] if d["day"] == "2026-10-05")["total"], 0.0)

    def test_private_dns_zones_are_always_on_and_dns_queries_are_not(self):
        self.assertEqual(classify("Azure DNS", "Private DNS Zone"), ("network", "Private DNS zone", "always"))
        self.assertEqual(classify("Azure DNS", "Private DNS Queries"), ("other", None, "use"))

    def test_the_least_a_month_can_cost_is_what_is_spent_and_what_is_always_on_for_the_days_to_come(self):
        s = summarize(self.rows())
        self.assertEqual(s["month_floor"], round(s["month"]["total"] + 12 * s["fixed"]["per_day"], 2), "19 of 31 days are over")

    def test_a_credit_is_part_of_the_total_and_more_than_one_currency_gives_no_total(self):
        rows = self.rows() + [{"day": "2026-10-10", "service": "Foundry Models", "meter": "5.5 ShortCo opt Gl 1M Tokens", "cost": -5.0, "currency": "USD"}]
        base = summarize(self.rows())["month"]["total"]
        self.assertAlmostEqual(summarize(rows)["month"]["total"], base - 5.0, delta=0.01)
        mixed = rows + [{"day": "2026-10-10", "service": "x", "meter": "y", "cost": 1.0, "currency": "EUR"}]
        self.assertEqual(summarize(mixed), {"state": "error", "reason": bill_mod.MIXED})


def sql(day: int, meter: str, cost: float, month: str = "2026-10") -> dict:
    return {"day": f"{month}-{day:02d}", "service": "SQL Database", "meter": meter, "cost": cost, "currency": "USD"}


def web(days, month: str = "2026-10", cost: float = 0.42) -> list[dict]:
    return [{"day": f"{month}-{d:02d}", "service": "Azure App Service", "meter": "B1", "cost": cost, "currency": "USD"} for d in days]


class DatabaseRowTests(unittest.TestCase):
    """What the Money tab says about the lab database after the move to the Basic tier (ADR 0028): a fixed price a day. The meter names are
    the ones Azure uses: "1 vCore - Free" and "General Purpose Data Stored - Free" on the free offer, "vCore" on paid serverless, "B DTU" on Basic."""

    @staticmethod
    def database(rows):
        s = summarize(rows)
        return s, next(g for g in s["groups"] if g["key"] == "database")

    def test_the_free_offer_lines_cost_nothing_and_are_not_listed(self):
        rows = web(range(1, 6)) + [sql(d, "1 vCore - Free", 0.0) for d in range(1, 6)] + [sql(d, "General Purpose Data Stored - Free", 0.0) for d in range(1, 6)]
        s, g = self.database(rows)
        self.assertEqual((g["kind"], g["parts"], g["total"], g["per_month"]), ("free", [], 0.0, None))
        self.assertEqual(g["note"], "The lab database. It has a fixed daily price (the Basic tier), charged whether or not anyone uses labs.")
        for old in ("free offer", "pauses"):
            self.assertNotIn(old, g["note"])

    def test_the_first_days_after_the_move_have_no_monthly_figure_anywhere(self):
        """Paid serverless until the 30th, a half day of Basic on the 30th and one full day on the 31st (the day still arriving)."""
        rows = (web(range(1, 32)) + [sql(d, "1 vCore - Free", 0.0) for d in range(1, 28)]
                + [sql(28, "vCore", 0.40), sql(29, "vCore", 5.10), sql(30, "vCore", 2.00), sql(30, "General Purpose Data Stored", 0.01),
                   sql(30, "B DTU", 0.08), sql(31, "B DTU", 0.161)])
        s, g = self.database(rows)
        self.assertEqual(g["kind"], "mixed")
        self.assertEqual([(p["label"], p["kind"], p["total"]) for p in g["parts"]],
                         [("Before the move to Basic", "use", 7.51), ("Basic tier", "always", 0.24)])
        self.assertEqual([p["per_month"] for p in g["parts"]], [None, None], "Basic has no complete day yet: nothing is guessed")
        self.assertIsNone(g["per_month"])
        self.assertEqual(s["fixed"]["total"], round(0.42 * 31 + 0.241, 2))
        self.assertIsNone(s["fixed"]["per_day"])
        self.assertIsNone(s["fixed"]["per_month"], "one always-on line has no rate yet: the figure for all of them is not said")
        self.assertIsNone(s["month_floor"])
        self.assertAlmostEqual(s["use_total"] + s["fixed"]["total"] + s["unknown_total"], s["month"]["total"], delta=0.02)

    def test_a_week_into_the_next_month_basic_has_its_monthly_price(self):
        s, g = self.database(web(range(1, 7), "2026-11") + [sql(d, "B DTU", 0.161, "2026-11") for d in range(1, 7)])
        self.assertEqual((g["kind"], g["total"], g["per_month"]), ("always", 0.97, 4.83), "30 days at USD 0.161")
        self.assertEqual([(p["label"], p["kind"], p["per_month"]) for p in g["parts"]], [("Basic tier", "always", 4.83)])
        self.assertEqual(s["fixed"]["per_day"], 0.58)
        self.assertEqual(s["fixed"]["per_month"], round((0.42 + 0.161) * 30, 2))
        self.assertEqual(s["month_floor"], round(s["month"]["total"] + 24 * (0.42 + 0.161), 2))

    def test_the_first_two_days_of_a_month_say_no_monthly_figure_and_the_third_does(self):
        def rows(through):
            return web(range(1, through + 1), "2026-11") + [sql(d, "B DTU", 0.161, "2026-11") for d in range(1, through + 1)]
        for through in (1, 2):
            s, g = self.database(rows(through))
            self.assertEqual((s["fixed"]["per_month"], s["month_floor"], g["per_month"]), (None, None, None), through)
        s, g = self.database(rows(3))
        self.assertEqual((s["fixed"]["per_month"], g["per_month"]), (round(0.581 * 30, 2), 4.83))

    def test_zero_cost_parts_decide_nothing_and_are_not_listed(self):
        rows = [sql(d, "1 vCore - Free", 0.0, "2026-11") for d in range(1, 6)] + [sql(d, "B DTU", 0.161, "2026-11") for d in range(1, 6)]
        _, g = self.database(rows)
        self.assertEqual(g["kind"], "always", "a part that costs nothing does not make the group partly something else")
        self.assertEqual([p["label"] for p in g["parts"]], ["Basic tier"])

    def test_a_charge_that_no_rule_names_is_not_called_use_or_always_and_has_no_group_price(self):
        rows = [sql(d, "B DTU", 0.161, "2026-11") for d in range(1, 7)] + [sql(d, "Backup LRS Data Stored", 0.05, "2026-11") for d in range(1, 7)]
        s, g = self.database(rows)
        self.assertEqual(g["kind"], "always", "what is identified decides the chip")
        self.assertEqual({p["label"]: (p["kind"], p["per_month"]) for p in g["parts"]}, {"Basic tier": ("always", 4.83), "Other database charges": ("unknown", None)})
        self.assertIsNone(g["per_month"], "no price for the whole group while a charge in it is not identified")
        self.assertEqual((s["unknown_total"], s["use_total"], s["fixed"]["total"]), (0.30, 0.0, 0.97))
        self.assertAlmostEqual(s["use_total"] + s["fixed"]["total"] + s["unknown_total"], s["month"]["total"], delta=0.02)
        _, only = self.database([sql(d, "Backup LRS Data Stored", 0.05, "2026-11") for d in range(1, 4)])
        self.assertEqual(only["kind"], "unknown")
        _, some_use = self.database([sql(1, "vCore", 1.0, "2026-11"), sql(2, "Backup LRS Data Stored", 0.3, "2026-11")])
        self.assertEqual(some_use["kind"], "use", "an unidentified charge never turns a group into 'partly always on'")

    def test_a_meter_that_is_new_under_an_old_label_is_waited_for_not_read_from_the_old_meters_days(self):
        """Two Defender meters are both "Billed per server": the second begins on the 5th and must not borrow the first one's history."""
        def defender(meter, days, cost):
            return [{"day": f"2026-11-{d:02d}", "service": "Microsoft Defender for Cloud", "meter": meter, "cost": cost, "currency": "USD"} for d in days]
        old = defender("Standard Node", range(1, 8), 0.64)
        s = summarize(old + defender("SqlServers Standard", range(5, 8), 0.50))
        group = next(g for g in s["groups"] if g["key"] == "defender")
        self.assertEqual(group["parts"][0]["label"], "Billed per server")
        self.assertIsNone(group["parts"][0]["per_month"], "the new meter has one complete day after its first one: not two yet")
        self.assertIsNone(s["fixed"]["per_month"])
        s = summarize(defender("Standard Node", range(1, 9), 0.64) + defender("SqlServers Standard", range(5, 9), 0.50))
        self.assertEqual(next(g for g in s["groups"] if g["key"] == "defender")["parts"][0]["per_month"], round((0.64 + 0.50) * 30, 2),
                         "each meter has its own rate; the part's is their sum")

    def test_what_an_always_on_line_costs_a_day_comes_from_its_complete_days(self):
        rate = bill_mod._daily_rate
        self.assertIsNone(rate({"2026-10-29": 0.08, "2026-10-30": 0.161, "2026-10-31": 0.161}, "2026-10-31", "2026-10-01"), "it began this month: its first day is left out")
        self.assertEqual(rate({"2026-11-03": 0.08, "2026-11-04": 0.161, "2026-11-05": 0.161, "2026-11-06": 0.161}, "2026-11-06", "2026-11-01"), 0.161)
        self.assertEqual(rate({"2026-11-01": 0.5, "2026-11-02": 0.5, "2026-11-03": 0.5}, "2026-11-03", "2026-11-01"), 0.5, "it ran from the 1st: no day is left out")
        self.assertEqual(rate({"2026-11-01": 1.0, "2026-11-02": 3.0, "2026-11-03": 2.0, "2026-11-04": 9.0}, "2026-11-04", "2026-11-01"), 2.0)
        self.assertEqual(rate({"2026-11-01": 1.0, "2026-11-02": 3.0, "2026-11-03": 2.0, "2026-11-04": 9.0, "2026-11-05": 9.0}, "2026-11-05", "2026-11-01"), 3.0,
                         "with an even number of days the upper middle one: a part day does not pull the price down")
        self.assertIsNone(rate({"2026-11-01": 0.0, "2026-11-02": 0.0}, "2026-11-02", "2026-11-01"))


class BillTests(BillCase):
    def test_a_first_visit_reads_azure_once_and_keeps_the_reading(self):
        bill = self.bill(Azure())
        v = bill.view()
        self.assertEqual(v["state"], "ok", v)
        self.assertEqual((v["month"]["to"], v["currency"], v["previous"]), ("2026-10-19", "USD", {"month": "2026-09", "total": 1234.56, "currency": "USD"}))
        self.assertFalse(v["stale"])
        self.assertEqual(self.tokens.scopes, ["https://management.azure.com/.default"], "one token for both questions")
        first, second = self.azure.requests
        self.assertEqual(first.url.path, f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.CostManagement/query")
        self.assertEqual(first.url.params["api-version"], "2023-11-01")
        self.assertEqual(first.headers["authorization"], "Bearer a-token")
        body = json.loads(first.content)
        self.assertEqual((body["type"], body["timeframe"], body["dataset"]["granularity"]), ("ActualCost", "MonthToDate", "Daily"))
        self.assertEqual([g["name"] for g in body["dataset"]["grouping"]], ["ServiceName", "Meter"])
        self.assertEqual(json.loads(second.content)["timeframe"], "TheLastMonth")
        kept = self.store.read("content", "bill")
        self.assertEqual((kept["month"], kept["attempt"]["state"]), ("2026-10", "ok"))
        before = len(self.azure.requests)
        self.assertEqual(bill.view()["state"], "ok")
        self.assertEqual(Bill(self.store, self.tokens, RESOURCE, httpx.Client(transport=httpx.MockTransport(self.azure))).view()["state"], "ok", "after a restart too")
        self.assertEqual(len(self.azure.requests), before, "six hours on the share: nobody asks Azure again")

    def test_a_reading_of_an_old_shape_is_never_shown_but_what_azure_said_about_waiting_is_kept(self):
        """A reading made by the rules before the move to Basic still says the database is free: it is not shown, it is read again."""
        scope = f"/subscriptions/{SUB}/resourceGroups/{RG}"
        old = {"shape": bill_mod.SHAPE - 1, "scope": scope, "read_at": iso(self.now), "month": "2026-10",
               "summary": {"state": "ok", "currency": "USD", "groups": [{"key": "database", "note": "The lab database. On the free offer it costs nothing and pauses when nobody uses labs."}]},
               "attempt": {"at": iso(self.now), "state": "ok", "retry_at": None, "reason": None}}
        self.store.write("content", "bill", value=old)
        azure = Azure()
        v = self.bill(azure).view()
        self.assertEqual(v["state"], "ok")
        self.assertNotIn("free offer", json.dumps(v))
        self.assertEqual(len(azure.requests), 2, "this month and last month, read again")
        self.assertEqual(self.store.read("content", "bill")["shape"], bill_mod.SHAPE)
        waiting = {**old, "attempt": {"at": iso(self.now), "state": "throttled", "retry_at": iso(self.now + timedelta(seconds=300)), "reason": bill_mod.WAITING}}
        self.store.write("content", "bill", value=waiting)
        azure = Azure()
        bill = self.bill(azure)
        v = bill.view()
        self.assertEqual((v["state"], v["reason"]), ("error", bill_mod.WAITING))
        self.assertNotIn("free offer", json.dumps(v))
        self.assertEqual(azure.requests, [], "Azure said to wait: nobody asks before then, the old reading or not")
        self.now += timedelta(seconds=301)
        self.assertEqual(bill.view()["state"], "ok")
        self.assertEqual(len(azure.requests), 2)

    def test_what_the_page_gets_holds_only_numbers_and_plain_words(self):
        v = self.bill(Azure()).view()
        text = json.dumps(v)
        self.assertNotIn("management.azure.com", text)
        self.assertNotIn("nextLink", text)
        self.assertNotIn(RESOURCE, text)
        self.assertIn(f"scope/%2Fsubscriptions%2F{SUB}%2FresourceGroups%2F{RG}", v["link"], "the owner's own link to Azure's cost analysis")

    def test_a_reading_older_than_six_hours_is_read_again_without_the_page_waiting(self):
        bill = self.bill(Azure())
        bill.view()
        done = threading.Event()
        calls = []

        def again(doc):
            calls.append(doc.get("read_at"))
            done.set()

        self.now += timedelta(hours=7)
        with mock.patch.object(bill, "_read", again):
            v = bill.view()
            self.assertTrue(done.wait(5), "a new reading was started")
        self.assertEqual(v["state"], "ok", "the page got the last reading at once")
        self.assertEqual(len(calls), 1)

    def test_a_new_month_is_read_at_once_and_the_old_reading_says_what_it_is(self):
        bill = self.bill(Azure())
        bill.view()
        self.now += timedelta(days=13)   # 1 November
        doc = bill._load()
        self.assertTrue(bill._due(doc, self.now))
        shown = bill._present(doc, self.now)
        self.assertTrue(shown["stale"])
        self.assertIn("last month", shown["stale_reason"])

    def test_last_months_cost_failing_alone_does_not_lose_this_months(self):
        v = self.bill(Azure(last_status=429)).view()
        self.assertEqual(v["state"], "ok", "this month was read: the comparison is a line beside it")
        self.assertIsNone(v["previous"])
        self.assertFalse(v["stale"])
        self.assertEqual(self.store.read("content", "bill")["attempt"]["state"], "ok")

    def test_a_month_of_credits_or_nothing_is_still_last_month(self):
        self.assertEqual(self.bill(Azure(last=[[-12.5, "USD"]])).view()["previous"], {"month": "2026-09", "total": -12.5, "currency": "USD"})
        self.store.delete("content", "bill")
        self.assertEqual(self.bill(Azure(last=[[0.0, "USD"]])).view()["previous"], {"month": "2026-09", "total": 0.0, "currency": "USD"})

    def test_last_month_in_two_currencies_is_not_compared(self):
        v = self.bill(Azure(last=[[100.0, "USD"], [90.0, "EUR"]])).view()
        self.assertEqual(v["state"], "ok")
        self.assertIsNone(v["previous"])
        self.store.delete("content", "bill")
        v = self.bill(Azure(last=[[90.0, "EUR"]])).view()
        self.assertEqual(v["previous"], {"month": "2026-09", "total": 90.0, "currency": "EUR"}, "its own currency travels with it")

    def test_a_reading_of_last_month_says_which_month_it_is_of(self):
        bill = self.bill(Azure())
        self.assertEqual(bill.view()["current_month"], "2026-10")
        self.now += timedelta(days=13)   # 1 November, before the new reading
        shown = bill._present(bill._load(), self.now)
        self.assertEqual((shown["month"]["id"], shown["current_month"]), ("2026-10", "2026-11"), "the page says this is last month's")

    def test_the_view_has_a_link_to_set_a_budget_for_the_owner(self):
        v = self.bill(Azure()).view()
        self.assertIn(f"budgets/scope/%2Fsubscriptions%2F{SUB}%2FresourceGroups%2F{RG}", v["budget_link"])

    def test_the_first_day_of_a_month_has_no_cost_yet_and_is_never_shown_as_zero(self):
        bill = self.bill(Azure(month=[], last=[[480.0, "USD"]]))
        v = bill.view()
        self.assertEqual(v["state"], "empty")
        self.assertEqual(v["reason"], bill_mod.EMPTY)
        self.assertEqual(v["previous"]["total"], 480.0)
        self.assertNotIn("groups", v)
        self.assertNotIn("total", v.get("month", {}))

    def test_an_empty_reading_kept_across_the_month_end_is_not_this_months(self):
        bill = self.bill(Azure(month=[], last=[[480.0, "USD"]]))
        self.assertEqual(bill.view()["state"], "empty")
        self.now += timedelta(days=13)   # 1 November: nothing was read since the first of October
        shown = bill._present(bill._load(), self.now)
        self.assertEqual(shown["state"], "error", "\"nothing yet this month\" was said of October")
        self.assertIn("reading this month's bill", shown["reason"])
        self.assertNotIn("previous", shown)
        broken = self.bill(Azure(status=500))   # the refresh that view() starts fails: the page must not say one is under way
        broken.refresh()
        again = broken._present(broken._load(), self.now)
        self.assertEqual((again["state"], again["reason"]), ("error", bill_mod.FAILED), "it says what happened, not that a read is running")

    def test_an_empty_reading_whose_refresh_failed_or_was_throttled_says_so_in_the_same_month_too(self):
        self.bill(Azure(month=[], last=[[480.0, "USD"]])).view()
        self.now += timedelta(hours=7)   # the reading is due again, still the same month
        for status, state in ((500, "failed"), (429, "throttled")):
            self.store.write("content", "bill", value={**self.store.read("content", "bill"), "attempt": {"at": iso(self.now), "state": "ok", "retry_at": None, "reason": None}})
            broken = self.bill(Azure(status=status, headers={"Retry-After": "120"}))
            broken.refresh()
            shown = broken._present(broken._load(), self.now)
            self.assertEqual(shown["state"], "error", state)
            self.assertNotIn("this month", shown["reason"].replace("this month's", ""), "it does not say nothing was reported this month")
        fresh = self.bill(Azure(month=[], last=[[480.0, "USD"]]))
        self.store.write("content", "bill", value={**self.store.read("content", "bill"), "attempt": {"at": iso(self.now), "state": "ok", "retry_at": None, "reason": None}})
        self.assertEqual(fresh._present(fresh._load(), self.now)["state"], "empty", "a good empty reading of this month is still shown as one")

    def test_without_the_role_the_tab_says_what_is_missing_and_asks_again_in_a_few_minutes(self):
        azure = Azure(status=403)
        bill = self.bill(azure)
        v = bill.view()
        self.assertEqual(v["state"], "not_connected")
        self.assertIn("Cost Management Reader", v["detail"])
        self.assertIn(RG, v["detail"])
        self.assertNotIn(SUB, json.dumps(v), "Azure's own error text, with the subscription in it, is never passed on")
        calls = len(azure.requests)
        bill.view()
        self.assertEqual(len(azure.requests), calls, "not again within five minutes")
        azure.status = 200
        self.now += timedelta(minutes=6)
        self.assertEqual(bill.view()["state"], "ok", "the role was granted: the next visit shows the bill")

    def test_a_visit_while_azure_says_wait_keeps_the_last_reading_and_waits_even_after_a_restart(self):
        azure = Azure()
        bill = self.bill(azure)
        bill.view()
        azure.status, azure.headers = 429, {"Retry-After": "120"}
        self.now += timedelta(hours=7)
        bill.refresh()
        v = bill.view()
        self.assertEqual(v["state"], "ok")
        self.assertTrue(v["stale"])
        self.assertEqual(v["stale_reason"], bill_mod.WAITING)
        calls = len(azure.requests)
        again = Bill(self.store, self.tokens, RESOURCE, httpx.Client(transport=httpx.MockTransport(azure)))
        again.view()
        self.now += timedelta(seconds=60)
        again.view()
        self.assertEqual(len(azure.requests), calls, "nobody asks while Azure said to wait, a restart included")
        azure.status = 200
        self.now += timedelta(seconds=90)
        again.refresh()
        self.assertFalse(again.view()["stale"], "after the wait the reading is fresh again")

    def test_how_long_azure_asks_to_wait_is_kept_within_bounds(self):
        self.assertEqual(Bill._wait({"Retry-After": "5"}), bill_mod.WAIT_MIN_S)
        self.assertEqual(Bill._wait({"Retry-After": "999999"}), bill_mod.WAIT_MAX_S)
        self.assertEqual(Bill._wait({"x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "90"}), 90)
        self.assertEqual(Bill._wait({}), bill_mod.WAIT_DEFAULT_S)
        self.assertEqual(Bill._wait({"Retry-After": "soon"}), bill_mod.WAIT_DEFAULT_S)

    def test_an_error_with_nothing_read_before_says_so_and_waits_before_trying_again(self):
        azure = Azure(status=503)
        bill = self.bill(azure)
        v = bill.view()
        self.assertEqual((v["state"], v["reason"]), ("error", bill_mod.FAILED))
        calls = len(azure.requests)
        bill.view()
        self.assertEqual(len(azure.requests), calls)

    def test_a_timeout_is_an_error_that_keeps_the_last_reading(self):
        bill = self.bill(Azure())
        bill.view()

        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        bill.http = httpx.Client(transport=httpx.MockTransport(slow))
        self.now += timedelta(hours=7)
        bill.refresh()
        v = bill.view()
        self.assertEqual((v["state"], v["stale"], v["stale_reason"]), ("ok", True, bill_mod.FAILED))

    def test_every_page_is_read_and_a_continuation_that_leaves_azure_is_never_followed(self):
        first = october()[:40]
        rest = october()[40:80]

        def pages(request: httpx.Request) -> httpx.Response:
            if "page2" in str(request.url):
                return httpx.Response(200, json={"properties": {"columns": COLUMNS, "rows": rest, "nextLink": None}})
            return httpx.Response(200, json={"properties": {"columns": COLUMNS, "rows": first,
                                                            "nextLink": f"https://management.azure.com/subscriptions/{SUB}/q?page2=1&api-version=2023-11-01"}})

        bill = self.bill(Azure(pages=pages))
        v = bill.view()
        self.assertEqual(v["state"], "ok")
        self.assertAlmostEqual(v["month"]["total"], round(sum(r[0] for r in first + rest), 2), delta=0.02, msg="both pages are in the total")

        def away(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"properties": {"columns": COLUMNS, "rows": first, "nextLink": "https://example.test/steal?token"}})

        self.store.delete("content", "bill")
        stolen = self.bill(Azure(pages=away))
        v = stolen.view()
        self.assertEqual((v["state"], v["reason"]), ("error", bill_mod.INCOMPLETE), "no total from a part of the lines")
        self.assertEqual([r.url.host for r in self.azure.requests], ["management.azure.com", "management.azure.com"], "the token went to Azure only")

    def test_more_pages_than_dojo_reads_is_not_shown_as_a_total(self):
        def endless(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"properties": {"columns": COLUMNS, "rows": october()[:3], "nextLink": f"https://management.azure.com/x?n={len(self.azure.requests)}"}})

        v = self.bill(Azure(pages=endless)).view()
        self.assertEqual((v["state"], v["reason"]), ("error", bill_mod.INCOMPLETE))
        self.assertEqual(len([r for r in self.azure.requests if "x" == r.url.path.strip("/")]), bill_mod.MAX_PAGES - 1)

    def test_a_line_with_a_cost_that_is_not_a_number_is_left_out_not_guessed(self):
        rows = october() + [[None, 20261019, "Foundry Models", "x", "USD"], ["n/a", 20261019, "Foundry Models", "x", "USD"]]
        clean = self.bill(Azure()).view()["month"]["total"]
        self.store.delete("content", "bill")
        self.assertEqual(self.bill(Azure(month=rows)).view()["month"]["total"], clean)

    def test_two_visits_at_once_make_one_reading(self):
        azure = Azure()
        azure.gate = threading.Event()
        bill = self.bill(azure)
        answers = []
        first = threading.Thread(target=lambda: answers.append(bill.view()))
        first.start()
        for _ in range(100):
            if azure.requests:
                break
            threading.Event().wait(0.02)
        second = threading.Thread(target=lambda: answers.append(bill.view()))
        second.start()
        threading.Event().wait(0.2)
        azure.gate.set()
        first.join(10)
        second.join(10)
        self.assertEqual([a["state"] for a in answers], ["ok", "ok"], "the second visit waited for the first reading, not an error")
        self.assertEqual(len(azure.requests), 2, "this month and last month, once")

    def test_warming_reads_an_old_reading_a_little_after_startup_and_a_fresh_one_not_at_all(self):
        azure = Azure()
        bill = self.bill(azure)
        bill.warm(delay=0)
        for _ in range(100):
            if len(azure.requests) >= 2:
                break
            threading.Event().wait(0.05)
        self.assertEqual(len(azure.requests), 2, "this month and last month")
        bill.warm(delay=0)
        threading.Event().wait(0.3)
        self.assertEqual(len(azure.requests), 2, "fresh: nobody asks Azure again")
        outside = self.bill(Azure(), tokens=False)
        outside.warm(delay=0)
        threading.Event().wait(0.2)
        self.assertEqual(self.azure.requests, [], "outside Azure there is nothing to warm")

    def test_outside_azure_nothing_is_asked(self):
        azure = Azure()
        v = self.bill(azure, tokens=False).view()
        self.assertEqual((v["state"], v["reason"]), ("not_connected", bill_mod.NOT_IN_AZURE))
        self.assertEqual(azure.requests, [])
        self.assertEqual(Bill(self.store, self.tokens, None).view()["reason"], bill_mod.NO_GROUP)
        self.assertEqual(Bill(self.store, self.tokens, "/subscriptions/not-a-subscription/resourceGroups/x").view()["reason"], bill_mod.NO_GROUP)


class RouteTests(unittest.TestCase):
    def test_only_the_owner_reads_it_and_a_copy_outside_azure_says_so(self):
        from tests.test_newexam import Web, make_app
        tmp = tempfile.mkdtemp(prefix="dojo-bill-route-")
        app = make_app(tmp, Web())
        c = TestClient(app)
        r = c.get("/api/studio/bill")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "not_connected")
        self.assertIn("only read when Dojo runs in Azure", r.json()["reason"])
        self.assertEqual(c.get("/api/studio/bill?fresh=true").json(), r.json(), "no parameter forces a new reading")


if __name__ == "__main__":
    unittest.main()
