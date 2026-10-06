"""The 5-minute session (ADR 0013): today's due recalls first, then one repair of a confident error,
then a clear stop. The nudge opens it. It adds no new kind of evidence: the recalls are an ordinary
recall check (ADR 0011) limited to two questions, and the repair is a lesson section and one new
check item, recorded as they always are."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import HTTPException

from . import push, relearn
from .core import Learner, utcnow

FIVE_RECALLS = 2          # two recalls of about two minutes each
REPAIR_MINUTES = 3        # a lesson section and one short answer
OPEN_CHECK = "You have a check open. Go on with it, or end it and start the 5 minutes."


class FiveMinutes:
    def __init__(self, dojo: Any, checks: Any):
        self.dojo = dojo
        self.checks = checks
        self.store = dojo.store

    def _pick(self, learner: Learner, at: datetime | None = None) -> tuple[str, dict]:
        """The exam: the active one if it has a recall due, else the first that has one, else the active one."""
        active = self.dojo.profile(learner)["active_package"]
        order = [active] + [p for p in self.dojo.packages.by_id if p != active]
        first: tuple[str, dict] | None = None
        for pid in order:
            r = self.dojo.recalls(learner, pid, at=at)
            if first is None:
                first = (pid, r)
            if r["due"]:
                return pid, r
        assert first is not None
        return first

    def _ordinary(self, learner: Learner, pid: str) -> dict | None:
        """An ordinary check that is still open. The 5 minutes never drop the learner into it unasked."""
        running = self.checks.open_session(learner, pid)
        return running if running and not running.get("recall") else None

    def view(self, learner: Learner) -> dict:
        pid, r = self._pick(learner)
        n = min(FIVE_RECALLS, len(r["due"]))
        asked = [{"skill": x["skill"], "text": x["text"]} for x in r["due"][:n]]
        skip = {x["skill"] for x in asked}
        repair = None
        for e in self.dojo.calibration(learner, pid)["errors"]:
            if e["skill"] in skip:
                continue
            repair = {"skill": e["skill"], "text": e["text"], "domain": e["domain"], "label": e["label"],
                      "stem": e["stem"], "where": e["where"]}
            break
        running = self.checks.open_session(learner, pid)
        ordinary = running if running and not running.get("recall") else None
        meta = self.dojo.packages.get(pid)["meta"]
        return {"package": pid, "exam": meta.get("exam") or pid, "day": relearn.local_day(utcnow()).isoformat(),
                "recalls": {"count": n, "minutes": relearn.minutes(n), "skills": asked,
                            "later": max(0, len(r["due"]) - n) + r["waiting"]},
                "repair": repair, "check": running["id"] if running and running.get("recall") else None,
                "open_check": {"id": ordinary["id"], "minutes": ordinary.get("minutes")} if ordinary else None,
                "minutes": relearn.minutes(n) + (REPAIR_MINUTES if repair else 0)}

    def package(self, learner: Learner) -> str:
        """The exam the 5 minutes would be about now: the route reserves for it before starting."""
        return self._pick(learner)[0]

    def start(self, learner: Learner, end_open: bool = False, pid: str | None = None) -> dict:
        """Start the two recalls. An ordinary check that is open is not taken over silently: the learner
        chooses to go on with it, or to end it (`end_open`) and start the 5 minutes. `pid`: the exam the
        route reserved for, so the money and the session are about the same exam."""
        with self.store.lock:
            if pid is None:
                pid, _ = self._pick(learner)
            ordinary = self._ordinary(learner, pid)
            if ordinary is not None:
                if not end_open:
                    raise HTTPException(409, OPEN_CHECK)
                self.checks.end(learner, ordinary["id"])
            return self.checks.create(learner, pid, recall=True, limit=FIVE_RECALLS)

    def nudge(self, learner: Learner, day: date | None = None) -> dict | None:
        """What the nudge for `day` (Berlin) would say, or None when nothing is due. A window that ran over
        midnight belongs to the chosen day, so the recalls and the Plan are judged for that day. Reads
        only: it never refreshes the Plan or writes a recall."""
        at = push.as_of(day, utcnow())
        _, r = self._pick(learner, at)
        n = min(FIVE_RECALLS, len(r["due"]))
        if n:
            return push.recall_message(n, relearn.minutes(n))
        count, minutes = push.planned_today(self.store.read("learners", learner.key, "plan"), at)
        if count:
            return push.plan_message(count, minutes)
        return None