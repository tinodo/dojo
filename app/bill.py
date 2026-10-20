"""What Azure charged for Dojo's resource group, for Studio's Money tab (docs/adr/0025-studio-v2.md).

It is read from the Cost Management query API with the app's own identity. That needs the built-in role Cost Management Reader on the
resource group, which the owner grants once (infra/bootstrap/app-roles.bicep): the deploy pipeline cannot assign roles. Nothing here
writes to Azure.

The reading is kept on the share (`content/bill.json`), so a visit after a restart shows the last one while a new one is read in the
background. Azure's cost data is 8 to 24 hours behind and the API throttles hard, so a reading is kept six hours, one is read at a
time, and an answer of "wait" is remembered across restarts. The browser has no way to force a reading. What is returned is only
normalised numbers and plain words: never an Azure URL, a continuation link or an error text of Azure's."""
from __future__ import annotations

import calendar
import logging
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from .core import iso, parse_iso, utcnow

log = logging.getLogger("dojo.bill")

API = "https://management.azure.com"
VERSION = "2023-11-01"
TOKEN_SCOPE = f"{API}/.default"
FRESH_S = 6 * 3600           # a reading is shown as it is this long; then a new one is read in the background
DENIED_RETRY_S = 300         # without the role Dojo asks again this often, so granting it shows up within minutes
FAILED_RETRY_S = 900         # after a failure Dojo waits this long
WAIT_MIN_S, WAIT_DEFAULT_S, WAIT_MAX_S = 30, 600, 3600   # what Azure's "ask again later" may make Dojo wait
MAX_PAGES = 20               # a reading that is not complete after this many pages is not shown as a total
KEPT = "bill"
SHAPE = 1                    # the shape of the kept reading; another shape is read again
QUIET_DAYS_MIN = 5           # a "quietest day" needs at least this many complete days to mean anything
MONEY = re.compile(r"^[A-Z]{3}$")

# What the bill's lines pay for, in the owner's words. The first rule that matches a line (its service and its meter) decides; a line no
# rule knows is "Everything else", so the groups always add up to the total. `kind`: "use" is charged for what is used, "always" is
# charged for as long as the resource exists, whether or not anyone uses Dojo.
GROUPS = {
    "ai": ("AI models", "The language models that write lessons, check them, answer questions and grade answers. Charged by how much text goes in and comes out."),
    "voices": ("Voices and presenter videos", "The presenters in the films, the narration you listen to, and the check of what the voices said."),
    "defender": ("Security scanning (Microsoft Defender for Cloud)",
                 "Turned on for your whole Azure subscription, not by Dojo. You can review it in Defender for Cloud, under Environment settings; keeping it is your Azure decision."),
    "web": ("The web app", "The server Dojo runs on."),
    "network": ("Private link to the database", "Keeps the database off the public internet."),
    "search": ("Web search for finding sources", "Used when Dojo looks for official pages."),
    "database": ("Database", "The lab database. On the free offer it costs nothing and pauses when nobody uses labs."),
    "other": ("Everything else", "Bandwidth, DNS and anything Dojo has no name for."),
}
RULES = [   # (service, meter or None, group, part label or None, kind)
    (r"foundry models|azure openai|openai", None, "ai", None, "use"),
    (r"foundry tools|cognitive services|speech", r"avatar", "voices", "Presenter videos", "use"),
    (r"foundry tools|cognitive services|speech", r"text to speech|neural", "voices", "Narration voices", "use"),
    (r"foundry tools|cognitive services|speech", r"speech to text|transcription", "voices", "Checking what was said", "use"),
    (r"foundry tools|cognitive services|speech", None, "voices", "Other voice services", "use"),
    (r"defender", r"token", "defender", "Billed by how much text goes to the AI", "use"),
    (r"defender", None, "defender", "Billed per server", "always"),
    (r"app service", None, "web", None, "always"),
    (r"virtual network|private link|private endpoint", None, "network", None, "always"),
    (r"dns", r"zone", "network", "Private DNS zone", "always"),
    (r"bing", None, "search", None, "use"),
    (r"\bsql\b", None, "database", None, "use"),
]
MODELS = [(r"\b5\.5\b|gpt", "gpt-5.5"), (r"grok", "Grok"), (r"v4 pro|deepseek", "DeepSeek"), (r"claude", "Claude")]

DENIED = "Dojo has no read access to Azure Cost Management yet."
DENIED_WHY = ("The app's identity needs the built-in role “Cost Management Reader” on the resource group{rg}. It is a one-time step, "
              "and it can take a few minutes to take effect.")
NOT_IN_AZURE = "The bill is only read when Dojo runs in Azure, not in this copy."
NO_GROUP = "Dojo does not know which resource group it runs in, so it cannot read its bill."
EMPTY = "Azure has not reported any cost for this month yet. It reports with a delay of usually 8 to 24 hours."
WAITING = "Azure asked Dojo to wait before asking again."
FAILED = "Azure did not answer."
MIXED = "Azure reported more than one currency, so no total is shown."
INCOMPLETE = "Azure returned more lines than Dojo reads, so no total is shown."


def _day(raw: Any) -> str | None:
    """Azure writes a usage day as a number such as 20261019, or as a date text."""
    text = str(raw or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    m = re.match(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else None


def classify(service: str, meter: str) -> tuple[str, str | None, str]:
    """(group, part label, kind) of one line of the bill."""
    service, meter = (service or "").lower(), (meter or "").lower()
    for svc, met, group, part, kind in RULES:
        if re.search(svc, service) and (met is None or re.search(met, meter)):
            if group == "ai":
                part = next((label for pattern, label in MODELS if re.search(pattern, meter)), "Other models")
            return group, part, kind
    return "other", None, "use"


def summarize(rows: list[dict], previous: dict | None = None, link: str | None = None, budget_link: str | None = None) -> dict:
    """The Money tab's numbers from the lines of this month: per group and per day, what is always on, the quietest and the busiest days.
    `rows` have `day` (YYYY-MM-DD), `service`, `meter`, `cost` and `currency`."""
    currencies = {r["currency"] for r in rows if r.get("currency")}
    if len(currencies) > 1:
        return {"state": "error", "reason": MIXED}
    currency = next(iter(currencies), "USD")
    days: dict[str, float] = {}
    groups: dict[str, dict] = {}
    for r in rows:
        group, part, kind = classify(r["service"], r["meter"])
        g = groups.setdefault(group, {"total": 0.0, "parts": {}})
        g["total"] += r["cost"]
        label = part or GROUPS[group][0]
        p = g["parts"].setdefault(label, {"total": 0.0, "kind": kind})
        p["total"] += r["cost"]
        days[r["day"]] = days.get(r["day"], 0.0) + r["cost"]
    last = max(days)
    first = date.fromisoformat(last[:8] + "01")
    through = date.fromisoformat(last)
    span = (through - first).days + 1
    series = [{"day": (first + timedelta(days=i)).isoformat(), "total": round(days.get((first + timedelta(days=i)).isoformat(), 0.0), 2),
               "partial": (first + timedelta(days=i)) == through} for i in range(span)]
    total = round(sum(g["total"] for g in groups.values()), 2)
    out_groups = []
    for key, g in groups.items():
        parts = [{"label": label, "total": round(p["total"], 2), "kind": p["kind"]} for label, p in
                 sorted(g["parts"].items(), key=lambda kv: -kv[1]["total"])]
        if len(parts) == 1 and parts[0]["label"] == GROUPS[key][0]:
            parts = []   # a group of one line has nothing to break down
        kinds = {p["kind"] for p in g["parts"].values() if abs(p["total"]) >= 0.005} or {next(iter(g["parts"].values()))["kind"]}
        kind = "mixed" if len(kinds) > 1 else next(iter(kinds))
        if key == "database" and abs(g["total"]) < 0.005:
            kind = "free"
        out_groups.append({"key": key, "label": GROUPS[key][0], "note": GROUPS[key][1], "total": round(g["total"], 2), "kind": kind, "parts": parts})
    out_groups.sort(key=lambda g: (g["key"] == "other", -g["total"]))
    always = sum(p["total"] for g in groups.values() for p in g["parts"].values() if p["kind"] == "always")
    in_month = calendar.monthrange(first.year, first.month)[1]
    complete = [d for d in series if not d["partial"] and d["total"] > 0]
    quiet = min(complete, key=lambda d: d["total"]) if len(complete) >= QUIET_DAYS_MIN else None
    busiest = sorted(complete, key=lambda d: -d["total"])[:3]
    return {"state": "ok", "reason": None, "currency": currency,
            "month": {"id": first.isoformat()[:7], "from": first.isoformat(), "to": last, "days": span, "total": total},
            "groups": out_groups, "days": series,
            "fixed": {"total": round(always, 2), "per_day": round(always / span, 2), "per_month": round(always / span * in_month, 2)},
            # what the month costs at the least: what is spent, and the always-on lines for the days to come
            "month_floor": round(total + max(0, in_month - span) * always / span, 2),
            "quiet_day": {"day": quiet["day"], "total": quiet["total"], "per_month": round(quiet["total"] * in_month, 2)} if quiet else None,
            "busiest": [{"day": d["day"], "total": d["total"]} for d in busiest],
            "previous": previous, "link": link, "budget_link": budget_link}


class Bill:
    """The cost of the resource group this month, read from Azure and kept."""

    def __init__(self, store: Any, tokens: Any, resource_id: str | None, http: httpx.Client | None = None):
        self.store, self.tokens = store, tokens
        match = re.match(r"^/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([^/]+)(?:/|$)", resource_id or "")
        self.scope = f"/subscriptions/{match.group(1)}/resourceGroups/{match.group(2)}" if match else None
        self.group = match.group(2) if match else None
        self.http = http or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), headers={"User-Agent": "dojo"})
        self._flight = threading.Lock()   # one reading at a time: a page that is open never makes two

    # ------------------------------------------------------------ what the page asks

    def view(self) -> dict:
        """The last reading, or why there is none. Never waits for Azure unless nothing has ever been read."""
        if self.tokens is None:
            return {"state": "not_connected", "reason": NOT_IN_AZURE, "detail": None}
        if self.scope is None:
            return {"state": "not_connected", "reason": NO_GROUP, "detail": None}
        doc = self._load()
        now = utcnow()
        if self._due(doc, now):
            if (doc or {}).get("summary"):
                threading.Thread(target=self.refresh, name="bill", daemon=True).start()
            else:
                self.refresh(wait=True)   # nothing has ever been read: this visit waits for the first reading
            doc = self._load()
        return self._present(doc or {}, now)

    def warm(self, delay: float = 100.0) -> None:
        """Read the bill a little after startup when the last reading is old, so the first visit to Money does not wait for Azure."""
        def run() -> None:
            threading.Event().wait(delay)
            self.refresh()

        if self.tokens is not None and self.scope is not None:
            threading.Thread(target=run, name="bill-warm", daemon=True).start()

    def refresh(self, wait: bool = False) -> None:
        """Read the cost again, unless another reading is under way (`wait` waits for it, and then finds it fresh) or Azure said to wait."""
        acquired = self._flight.acquire(timeout=45) if wait else self._flight.acquire(blocking=False)
        if not acquired:
            return
        try:
            doc = self._load() or {}
            if self._due(doc, utcnow()):
                self._read(doc)
        except Exception:  # noqa: BLE001 - the Money tab is a page of numbers, never a reason to fail
            log.exception("the bill could not be read")
        finally:
            self._flight.release()

    # ------------------------------------------------------------ what is kept

    def _load(self) -> dict | None:
        doc = self.store.read("content", KEPT, default=None)
        return doc if isinstance(doc, dict) and doc.get("shape") == SHAPE and doc.get("scope") == self.scope else None

    def _save(self, doc: dict) -> None:
        self.store.write("content", KEPT, value={**doc, "shape": SHAPE, "scope": self.scope})

    def _due(self, doc: dict | None, now: datetime) -> bool:
        if not doc:
            return True
        attempt = doc.get("attempt") or {}
        if attempt.get("retry_at") and parse_iso(attempt["retry_at"]) > now:
            return False   # Azure said to wait, or Dojo is not allowed yet: not before then, restarts included
        if attempt.get("state") in ("denied", "throttled", "failed"):
            return True
        if not doc.get("read_at") or not doc.get("summary"):
            return True
        month = now.date().isoformat()[:7]
        return parse_iso(doc["read_at"]) + timedelta(seconds=FRESH_S) <= now or doc.get("month") != month

    def _present(self, doc: dict, now: datetime) -> dict:
        attempt = doc.get("attempt") or {}
        summary = doc.get("summary")
        if attempt.get("state") == "denied":
            return {"state": "not_connected", "reason": DENIED, "detail": DENIED_WHY.format(rg=f" {self.group}" if self.group else ""),
                    "read_at": doc.get("read_at")}
        if not summary:
            return {"state": "error", "reason": attempt.get("reason") or FAILED, "detail": None, "read_at": None}
        out = dict(summary)
        out["read_at"] = doc.get("read_at")
        out["current_month"] = now.date().isoformat()[:7]   # the page says "last month" when the reading is of another
        stale = attempt.get("state") in ("throttled", "failed") or (
            bool(doc.get("read_at")) and parse_iso(doc["read_at"]) + timedelta(seconds=2 * FRESH_S) <= now)
        out["stale"] = stale
        out["stale_reason"] = attempt.get("reason") if stale else None
        if out["state"] == "ok" and doc.get("month") != now.date().isoformat()[:7]:
            out["stale"], out["stale_reason"] = True, out.get("stale_reason") or "This is last month's reading; this month's has not been read yet."
        return out

    # ------------------------------------------------------------ reading Azure

    def _query(self, timeframe: str, daily: bool) -> dict:
        dataset: dict = {"granularity": "Daily" if daily else "None", "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}}
        if daily:
            dataset["grouping"] = [{"type": "Dimension", "name": "ServiceName"}, {"type": "Dimension", "name": "Meter"}]
        return {"type": "ActualCost", "timeframe": timeframe, "dataset": dataset}

    def _post(self, url: str, body: dict, token: str) -> httpx.Response:
        return self.http.post(url, json=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    def _rows(self, body: dict, token: str) -> list[dict] | None:
        """Every line of a query as a dict, following Azure's continuation. None when it was not complete."""
        url = f"{API}{self.scope}/providers/Microsoft.CostManagement/query?api-version={VERSION}"
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            r = self._post(url, body, token)
            r.raise_for_status()
            props = (r.json() or {}).get("properties") or {}
            cols = [str(c.get("name")) for c in props.get("columns") or []]
            out += [dict(zip(cols, row)) for row in props.get("rows") or []]
            url = props.get("nextLink")
            if not url:
                return out
            if not str(url).startswith(f"{API}/"):
                return None   # Dojo's token goes to Azure's management endpoint only
        return None

    def _read(self, doc: dict) -> None:
        now = utcnow()
        try:
            token = self.tokens.get(TOKEN_SCOPE)
            lines = self._rows(self._query("MonthToDate", True), token)
            try:
                before = self._rows(self._query("TheLastMonth", False), token)
            except (httpx.HTTPError, ValueError):   # last month is a line beside the total: without it the reading is still this month's
                log.info("last month's cost could not be read")
                before = None
        except httpx.HTTPStatusError as e:
            return self._failed(doc, now, e.response.status_code, e.response.headers)
        except (httpx.HTTPError, ValueError):
            return self._failed(doc, now, None, {})
        except Exception:  # noqa: BLE001 - no token, or a library failure: nothing to say about the role
            log.warning("the bill could not be read: no token")
            return self._failed(doc, now, None, {})
        if lines is None:
            return self._kept(doc, now, {"state": "error", "reason": INCOMPLETE}, None)
        rows = []
        for r in lines:
            day = _day(r.get("UsageDate"))
            try:
                cost = float(r.get("Cost"))
            except (TypeError, ValueError):
                continue
            if day:
                rows.append({"day": day, "service": str(r.get("ServiceName") or ""), "meter": str(r.get("Meter") or ""), "cost": cost,
                             "currency": str(r.get("Currency") or "") if MONEY.match(str(r.get("Currency") or "")) else ""})
        month = now.date().isoformat()[:7]
        previous = None
        if before and len({str(r.get("Currency") or "") for r in before}) == 1:   # one currency, or no comparison
            total = sum(float(r.get("Cost") or 0) for r in before)
            if total > 0:
                last = date.fromisoformat(month + "-01") - timedelta(days=1)
                previous = {"month": last.isoformat()[:7], "total": round(total, 2), "currency": str(before[0].get("Currency") or "")}
        where = quote(self.scope or "", safe="")
        link = f"https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/costanalysis/scope/{where}"
        budgets = f"https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/budgets/scope/{where}"
        summary = summarize(rows, previous, link, budgets) if rows else {"state": "empty", "reason": EMPTY, "previous": previous, "link": link,
                                                                         "budget_link": budgets, "currency": previous["currency"] if previous else "USD"}
        self._kept(doc, now, summary, month)

    def _kept(self, doc: dict, now: datetime, summary: dict, month: str | None) -> None:
        keep = {"read_at": iso(now), "month": month or now.date().isoformat()[:7], "summary": summary,
                "attempt": {"at": iso(now), "state": "ok", "retry_at": None, "reason": None}}
        self._save(keep)

    def _failed(self, doc: dict, now: datetime, status: int | None, headers: Any) -> None:
        if status in (401, 403, 404):
            state, wait, reason = "denied", DENIED_RETRY_S, DENIED
        elif status == 429:
            state, wait, reason = "throttled", self._wait(headers), WAITING
        else:
            state, wait, reason = "failed", FAILED_RETRY_S, FAILED
        log.warning("the bill could not be read: %s", state)
        self._save({**doc, "attempt": {"at": iso(now), "state": state, "retry_at": iso(now + timedelta(seconds=wait)), "reason": reason}})

    @staticmethod
    def _wait(headers: Any) -> int:
        for name in ("Retry-After", "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
                     "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after", "x-ms-ratelimit-microsoft.costmanagement-client-retry-after"):
            try:
                return max(WAIT_MIN_S, min(WAIT_MAX_S, int(float(headers.get(name)))))
            except (TypeError, ValueError, AttributeError):
                continue
        return WAIT_DEFAULT_S
