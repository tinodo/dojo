"""Successive relearning: when Dojo asks about a skill again (ADR 0011).

Everything here is derived from the Record each time, by pure functions, so there is no stored schedule
that could drift out of step with the events. The intervals are scheduling, never evidence: a recall
answer is an ordinary check item and fills a pip only under the existing rules (app/evidence.py).

- The first success (no help, every point met, not disputed) starts the schedule: due the next day.
- A success on or after the due day moves one step up INTERVALS. A success before it changes nothing.
- A miss (any judged open answer with a point not met, disputed or not) resets it: due the next day.
- Relearned: RELEARNED_DAYS on-time successes on different days since the last miss.
- Days are calendar days in the Plan's zone (Europe/Berlin)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Collection

from .core import parse_iso
from .plan import to_local

INTERVALS = (1, 3, 7, 16, 35)   # days; ADR 0011 says why
RECALL_MINUTES = 2              # about two minutes a recall
DAY_MINUTES = 10                # about ten minutes of recalls a day at most
DAY_CAP = DAY_MINUTES // RECALL_MINUTES
RELEARNED_DAYS = 3
OPEN_MODES = ("probe", "practice", "check")


def minutes(n: int) -> int:
    """How long n recalls take, as Today, the recall check and the Plan all say it."""
    return RECALL_MINUTES * n


def local_day(when: str | datetime) -> date:
    return to_local(parse_iso(when) if isinstance(when, str) else when).date()


def is_success(a: dict) -> bool:
    return bool(a.get("unaided") and a.get("all_met") and not a.get("disputed"))


def is_miss(a: dict) -> bool:
    """A missing or disputed result is never read in the learner's favour: a disputed miss still brings
    the skill back sooner. An answer with nothing to judge is neither."""
    return a.get("mode") in OPEN_MODES and int(a.get("total") or 0) > 0 and not a.get("all_met")


def schedule(attempts: list[dict]) -> dict:
    """The schedule of one skill from its judged open answers (evidence attempts), in time order.

    Returns step (None before the first success), due (a date or None), on_time (the days of on-time
    successes since the last miss), on_time_at (their times), relearned, relearned_at (the first time it was
    reached, kept after a later miss) and relearned_item (the item of that answer, so a reader can find the
    exact answer in the Record, not only its whole-second time), repair (the item of the miss that last reset
    it, until the next success), started."""
    step: int | None = None
    due: date | None = None
    on_time: list[date] = []
    on_time_at: list[str] = []
    relearned_at: str | None = None
    relearned_item: str | None = None
    repair: str | None = None
    started: str | None = None
    last_success: str | None = None
    for a in sorted(attempts, key=lambda x: x["at"]):
        day = local_day(a["at"])
        if is_success(a):
            last_success = a["at"]
            repair = None
            if step is None:
                step, due, started = 0, day + timedelta(days=INTERVALS[0]), a["at"]
            elif due is not None and day >= due:
                step = min(step + 1, len(INTERVALS) - 1)
                due = day + timedelta(days=INTERVALS[step])
                if day not in on_time:
                    on_time.append(day)
                    on_time_at.append(a["at"])
                if len(on_time) >= RELEARNED_DAYS and relearned_at is None:
                    relearned_at, relearned_item = a["at"], a.get("item")
            # Before the due day: kept in the Record, but cramming does not count as spacing.
        elif is_miss(a) and step is not None:
            step, due, on_time, on_time_at, repair = 0, day + timedelta(days=INTERVALS[0]), [], [], a.get("item")
    return {"step": step, "due": due, "on_time": on_time, "on_time_at": on_time_at,
            "relearned": len(on_time) >= RELEARNED_DAYS, "relearned_at": relearned_at,
            "relearned_item": relearned_item, "repair": repair, "started": started, "last_success": last_success}


def eligible(package: dict, sid: str, e: dict, gone: Collection[str] = ()) -> bool:
    """A skill a recall question can be written for, and that is not waiting for its later check: the
    later check is its first recall, and it is already in Today, the Plan and the spoken check."""
    if not package["skills"][sid]["sources"] or sid in gone:
        return False
    return not waiting_later(e)


def waiting_later(e: dict) -> bool:
    """Met without help but not yet later after its teaching: its later check comes first (evidence.due_later).
    A skill never taught in Dojo has no later check to wait for."""
    return bool(e["checks"]["unaided"] and not e["checks"]["later"] and e.get("last_teaching"))


def due_recalls(package: dict, evidence: dict[str, dict], today: date, gone: Collection[str] = ()) -> list[dict]:
    """Every skill due today or earlier, oldest due first, then heaviest in the exam. Overdue is simply due."""
    skills = package["skills"]
    out = []
    for sid, e in evidence.items():
        if not eligible(package, sid, e, gone):
            continue
        sch = schedule(e["attempts"])
        if sch["due"] is None or sch["due"] > today:
            continue
        s = skills[sid]
        out.append({"skill": sid, "text": s["text"], "domain": s["domain_title"], "kind": s["kind"],
                    "weight": round(s["share"] * 100, 1), "share": s["share"], "due": sch["due"].isoformat(),
                    "step": sch["step"], "repair": sch["repair"], "relearned": sch["relearned"],
                    "last_success": sch["last_success"]})
    out.sort(key=lambda r: (r["due"], -r["share"], r["skill"]))
    return out


def upcoming(package: dict, evidence: dict[str, dict], gone: Collection[str] = ()) -> list[tuple[date, float, str]]:
    """(due day, -share, skill) for every skill with a schedule, for the Plan."""
    out = []
    for sid, e in evidence.items():
        if not eligible(package, sid, e, gone):
            continue
        sch = schedule(e["attempts"])
        if sch["due"] is not None:
            out.append((sch["due"], -package["skills"][sid]["share"], sid))
    return sorted(out)


def done_on(events: list[dict], day: date) -> int:
    """Recall answers sent on that day, in every exam."""
    return sum(1 for ev in events if ev.get("type") == "answer.submitted"
               and (ev.get("data") or {}).get("recall") is True and local_day(ev["at"]) == day)


def check_sessions(store: Any, learner_key: str) -> list[dict]:
    """Every check session of a learner, in every exam (kept by app/checks.py under the learner)."""
    docs = (store.read("learners", learner_key, "checks", n) for n in store.names("learners", learner_key, "checks"))
    return [d for d in docs if d]


def _reserved(q: dict) -> bool:
    """A recall question holds its place in the day's budget from the moment its check starts. Only one
    that ended before any writer was given it frees its place again: nothing was spent on it."""
    return q.get("picked") == "recall" and not (q.get("state") == "skipped" and not q.get("job") and not q.get("item"))


def used_on(events: list[dict], sessions: list[dict], day: date) -> int:
    """The day's recall budget used, in every exam: the recall questions of the checks started that day,
    written, being written or waiting for a writer, answered or not, plus recall answers sent that day to
    a question of an earlier day. Counting the questions, not only the answers, means ending a check
    unanswered and starting another does not write five more."""
    items: set[str] = set()
    n = 0
    for doc in sessions:
        if not doc.get("created") or local_day(doc["created"]) != day:
            continue
        for q in doc.get("questions") or []:
            if _reserved(q):
                n += 1
                if q.get("item"):
                    items.add(q["item"])
    n += sum(1 for ev in events if ev.get("type") == "answer.submitted"
             and (ev.get("data") or {}).get("recall") is True and local_day(ev["at"]) == day
             and (ev.get("data") or {}).get("item") not in items)
    return n


def left_today(events: list[dict], today: date, sessions: list[dict] = ()) -> int:
    return max(0, DAY_CAP - used_on(events, list(sessions), today))


def waiting_in_checks(events: list[dict], sessions: list[dict], package: str, now: datetime, open_hours: float) -> int:
    """Recall questions of this exam in a check still open today and not answered yet: their place in the
    budget is taken, and Today still shows them, as the learner goes back to that check to answer them."""
    answered = {(ev.get("data") or {}).get("item") for ev in events if ev.get("type") == "answer.submitted"}
    today = local_day(now)
    n = 0
    for doc in sessions:
        created = doc.get("created")
        if (doc.get("package") != package or doc.get("state") == "ended" or not created
                or local_day(created) != today or (now - parse_iso(created)).total_seconds() > open_hours * 3600):
            continue
        n += sum(1 for q in doc.get("questions") or [] if q.get("picked") == "recall"
                 and q.get("state") in ("waiting", "writing", "ready") and q.get("item") not in answered)
    return n


def spread(due: list[tuple[date, Any]], first: date, days: int, cap_today: int,
           before: Callable[[Any], date | None] | None = None) -> dict[date, list[Any]]:
    """Puts what is due onto days, at most DAY_CAP a day (cap_today on the first), oldest first. What does
    not fit waits for the next day: a backlog becomes a few short days, never one long one. `before` gives
    the day an item must come before (its exam day): one that can no longer come before it is dropped
    before the day's places are handed out, so it never takes a place another exam's recall could use."""
    queue = sorted(due, key=lambda x: x[0])
    out: dict[date, list[Any]] = {}
    for i in range(days):
        d = first + timedelta(days=i)
        if before is not None:
            queue = [x for x in queue if (b := before(x[1])) is None or d < b]
        cap = cap_today if i == 0 else DAY_CAP
        ready = [x for x in queue if x[0] <= d][:cap]
        for x in ready:
            queue.remove(x)
        if ready:
            out[d] = [x[1] for x in ready]
    return out


def log_line(learner_key: str, attempts_before: list[dict], attempt: dict, confidence: str | None, recall: bool,
             package: str, skill: str) -> dict:
    """One line of the recall log (ADR 0011): the fields a later scheduler such as FSRS needs."""
    before = schedule(attempts_before)
    after = schedule(attempts_before + [attempt])
    previous = max((a["at"] for a in attempts_before), default=None)
    days = None
    if previous:
        days = round((parse_iso(attempt["at"]) - parse_iso(previous)).total_seconds() / 86400, 3)
    met, total = int(attempt.get("met") or 0), int(attempt.get("total") or 0)
    result = "met" if total and met == total else "partly" if met else "missed"
    return {"at": attempt["at"], "learner": learner_key, "package": package, "skill": skill, "item": attempt.get("item"),
            "mode": attempt.get("mode"), "recall": bool(recall), "result": result, "met": met, "total": total,
            "unaided": bool(attempt.get("unaided")), "confidence": confidence, "days_since_previous": days,
            "step_before": before["step"], "step_after": after["step"],
            "due_before": before["due"].isoformat() if before["due"] else None}
