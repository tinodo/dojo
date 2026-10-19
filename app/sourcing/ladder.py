"""The search for the official pages of an added exam's skills (ADR 0018, 4.4 to 4.8).

Each skill has a document of its own under `sourcing/<exam>/`: its state (`covered`, `partial`, `searching`,
`exhausted`, `blocked`), the round it reached, the phrases tried, every page found with its verdict, the
accepted pages, and the ledger of its steps. The work goes round by round over the whole exam:

1. phrases from the planner; Learn's search (narrowed to the exam's products, and not), Learn's MCP server, the
   exam's table-of-contents inventory and web search; the best 20 pages are judged by two model families;
2. new phrases that know why pages were refused, the next 20 pages, refused pages that several searches agree
   on judged once more with more of the page;
3. every title of the inventory, read by the gate model, for the skills that are still open;
4. web search with the exam's wording and no domain filter (documentation only, checked afterwards).

A skill whose accepted pages are enough is `covered` and written into the package at once. After round 4 the
accepted pages of a `partial` skill are written too (the lesson checker refuses what a page does not support)
and the search goes on at the retry schedule of `exhausted`: three days in a row, then weekly. Only skills
without sources are written, so no lesson, narration or film is ever orphaned; a skill whose cited pages both
models refuse (twice, a day apart) is replaced whether or not its lesson has a film (ADR 0021), and a `partial`
skill gains pages only while its lesson has no film. A step that failed three times blocks its skill
for the day; `exhausted` is reached only when every step of the four rounds succeeded and nothing was
accepted: it says that the configured retrieval is exhausted, never that the documentation lacks a page."""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from ..ai import AIError
from ..core import UserError, carried, iso, parse_iso, utcnow
from ..pagefinder import LICENSE, PUBLISHER, SCOPED_TOP, hub_pages
from ..sources import same_page
from .counted import Counted
from .inventory import Inventory
from .net import Net, SourcingError
from .plan import plan_group
from .retrieve import McpClient, canon_url, deny_url, fuse, github_search, learn_api
from .scan import scan_window
from .state import Books, DeadStep, Lease, LeaseLost, begin, due, finish, fingerprint, skill_key
from .verify import VERIFIER_VERSION, Verifier, read_page

log = logging.getLogger("dojo.sourcing")

ROUNDS = 4
VERIFY_LIMIT = 40          # pages judged for a skill in one round: all that the searches add (POOL_ADD), so none is left unjudged
SLICE = 5                  # pages judged in one step (one batch of the gate model)
MAX_ACCEPTED = 3           # a skill cites at most this many pages (app/packages.py)
KEEP_ACCEPTED = 14          # accepted pages kept in a skill's book; the best ones when there are more
RANK_LIMIT = 12             # accepted pages the two models order and sift (the best by the plain order)
APPEAL_LIMIT = 6
APPEALABLE = ("other-task", "mention", "quote")
QUOTAS = {"r1s": 4, "r1u": 4, "r2": 5, "r3": 5, "r4": 10, "r7": 10}   # Learn's own search brings the most noise, web search the least
POOL_ADD = 40              # pages one round's searches add to a skill's pool, at most
POOL_LIMIT = 200            # pages kept in a skill's pool; the last round judges every one that is still new, up to this many
BATCH_SKILLS = 4           # skills of one group worked on together
PARALLEL = 4
DAY_CALLS = 4000           # model calls an exam may use in a UTC day; a day's cap pauses the work, it never ends a skill
WINDOW = 1500
SCAN_PICKS = 10
RETRY_DAYS = (1, 1, 1, 7)  # days until the next cycle of an `exhausted` or `partial` skill: three daily, then weekly
BACKOFF_MIN = 5            # minutes after a failed step, doubled for each try
AUDIT_PER_STEP = 4
AUDIT_TRIES = 3             # tries of the audit of one skill, counted before each paid call: then it is left `unchecked`
REPLACE_PER_DAY = 3        # skills whose cited pages both models refused, put back to the search, in a UTC day
CONFIRM_HOURS = 20         # a skill whose cited pages were refused is judged once more after this, before anything is replaced
SEED_TOP = 3               # pages of each skill that grow the inventory
WEB_CHECK_H = 24            # hours between two web search self-tests
SUMMARY_S = 8               # seconds the exam page's numbers are kept


class StepFailed(Exception):
    """A step that has to be made again: a mandatory search or model call failed."""


class CapReached(Exception):
    """The exam used its model calls for today."""


def title_of(url: str) -> str:
    """A readable title for an address that came without one (web search gives addresses only)."""
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return " ".join(slug.replace("-", " ").replace("_", " ").split())[:200] or url


def interleave(lists: list[list[dict]]) -> list[dict]:
    """The hits of several searches in rank order: every search's first, then every search's second, ..."""
    out: list[dict] = []
    for rank in range(max([0] + [len(x) for x in lists])):
        out += [x[rank] for x in lists if rank < len(x)]
    return out


def add_to_pool(doc: dict, entries: list[dict], round_no: int) -> None:
    pool = doc["pool"]
    for e in entries:
        cur = pool.get(e["url"])
        if cur:
            cur["by"] = sorted(set(cur["by"]) | set(e["by"]))
            cur["rank"] = min(cur["rank"], e["rank"])
            continue
        doc["seq"] = doc.get("seq", 0) + 1
        pool[e["url"]] = {"title": str(e.get("title") or title_of(e["url"]))[:200], "by": e["by"], "rank": e["rank"],
                          "round": round_no, "verdict": "new", "order": doc["seq"]}
    if len(pool) > POOL_LIMIT:
        for url in sorted((u for u, e in pool.items() if e["verdict"] == "rejected"), key=lambda u: pool[u]["order"])[:len(pool) - POOL_LIMIT]:
            del pool[url]


def _slim(v: dict | None) -> dict | None:
    return {k: v[k] for k in ("grade", "quote", "reason", "model") if k in v} if v else None


def apply_result(doc: dict, url: str, res: dict) -> None:
    """Put one page's verdict into the skill's pool and, when accepted, into its accepted pages."""
    e = doc["pool"][url]
    e["v"] = res["v"]
    if res["state"] == "accepted":
        e["verdict"], e["grade"] = "accepted", res["grade"]
        if not any(a["url"] == res["url"] for a in doc["accepted"]):
            doc["accepted"].append({"url": res["url"], "title": res["title"], "grade": res["grade"],
                                    "gate": _slim(res["gate"]), "author": _slim(res["author"]), "sha256": res["sha256"],
                                    "by": e["by"], "rank": e["rank"], "at": res["at"]})
            if len(doc["accepted"]) > KEEP_ACCEPTED:
                worst = {id(a) for a in sorted(doc["accepted"], key=_plain)[KEEP_ACCEPTED:]}
                doc["accepted"] = [a for a in doc["accepted"] if id(a) not in worst]
    elif res["state"] == "unread":
        e["verdict"], e["why"] = "unread", res["why"]
        e["tries"] = e.get("tries", 0) + 1
    else:
        e["verdict"], e["why"] = "rejected", res["why"]


def _plain(a: dict) -> tuple:
    """The order of accepted pages before the models have compared them: both-`teaches` first, then those more
    searches agreed on, then the better rank."""
    return a["grade"] != "teaches", -len(a.get("by") or []), a.get("rank") or 99


def best(doc: dict, limit: int = MAX_ACCEPTED) -> list[dict]:
    """The accepted pages a skill cites. Once the two models have compared the pages (`_rank`) their order counts, the
    pages both called direct come first, and the pages both dropped are left out (never all of them). When some page is
    direct, a page only one model dropped does not fill a place beside it. A page accepted after the comparison comes
    after the compared ones."""
    ranked = doc.get("ranked") or {}
    drop, direct = set(ranked.get("drop") or []), set(ranked.get("direct") or [])
    pages = [a for a in doc["accepted"] if a["url"] not in drop] or list(doc["accepted"])
    if direct:
        order = ranked.get("order") or []
        one_dropped = {u for m in (ranked.get("models") or {}).values() for u in (m or {}).get("drop") or []} - set(order[:1])
        pages = [a for a in pages if a["url"] in direct or a["url"] not in one_dropped] or pages
    place = {u: i for i, u in enumerate(ranked.get("order") or [])}
    return sorted(pages, key=lambda a: (a["url"] not in direct, place.get(a["url"], len(place)), _plain(a)))[:limit]


def refs_of(doc: dict, limit: int = MAX_ACCEPTED) -> list[dict]:
    """The source references of a package for the accepted pages (the shape app/pagefinder.py keeps)."""
    out = []
    for a in best(doc, limit):
        out.append({"url": a["url"], "title": a["title"], "publisher": PUBLISHER[urlparse(a["url"]).hostname], "license": LICENSE,
                    "use": "ground", "verified": {"quote": a["gate"]["quote"], "by": f"{a['gate']['model']}; {a['author']['model']}",
                                                  "on": a["at"][:10], "sha256": a["sha256"]}})
    return out


def _cites(pages: list[dict], urls: list) -> bool:
    """Whether the package cites any of these addresses: the same page, or the very same address (one that names no
    documentation page cannot be compared as a page)."""
    return any(c.get("url") == u or same_page(c.get("url"), u) for c in pages for u in urls)


def _refused(res: dict) -> bool:
    """Whether both models read the page and each gave it no grade at all."""
    gate, author = res.get("gate"), res.get("author")
    return bool(gate and author and gate.get("grade") == "no" and author.get("grade") == "no")


def audit_verdicts(pages: list[dict], results: list[dict]) -> tuple[list[dict], str]:
    """How both models judged the pages a skill cites: one line for each page, and the skill's verdict. `supported`: a
    page both accepted. `invalid`: both models read every page and gave each no grade. `weak`: anything between."""
    verdicts = [{"url": p["url"], "state": res["state"], "grade": res.get("grade"), "why": res.get("why"), "refused": _refused(res),
                 **({"moved_to": res["moved_to"]} if res.get("moved_to") else {})}
                for p, res in zip(pages, results)]
    if any(v["state"] == "accepted" for v in verdicts):
        return verdicts, "supported"
    if verdicts and all(v["refused"] for v in verdicts):
        return verdicts, "invalid"
    return verdicts, "weak"


def needs_work(doc: dict, has_sources: bool, now: Any) -> bool:
    """Whether a skill is worked on now. A skill with sources is worked on only when its cited pages were refused
    (`replacing`) or it is `partial` and a later cycle looks for more pages (`growing`); a `partial` skill that
    cannot gain a page (its package cites three already, or its lesson has a film) is `stuck` and is left as it is."""
    state = doc["state"]
    if state == "covered" or (state == "partial" and doc.get("stuck")):
        return False
    if state == "searching":
        return (not has_sources or bool(doc.get("replacing")) or bool(doc.get("growing"))) and due(doc, now)
    return due(doc, now)


class Run:
    """One exam's work: the lease, the books, the inventory, the study guide and the model calls counted."""

    def __init__(self, sourcer: "Sourcer", pid: str, found: dict):
        self.sourcer, self.pid, self.found = sourcer, pid, found
        self.lease = sourcer.lease(pid)
        self.books = Books(sourcer.store, pid, self.lease)
        self.inv = Inventory(sourcer.store, sourcer.net, sourcer.dojo.sources, pid, lease=self.lease)
        self.ai = Counted(sourcer.dojo.ai, self.count)   # every attempt to ask a model is counted against the day's calls
        self.verifier = Verifier(sourcer.dojo, count=self.count)
        self.scope: tuple[str, ...] = ()
        self._lock = threading.Lock()
        self.calls = 0
        self.base = self.books.calls_today()

    def count(self) -> None:
        with self._lock:
            if self.base + self.calls >= DAY_CALLS:
                raise CapReached()
            self.calls += 1

    def flush(self) -> None:
        with self._lock:
            n, self.calls = self.calls, 0
        if n:
            self.books.add_calls(n)
            self.base += n

    def prepare(self) -> None:
        """The exam's products (a prior for Learn's search) and the inventory of the study guide's own pages."""
        guide = self.found.get("_guide")
        if isinstance(guide, dict):
            try:
                self.scope = self.sourcer.nx.finder.scope(self.found)
            except Exception as e:  # noqa: BLE001 - the scope is only a prior
                log.info("no product scope for %s: %s", self.pid, e)
            if not self.found["code"].startswith("GH-"):
                self.inv.seed([u for u in hub_pages(guide) if not deny_url(u)])


class Sourcer:
    """Finds the pages that teach every skill of the added exams, by itself, in the background."""

    def __init__(self, dojo: Any, newexams: Any, now: Callable[[], Any] = utcnow):
        self.dojo = dojo
        self.nx = newexams
        self.store = dojo.store
        local = bool(getattr(getattr(dojo, "settings", None), "local", False))
        # With the stand-in models and a mock site (tests, DOJO_LOCAL) there is nobody to be polite to.
        self.net = Net(dojo.sources, per_second=1000.0, sleep=lambda s: None) if local else Net(dojo.sources)
        self.mcp = McpClient(self.net)
        self.now = now
        self._leases: dict[str, Lease] = {}
        self._summaries: dict[str, tuple[float, dict]] = {}   # the exam page's numbers, read at most every SUMMARY_S seconds
        self._lock = threading.Lock()
        self._package = threading.RLock()   # the package is read, changed and written here one at a time (never under the store's lock)
        self.state: dict[str, Any] = {"current": None, "last": None, "worked": 0, "note": ""}

    def lease(self, pid: str) -> Lease:
        with self._lock:
            if pid not in self._leases:
                self._leases[pid] = Lease(self.store, pid)
            return self._leases[pid]

    # ------------------------------------------------------------ the package's skills

    @staticmethod
    def skills_of(raw: dict) -> list[dict]:
        out = []
        for d in raw["domains"]:
            for g in d["groups"]:
                for s in g["skills"]:
                    out.append({"key": skill_key(d["title"], g["title"], s["text"]), "id": s["id"], "domain": d["title"],
                                "group": g["title"], "text": s["text"], "sources": s.get("sources") or []})
        return out

    def _context(self, raw: dict, s: dict) -> dict:
        return {"code": raw["exam"], "title": raw["title"], "domain": s["domain"], "group": s["group"], "text": s["text"]}

    def _share(self, pid: str, sid: str) -> float:
        try:
            return float(self.dojo.packages.skill(pid, sid)["share"])
        except Exception:  # noqa: BLE001 - the order is only a preference
            return 0.0

    def _found(self, raw: dict) -> dict:
        try:
            found = self.nx._found(raw["exam"])
            if isinstance(found, dict) and found.get("state") == "found":
                return found
        except Exception as e:  # noqa: BLE001 - the study guide is only needed for the products
            log.info("could not read the study guide of %s again: %s", raw.get("exam"), e)
        return {"code": raw["exam"], "title": raw["title"]}

    # ------------------------------------------------------------ the background step

    def exams(self) -> list[str]:
        return [pid for pid in self.dojo.packages.ids() if self.dojo.packages.is_added(pid)]

    def step(self) -> bool:
        """One bounded piece of work for the first exam that needs it: True when more is to come."""
        if self.exams():
            try:
                self.check_web()
            except Exception:  # noqa: BLE001 - a failed self-test never stops the search
                log.info("the web search self-test failed")
        for pid in self.exams():
            raw = self.store.read("packages", pid, default=None)
            if not isinstance(raw, dict):
                continue
            run = Run(self, pid, self._found(raw))
            if not run.lease.acquire():
                continue
            self.state.update(current=pid)
            try:
                if run.base >= DAY_CALLS:
                    self.state["note"] = "paused until tomorrow: the day's model calls for this exam are used"
                    continue
                if self._work(run, raw):
                    self.state.update(last=iso(), worked=self.state["worked"] + 1, note="")
                    return True
            except CapReached:
                self.state["note"] = "paused until tomorrow: the day's model calls for this exam are used"
            except LeaseLost:
                log.warning("the sourcing lease for %s was taken over", pid)
            except Exception:  # noqa: BLE001 - never let the background loop die
                log.exception("the search for the pages of %s failed", pid)
            finally:
                try:
                    run.flush()
                    run.lease.release()
                except Exception:  # noqa: BLE001
                    log.info("could not release the sourcing lease for %s", pid)
                self._summaries.clear()   # the books changed
                self.state.update(current=None)
        return False

    def _items(self, run: Run, raw: dict) -> list[dict]:
        """The skills that need work now, heaviest first."""
        now = self.now()
        items = []
        for s in self.skills_of(raw):
            doc = run.books.skill(s["key"])
            if needs_work(doc, bool(s["sources"]), now):
                if doc["state"] in ("exhausted", "partial", "blocked"):
                    self._restart(doc)
                items.append({**s, "doc": doc, "ctx": self._context(raw, s), "has_sources": bool(s["sources"])})
        return sorted(items, key=lambda w: (-self._share(run.pid, w["id"]), w["id"]))

    @staticmethod
    def _restart(doc: dict) -> None:
        """A new cycle for a skill that waited: its old verdicts stay, its steps start afresh. A `partial` skill
        that was written already looks for further pages (`growing`)."""
        doc["cycle"] += 1
        if doc["state"] == "partial":
            doc["growing"] = True
        if doc["state"] != "blocked":
            # The planner is asked again, and told which phrases were tried and which pages were refused.
            doc["tried"] = (doc.get("tried", []) + [q for k in sorted(doc["plan"]) for q in doc["plan"][k]])[-40:]
            doc["plan"] = {}
            doc["round"] = 0
        doc.update(state="searching", next=None, blocked=None)

    def _tidy(self, run: Run, raw: dict) -> dict:
        """The note an earlier version of Dojo wrote into a package ("no official page passed the check ... counts as
        not shown") is stale once the search runs by itself: the exam page says how far it is, live."""
        if not any("no official page passed the check" in n for n in raw.get("notes") or []):
            return raw
        with self._package:
            fresh = self.store.read("packages", run.pid, default=None)
            if not isinstance(fresh, dict):
                return raw
            fresh["notes"] = [n for n in fresh.get("notes") or [] if "no official page passed the check" not in n]
            self.dojo.packages.add(fresh)
            return fresh

    @property
    def package_lock(self) -> Any:
        """Held while a package is read, changed and written here, and by the removal of an exam, so a skill is
        never written back into a package that was just removed."""
        return self._package

    def forget(self, pid: str) -> None:
        """The exam was removed: what the search kept for it goes too, and any step still running for it stops at its
        next write (its lease is revoked)."""
        with self._lock:
            lease = self._leases.pop(pid, None)
            self._summaries.pop(pid, None)
        if lease is not None:
            lease.revoke()
        for name in self.store.names("sourcing", pid):
            self.store.delete("sourcing", pid, name)
        self.store.delete("sourcing", pid)
        Inventory.forget(self.store, pid)
        try:
            self.store.path("sourcing", f"{pid}.lease").unlink(missing_ok=True)   # a re-added exam is not kept waiting for a lease nobody holds
        except OSError:
            log.info("could not remove the sourcing lease of a removed exam")

    def _work(self, run: Run, raw: dict) -> bool:
        raw = self._tidy(run, raw)
        if self._resume_replacements(run, raw):
            return True
        items = self._items(run, raw)
        if not items:
            return self._audit(run, raw)
        run.prepare()
        r = min(w["doc"]["round"] for w in items) + 1
        cohort = [w for w in items if w["doc"]["round"] + 1 == r]
        self.state["note"] = f"round {r} for {len(cohort)} skills of {run.pid}"
        if r == 3:
            return self._scan_round(run, cohort)
        first = cohort[0]
        batch = [w for w in cohort if (w["domain"], w["group"]) == (first["domain"], first["group"])][:BATCH_SKILLS]
        self._rounds(run, first["domain"], first["group"], batch, r, r)
        return True

    # ------------------------------------------------------------ the build's first pass

    def match_group(self, found: dict, domain: dict, group: dict, pending: list[tuple[str, str]],
                    offered: list[dict] | None = None) -> dict[str, dict]:
        """Rounds 1 and 2 for the skills of one group while an exam is being added (app/newexam.py). The skills
        that are `covered` come back with their sources; the others are searched further in the background once
        the exam is added."""
        pid = found["code"].lower()
        run = Run(self, pid, found)
        if not run.lease.acquire():
            raise UserError("Another Dojo process is searching for the pages of this exam. Press Build again in a few minutes.")
        try:
            run.prepare()
            code, title = found["code"], found.get("title", "")
            batch = []
            for key, text in pending:
                doc = run.books.skill(key)
                batch.append({"key": key, "id": key, "domain": domain["title"], "group": group["title"], "text": text,
                              "sources": [], "doc": doc, "has_sources": False,
                              "ctx": {"code": code, "title": title, "domain": domain["title"], "group": group["title"], "text": text}})
            for r in (1, 2):
                todo = [w for w in batch if w["doc"]["state"] == "searching" and w["doc"]["round"] == r - 1]
                if todo:
                    self._rounds(run, domain["title"], group["title"], todo, r, r)
            out = {}
            for w in batch:
                doc = w["doc"]
                error = (doc.get("blocked") or {}).get("error") if doc["state"] == "blocked" else doc.get("failure")
                # Only a `covered` skill is written into the package now; the others are searched further in the
                # background and written when the search is done with them (`_close`).
                out[w["key"]] = {"sources": refs_of(doc) if doc["state"] == "covered" else [], "checked": iso(),
                                 "v": VERIFIER_VERSION, "accepted": len(doc["accepted"]),
                                 "error": error if not doc["accepted"] and error else None, "candidates": len(doc["pool"])}
            return out
        except CapReached as e:
            raise UserError("Dojo used today's model calls for searching the pages of this exam. The pages found so far are kept: "
                            "press Build again tomorrow to go on.") from e
        finally:
            run.flush()
            run.lease.release()
            self._summaries.clear()

    def adopt(self, pid: str, raw: dict) -> None:
        """The exam was just added with the sources of its `covered` skills: note it in their books."""
        run = Run(self, pid, {"code": raw["exam"], "title": raw["title"]})
        if not run.lease.acquire():
            return
        try:
            for s in self.skills_of(raw):
                doc = run.books.skill(s["key"])
                if doc["state"] == "covered" and doc["accepted"] and s["sources"] and not doc.get("activated"):
                    doc["activated"] = {"at": iso(), "sources": len(s["sources"]), "partial": False, "build": True}
                    run.books.save_skill(doc)
        finally:
            run.lease.release()
            self._summaries.clear()

    # ------------------------------------------------------------ rounds

    def _rounds(self, run: Run, domain: str, group: str, batch: list[dict], first: int, last: int) -> None:
        """Rounds `first` to `last` (1, 2 or 4) for skills of one group, a few at a time."""
        for r in range(first, last + 1):
            self._plan(run, domain, group, batch, r)
            with ThreadPoolExecutor(min(PARALLEL, len(batch))) as pool:
                for f in [pool.submit(carried(self._advance), run, w, r) for w in batch]:   # the learner and the action travel to the threads
                    f.result()
            self._grow(run, batch)

    def _plan(self, run: Run, domain: str, group: str, batch: list[dict], r: int) -> None:
        if r not in (1, 2):
            return
        todo = [w for w in batch if str(r) not in w["doc"]["plan"] and w["doc"]["state"] == "searching" and w["doc"]["round"] == r - 1]
        specs = []
        for w in todo:
            doc = w["doc"]
            tried = doc.get("tried", []) + [q for k in sorted(doc["plan"]) for q in doc["plan"][k]]
            direct = set((doc.get("ranked") or {}).get("direct") or [])
            rejected = ([{"title": e["title"], "why": e.get("why") or "other-task"}
                         for e in sorted(doc["pool"].values(), key=lambda e: e["order"]) if e["verdict"] == "rejected"]
                        + [{"title": a["title"], "why": "only the same area"} for a in doc["accepted"] if a["url"] not in direct])[-8:]
            fp = fingerprint("plan", r, w["text"], tried, rejected)
            try:
                if begin(doc, "plan", fp):
                    run.books.save_skill(doc)
                    specs.append({"w": w, "key": w["key"], "text": w["text"], "tried": tried, "rejected": rejected})
            except DeadStep:
                self._block(run, w, "plan")
        if not specs:
            return
        phrases = plan_group(run.ai, run.found, {"title": domain}, {"title": group}, specs)
        for s in specs:
            doc = s["w"]["doc"]
            doc["plan"][str(r)] = phrases[s["key"]]
            finish(doc, "plan")
            run.books.save_skill(doc)

    def _advance(self, run: Run, w: dict, r: int) -> None:
        """Round `r` for one skill: search, judge, assess, and write the skill when it is `covered`."""
        doc = w["doc"]
        if doc["state"] != "searching" or doc["round"] != r - 1:
            return
        try:
            self._retrieve(run, w, r)
            self._verify(run, w, r, drain=r >= ROUNDS)
            self._appeal(run, w, r)
            self._rank(run, w)
            self._assess(run, w)
            doc["round"] = r
            doc["failure"] = None
            if doc["state"] == "covered" and not self._activate(run, w):
                doc["state"], doc["partial"] = "searching", True   # its package keeps other pages: not covered by what it cites
            if doc["state"] != "covered" and r >= ROUNDS:
                self._close(run, w)
            run.books.save_skill(doc)
        except DeadStep as e:
            self._block(run, w, str(e))
        except (StepFailed, AIError, SourcingError, UserError) as e:
            self._failed(run, w, str(e))

    def _failed(self, run: Run, w: dict, why: str) -> None:
        """A step failed: it is tried again after a pause that grows with the tries."""
        doc = w["doc"]
        tries = max([s.get("tries", 0) for s in doc["steps"].values() if not s.get("done")] + [1])
        doc["failure"] = why[:300]
        doc["next"] = iso(self.now() + timedelta(minutes=BACKOFF_MIN * 2 ** (tries - 1)))
        log.info("a step for skill %s of %s failed (%s); tried again after a pause", w["key"], run.pid, why[:120])
        run.books.save_skill(doc)

    def _block(self, run: Run, w: dict, step: str) -> None:
        doc = w["doc"]
        doc.update(state="blocked", blocked={"step": step, "at": iso(), "error": doc.get("failure") or "a step failed three times"},
                   next=iso(self.now() + timedelta(days=1)))
        run.books.save_skill(doc)

    # ------------------------------------------------------------ searching

    def _queries(self, w: dict, r: int) -> list[str]:
        plan = w["doc"]["plan"]
        return plan.get(str(r)) or plan.get("2") or plan.get("1") or [w["text"]]

    def _retrieve(self, run: Run, w: dict, r: int) -> None:
        doc, ctx = w["doc"], w["ctx"]
        queries = self._queries(w, r)
        fp = fingerprint("retrieve", r, queries, list(run.scope), run.inv.size() > 0)
        if not begin(doc, "retrieve", fp):
            return
        run.books.save_skill(doc)
        by: dict[str, list[dict]] = {}
        problems: list[str] = []
        github = ctx["code"].startswith("GH-")
        try:
            if r in (1, 2):
                if github:
                    by["r1u"] = interleave([github_search(self.net, q, 10) for q in queries[:3]])
                else:
                    if r == 1 and run.scope:
                        by["r1s"] = learn_api(self.net, queries[0], products=run.scope, top=SCOPED_TOP)
                    by["r1u"] = interleave([learn_api(self.net, q, top=20) for q in queries[:5]])
                    try:
                        by["r2"] = interleave([self.mcp.search(q) for q in queries[:3]])
                    except SourcingError as e:
                        problems.append(f"r2: {e}")
                    if run.inv.size():
                        by["r3"] = run.inv.search(queries, top=30)
            by["r4"] = self._web(run, ctx, queries, r)
        except SourcingError as e:
            raise StepFailed(f"search: {e}") from e
        add_to_pool(doc, fuse(by, QUOTAS, POOL_ADD), r)
        doc["problems"] = problems[:4]
        finish(doc, "retrieve")
        run.books.save_skill(doc)

    def _web(self, run: Run, ctx: dict, queries: list[str], r: int) -> list[dict]:
        """Web search for the skill: only addresses come back, each judged like any other page. Round 4 asks with the
        exam's wording and no domain filter; rounds 1 and 2 ask with the planner's phrase for the skill and with its
        phrases for a product or deployment option (a skill that several products answer differently is not found by
        one phrase: "troubleshoot an HA/DR solution" finds Azure SQL's pages, "... SQL Server on Azure VMs availability
        groups" finds the troubleshooting guides of Always On)."""
        github = ctx["code"].startswith("GH-")
        if r == 4:
            phrases = [f"{ctx['title']} {ctx['text']} official documentation"]
        else:
            asked = [queries[0] if r == 1 else queries[-1]]
            asked += [q for q in queries[3:5] if q not in asked]
            phrases = [f"{ctx['title']} ({ctx['code']}) exam, domain {ctx['domain']}, group {ctx['group']}. Skill: {q}" for q in asked]
        domains = ("docs.github.com",) if github else (() if r == 4 else ("learn.microsoft.com",))
        lists: list[list[dict]] = []
        for phrase in phrases:
            try:
                found = run.ai.web_search(phrase, domains=domains, max_searches=2 if r == 4 else 1)
            except AIError as e:
                raise StepFailed(f"web search: {e}") from e
            hits = []
            for u in found["urls"]:
                c = canon_url(u)
                if c and not deny_url(c):
                    hits.append({"url": c, "title": title_of(c), "summary": ""})
            lists.append(hits)
        return interleave(lists)   # every search's first hit, then every search's second, ...

    def _grow(self, run: Run, batch: list[dict]) -> None:
        """Pages the searches found grow the inventory: their tables of contents may hold what is still missing."""
        urls = []
        for w in batch:
            doc = w["doc"]
            top = sorted((u for u, e in doc["pool"].items() if set(e["by"]) & {"r1u", "r2", "r4"}),
                         key=lambda u: (-len(doc["pool"][u]["by"]), doc["pool"][u]["rank"]))[:SEED_TOP]
            urls += top + [a["url"] for a in doc["accepted"]]
        if urls and not batch[0]["ctx"]["code"].startswith("GH-"):
            try:
                run.inv.seed(list(dict.fromkeys(urls)), max_new=40)
            except LeaseLost:
                raise
            except Exception as e:  # noqa: BLE001 - the inventory is a help, never a reason to stop
                log.info("could not grow the inventory of %s: %s", run.pid, e)

    # ------------------------------------------------------------ judging

    def _models(self) -> list[str]:
        m = self.dojo.models("gate", "author")
        return [m["gate"], m["author"]]

    def _verify(self, run: Run, w: dict, r: int, drain: bool = False) -> None:
        """Judge the new pages of the pool, VERIFY_LIMIT in a round; the pages left over are judged in the next round. The
        last round of a cycle (`drain`) judges every one that is left, and tries once more, at its end, what failed in it
        (a page that could not be read, a quote that was not on the page), so a transient failure does not cost a page when
        the skill is written. What a step is called depends only on what the books say (the pages of the batch and how
        often each was judged), never on where this call is in its loops: a restart in the middle of the second pass
        finds the batch under the same name and does it, and does not take it for the first pass that is done."""
        doc, ctx = w["doc"], w["ctx"]
        limit = POOL_LIMIT if drain else VERIFY_LIMIT
        for again in range(2 if drain else 1):
            for e in doc["pool"].values():
                if e.get("round", 0) >= r and not again:
                    continue   # what the searches of this round found is judged now, and not tried again at once
                if e["verdict"] == "unread" and e.get("tries", 0) < 3:
                    e["verdict"] = "new"
                elif e["verdict"] == "rejected" and e.get("why") == "quote" and not e.get("quote_tries"):
                    e["verdict"], e["quote_tries"] = "new", 1   # no valid quote is a failure of the models, not a verdict on the page: once more
            done = 0
            while done < limit:   # the whole round's pool: the best pages are chosen among what was accepted, not the first three
                new = sorted((u for u, e in doc["pool"].items() if e["verdict"] == "new"), key=lambda u: doc["pool"][u]["order"])[:SLICE]
                if not new:
                    break
                step = "verify." + fingerprint("batch", [(u, doc["pool"][u].get("judged", 0)) for u in sorted(new)])[:10]   # a batch that fails again is this step with one more try
                if not begin(doc, step, fingerprint("verify", VERIFIER_VERSION, new, self._models())):
                    for u in new:
                        doc["pool"][u]["verdict"] = "rejected"
                        doc["pool"][u]["why"] = "repeat"
                    continue
                run.books.save_skill(doc)
                results = run.verifier.grade_pages(ctx, [{"url": u, "title": doc["pool"][u]["title"]} for u in new])
                for u, res in zip(new, results):
                    apply_result(doc, u, res)
                    doc["pool"][u]["judged"] = doc["pool"][u].get("judged", 0) + 1
                finish(doc, step)
                run.books.save_skill(doc)
                done += len(new)

    def _appeal(self, run: Run, w: dict, r: int) -> None:
        """Pages one model refused that several searches agree on are judged by both models once more, with more of
        the page. Acceptance still needs both models; the searches only say which refusals deserve a second look."""
        doc, ctx = w["doc"], w["ctx"]
        if r < 2:
            return
        cands = sorted((u for u, e in doc["pool"].items() if e["verdict"] == "rejected" and e.get("why") in APPEALABLE
                        and not e.get("appealed") and (len(e["by"]) >= 2 or e["rank"] <= 3)),
                       key=lambda u: doc["pool"][u]["order"])[:APPEAL_LIMIT]
        if not cands:
            return
        if not begin(doc, "appeal", fingerprint("appeal", VERIFIER_VERSION, cands, self._models())):
            return
        run.books.save_skill(doc)
        results = run.verifier.grade_pages(ctx, [{"url": u, "title": doc["pool"][u]["title"]} for u in cands], big=True, both_all=True)
        for u, res in zip(cands, results):
            doc["pool"][u]["appealed"] = True
            apply_result(doc, u, res)
        finish(doc, "appeal")
        run.books.save_skill(doc)

    def _rank(self, run: Run, w: dict) -> None:
        """Which of the accepted pages a lesson is written from, and whether any of them explains the skill itself. A page
        that two models accepted one by one can still be a checklist of a wider topic, or another task's page: both
        models read the accepted pages side by side, order them by how directly they explain the skill, name the ones
        that do not, and name the ones a lesson can be written from mainly (`direct`). Pages are only left out here,
        never added, and the best one always stays."""
        doc, ctx = w["doc"], w["ctx"]
        for _ in range(KEEP_ACCEPTED + 1):   # a page that is gone or has moved out of the documentation is taken out; each pass takes out at least one, so this ends
            pages = sorted(sorted(doc["accepted"], key=_plain)[:RANK_LIMIT], key=lambda a: a["url"])   # shown in address order: no place is favoured
            if not pages:
                doc["ranked"] = None
                return
            fp = fingerprint("rank", VERIFIER_VERSION, [(a["url"], a["sha256"]) for a in pages], self._models())
            if (doc.get("ranked") or {}).get("fp") == fp or not begin(doc, "rank", fp):
                return
            run.books.save_skill(doc)
            snapped = self._snaps(doc, pages)
            if snapped is None:
                continue
            result = run.verifier.rank_pages(ctx, snapped)
            doc["ranked"] = {"fp": fp, "order": result["order"], "drop": result["drop"], "direct": result["direct"],
                             "models": result["models"], "at": iso()}
            finish(doc, "rank")
            run.books.save_skill(doc)
            return

    def _snaps(self, doc: dict, pages: list[dict]) -> list[dict] | None:
        """Accepted pages read again for the rank step and the set check, through the checks of a candidate: a page that
        now leads out of the documentation (`/previous-versions/`, training) or is gone reaches no model and is accepted
        no longer. None when such a page was taken out: the caller goes on with what is left."""
        snapped, lost = [], []
        for a in pages:
            snap, why = read_page(self.dojo.sources, a["url"])
            if snap is not None:
                snapped.append({"url": a["url"], "title": a["title"], "grade": a["grade"], "snap": snap})
            elif why == "unread":
                raise StepFailed("a page cannot be read again")
            else:
                lost.append((a, why))
        if not lost:
            return snapped
        gone = {id(a) for a, _ in lost}
        doc["accepted"] = [a for a in doc["accepted"] if id(a) not in gone]
        for a, why in lost:
            entry = doc["pool"].get(canon_url(a["url"]) or "")
            if entry:
                entry["verdict"], entry["why"] = "rejected", why
        doc["ranked"] = doc["set"] = None
        return None

    def _assess(self, run: Run, w: dict) -> None:
        """`covered` when a page the skill cites has both grades `teaches` and both models call it direct (it explains the
        skill itself, not only the topic around it), or when both models say the cited pages together cover the skill;
        otherwise `partial` (when there are accepted pages), and the search goes on."""
        doc, ctx = w["doc"], w["ctx"]
        if not doc["accepted"]:
            return
        chosen = best(doc)
        direct = set((doc.get("ranked") or {}).get("direct") or [])
        if any(a["grade"] == "teaches" and a["url"] in direct for a in chosen):
            doc["state"] = "covered"
            return
        fp = fingerprint("set", VERIFIER_VERSION, [(a["url"], a["sha256"]) for a in chosen], self._models())
        if not (doc.get("set") and doc["set"].get("fp") == fp):
            if begin(doc, "set", fp):
                run.books.save_skill(doc)
                pages = self._snaps(doc, chosen)
                if pages is None:
                    return self._assess(run, w)   # a page was taken out: judged again on what is left (the accepted pages only shrink)
                result = run.verifier.complete_set(ctx, pages)
                doc["set"] = {"fp": fp, "complete": result["complete"], "at": iso(),
                              "gate": {k: result["gate"][k] for k in ("answer", "unsupported", "model")},
                              "author": {k: result["author"][k] for k in ("answer", "unsupported", "model")}}
                finish(doc, "set")
        doc["state"] = "covered" if doc.get("set") and doc["set"].get("complete") else "partial"
        if doc["state"] == "partial":
            doc["state"] = "searching"   # a partial skill keeps searching until round 4 has run; `_close` settles it
            doc["partial"] = True

    # ------------------------------------------------------------ round 3: every title of the inventory

    def _scan_round(self, run: Run, cohort: list[dict]) -> bool:
        inv = run.inv
        if inv.size() == 0:
            for w in cohort:
                w["doc"]["round"] = 3
                run.books.save_skill(w["doc"])
            return True
        skills = [{"key": w["key"], "text": w["text"]} for w in cohort]
        skills_fp = fingerprint("scan", sorted((w["key"], w["doc"]["cycle"]) for w in cohort))   # a new cycle reads the titles again
        side = run.books.side("scan", None)
        if not isinstance(side, dict) or side.get("skills_fp") != skills_fp:
            side = {"skills_fp": skills_fp, "windows": {}, "tries": {}}
        for window in inv.windows(WINDOW):
            wfp = fingerprint("scan", [e["url"] for e in window], VERIFIER_VERSION)
            if wfp in side["windows"]:
                continue
            if side["tries"].get(wfp, 0) >= 3:
                for w in cohort:
                    self._block(run, w, f"scan.{wfp}")
                return True
            side["tries"][wfp] = side["tries"].get(wfp, 0) + 1
            run.books.save_side("scan", side)
            try:
                found = scan_window(run.ai, run.found, skills, window)
            except (AIError, UserError) as e:
                log.info("a window of titles could not be scanned for %s: %s", run.pid, e)
                for w in cohort:
                    self._failed(run, w, f"scan: {e}")
                return True
            side["windows"][wfp] = {k: [{"url": e["url"], "title": e["title"]} for e in v] for k, v in found.items()}
            run.books.save_side("scan", side)
        order = list(side["windows"].values())
        for w in cohort:
            picks: list[dict] = []
            for rank in range(5):
                for found in order:
                    mine = found.get(w["key"]) or []
                    if rank < len(mine) and mine[rank] not in picks:
                        picks.append(mine[rank])
            entries = [{"url": canon_url(p["url"]), "title": p["title"], "by": ["r7"], "rank": i} for i, p in enumerate(picks[:SCAN_PICKS], 1)
                       if canon_url(p["url"]) and not deny_url(p["url"])]
            add_to_pool(w["doc"], entries, 3)
        with ThreadPoolExecutor(min(PARALLEL, len(cohort))) as pool:
            for f in [pool.submit(carried(self._after_scan), run, w) for w in cohort]:
                f.result()
        return True

    def _after_scan(self, run: Run, w: dict) -> None:
        doc = w["doc"]
        try:
            self._verify(run, w, 3)
            self._appeal(run, w, 3)
            self._rank(run, w)
            self._assess(run, w)
            doc["round"] = 3
            doc["failure"] = None
            if doc["state"] == "covered" and not self._activate(run, w):
                doc["state"], doc["partial"] = "searching", True
            run.books.save_skill(doc)
        except DeadStep as e:
            self._block(run, w, str(e))
        except (StepFailed, AIError, SourcingError, UserError) as e:
            self._failed(run, w, str(e))

    # ------------------------------------------------------------ writing the package

    def _filmed(self, pid: str, sid: str) -> bool:
        """Whether a skill's lesson has a film. Anything that cannot be told counts as filmed."""
        try:
            media = self.nx._media()
            for lesson in self.dojo.lessons_for(pid, sid):
                for f in self.nx.checker.plan(lesson["id"])["films"].values():
                    if f + ".webm" in media:
                        return True
            return False
        except Exception:  # noqa: BLE001
            return True

    def _activate(self, run: Run, w: dict, partial: bool = False) -> bool:
        """Write the accepted pages into the package: for a skill without sources; or, for a skill whose cited
        pages both models refuse, in their place, filmed or not (ADR 0021); or, for a `partial` skill that was written
        before, as further pages (at most three). Any other skill whose lesson has a film is never changed. True unless
        the package keeps pages that do not make the skill `covered`: a skill is covered only by what its package cites."""
        doc = w["doc"]
        pid = run.pid
        store = self.store
        swapped = settled = None   # the skill whose refused pages were replaced, now or by an earlier step; the answer when nothing is written
        with self._package:
            run.lease.check()   # an exam removed meanwhile (`forget`) is never written back
            raw = store.read("packages", pid, default=None)
            if not isinstance(raw, dict):
                return True   # the exam is not added yet: `adopt` notes it afterwards
            skill = next((s for d in raw["domains"] for g in d["groups"] for s in g["skills"]
                          if skill_key(d["title"], g["title"], s["text"]) == w["key"]), None)
            if skill is None:
                return True
            current = skill.get("sources") or []
            doc["cited"] = len(current)
            fresh = refs_of(doc)
            if not fresh:
                return True
            if not current:
                skill["sources"] = fresh
            elif doc.get("replacing"):
                refused, doc["replacing"] = doc["replacing"], None
                doc.pop("replacement_waits", None)
                swapped = skill["id"]
                if _cites(current, refused):
                    skill["sources"] = fresh   # a film does not keep pages both models refused: it would go on teaching from them
                else:
                    settled = self._justified(doc, current)   # none is cited any more: written by an earlier step, or changed since; what it cites stays
            elif doc.get("activated") and len(current) < MAX_ACCEPTED and not self._filmed(pid, skill["id"]):
                more = [r for r in fresh if not any(same_page(r["url"], c.get("url")) for c in current)]
                skill["sources"] = (current + more)[:MAX_ACCEPTED]
                if skill["sources"] == current:
                    return self._justified(doc, current)
            else:
                doc["activated"] = doc.get("activated") or {"at": iso(), "sources": len(current), "partial": partial, "already": True}
                return self._justified(doc, current)
            if settled is None:
                idx = run.books.index()
                idx["generation"] = int(idx.get("generation") or 0) + 1
                self.dojo.packages.add(raw)
                run.books.save_index(idx)
                doc["activated"] = {"at": iso(), "generation": idx["generation"], "sources": len(skill["sources"]), "partial": partial}
                doc["cited"] = len(skill["sources"])
                cited = list(skill["sources"])
        preparer = getattr(self.nx, "preparer", None)
        if preparer is not None:
            if swapped:
                self._start_again(preparer, pid, swapped)
            preparer.wake()
        return settled if settled is not None else self._justified(doc, cited)

    @staticmethod
    def _start_again(preparer: Any, pid: str, sid: str) -> None:
        """The pages of a skill were replaced: a failed or given-up attempt at its lesson was made on the old pages, and
        neither makes the new lesson wait nor stops it (app/deeper.py). It runs on every settled replacement, also one an
        earlier step wrote before it stopped, and a failure is not swallowed: the book is not saved, so the next step settles it again."""
        deeper = getattr(preparer, "deeper", None)
        if deeper is not None:
            deeper.start_again(pid, sid)

    @staticmethod
    def _justified(doc: dict, cited: list[dict]) -> bool:
        """Whether the pages the package cites are what made the skill `covered`: a direct both-`teaches` page among them,
        or, when the models said the best pages together cover it, all of those pages."""
        chosen = best(doc)
        direct = set((doc.get("ranked") or {}).get("direct") or [])
        has = lambda a: any(same_page(a["url"], c.get("url")) for c in cited)   # noqa: E731
        if any(a["grade"] == "teaches" and a["url"] in direct and has(a) for a in chosen):
            return True
        return bool(doc.get("set") and doc["set"].get("complete")) and all(has(a) for a in chosen)

    def _resume_replacements(self, run: Run, raw: dict) -> bool:
        """Skills whose cited pages both models refused, and whose replacement was held back because their lesson has a
        film (the rule before ADR 0021): the pages the search accepted for them are written now, if the refusal is still
        on record. Nothing is searched and no model is called: it is files, and the new lesson is written by the
        preparer as for any change of pages. One skill a step; True when one was settled."""
        side = run.books.side("audit", None)
        refused = (side.get("skills") or {}) if isinstance(side, dict) and side.get("v") == VERIFIER_VERSION else {}
        for s in self.skills_of(raw):
            doc = run.books.skill(s["key"])
            if not (doc["state"] == "partial" and doc.get("replacing") and doc.get("replacement_waits") and doc.get("accepted")):
                continue
            entry = refused.get(s["key"]) or {}
            if not (entry.get("verdict") == "invalid" and entry.get("confirmed")):
                continue   # the refusal is not on record any more (the audit was made again): the pages stay as they are
            justified = self._activate(run, {"key": s["key"], "id": s["id"], "doc": doc}, partial=True)
            if doc.get("replacing"):
                continue   # nothing could be written (the exam is gone, or no accepted page is left to cite): it waits as before
            if justified:
                doc.update(state="covered", partial=False)   # what the package cites now is what covers the skill
                doc.pop("stuck", None)
            run.books.save_skill(doc)
            log.info("the held replacement of the refused pages of skill %s of %s was settled", s["key"], run.pid)
            return True
        return False

    def _close(self, run: Run, w: dict) -> None:
        """Round 4 is done. Accepted pages that did not make the skill `covered` are written; with none, the
        configured retrieval is exhausted. Either way the search comes back at the retry schedule."""
        doc = w["doc"]
        doc["cycles"] = doc.get("cycles", 0) + 1
        doc["growing"] = False
        delay = timedelta(days=RETRY_DAYS[min(doc["cycles"] - 1, len(RETRY_DAYS) - 1)])
        doc["state"] = "partial" if doc["accepted"] else "exhausted"
        if doc["accepted"]:
            self._activate(run, w, partial=True)
            doc["stuck"] = bool(doc.get("cited", 0) >= MAX_ACCEPTED or self._filmed(run.pid, w["id"]))
        doc["next"] = iso(self.now() + delay)

    # ------------------------------------------------------------ the audit of pages cited before

    def _audit(self, run: Run, raw: dict) -> bool:
        """Judge the pages the skills already cite with the same two models, page by page: a report for Studio.
        A skill whose cited pages both models refuse is searched again (its old lesson stays until the new one exists)."""
        side = run.books.side("audit", None)
        if not isinstance(side, dict) or side.get("v") != VERIFIER_VERSION:
            side = {"v": VERIFIER_VERSION, "skills": {}}
        now = self.now()
        legacy = [s for s in self.skills_of(raw) if s["sources"] and not run.books.skill(s["key"]).get("accepted")
                  and run.books.skill(s["key"])["state"] == "searching"]
        todo = [s for s in legacy if s["key"] not in side["skills"]
                or (side["skills"][s["key"]].get("verdict") == "invalid" and not side["skills"][s["key"]].get("confirmed")
                    and parse_iso(side["skills"][s["key"]]["wait_until"]) <= now)][:AUDIT_PER_STEP]
        if not todo:
            return False
        tries = side.setdefault("tries", {})
        for s in todo:
            ctx = self._context(raw, s)
            if tries.get(s["key"], 0) >= AUDIT_TRIES:
                side["skills"][s["key"]] = {"verdict": "unchecked", "pages": [], "at": iso(), "error": "the check failed three times"}
                tries.pop(s["key"], None)
                run.books.save_side("audit", side)
                continue
            tries[s["key"]] = tries.get(s["key"], 0) + 1
            run.books.save_side("audit", side)   # the try is written down before the paid call: a failing check ends
            pages = [{"url": r["url"], "title": r.get("title") or r["url"]} for r in s["sources"]]
            results = run.verifier.grade_pages(ctx, pages, both_all=True)
            tries.pop(s["key"], None)
            verdicts, verdict = audit_verdicts(pages, results)   # invalid: both models read every page and gave each no grade, not a failed quote, not a split
            before = side["skills"].get(s["key"])
            entry = {"verdict": verdict, "pages": verdicts, "at": iso()}
            if verdict == "invalid":
                entry["wait_until"] = iso(now + timedelta(hours=CONFIRM_HOURS))
            if verdict == "invalid" and before and before.get("verdict") == "invalid":
                # Refused twice, a day apart. Only now is the skill put back to the search, a few a day.
                today = now.date().isoformat()
                put_back = side.setdefault("put_back", {})
                if put_back.get(today, 0) < REPLACE_PER_DAY:
                    put_back[today] = put_back.get(today, 0) + 1
                    entry["confirmed"] = True
                    doc = run.books.skill(s["key"])
                    doc["replacing"] = [p["url"] for p in pages]
                    run.books.save_skill(doc)
            side["skills"][s["key"]] = entry
            run.books.save_side("audit", side)
        return True

    # ------------------------------------------------------------ what Studio and the exam page show

    def status(self, pid: str) -> dict:
        raw = self.store.read("packages", pid, default=None)
        if not isinstance(raw, dict):
            return {}
        counts: Counter = Counter()
        skills = []
        settled = 0   # partly covered skills that cannot gain another page: the search is done with them
        audit = self.store.read("sourcing", pid, "side-audit", default=None) or {}
        for s in self.skills_of(raw):
            doc = self.store.read("sourcing", pid, s["key"], default=None)
            doc = doc if isinstance(doc, dict) and doc.get("key") == s["key"] else None
            state = doc["state"] if doc else ("legacy" if s["sources"] else "searching")
            if doc and doc.get("replacing") and doc["state"] == "searching":
                state = "searching"
            counts[state] += 1
            settled += bool(doc and doc["state"] == "partial" and doc.get("stuck"))
            skills.append({"id": s["id"], "text": s["text"], "domain": s["domain"], "group": s["group"], "state": state,
                           "round": doc["round"] if doc else 0, "sources": len(s["sources"]),
                           "accepted": [{k: a[k] for k in ("url", "title", "grade", "by", "rank")} | {"gate": a["gate"]["quote"], "author": a["author"]["quote"],
                                                                                                     "cited": a in cited}
                                        for cited in [self._cited(s, doc)] for a in (doc or {}).get("accepted", [])],
                           "next": (doc or {}).get("next"), "blocked": (doc or {}).get("blocked"),
                           "failure": (doc or {}).get("failure"), "replacing": bool((doc or {}).get("replacing")),
                           "audit": self._audit_row(audit, s)})
        index = self.store.read("sourcing", pid, default=None) or {}
        total = sum(counts.values())
        missing = sum(1 for k in skills if k["sources"] == 0)
        incomplete = total - counts["covered"] - counts["legacy"] - settled
        return {"package": pid, "skills": total, "counts": dict(counts), "missing": missing, "incomplete": incomplete,
                "settled": settled, "complete": incomplete == 0, "calls_today": int((index.get("calls") or {}).get(utcnow().date().isoformat()) or 0),
                "generation": int(index.get("generation") or 0), "note": self.state.get("note"), "details": skills}

    @staticmethod
    def _audit_row(audit: dict, s: dict) -> dict | None:
        """What the audit said of a skill's pages, and whether the skill still cites them: once they were replaced, the
        verdict is history and no longer counts."""
        entry = (audit.get("skills") or {}).get(s["key"])
        judged = [p["url"] for p in (entry or {}).get("pages") or [] if isinstance(p, dict) and p.get("url")]
        if not judged:
            return entry
        return {**entry, "current": _cites(s["sources"], judged)}

    @staticmethod
    def _cited(s: dict, doc: dict | None) -> list[dict]:
        """The accepted pages a lesson is written from: those the package cites now; for a skill the package has no
        pages for yet, those it will cite (the best three)."""
        if not doc:
            return []
        if s["sources"]:
            return [a for a in doc["accepted"] if any(same_page(a["url"], r.get("url")) for r in s["sources"])]
        return best(doc)

    def activations(self, pid: str, since: Any) -> list[dict]:
        """When the search gave skills their pages since `since`, newest first, for Studio's activity: the skill, how
        far it got (covered or partly), and how many pages its package cites now."""
        raw = self.store.read("packages", pid, default=None)
        if not isinstance(raw, dict):
            return []
        out = []
        for s in self.skills_of(raw):
            doc = self.store.read("sourcing", pid, s["key"], default=None)
            at = ((doc or {}).get("activated") or {}).get("at") if isinstance(doc, dict) and doc.get("key") == s["key"] else None
            if at and parse_iso(at) >= since:
                out.append({"at": at, "skill": s["text"], "state": doc["state"], "pages": len(s["sources"])})
        return sorted(out, key=lambda x: x["at"], reverse=True)

    def summary(self, pid: str) -> dict:
        """The numbers the exam page shows. Every skill's document is read to make them, and the page asks every few
        seconds while the search runs, so they are kept for a few seconds."""
        hit = self._summaries.get(pid)
        if hit and time.monotonic() - hit[0] < SUMMARY_S:
            return hit[1]
        s = self.status(pid)
        out = {k: s.get(k) for k in ("missing", "incomplete", "complete")} if s else {}
        self._summaries[pid] = (time.monotonic(), out)
        return out

    def wait_s(self) -> float:
        """Seconds until the search has work again, 15 to 1800: what the preparer sleeps when nothing was to do. A
        step that failed is tried again after its pause, a skill that waits for its next cycle at its date."""
        now = self.now()
        soonest = 1800.0
        far = now + timedelta(days=36500)
        try:
            for pid in self.exams():
                raw = self.store.read("packages", pid, default=None)
                if not isinstance(raw, dict):
                    continue
                index = self.store.read("sourcing", pid, default=None) or {}
                if int((index.get("calls") or {}).get(utcnow().date().isoformat()) or 0) >= DAY_CALLS:
                    continue   # the day's calls are used: nothing is due before tomorrow
                for s in self.skills_of(raw):
                    doc = self.store.read("sourcing", pid, s["key"], default=None)
                    if not (isinstance(doc, dict) and doc.get("key") == s["key"]):
                        if not s["sources"]:
                            return 15.0
                        continue
                    if not needs_work(doc, bool(s["sources"]), far):
                        continue
                    if due(doc, now):
                        return 15.0
                    soonest = min(soonest, (parse_iso(doc["next"]) - now).total_seconds())
                audit = self.store.read("sourcing", pid, "side-audit", default=None) or {}
                for entry in (audit.get("skills") or {}).values():
                    if entry.get("verdict") == "invalid" and not entry.get("confirmed") and entry.get("wait_until"):
                        soonest = min(soonest, (parse_iso(entry["wait_until"]) - now).total_seconds())
        except Exception:  # noqa: BLE001 - the pause is only a preference
            return 60.0
        return max(15.0, min(1800.0, soonest))

    def diag(self) -> dict:
        return {"current": self.state["current"], "last": self.state["last"], "worked": self.state["worked"],
                "note": self.state["note"], "exams": self.exams(), "web_search": self.web_state()}

    # ------------------------------------------------------------ is web search working? (cached, never at startup)

    def web_state(self) -> dict | None:
        """The last web search self-test: {at, ok, reason}, or None when it was not made yet."""
        doc = self.store.read("sourcing", "web-check", default=None)
        return doc if isinstance(doc, dict) else None

    def check_web(self) -> None:
        """One web search a day, from the background, so Studio can say whether R4 works. Never at startup."""
        doc = self.web_state()
        if doc and utcnow() - parse_iso(doc["at"]) < timedelta(hours=WEB_CHECK_H):
            return
        try:
            found = self.dojo.ai.web_search("Azure SQL Database overview", domains=("learn.microsoft.com",), max_searches=1)
            doc = {"at": iso(), "ok": bool(found["urls"]), "reason": "" if found["urls"] else "the search returned no page"}
        except (AIError, UserError) as e:
            doc = {"at": iso(), "ok": False, "reason": str(e)[:200]}
        self.store.write("sourcing", "web-check", value=doc)
