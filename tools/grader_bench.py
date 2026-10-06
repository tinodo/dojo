"""The grader bench: how often Dojo's grader gets a rubric point right, measured on answers whose right result is
known. Each case file in tests/data/grader_bench/ holds one item as Dojo's item writer wrote it, and answers
written for it with the expected result for every rubric point. The bench judges each answer with today's
judging rules and the real models (Dojo._judge, exactly as an answer is judged in the app) and compares.

It calls the models, so it costs money (about 1 to 2 US cents an answer) and needs the same settings as a local
run against real models:

  DOJO_LOCAL=1 DOJO_LOCAL_AI=real DOJO_TENANT_ID=... DOJO_AI_ENDPOINT=... DOJO_MODELS='{"author":...,"gate":...,"grader":...}'
  python tools/grader_bench.py [--only TEXT] [--repeat N] [--workers N] [--out results.json] [--min-agreement 0.9]

It exits 1 when nothing was judged, when a judgement failed, or below --min-agreement.
The official pages are read as they are today; the sha256 of each page used is kept in the results.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "data" / "grader_bench"


def load_cases(folder: Path = CASES) -> list[dict]:
    """Every case file, checked: each answer expects a result for exactly the item's rubric points."""
    cases = []
    for path in sorted(folder.glob("*.json")):
        case = json.loads(path.read_text("utf-8"))
        item = case["item"]
        ids = [r["id"] for r in item["rubric"]]
        if len(set(ids)) != len(ids) or not ids:
            raise ValueError(f"{path.name}: rubric ids must be unique and present")
        seen = set()
        for a in case["answers"]:
            if a["id"] in seen:
                raise ValueError(f"{path.name}: answer id {a['id']} twice")
            seen.add(a["id"])
            if set(a["expect"]) != set(ids):
                raise ValueError(f"{path.name}/{a['id']}: expect must name exactly {ids}")
            if not isinstance(a.get("text"), str) or not a["text"].strip():
                raise ValueError(f"{path.name}/{a['id']}: no answer text")
            if not all(isinstance(v, bool) for v in a["expect"].values()):
                raise ValueError(f"{path.name}/{a['id']}: every expectation is true or false")
        cases.append({**case, "file": path.name})
    return cases


def compare(expect: dict[str, bool], result: dict) -> list[dict]:
    """Per rubric point: what was expected, what counted, and how a difference errs. A point counts as met only
    when it is met and its quote was verified, as in the app."""
    got = {p["id"]: bool(p.get("met") and p.get("verified")) for p in result.get("points") or []}
    out = []
    for pid, want in expect.items():
        g = got.get(pid, False)
        out.append({"id": pid, "expect": want, "got": g,
                    "verdict": "agree" if g == want else ("too_generous" if g else "too_harsh")})
    return out


def summarize(runs: list[dict]) -> dict:
    """Agreement over every judged point, the two ways to disagree, and how often the referee decided a point
    (and changed the grader's verdict). With --repeat, `unstable` counts the answers whose judgements differ."""
    points = [p for r in runs if "points" in r for p in r["points"]]
    n = len(points)
    count = {k: sum(1 for p in points if p["verdict"] == k) for k in ("agree", "too_generous", "too_harsh")}
    answers = [r for r in runs if "points" in r]
    refereed = [j for r in answers for j in r.get("judged", []) if j.get("refereed")]
    outcomes: dict[tuple[str, str], set] = {}
    for r in answers:
        outcomes.setdefault((r["case"], r["answer"]), set()).add(tuple(p["got"] for p in r["points"]))
    return {"answers": len(answers), "failed": sum(1 for r in runs if "error" in r), "points": n, **count,
            "agreement": round(count["agree"] / n, 3) if n else None,
            "answers_fully_right": sum(1 for r in answers if all(p["verdict"] == "agree" for p in r["points"])),
            "refereed": len(refereed),
            "referee_changed": sum(1 for j in refereed if j["refereed"]["grader_met"] != bool(j["met"] and j["verified"])),
            "unstable": sum(1 for v in outcomes.values() if len(v) > 1)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="only cases whose file or answer id contains this text")
    ap.add_argument("--repeat", type=int, default=1, help="judge each answer this many times (stability)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="", help="write every judgement and comparison to this JSON file")
    ap.add_argument("--min-agreement", type=float, default=0.0, help="exit 1 below this agreement (0 to 1)")
    args = ap.parse_args(argv)
    if os.environ.get("DOJO_LOCAL_AI") != "real" or not os.environ.get("DOJO_AI_ENDPOINT") or not os.environ.get("DOJO_MODELS"):
        print("Set DOJO_LOCAL=1, DOJO_LOCAL_AI=real, DOJO_TENANT_ID, DOJO_AI_ENDPOINT and DOJO_MODELS first (see the docstring).")
        return 2
    os.environ.setdefault("DOJO_LOCAL", "1")
    os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-bench-"))
    sys.path.insert(0, str(ROOT))
    from app import ai  # noqa: PLC0415
    from app.main import create_app  # noqa: PLC0415

    usage: list[dict] = []
    lock = threading.Lock()
    post = ai.Foundry._post

    def metered(self: Any, url: str, scope: str, body: dict) -> Any:
        r = post(self, url, scope, body)
        try:
            u = r.json().get("usage") or {}
        except ValueError:
            u = {}
        with lock:
            usage.append({"model": body.get("model"), "task": ai.task_of(body["messages"][0]["content"]), **u})
        return r

    ai.Foundry._post = metered
    app = create_app()
    dojo = app.state.dojo
    cases = load_cases()
    pages: dict[str, str] = {}
    for case in cases:
        for ref in case["item"]["sources"]:
            if ref["url"] not in pages:
                pages[ref["url"]] = dojo.sources.get(ref["url"])["sha256"]

    jobs = [(case, a, n) for case in cases for a in case["answers"] for n in range(args.repeat)
            if not args.only or args.only in case["file"] or args.only in a["id"]]

    def run(job: tuple[dict, dict, int]) -> dict:
        case, a, n = job
        it = {**case["item"], "id": f"bench-{case['file']}-{a['id']}-{n}",
              "answer": {"text": a["text"], "input": a.get("input", "typed")}}
        started = time.time()
        try:
            j = dojo._judge(lambda _step: None, it)
        except Exception as e:  # noqa: BLE001 - a failed call is reported, not fatal
            return {"case": case["file"], "answer": a["id"], "run": n, "error": f"{type(e).__name__}: {e}"}
        res = j["result"]
        return {"case": case["file"], "answer": a["id"], "run": n, "seconds": round(time.time() - started, 1),
                "met": res["met"], "total": res["total"], "points": compare(a["expect"], res),
                "judged": [{"id": p["id"], "met": p["met"], "verified": p["verified"], "quote": p["learner_quote"],
                            "why": p["why"], **({"refereed": p["refereed"]} if p.get("refereed") else {})} for p in res["points"]],
                "feedback": res["feedback"], "withheld": res["withheld_reason"], "unshown": j["unshown"],
                "bad_quotes": j["bad"], "version": res["version"], "models": res["models"]}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        runs = list(pool.map(run, jobs))
    summary = summarize(runs)
    tokens = {k: sum(u.get(k) or 0 for u in usage) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    for r in runs:
        if "error" in r:
            print(f"ERROR  {r['case']} {r['answer']}#{r['run']}: {r['error'][:200]}")
            continue
        wrong = [f"{p['id']}:{p['verdict']}" for p in r["points"] if p["verdict"] != "agree"]
        ref = [f"{j['id']}:refereed" for j in r["judged"] if j.get("refereed")]
        print(f"{'ok   ' if not wrong else 'WRONG'}  {r['case']} {r['answer']}#{r['run']}  {r['met']}/{r['total']}  {' '.join(wrong + ref)}")
    print(json.dumps({"summary": summary, "calls": len(usage), "tokens": tokens}, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "tokens": tokens, "calls": usage, "pages": pages,
                                              "runs": runs}, ensure_ascii=False, indent=1), "utf-8")
    # An incomplete measurement is no measurement: nothing judged, or a judgement that failed, exits 1 too.
    if summary["agreement"] is None or summary["failed"]:
        return 1
    return 1 if summary["agreement"] < args.min_agreement else 0


if __name__ == "__main__":
    raise SystemExit(main())
