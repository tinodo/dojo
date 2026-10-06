"""What a deploy step may print to the Actions log: counts and error codes, never resource names, ids or messages.

The workflow logs of a public repository are public. Azure CLI errors quote resource paths (with the subscription
and generated server names), principal ids and policy names. So the deploy workflow keeps the output of each az
command in files and prints only this summary. The details stay where only the operator can read them: the
deployment's record in the resource group, the web app's deployment log, or the same command run locally
(docs/operations.md, "When a deploy fails").

    python tools/public_log.py preview|infra|app <exit-status> <stderr-file> [<what-if-json-file>]

Exits with the az command's status, so a failed step still fails the job.
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
from pathlib import Path

# Identifiers only, and only where Azure CLI puts error codes: the "code" field of ARM's JSON errors, azure-core's
# "(Code) message" and "Code: X" lines, and Bicep's BCPnnn diagnostics. No dashes or spaces, so neither a GUID nor
# a resource name such as rg-x or sql-x-abc can pass as a code.
_ID = r"([A-Za-z][A-Za-z0-9_.]{1,63})"
CODE_PATTERNS = (
    re.compile(r'"code"\s*:\s*"' + _ID + r'"'),
    re.compile(r"(?m)^(?:ERROR: )?\(" + _ID + r"\) "),
    re.compile(r"(?m)^\s*Code: " + _ID + r"\s*$"),
    re.compile(r"\b(BCP\d{3})\b"),
)
STEPS = {
    "preview": ("The preview (what-if)", "run the same what-if locally (docs/operations.md, \"When a deploy fails\")."),
    "infra": ("The infrastructure deployment",
              "the deployment record {deployment} in the app resource group: Azure portal, Deployments, or "
              "az deployment group show --resource-group <app-resource-group> --name {deployment} "
              "--query properties.error"),
    "app": ("The app deployment", "the web app's deployment log: Azure portal, the web app, Deployment Center, Logs."),
}


def deployment_name() -> str:
    """The name deploy.yml gives the infrastructure deployment."""
    return f"dojo-{os.environ.get('GITHUB_RUN_NUMBER') or '<run-number>'}"


def error_codes(text: str) -> list[str]:
    """Each error code once, in the order the output first mentions it."""
    found = sorted((m.start(1), m.group(1)) for p in CODE_PATTERNS for m in p.finditer(text))
    return list(dict.fromkeys(code for _, code in found))


def what_if_counts(text: str) -> str:
    changes = json.loads(text).get("changes") or []
    counts = collections.Counter(c.get("changeType", "?") for c in changes)
    return "What-if: " + (", ".join(f"{kind} {n}" for kind, n in sorted(counts.items())) or "no changes")


def summary(step: str, status: int, err: str, what_if: str | None = None) -> list[str]:
    name, details = STEPS[step]
    if status:
        codes = error_codes(err)
        return [f"{name} failed (exit {status}). Error codes: {', '.join(codes) if codes else 'none found'}.",
                "The messages are not printed: they name resources and ids, and this log is public.",
                "Details: " + details.format(deployment=deployment_name())]
    lines = [what_if_counts(what_if)] if what_if is not None else []
    warnings = sum(1 for line in err.splitlines() if line.strip())
    if warnings:
        lines.append(f"{name}: {warnings} line(s) of warnings, not printed (this log is public). "
                     "Bicep's: az bicep build --file infra/main.bicep.")
    return lines


def _read(path: str) -> str:
    p = Path(path)
    return p.read_text("utf-8", errors="replace") if p.is_file() else ""


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4) or argv[0] not in STEPS or not argv[1].isdigit():
        print("usage: public_log.py preview|infra|app <exit-status> <stderr-file> [<what-if-json-file>]")
        return 2
    step, status, err = argv[0], int(argv[1]), _read(argv[2])
    what_if = _read(argv[3]) if len(argv) == 4 and not status else None
    try:
        print("\n".join(summary(step, status, err, what_if)))
    except (ValueError, AttributeError):
        print("What-if: the result could not be read.")
        return status or 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
