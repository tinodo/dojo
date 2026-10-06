"""Local harness against the REAL models (for prompt work). Uses your Azure CLI sign-in for the tenant in
DOJO_TENANT_ID through AzureCliCredential, which only requests tokens and never changes the CLI context.

Set these first (your own resources; see docs/operations.md):
  DOJO_TENANT_ID        the tenant id
  DOJO_AI_ENDPOINT      https://<ai-account>.services.ai.azure.com/openai/v1
  DOJO_SPEECH_ENDPOINT  https://<ai-account>.cognitiveservices.azure.com
  DOJO_AI_RESOURCE_ID   /subscriptions/<subscription-id>/resourceGroups/<group>/providers/Microsoft.CognitiveServices/accounts/<ai-account>
Optional: DOJO_MODELS (JSON with author, gate and grader deployment names) and DOJO_DATA_DIR.

  python tools/dev_real.py selftest
  python tools/dev_real.py lesson gh-300 d1.g1.s1
  python tools/dev_real.py item gh-300 d2.g1.s1 probe
  python tools/dev_real.py answer <item-id> "my answer text"
  python tools/dev_real.py rehearsal gh-300 4
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ["DOJO_LOCAL"] = "1"
os.environ["DOJO_LOCAL_AI"] = "real"
REQUIRED = ("DOJO_TENANT_ID", "DOJO_AI_ENDPOINT", "DOJO_SPEECH_ENDPOINT", "DOJO_AI_RESOURCE_ID")
missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
if missing:
    sys.exit("dev_real.py needs these environment variables for your own Azure resources: "
             + ", ".join(missing) + ". See the top of tools/dev_real.py and docs/operations.md.")
os.environ.setdefault("DOJO_MODELS", '{"author":"gpt-5.5","gate":"grok-4-1-fast-reasoning","grader":"DeepSeek-V4-Pro"}')
os.environ.setdefault("DOJO_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", ".dojo-data", "real"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

H = {"X-Dojo": "1"}


def wait(c: TestClient, job: dict) -> dict:
    started = time.time()
    last = ""
    while job["state"] in ("queued", "running"):
        if job["step"] != last:
            print(f"  [{int(time.time() - started):>4}s] {job['step']}", flush=True)
            last = job["step"]
        time.sleep(2)
        job = c.get(f"/api/jobs/{job['id']}").json()
    print(f"  [{int(time.time() - started):>4}s] {job['state']} {job.get('error') or ''}", flush=True)
    return job


def show(data: object) -> None:
    print(json.dumps(data, indent=1, ensure_ascii=False)[:20000])


def main(argv: list[str]) -> None:
    c = TestClient(create_app())
    cmd = argv[0] if argv else "selftest"
    if cmd == "selftest":
        show(c.post("/api/diag/run", headers=H).json())
    elif cmd == "lesson":
        job = wait(c, c.post("/api/lessons", json={"package": argv[1], "skill": argv[2]}, headers=H).json())
        if job["state"] == "done":
            show(c.get(f"/api/lessons/{job['result']['lesson']}").json())
    elif cmd == "item":
        job = wait(c, c.post("/api/items", json={"package": argv[1], "skill": argv[2], "mode": argv[3]}, headers=H).json())
        if job["state"] == "done":
            item = job["result"]["item"]
            show(c.get(f"/api/items/{item}").json())
            print("ITEM", item)
    elif cmd == "answer":
        job = wait(c, c.post(f"/api/items/{argv[1]}/answer", json={"text": argv[2], "elapsed_s": 60}, headers=H).json())
        show(c.get(f"/api/items/{argv[1]}").json())
    elif cmd == "rehearsal":
        job = wait(c, c.post("/api/rehearsals", json={"package": argv[1], "count": int(argv[2])}, headers=H).json())
        if job["state"] == "done":
            show(c.get(f"/api/rehearsals/{job['result']['rehearsal']}").json())
    elif cmd == "quality":
        show(c.get("/api/quality").json()[:40])
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
