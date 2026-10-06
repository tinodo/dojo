"""The cost view (DESIGN section 3, brief D15): list prices parsed from a saved Retail Prices API answer,
fetched at most once a day, "price not found" instead of a guess, usage counted from what is stored,
and nothing written but the price cache."""
import dataclasses
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import costs  # noqa: E402
from app.core import iso, load_settings, utcnow  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import settings, web  # noqa: E402
from tests.test_delivery import EXCLUSION, SCOPE, add_lesson, film  # noqa: E402

SAMPLE = json.loads((Path(__file__).parent / "data" / "retail-prices-speech-swedencentral.json").read_text("utf-8"))
# The Foundry Models meters of the three models Dojo runs, as the Retail Prices API gave them for Sweden Central.
MODELS = json.loads((Path(__file__).parent / "data" / "retail-prices-models-swedencentral.json").read_text("utf-8"))
VIDEO, VOICE, STT = "TTS Standard Avatar Batch Speech", "S1 Neural Text To Speech Characters", "Fast Transcription Speech To Text"
# The models as the live settings name them: each pinned to a version.
LIVE = {"author": "gpt-5.5@2026-04-24", "gate": "grok-4-1-fast-reasoning@1", "grader": "DeepSeek-V4-Pro@2026-04-23"}
# model -> deployment type -> (input, output) list price per million tokens
PER_MILLION = {
    "gpt-5.5": {"GlobalStandard": (5.0, 30.0), "DataZoneStandard": (5.5, 33.0)},
    "grok-4-1-fast-reasoning": {"GlobalStandard": (0.2, 0.5), "DataZoneStandard": (0.22, 0.55)},
    "deepseek-v4-pro": {"GlobalStandard": (1.74, 3.48), "DataZoneStandard": (1.91, 3.83)},
}


def item(name: str) -> dict:
    return next(i for i in SAMPLE["Items"] if i["meterName"] == name)


def files(root: str) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): p.stat().st_mtime_ns for p in Path(root).rglob("*") if p.is_file()}


class RetailPriceParsingTests(unittest.TestCase):
    def test_the_saved_answer_gives_the_three_speech_meters(self):
        found = costs.parse_prices(SAMPLE["Items"], "swedencentral")
        self.assertEqual({k: v["meter"] for k, v in found.items()}, {"video": VIDEO, "voice": VOICE, "transcription": STT})
        self.assertEqual((found["video"]["price"], found["video"]["unit"]), (1.0, "1 Minute"))
        self.assertEqual((found["voice"]["price"], found["voice"]["unit"]), (15.0, "1M"))
        self.assertEqual((found["transcription"]["price"], found["transcription"]["unit"]), (0.36, "1 Hour"))
        self.assertAlmostEqual(found["video"]["per_second"], 1 / 60)
        self.assertAlmostEqual(found["voice"]["per_character"], 15 / 1_000_000)
        self.assertAlmostEqual(found["transcription"]["per_second"], 0.36 / 3600)
        self.assertTrue(all(v["currency"] == "USD" and v["meter_id"] and v["product"] == "Azure Speech" for v in found.values()))

    def test_a_missing_meter_or_another_region_is_not_found_rather_than_guessed(self):
        without = [i for i in SAMPLE["Items"] if i["meterName"] != STT]
        self.assertIsNone(costs.parse_prices(without, "swedencentral")["transcription"])
        self.assertEqual(costs.parse_prices(SAMPLE["Items"], "westeurope"), {k: None for k in costs.METERS})
        self.assertEqual(costs.parse_prices([], "swedencentral"), {k: None for k in costs.METERS})

    def test_only_the_first_pay_as_you_go_tier_at_the_latest_date_counts(self):
        video = item(VIDEO)
        older = {**video, "retailPrice": 0.5, "effectiveStartDate": "2025-01-01T00:00:00Z"}
        tier = {**video, "retailPrice": 0.1, "tierMinimumUnits": 1000.0}
        reserved = {**video, "retailPrice": 0.2, "type": "Reservation"}
        self.assertEqual(costs.pick([older, tier, reserved, video], VIDEO, "swedencentral")["retailPrice"], 1.0)
        self.assertEqual(costs.pick([older, tier], VIDEO, "swedencentral")["retailPrice"], 0.5)
        self.assertIsNone(costs.pick([tier, reserved], VIDEO, "swedencentral"))
        broken = [{**video, "retailPrice": "1.0"}, {**video, "tierMinimumUnits": "n/a"}, {**video, "retailPrice": True}, "x"]
        self.assertIsNone(costs.pick(broken, VIDEO, "swedencentral"))
        self.assertIsNone(costs.parse_prices([{**video, "unitOfMeasure": "1 GB"}], "swedencentral")["video"],
                          "a unit that is not time is not a price per minute")
        self.assertIsNone(costs.parse_prices([{**item(VOICE), "unitOfMeasure": "1 Hour"}], "swedencentral")["voice"])

    def test_units_of_measure(self):
        cases = {"1 Minute": (1, "minute"), "1 Hour": (1, "hour"), "1 Hours": (1, "hour"), "100 Minutes": (100, "minute"),
                 "1/Hour": (1, "hour"), "1M": (1_000_000, "unit"), "1 M": (1_000_000, "unit"), "10K": (10_000, "unit"),
                 "1K Characters": (1000, "character"), "1 Month": (1, "month"), "1": (1, "unit"), " 1 Second ": (1, "second")}
        for unit, expected in cases.items():
            self.assertEqual(costs.parse_unit(unit), expected, unit)
        for unit in ("", None, "0 Minute", "per minute", "Minute", "1 Minute!"):
            self.assertIsNone(costs.parse_unit(unit), unit)
        self.assertAlmostEqual(costs.per_measure(1.0, "1 Minute", "second"), 1 / 60)
        self.assertAlmostEqual(costs.per_measure(0.36, "1 Hour", "second"), 0.0001)
        self.assertAlmostEqual(costs.per_measure(15.0, "1M", "character"), 0.000015)
        self.assertIsNone(costs.per_measure(15.0, "1M", "second"))
        self.assertIsNone(costs.per_measure(1.0, "1 Minute", "character"))
        self.assertIsNone(costs.per_measure(1.0, "1 Month", "second"), "a month is not a fixed number of seconds here")

    def test_the_filter_asks_for_the_speech_meters_in_the_region(self):
        sent = costs.retail_filter("swedencentral")
        self.assertTrue(sent.startswith("armRegionName eq 'swedencentral' and priceType eq 'Consumption' and ("))
        for name in (VIDEO, VOICE, STT):
            self.assertIn(f"meterName eq '{name}'", sent)

    def test_the_filter_asks_for_every_models_input_and_output_meters(self):
        sent = costs.retail_filter("swedencentral")
        self.assertEqual(len(costs.model_meters()), 12, "3 models, 2 deployment types, input and output")
        for name in costs.model_meters():
            self.assertIn(f"meterName eq '{name}'", sent)
        self.assertEqual(sorted(costs.model_meters()), sorted(i["meterName"] for i in MODELS["Items"]),
                         "the saved answer holds exactly the meters asked for")


class ModelPriceParsingTests(unittest.TestCase):
    """The models' token meters, as the Retail Prices API names them (service "Foundry Models")."""

    def test_the_saved_answer_gives_each_models_input_and_output_price_per_deployment_type(self):
        found = costs.parse_models(MODELS["Items"], "swedencentral")
        self.assertEqual(set(found), set(PER_MILLION))
        products = {"gpt-5.5": "Azure OpenAI GPT5", "grok-4-1-fast-reasoning": "Azure Grok Models",
                    "deepseek-v4-pro": "Azure Deepseek Models"}
        for model, deployments in PER_MILLION.items():
            self.assertEqual(set(found[model]), {"GlobalStandard", "DataZoneStandard"}, model)
            for deployment, (price_in, price_out) in deployments.items():
                entry = found[model][deployment]
                inp, outp, product = costs.MODEL_METERS[model][deployment]
                self.assertEqual((entry["product"], entry["service"], entry["currency"]),
                                 (products[model], "Foundry Models", "USD"), (model, deployment))
                self.assertEqual((entry["input"]["meter"], entry["output"]["meter"]), (inp, outp))
                self.assertEqual((entry["input"]["per_million"], entry["output"]["per_million"]), (price_in, price_out))
                self.assertAlmostEqual(entry["input"]["per_token"], price_in / 1e6)
                self.assertAlmostEqual(entry["output"]["per_token"], price_out / 1e6)
                self.assertTrue(entry["input"]["meter_id"] and entry["output"]["meter_id"])
        # gpt-5.5 is listed per million tokens; Grok and DeepSeek per thousand.
        self.assertEqual((found["gpt-5.5"]["GlobalStandard"]["input"]["price"],
                          found["gpt-5.5"]["GlobalStandard"]["input"]["unit"]), (5.0, "1M"))
        self.assertEqual((found["grok-4-1-fast-reasoning"]["GlobalStandard"]["output"]["price"],
                          found["grok-4-1-fast-reasoning"]["GlobalStandard"]["output"]["unit"]), (0.0005, "1K"))
        self.assertEqual((found["deepseek-v4-pro"]["DataZoneStandard"]["input"]["price"],
                          found["deepseek-v4-pro"]["DataZoneStandard"]["input"]["unit"]), (0.00191, "1K"))

    def test_a_model_price_needs_both_of_its_meters_from_its_own_product(self):
        items = MODELS["Items"]
        without = [i for i in items if i["meterName"] != "V4 Pro Outp glbl Tokens"]
        found = costs.parse_models(without, "swedencentral")
        self.assertIsNone(found["deepseek-v4-pro"]["GlobalStandard"], "an input price alone is not a price")
        self.assertIsNotNone(found["deepseek-v4-pro"]["DataZoneStandard"])
        # The same meter name sold by another product (like the Fireworks-hosted models) is not this price.
        elsewhere = [{**i, "productName": "Azure Fireworks Models"} if i["productName"] == "Azure Grok Models" else i
                     for i in items]
        self.assertEqual(costs.parse_models(elsewhere, "swedencentral")["grok-4-1-fast-reasoning"],
                         {"GlobalStandard": None, "DataZoneStandard": None})
        per_hour = [{**i, "unitOfMeasure": "1 Hour"} if i["meterName"].startswith("5.5 ") else i for i in items]
        self.assertEqual(costs.parse_models(per_hour, "swedencentral")["gpt-5.5"],
                         {"GlobalStandard": None, "DataZoneStandard": None}, "a unit that is not tokens")
        nothing = {model: {d: None for d in deployments} for model, deployments in costs.MODEL_METERS.items()}
        self.assertEqual(costs.parse_models(items, "westeurope"), nothing)
        self.assertEqual(costs.parse_models([], "swedencentral"), nothing)

    def test_a_model_is_looked_up_without_the_version_it_is_pinned_to(self):
        cases = {"gpt-5.5@2026-04-24": "gpt-5.5", "DeepSeek-V4-Pro@2026-04-23": "DeepSeek-V4-Pro",
                 "grok-4-1-fast-reasoning": "grok-4-1-fast-reasoning", " gpt-5.5 @1": "gpt-5.5", "": "", None: ""}
        for configured, expected in cases.items():
            self.assertEqual(costs.model_name(configured), expected, configured)

    def test_the_known_meters_are_the_ones_the_prices_pages_list(self):
        self.assertEqual(set(costs.MODEL_METERS), set(PER_MILLION), "the three models Dojo runs")
        for model, deployments in costs.MODEL_METERS.items():
            self.assertEqual(set(deployments), set(costs.DEPLOYMENTS), model)
            self.assertEqual(model, model.lower(), "looked up in lower case")
        self.assertEqual(costs.DEPLOYMENT, "GlobalStandard")


class PriceDesk:
    """prices.azure.com on a leash: the saved answer, or a failure when told; counts the calls."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.body = SAMPLE

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "prices.azure.com":
            return web(request)
        self.calls.append(request)
        return httpx.Response(self.status, json=self.body if self.status == 200 else {"error": "down"})


class CostViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-costs-")
        self.desk = PriceDesk()
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(self.desk)))
        self.dojo, self.check, self.costs = self.app.state.dojo, self.app.state.checker, self.app.state.costs

    def with_models(self, **changes) -> costs.Costs:
        """Dojo with other settings (the live model names, another deployment type), and a price list that
        holds the Speech and the models' meters."""
        self.desk.body = {**SAMPLE, "Items": SAMPLE["Items"] + MODELS["Items"], "Count": 15}
        tmp = tempfile.mkdtemp(prefix="dojo-costs-")
        app = create_app(dataclasses.replace(settings(tmp), **changes),
                         http=httpx.Client(transport=httpx.MockTransport(self.desk)))
        return app.state.costs

    def age_prices(self, hours: float) -> None:
        doc = self.dojo.store.read("content", "prices")
        doc["fetched"] = iso(utcnow() - timedelta(hours=hours))
        self.dojo.store.write("content", "prices", value=doc)

    def test_prices_are_fetched_once_a_day(self):
        first = self.costs.prices()
        self.assertEqual(len(self.desk.calls), 1)
        self.assertEqual(self.desk.calls[0].url.params["$filter"], costs.retail_filter("swedencentral"))
        self.assertEqual(first["meters"]["video"]["price"], 1.0)
        self.assertEqual(self.costs.prices(), first)
        self.assertEqual(len(self.desk.calls), 1, "kept on the share for a day")
        self.age_prices(25)
        self.assertNotEqual(self.costs.prices()["fetched"], iso(utcnow() - timedelta(hours=25)))
        self.assertEqual(len(self.desk.calls), 2, "fetched again after a day")

    def test_without_prices_every_line_says_price_not_found_and_nothing_is_guessed(self):
        self.desk.status = 503
        view = self.costs.view()
        self.assertEqual({line["note"] for line in view["lines"]}, {costs.NOT_FOUND})
        self.assertTrue(all(line["cost"] is None and line["price"] is None and not line["found"] for line in view["lines"]))
        self.assertEqual([line["meter"] for line in view["lines"]], [VIDEO, VOICE, VOICE, VOICE, STT], "the meter it looked for")
        self.assertEqual((view["total"], view["complete"], view["stale"], view["fetched"]), (0.0, False, True, None))
        self.assertEqual({m["note"] for m in view["models"]}, {costs.UNREAD}, "the models' prices could not be read either")
        self.assertFalse(any(m["found"] or m["input"] or m["output"] for m in view["models"]))
        self.costs.view()
        self.assertEqual(len(self.desk.calls), 1, "no second try within the hour")
        self.costs._failed_at -= costs.RETRY_AFTER_FAILURE_S + 1
        self.desk.status = 200
        self.assertTrue(self.costs.view()["complete"])
        self.assertEqual(len(self.desk.calls), 2)

    def test_a_failed_refresh_keeps_the_last_prices_and_says_they_are_old(self):
        self.costs.prices()
        fresh = TestClient(self.app).get("/api/studio/costs").json()
        self.assertEqual((fresh["stale"], fresh["fetched"]), (False, self.dojo.store.read("content", "prices")["fetched"]))
        self.age_prices(30)
        old = self.dojo.store.read("content", "prices")["fetched"]
        self.desk.status = 500
        doc = self.costs.prices()
        self.assertTrue(doc["stale"])
        self.assertEqual(doc["meters"]["transcription"]["price"], 0.36)
        # The Studio gets the time of the old list and that it could not be refreshed, with its total.
        body = TestClient(self.app).get("/api/studio/costs").json()
        self.assertEqual((body["stale"], body["fetched"]), (True, old))
        self.assertTrue(all(line["found"] for line in body["lines"]), "the old prices still count")

    def test_usage_is_counted_from_what_is_stored_and_priced_per_meter(self):
        one = add_lesson(self.dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00")
        two = add_lesson(self.dojo, 2, SCOPE, "2026-09-02T00:00:00+00:00", audio=False)
        add_lesson(self.dojo, 3, EXCLUSION, "2026-09-03T00:00:00+00:00")   # the same narration: the same audio, paid once
        first_slide = self.check.plan(two)["audio"][0]
        self.dojo.store.write_bytes("media", first_slide + ".mp3", data=b"ID3")
        film(self.dojo, self.check, one, "guide")   # two turns: the last one ends at 18 seconds
        self.dojo.store.write("content", "delivery-state", value={
            "day": iso()[:10], "today": {"listen": 0, "watch": 0}, "seconds": {"listen": 120.0, "watch": 60.0}, "failed": {}})
        listen = sum(len(t) for slide in EXCLUSION for _, t in slide) + sum(len(t) for _, t in SCOPE[0])
        watch = sum(len(t) for slide in EXCLUSION for who, t in slide if who == "guide")

        view = self.costs.view()
        usage = view["usage"]
        self.assertEqual((usage["videos"], usage["video_seconds"], usage["audio_files"], usage["narrated_lessons"]), (1, 18.0, 3, 3))
        self.assertEqual((usage["listen_characters"], usage["watch_characters"], usage["transcribed_seconds"]), (listen, watch, 180.0))
        lines = {line["key"]: line for line in view["lines"]}
        self.assertEqual([line["key"] for line in view["lines"]],
                         ["video", "listen_voice", "deep_voice", "watch_voice", "transcription"])
        self.assertEqual((lines["deep_voice"]["quantity"], lines["deep_voice"]["cost"]), (0, 0.0), "no Deeper Listen yet")
        self.assertEqual((lines["video"]["quantity"], lines["video"]["unit"], lines["video"]["cost"]), (0.3, "minutes", 0.3))
        self.assertEqual((lines["listen_voice"]["quantity"], lines["listen_voice"]["cost"]), (listen, round(listen * 15e-6, 4)))
        self.assertEqual((lines["watch_voice"]["quantity"], lines["watch_voice"]["cost"]), (watch, round(watch * 15e-6, 4)))
        self.assertEqual((lines["transcription"]["quantity"], lines["transcription"]["cost"]), (3.0, 0.018))
        self.assertEqual(lines["video"]["price"], 1.0)
        self.assertEqual(lines["video"]["price_unit"], "1 Minute")
        self.assertEqual(view["total"], round(0.3 + round(listen * 15e-6, 4) + round(watch * 15e-6, 4) + 0.018, 2))
        self.assertTrue(view["complete"])
        self.assertEqual(view["label"], "Estimate from list prices; your bill is in Azure Cost Management.")

    def test_a_meter_missing_from_the_answer_leaves_only_its_line_unpriced(self):
        self.desk.body = {**SAMPLE, "Items": [i for i in SAMPLE["Items"] if i["meterName"] != STT], "Count": 2}
        self.dojo.store.write("content", "delivery-state", value={
            "day": iso()[:10], "today": {"listen": 0, "watch": 0}, "seconds": {"listen": 600.0, "watch": 0.0}, "failed": {}})
        view = self.costs.view()
        lines = {line["key"]: line for line in view["lines"]}
        self.assertEqual((lines["transcription"]["found"], lines["transcription"]["note"], lines["transcription"]["cost"]),
                         (False, costs.NOT_FOUND, None))
        self.assertEqual(lines["transcription"]["quantity"], 10.0, "the amount is still shown")
        self.assertTrue(all(lines[k]["found"] for k in ("video", "listen_voice", "deep_voice", "watch_voice")))
        self.assertFalse(view["complete"])
        self.assertFalse(view["stale"], "the answer came; one meter was just not in it")

    def test_the_cost_view_writes_nothing_but_the_price_cache(self):
        add_lesson(self.dojo, 1, EXCLUSION, "2026-09-01T00:00:00+00:00")
        before = files(self.tmp)
        self.costs.view()
        after = files(self.tmp)
        self.assertEqual({k for k in after if before.get(k) != after[k]}, {"content/prices.json"})

    def test_studio_shows_the_models_list_prices_from_the_lookup_the_estimate_uses(self):
        view_costs = self.with_models(models=LIVE)
        view = view_costs.view()
        rows = view["models"]
        self.assertEqual([(m["role"], m["model"], m["deployment"]) for m in rows],
                         [("author", "gpt-5.5", "GlobalStandard"), ("gate", "grok-4-1-fast-reasoning", "GlobalStandard"),
                          ("grader", "DeepSeek-V4-Pro", "GlobalStandard")])
        for m in rows:
            key = m["model"].lower()
            self.assertEqual((m["found"], m["note"]), (True, None), key)
            self.assertEqual((m["input"]["per_million"], m["output"]["per_million"]), PER_MILLION[key]["GlobalStandard"])
            self.assertEqual(m["meters"], list(costs.MODEL_METERS[key]["GlobalStandard"][:2]))
            self.assertEqual(m, view_costs.model_price(view_costs.prices(), m["role"]))
        by_role = {m["role"]: m for m in rows}
        for line in view_costs.course_estimate(10)["lines"][:4]:
            m = by_role[line["role"]]
            self.assertEqual(line["per_million"], {"input": m["input"]["per_million"], "output": m["output"]["per_million"]},
                             "the estimate prices a model the way Studio shows it")
        self.assertEqual((view["total"], view["complete"]), (0.0, True), "the Studio total is Speech only")
        self.assertTrue(any("Model calls" in note and "not in this total" in note for note in view["notes"]))

    def test_a_model_with_no_public_list_price_says_so_in_studio(self):
        view = self.with_models(models={**LIVE, "grader": "Mistral-Imaginary@2026-01-01"}).view()
        grader = view["models"][2]
        self.assertEqual((grader["model"], grader["found"], grader["input"], grader["output"], grader["meters"]),
                         ("Mistral-Imaginary", False, None, None, []))
        self.assertEqual(grader["note"], "no public list price for Mistral-Imaginary; not included")
        self.assertTrue(view["models"][0]["found"] and view["models"][1]["found"])
        self.assertTrue(view["complete"], "the Speech total does not hold model calls, so nothing is missing from it")

    def test_a_data_zone_deployment_shows_its_own_prices(self):
        rows = self.with_models(models=LIVE, model_sku="DataZoneStandard").view()["models"]
        self.assertEqual([(m["input"]["per_million"], m["output"]["per_million"]) for m in rows],
                         [(5.5, 33.0), (0.22, 0.55), (1.91, 3.83)])
        self.assertEqual({m["deployment"] for m in rows}, {"DataZoneStandard"})

    def test_a_price_list_kept_in_an_older_shape_is_fetched_again(self):
        # What build b0e00ea kept on the share: no version, and one price per model.
        old = {"fetched": iso(), "region": "swedencentral", "source": costs.PRICES_URL, "filter": "armRegionName eq ...",
               "meters": {key: None for key in costs.METERS}, "models": {"gpt-5.5": None, "grok-4-1-fast-reasoning": None}}
        self.dojo.store.write("content", "prices", value=old)
        self.desk.status = 500
        doc = self.costs.prices()
        self.assertEqual(len(self.desk.calls), 1, "not kept: it has another shape")
        self.assertEqual((doc["fetched"], doc["stale"], doc["version"]), (None, True, costs.PRICES_VERSION),
                         "and not used when the new list cannot be read")
        self.assertEqual(doc["models"]["gpt-5.5"], {"GlobalStandard": None, "DataZoneStandard": None})
        self.costs._failed_at = None
        self.desk.status = 200
        doc = self.costs.prices()
        self.assertEqual((len(self.desk.calls), doc["version"]), (2, costs.PRICES_VERSION))
        self.assertEqual(self.dojo.store.read("content", "prices")["version"], costs.PRICES_VERSION)

    def test_the_studio_api_shape(self):
        body = TestClient(self.app).get("/api/studio/costs").json()
        self.assertEqual(set(body), {"label", "region", "currency", "fetched", "stale", "source", "lines", "total",
                                     "complete", "usage", "models", "notes", "deep", "exam_items"})
        self.assertEqual((body["label"], body["region"], body["currency"], body["source"]),
                         (costs.LABEL, "swedencentral", "USD", costs.PRICES_URL))
        for line in body["lines"]:
            self.assertEqual(set(line), {"key", "what", "quantity", "unit", "meter", "price", "price_unit", "currency",
                                         "cost", "found", "note"})
        self.assertEqual([m["role"] for m in body["models"]], ["author", "gate", "grader"])
        for m in body["models"]:
            self.assertEqual(set(m), {"role", "model", "deployment", "found", "product", "input", "output", "meters", "note"})
        self.assertFalse({"budget", "alert", "alerts", "budgets"} & set(body), "budgets and alerts are the owner's decision")
        self.assertEqual(len(body["notes"]), 4)
        self.assertTrue(any("private endpoint" in note and "free offer" in note for note in body["notes"]))
        self.assertTrue(any(all(part in note for part in
                                ("$23/month", "SqlServers Standard", "$15", "$7.30", "$0.50", "$0"))
                            for note in body["notes"]))


class DeploymentTypeTests(unittest.TestCase):
    """The deployment type the prices are looked up for is the one the models are deployed with."""

    def test_the_bicep_deploys_every_model_with_the_type_it_tells_the_app(self):
        bicep = (Path(__file__).resolve().parents[1] / "infra" / "main.bicep").read_text("utf-8")
        self.assertIn(f"param modelSku string = '{costs.DEPLOYMENT}'", bicep)
        for role in costs.ROLES:
            deployment = bicep.split(f"resource {role}Deployment ", 1)[1].split("\nresource ", 1)[0]
            self.assertRegex(deployment, r"sku:\s*\{\s*name: modelSku\s", role)
        self.assertRegex(bicep, r"name: 'DOJO_MODEL_SKU'\s*value: modelSku\s")

    def test_the_setting_is_read_and_defaults_to_global_standard(self):
        with mock.patch.dict(os.environ, {"DOJO_MODEL_SKU": " DataZoneStandard "}):
            self.assertEqual(load_settings().model_sku, "DataZoneStandard")
        with mock.patch.dict(os.environ, {"DOJO_MODEL_SKU": ""}):
            self.assertEqual(load_settings().model_sku, costs.DEPLOYMENT)


if __name__ == "__main__":
    unittest.main()
