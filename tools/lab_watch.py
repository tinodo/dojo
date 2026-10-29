"""The daily check of the lab database (ADR 0028): online, on the tier the template declares, with room, and not overloaded.

.github/workflows/lab-watch.yml runs this after it signed in to Azure as the deploy identity. It reads the database's state and its metrics from
Azure Resource Manager and prints numbers and plain words only. The Actions log of a public repository is public, so never a resource name, an id
or a message of Azure (the same rule as tools/public_log.py). It writes nothing to Azure.

    python tools/lab_watch.py

Exit 0: all is well. Exit 1: something needs the owner; the log says what. Exit 2: Azure could not be read, which is not "all is well".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARM = "https://management.azure.com"
SQL_API = "2023-08-01"
METRICS_API = "2018-01-01"
BICEP = Path(__file__).resolve().parent.parent / "infra" / "main.bicep"

WINDOW_HOURS = 24
STORAGE_MAX_PERCENT = 80      # of what the tier holds
LOAD_MAX_PERCENT = 50         # the DTU percentage, averaged over the window
FAILED_MAX = 25               # failed connections in the window: this many or more needs the owner
METRICS = ("dtu_consumption_percent", "storage_percent", "connection_successful", "connection_failed")

GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
GROUP = re.compile(r"[A-Za-z0-9._()-]{1,90}")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,23}")   # a state or a tier name: the only text of Azure that is ever printed


class Unreadable(Exception):
    """Azure could not be read. The text is safe to print: a plain phrase and at most an HTTP status."""


def token() -> str:
    try:
        out = subprocess.run([shutil.which("az") or "az", "account", "get-access-token", "--resource", ARM, "--query", "accessToken",
                              "--output", "tsv"], capture_output=True, text=True, timeout=120, check=False, shell=os.name == "nt")
    except (OSError, subprocess.SubprocessError):
        raise Unreadable("there is no Azure token (az did not run)") from None
    if out.returncode or not out.stdout.strip():
        raise Unreadable(f"there is no Azure token (az exit {out.returncode})")
    return out.stdout.strip()


def get(url: str, tok: str, tries: int = 3) -> dict:
    """One GET to Resource Manager, tried again for a throttle or a hiccup. Whatever goes wrong, only a status leaves this function."""
    for attempt in range(tries):
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise Unreadable(f"Azure answered HTTP {e.code}") from None
        except ValueError:
            raise Unreadable("Azure's answer could not be read") from None
        except OSError:
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise Unreadable("Azure did not answer") from None
    raise Unreadable("Azure did not answer")


def declared_sku(bicep: str) -> str | None:
    """The tier the template gives the lab database."""
    block = re.search(r"(?ms)^resource labDatabase [^\n]*\{\n(.*?)^\}", bicep)
    found = re.search(r"sku:\s*\{[^}]*?name:\s*'([^']+)'", block.group(1)) if block else None
    return found.group(1) if found else None


def find_database(tok: str, sub: str, group: str) -> tuple[dict | None, str | None]:
    """(the lab database as Azure reports it, None) or (None, why not). There is one SQL server in the group and one database on it besides master."""
    scope = f"/subscriptions/{sub}/resourceGroups/{group}".lower()
    servers = get(f"{ARM}{scope}/providers/Microsoft.Sql/servers?api-version={SQL_API}", tok).get("value") or []
    if len(servers) != 1:
        return None, f"expected one SQL server in the resource group, found {len(servers)}"
    server_id = str(servers[0].get("id") or "")
    if not server_id.lower().startswith(scope + "/"):
        raise Unreadable("Azure named a server outside the resource group")
    databases = [d for d in get(f"{ARM}{server_id}/databases?api-version={SQL_API}", tok).get("value") or []
                 if str(d.get("name") or "").lower() != "master"]
    if len(databases) != 1:
        return None, f"expected one lab database on the server, found {len(databases)}"
    return databases[0], None


def read_metrics(tok: str, database_id: str, now: datetime) -> dict:
    query = urllib.parse.urlencode({
        "api-version": METRICS_API, "metricnames": ",".join(METRICS), "aggregation": "Average,Maximum,Total", "interval": "PT1H",
        "timespan": f"{now - timedelta(hours=WINDOW_HOURS):%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}"})
    return get(f"{ARM}{database_id}/providers/Microsoft.Insights/metrics?{query}", tok)


def numbers(payload: dict) -> dict:
    """The few numbers the judgement needs, from the hourly points of the four metrics. None where Azure reported no point."""
    points: dict[str, list[dict]] = {name: [] for name in METRICS}
    for metric in payload.get("value") or []:
        name = (metric.get("name") or {}).get("value")
        if name in points:
            points[name] += [pt for series in metric.get("timeseries") or [] for pt in series.get("data") or []]

    def column(name: str, key: str) -> list[float]:
        return [float(pt[key]) for pt in points[name] if isinstance(pt.get(key), (int, float))]

    load, storage = column("dtu_consumption_percent", "average"), column("storage_percent", "maximum")
    peak, ok, failed = column("dtu_consumption_percent", "maximum"), column("connection_successful", "total"), column("connection_failed", "total")
    return {"load_average": sum(load) / len(load) if load else None, "load_peak": max(peak) if peak else None, "hours": len(load),
            "storage_peak": max(storage) if storage else None,
            "ok": int(sum(ok)) if ok else None, "failed": int(sum(failed)) if failed else None}


def word(value: object) -> str:
    return value if isinstance(value, str) and WORD.fullmatch(value) else "unknown"


def judge(database: dict, declared: str | None, n: dict | None, why_no_metrics: str | None = None) -> tuple[list[str], list[str], list[str]]:
    """(the lines of the report, what needs the owner, what could not be read). `n` is None when the metrics could not be read."""
    props, sku = database.get("properties") or {}, database.get("sku") or {}
    state, tier, units = word(props.get("status")), word(sku.get("name")), sku.get("capacity")
    findings: list[str] = []
    gaps: list[str] = []
    lines = [f"Lab database, last {WINDOW_HOURS} hours."]
    lines.append(f"State: {state}. Tier: {tier}" + (f" with {int(units)} DTUs" if isinstance(units, (int, float)) and units == int(units) else "")
                 + (f"; the template declares {word(declared)}." if declared else "."))
    if state != "Online":
        findings.append(f"The database is {state}, not Online.")
    if declared is None:
        gaps.append("The tier in infra/main.bicep could not be read.")
    elif tier != declared:
        findings.append(f"The database is on the {tier} tier, but the template declares {word(declared)}.")
    if n is None:
        gaps.append(f"The storage, the load and the connections could not be read ({why_no_metrics or 'no answer'}).")
        return lines, findings, gaps
    size = props.get("maxSizeBytes")
    room = f" of the {size / 2 ** 30:g} GB" if isinstance(size, (int, float)) and size > 0 else ""
    if n["storage_peak"] is None:
        gaps.append("Azure gave no storage reading.")
    else:
        lines.append(f"Storage: at most {n['storage_peak']:.1f}%{room}.")
        if n["storage_peak"] > STORAGE_MAX_PERCENT:
            findings.append(f"Storage reached {n['storage_peak']:.1f}%, above {STORAGE_MAX_PERCENT}%.")
    if n["load_average"] is None:
        gaps.append("Azure gave no load reading.")
    else:
        lines.append(f"Load: {n['load_average']:.1f}% of the DTUs on average, {n['load_peak']:.1f}% at most ({n['hours']} hourly readings).")
        if n["load_average"] > LOAD_MAX_PERCENT:
            findings.append(f"The load averaged {n['load_average']:.1f}%, above {LOAD_MAX_PERCENT}%.")
    # Azure sends no point for a count that did not happen, so a missing connection reading is zero, not a gap.
    failed = n["failed"] or 0
    lines.append(f"Connections: {n['ok'] or 0} succeeded, {failed} failed.")
    if failed >= FAILED_MAX:
        findings.append(f"{failed} connections failed, {FAILED_MAX} or more.")
    return lines, findings, gaps


def run(env: dict, now: datetime | None = None, bicep: Path = BICEP) -> tuple[list[str], int]:
    """The whole check. Returns the lines to print and the exit status."""
    sub, group = env.get("AZURE_SUBSCRIPTION_ID", ""), env.get("RESOURCE_GROUP", "")
    if not GUID.fullmatch(sub) or not GROUP.fullmatch(group):
        return ["Could not check the lab database: the subscription or the resource group is not set."], 2
    try:
        tok = token()
        database, why = find_database(tok, sub, group)
    except Unreadable as e:
        return [f"Could not read the lab database: {e}."], 2
    if database is None:
        return [f"The lab database was not found: {why}."], 1
    n, why_no_metrics = None, None
    try:
        n = numbers(read_metrics(tok, str(database.get("id") or ""), now or datetime.now(timezone.utc)))
    except Unreadable as e:
        why_no_metrics = str(e)
    try:
        declared = declared_sku(bicep.read_text("utf-8"))
    except OSError:
        declared = None
    lines, findings, gaps = judge(database, declared, n, why_no_metrics)
    if findings:
        return lines + ["Needs the owner: " + " ".join(findings)] + (["Also not read: " + " ".join(gaps)] if gaps else []), 1
    if gaps:
        return lines + ["Could not judge: " + " ".join(gaps)], 2
    return lines + ["All well."], 0


def main() -> int:
    try:
        lines, status = run(os.environ)
    except Exception as e:  # noqa: BLE001 - a log of a public repository must never carry a traceback with values
        lines, status = [f"The lab check failed unexpectedly ({type(e).__name__})."], 2
    print("\n".join(lines))
    if status and os.environ.get("GITHUB_ACTIONS") == "true":
        said = next((line for line in lines if line.startswith(("Needs the owner", "Could not", "The lab database was not found", "The lab check failed"))), lines[-1])
        print("::error title=Lab database::" + said)
    return status


if __name__ == "__main__":
    sys.exit(main())
