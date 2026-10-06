"""What Dojo's media costs, estimated from public list prices (DESIGN section 3, brief D15).

Three things drive the Speech bill, and each is counted from what is stored:
- minutes of presenter video filmed: batch avatar synthesis is billed per second of video. Counted
  from the stored turn timings of every video still on the share (the end of its last turn);
- characters of narration synthesized: the approved text of every slide whose Listen audio exists,
  and of every turn in a filmed presenter video, because an avatar's voice is billed as neural text
  to speech on top of the video;
- minutes transcribed by the delivered-meaning check (`delivery`).

Unit prices come from the public Azure Retail Prices API for the Speech meters in the app's region,
fetched at most once a day and kept on the share. A meter that is not found is shown as "price not
found", never guessed. This is an estimate from list prices; the bill is in Azure Cost Management.
There are no budgets or alerts here: those are the owner's decision.

Before the owner adds an exam (app/newexam.py), the same prices, with the models' token meters, give
an estimate of what building its course will cost, and before filming, what filming will cost. A
model's meters are looked up by its name without the pinned version and by the deployment type it
runs on (infra/main.bicep, modelSku). A model with no known public meter is named and left out of the
total ("no public list price for <model>; not included"), never guessed. Studio shows the models'
list prices from the same lookup, but Dojo does not count model tokens yet, so its total is Speech only.

Deeper Listen (ADR 0007) has its own line in Studio: how many lessons play it, what it has cost so far
and what it costs for every lesson. Its writing and checking are priced from the rounds each lesson
took and the tokens assumed per round; its speech and transcription are counted like the rest."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import timedelta
from typing import Any

import httpx

from . import items as exam_items
from .core import iso, parse_iso, utcnow

log = logging.getLogger("dojo.costs")

PRICES_URL = "https://prices.azure.com/api/retail/prices"
PRICES_VERSION = 2           # the shape of the price list kept on the share; another shape is fetched again
LABEL = "Estimate from list prices; your bill is in Azure Cost Management."
NOT_FOUND = "price not found"
NO_LIST_PRICE = "no public list price for {model}; not included"
UNREAD = "the price list could not be read; not included"
CACHE_HOURS = 24
RETRY_AFTER_FAILURE_S = 3600
USAGE_CACHE_S = 600
MAX_PAGES = 5
# key -> (meterName in the Retail Prices API, what is measured)
METERS: dict[str, tuple[str, str]] = {
    "video": ("TTS Standard Avatar Batch Speech", "second"),
    "voice": ("S1 Neural Text To Speech Characters", "character"),
    "transcription": ("Fast Transcription Speech To Text", "second"),
}
ROLES = ("author", "gate", "grader")
# The deployment types a model can be deployed with (infra/main.bicep, modelSku), as the pages name them.
DEPLOYMENT = "GlobalStandard"
DEPLOYMENTS = {"GlobalStandard": "Global Standard", "DataZoneStandard": "Data Zone Standard"}
# Model (lower case, without the version) -> deployment type -> (input token meter, output token meter,
# the product they belong to). All are in the service "Foundry Models" of the Retail Prices API. gpt-5.5
# is priced by context length: Dojo's prompts are short, so its short-context meters count here. A
# model or deployment type that is not in this table has no known public list price: it is named and
# left out of the total, never guessed.
MODEL_METERS: dict[str, dict[str, tuple[str, str, str]]] = {
    "gpt-5.5": {
        "GlobalStandard": ("5.5 ShortCo inp Gl 1M Tokens", "5.5 ShortCo opt Gl 1M Tokens", "Azure OpenAI GPT5"),
        "DataZoneStandard": ("5.5 ShortCo inp Dz 1M Tokens", "5.5 ShortCo opt Dz 1M Tokens", "Azure OpenAI GPT5"),
    },
    "grok-4-1-fast-reasoning": {
        "GlobalStandard": ("Grok 4.1 Inp Glbl Tokens", "Grok 4.1 Outp Glbl Tokens", "Azure Grok Models"),
        "DataZoneStandard": ("Grok 4.1 Inp DZone Tokens", "Grok 4.1 Outp DZone Tokens", "Azure Grok Models"),
    },
    "deepseek-v4-pro": {
        "GlobalStandard": ("V4 Pro Inp glbl Tokens", "V4 Pro Outp glbl Tokens", "Azure Deepseek Models"),
        "DataZoneStandard": ("V4 Pro Inp DZ Tokens", "V4 Pro Outp DZ Tokens", "Azure Deepseek Models"),
    },
}
LESSON_ROUNDS = 1.5          # a lesson is written again when too much of it fails the check
# What building a course for an added exam takes for each skill: (key, what, the role that makes the
# call, input tokens, output tokens, calls), the tokens from the size of the prompts. Output includes
# the models' reasoning, which is billed as output. Dojo does not count model tokens yet, so these are
# assumptions, shown with the estimate.
COURSE_CALLS = (
    ("pick", "Picking official pages", "author", 900, 800, 1.0),    # its share of picking pages for a group of skills
    ("verify", "Checking the picked pages", "gate", 3500, 1000, 1.0),   # reads up to three page excerpts for the skill
    # A lesson on the deeper template (ADR 0008): up to three page excerpts in, about 20 points, 5 questions
    # and 8 slides out, so about twice the template-1 lesson's output, with the author's reasoning.
    ("write", "Writing the lessons", "author", 10000, 10000, LESSON_ROUNDS),
    ("check", "Checking the lessons", "gate", 31000, 9000, LESSON_ROUNDS),   # every sentence, in parts that each see the pages
)
NARRATION_CHARS = 3000       # characters narrated per lesson, until three lessons have been narrated
SPEAKING_CPS = 14.0          # characters a presenter says per second, until a minute of video is filmed
# Deeper Listen (ADR 0007), per round of one lesson: (key, what, role, input tokens, output tokens). The
# author reads the lesson and its sources and writes about 1,000 words; the checker reads the sources
# with every line. Assumptions, like COURSE_CALLS.
DEEP_CALLS = (
    ("deep_write", "Writing the conversations", "author", 10_000, 6_000),
    ("deep_gate", "Checking every line", "gate", 23_000, 7_000),
)
DEEP_ROUNDS = 1.5            # parts that do not hold are written and checked again
# Practice-exam items (ADR 0012), per item of each kind: (author input, author output, gate input, gate output,
# questions in the item). The author's input is the skill's page excerpts and the prompt; its output the item
# with a quote for every part of the key, and its reasoning. Assumptions, like COURSE_CALLS.
ITEM_CALLS = {
    "single": (2200, 1000, 2600, 500, 1),
    "multi": (2300, 1400, 2800, 700, 1),
    "sequence": (2300, 1400, 2800, 700, 1),
    "match": (2300, 1600, 3000, 800, 1),
    "hotarea": (2300, 1600, 3000, 800, 1),
    "yesno": (2500, 2500, 3500, 1200, 3),
    "case": (2500, 6000, 6500, 2000, 4),
}
ITEM_ROUNDS = 1.25           # an item the check drops is written again in a later batch
DEEP_CHARS = 5800            # characters of Deeper Listen spoken per lesson, until three lessons are spoken
# (key, what it is, meter, unit shown)
LINES = (
    ("video", "Presenter video filmed", "video", "minutes"),
    ("listen_voice", "Narration synthesized for Listen", "voice", "characters"),
    ("deep_voice", "Narration synthesized for Deeper Listen", "voice", "characters"),
    ("watch_voice", "Presenter voices in the filmed videos", "voice", "characters"),
    ("transcription", "Transcribed by the voice check", "transcription", "minutes"),
)
NOTES = (
    "DP-800 labs: about $23/month outside the Speech total: subscription-policy Defender for Cloud (SqlServers Standard) about $15 per new SQL server, private endpoint about $7.30, and private DNS zone about $0.50. The Azure SQL database is $0 if eligible for the free offer with AutoPause at its limit. Check your Azure bill and free-offer eligibility before deployment.",
    "Counts what is stored now: a video removed to stay inside the video storage budget is no longer counted, although it was paid for.",
    "Model calls (writing and checking lessons, questions and feedback) are not in this total: Dojo does not count model tokens yet. The models' list prices are shown above.",
    "Dojo sets no budget and no alert; that is your decision in Azure.",
)
_UNIT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:([KkMm])(?![A-Za-z]))?\s*/?\s*([A-Za-z][A-Za-z ]*?)?\s*$")
_TIME = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_REGION = re.compile(r"^[a-z0-9]{2,40}$")


def model_name(configured: Any) -> str:
    """A configured model without the version it is pinned to: 'gpt-5.5@2026-04-24' is 'gpt-5.5'."""
    return str(configured or "").split("@", 1)[0].strip()


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def model_meters() -> list[str]:
    return [name for deployments in MODEL_METERS.values() for inp, outp, _ in deployments.values() for name in (inp, outp)]


def retail_filter(region: str) -> str:
    wanted = [name for name, _ in METERS.values()] + model_meters()
    names = " or ".join(f"meterName eq '{name}'" for name in wanted)
    return f"armRegionName eq '{region}' and priceType eq 'Consumption' and ({names})"


def parse_unit(unit: str) -> tuple[float, str] | None:
    """'1 Minute' is (1, 'minute'), '1 Hour' is (1, 'hour'), '1M' is (1000000, 'unit'), '1/Hour' is (1, 'hour')."""
    m = _UNIT.match(str(unit or ""))
    if not m:
        return None
    quantity = float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[(m.group(2) or "").lower()]
    name = (m.group(3) or "").strip().lower()
    name = name[:-1] if name.endswith("s") else name
    return (quantity, name or "unit") if quantity > 0 else None


def per_measure(price: float, unit: str, measure: str) -> float | None:
    """The price of one second (time meters), one character (text meters) or one token (model meters),
    or None when the unit does not fit what is measured."""
    parsed = parse_unit(unit)
    if not parsed:
        return None
    quantity, name = parsed
    if measure == "second":
        return price / (quantity * _TIME[name]) if name in _TIME else None
    if measure in ("character", "token"):
        return price / quantity if name in ("unit", measure) else None
    return None


def _first_tier(item: dict) -> bool:
    try:
        return float(item.get("tierMinimumUnits") or 0) == 0
    except (TypeError, ValueError):
        return False


def pick(items: list[dict], meter_name: str, region: str, product: str | None = None) -> dict | None:
    """The list price of one meter: pay-as-you-go, the first tier, the latest effective date."""
    found = [i for i in items if isinstance(i, dict) and i.get("meterName") == meter_name
             and (product is None or i.get("productName") == product)
             and i.get("armRegionName") == region and i.get("type", "Consumption") == "Consumption"
             and _first_tier(i) and isinstance(i.get("retailPrice"), (int, float)) and not isinstance(i.get("retailPrice"), bool)]
    found.sort(key=lambda i: (str(i.get("effectiveStartDate") or ""), bool(i.get("isPrimaryMeterRegion"))), reverse=True)
    return found[0] if found else None


def parse_prices(data: list[dict], region: str) -> dict[str, dict | None]:
    """What each meter costs, from Retail Prices API items; None for a meter that is not there."""
    out: dict[str, dict | None] = {}
    for key, (name, measure) in METERS.items():
        item = pick(data, name, region)
        per = per_measure(float(item["retailPrice"]), item.get("unitOfMeasure", ""), measure) if item else None
        out[key] = None if per is None else {
            "meter": name, "product": item.get("productName"), "sku": item.get("skuName"),
            "service": item.get("serviceName"), "meter_id": item.get("meterId"), "price": float(item["retailPrice"]),
            "unit": item.get("unitOfMeasure"), "currency": item.get("currencyCode"),
            "effective": item.get("effectiveStartDate"), "per_" + measure: per}
    return out


def _token_price(item: dict | None) -> dict | None:
    per = per_measure(float(item["retailPrice"]), item.get("unitOfMeasure", ""), "token") if item else None
    return None if per is None else {
        "meter": item.get("meterName"), "meter_id": item.get("meterId"), "price": float(item["retailPrice"]),
        "unit": item.get("unitOfMeasure"), "effective": item.get("effectiveStartDate"),
        "per_token": per, "per_million": round(per * 1_000_000, 6)}


def parse_models(data: list[dict], region: str) -> dict[str, dict[str, dict | None]]:
    """What each known model's input and output tokens cost, per deployment type; None where its two
    meters are not both on the list."""
    out: dict[str, dict[str, dict | None]] = {}
    for model, deployments in MODEL_METERS.items():
        out[model] = {}
        for deployment, (inp, outp, product) in deployments.items():
            a, b = pick(data, inp, region, product), pick(data, outp, region, product)
            price_in, price_out = _token_price(a), _token_price(b)
            out[model][deployment] = None if price_in is None or price_out is None else {
                "product": product, "service": a.get("serviceName"), "currency": a.get("currencyCode"),
                "input": price_in, "output": price_out}
    return out


def left_out(lines: list[dict]) -> list[str]:
    """What a total leaves out: the lines without a price, grouped by why."""
    why: dict[str, list[str]] = {}
    for line in lines:
        if not line["found"]:
            why.setdefault(line["note"] or NOT_FOUND, []).append(line["what"])
    return [f"{whats[0] if len(whats) == 1 else ', '.join(whats[:-1]) + ' and ' + whats[-1]}: {note}"
            for note, whats in why.items()]


class Costs:
    """Studio's cost estimates: reads list prices from the public Azure Retail Prices API (cached), counts
    what Dojo used, and prices it. An estimate only; the bill is in Azure Cost Management."""
    def __init__(self, dojo: Any, check: Any, region: str, http: httpx.Client | None = None, deployment: str = DEPLOYMENT):
        self.dojo = dojo
        self.check = check
        self.region = region if _REGION.match(region or "") else "swedencentral"
        self.deployment = str(deployment or "").strip() or DEPLOYMENT
        self.http = http or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), headers={"User-Agent": "dojo"})
        self._lock = threading.Lock()
        self._failed_at: float | None = None
        self._usage: tuple[float, dict] | None = None
        self.deep_progress: Any = None   # set at startup: how far Deeper Listen is (Preparer.deep_status)

    # ------------------------------------------------------------ prices, fetched at most once a day

    def _ours(self, doc: Any) -> bool:
        """A price list in this version's shape, for this region and these meters."""
        return (isinstance(doc, dict) and doc.get("version") == PRICES_VERSION and doc.get("region") == self.region
                and doc.get("filter") == retail_filter(self.region)
                and isinstance(doc.get("meters"), dict) and isinstance(doc.get("models"), dict))

    def _fresh(self, doc: Any) -> bool:
        try:
            return self._ours(doc) and utcnow() - parse_iso(doc["fetched"]) < timedelta(hours=CACHE_HOURS)
        except (KeyError, TypeError, ValueError):
            return False

    def _fetch(self) -> list[dict]:
        url: str | None = PRICES_URL
        params: dict | None = {"$filter": retail_filter(self.region)}
        items: list[dict] = []
        for _ in range(MAX_PAGES):
            r = self.http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            items += [i for i in data.get("Items") or [] if isinstance(i, dict)]
            url, params = data.get("NextPageLink"), None
            if not (isinstance(url, str) and url.startswith(PRICES_URL)):
                break
        return items

    def prices(self) -> dict:
        cached = self.dojo.store.read("content", "prices", default=None)
        if self._fresh(cached):
            return cached
        with self._lock:
            cached = self.dojo.store.read("content", "prices", default=None)
            if self._fresh(cached):
                return cached
            old = cached if self._ours(cached) else None
            if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER_FAILURE_S:
                return {**(old or self._nothing()), "stale": True}
            try:
                items = self._fetch()
            except (httpx.HTTPError, ValueError) as e:
                self._failed_at = time.monotonic()
                log.warning("retail prices could not be fetched: %s", type(e).__name__)
                return {**(old or self._nothing()), "stale": True}
            self._failed_at = None
            doc = {"version": PRICES_VERSION, "fetched": iso(), "region": self.region, "source": PRICES_URL,
                   "filter": retail_filter(self.region), "meters": parse_prices(items, self.region),
                   "models": parse_models(items, self.region)}
            self.dojo.store.write("content", "prices", value=doc)
            log.info("retail prices fetched: %d of %d meters and %d of %d model prices found",
                     sum(1 for v in doc["meters"].values() if v), len(METERS),
                     sum(1 for d in doc["models"].values() for v in d.values() if v), sum(len(d) for d in MODEL_METERS.values()))
            return doc

    def _nothing(self) -> dict:
        return {"version": PRICES_VERSION, "fetched": None, "region": self.region, "source": PRICES_URL,
                "filter": retail_filter(self.region), "meters": {key: None for key in METERS},
                "models": {model: {d: None for d in deployments} for model, deployments in MODEL_METERS.items()}}

    def model_price(self, prices: dict, role: str) -> dict:
        """The list price of the model a role runs on, for the deployment type Dojo deploys it with; the
        honest note when there is none."""
        model = model_name(self.dojo.models(role)[role])
        known = MODEL_METERS.get(model.lower(), {}).get(self.deployment)
        per_model = (prices.get("models") or {}).get(model.lower()) if known else None
        entry = per_model.get(self.deployment) if isinstance(per_model, dict) else None
        found = isinstance(entry, dict)
        note = None if found else UNREAD if known and not prices.get("fetched") else NO_LIST_PRICE.format(model=model)
        return {"role": role, "model": model, "deployment": self.deployment, "found": found,
                "product": entry.get("product") if found else None,
                "input": entry.get("input") if found else None, "output": entry.get("output") if found else None,
                "meters": list(known[:2]) if known else [], "note": note}

    # ------------------------------------------------------------ what was made

    def usage(self) -> dict:
        if self._usage and time.monotonic() - self._usage[0] < USAGE_CACHE_S:
            return self._usage[1]
        folder = self.dojo.store.path("media")
        stored = set(os.listdir(folder)) if folder.is_dir() else set()   # one listing: the share is slow per file
        filmed = {name[:-len(".webm")] for name in stored if name.endswith(".webm")}
        video_s = 0.0
        for media_id in filmed:
            spans = self.dojo.avatar.timings(media_id) or []
            video_s += max((float(s.get("end") or 0) for s in spans if isinstance(s, dict)), default=0.0)
        listen_chars = watch_chars = narrated = 0
        heard: set[str] = set()
        seen_video: set[str] = set()
        for meta in self.dojo.lesson_index():
            plan = self.check.plan(meta["id"])
            narrated += any(a + ".mp3" in stored for a in plan["audio"])
            for media_id, chars in zip(plan["audio"], plan["chars"]):
                if media_id + ".mp3" in stored and media_id not in heard:
                    listen_chars += chars
                    heard.add(media_id)
            for speaker, media_id in plan["films"].items():
                if media_id in filmed and media_id not in seen_video:
                    watch_chars += sum(len(t) for turn in plan["turns"][speaker] for t in turn)
                    seen_video.add(media_id)
        deep_chars = deep_files = deep_lessons = whole_chars = 0
        for doc in self.dojo.deep_docs():   # replaced ones too: their audio was paid for
            parts = [(media_id, sum(len(str(x.get("text") or "").strip()) for x in seg.get("lines") or []))
                     for seg, media_id in zip(doc.get("deep_narration") or [], self.dojo._deep_media(doc))]
            there = [(m, c) for m, c in parts if m + ".mp3" in stored]
            if parts and len(there) == len(parts):
                deep_lessons += 1
                whole_chars += sum(c for _, c in parts)
            for media_id, chars in there:
                if media_id not in heard:
                    deep_chars += chars
                    deep_files += 1
                    heard.add(media_id)
        seconds = self.check.seconds()
        value = {"video_seconds": round(video_s, 1), "videos": len(filmed), "listen_characters": listen_chars,
                 "narrated_lessons": narrated, "audio_files": len(heard) - deep_files, "watch_characters": watch_chars,
                 "deep_characters": deep_chars, "deep_audio_files": deep_files, "deep_lessons": deep_lessons,
                 "deep_characters_per_lesson": round(whole_chars / deep_lessons) if deep_lessons else None,
                 "transcribed_seconds": round(sum(seconds.values()), 1), "transcribed": seconds}
        self._usage = (time.monotonic(), value)
        return value

    # ------------------------------------------------------------ the estimate

    def view(self) -> dict:
        prices, usage = self.prices(), self.usage()
        amounts = {"video": usage["video_seconds"], "listen_voice": usage["listen_characters"],
                   "deep_voice": usage["deep_characters"], "watch_voice": usage["watch_characters"],
                   "transcription": usage["transcribed_seconds"]}
        lines, total, complete = [], 0.0, True
        for key, what, meter_key, unit in LINES:
            meter = (prices.get("meters") or {}).get(meter_key)
            measure = METERS[meter_key][1]
            amount = amounts[key]
            quantity = round(amount / 60, 1) if unit == "minutes" else int(amount)
            cost = round(amount * meter["per_" + measure], 4) if meter else None
            if cost is None:
                complete = False
            else:
                total += cost
            lines.append({"key": key, "what": what, "quantity": quantity, "unit": unit,
                          "meter": meter["meter"] if meter else METERS[meter_key][0],
                          "price": meter["price"] if meter else None, "price_unit": meter["unit"] if meter else None,
                          "currency": meter.get("currency") if meter else None,
                          "cost": cost, "found": bool(meter), "note": None if meter else NOT_FOUND})
        return {"label": LABEL, "region": self.region, "currency": "USD", "fetched": prices.get("fetched"),
                "stale": bool(prices.get("stale")), "source": PRICES_URL, "lines": lines,
                "total": round(total, 2), "complete": complete, "usage": usage,
                "models": [self.model_price(prices, role) for role in ROLES], "notes": list(NOTES),
                "deep": self.deep_view(prices, usage), "exam_items": self.items_view(prices)}

    # ------------------------------------------------------------ practice-exam items (ADR 0012)

    def _item_lines(self, prices: dict, kind: str, questions: float) -> list[dict]:
        """Writing and checking `questions` questions of one kind, ITEM_ROUNDS times over."""
        ain, aout, gin, gout, size = ITEM_CALLS[kind]
        units = questions / size * ITEM_ROUNDS
        return [self._model_line(prices, f"{kind}_write", "Writing", "author", ain, aout, units),
                self._model_line(prices, f"{kind}_check", "Checking", "gate", gin, gout, units)]

    def items_view(self, prices: dict | None = None) -> dict:
        """What 100 practice-exam questions cost at list prices: all single choice, in the exam mix, and
        each kind alone. The background pool's daily limit caps what it spends whatever the mix."""
        from .exam import POOL_DAILY_BATCHES, POOL_MEMBER_DAILY_BATCHES, POOL_MEMBERS_DAILY_BATCHES   # noqa: PLC0415 - the exam room imports nothing from here
        prices = self.prices() if prices is None else prices
        kinds = {}
        for kind in ITEM_CALLS:
            total, complete = self._summed(self._item_lines(prices, kind, 100))
            kinds[kind] = {"label": exam_items.LABELS[kind], "per_100": total if complete else None}
        hundred = exam_items.mix(100)
        lines = [line for kind, n in hundred.items() if n for line in
                 self._item_lines(prices, kind, n * exam_items.UNIT_SIZE.get(kind, 1))]
        exam_total, complete = self._summed(lines)
        single = kinds["single"]["per_100"]
        models = self.dojo.models("author", "gate")
        return {
            "label": LABEL, "currency": "USD", "fetched": prices.get("fetched"),
            "kinds": kinds, "mix_100": hundred, "mix_full": exam_items.mix(50), "pool_mix": dict(exam_items.POOL_MIX),
            "single_per_100": single, "exam_per_100": exam_total if complete else None,
            "extra_per_100": round(exam_total - single, 2) if complete and single is not None else None,
            "left_out": left_out(list({(x["what"], x["note"]): x for x in lines if not x["found"]}.values())),
            "assumptions": [
                f"The author ({model_name(models['author'])}) writes each item with a quote for every part of its key; "
                f"the checker ({model_name(models['gate'])}) checks every part.",
                f"Each item is written and checked {ITEM_ROUNDS:g} times on average, because the check drops some.",
                "Tokens per item, from the size of Dojo's prompts; a yes/no series is 3 questions and a case study 4. "
                "These are assumptions: Dojo does not count model tokens yet.",
                f"The background pool still spends at most {POOL_DAILY_BATCHES} batches a day, whatever the mix."
                + (f" In team mode each member's pool spends at most {POOL_MEMBER_DAILY_BATCHES} and all members' "
                   f"pools together at most {POOL_MEMBERS_DAILY_BATCHES}." if getattr(getattr(self.dojo, "settings", None), "team", False) else ""),
                "Every input token is priced in full. Input the model has cached is cheaper, so the bill can be lower.",
            ],
        }

    # ------------------------------------------------------------ Deeper Listen (ADR 0007)

    def _deep_lines(self, prices: dict, rounds: float, chars: float, seconds: float) -> list[dict]:
        """Writing and checking `rounds` times, speaking `chars` characters and transcribing `seconds`."""
        meters = prices.get("meters") or {}
        voice, stt = meters.get("voice"), meters.get("transcription")
        note = NOT_FOUND if prices.get("fetched") else UNREAD
        lines = [self._model_line(prices, key, what, role, tokens_in, tokens_out, rounds)
                 for key, what, role, tokens_in, tokens_out in DEEP_CALLS]
        lines.append(self._line("deep_voice", "Speaking them", round(chars), "characters",
                                chars * voice["per_character"] if voice else None, note))
        lines.append(self._line("deep_transcription", "Transcribed by the voice check", round(seconds / 60, 1), "minutes",
                                seconds * stt["per_second"] if stt else None, note))
        return lines

    def _deep_chars(self, usage: dict) -> tuple[float, bool]:
        """Characters of Deeper Listen per lesson: the average of the lessons spoken, once there are three."""
        measured = usage["deep_lessons"] >= 3
        return (float(usage["deep_characters_per_lesson"]) if measured else float(DEEP_CHARS)), measured

    def deep_view(self, prices: dict | None = None, usage: dict | None = None) -> dict:
        """Deeper Listen: how many lessons play it, what it has cost so far and what it costs for every
        lesson, at list prices. So far counts every round started, as Dojo.make_deep records it before the
        models are called: of attempts that did not pass, stopped on an error or were replaced too. Then the
        characters spoken and the minutes the voice check transcribed."""
        prices = self.prices() if prices is None else prices
        usage = self.usage() if usage is None else usage
        progress = dict(self.deep_progress()) if self.deep_progress is not None else {}
        lessons = _count(progress.get("lessons"))
        docs = self.dojo.deep_docs()
        rounds = sum(_count(d.get("spent_rounds")) for d in docs)
        chars, measured = self._deep_chars(usage)
        so_far = self._deep_lines(prices, rounds, usage["deep_characters"], float(usage["transcribed"].get("deep") or 0))
        every = self._deep_lines(prices, DEEP_ROUNDS * lessons, chars * lessons, chars / SPEAKING_CPS * lessons)
        spent, spent_complete = self._summed(so_far)
        total, complete = self._summed(every)
        models = self.dojo.models("author", "gate")
        per_round = ", ".join(f"{what.lower()} {tokens_in:,} input and {tokens_out:,} output"
                              for _, what, _, tokens_in, tokens_out in DEEP_CALLS)
        return {
            **progress, "lessons": lessons, "ready": _count(progress.get("ready")),
            "label": LABEL, "currency": "USD", "fetched": prices.get("fetched"), "stale": bool(prices.get("stale")),
            "so_far": {"total": spent, "complete": spent_complete, "rounds": rounds, "conversations": len(docs),
                       "lines": so_far, "left_out": left_out(so_far)},
            "every_lesson": {"lessons": lessons, "total": total, "complete": complete,
                             "per_lesson": round(total / lessons, 2) if lessons else None,
                             "lines": every, "left_out": left_out(every)},
            "assumptions": [
                f"Each round, the author ({model_name(models['author'])}) writes the conversation and the checker "
                f"({model_name(models['gate'])}) checks every line. Tokens for each round: {per_round}. These are "
                "assumptions: Dojo does not count model tokens yet.",
                f"So far, {rounds} rounds for {len(docs)} conversations, counted when each round starts: also those "
                "that did not pass, stopped on an error or were replaced.",
                f"For every lesson, {DEEP_ROUNDS:g} rounds each on average, because parts that do not hold are written "
                "and checked again.",
                f"About {round(chars):,} characters spoken per lesson"
                + (", the average of the lessons spoken so far." if measured else ", until three lessons are spoken."),
                "The voice check transcribes each lesson's Deeper Listen once.",
                "Every input token is priced in full. Input the model has cached is cheaper, so the bill can be lower.",
            ],
        }

    def deep_line(self) -> dict:
        """Deeper Listen for /api/diag, priced from the price list already kept: diag never fetches prices."""
        cached = self.dojo.store.read("content", "prices", default=None)
        view = self.deep_view(cached if self._ours(cached) else self._nothing())
        keep = {k: v for k, v in view.items() if k not in ("label", "so_far", "every_lesson", "assumptions")}
        return {**keep, "so_far": view["so_far"]["total"], "rounds": view["so_far"]["rounds"],
                "every_lesson": view["every_lesson"]["total"], "per_lesson": view["every_lesson"]["per_lesson"],
                "complete": view["so_far"]["complete"] and view["every_lesson"]["complete"]}

    # ------------------------------------------------------------ what an added exam will cost

    @staticmethod
    def _line(key: str, what: str, quantity: float, unit: str, cost: float | None, note: str = NOT_FOUND) -> dict:
        return {"key": key, "what": what, "quantity": quantity, "unit": unit,
                "cost": None if cost is None else round(cost, 2), "found": cost is not None,
                "note": None if cost is not None else note}

    def _model_line(self, prices: dict, key: str, what: str, role: str, tokens_in: int, tokens_out: int,
                    calls: float) -> dict:
        """Calls one role makes `calls` times: its input and output tokens at its model's list prices, or
        the honest note when the model has none."""
        m = self.model_price(prices, role)
        tin, tout = round(calls * tokens_in), round(calls * tokens_out)
        found = m["found"]
        cost = tin * m["input"]["per_token"] + tout * m["output"]["per_token"] if found else None
        return {**self._line(key, what, tin + tout, "tokens", cost, m["note"]),
                "role": role, "model": m["model"], "deployment": m["deployment"],
                "input_tokens": tin, "output_tokens": tout,
                "per_million": {"input": m["input"]["per_million"], "output": m["output"]["per_million"]} if found else None}

    @staticmethod
    def _summed(lines: list[dict]) -> tuple[float, bool]:
        found = [line["cost"] for line in lines if line["cost"] is not None]
        return round(sum(found), 2), len(found) == len(lines)

    @staticmethod
    def _narration(usage: dict) -> tuple[float, bool]:
        """Characters narrated per lesson: the average so far once three lessons are narrated."""
        measured = usage["narrated_lessons"] >= 3
        return (usage["listen_characters"] / usage["narrated_lessons"] if measured else NARRATION_CHARS), measured

    def _narration_line(self, prices: dict, what: str, chars: float) -> dict:
        voice = (prices.get("meters") or {}).get("voice")
        return self._line("narration", what, round(chars), "characters", round(chars) * voice["per_character"] if voice else None,
                          NOT_FOUND if prices.get("fetched") else UNREAD)

    def course_estimate(self, skills: int) -> dict:
        """What building the course for an added exam with this many skills should cost at list prices:
        finding and checking official pages, writing and checking one lesson per skill, and narrating it.
        All of it runs on its own once the owner confirms. Filming is priced apart (film_estimate)."""
        prices, usage = self.prices(), self.usage()
        chars, measured = self._narration(usage)
        lines = [self._model_line(prices, key, what, role, tokens_in, tokens_out, skills * calls)
                 for key, what, role, tokens_in, tokens_out, calls in COURSE_CALLS]
        lines.append(self._narration_line(prices, "Narrating the lessons for Listen", skills * chars))
        total, complete = self._summed(lines)
        models = self.dojo.models("author", "gate")
        deployment = DEPLOYMENTS.get(self.deployment, self.deployment)
        per_call = ", ".join(f"{what.lower()} {tokens_in:,} input and {tokens_out:,} output"
                             for _, what, _, tokens_in, tokens_out, _ in COURSE_CALLS)
        deep_chars, _ = self._deep_chars(usage)
        deep, deep_complete = self._summed(self._deep_lines(prices, DEEP_ROUNDS * skills, deep_chars * skills,
                                                            deep_chars / SPEAKING_CPS * skills))
        return {
            "label": LABEL, "currency": "USD", "skills": skills, "lines": lines, "total": total, "complete": complete,
            "left_out": left_out(lines), "fetched": prices.get("fetched"), "stale": bool(prices.get("stale")),
            "assumptions": [
                f"One lesson for each of the {skills} skills. A skill with no verified official page gets no lesson.",
                f"The author ({model_name(models['author'])}) picks the pages and writes the lessons; the checker "
                f"({model_name(models['gate'])}) checks both.",
                f"Tokens for each skill and call, from the size of Dojo's prompts: {per_call}. These are "
                "assumptions: Dojo does not count model tokens yet.",
                "The models' reasoning is billed as output tokens, so it is counted in the output.",
                f"A lesson is written {LESSON_ROUNDS:g} times on average, because a draft that fails the check is "
                f"written again, so writing and checking count {LESSON_ROUNDS:g} times.",
                f"List prices for {deployment} deployments in {self.region}. Where a model is priced by context "
                "length, its short-context price counts: Dojo's prompts are short.",
                "Every input token is priced in full. Input the model has cached is cheaper, so the bill can be lower.",
                f"About {round(chars):,} characters of narration per lesson"
                + (", the average of the lessons narrated so far." if measured else ", until three lessons are narrated."),
                "Practice questions are made in the background within the daily limit all exams share, so they are "
                "not part of this estimate.",
                "Deeper Listen is not part of this estimate either: it is written for each lesson later, in the "
                "background, within its daily limit"
                + (f": about ${deep:,.2f} more for {skills} lessons at list price." if deep_complete
                   else ". Studio shows what it costs."),
            ],
        }

    def deeper_estimate(self, searches: int, lessons: int, narrated: int | None = None, deep_again: int = 0) -> dict:
        """What deeper lessons (ADR 0008) cost at list prices: searching for more official pages for
        `searches` skills, writing and checking `lessons` lessons again on the deeper template, and
        narrating `narrated` of them (all of them when not given), with the voice check that transcribes
        each narration once. Used both for what is left to do and, from the work counted so far, for what
        was spent: Dojo does not count model tokens, so both are estimates from the same assumptions.
        `deep_again` lessons play a Deeper Listen (ADR 0007) that their new lesson needs written again; a
        lesson without one gets it once either way, so only those are counted here."""
        prices, usage = self.prices(), self.usage()
        chars, measured = self._narration(usage)
        narrated = lessons if narrated is None else narrated
        calls = {"pick": searches, "verify": searches, "write": lessons * LESSON_ROUNDS, "check": lessons * LESSON_ROUNDS}
        lines = [self._model_line(prices, key, what, role, tokens_in, tokens_out, calls[key])
                 for key, what, role, tokens_in, tokens_out, _ in COURSE_CALLS]
        lines.append(self._narration_line(prices, "Narrating the new lessons for Listen", narrated * chars))
        # The voice check (app/delivery.py) transcribes each new lesson's narration once.
        stt, heard = (prices.get("meters") or {}).get("transcription"), narrated * chars / SPEAKING_CPS
        lines.append(self._line("narration_check", "Transcribed by the voice check", round(heard / 60, 1), "minutes",
                                heard * stt["per_second"] if stt else None, NOT_FOUND if prices.get("fetched") else UNREAD))
        deep_chars, _ = self._deep_chars(usage)
        if deep_again:
            deep, whole = self._summed(self._deep_lines(prices, DEEP_ROUNDS * deep_again, deep_chars * deep_again,
                                                        deep_chars / SPEAKING_CPS * deep_again))
            lines.append(self._line("deep_again", "Deeper Listen written again for lessons that play one", deep_again,
                                    "lesson" if deep_again == 1 else "lessons", deep if whole else None,
                                    NOT_FOUND if prices.get("fetched") else UNREAD))
        total, complete = self._summed(lines)
        models = self.dojo.models("author", "gate")
        deployment = DEPLOYMENTS.get(self.deployment, self.deployment)
        per_call = ", ".join(f"{what.lower()} {tokens_in:,} input and {tokens_out:,} output"
                             for _, what, _, tokens_in, tokens_out, _ in COURSE_CALLS)
        return {
            "label": LABEL, "currency": "USD", "searches": searches, "lessons": lessons, "narrated": narrated,
            "deep_again": deep_again, "lines": lines, "total": total, "complete": complete, "left_out": left_out(lines),
            "fetched": prices.get("fetched"), "stale": bool(prices.get("stale")),
            "assumptions": [
                f"{searches} skills searched for more official pages, and {lessons} lessons written again on the deeper "
                "template. A search that finds nothing new costs the same.",
                f"The author ({model_name(models['author'])}) picks the pages and writes the lessons; the checker "
                f"({model_name(models['gate'])}) checks both.",
                f"Tokens for each skill and call, from the size of Dojo's prompts: {per_call}. These are "
                "assumptions: Dojo does not count model tokens yet.",
                "The models' reasoning is billed as output tokens, so it is counted in the output.",
                f"A lesson is written {LESSON_ROUNDS:g} times on average, because a draft that fails the check is "
                f"written again, so writing and checking count {LESSON_ROUNDS:g} times. An attempt that fails costs "
                "the same and is counted too.",
                f"List prices for {deployment} deployments in {self.region}. Where a model is priced by context "
                "length, its short-context price counts: Dojo's prompts are short.",
                "Every input token is priced in full. Input the model has cached is cheaper, so the bill can be lower.",
                f"About {round(chars):,} characters of narration per lesson"
                + (", the average of the lessons narrated so far." if measured else ", until three lessons are narrated."),
                f"The voice check transcribes each new lesson's narration once, about {chars / SPEAKING_CPS / 60:.1f} "
                f"minutes a lesson at {SPEAKING_CPS:g} characters a second.",
                "Deeper Listen (ADR 0007) is written for each new lesson by its own pass and is priced on its own "
                "line. A lesson that plays one now needs it written again once it is replaced"
                + (f": {deep_again} such {'lesson' if deep_again == 1 else 'lessons'}, at about {round(deep_chars):,} "
                   f"characters and {DEEP_ROUNDS:g} rounds each, as on that line, are counted here." if deep_again
                   else "; no such lesson is waiting now."),
                "A presenter video is never filmed again on its own: Studio's Film again button shows its price first.",
            ],
        }

    def film_estimate(self, plans: list[dict]) -> dict:
        """What filming these lessons' presenter videos should cost at list prices: the seconds of video and
        the presenters' voices, counted the way `view` counts them once they are made."""
        prices, usage = self.prices(), self.usage()
        meters = prices.get("meters") or {}
        measured = usage["video_seconds"] >= 60 and usage["watch_characters"] > 0
        cps = usage["watch_characters"] / usage["video_seconds"] if measured else SPEAKING_CPS
        seen: set[str] = set()
        chars = 0
        for plan in plans:
            for speaker, media_id in plan["films"].items():
                if media_id not in seen:
                    seen.add(media_id)
                    chars += sum(len(t) for turn in plan["turns"][speaker] for t in turn)
        seconds = chars / cps
        video, voice = meters.get("video"), meters.get("voice")
        note = NOT_FOUND if prices.get("fetched") else UNREAD
        lines = [
            self._line("video", "Presenter video filmed", round(seconds / 60, 1), "minutes",
                       seconds * video["per_second"] if video else None, note),
            self._line("voice", "Presenter voices", chars, "characters", chars * voice["per_character"] if voice else None, note),
        ]
        total, complete = self._summed(lines)
        return {
            "label": LABEL, "currency": "USD", "lessons": len(plans), "videos": len(seen), "lines": lines,
            "total": total, "complete": complete, "left_out": left_out(lines),
            "fetched": prices.get("fetched"), "stale": bool(prices.get("stale")),
            "assumptions": [f"About {cps:.0f} characters spoken per second of video"
                            + (", measured from the videos filmed so far." if measured else ", until a minute of video is filmed.")],
        }
