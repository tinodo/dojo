"""The two-model check of the pages a course already cites (docs/adr/0019-studio-overview.md).

Dojo's own search gives every page it finds a check by two model families (ADR 0018). The original courses were put together
before it existed: each skill cites one to three official pages, and nobody has asked whether a page teaches its skill. This
asks, as a report. Each skill gets a verdict: `supported` (a page both models accept), `weak` (none, but not every page was
refused) or `invalid` (both models refuse every page). It changes nothing: no source, no lesson, no film, no question.

It runs when the owner asks, with the price shown, as a job. What was judged is written after every skill, so a run that
stops (a restart, a failed call, the limit) loses nothing and the next run goes on with the skills left. A verdict is kept
with the skill's words and pages it was made for: when either changes, the skill has no verdict again. A page that could
not be read is not judged either way (its skill is left to the next run, unless another page of it was accepted). A page
that now leads to another page is not credited to the address the course cites: the report says where it went.

Reading a page refreshes Dojo's stored copy of it, as every read of a cited page does (the lessons and the drift check read
them too); nothing else of the course is written."""
from __future__ import annotations

import logging
import threading
from collections import Counter
from typing import Any

from .core import UserError, iso, sha256
from .sources import same_page
from .sourcing.ladder import audit_verdicts
from .sourcing.verify import VERIFIER_VERSION, Verifier

log = logging.getLogger("dojo.pagecheck")

KIND = "pagecheck"
MAX_CALLS = 1500          # model calls in one run: about six for a skill, far more than a course needs
SHOWN = 60                # skills listed with their pages at most: a course has fewer than this that are not supported


class _Calls:
    """Counts every model call of a run, and stops the run at MAX_CALLS."""

    def __init__(self, cap: int):
        self.cap, self.n, self._lock = cap, 0, threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            self.n += 1
            if self.n > self.cap:
                raise UserError("The page check stopped at its limit of model calls. What was judged is kept: start it again to go on.")


class PageCheck:
    def __init__(self, dojo: Any, owner: Any, costs: Any):
        self.dojo, self.owner, self.costs = dojo, owner, costs
        self._lock = threading.Lock()

    def _doc(self, pid: str) -> dict:
        doc = self.dojo.store.read("page-check", pid, default=None)
        if not isinstance(doc, dict) or doc.get("v") != VERIFIER_VERSION or not isinstance(doc.get("skills"), dict):
            return {"package": pid, "v": VERIFIER_VERSION, "skills": {}, "started": None, "finished": None}
        return doc

    def _skills(self, pid: str) -> list[dict]:
        return [s for s in self.dojo.packages.get(pid)["skills"].values() if s["sources"]]

    @staticmethod
    def _sig(skill: dict) -> str:
        """What a verdict was made for: the skill's words and the addresses of the pages it cites."""
        return sha256("|".join([skill["text"], *sorted(r["url"] for r in skill["sources"])]))[:16]

    def _judged(self, skills: list[dict], doc: dict) -> dict[str, dict]:
        """The verdicts that are still those of the skills as they are now."""
        return {s["id"]: doc["skills"][s["id"]] for s in skills if doc["skills"].get(s["id"], {}).get("sig") == self._sig(s)}

    def view(self, pid: str) -> dict:
        """For Studio: how far the check is and what it found. The price is that of the pages still to check."""
        skills = self._skills(pid)
        doc = self._doc(pid)
        judged = self._judged(skills, doc)
        todo = [s for s in skills if s["id"] not in judged]
        counts = Counter(v.get("verdict") for v in judged.values())
        running = bool(self.dojo.jobs.existing(self.owner, KIND, pid))
        state = "running" if running else "none" if not judged else "partial" if todo else "done"
        titles = {r["url"]: r.get("title") or r["url"] for s in skills for r in s["sources"]}
        weak = sorted(((s, judged[s["id"]]) for s in skills if judged.get(s["id"], {}).get("verdict") in ("weak", "invalid")),
                      key=lambda x: (x[1]["verdict"] != "invalid", x[0]["id"]))
        pages_left = sum(len(s["sources"]) for s in todo)
        price = None
        if todo:
            try:
                est = self.costs.pagecheck_estimate(pages_left)
                price = {"pages": pages_left, "total": est.get("total"), "complete": bool(est.get("complete")), "currency": est.get("currency") or "USD"}
            except Exception:  # noqa: BLE001 - the price is a line beside the button, never a reason to lose the page
                price = {"pages": pages_left, "total": None, "complete": False, "currency": "USD"}
        return {"state": state, "skills": len(skills), "checked": len(judged), "unchecked": len(todo),
                "supported": counts.get("supported", 0), "weak": counts.get("weak", 0), "invalid": counts.get("invalid", 0),
                "finished": doc.get("finished"), "price": price,
                "not_supported": [{"id": s["id"], "text": s["text"], "verdict": v["verdict"], "at": v.get("at"),
                                   "pages": [{"url": p["url"], "title": titles.get(p["url"], p["url"]), "grade": p.get("grade"),
                                              "why": p.get("why"), "moved_to": p.get("moved_to")} for p in v.get("pages") or []]}
                                  for s, v in weak[:SHOWN]]}

    def start(self, learner: Any, pid: str) -> dict:
        """Starts the check of a course's pages as the owner's job. Asking again while it runs returns the same job."""
        if self.dojo.packages.is_added(pid):
            raise UserError("The pages of an added exam are checked by Dojo's own search: Studio shows how that stands.")
        self.dojo.packages.get(pid)
        running = self.dojo.jobs.existing(learner, KIND, pid)
        if running:
            return running
        return self.dojo.jobs.start(learner, KIND, self.run, pid, ref=pid, busy_message="This course's pages are being checked already.")

    def run(self, progress: Any, pid: str) -> dict:
        """Judges the skills that have no verdict yet, one at a time, and writes each verdict down at once."""
        pkg = self.dojo.packages.get(pid)
        meta, skills = pkg["meta"], self._skills(pid)
        with self._lock:
            doc = self._doc(pid)
        todo = [s for s in skills if s["id"] not in self._judged(skills, doc)]
        doc["started"] = doc.get("started") or iso()
        verifier = Verifier(self.dojo, count=_Calls(MAX_CALLS))
        unread = 0
        for k, s in enumerate(todo, 1):
            progress(f"Checking the pages of skill {k} of {len(todo)}")
            ctx = {"code": meta["exam"], "title": meta["title"], "domain": s.get("domain_title") or "", "group": s.get("group") or "", "text": s["text"]}
            pages = [{"url": r["url"], "title": r.get("title") or r["url"]} for r in s["sources"]]
            results = verifier.grade_pages(ctx, pages, both_all=True)
            for i, (page, res) in enumerate(zip(pages, results)):
                if res.get("url") and not same_page(page["url"], res["url"]):   # it now leads to another page: that page was judged, not this address
                    results[i] = {"state": "rejected", "grade": None, "why": "moved", "gate": None, "author": None, "moved_to": res["url"]}
            verdicts, verdict = audit_verdicts(pages, results)
            if verdict != "supported" and any(v["state"] == "unread" for v in verdicts):
                unread += 1   # a page that could not be read says nothing about the skill: it is judged on the next run
                continue
            doc["skills"][s["id"]] = {"verdict": verdict, "pages": verdicts, "at": iso(), "sig": self._sig(s)}
            self._save(pid, doc)
        if len(self._judged(skills, doc)) == len(skills):
            doc["finished"] = iso()
            self._save(pid, doc)
        return {"package": pid, "checked": len(todo) - unread, "unread": unread}

    def _save(self, pid: str, doc: dict) -> None:
        with self._lock:
            self.dojo.store.write("page-check", pid, value=doc)
