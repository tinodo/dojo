"""Source discovery's measurement (ADR 0018): the locked sets in tests/data/sourcing/ against the real pipeline.

Modes (choose with --mode):
  verify     the real gate and author models judge every locked positive and negative page of the chosen skills,
             exactly as the build does (app/sourcing/verify.py). Accepted hard negatives must be 0.
  sets       the set-completeness cases: complete sets must say "complete", deliberately incomplete sets must not.
  retrieval  free, no model: how many locked positives Learn's search, Learn's MCP server and the inventory find.
  e2e        the production search (stage 0 of the preparer) on a temporary package of the locked skills with no
             sources: the pages it accepts are compared with the locked positives and negatives. Costs money.

It must be run, and every accepted negative explained, before any change to the verifier prompts, VERIFIER_VERSION, or
the gate or author deployment (like tools/grader_bench.py). verify, sets and e2e call the models and need the settings
of a local run against real models:

  DOJO_LOCAL=1 DOJO_LOCAL_AI=real DOJO_TENANT_ID=... DOJO_AI_ENDPOINT=... DOJO_MODELS='{"author":...,"gate":...,"grader":...}'
  python tools/source_eval.py --mode verify [--sets gold13,verified20] [--only TEXT] [--out results.json]

Exit code 1: an accepted negative, nothing judged, a page that could not be read, or a rate below the minimum."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data" / "sourcing"
HUBS = {"DP-300": ["https://learn.microsoft.com/en-us/azure/azure-sql/", "https://learn.microsoft.com/en-us/azure/azure-sql/database/"]}


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text("utf-8"))


def locked(sets: list[str]) -> tuple[dict, list[dict]]:
    """The exam and its locked skills of the given sets, from dp300.json."""
    data = load("dp300.json")
    return data["exam"], [s for s in data["skills"] if s["set"] in sets]


def context(exam: dict, s: dict) -> dict:
    return {"code": exam["code"], "title": exam["title"], "domain": s["domain"], "group": s["group"], "text": s["text"]}


def wanted(text: str, only: str) -> bool:
    """Whether a skill is chosen by --only: any of its comma-separated parts is in the text."""
    return not only or any(part.strip().lower() in text.lower() for part in only.split(",") if part.strip())


def title_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].replace("-", " ")


TOKENS: dict[str, list[int]] = {}   # model -> [calls, input tokens, output tokens], as the service reported them
LIST_PRICE = {"gpt-5.5": (5.0, 30.0), "grok-4-1-fast-reasoning": (0.2, 0.5)}   # USD per million tokens (app/costs.py)


def count_tokens() -> None:
    """Add up the usage the service reports for every model call of this run: the measured size of the work, which
    the cost estimate of app/costs.py (`SOURCING_CALLS`) is set from."""
    import threading  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    lock = threading.Lock()
    send = httpx.Client.send

    def counted(self, request, *args, **kwargs):
        response = send(self, request, *args, **kwargs)
        try:
            if (request.url.host or "").endswith(".services.ai.azure.com") and response.status_code == 200:
                body = response.json()
                usage = body.get("usage") or {}
                tokens_in = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
                # Some services (grok) count their reasoning outside completion_tokens: total_tokens holds it.
                tokens_out = max(int(usage.get("completion_tokens", usage.get("output_tokens", 0))), int(usage.get("total_tokens", 0)) - tokens_in)
                with lock:
                    row = TOKENS.setdefault(str(body.get("model") or "?").split("@")[0], [0, 0, 0])
                    row[0] += 1
                    row[1] += tokens_in
                    row[2] += tokens_out
        except Exception:  # noqa: BLE001 - counting must never change a run
            pass
        return response

    httpx.Client.send = counted


def token_report() -> dict:
    out = {}
    for model, (calls, tokens_in, tokens_out) in sorted(TOKENS.items()):
        price = next((v for k, v in LIST_PRICE.items() if model.startswith(k)), None)
        out[model] = {"calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
                      "usd_at_list_price": round(tokens_in * price[0] / 1e6 + tokens_out * price[1] / 1e6, 2) if price else None}
    return out


def setup(models: bool) -> object:
    if models and (os.environ.get("DOJO_LOCAL_AI") != "real" or not os.environ.get("DOJO_AI_ENDPOINT") or not os.environ.get("DOJO_MODELS")):
        print("Set DOJO_LOCAL=1, DOJO_LOCAL_AI=real, DOJO_TENANT_ID, DOJO_AI_ENDPOINT and DOJO_MODELS first (see the docstring).")
        raise SystemExit(2)
    os.environ.setdefault("DOJO_LOCAL", "1")
    os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-source-eval-"))
    sys.path.insert(0, str(ROOT))
    if models:
        # `az` can take longer than the 10 seconds the credential allows it by default on a busy machine.
        import azure.identity as identity  # noqa: PLC0415

        class PatientCliCredential(identity.AzureCliCredential):
            def __init__(self, *a, **k):
                k.setdefault("process_timeout", int(os.environ.get("SOURCE_EVAL_AZ_TIMEOUT", "120")))
                super().__init__(*a, **k)

        identity.AzureCliCredential = PatientCliCredential
    import logging  # noqa: PLC0415
    logging.disable(logging.INFO)
    from app.main import create_app  # noqa: PLC0415
    if models:
        count_tokens()
    return create_app()


def verify_mode(app, args) -> int:
    from app.sourcing.retrieve import canon_url  # noqa: PLC0415
    from app.sourcing.verify import Verifier  # noqa: PLC0415

    exam, skills = locked(args.sets.split(","))
    skills = [s for s in skills if wanted(s["text"], args.only)]
    verifier = Verifier(app.state.dojo)
    rows = []
    for s in skills:
        cands = [{"url": canon_url(p["url"]), "title": title_of(p["url"]), "label": "P", "locked": p} for p in s["positives"]]
        cands += [{"url": canon_url(n["url"]), "title": title_of(n["url"]), "label": "N", "locked": n} for n in s["negatives"]]
        results = verifier.grade_pages(context(exam, s), cands)
        for c, r in zip(cands, results):
            rows.append({"skill": s["text"], "label": c["label"], "url": c["url"], "state": r["state"], "grade": r["grade"], "why": r["why"],
                         "kind": c["locked"].get("kind"), "locked_grade": c["locked"].get("grade"), "third": c["locked"].get("third")})
        print(f"done: {s['text'][:70]}")
    return report_verify(rows, args)


def report_verify(rows: list[dict], args) -> int:
    pos = [r for r in rows if r["label"] == "P"]
    neg = [r for r in rows if r["label"] == "N"]
    unread = [r for r in rows if r["state"] == "unread"]
    accepted_pos = [r for r in pos if r["state"] == "accepted"]
    accepted_neg = [r for r in neg if r["state"] == "accepted"]
    skills = {}
    for r in pos:
        skills[r["skill"]] = skills.get(r["skill"], False) or r["state"] == "accepted"
    for r in rows:
        right = (r["label"] == "P") == (r["state"] == "accepted")
        print(f"{'ok   ' if right else 'WRONG'} {r['label']} {r['state']:8s} {str(r['grade'] or r['why']):9s} {r['skill'][:44]:44s} {r['url'][-70:]}")
    summary = {"positives_accepted": f"{len(accepted_pos)}/{len(pos)}", "negatives_accepted": f"{len(accepted_neg)}/{len(neg)}",
               "skills_with_an_accepted_positive": f"{sum(skills.values())}/{len(skills)}", "unread": len(unread),
               "accepted_negatives": [{k: r[k] for k in ("skill", "url", "kind", "grade", "third")} for r in accepted_neg]}
    summary["tokens"] = token_report()
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), "utf-8")
    rate = len(accepted_pos) / len(pos) if pos else 0.0
    return 1 if (not rows or accepted_neg or unread or rate < args.min_positives) else 0


def sets_mode(app, args) -> int:
    from app.sourcing.verify import Verifier  # noqa: PLC0415

    dojo = app.state.dojo
    verifier = Verifier(dojo)
    exam = load("dp300.json")["exam"]
    bad = 0
    wrong = []
    for case in load("set_checks.json")["cases"]:
        if not wanted(case["text"], args.only):
            continue
        pages = [{"url": p["url"], "title": title_of(p["url"]), "grade": p["grade"], "snap": dojo.sources.candidate(p["url"])} for p in case["pages"]]
        out = verifier.complete_set(context(exam, case), pages)
        got = "complete" if out["complete"] else "incomplete"
        ok = got == case["label"]
        bad += (not ok and case["label"] == "incomplete")
        if not ok:
            wrong.append(case["text"])
        print(f"{'ok   ' if ok else 'WRONG'} expected {case['label']:10s} got {got:10s} gate={out['gate']['answer']} author={out['author']['answer']}  {case['text'][:60]}")
        if out["gate"]["unsupported"] or out["author"]["unsupported"]:
            print(f"       unsupported: gate {out['gate']['unsupported']} author {out['author']['unsupported']}")
    print(json.dumps({"wrong": wrong, "incomplete_sets_called_complete": bad, "tokens": token_report()}, indent=1))
    return 1 if bad or (wrong and args.strict) else 0


def retrieval_mode(app, args) -> int:
    from app.pagefinder import _queries  # noqa: PLC0415
    from app.sourcing.inventory import Inventory  # noqa: PLC0415
    from app.sourcing.net import Net, SourcingError  # noqa: PLC0415
    from app.sourcing.retrieve import McpClient, canon_url, learn_api  # noqa: PLC0415

    dojo = app.state.dojo
    net = Net(dojo.sources)
    mcp = McpClient(net)
    exam, skills = locked(args.sets.split(","))
    skills = [s for s in skills if wanted(s["text"], args.only)]
    inv = Inventory(dojo.store, net, dojo.sources, "eval")
    inv.seed(HUBS.get(exam["code"], []))
    print(f"inventory: {inv.size()} pages")
    found = {"code": exam["code"], "title": exam["title"]}
    totals = {k: [0, 0] for k in ("r1", "r2", "r3", "union")}
    for s in skills:
        want = {canon_url(p["url"]) for p in s["positives"]}
        queries = _queries(found, {"title": s["domain"]}, {"title": s["group"]}, s["text"])
        got = {"r1": set(), "r2": set(), "r3": set()}
        for q in queries[:3]:
            try:
                got["r1"] |= {h["url"] for h in learn_api(net, q, top=20)}
                got["r2"] |= {h["url"] for h in mcp.search(q)}
            except SourcingError as e:
                print("  search failed:", e)
        got["r3"] = {h["url"] for h in inv.search(queries, top=30)}
        got["union"] = got["r1"] | got["r2"] | got["r3"]
        for k in totals:
            totals[k][0] += len(want & got[k])
            totals[k][1] += len(want)
        print(f"{s['text'][:60]:60s} " + " ".join(f"{k}={len(want & got[k])}/{len(want)}" for k in totals))
    print(json.dumps({k: f"{a}/{b}" for k, (a, b) in totals.items()}, indent=1))
    return 0


def e2e_mode(app, args) -> int:
    from app.core import iso, sha256  # noqa: PLC0415
    from app.sources import normalize  # noqa: PLC0415
    from app.sourcing.retrieve import canon_url  # noqa: PLC0415
    from app.sourcing.state import skill_key  # noqa: PLC0415

    dojo, nx = app.state.dojo, app.state.newexams
    exam, skills = locked(args.sets.split(","))
    skills = [s for s in skills if wanted(s["text"], args.only)][:args.limit or None]
    domains: dict[str, dict[str, list[dict]]] = {}
    for s in skills:
        domains.setdefault(s["domain"], {}).setdefault(s["group"], []).append(s)
    raw = {"id": exam["code"].lower(), "exam": exam["code"], "title": exam["title"], "issuer": "Microsoft", "kind": "certification",
           "version": f"retrieved-{iso()[:10]}", "added": iso(), "notes": [],
           "source": {"name": f"Study guide for Exam {exam['code']}", "url": f"https://learn.microsoft.com/en-us/credentials/certifications/resources/study-guides/{exam['code'].lower()}"},
           "official_practice_assessment": None, "exam_page": None,
           "domains": [{"id": f"d{i}", "title": d, "weight_min": 20, "weight_max": 25,
                        "groups": [{"title": g, "skills": [{"id": f"d{i}.g{j}.s{k}.{sha256(normalize(s['text']))[:6]}", "text": s["text"], "sources": [], "learn": []}
                                                           for k, s in enumerate(ss, 1)]} for j, (g, ss) in enumerate(gs.items(), 1)]}
                       for i, (d, gs) in enumerate(domains.items(), 1)]}
    dojo.packages.add(raw)
    sourcer = nx.sourcer
    started = time.time()
    steps = 0
    while sourcer.step() and steps < args.max_steps:
        steps += 1
        st = sourcer.status(raw["id"])
        print(f"step {steps} ({time.time() - started:.0f}s): {st['counts']} calls={st['calls_today']} {st.get('note') or ''}")
    st = sourcer.status(raw["id"])
    by_text = {s["text"]: s for s in skills}
    critical, dropped_bad, unlabelled, found = [], [], [], 0
    for d in st["details"]:
        s = by_text.get(d["text"])
        pos = {canon_url(p["url"]) for p in s["positives"]}
        neg = {canon_url(n["url"]) for n in s["negatives"]}
        # Dojo keeps addresses as Learn redirects them (with `?view=`): they are compared as the locked sets write them.
        acc = [(canon_url(a["url"]), bool(a.get("cited"))) for a in d["accepted"]]
        cited = [u for u, c in acc if c]
        hit = [u for u in cited if u in pos]
        found += bool(hit)
        critical += [(d["text"], u) for u in cited if u in neg]   # a negative a lesson would be written from
        dropped_bad += [(d["text"], u) for u, c in acc if u in neg and not c]
        unlabelled += [(d["text"], u) for u in cited if u not in pos and u not in neg]
        print(f"{d['state']:10s} round {d['round']} accepted={len(acc)} cited={len(cited)} locked-positives-cited={len(hit)}/{len(pos)}  {d['text'][:60]}")
        for u, c in acc:
            print(f"   {'*' if c else ' '} {'POSITIVE' if u in pos else 'NEGATIVE' if u in neg else 'unlabelled'}  {u}")
    summary = {"counts": st["counts"], "skills_with_a_locked_positive_cited": f"{found}/{len(st['details'])}", "cited_negatives": critical,
               "accepted_but_not_cited_negatives": dropped_bad, "unlabelled_cited_pages": len(unlabelled), "calls": st["calls_today"],
               "seconds": round(time.time() - started), "tokens": token_report()}
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "status": st, "unlabelled": unlabelled}, ensure_ascii=False, indent=1), "utf-8")
    return 1 if critical or not st["details"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=("verify", "sets", "retrieval", "e2e"), required=True)
    ap.add_argument("--sets", default="gold13,verified20", help="dp300.json sets: gold13, verified20, silver (comma separated)")
    ap.add_argument("--only", default="", help="only skills whose text contains this (several: comma separated)")
    ap.add_argument("--limit", type=int, default=0, help="e2e: at most this many skills")
    ap.add_argument("--max-steps", type=int, default=300, help="e2e: at most this many background steps")
    ap.add_argument("--min-positives", type=float, default=0.8, help="verify: exit 1 below this share of positives accepted")
    ap.add_argument("--strict", action="store_true", help="sets: exit 1 for any wrong answer, not only an incomplete set called complete")
    ap.add_argument("--out", default="", help="write the results to this JSON file")
    args = ap.parse_args(argv)
    app = setup(models=args.mode != "retrieval")
    return {"verify": verify_mode, "sets": sets_mode, "retrieval": retrieval_mode, "e2e": e2e_mode}[args.mode](app, args)


if __name__ == "__main__":
    raise SystemExit(main())
