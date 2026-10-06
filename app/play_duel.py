"""Play, duels by invitation (ADR 0010 section 6).

Part of the social layer: team mode only, behind the owner's switch and both members' opt-in. A member invites
someone who turned on "Others can invite me" and studies the same exam. Dojo writes 10 new checked
multiple-choice questions, once, as one quick-practice job of the inviter. Each player gets the questions as a
quick practice of their own (`learners/<key>/rehearsals/<id>`) only when they open the duel; only then are they
marked seen for that player. They never enter a practice-exam pool.

What is stored, in `play/duels/<id>.json`: the two learner keys, the exam (and domain), the questions, the
invitation time (for expiry only), whether the invitation was accepted, each player's quick-practice id and,
once both have finished, the two numbers right. A decline removes the invited member's key and nothing else,
so it looks exactly like a lapse. Deleted 7 days after the invitation, or at once when either player leaves.
No time is ever shown to the other player or used for the result; there is no win, no loss, no record.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from .core import Learner, UserError, iso, new_id, parse_iso, utcnow
from .exam import mark_seen, question_key, seen_keys
from .plan import to_local
from .play_social import MONTHS, person_id

log = logging.getLogger("dojo")

QUESTIONS = 10
HOURS = 48
KEEP_DAYS = 7
RUNNING_EACH = 3        # duels a member has sent that are still running
WRITE_ROUNDS = 2        # a second round writes what the gate held back; then the duel is given up
WRITING_GRACE = timedelta(minutes=2)   # a duel being written with no job behind it after this is given up


def _when(at: datetime) -> str:
    t = to_local(at)
    return f"{t.day} {MONTHS[t.month - 1]}, {t.hour:02d}:{t.minute:02d}"


class Duels:
    def __init__(self, social: Any, dojo: Any):
        self.social = social
        self.dojo = dojo
        self.store = social.store
        social.parts.append(self)

    # ---------------------------------------------------------------- documents

    def _read(self, did: str) -> dict | None:
        d = self.store.read("play", "duels", did)
        return d if isinstance(d, dict) and d.get("id") == did else None

    def _write(self, d: dict) -> None:
        self.store.write("play", "duels", d["id"], value=d)

    def _all(self) -> list[dict]:
        out = []
        now = utcnow()
        with self.store.lock:
            for did in self.store.names("play", "duels"):
                d = self._read(did)
                if d is None:
                    continue
                if parse_iso(d["created"]) + timedelta(days=KEEP_DAYS) <= now or self._orphaned(d, now):
                    self.store.delete("play", "duels", did)
                    continue
                out.append(d)
        return out

    def _orphaned(self, d: dict, now: datetime) -> bool:
        """A duel still being written whose job is gone (Dojo restarted, or the job failed before it began)
        would hold the inviter's limits for a week: it is given up, so the inviter can invite again."""
        if d.get("state") != "writing" or parse_iso(d["created"]) + WRITING_GRACE > now:
            return False
        return not self.dojo.jobs.busy(Learner("", d["a"], ""), "rehearsal", d["id"])

    @staticmethod
    def _deadline(d: dict) -> datetime | None:
        return parse_iso(d["invited"]) + timedelta(hours=HOURS) if d.get("invited") else None

    def _role(self, d: dict, key: str) -> str | None:
        if d.get("a") == key:
            return "a"
        if d.get("b") == key and d.get("state") == "ready":
            return "b"
        return None

    def _mine(self, learner: Learner, did: str) -> tuple[dict, str]:
        d = self._read(did)
        role = self._role(d, learner.key) if d else None
        if role is None or parse_iso(d["created"]) + timedelta(days=KEEP_DAYS) <= utcnow():
            raise HTTPException(404, "No such duel.")
        return d, role

    # ---------------------------------------------------------------- progress, from each player's own quick practice

    def _progress(self, d: dict, role: str) -> dict | None:
        """How far a player is: answers given before the deadline count, held questions cannot be answered."""
        rid = (d.get("rids") or {}).get(role)
        key = d.get(role)
        if not rid or not key:
            return None
        doc = self.store.read("learners", key, "rehearsals", rid, default=None)
        if not isinstance(doc, dict):
            return None
        deadline = self._deadline(d)
        answered = right = 0
        finished = True
        for i, it in enumerate(doc.get("items") or []):
            a = (doc.get("answers") or {}).get(str(i))
            if a and parse_iso(a["at"]) <= deadline:
                answered += 1
                right += 1 if a.get("correct") else 0
            elif a or not self.dojo.question_held(it):
                finished = False
        return {"answered": answered, "right": right, "of": len(doc.get("items") or []), "finished": finished}

    def _settle(self, d: dict) -> dict:
        """Both finished before the deadline: keep the two numbers right, once."""
        if d.get("result") or d.get("state") != "ready":
            return d
        pa, pb = self._progress(d, "a"), self._progress(d, "b")
        if pa and pb and pa["finished"] and pb["finished"]:
            with self.store.lock:
                live = self._read(d["id"])
                if live is not None and not live.get("result"):
                    live["result"] = {"a": [pa["right"], pa["answered"]], "b": [pb["right"], pb["answered"]]}
                    self._write(live)
                    return live
        return d

    # ---------------------------------------------------------------- the view

    def view(self, learner: Learner) -> dict:
        self.social.gate()
        key = learner.key
        out: dict[str, Any] = {"owner": self.social._is_owner(key), "opted": self.social._opted(key),
                               "limits": {"questions": QUESTIONS, "hours": HOURS, "keep_days": KEEP_DAYS,
                                          "running": RUNNING_EACH}}
        if not out["opted"]:
            return out
        rows = self.social._rows()
        out["can_invite"] = not out["owner"]
        if out["can_invite"]:
            # Who can be invited, per exam. The owner never invites, so he never gets this list: it would tell
            # him which colleagues study which exam.
            mine = self.dojo.enrolled(learner)
            people = [k for k in self.social._everyone(rows)
                      if k != key and self.social._alive(k, rows) and self.social._invitable(k)]
            studies = {k: set(self.dojo.enrolled(Learner("", k, ""))) for k in people}
            exams = []
            for pid in mine:
                pkg = self.dojo.packages.get(pid)
                exams.append({"id": pid, "exam": pkg["meta"]["exam"], "title": pkg["meta"]["title"],
                              "domains": [{"id": x["id"], "title": x["title"]} for x in pkg.get("domains") or []],
                              "people": sorted((self.social._person(k, rows) for k in people if pid in studies[k]),
                                               key=lambda p: (p["name"].lower(), p["id"]))})
            out["exams"] = exams
        duels = [self._settle(d) for d in self._all() if self._role(d, key)]
        out["duels"] = [self._duel_view(d, key, rows) for d in sorted(duels, key=lambda d: d["created"], reverse=True)]
        return out

    def _duel_view(self, d: dict, key: str, rows: dict[str, dict]) -> dict:
        role = self._role(d, key)
        other = "b" if role == "a" else "a"
        other_key = d.get(other)
        if other == "b" and not other_key:
            # Declined or let lapse: the invited member's name, found again from the opaque id only.
            other_key = next((k for k in self.social._everyone(rows) if person_id(k) == d.get("b_id")), None)
        person = (self.social._person(other_key, rows) if other_key
                  else {"id": d.get("b_id"), "name": "Someone", "label": None})
        pkg = self.dojo.packages.get(d["package"])
        domain = next((x["title"] for x in pkg.get("domains") or [] if x["id"] == d.get("domain")), None)
        v: dict[str, Any] = {"id": d["id"], "package": d["package"], "exam": pkg["meta"]["exam"], "domain": domain,
                             "with": person, "sent": role == "a", "state": None, "text": None,
                             "rehearsal": (d.get("rids") or {}).get(role), "progress": None, "result": None,
                             "questions": QUESTIONS}
        if d.get("state") == "writing":
            v["state"], v["text"] = "writing", "Dojo is writing and checking the 10 questions."
            return v
        deadline = self._deadline(d)
        v["until"] = _when(deadline)
        mine = self._progress(d, role)
        v["progress"] = {k: mine[k] for k in ("answered", "of", "finished")} if mine else None
        if d.get("result"):
            r = d["result"]
            v["state"] = "result"
            v["result"] = {"mine": r[role][0], "mine_of": r[role][1], "theirs": r[other][0], "theirs_of": r[other][1]}
            v["text"] = "You have both finished."
            return v
        if utcnow() >= deadline:
            v["state"] = "closed"
            v["result"] = {"mine": mine["right"], "mine_of": mine["answered"]} if mine else None
            v["text"] = "The duel closed. Here is your result." if mine else "The duel closed."
            return v
        if role == "b" and not d.get("accepted"):
            v["state"] = "invited"
            v["text"] = (f"{self.social._spoken(person)} invites you to a duel: {QUESTIONS} questions on "
                         f"{pkg['meta']['exam']}{', ' + domain if domain else ''}. Answer by {v['until']}, in your own time.")
            return v
        if mine and mine["finished"]:
            v["state"] = "waiting"
            v["text"] = f"You have finished. The result shows when you have both finished, or the duel closes on {v['until']}."
            return v
        v["state"] = "open"
        v["text"] = f"Answer the {QUESTIONS} questions by {v['until']}, in your own time. There is no clock."
        return v

    # ---------------------------------------------------------------- inviting

    def invite(self, learner: Learner, person: str | None, pid: str | None, domain: str | None) -> dict:
        self.social._need_opted(learner)
        if self.social._is_owner(learner.key):
            raise HTTPException(403, "The person who runs this Dojo does not invite anyone to a duel.")
        if pid not in self.dojo.enrolled(learner):
            raise HTTPException(404, "No such exam.")
        rows = self.social._rows()
        targets = self.social._targets([person] if person else [], learner.key, rows)
        if len(targets) != 1:
            raise HTTPException(400, "Pick one person to invite.")
        b = targets[0]
        if pid not in self.dojo.enrolled(Learner("", b, "")):
            raise HTTPException(400, "They do not study this exam.")
        pkg = self.dojo.packages.get(pid)
        if domain is not None and not any(x["id"] == domain for x in pkg.get("domains") or []):
            raise HTTPException(400, "No such domain.")
        now = utcnow()
        with self.store.lock:
            # Again under the lock: a switch-off, or either player leaving circles, while this waited wins.
            self.social._need_opted(learner)
            if self.social._targets([person], learner.key, self.social._rows()) != [b]:
                raise HTTPException(400, "Pick one person to invite.")
            running = [d for d in self._all() if d.get("a") == learner.key and not self._over(d, now)]
            if any(d.get("b_id") == person_id(b) for d in running):
                raise HTTPException(409, "You have a duel with them running already.")
            if len(running) >= RUNNING_EACH:
                raise HTTPException(409, f"You have {RUNNING_EACH} duels running. Wait until one closes.")
            d = {"id": new_id("duel"), "package": pid, "domain": domain, "created": iso(now), "state": "writing",
                 "invited": None, "a": learner.key, "b": b, "b_id": person_id(b), "accepted": False,
                 "items": [], "models": None, "rids": {"a": None, "b": None}, "result": None}
            self._write(d)
        try:
            job = self.dojo.jobs.start(learner, "rehearsal", self._write_questions, learner, d["id"], ref=d["id"])
        except Exception:
            self.store.delete("play", "duels", d["id"])
            raise
        return {"duel": d["id"], "job": job}

    def _over(self, d: dict, now: datetime) -> bool:
        deadline = self._deadline(d)
        return bool(d.get("result")) or (deadline is not None and now >= deadline)

    def _skills(self, pkg: dict, domain: str | None) -> list[str]:
        if domain:
            pkg = {**pkg, "skills": {k: s for k, s in pkg["skills"].items() if s.get("domain") == domain}}
        chosen = self.dojo._sample_skills(pkg, QUESTIONS)
        if not chosen:
            raise UserError("No skill of this exam can be asked about right now.")
        return (chosen * QUESTIONS)[:QUESTIONS]

    def _write_questions(self, progress: Any, learner: Learner, did: str) -> dict:
        """The one job that writes the duel's questions, charged to the inviter like one quick practice."""
        d = self._read(did)
        if d is None:
            raise UserError("The duel is gone.")
        pid = d["package"]
        pkg = self.dojo.packages.get(pid)
        seen = seen_keys(self.store, d["a"], pid) | (seen_keys(self.store, d["b"], pid) if d.get("b") else set())
        items: list[dict] = []
        try:
            progress("Writing 10 new questions (author model) and checking them (gate model)")
            for _ in range(WRITE_ROUNDS):
                need = QUESTIONS - len(items)
                if need <= 0:
                    break
                written, blocked = self.dojo.write_mcqs(pid, self._skills(pkg, d.get("domain"))[:need])
                for b in blocked:
                    self.dojo.quality(learner.key, "rehearsal", {"package": pid, **b})
                keys = {question_key(it["stem"]) for it in items}
                for it in written:
                    k = question_key(it["stem"])
                    if k not in seen and k not in keys:   # new to both players
                        items.append(it)
                        keys.add(k)
            if len(items) < QUESTIONS:
                raise UserError("Dojo could not check 10 new questions against the sources right now, so no "
                                "invitation was sent. The quality log says why.")
        except Exception:
            self.store.delete("play", "duels", did)
            raise
        random.Random().shuffle(items)
        with self.store.lock:
            live = self._read(did)
            if live is None:
                raise UserError("The duel is gone.")
            live.update(items=items[:QUESTIONS], state="ready", invited=iso(), models=self.dojo.models("author", "gate"))
            self._write(live)
        return {"duel": did}

    # ---------------------------------------------------------------- answering an invitation

    def accept(self, learner: Learner, did: str) -> dict:
        self.social._need_opted(learner)
        with self.store.lock:
            self.social._need_opted(learner)
            d, role = self._mine(learner, did)
            if role != "b" or self._over(d, utcnow()):
                raise HTTPException(404, "No such duel.")
            d["accepted"] = True
            self._write(d)
        return self.view(learner)

    def decline(self, learner: Learner, did: str) -> dict:
        """Not kept anywhere: the invited member's key goes, and the inviter's view stays as it was until the
        48 hours are over, exactly as for an invitation nobody answered."""
        self.social._need_opted(learner)
        with self.store.lock:
            self.social._need_opted(learner)
            d, role = self._mine(learner, did)
            if role != "b" or d.get("accepted"):
                raise HTTPException(404, "No such duel.")
            d["b"] = None
            self._write(d)
        return self.view(learner)

    def open(self, learner: Learner, did: str) -> dict:
        """The player's own quick practice of the 10 questions, made at the first open: only now are they
        marked seen for this player (ADR 0009 keeps pools per learner because peers discuss questions)."""
        self.social._need_opted(learner)
        with self.store.lock:
            self.social._need_opted(learner)
            d, role = self._mine(learner, did)
            if d.get("state") != "ready" or (role == "b" and not d.get("accepted")):
                raise HTTPException(409, "Accept the invitation first.")
            rid = d["rids"].get(role)
            if rid:
                return {"rehearsal": rid}
            if self._over(d, utcnow()):
                raise HTTPException(409, "The duel closed.")
            pid = d["package"]
            doc = {"id": new_id("reh"), "package": pid, "created": iso(), "requested": len(d["items"]),
                   "items": d["items"], "answers": {}, "dropped": 0, "models": d.get("models"), "origin": "duel"}
            self.store.write("learners", learner.key, "rehearsals", doc["id"], value=doc)
            mark_seen(self.store, learner.key, pid, [question_key(it["stem"]) for it in d["items"]])
            self.store.append_event(learner.key, "rehearsal.created",
                                    {"package": pid, "rehearsal": doc["id"], "items": len(d["items"]),
                                     "requested": len(d["items"])})
            d["rids"][role] = doc["id"]
            self._write(d)
        return {"rehearsal": doc["id"]}

    # ---------------------------------------------------------------- the social layer's hooks

    def forget(self, learner: Learner) -> None:
        """Leave Play (or circles), Delete my record, Leave Dojo: every duel of this learner goes at once, also one
        they declined (only the opaque id is left of them there)."""
        pid = person_id(learner.key)
        with self.store.lock:
            for did in self.store.names("play", "duels"):
                d = self._read(did)
                if d and (learner.key in (d.get("a"), d.get("b")) or d.get("b_id") == pid):
                    self.store.delete("play", "duels", did)

    def not_invitable(self, key: str) -> None:
        """Turning "Others can invite me" off ends the invitations not yet accepted, like a decline."""
        with self.store.lock:
            for did in self.store.names("play", "duels"):
                d = self._read(did)
                if d and d.get("b") == key and not d.get("accepted"):
                    d["b"] = None
                    self._write(d)

    def wipe(self) -> None:
        for did in self.store.names("play", "duels"):
            self.store.delete("play", "duels", did)

    def sweep(self) -> None:
        self._all()
