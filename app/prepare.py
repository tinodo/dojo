"""Background preparation: writes missing lessons and their narration ahead of time, most exam weight
first, so a lesson is ready when the learner opens a skill. A lesson that a changed source page no
longer supports (`drift`) is written again before anything else, within the drift check's daily
allowance of rewrites: the active exam's first, then by most exam weight (`rewrites`). The rest wait for
the next day, in the same order. A skill whose every cited page is gone cannot be written again, so it
is not tried: it uses none of the allowance and calls no model until a page is back or its package cites
another page. After the rewrites and the missing lessons, a lesson on an earlier template, or not written
from every page its package now cites, is written again on the deeper template (app/deeper.py, ADR 0008)
within that pass's own daily allowance: the active exam first, then by most exam weight. The lesson the
learner sees stays the earlier one until the new one passes every check. When nothing is left to write it
narrates the lessons that have no audio yet. It works on one lesson at a time and waits whenever the
learner has something running, so it never competes with the learner's own requests. Everything it
removes is listed in the learner's quality log.

For new lessons and narration the exams take turns. The active exam goes first, and after a lesson for
one exam the next exam with work gets the next turn, so a large exam the owner has just added cannot hold
up the others. Rewrites do not take turns, and they do not move the turn: the day's allowance goes to the
active exam first, as Studio says (ADR 0006). A skill of an added exam with no verified official source
gets no lesson (app/newexam.py).

Then it writes the Deeper Listen of the newest lesson of every skill (ADR 0007): the active exam first,
then the other exams, most exam weight first. A lesson a changed source page holds back gets none: its
rewrite gets its own. Nor does a lesson that will be written again on the deeper template (ADR 0008):
the new lesson gets it, so it is paid for once. It writes at most DEEP_PER_DAY a day, counted on the data
share before the work, so a restart gets no fresh allowance. An attempt is kept on the share before its
first model call, so one that stops on an error or a restart still waits a week and counts toward the
DEEP_ATTEMPTS. Text already written and checked is spoken
first, at no charge. It stops between the writing and the speaking, and between two parts, when the
learner starts something.

When nothing else is left, it files filming jobs for an added exam, but only for lessons the owner
approved at a price shown first (app/newexam.py). It files one at a time, through the same job and
work slots as the Film button, and only after the owner's jobs have been quiet for a few minutes, so
the question pool and the delivery check get their turns between films. It stops before presenter
videos fill 90% of their storage budget, so filming a new exam never clears away other videos."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from .ai import AIError
from .core import Learner, UserError, iso, parse_iso, utcnow

log = logging.getLogger("dojo.prepare")

FILM_QUIET_S = 180          # the owner's jobs have done nothing for this long before a filming job is filed
FILM_STORAGE_SHARE = 0.9    # approved filming stops at this share of the presenter video storage budget
DEEP_PER_DAY = 20           # Deeper Listen narrations written a day, UTC days (ADR 0007)
DEEP_RETRY = timedelta(days=7)  # a Deeper Listen that did not pass its check is written again after this,
DEEP_ATTEMPTS = 2               # at most this many times in all


class Preparer:
    """Background preparation for the owner's exams: lessons, rewrites, narration, Deeper Listen and
    approved filming, one lesson at a time and never while the learner has work running."""
    def __init__(self, dojo: Any, owner: Learner):
        self.dojo = dojo
        self.owner = owner
        self.state: dict = {"running": False, "current": None, "prepared": 0, "narrated": 0, "failed": {}, "last": None,
                            "filmed": 0, "film_note": "", "upgraded": 0, "deepened": 0}
        self.films: Any = None             # set at startup: the approved filming of added exams (app/newexam.py)
        self.deeper: Any = None            # set at startup: lessons written again on the deeper template (app/deeper.py)
        self._started = False
        self._narrated: set[str] = set()   # lessons known to be fully narrated; narration audio is never deleted
        self._last_pid: str | None = None  # the exam the last new lesson or narration was for
        self._wake = threading.Event()
        self.scheduled = False             # team mode: the BackgroundScheduler runs step() in a spare slot (ADR 0009)

    def start(self, delay: int = 120) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, args=(delay,), name="prepare", daemon=True).start()

    def wake(self) -> None:
        """Look for work now instead of after the pause: an exam was added, or filming was approved."""
        self._wake.set()

    def _loop(self, delay: int) -> None:
        time.sleep(delay)
        while True:
            try:
                worked = self.step()
            except Exception:  # noqa: BLE001 - never let the background loop die
                log.exception("preparation step crashed")
                worked = False
            self._wake.wait(15 if worked else 1800)
            self._wake.clear()

    def order(self) -> list[str]:
        """The exams in turn, for new lessons and narration: the owner's active exam first, then the
        others by how many members study them (ADR 0009), and after work on one exam, the exam after it."""
        pids = self.dojo.exam_order(self.owner)
        if self._last_pid in pids:
            k = pids.index(self._last_pid) + 1
            pids = pids[k:] + pids[:k]
        return pids

    def rewrites(self) -> list[tuple[str, str]]:
        """The skills whose lesson a changed source page no longer supports (ADR 0006), in the order they
        are written again: the active exam first, then the exams most members study, then most exam
        weight, across the other exams too. A skill whose every cited page is gone is left out, as nothing
        can be written from a page that is gone. It is back in the queue once a page answers again or its
        package cites another page."""
        active = self.dojo.profile(self.owner)["active_package"]
        counts = self.dojo.enrolled_counts()
        queue = []
        for pid in self.dojo.packages.ids():
            again = self._rechecking(pid)
            if again:
                again -= self._gone_skills(pid)
            queue += [(pid != active, -counts.get(pid, 0), -s["share"], pid, s["id"]) for s in self._skills(pid) if s["id"] in again]
        return [(pid, sid) for *_, pid, sid in sorted(queue)]

    def _learner_busy(self) -> bool:
        """Today's rule with one learner: background work waits while the owner has a job. In team mode
        the BackgroundScheduler gives this work a spare slot instead, so it is never busy here."""
        if self.scheduled:
            return False
        return any(j.get("state") in ("queued", "running") for j in self.dojo.jobs.for_learner(self.owner))

    def _skills(self, pid: str) -> list[dict]:
        """The skills that get a lesson, heaviest first. An exam removed a moment ago has none."""
        pkg = self.dojo.packages.by_id.get(pid)
        return sorted((s for s in pkg["skills"].values() if s["sources"]) if pkg else [], key=lambda s: (-s["share"], s["id"]))

    def _waiting(self, key: str, now: Any) -> bool:
        """A failure is left alone for a day, so one bad skill does not block the queue."""
        failed_at = self.state["failed"].get(key)
        return bool(failed_at and now - parse_iso(failed_at) < timedelta(hours=24))

    def _rechecking(self, pid: str) -> set[str]:
        drift = getattr(self.dojo, "drift", None)
        return drift.rechecking(pid) if drift else set()

    def _gone_skills(self, pid: str) -> set[str]:
        drift = getattr(self.dojo, "drift", None)
        return drift.gone_skills(pid) if drift else set()

    def _rewrites_left(self) -> int:
        drift = getattr(self.dojo, "drift", None)
        return drift.rewrites_left() if drift else 0

    def next_skill(self) -> tuple[str, str] | None:
        """First a skill whose lesson a changed source page no longer supports, in `rewrites` order, while
        the day's allowance of such rewrites lasts; a rewrite beyond it waits for the next day. Then a
        skill with no lesson yet, by exam weight, the exams taking turns (`order`)."""
        now = utcnow()
        if self._rewrites_left() > 0:
            for pid, sid in self.rewrites():
                if not self._waiting(f"{pid}/{sid}", now):
                    return pid, sid
        for pid in self.order():
            ready = self.dojo.lesson_skills(pid)
            for s in self._skills(pid):
                if s["id"] in ready or self._waiting(f"{pid}/{s['id']}", now):
                    continue
                return pid, s["id"]
        return None

    def narration_targets(self) -> list[tuple[str, str, str]]:
        """The newest lesson of every skill whose narration is still missing, in preparation order.

        Narration media ids are hashes of the spoken text and the voice, so a lesson written before a
        voice changed keeps pointing at audio that is no longer there; this is what fills it in again."""
        out = []
        for pid in self.order():
            for s in self._skills(pid):
                lessons = self.dojo.lessons_for(pid, s["id"])
                if lessons and self._needs_narration(lessons[0]["id"]):
                    out.append((pid, s["id"], lessons[0]["id"]))
        return out

    def _needs_narration(self, lesson_id: str) -> bool:
        if lesson_id in self._narrated:
            return False
        try:
            missing = self.dojo.lesson_audio_missing(lesson_id)
        except HTTPException:
            return False   # the lesson went with its exam a moment ago
        if missing:
            return True
        self._narrated.add(lesson_id)
        return False

    def next_narration(self) -> tuple[str, str, str] | None:
        now = utcnow()
        for pid, sid, lesson_id in self.narration_targets():
            if not self._waiting(f"audio:{pid}/{sid}", now):
                return pid, sid, lesson_id
        return None

    def step(self) -> bool:
        """Write one lesson, write one lesson again on the deeper template, narrate one lesson that has no
        audio, or file one approved filming job. True when there may be more soon."""
        if self._learner_busy():
            return True
        skill = self.next_skill()
        upgrade = None if skill else self.next_upgrade()
        if upgrade:
            return self._upgrade(*upgrade)
        narration = None if skill else self.next_narration()
        if not skill and not narration:
            self.state.update(running=False, current=None)
            return self._deepen() or self._film()
        pid, sid = skill or narration[:2]
        # A lesson written again because a page changed counts against the day's allowance, before it
        # starts. It is not the exam's turn, so the turn does not move. When the skill's last page went
        # after it was picked, nothing is counted and nothing is written (DriftCheck.charge_rewrite).
        rewrite = bool(skill) and sid in self._rechecking(pid)
        if rewrite and not self.dojo.drift.charge_rewrite(pid, sid):
            return True
        if not rewrite:
            self._last_pid = pid
        self.state.update(running=True, current=f"{pid}/{sid}")
        try:
            lesson = narration[2] if narration else self.dojo.make_lesson(lambda step: None, self.owner, pid, sid)["lesson"]
            if self._gone(pid):
                return True
            self.dojo.lesson_audio(lambda step: None, self.owner, lesson)
            if self._gone(pid):
                return True
            self.state["narrated" if narration else "prepared"] += 1
            if narration:
                self._narrated.add(lesson)
        except (UserError, AIError, HTTPException) as e:
            if self._gone(pid):
                return True
            what = "Narrating this lesson" if narration else "Preparing this lesson"
            self.state["failed"][f"audio:{pid}/{sid}" if narration else f"{pid}/{sid}"] = iso()
            self.dojo.quality(None, "preparation", {"package": pid, "skill": sid, "text": "",
                                                    "reason": f"{what} ahead of time failed: {getattr(e, 'detail', None) or e}"})
        finally:
            self.state.update(current=None, last=iso())
        return True

    def next_upgrade(self) -> tuple[str, str, str] | None:
        """A lesson to write again on the deeper template (app/deeper.py): after the rewrites and the missing
        lessons, before narration, within its own daily allowance."""
        return self.deeper.next_upgrade() if self.deeper is not None else None

    def _upgrade(self, pid: str, sid: str, old: str) -> bool:
        """Write a skill's lesson again on the deeper template. The attempt is counted and written down
        before any model is called (DeeperLessons.charge_upgrade). The earlier lesson stays the one the
        learner sees unless the new one passes every check; then the new one is narrated like any new
        lesson, and when that fails the narration pass fills it in later."""
        if not self.deeper.charge_upgrade(pid, sid, old):
            return True
        self.state.update(running=True, current=f"{pid}/{sid}")
        try:
            try:
                lesson = self.dojo.make_lesson(lambda step: None, self.owner, pid, sid, upgrade_of=old)["lesson"]
            except (UserError, AIError, HTTPException) as e:
                if not self._gone(pid):
                    self.deeper.failed(pid, sid, old, str(getattr(e, "detail", None) or e))
                return True
            self.deeper.upgraded(pid, sid, old, lesson)
            self.state["upgraded"] += 1
            try:
                if not self._gone(pid):
                    self.dojo.lesson_audio(lambda step: None, self.owner, lesson)
            except (UserError, AIError, HTTPException) as e:
                if not self._gone(pid):
                    self.state["failed"][f"audio:{pid}/{sid}"] = iso()
                    self.dojo.quality(None, "preparation", {"package": pid, "skill": sid, "text": "",
                                                            "reason": f"Narrating this lesson ahead of time failed: {getattr(e, 'detail', None) or e}"})
        finally:
            self.state.update(current=None, last=iso())
        return True

    def _gone(self, pid: str) -> bool:
        """The owner removed this exam while its lesson was being written or narrated: what was just made
        for it is removed as well (app/newexam.py)."""
        if pid in self.dojo.packages.by_id:
            return False
        if self.films is not None:
            self.films.forget(pid)
        return True

    # ------------------------------------------------------------ Deeper Listen (ADR 0007)

    def deep_targets(self) -> list[tuple[str, str, str]]:
        """The newest lesson of every skill, in the order Deeper Listen writes them: the active exam first,
        then the other exams (those most members study first), most exam weight first."""
        out = []
        for pid in self.dojo.exam_order(self.owner):
            for s in self._skills(pid):
                lessons = self.dojo.lessons_for(pid, s["id"])
                if lessons:
                    out.append((pid, s["id"], lessons[0]["id"]))
        return out

    def _deep_due(self, lesson_id: str, now: Any) -> str | None:
        """What the lesson's Deeper Listen still needs: "speak" its checked text, "write" it, or nothing.
        A lesson that a changed source page no longer supports gets none: its rewrite will. An attempt counts
        from its first model call (Dojo.make_deep), so one that stopped on an error, a crash or a restart
        waits its week and counts toward the attempts like one that did not pass its checks."""
        if self.dojo.deep_held(lesson_id):
            return None
        d = self.dojo.deep(lesson_id)
        if not d or d.get("state") == "invalidated":
            return "write"
        if d.get("state") == "ready":
            return "speak" if self.dojo.deep_audio_missing(lesson_id) else None
        if int(d.get("attempts") or 0) >= DEEP_ATTEMPTS or now - parse_iso(d["created"]) < DEEP_RETRY:
            return None
        return "write"

    def _deep_day(self) -> dict:
        doc = self.dojo.store.read("content", "deep-state", default=None) or {}
        day = iso()[:10]
        if doc.get("day") != day:
            doc["day"], doc["today"] = day, 0
        doc.setdefault("written", 0)
        return doc

    def deep_left(self) -> int:
        return max(0, DEEP_PER_DAY - self._deep_day()["today"])

    def _charge_deep(self) -> bool:
        """Count a Deeper Listen before it is written, so a crash or a restart gets no free one."""
        with self.dojo.store.lock:
            doc = self._deep_day()
            if doc["today"] >= DEEP_PER_DAY:
                return False
            doc["today"] += 1
            doc["written"] += 1
            doc["updated"] = iso()
            self.dojo.store.write("content", "deep-state", value=doc)
            return True

    def _upgrading(self) -> set[str]:
        """Lessons that will be written again on the deeper template (app/deeper.py, ADR 0008): their Deeper
        Listen waits for the new lesson, so it is written, and paid for, once."""
        return self.deeper.to_write_again() if self.deeper is not None else set()

    def next_deep(self) -> tuple[str, str, str, str] | None:
        """The next Deeper Listen work: text already checked is spoken first, then the next lesson without one
        is written, while the day's allowance lasts. A lesson that will be written again on the deeper
        template is left for its new lesson."""
        now = utcnow()
        later = self._upgrading()
        todo = [(self._deep_due(lesson, now), pid, sid, lesson) for pid, sid, lesson in self.deep_targets() if lesson not in later]
        todo = [t for t in todo if t[0] and not self._waiting(f"deep:{t[1]}/{t[2]}", now)]
        speak = next((t for t in todo if t[0] == "speak"), None)
        write = next((t for t in todo if t[0] == "write"), None) if self.deep_left() > 0 else None
        return speak or write

    def _deepen(self) -> bool:
        """Write and speak the Deeper Listen of one lesson. False when there is nothing to do now."""
        work = self.next_deep()
        if not work:
            return False
        what, pid, sid, lesson = work
        if what == "write" and not self._charge_deep():
            return False
        self.state.update(running=True, current=f"{pid}/{sid}")
        media: list[str] = []
        try:
            if what == "write":
                self.dojo.make_deep(lambda step: None, lesson)
            if not self._gone(pid) and self.dojo.deep_written(lesson):
                media = self.dojo._deep_media(self.dojo.deep(lesson))
                if self.dojo.deep_audio(lambda step: None, lesson,
                                        stop=lambda: self._learner_busy() or pid not in self.dojo.packages.by_id):
                    self.state["deepened"] += 1
            if self._gone(pid):
                self.dojo.drop_deep(lesson, media)   # its exam went meanwhile: what was made for it goes too
        except (UserError, AIError, HTTPException) as e:
            if self._gone(pid):
                self.dojo.drop_deep(lesson, media)
                return True
            self.state["failed"][f"deep:{pid}/{sid}"] = iso()
            self.dojo.quality(None, "preparation", {"package": pid, "skill": sid, "lesson": lesson, "text": "",
                                                    "reason": f"Deeper Listen ahead of time failed: {getattr(e, 'detail', None) or e}"})
        finally:
            self.state.update(current=None, last=iso())
        return True

    def deep_status(self) -> dict:
        """Deeper Listen for Studio and /api/diag: how many of the newest lessons play it, and today's count.
        `failed` counts the lessons whose last attempt failed; `stopped`, those of them that stopped before
        they were finished: on an error, a crash or a restart. `waiting_upgrade`, the lessons whose Deeper
        Listen waits until they are written again on the deeper template (ADR 0008)."""
        targets = self.deep_targets()
        day = self._deep_day()
        now, later = utcnow(), self._upgrading()
        docs = [d for d in (self.dojo.deep(lesson) for _, _, lesson in targets) if d and d.get("state") == "failed"]
        return {"ready": sum(1 for _, _, lesson in targets if self.dojo.deep_ready(lesson)), "lessons": len(targets),
                "failed": len(docs), "stopped": sum(1 for d in docs if d.get("stopped")),
                "speaking": sum(1 for _, _, lesson in targets if self.dojo.deep_audio_missing(lesson) and lesson not in later),
                "waiting_upgrade": sum(1 for _, _, lesson in targets if lesson in later and self._deep_due(lesson, now)),
                "today": day["today"], "per_day": DEEP_PER_DAY, "written": day["written"]}

    def _quiet_for(self) -> float:
        """Seconds since any of the owner's jobs last did anything, filming jobs included."""
        beats = [parse_iso(j["heartbeat"]) for j in self.dojo.jobs.for_learner(self.owner) if j.get("heartbeat")]
        return (utcnow() - max(beats)).total_seconds() if beats else float("inf")

    def _film(self) -> bool:
        """File the filming job for one lesson the owner approved. False when there is nothing to file now."""
        lesson = self.films.next_film() if self.films is not None else None
        if not lesson:
            self.state["film_note"] = ""
            return False
        usage = self.dojo.avatar.usage()
        if usage["bytes"] >= FILM_STORAGE_SHARE * usage["budget_bytes"]:
            self.state["film_note"] = (f"Filming is paused: presenter videos use {usage['bytes'] / usage['budget_bytes']:.0%} "
                                       "of their storage budget, and filming more would clear away older videos.")
            return False
        if self._quiet_for() < FILM_QUIET_S:
            self.state["film_note"] = "The next film starts when nothing has run for three minutes."
            return True
        running = self.dojo.jobs.existing(self.owner, "video", lesson)
        try:
            job = running or self.dojo.jobs.start(self.owner, "video", self.dojo.lesson_video, self.owner, lesson,
                                                  ref=lesson, only_if_free=True)
        except HTTPException:
            self.state["film_note"] = "The next film waits for a free work slot."
            return True
        self.films.filed(lesson, job["id"])
        self.state["filmed"] += 0 if running else 1
        self.state["film_note"] = ""
        return True

    def status(self) -> dict:
        packages = {}
        for pid, pkg in list(self.dojo.packages.by_id.items()):
            packages[pid] = {"ready": len(self.dojo.lesson_skills(pid)), "skills": len(pkg["skills"]),
                             "teachable": sum(1 for s in pkg["skills"].values() if s["sources"])}
        return {"running": self.state["running"], "current": self.state["current"], "prepared": self.state["prepared"],
                "narrated": self.state["narrated"], "needs_narration": len(self.narration_targets()),
                "rewriting": len(self.rewrites()), "upgraded": self.state["upgraded"],
                "upgrading": len(self.deeper.upgrades()) if self.deeper is not None else 0,
                "failed": len(self.state["failed"]), "last": self.state["last"], "packages": packages,
                "filmed": self.state["filmed"], "film_note": self.state["film_note"]}
