"""The 3-minute check: a short session where Dojo asks out loud and the learner answers out loud.

Dojo picks up to three skills that are due, writes one **check** item for each in the background,
reads the question in the coach's voice and takes the answer spoken or typed. Nothing new is recorded:
these are ordinary check items, so their answers reach the Record exactly as any other answer does,
with the same conditions. Reading the question out loud is not teaching - it is the question - so
nothing is written down for it, and the skill's lesson is not shown during the session.

Only the question is ever spoken or sent to the browser before the answer: the rubric, the model
answer, the hints and the sources stay on the server until the answer has been judged.

The answers go through the ordinary answer route, so when the podcast feed has offered a skill's
episode, a check asks the same "did you listen in a podcast app?" question first (app/podcast.py).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Collection

from fastapi import HTTPException

from . import relearn
from .core import Abandoned, Learner, Spend, UserError, current_spend, iso, new_id, parse_iso, spending, utcnow

log = logging.getLogger("dojo.checks")

# Three questions, three minutes: the timer is a guide, never a limit, and the items themselves are
# untimed. A session that is still open after this long is treated as yesterday's.
LIMIT = 3
MINUTES = 3
OPEN_HOURS = 2
# The question is read by the guide voice, the one labelled "Coach · Harry" in the app.
QUESTION_VOICE = "guide"
POLL_S = 1.0
GIVE_UP_S = 900.0
SKIPPED = ("Dojo could not write a fair, new, source-checked question for this skill just now, so it "
           "is left out of this check. The studio log says why.")
STOPPED = "The check ended before this question was written, so Dojo stopped writing it."
LOST = ("Dojo stopped while this question was being written, so it is left out of this check. "
        "Nothing was recorded for it.")
NOTE = ("Each question is a check: no hints, no lesson, one answer. Dojo judges your answers in the "
        "background and the result appears in your Record. Nothing here predicts an exam result.")


def _ago(when: datetime, now: datetime) -> str:
    hours = max(0, int((now - when).total_seconds() // 3600))
    if hours < 48:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // 24
    return f"{days} day{'' if days == 1 else 's'} ago"


def _weight(share: float) -> float:
    return round(share * 100, 1)


def pick(package: dict, evidence: dict[str, dict], now: datetime, later_hours: int, limit: int = LIMIT,
         gone: Collection[str] = ()) -> list[dict]:
    """The skills this check asks about, best first, each with the reason in plain words.

    First the skills that would earn "later" now: met on your own, taught more than `later_hours` ago
    and not yet shown later. Then the skills that have never been tried, heaviest in the exam first.
    Ties go to the lower skill id, so the same record always picks the same skills. A skill no question
    can be written for is left out: an added exam's skill with no verified source, and, while it lasts,
    one whose every cited page is gone (`gone`, ADR 0006)."""
    skills = package["skills"]
    due: list[tuple[str, str]] = []
    untested: list[tuple[float, str]] = []
    for sid, e in evidence.items():
        if not skills[sid]["sources"] or sid in gone:
            continue  # nothing can be written for it (see above)
        earned = e["checks"]["unaided"] or e["checks"]["later"]
        if e["checks"]["unaided"] and not e["checks"]["later"] and e["last_teaching"]:
            taught = parse_iso(e["last_teaching"])
            if now >= taught + timedelta(hours=later_hours):
                due.append((e["last_teaching"], sid))
        elif not e["attempts"] and not earned:
            untested.append((-skills[sid]["share"], sid))
    due.sort()
    untested.sort()
    picks = []
    for last_teaching, sid in due:
        s = skills[sid]
        picks.append({"skill": sid, "text": s["text"], "domain": s["domain_title"], "kind": s["kind"],
                      "weight": _weight(s["share"]), "picked": "due_later",
                      "why": f"You met every point on your own, and the last teaching was "
                             f"{_ago(parse_iso(last_teaching), now)}. That is more than {later_hours} hours, so "
                             "answering now shows you still know it later."})
    for _, sid in untested:
        s = skills[sid]
        picks.append({"skill": sid, "text": s["text"], "domain": s["domain_title"], "kind": s["kind"],
                      "weight": _weight(s["share"]), "picked": "untested",
                      "why": f"Not tested yet. It carries about {_weight(s['share'])}% of the exam, "
                             "and nothing in your record says you can do it."})
    return picks[:limit]


def _minutes(picks: list[dict], recall: bool) -> int:
    return max(1, relearn.minutes(len(picks))) if recall else MINUTES


def _recall_pick(r: dict, now: datetime) -> dict:
    if r["repair"]:
        why = ("Not right last time, so it is due again today. Look at the lesson section first if you like, "
               "then answer a new question.")
    elif r["last_success"]:
        why = (f"You got it right {_ago(parse_iso(r['last_success']), now)}. Recalling it again now, on a later day, "
               "helps it last.")
    else:
        why = "Due for a recall today. Recalling it on a later day helps it last."
    return {"skill": r["skill"], "text": r["text"], "domain": r["domain"], "kind": r["kind"], "weight": r["weight"],
            "picked": "recall", "why": why}


class CheckRoom:
    """Runs one 3-minute check at a time, per learner."""

    def __init__(self, dojo: Any):
        self.dojo = dojo
        self.store = dojo.store
        # The sessions this process is writing questions for. A filler thread does not survive a
        # restart, so a session that is not in here has nobody left to finish it.
        self._filling: set[str] = set()

    # ---------------------------------------------------------------- storage

    def _path(self, learner: Learner) -> tuple[str, str, str]:
        return ("learners", learner.key, "checks")

    def _doc(self, learner: Learner, cid: str) -> dict:
        try:
            doc = self.store.read(*self._path(learner), cid)
        except ValueError:
            doc = None
        if not doc:
            raise HTTPException(404, "No such check.")
        return doc

    def _save(self, learner: Learner, doc: dict) -> None:
        self.store.write(*self._path(learner), doc["id"], value=doc)

    def _change(self, learner: Learner, cid: str, index: int, only_from: tuple[str, ...] | None = None, **fields: Any) -> None:
        """Change one question. `only_from` keeps a slow writer from undoing a question that is already
        ready: the job that writes it and the loop that watches the job both change the same line."""
        with self.store.lock:
            try:
                doc = self._doc(learner, cid)
            except HTTPException:  # the record was deleted while this was running
                return
            q = doc["questions"][index]
            if only_from is not None and q["state"] not in only_from:
                return
            q.update(fields)
            if doc["state"] == "getting_ready" and any(x["state"] == "ready" for x in doc["questions"]):
                doc["state"] = "running"
            self._save(learner, doc)

    def sessions(self, learner: Learner, pid: str) -> list[dict]:
        docs = (self.store.read(*self._path(learner), n) for n in self.store.names(*self._path(learner)))
        return sorted((d for d in docs if d and d.get("package") == pid), key=lambda d: d["created"], reverse=True)

    def open_session(self, learner: Learner, pid: str) -> dict | None:
        """The check the learner is in the middle of, if there is one: not ended, started in the last
        couple of hours, and with a question still to answer."""
        fresh = utcnow() - timedelta(hours=OPEN_HOURS)
        for doc in self.sessions(learner, pid):
            if doc.get("state") == "ended" or parse_iso(doc["created"]) < fresh:
                continue
            if any(q["state"] in ("waiting", "writing") and not self._lost(q, doc) for q in doc["questions"]):
                return doc
            if any(q["state"] == "ready" and not self._answered(learner, q) and not self._withheld(learner, q)
                   for q in doc["questions"]):
                return doc
        return None

    def _withheld(self, learner: Learner, q: dict) -> str | None:
        """Why a written question may not be answered: a source page changed and no longer has the words
        its rubric came from (ADR 0006). It is shown as skipped, with that reason."""
        if not q.get("item"):
            return None
        try:
            return self.dojo.withheld_note(self.dojo.item(learner, q["item"]))
        except HTTPException:
            return None

    def _lost(self, q: dict, doc: dict) -> bool:
        """A question nobody is writing any more. The loop that would have marked it skipped lives in
        this process, so a restart leaves questions stranded: if no loop of ours is on this session,
        anything still waiting or being written is lost. The job document says so too."""
        if q["state"] not in ("waiting", "writing"):
            return False
        if doc["id"] not in self._filling:
            return True
        if q["state"] == "writing":
            job = (self.store.read("jobs", q["job"]) or {}) if q.get("job") else {}
            return job.get("state") not in ("queued", "running", "done")
        return False

    def _answered(self, learner: Learner, q: dict) -> bool:
        if not q.get("item"):
            return False
        try:
            return bool(self.dojo.item(learner, q["item"]).get("answer"))
        except HTTPException:
            return False

    # ---------------------------------------------------------------- starting

    def _gone(self, pid: str) -> set[str]:
        """The skills whose every cited page is gone (ADR 0006): no question can be written for them now."""
        drift = getattr(self.dojo, "drift", None)
        return drift.gone_skills(pid) if drift else set()

    def _picks(self, learner: Learner, pid: str, recall: bool = False, limit: int | None = None) -> tuple[dict, dict, list[dict]]:
        """Due recalls first, up to what is left of the day's cap (ADR 0011). A recall check asks only
        those. The ordinary check fills up with the skills due for their later check, then untested ones."""
        pkg, profile, ev = self.dojo.evidence(learner, pid)
        now = utcnow()
        gone = self._gone(pid)
        events = self.store.events(learner.key)
        # The day's budget counts the recall questions already given to a check in any exam, not only the
        # answers. create() calls this under the store lock and saves the session before any writer
        # starts, so the places are reserved before a model call is made.
        room = relearn.left_today(events, relearn.local_day(now), relearn.check_sessions(self.store, learner.key))
        cap = min(room, relearn.DAY_CAP if recall else LIMIT)
        if limit is not None:
            cap = min(cap, max(0, limit))
        due = relearn.due_recalls(pkg, ev, relearn.local_day(now), gone)[:cap]
        picks = [_recall_pick(r, now) for r in due]
        if not recall:
            taken = {p["skill"] for p in picks}
            picks += [p for p in pick(pkg, ev, now, profile["later_hours"], LIMIT, gone) if p["skill"] not in taken]
            picks = picks[:LIMIT]
        return pkg, profile, picks

    def plan(self, learner: Learner, pid: str, recall: bool = False) -> dict:
        pkg, profile, picks = self._picks(learner, pid, recall)
        running = self.open_session(learner, pid)
        return {"package": pkg["meta"], "minutes": _minutes(picks, recall), "limit": LIMIT, "picks": picks,
                "recall": recall, "later_hours": profile["later_hours"], "running": running["id"] if running else None,
                "note": NOTE}

    def create(self, learner: Learner, pid: str, recall: bool = False, limit: int | None = None) -> dict:
        """Start a check. One at a time: if the learner is in the middle of one, they go back to it.
        The look and the save happen under one lock, so two taps in the same second cannot start two
        sessions and spend two sets of model calls. `recall` starts one made only of due recalls, and `limit`
        asks fewer of them (the 5-minute session asks two, ADR 0013)."""
        with self.store.lock:
            running = self.open_session(learner, pid)
            if running:
                spend = current_spend()
                if spend is not None and spend.hold is not None:
                    spend.hold.void()   # nothing new was started: the reservation is given back, uncounted
                return self.view(learner, running["id"])
            _, _, picks = self._picks(learner, pid, recall, limit)
            if not picks and recall:
                raise UserError("No recalls are due right now. They come back on the day they are due.")
            if not picks:
                raise UserError("Nothing is due for a 3-minute check right now. Every skill you have met on your own "
                                "has already been shown later. Every other skill has been tried at least once, or has "
                                "no official page to write a question from just now. "
                                "Pick any skill in the course instead.")
            doc = {"id": new_id("check"), "package": pid, "created": iso(), "minutes": _minutes(picks, recall),
                   "state": "getting_ready", "ended": None, "recall": recall,
                   "questions": [{**p, "state": "waiting", "item": None, "audio": "", "note": "", "job": None} for p in picks]}
            self._save(learner, doc)
            # Claimed before the thread runs, so the first look at the session does not think the
            # questions have been abandoned.
            self._filling.add(doc["id"])
        # The writer thread spends from the reservation the route made for this check (ADR 0009, "Money").
        spend = current_spend()
        hold = spend.hold.adopt() if spend is not None and spend.hold is not None else None
        threading.Thread(target=self._fill, args=(learner, doc["id"], hold), name=f"fill-{doc['id']}", daemon=True).start()
        return self.view(learner, doc["id"])

    def end(self, learner: Learner, cid: str) -> dict:
        with self.store.lock:
            doc = self._doc(learner, cid)
            if doc["state"] != "ended":
                doc["state"] = "ended"
                doc["ended"] = iso()
                self._save(learner, doc)
        return self.view(learner, cid, words=False)

    # ---------------------------------------------------------------- writing the questions

    def _fill(self, learner: Learner, cid: str, hold: Any = None) -> None:
        """Write the questions in the background, taking only the job slots nobody else is using, so a
        check never queues up behind itself or pushes everything else out of the way. The learner
        starts on the first question that is ready."""
        try:
            with spending(Spend(learner.key, "check", getattr(hold, "package", None), hold)):
                self._work(learner, cid)
        finally:
            self._filling.discard(cid)
            if hold is not None:
                hold.release()

    def _work(self, learner: Learner, cid: str) -> None:
        try:
            todo = list(range(len(self._doc(learner, cid)["questions"])))
        except HTTPException:
            return
        running: dict[int, str] = {}
        stopped = False
        give_up = time.monotonic() + GIVE_UP_S
        while (todo or running) and time.monotonic() < give_up:
            try:
                if self._doc(learner, cid)["state"] == "ended":  # the learner is done: write no more
                    stopped = True
                    break
            except HTTPException:  # the record was deleted while this was running
                return
            for index, job_id in list(running.items()):
                job = self.store.read("jobs", job_id) or {}
                if job.get("state") in ("queued", "running"):
                    continue
                running.pop(index)
                if job.get("state") != "done":
                    self._change(learner, cid, index, ("waiting", "writing"), state="skipped", note=SKIPPED)
            while todo and len(running) < LIMIT:
                index = todo[0]
                try:
                    # Only a slot nobody is using: the exam pool and the delivery check hold the same
                    # few slots, and a check must never take one of theirs.
                    job = self.dojo.jobs.start(learner, "item", self._one, learner, cid, index,
                                               ref=f"{cid}.{index}", only_if_free=True)
                except Exception:  # noqa: BLE001 - too much is running: wait and try again
                    break
                todo.pop(0)
                running[index] = job["id"]
                self._change(learner, cid, index, ("waiting",), state="writing", job=job["id"])
            time.sleep(POLL_S)
        for index in list(todo) + list(running):
            self._change(learner, cid, index, ("waiting", "writing"), state="skipped", note=STOPPED if stopped else SKIPPED)

    def _one(self, progress: Any, learner: Learner, cid: str, index: int) -> dict:
        if self._stop(learner, cid, index):  # ended while this waited for a slot
            return {"stopped": True}
        doc = self._doc(learner, cid)
        q = doc["questions"][index]
        made = self.dojo.make_item(progress, learner, doc["package"], q["skill"], "check", recall=q.get("picked") == "recall")
        if self._stop(learner, cid, index):  # ended while the question was being written
            return {"stopped": True, "item": made["item"]}
        item = self.dojo.item(learner, made["item"])
        progress("Reading the question out loud")
        audio = self._say(learner, item["stem"])
        # Only from a question still on its way: a question the learner gave up on stays skipped.
        self._change(learner, cid, index, ("waiting", "writing"), state="ready", item=item["id"], audio=audio, note="")
        return {"item": item["id"], "audio": audio}

    def _stop(self, learner: Learner, cid: str, index: int) -> bool:
        """Nothing more to write here: the check has ended, or this question was given up on."""
        try:
            doc = self._doc(learner, cid)
        except HTTPException:  # the record was deleted while this was running
            return True
        return doc["state"] == "ended" or doc["questions"][index]["state"] not in ("waiting", "writing")

    def _say(self, learner: Learner, stem: str) -> str:
        """The question, and nothing else, in the coach's voice. The audio is kept under a hash of what
        is said, exactly like the lesson narration, so the same question is only ever made once. In team
        mode it is kept in the learner's own folder, as the question is theirs (ADR 0009).
        If Speech cannot be reached the session still works: the question is always on the screen."""
        lines = [{"voice": QUESTION_VOICE, "text": stem}]
        try:
            if getattr(self.dojo.settings, "team", False):
                return self.dojo.speech.synthesize(lines, folder=("learners", learner.key, "media"))
            return self.dojo.speech.synthesize(lines)
        except Abandoned:
            raise
        except Exception:  # noqa: BLE001 - never says what the question was
            log.warning("a check question could not be read out loud")
            return ""

    # ---------------------------------------------------------------- what the browser sees

    def view(self, learner: Learner, cid: str, words: bool = True) -> dict:
        """The session as the browser may see it. `words=False` leaves the questions themselves out:
        ending a check is allowed while a timed exam is open, but reading one is not."""
        doc = self._doc(learner, cid)
        questions = [self._question(learner, q, doc, words) for q in doc["questions"]]
        counts = {state: sum(1 for q in questions if q["state"] == state) for state in ("waiting", "writing", "ready", "skipped")}
        return {
            "id": doc["id"], "package": doc["package"], "created": doc["created"], "minutes": doc["minutes"],
            "state": doc["state"], "ended": doc["ended"], "voice": QUESTION_VOICE, "questions": questions,
            "recall": bool(doc.get("recall")),
            "total": len(questions), "ready": counts["ready"], "skipped": counts["skipped"],
            "coming": counts["waiting"] + counts["writing"],
            "answered": sum(1 for q in questions if q["answered"]), "note": NOTE,
        }

    def _question(self, learner: Learner, q: dict, doc: dict, words: bool = True) -> dict:
        """One question as the browser may see it: the words of the question and how it was picked.
        The rubric, the model answer, the hints and the sources are not here - they belong to the
        judgement, which happens after the answer."""
        out = {k: q[k] for k in ("skill", "text", "domain", "kind", "weight", "picked", "why", "state", "note")}
        out.update(item=q["item"], audio=q["audio"] if words else "", stem="", answered=False, step="")
        if q["state"] in ("waiting", "writing"):
            if self._lost(q, doc):  # Dojo was restarted, or the job failed: say so instead of waiting for ever
                gone = doc["id"] not in self._filling
                out.update(state="skipped", note=q["note"] or (LOST if gone else SKIPPED))
            elif q["state"] == "writing" and q.get("job"):
                job = self.store.read("jobs", q["job"]) or {}
                out["step"] = job.get("step") or "Waiting for a free slot"
        if q["item"]:
            item = self.dojo.item(learner, q["item"])
            notice = self.dojo.withheld_note(item)
            if notice:
                out.update(state="skipped", note=notice, audio="")
            elif words:
                out["stem"] = item["stem"]
            out["answered"] = bool(item.get("answer"))
        return out
