"""The timed practice exam: how one is built, how it runs and what it leaves in the Record.

What this module guarantees:
- The deadline is kept by the server. An answer that arrives after it is refused and the attempt
  closes itself, so a closed browser tab cannot buy extra time.
- Questions are selected-response items (single choice, and the kinds of ADR 0012), so they never fill a
  pip. They are counted apart from the evidence (see app/evidence.py) and the advice in app/readiness.py
  says so. Scoring is one point per correctly answered component, nothing taken off (app/items.py).
- A yes/no series and a case study are sections of their own: once the learner leaves one it is locked,
  by the server, and a yes/no statement cannot be changed once the next one is answered.
- On a Microsoft associate or expert exam the learner may take it "with Microsoft Learn allowed", as the
  real exam allows; the lookups and the time away are counted and said, never hidden.
- The rationales are held back until the exam has ended. They are only sent after the moment they
  are shown is in the Record, so the teaching clock can never start later than the teaching itself.
- A question the learner has already seen is avoided while enough new ones exist; where too few new ones
  can be written, a seen one is used rather than a short exam, and the attempt records how many were new,
  and each question whether it was.
- Conditions that can no longer be known are recorded as unknown, never as "without help".
- A question whose official source changed while the exam ran is withdrawn when it closes (ADR 0006):
  not scored and not counted, while the answer given stays in the attempt. Its key is never shown.
- Dojo cannot see what a podcast app plays. When an episode from the podcast feed could be on the phone (the
  feed is on, or it has listed or served an episode, which plays on after the feed is turned off), an exam
  asks, before it closes, whether the learner listened to a lesson in a podcast app while it was open. Only
  an explicit "no" lets it claim exam conditions.
- The answer key never leaves Dojo while an attempt can still be taken - not through the export either.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable

from fastapi import HTTPException

from . import items as exam_items
from .budget import Refused, writer_batch
from .core import Abandoned, Learner, Spend, Store, UserError, iso, new_id, parse_iso, sha256, spending, utcnow
from .evidence import VIEW_MINUTES
from .itemwriter import PER_CALL as ITEM_PER_CALL
from .readiness import WEAK_GAP, WEAK_MIN, new_scored, withdrawn_note, withdrawn_questions
from .sources import normalize

log = logging.getLogger("dojo.exam")

DEFAULT_QUESTIONS = 50          # Dojo's own stated default where Learn publishes no question count
SHORT_QUESTIONS = 15
POOL_TARGET = 24                # checked questions kept ready per exam, so starting one is not a long wait
POOL_BATCH = 4
POOL_MIN_WAIT = 60              # the keeper looks again a minute later while there is work to do
POOL_MAX_WAIT = 6 * 3600        # ...and doubles its wait up to six hours when a batch adds nothing
POOL_TEAM_LOOK = 30 * 60        # team mode: with nothing to fill, a learner's pool is looked at again within half an hour
POOL_DAILY_BATCHES = 12         # hard cap: the background pool spends at most 12 model batches a day
POOL_MEMBER_DAILY_BATCHES = 6   # team mode: a member's own pool (ADR 0009, the allowance table)
POOL_MEMBERS_DAILY_BATCHES = 24  # team mode: all members' pools together, however many members there are
POOL_PAUSE_WAIT = 30            # how long a deletion waits for a batch that is already running
POOL_SLOT_WAIT = 5              # how long a batch waits for one of the three job slots before giving up
POOL_RECENT_DAYS = 14           # team mode: members whose record changed in the last 14 days have pools kept (ADR 0009)
LENGTHS = ("full", "short")
LATE_ANSWER = "Time is up. This exam closed itself at its deadline, so the answer was not accepted."
PODCAST_FIRST = ("Answer the podcast question first: did you listen to a lesson in a podcast app while this "
                 "exam was open?")
EXAM_NOTE = "Timed, no hints, no coach, and the rationales held back until the end."

# Anything that teaches or helps. Seen inside an attempt's window, the attempt cannot claim exam conditions.
HELP_EVENTS = {
    "hint.shown": "a hint",
    "ask.answered": "an answer from the coach",
    "mcq.answered": "a rehearsal question with its rationale",
    "answer.judged": "feedback on an answer",
    "answer.rejudged": "feedback on an answer",
    "lesson.viewed": "a lesson",
    "lab.opened": "a lab and its starter",
    "lab.hint": "a lab hint",
    "lab.ran": "a lab run",
    "lab.checked": "lab feedback",
    "exam.rationales_shown": "the answers to another practice exam",
}
KEY_FIELDS = exam_items.KEY_FIELDS
QUESTION_FIELDS = ("stem", "options", "targets", "text", "blanks", "statements", "exhibits", "questions", "title")
STYLES = ("single", "exam")
LOCKED = ("A yes/no series or case study you have left is locked, as in the real exam: "
          "its answers can no longer be changed.")
EXAM_DURATION_PAGE = "https://learn.microsoft.com/en-us/credentials/support/exam-duration-exam-experience"
# Only when a certification page states no duration but its level is known (ADR 0012).
TYPICAL_MINUTES = {"fundamentals": 45, "associate": 100, "expert": 100}
LEARN_URL = "https://learn.microsoft.com/"
LEARN_RULE = ("Microsoft's exam page says: \u201cYou can access Microsoft Learn as you complete your associate or expert "
              "exam. NOTE that access to Learn is NOT available on Fundamentals exams or GitHub exams.\u201d")


def for_export(doc: dict) -> dict:
    """One attempt as it may leave Dojo.

    While the exam can still be taken, the answer key and the rationales are held back: the export is
    one link away, and reading it mid-exam would make "no hints, no coach" a lie. They are added once
    the attempt has ended and the moment the answers were shown is in the Record.

    An exam that has been built but not started holds back its questions as well. They have not been
    shown, so they are not marked as seen, and a new exam would still count them as new: reading them
    here would quietly turn "questions you haven't seen" into questions you have.

    A question withdrawn at the close because its official source changed (ADR 0006) never shows its key:
    the key may no longer be what the page says."""
    if doc.get("state") == "closed" and doc.get("rationales_shown_at"):
        out = withdrawn_questions(doc)
        if not out:
            return doc
        return {**doc,
                "items": [it if i not in out else
                          {**exam_items.public(it), **({"source": it["source"]} if "source" in it else {})}
                          for i, it in enumerate(doc.get("items") or [])],
                "withheld": (f"{withdrawn_note(len(out))} The answer key and rationale of a withdrawn question are "
                             "not in the export: the key may no longer be what the page says.")}
    answers = {i: {k: v for k, v in a.items() if k not in ("correct", "points", "max_points")}
               for i, a in (doc.get("answers") or {}).items()}
    waiting = doc.get("state") in ("ready", "building")
    note = ("This exam is written but not started, so its questions are not in the export: they still "
            "count as questions you have not seen. Everything arrives once it has ended and its answers "
            "have been shown to you.") if waiting else (
           "The answer key and the rationales are not in the export yet. Dojo adds them once this exam "
           "has ended and its answers have been shown to you.")
    public = [exam_items.public(it) for it in doc.get("items") or []]
    return {**doc,
            "items": [{k: v for k, v in it.items() if k not in QUESTION_FIELDS} for it in public] if waiting else public,
            "answers": answers,
            "withheld": note}


# ---------------------------------------------------------------- questions the learner has seen

def question_key(stem: str) -> str:
    return sha256(normalize(stem or ""))[:16]


def item_key(it: dict) -> str:
    """The exposure key of an item: its stem (a case study's title and scenario), as before ADR 0012."""
    return question_key(exam_items.unit_text(it))


def seen_keys(store: Store, learner_key: str, pid: str) -> set[str]:
    doc = store.read("learners", learner_key, "seen", pid, default=None)
    return set((doc or {}).get("keys") or [])


def mark_seen(store: Store, learner_key: str, pid: str, keys: list[str]) -> None:
    with store.lock:
        doc = store.read("learners", learner_key, "seen", pid, default=None) or {"package": pid, "keys": []}
        known = set(doc["keys"])
        doc["keys"] = doc["keys"] + [k for k in dict.fromkeys(keys) if k not in known]
        doc["updated"] = iso()
        store.write("learners", learner_key, "seen", pid, value=doc)


# ---------------------------------------------------------------- what the real exam states

def untestable(pkg: dict) -> list[str]:
    """The domains in which no skill has a checked official source. No question can be written for
    them, so no practice exam could follow the blueprint. Only an added exam can have one."""
    return [d["title"] for d in pkg["domains"]
            if not any(pkg["skills"][sid]["sources"] for g in d["groups"] for sid in g["skills"])]


def level(pkg: dict) -> str | None:
    """The certification level the certification page shows (fundamentals, associate or expert), if known."""
    lv = (pkg["meta"].get("exam_format") or {}).get("level")
    return lv if lv in TYPICAL_MINUTES else None


def duration(pkg: dict) -> tuple[int, str]:
    """The real exam's minutes, and where they come from: "stated" on its certification page, or "typical"
    from Microsoft's exam-duration page when the level is known and no duration is stated. (0, "") when
    neither is known: a missing fact is not filled in."""
    minutes = int((pkg["meta"].get("exam_format") or {}).get("duration_minutes") or 0)
    if minutes:
        return minutes, "stated"
    lv = level(pkg)
    if pkg["meta"].get("issuer") == "Microsoft" and lv:
        return TYPICAL_MINUTES[lv], "typical"
    return 0, ""


def open_book(pkg: dict) -> dict:
    """Whether the real exam lets the learner open Microsoft Learn, and why, in one sentence each."""
    meta = pkg["meta"]
    lv = level(pkg)
    if meta.get("issuer") == "GitHub":
        reason = f"{meta['exam']} is a GitHub exam, and GitHub exams are closed-book: Microsoft Learn is not available in them."
    elif lv == "fundamentals":
        reason = f"{meta['exam']} is a fundamentals exam, and fundamentals exams are closed-book: Microsoft Learn is not available in them."
    elif lv in ("associate", "expert"):
        return {"allowed": True, "level": lv, "rule": LEARN_RULE, "source": EXAM_DURATION_PAGE, "url": LEARN_URL,
                "reason": (f"{meta['exam']} is an {lv} exam, so the real exam lets you open Microsoft Learn (not its Q&A, "
                           "practice assessments or your profile). The clock keeps running and no time is added.")}
    else:
        reason = (f"Dojo does not know the certification level of {meta['exam']}, so it cannot tell whether the real exam "
                  "allows Microsoft Learn, and this practice exam stays closed-book.")
    return {"allowed": False, "level": lv, "rule": LEARN_RULE, "source": EXAM_DURATION_PAGE, "url": "", "reason": reason}


def learn_line(doc: dict) -> str:
    """What an open-book attempt says about Microsoft Learn: what Dojo counted, and that it saw no more."""
    got = doc.get("learn") or {}
    n, secs = int(got.get("lookups") or 0), int(got.get("away_seconds") or 0)
    minutes = round(secs / 60)
    away = ("less than a minute" if secs and not minutes else f"about {minutes} minute{'s' if minutes != 1 else ''}"
            if secs else "no time")
    looks = f"{n} lookup{'s' if n != 1 else ''}"
    return (f"Microsoft Learn was allowed, as in the real exam: {looks}, and {away} with this exam page out of view. "
            "Dojo cannot see what you read there.")


def blocker(pkg: dict) -> str | None:
    """Why Dojo gives no practice exam for this package, or None when it can give one."""
    if not duration(pkg)[0]:
        return "This exam package does not record a duration from the official pages, so Dojo cannot time a practice exam for it."
    gaps = untestable(pkg)
    if gaps:
        names = " and ".join(f'"{t}"' for t in gaps)
        return (f"No practice exam can follow the blueprint yet: no official page passed the check for any skill in "
                f"{'the domain' if len(gaps) == 1 else 'the domains'} {names}, so Dojo cannot write questions for "
                f"{'it' if len(gaps) == 1 else 'them'}. An exam without {'that domain' if len(gaps) == 1 else 'those domains'} "
                "would not show how ready you are.")
    return None


def spec(pkg: dict, length: str, questions: int | None = None) -> dict:
    """Length and time for one practice exam, from what Microsoft Learn states about the real one."""
    fmt = pkg["meta"].get("exam_format") or {}
    minutes, origin = duration(pkg)
    stated = fmt.get("question_count")
    full = int(stated or DEFAULT_QUESTIONS)
    reason = blocker(pkg)
    if reason:
        raise UserError(reason)
    if length not in LENGTHS:
        raise UserError("Choose a full-length or a short practice exam.")
    wanted = full if length == "full" else min(SHORT_QUESTIONS, full)
    count = max(1, int(questions if questions is not None else wanted))
    typical = origin == "typical"
    return {
        "length": length,
        "questions": count,
        "minutes": max(5, round(minutes * count / full)),
        "real_minutes": minutes,
        "real_questions": stated,
        "count_is_stated": stated is not None,
        "default_questions": full,
        "duration_is_stated": not typical,
        "duration_quote": (f"Microsoft's exam-duration page gives {minutes} minutes as the typical time for a {level(pkg)} exam; "
                           "this exam's own page states none.") if typical else fmt.get("duration_quote", ""),
        "duration_source": EXAM_DURATION_PAGE if typical else fmt.get("duration_source", ""),
        "count_note": fmt.get("question_count_note", ""),
        "count_source": fmt.get("question_count_source", ""),
        "checked": fmt.get("checked", ""),
    }


def plan_skills(pkg: dict, count: int) -> list[str]:
    """Which skills the questions are written for: shared out over the domains by blueprint weight
    (the middle of each published range), then over that domain's skills."""
    rng = random.Random()
    mids = {d["id"]: (d["weight_min"] + d["weight_max"]) / 2 for d in pkg["domains"]}
    total = sum(mids.values()) or 1.0
    exact = {did: count * w / total for did, w in mids.items()}
    per = {did: int(v) for did, v in exact.items()}
    for did, _ in sorted(exact.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:count - sum(per.values())]:
        per[did] += 1
    out: list[str] = []
    for d in pkg["domains"]:
        # Only a skill with a checked official source can be asked about. An added exam can hold others;
        # its domain's share goes to the skills that can be, so the blueprint weights still hold.
        skills = [sid for g in d["groups"] for sid in g["skills"] if pkg["skills"][sid]["sources"]]
        if not skills:
            continue   # a whole domain: blocker() refuses the exam before anything is planned
        rng.shuffle(skills)
        out += [skills[i % len(skills)] for i in range(per[d["id"]])]
    rng.shuffle(out)
    return out


def plan_units(pkg: dict, count: int, style: str = "single") -> list[tuple[str, str]]:
    """(kind, skill) for every item of an exam of `count` questions. Single choice only for the "single"
    style (and below 10 questions); otherwise the mix of ADR 0012. The skills are planned by blueprint weight
    first; a yes/no series or a case study then takes its slots from one domain (the one with the most), so
    the domain shares hold."""
    skills = plan_skills(pkg, count)
    if style != "exam":
        return [("single", sid) for sid in skills]
    m = exam_items.mix(count)
    rng = random.Random()
    units: list[tuple[str, str]] = []
    for kind in ("yesno", "case"):
        for _ in range(m.get(kind, 0)):
            per: dict[str, list[int]] = {}
            for n, sid in enumerate(skills):
                per.setdefault(pkg["skills"][sid]["domain"], []).append(n)
            slots = max(per.values(), key=len, default=[])
            take = exam_items.UNIT_SIZE[kind]
            if len(slots) < take:
                continue   # no domain has room: the questions stay single choice
            picked = slots[:take]
            units.append((kind, skills[picked[0]]))
            skills = [sid for n, sid in enumerate(skills) if n not in picked]
    kinds = [k for k in ("multi", "sequence", "match", "hotarea") for _ in range(m.get(k, 0))][:len(skills)]
    kinds += ["single"] * (len(skills) - len(kinds))
    rng.shuffle(kinds)
    return list(zip(kinds, skills)) + units


def questions_in(units: list[dict]) -> int:
    """How many questions some items are: a yes/no series or a case study counts its questions."""
    return sum(exam_items.size(q) for q in units)


def kinds_in(units: list[dict]) -> dict[str, int]:
    """How many items of each kind (a series or a case study is one item)."""
    have: dict[str, int] = {}
    for q in units:
        have[exam_items.kind_of(q)] = have.get(exam_items.kind_of(q), 0) + 1
    return have


def kind_needed(pool: list[dict]) -> str:
    """The kind a pool is shortest of against POOL_MIX (in items). Single choice wins a tie."""
    have = kinds_in(pool)
    gaps = {k: n - have.get(k, 0) for k, n in exam_items.POOL_MIX.items()}
    best = max(gaps.values())
    return "single" if gaps["single"] == best else next(k for k in exam_items.POOL_MIX if gaps[k] == best)


# ---------------------------------------------------------------- attempts

class ExamRoom:
    """Timed practice exams (ADR 0012): starts an attempt from the question pool, keeps the clock and the
    sections on the server, records the answers, and scores the attempt when it closes."""
    def __init__(self, dojo: Any, podcast: Any):
        self.dojo = dojo
        self.store: Store = dojo.store
        self.packages = dojo.packages
        self.podcast = podcast  # app/podcast.py, which knows whether an episode could be on the phone
        # Exams being removed right now (app/newexam.py). Read and changed under the store lock, so an
        # attempt cannot start between removal's check for running attempts and the package going.
        self.closing: set[str] = set()

    # ---- storage

    def attempt(self, learner: Learner, aid: str) -> dict:
        try:
            doc = self.store.read("learners", learner.key, "exams", aid)
        except ValueError:
            doc = None
        if not doc:
            raise HTTPException(404, "No such practice exam.")
        return doc

    def _save(self, learner: Learner, doc: dict) -> None:
        self.store.write("learners", learner.key, "exams", doc["id"], value=doc)

    def _gone(self, pid: str) -> bool:
        """The exam is being removed, or is gone. Read under the store lock."""
        return pid in self.closing or pid not in self.packages.by_id

    def _save_building(self, learner: Learner, doc: dict) -> None:
        """A build saves only while its exam is live. Checked and saved in one step under the store lock,
        which removal holds when it marks the exam closing: a build never outlives its exam (ADR 0009)."""
        with self.store.lock:
            if self._gone(doc["package"]):
                self._abandon(learner, doc)
            self._save(learner, doc)

    def _abandon(self, learner: Learner, doc: dict) -> None:
        """The exam was removed while this attempt was being written: it is marked broken, without the
        removed exam's questions, and the build stops. Call under the store lock."""
        doc.update(state="broken", items=[], conditions="unknown",
                   conditions_note="This exam was removed from this Dojo while Dojo was writing it, so it was never taken.")
        self._save(learner, doc)
        raise UserError("This exam was removed from this Dojo while Dojo was writing your practice exam.")

    def bind_versions(self, learners: list[Learner], pid: str, version: str | None) -> int:
        """Before an added exam is removed: every attempt of it that was written before attempts kept their
        package version gets the version being retired, so it is always shown with the package it was written
        from, even if a changed exam is later added under the same id (ADR 0009). Call under the store lock."""
        if version is None:
            return 0
        bound = 0
        for who in learners:
            folder = self.store.path("learners", who.key, "exams")
            for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                try:
                    doc = self.store.read("learners", who.key, "exams", path.stem, default=None)
                    if isinstance(doc, dict) and doc.get("package") == pid and not doc.get("package_version"):
                        doc["package_version"] = version
                        self._save(who, doc)
                        bound += 1
                except (Abandoned, ValueError) as e:   # a learner leaving right now: their folder goes anyway
                    log.warning("an attempt kept no package version: %s", type(e).__name__)
        return bound

    def _live(self, learner: Learner, doc: dict) -> None:
        """An attempt starts or takes answers only while its exam is in this Dojo and the learner studies it
        (ADR 0009). Viewing a past attempt, and closing one, stay possible. Call under the store lock."""
        pid = doc["package"]
        if pid in self.closing or pid not in self.packages.by_id:
            raise HTTPException(409, "This exam was removed from this Dojo, so this practice exam cannot run.")
        team = getattr(getattr(self.dojo, "settings", None), "team", False)   # off: the owner studies every exam
        if team and pid not in self.dojo.enrolled(learner):
            raise HTTPException(409, "You no longer study this exam. Choose it again under Your exams to take "
                                     "this practice exam.")

    def pool(self, learner: Learner, pid: str, seen: set[str] | None = None) -> list[dict]:
        """The questions waiting for this exam that the learner has not seen.

        A question that has been seen is no use to a new exam, so it is not counted towards the pool's
        target and not shown as "waiting": it would keep the pool looking full while it had nothing new
        to give. Nor is a question whose quote is no longer on its source page (ADR 0006). Writing the
        pool back (in build and top_up) is what drops them for good."""
        doc = self.store.read("learners", learner.key, "pool", pid, default=None)
        items = list((doc or {}).get("items") or [])
        seen = seen_keys(self.store, learner.key, pid) if seen is None else seen
        drift = getattr(self.dojo, "drift", None)
        return [q for q in items if item_key(q) not in seen and (drift is None or drift.question_ok(q))]

    def _write_pool(self, learner: Learner, pid: str, items: list[dict]) -> None:
        self.store.write("learners", learner.key, "pool", pid, value={"package": pid, "items": items, "updated": iso()})

    # ---- building

    def create(self, learner: Learner, pid: str, length: str, style: str = "single", open_book_: bool = False) -> dict:
        """Reserve the attempt, so the page can follow the build with the attempt's own id."""
        pkg = self.packages.get(pid)
        s = spec(pkg, length)
        if style not in STYLES:
            raise UserError("Choose single-choice questions or the real exam's question types.")
        rule = open_book(pkg)
        if open_book_ and not rule["allowed"]:
            raise UserError(rule["reason"])
        doc = {
            "id": new_id("exam"), "package": pid, "created": iso(), "state": "building", "length": length,
            "wanted": s["questions"], "spec": s, "items": [], "answers": {}, "flagged": [], "started": None,
            "deadline": None, "minutes": s["minutes"], "closed_at": None, "closed_reason": None,
            "rationales_shown_at": None, "conditions": None, "conditions_note": "", "new_questions": 0,
            "dropped": 0, "models": self.dojo.models("author", "gate"),
            "blueprint": pkg["meta"]["blueprint"],   # readiness counts the attempt only while the exam keeps it
            "style": style, "open_book": bool(open_book_), "locked": [], "inside": None,
            "package_version": None,   # set below, with the check that the exam is still here
            # Microsoft Learn beside the exam: what Dojo can count, and nothing about what was read.
            "learn": {"lookups": 0, "away_seconds": 0, "away_since": None} if open_book_ else None,
        }
        # One step under the store lock, which removal holds when it marks the exam closing: an attempt is
        # never written for an exam that is being removed (ADR 0009).
        with self.store.lock:
            if self._gone(pid):
                raise HTTPException(409, "This exam is being removed from this Dojo.")
            doc["package_version"] = self.packages.version(pid)   # how it is shown after the exam is removed
            self._save(learner, doc)
        return doc

    def first_round(self, learner: Learner, pid: str, length: str, style: str = "single") -> list[tuple[str, str, int]]:
        """The model calls of a build's first round, at most (ADR 0009, "Money"): every batch of every kind the
        plan needs after the questions waiting in the learner's pool. Reserved at the route before the attempt
        is written. The second round (single choice for what is still missing) only spends what the first
        left over: a batch it cannot cover is left out and the exam is shorter, never stopped half-way."""
        if style not in STYLES:
            return []
        pkg = self.packages.get(pid)
        need: dict[str, list[str]] = {}
        for kind, sid in plan_units(pkg, spec(pkg, length)["questions"], style):
            need.setdefault(kind, []).append(sid)
        seen = seen_keys(self.store, learner.key, pid)
        for q in self.pool(learner, pid, seen):
            skills = need.get(exam_items.kind_of(q))
            if skills:
                skills.remove(q["skill"]) if q["skill"] in skills else skills.pop()
        calls: list[tuple[str, str, int]] = []
        for kind, skills in need.items():
            if not skills:
                continue
            size = 4 if kind == "single" else ITEM_PER_CALL.get(kind, 1)
            for _ in self.dojo._mcq_batches(skills, size):
                calls += writer_batch(kind)
        return calls

    def _write(self, pid: str, kind: str, skills: list[str], progress: Callable[[int, int], None]) -> tuple[list, list]:
        if kind == "single":
            return self.dojo.write_mcqs(pid, skills, progress)
        return self.dojo.write_items(pid, kind, skills, progress)

    def build(self, progress: Callable[[str], None], learner: Learner, aid: str) -> dict:
        """Write one exam (below). If its exam is removed while it is written, whatever step notices it, the
        attempt is marked broken and the build is refused (ADR 0009)."""
        try:
            return self._build(progress, learner, aid)
        except Exception:
            # Every failure (a refusal, a model error, anything else): if the exam went meanwhile, the attempt
            # is marked broken and the refusal says why. A failure of a live exam stays as it was.
            with self.store.lock:
                doc = self.store.read("learners", learner.key, "exams", aid, default=None)
                if isinstance(doc, dict) and doc.get("state") == "building" and self._gone(doc["package"]):
                    self._abandon(learner, doc)
            raise

    def _build(self, progress: Callable[[str], None], learner: Learner, aid: str) -> dict:
        """Write one exam. Questions already checked and waiting in the pool are used first, so the
        learner does not wait for all of them; the rest are written and checked now. Counts are in
        questions: a yes/no series and a case study are several questions each (ADR 0012)."""
        doc = self.attempt(learner, aid)
        pid, wanted = doc["package"], doc["wanted"]
        style = doc.get("style") or "single"
        pkg = self.packages.get(pid)
        seen = seen_keys(self.store, learner.key, pid)
        chosen_keys: set[str] = set()
        need: dict[str, list[str]] = {}
        for kind, sid in plan_units(pkg, wanted, style):
            need.setdefault(kind, []).append(sid)

        def size_of(units: list[dict]) -> int:
            return sum(exam_items.size(u) for u in units)

        with self.store.lock:
            if self._gone(pid):   # never write a pool back for an exam that is being removed
                self._abandon(learner, doc)
            taken, keep = [], []
            for q in self.pool(learner, pid, seen):   # seen questions are not in here, and are dropped below
                key, kind = item_key(q), exam_items.kind_of(q)
                if need.get(kind) and key not in chosen_keys:
                    taken.append(dict(q, new=True))
                    chosen_keys.add(key)
                    skills = need[kind]
                    skills.remove(q["skill"]) if q["skill"] in skills else skills.pop()
                else:
                    keep.append(q)
            self._write_pool(learner, pid, keep)
        doc["items"] = taken
        self._save_building(learner, doc)
        progress(f"{size_of(taken)} of {wanted} questions ready")
        blocked: list[dict] = []
        repeats: list[dict] = []

        def add(written: list[dict], room_for: int) -> None:
            for it in written:
                key = item_key(it)
                if key in chosen_keys or exam_items.size(it) > room_for - size_of(doc["items"]):
                    continue
                if key in seen:
                    repeats.append(it)  # held back: only used if too few new questions can be written
                    continue
                chosen_keys.add(key)
                doc["items"].append(dict(it, new=True))

        # First round: every kind the plan still needs. Second round: single choice for what is missing,
        # so a kind the checker keeps blocking costs the exam its variety, not its length.
        for kind in [k for k in exam_items.KINDS if need.get(k)]:
            have = size_of(doc["items"])
            written, lost = self._write(pid, kind, need[kind],
                                        lambda done, total, have=have: progress(f"{have + done} of {wanted} questions ready"))
            blocked += lost
            add(written, wanted)
            self._save_building(learner, doc)
            progress(f"{size_of(doc['items'])} of {wanted} questions ready")
        missing = wanted - size_of(doc["items"])
        if missing > 0:
            have = size_of(doc["items"])
            written, lost = self.dojo.write_mcqs(pid, plan_skills(pkg, missing),
                                                 lambda done, total: progress(f"{have + done} of {wanted} questions ready"))
            blocked += lost
            add(written, wanted)
            self._save_building(learner, doc)
            progress(f"{size_of(doc['items'])} of {wanted} questions ready")
        new = size_of(doc["items"])
        for it in repeats:
            # Not enough new questions exist yet. A question you have seen is better than a short exam,
            # as long as the results say how many were new.
            if size_of(doc["items"]) >= wanted:
                break
            key = item_key(it)
            if key in chosen_keys or exam_items.size(it) > wanted - size_of(doc["items"]):
                continue
            chosen_keys.add(key)
            doc["items"].append(dict(it, new=False))
        for b in blocked:
            self.dojo.quality(learner.key, "exam", {"package": pid, **b})
        if not doc["items"]:
            self.store.delete("learners", learner.key, "exams", aid)
            raise UserError("No exam question could be verified against the sources right now. The quality log says why.")
        doc["items"] = self._order(doc["items"])
        total = size_of(doc["items"])
        doc.update(state="ready", dropped=wanted - total, new_questions=new,
                   minutes=spec(pkg, doc["length"], total)["minutes"])
        self._save_building(learner, doc)
        return {"attempt": doc["id"], "questions": total, "dropped": doc["dropped"],
                "new_questions": new, "minutes": doc["minutes"]}

    @staticmethod
    def _order(units: list[dict]) -> list[dict]:
        """The main section shuffled; a yes/no series and a case study after it, as sections of their own
        that the learner enters when they choose."""
        main = [u for u in units if exam_items.kind_of(u) not in exam_items.LOCKING]
        random.Random().shuffle(main)
        return (main + [u for u in units if exam_items.kind_of(u) == "yesno"]
                + [u for u in units if exam_items.kind_of(u) == "case"])

    # ---- running

    def start(self, learner: Learner, aid: str) -> dict:
        with self.store.lock:
            doc = self.attempt(learner, aid)
            if doc["state"] == "open":
                return self.view(learner, aid)
            if doc["state"] != "ready":
                raise HTTPException(409, "This practice exam cannot be started." if doc["state"] == "building"
                                    else "This practice exam has already ended.")
            self._live(learner, doc)
            # Two exams can be written and wait. Only one may run: with two clocks going, neither
            # attempt could say what the other was doing, and both would claim exam conditions.
            running = self.open_attempt(learner)
            if running and running["id"] != aid:
                raise HTTPException(409, "A timed practice exam is already running. Finish it, or let its "
                                         "clock run out, before starting another.")
            self._drop_withheld(learner, doc)
            now = utcnow()
            # podcast_feed: an episode from the podcast feed could be on the phone, so the exam asks before it closes.
            doc.update(state="open", started=iso(now), deadline=iso(now + timedelta(minutes=doc["minutes"])),
                       podcast_feed=self._podcast_possible(learner))
            self._save(learner, doc)
            mark_seen(self.store, learner.key, doc["package"], [item_key(it) for it in doc["items"]])
            self.store.append_event(learner.key, "exam.started", {
                "package": doc["package"], "attempt": aid, "questions": sum(exam_items.size(it) for it in doc["items"]), "minutes": doc["minutes"],
                "deadline": doc["deadline"], "length": doc["length"], "new_questions": doc["new_questions"]})
        return self.view(learner, aid)

    def _source_gone(self, doc: dict) -> list[dict]:
        """The questions of an exam whose quote a changed source page no longer has (ADR 0006)."""
        drift = getattr(self.dojo, "drift", None)
        return [] if drift is None else [it for it in doc["items"] if not drift.question_ok(it)]

    def _drop_withheld(self, learner: Learner, doc: dict) -> None:
        """An exam written before a source page changed loses, when it starts, the questions whose quote
        is no longer on the page (ADR 0006). Its clock is worked out again for the questions left.
        Questions of an exam that is already running stay until it closes; then they are withdrawn."""
        gone = {id(it) for it in self._source_gone(doc)}
        if not gone:
            return
        keep = [it for it in doc["items"] if id(it) not in gone]
        if not keep:
            raise HTTPException(409, "A source page changed, and none of this exam's questions can be checked against it "
                                     "any more. Build a new practice exam.")
        seen = seen_keys(self.store, learner.key, doc["package"])
        # Which questions are new is read again with the count, so the two agree. One recorded as seen stays seen.
        keep = [dict(it, new=it.get("new") is not False and item_key(it) not in seen) for it in keep]
        doc.update(items=keep, source_dropped=int(doc.get("source_dropped") or 0) + len(gone),
                   new_questions=sum(exam_items.size(it) for it in keep if it["new"]),
                   minutes=spec(self.packages.get(doc["package"]), doc["length"],
                                sum(exam_items.size(it) for it in keep))["minutes"])

    def answer(self, learner: Learner, aid: str, index: int, choice: int | None = None,
               response: dict | None = None, part: int | None = None, rev: int | None = None) -> dict:
        """Store an answer. A single-choice question takes `choice` (as it always has); the kinds of ADR 0012
        take `response`, and a yes/no statement or a case-study question its `part` as well.

        `rev` is the page's count of writes to this item: a request that arrives after a later one is
        stale and changes nothing, and the reply says what the server holds. An answer that is refused
        changes nothing either: it neither moves the learner nor locks the section they were in."""
        with self.store.lock:
            doc = self._fresh(learner, aid)
            if doc["state"] != "open":
                raise HTTPException(409, LATE_ANSWER if doc.get("closed_reason") == "time" else "This practice exam has ended.")
            self._live(learner, doc)
            if not 0 <= index < len(doc["items"]):
                raise HTTPException(404, "No such question.")
            it = doc["items"][index]
            last = int((doc.get("revs") or {}).get(str(index)) or 0)
            if rev is not None and rev <= last:
                return {**self._answer_out(doc, index), "stale": True}
            # Nothing is saved until the whole answer holds (the document is read afresh for every request).
            self._enter(doc, index)
            raw = response if response is not None else {"choice": choice}
            if exam_items.kind_of(it) == "single" and choice is not None:
                raw = {"choice": choice}
            try:
                stored = exam_items.apply(it, doc["answers"].get(str(index)), raw, part)
            except LookupError as e:
                raise HTTPException(409, str(e))
            except ValueError as e:
                raise HTTPException(400, "No such option." if exam_items.kind_of(it) == "single" else str(e))
            if stored:
                doc["answers"][str(index)] = {**stored, "at": iso(), "seconds": self._seconds_in(doc)}
            else:
                doc["answers"].pop(str(index), None)
            doc.setdefault("revs", {})[str(index)] = rev if rev is not None else last + 1
            self._save(learner, doc)
        return {**self._answer_out(doc, index), "stale": False}

    def _answer_out(self, doc: dict, index: int) -> dict:
        it, given = doc["items"][index], doc["answers"].get(str(index))
        return {"answered": index, "answers": self._answered(doc), "questions": self._questions(doc),
                "seconds_left": self._left(doc), "locked": doc.get("locked") or [], "inside": doc.get("inside"),
                "response": exam_items.response_view(it, given),
                "choice": given.get("choice") if given and exam_items.kind_of(it) == "single" else None,
                "rev": int((doc.get("revs") or {}).get(str(index)) or 0)}

    def _enter(self, doc: dict, index: int) -> None:
        """Moving to item `index`: a yes/no series or case study the learner was in is locked once they
        leave it, and a locked one cannot be entered again. Raises 409 for a locked one, before anything
        changes: a refused move leaves the learner where they were."""
        locked = {int(i) for i in doc.get("locked") or []}
        if index in locked:
            raise HTTPException(409, LOCKED)
        inside = doc.get("inside")
        if inside is not None and inside != index:
            locked.add(int(inside))
            doc["inside"] = None
        doc["locked"] = sorted(locked)
        if 0 <= index < len(doc["items"]) and exam_items.kind_of(doc["items"][index]) in exam_items.LOCKING:
            doc["inside"] = index

    def visit(self, learner: Learner, aid: str, index: int) -> dict:
        """The page moved to item `index` (-1 for the review screen). Leaving a yes/no series or a case
        study locks it: the server decides, so a reload or a second tab cannot reopen one."""
        with self.store.lock:
            doc = self._fresh(learner, aid)
            if doc["state"] != "open":
                raise HTTPException(409, "This practice exam has ended.")
            self._live(learner, doc)
            if not -1 <= index < len(doc["items"]):
                raise HTTPException(404, "No such question.")
            # Saved only when the move is allowed; a refused one changes nothing.
            self._enter(doc, index)
            self._save(learner, doc)
        return {"locked": doc["locked"], "inside": doc.get("inside"), "seconds_left": self._left(doc)}

    def learn(self, learner: Learner, aid: str, event: str, seconds: int = 0) -> dict:
        """Microsoft Learn beside an open-book exam: "open" is one lookup, "away" the seconds the exam page
        was out of view. Never more time away than the exam has been open."""
        with self.store.lock:
            doc = self._fresh(learner, aid)
            if doc["state"] != "open":
                raise HTTPException(409, "This practice exam has ended.")
            self._live(learner, doc)
            if not doc.get("open_book"):
                raise HTTPException(409, "This practice exam is closed-book.")
            got = dict(doc.get("learn") or {"lookups": 0, "away_seconds": 0})
            if event == "open":
                got["lookups"] = int(got.get("lookups") or 0) + 1
            elif event == "away":
                got["away_seconds"] = min(self._seconds_in(doc), int(got.get("away_seconds") or 0) + max(0, int(seconds)))
            else:
                raise HTTPException(400, "Say whether Microsoft Learn was opened or how long you were away.")
            doc["learn"] = got
            self._save(learner, doc)
        return {"lookups": got["lookups"], "away_seconds": got["away_seconds"]}

    @staticmethod
    def _questions(doc: dict, out: set[int] = frozenset()) -> int:
        return sum(exam_items.size(it) for i, it in enumerate(doc["items"]) if i not in out)

    @staticmethod
    def _answered(doc: dict, out: set[int] = frozenset()) -> int:
        return sum(exam_items.answered_parts(doc["items"][int(k)], a) for k, a in doc["answers"].items()
                   if int(k) not in out and 0 <= int(k) < len(doc["items"]))

    def flag(self, learner: Learner, aid: str, index: int, flagged: bool) -> dict:
        with self.store.lock:
            doc = self._fresh(learner, aid)
            if doc["state"] != "open":
                raise HTTPException(409, "This practice exam has ended.")
            self._live(learner, doc)
            if not 0 <= index < len(doc["items"]):
                raise HTTPException(404, "No such question.")
            if index in (doc.get("locked") or []):
                raise HTTPException(409, LOCKED)
            marks = {int(i) for i in doc.get("flagged") or []}
            marks.add(index) if flagged else marks.discard(index)
            doc["flagged"] = sorted(marks)
            self._save(learner, doc)
        return {"flagged": doc["flagged"]}

    def submit(self, learner: Learner, aid: str, reason: str = "submitted", podcast_heard: bool | None = None) -> dict:
        with self.store.lock:
            doc = self.attempt(learner, aid)
            if doc["state"] == "open" and doc.get("podcast_heard") is None and self._asks_podcast(learner, doc):
                if podcast_heard is not None:
                    self._declare(learner, doc, podcast_heard)
                elif reason != "time" and self._left(doc) > 0:
                    # Finishing early is the learner's choice, so the question is answered first. When
                    # the clock has run out, the exam closes without a reply, and its conditions are unknown.
                    raise HTTPException(409, PODCAST_FIRST)
            doc = self._fresh(learner, aid)
            if doc["state"] == "open":
                self._close(learner, doc, reason if reason in ("submitted", "time") else "submitted")
            elif doc["state"] != "closed":
                raise HTTPException(409, "This practice exam has not started.")
        return self.view(learner, aid)

    def _podcast_possible(self, learner: Learner) -> bool:
        """Whether an episode could be on the phone: the feed is on, or it has listed or served one, which
        plays on after the feed is turned off. Only deleting the record forgets that (app/podcast.py)."""
        return self.podcast.could_be_on_phone(learner)

    def _asks_podcast(self, learner: Learner, doc: dict) -> bool:
        """With an episode possibly on the phone at the start, or at any point after it, one could have played unseen."""
        return bool(doc.get("podcast_feed")) or self._podcast_possible(learner)

    def _declare(self, learner: Learner, doc: dict, heard: bool) -> None:
        """What the learner said is in the Record before the attempt relies on it."""
        self.store.append_event(learner.key, "podcast.declared", {
            "package": doc["package"], "attempt": doc["id"], "heard": bool(heard), "asked": True})
        doc.update(podcast_feed=True, podcast_heard=bool(heard))
        self._save(learner, doc)

    def reveal(self, learner: Learner, aid: str) -> dict:
        """Record that the answers and rationales were shown, then show them. They are never sent
        before that moment is in the Record, because it is when the teaching clock starts.

        The event is written before the flag, and a flag without its event is repaired here: if the
        write of the event ever failed, the teaching clock would otherwise start at nothing and a
        later check would look later than it is."""
        with self.store.lock:
            doc = self._fresh(learner, aid)
            if doc["state"] != "closed":
                raise HTTPException(409, "The answers are shown when the exam has ended.")
            at = self._shown_at(learner, aid)
            if not at:
                # A withdrawn question's key and rationale are not shown (ADR 0006), so it teaches nothing: its
                # skill is taught only if a scored question of that skill was shown too.
                out = withdrawn_questions(doc)
                shown = [it for i, it in enumerate(doc["items"]) if i not in out]
                at = self.store.append_event(learner.key, "exam.rationales_shown", {
                    "package": doc["package"], "attempt": aid, "questions": len(shown),
                    "skills": sorted({it["skill"] for it in shown})})["at"]
            if doc.get("rationales_shown_at") != at:
                doc["rationales_shown_at"] = at
                self._save(learner, doc)
        return self.view(learner, aid)

    def _shown_at(self, learner: Learner, aid: str) -> str | None:
        """When this attempt's answers were shown, as the Record has it - the Record decides, not the
        attempt document, because the teaching clock is read from the events."""
        for ev in self.store.events(learner.key):
            if ev.get("type") == "exam.rationales_shown" and (ev.get("data") or {}).get("attempt") == aid:
                return ev["at"]
        return None

    def _fresh(self, learner: Learner, aid: str) -> dict:
        """The attempt as it stands now: a deadline that has passed closes it, and a build that
        stopped halfway is marked broken instead of pretending to be startable."""
        doc = self.attempt(learner, aid)
        if doc["state"] == "open" and self._left(doc) <= 0:
            self._close(learner, doc, "time")
        elif doc["state"] == "building" and not self.dojo.jobs.busy(learner, "exam", aid):
            doc.update(state="broken", conditions="unknown",
                       conditions_note="Dojo stopped while it was writing this exam, so it was never taken.")
            self._save(learner, doc)
        return doc

    def _withdrawn_now(self, doc: dict) -> list[int]:
        """The questions whose quote their page lost while the exam ran (ADR 0006)."""
        drift = getattr(self.dojo, "drift", None)
        return [] if drift is None else [i for i, it in enumerate(doc["items"]) if not drift.question_ok(it)]

    def _close(self, learner: Learner, doc: dict, reason: str) -> None:
        # The exam ran as it was. Now every question is checked against its page once more, and one whose
        # quote is gone is withdrawn: not scored and not counted. The answer given stays in the attempt.
        doc["withdrawn"] = self._withdrawn_now(doc)
        out = set(doc["withdrawn"])
        scored = [i for i in range(len(doc["items"])) if i not in out]
        correct = 0
        for i in scored:
            given = doc["answers"].get(str(i))
            if given:
                it = doc["items"][i]
                # One point per correctly answered component, nothing taken off (app/items.py).
                given["points"] = exam_items.points(it, given)
                given["max_points"] = exam_items.max_points(it)
                given["correct"] = given["points"] >= given["max_points"]
                correct += given["points"]
        if doc.get("inside") is not None:
            doc["locked"] = sorted({*(doc.get("locked") or []), int(doc["inside"])})
            doc["inside"] = None
        if not doc.get("podcast_feed") and self._podcast_possible(learner):
            doc["podcast_feed"] = True
        doc["conditions"], doc["conditions_note"] = self._conditions(learner, doc)
        if doc.get("open_book"):
            doc["conditions_note"] = f'{doc["conditions_note"]} {learn_line(doc)}'.strip()
        doc.update(state="closed", closed_at=iso(), closed_reason=reason)
        self._save(learner, doc)
        for i in scored:
            given = doc["answers"].get(str(i))
            if given:
                it = doc["items"][i]
                data = {"package": doc["package"], "skill": it["skill"], "attempt": doc["id"], "index": i,
                        "correct": bool(given["correct"]), "conditions": doc["conditions"]}
                if exam_items.kind_of(it) != "single":
                    # Rehearsal only, whatever the kind: no selected-response answer fills a pip (app/evidence.py).
                    data.update(kind=exam_items.kind_of(it), points=given["points"], max_points=given["max_points"])
                self.store.append_event(learner.key, "exam.answered", data)
        self.store.append_event(learner.key, "exam.closed", {
            "package": doc["package"], "attempt": doc["id"], "questions": self._questions(doc, out),
            "answered": self._answered(doc, out), "correct": correct,
            "points": sum(exam_items.max_points(doc["items"][i]) for i in scored), "reason": reason,
            "conditions": doc["conditions"], "new_questions": new_scored(doc), "withdrawn": len(out)})

    def _conditions(self, learner: Learner, doc: dict) -> tuple[str, str]:
        """Exam conditions are claimed only when Dojo can still see they held. Anything that teaches or
        helps, anywhere in Dojo, makes them unknown: the routes refuse it while an exam is open, so an
        event that arrived anyway is a sign Dojo cannot account for the attempt.

        A lesson view is recorded when it starts and states how long it may still be playing, so a view
        that started before the exam but could still be running inside it counts too. The window is the
        one app/evidence.py uses for the teaching clock: at least VIEW_MINUTES, longer if the event
        itself says so.

        A podcast app is the one place Dojo cannot see: when an episode could be on the phone during the
        attempt (podcast_feed), only the learner's explicit "no" to the podcast question lets it claim exam
        conditions. No reply is read as the worse case, never as "no"."""
        if not doc.get("started") or not doc.get("deadline"):
            return "unknown", "Dojo cannot tell when this attempt ran, so its conditions are unknown."
        start, end = parse_iso(doc["started"]), utcnow()
        helped = set()
        for ev in self.store.events(learner.key):
            kind, d = ev.get("type"), ev.get("data") or {}
            if kind not in HELP_EVENTS or d.get("attempt") == doc["id"]:
                continue
            at = parse_iso(ev["at"])
            window = max(VIEW_MINUTES, int(d.get("repeats_within_min") or 0)) if kind == "lesson.viewed" or kind.startswith("lab.") else 0
            if at <= end and at + timedelta(minutes=window) >= start:
                helped.add(kind)
        if helped:
            what = ", ".join(dict.fromkeys(HELP_EVENTS[k] for k in sorted(helped)))   # judged and judged again: one kind
            return "unknown", (f"Dojo showed something that teaches while this exam was open ({what}), so its "
                               "conditions are recorded as unknown.")
        if doc.get("podcast_feed"):
            heard = doc.get("podcast_heard")
            if heard is None:
                return "unknown", ("Episodes from your podcast feed could be on your phone, and this exam ended "
                                   "before you said whether you listened to a lesson in a podcast app. Dojo cannot "
                                   "see what a podcast app plays, so its conditions are recorded as unknown.")
            if heard is not False:
                return "unknown", ("You said you listened to a lesson in a podcast app while this exam was open, "
                                   "so its conditions are recorded as unknown.")
            return "exam", f"{EXAM_NOTE} You said you did not listen to a lesson in a podcast app while it was open."
        return "exam", EXAM_NOTE

    @staticmethod
    def _seconds_in(doc: dict) -> int:
        started = parse_iso(doc["started"]) if doc.get("started") else None
        return 0 if started is None else max(0, int((utcnow() - started).total_seconds()))

    @staticmethod
    def _left(doc: dict) -> int:
        if not doc.get("deadline"):
            return 0
        return int((parse_iso(doc["deadline"]) - utcnow()).total_seconds())

    # ---- what the learner sees

    def view(self, learner: Learner, aid: str) -> dict:
        doc = self._fresh(learner, aid)
        # A removed exam's past attempt still shows, with the version it was written from (ADR 0009).
        pkg = self.packages.get_any(doc["package"], doc.get("package_version"))
        shown = bool(doc.get("rationales_shown_at"))
        closed = doc["state"] == "closed"
        # The questions are sent when the clock starts, and not before. While an exam is being written
        # or waiting to be started, reading its stems would be reading it early - and they would still
        # count as questions "you have not seen", because seen is marked at the start.
        items = []
        out_ = withdrawn_questions(doc) if closed else set()
        locked = set(doc.get("locked") or [])
        for i, it in enumerate(doc["items"] if doc["state"] in ("open", "closed") else []):
            s = pkg["skills"].get(it["skill"], {})
            given = doc["answers"].get(str(i))
            single = exam_items.kind_of(it) == "single"
            view = {"index": i, "stem": it["stem"], "options": it.get("options"),
                    "flagged": i in (doc.get("flagged") or []), "rev": int((doc.get("revs") or {}).get(str(i)) or 0)}
            if not single:
                view.update(exam_items.shown(it), index=i, locked=i in locked)
            if given:
                if single:
                    view["choice"] = given["choice"]
                else:
                    view["response"] = exam_items.response_view(it, given)
                view["seconds"] = given.get("seconds")
            if i in out_:
                # The answer given stays visible; the key is not shown, as it may no longer be what the page says.
                view["withdrawn"] = True
            if shown:
                view.update(skill=it["skill"], skill_text=s.get("text", ""), domain=s.get("domain_title", ""), source=it["source"])
                if i not in out_:
                    if single:
                        view.update(answer=it["answer"], rationale=it["rationale"], quote=it["quote"],
                                    correct=bool(given and given["choice"] == it["answer"]))
                    else:
                        keyed = exam_items.key(it)
                        if "questions" in keyed:
                            # A case study's questions keep what they show and gain their key.
                            keyed["questions"] = [{**sq, **kq} for sq, kq in zip(view["questions"], keyed["questions"])]
                        view.update(keyed, correct=exam_items.full(it, given), earned=exam_items.points(it, given))
            items.append(view)
        out = {
            "id": doc["id"], "package": doc["package"], "exam": pkg["meta"]["exam"], "state": doc["state"],
            "created": doc["created"], "length": doc["length"], "minutes": doc["minutes"], "spec": doc["spec"],
            # Once closed, the totals count the scored questions only.
            "questions": self._questions(doc, out_), "wanted": doc["wanted"], "dropped": doc["dropped"],
            "source_dropped": int(doc.get("source_dropped") or 0),
            # Left out when it starts: a source page no longer has their quote.
            "source_waiting": len(self._source_gone(doc)) if doc["state"] == "ready" else 0,
            "withdrawn": len(out_), "withdrawn_note": withdrawn_note(len(out_)) if out_ else "",
            "new_questions": new_scored(doc) if closed else doc["new_questions"],
            "answered": self._answered(doc, out_), "flagged": doc.get("flagged") or [],
            "deadline": doc["deadline"], "seconds_left": max(0, self._left(doc)) if doc["state"] == "open" else 0,
            "closed_at": doc["closed_at"], "closed_reason": doc["closed_reason"], "conditions": doc["conditions"],
            "conditions_note": doc["conditions_note"], "rationales_shown": shown, "items": items,
            # Asked before the exam closes, so it is known before the conditions are decided.
            "podcast_question": (doc["state"] in ("ready", "open") and doc.get("podcast_heard") is None
                                 and self._asks_podcast(learner, doc)),
            "note": ("Selected-response answers can be guesses. This is practice in the exam's format, not a prediction "
                     "of your result, and it fills no pip in your record."),
            "style": doc.get("style") or "single", "units": len(doc["items"]),
            "locked": sorted(locked), "inside": doc.get("inside"), "locked_note": LOCKED,
            "open_book": bool(doc.get("open_book")), "open_book_rule": open_book(pkg),
            "learn": ({k: v for k, v in (doc.get("learn") or {}).items() if k != "away_since"}
                      if doc.get("open_book") else None),
            "learn_line": learn_line(doc) if doc.get("open_book") and closed else "",
            "mix": self._mix(doc),
        }
        if closed and shown:
            out.update(self.results(learner, doc, pkg))
        elif closed:
            out["correct"] = sum(exam_items.points(it, doc["answers"].get(str(i)))
                                 for i, it in enumerate(doc["items"]) if i not in out_)
            out["points"] = sum(exam_items.max_points(it) for i, it in enumerate(doc["items"]) if i not in out_)
        return out

    @staticmethod
    def _mix(doc: dict) -> dict[str, int]:
        """How many questions of each kind the exam holds."""
        out: dict[str, int] = {}
        for it in doc["items"]:
            k = exam_items.kind_of(it)
            out[k] = out.get(k, 0) + exam_items.size(it)
        return out

    def results(self, learner: Learner, doc: dict, pkg: dict) -> dict:
        """The diagnosis: raw score, per domain, the misses grouped by skill, and where to go next.
        No 700 scale, no pass line: the package's passing score belongs to Microsoft's exam, not to this one.
        A withdrawn question (ADR 0006) is left out of all of it: it is neither right nor missed."""
        out_ = withdrawn_questions(doc)
        by_domain: dict[str, dict] = {}
        misses: dict[str, dict] = {}
        correct = possible = partial = 0
        for i, it in enumerate(doc["items"]):
            s = pkg["skills"].get(it["skill"], {})
            did = s.get("domain", "?")
            d = by_domain.setdefault(did, {"id": did, "domain": s.get("domain_title", ""), "weight": s.get("domain_weight", ""),
                                           "questions": 0, "answered": 0, "correct": 0, "points": 0, "withdrawn": 0})
            size = exam_items.size(it)
            if i in out_:
                d["withdrawn"] += size
                continue
            d["questions"] += size
            given = doc["answers"].get(str(i))
            got, most = exam_items.points(it, given), exam_items.max_points(it)
            ok = got >= most
            correct += got
            possible += most
            partial += 1 if 0 < got < most else 0
            d["points"] += most
            d["correct"] += got
            d["answered"] += exam_items.answered_parts(it, given)
            if not ok:
                m = misses.setdefault(it["skill"], {"skill": it["skill"], "text": s.get("text", ""),
                                                    "domain": s.get("domain_title", ""), "misses": 0, "unanswered": 0})
                m["misses"] += 1
                m["unanswered"] += 0 if given else 1
        patterns = sorted(misses.values(), key=lambda m: (-m["misses"], m["skill"]))
        taught = self.dojo.lesson_skills(doc["package"])
        steps = []
        for m in patterns:
            lessons = self.dojo.lessons_for(doc["package"], m["skill"]) if m["skill"] in taught else []
            steps.append({**m, "lesson": lessons[0]["id"] if lessons else None})
        average = round(100 * correct / possible) if possible else 0
        for d in by_domain.values():
            d["score"] = round(100 * d["correct"] / d["points"]) if d["points"] else 0
            d["weak"] = d["questions"] >= WEAK_MIN and d["score"] <= average - WEAK_GAP
            d["judged"] = d["questions"] >= WEAK_MIN
            d["thinned"] = d["questions"] < WEAK_MIN <= d["questions"] + d["withdrawn"]
        thinned = [d["domain"] or d["id"] for d in sorted(by_domain.values(), key=lambda d: d["id"]) if d["thinned"]]
        return {
            "correct": correct, "points": possible, "partial": partial,
            "by_domain": sorted(by_domain.values(), key=lambda d: d["id"]),
            "average": average, "weak_gap": WEAK_GAP, "weak_min": WEAK_MIN,
            "patterns": [p for p in patterns if p["misses"] > 1], "single_misses": sum(1 for p in patterns if p["misses"] == 1),
            "next_steps": steps[:8],
            # Rule 2 of the readiness advice needs every domain judged; withdrawn questions may leave one too thin.
            "thinned": thinned,
            "thinned_note": (f"That leaves fewer than {WEAK_MIN} questions to judge {' and '.join(thinned)}, so this exam "
                             f"does not count towards “Should you book it?” (rule 2).") if thinned else "",
            "diagnosis_note": ("Not a pass prediction. These are Dojo's own questions; the real exam uses different "
                              "questions and a scaled score. Dojo never maps this to that scale."),
        }

    def list_for(self, learner: Learner, pid: str) -> list[dict]:
        docs = (self.store.read("learners", learner.key, "exams", n) for n in self.store.names("learners", learner.key, "exams"))
        out = []
        for d in docs:
            if not d or d.get("package") != pid:
                continue
            if d.get("state") == "open" and self._left(d) <= 0:
                # The deadline is the server's. An exam past it is over, so the list closes it here
                # rather than calling it "in progress" until someone happens to open it.
                d = self._fresh(learner, d["id"])
            # No running total: an exam that can still be taken never says how many are right, or the
            # list would answer the questions for you, one option at a time.
            closed = d.get("state") == "closed"
            gone = withdrawn_questions(d)
            correct = sum(exam_items.points(it, d["answers"].get(str(i))) for i, it in enumerate(d["items"]) if i not in gone)
            out.append({"id": d["id"], "created": d["created"], "state": d["state"], "length": d["length"],
                        "questions": self._questions(d, gone), "answered": self._answered(d, gone),
                        "correct": correct if closed else None,
                        "points": sum(exam_items.max_points(it) for i, it in enumerate(d["items"]) if i not in gone)
                        if closed else None, "withdrawn": len(gone),
                        "new_questions": new_scored(d), "conditions": d.get("conditions"),
                        "closed_at": d.get("closed_at"), "closed_reason": d.get("closed_reason"),
                        "minutes": d.get("minutes"), "style": d.get("style") or "single",
                        "open_book": bool(d.get("open_book"))})
        return sorted(out, key=lambda d: d["created"], reverse=True)

    def closed_attempts(self, learner: Learner, pid: str) -> list[dict]:
        docs = (self.store.read("learners", learner.key, "exams", n) for n in self.store.names("learners", learner.key, "exams"))
        return sorted((d for d in docs if d and d.get("package") == pid and d.get("state") == "closed"),
                      key=lambda d: d.get("closed_at") or d["created"])

    def open_attempt(self, learner: Learner, pid: str | None = None) -> dict | None:
        for name in self.store.names("learners", learner.key, "exams"):
            d = self.store.read("learners", learner.key, "exams", name)
            if d and d.get("state") == "open" and (pid is None or d.get("package") == pid) and self._left(d) > 0:
                return d
        return None

    # ---- the pool that keeps the wait short

    def pool_size(self, learner: Learner, pid: str) -> int:
        """The questions waiting for this exam: a yes/no series or a case study counts its questions."""
        return questions_in(self.pool(learner, pid))

    def top_up(self, learner: Learner, pid: str, batch: int = POOL_BATCH, target: int = POOL_TARGET,
               report: bool = True, stale: Callable[[], bool] | None = None, kind: str = "single") -> dict:
        """Write one small batch of checked questions of one kind into the pool.

        Says what happened: how many were added, how many the checker blocked, and why it stopped.
        `stale` lets a caller drop the result of a batch whose record was deleted while it ran, so
        nothing is written back under a learner who asked to be forgotten."""
        pkg = self.packages.get(pid)
        if blocker(pkg):
            # Only a practice exam takes questions from the pool, so none are paid for here.
            return {"added": 0, "blocked": 0, "state": "no-exam"}
        seen = seen_keys(self.store, learner.key, pid)
        have = self.pool(learner, pid, seen)
        size = sum(exam_items.size(q) for q in have)
        if size >= target:
            return {"added": 0, "blocked": 0, "state": "full"}
        known = {item_key(q) for q in have} | seen
        if kind == "single":
            items, blocked = self.dojo.write_mcqs(pid, plan_skills(pkg, min(batch, target - size)))
        else:
            # A series or a case study is one unit of several questions; the other kinds are a few units.
            units = 1 if kind in exam_items.LOCKING else max(1, min(3, exam_items.POOL_MIX.get(kind, 1)))
            items, blocked = self.dojo.write_items(pid, kind, plan_skills(pkg, units))
        if stale and stale():
            return {"added": 0, "blocked": len(blocked), "state": "stale"}
        if report:
            for b in blocked:
                self.dojo.quality(learner.key, "exam", {"package": pid, **b})
        fresh = [it for it in items if item_key(it) not in known]
        if not fresh:
            return {"added": 0, "blocked": len(blocked), "state": "nothing-new"}
        with self.store.lock:
            if stale and stale():
                return {"added": 0, "blocked": len(blocked), "state": "stale"}
            # self.pool() drops the seen ones, so writing it back is also what clears them out.
            self._write_pool(learner, pid, self.pool(learner, pid) + fresh)
        return {"added": sum(exam_items.size(it) for it in fresh), "blocked": len(blocked), "state": "added"}


class PoolKeeper:
    """Keeps a few checked questions ready per exam, in the background.

    Five rules keep it cheap and safe. It never runs while one of the learner's own jobs is queued or
    running, and a batch holds one of the same three job slots the learner's work uses, so it can never
    add a fourth model call alongside them. A batch that adds nothing, or fails, doubles the wait up to
    six hours, and writes one line in the quality log per run of bad luck, not one per try. It spends at
    most POOL_DAILY_BATCHES model batches a day: the count and its date are written down on the share
    before each batch, so restarting Dojo does not hand the pool a fresh allowance. A record deletion
    pauses it: a batch already in flight is waited for and anything it was about to write is dropped,
    and so is the line it would have written about a bad batch. And it only ever tops up a learner who
    already has a record - after "delete everything" it writes nothing at all, restart or no restart.

    In team mode (ADR 0009) the same rules hold per learner: it serves the owner and the members who used
    Dojo in the last POOL_RECENT_DAYS days in turn, one batch at a time, each with their own pool, daily
    allowance, back-off and pause. Every batch is written, and its model calls attributed, as the learner
    whose pool it fills. The BackgroundScheduler then holds the job slot for it."""

    def __init__(self, room: ExamRoom, owner: Learner):
        self.room = room
        self.owner = owner
        self.state: dict = {"running": False, "added": 0, "last": None, "current": None,
                            "wait": POOL_MIN_WAIT, "misses": 0, "note": ""}
        self.members: Callable[[], list[Learner]] = lambda: []   # team mode: the active members (ADR 0009)
        self.scheduled = False           # team mode: the BackgroundScheduler holds the job slot for a batch
        self.budget: Any = None          # team mode: each batch reserves against the learner it serves (ADR 0009)
        self._started = False
        self._batch = threading.Lock()   # one batch at a time; a deletion waits on this
        self._gens: dict[str, int] = {}  # per learner, bumped by a deletion: a batch of an older generation is dropped
        self._paused: set[str] = set()   # learners whose deletion is running
        self._forgotten: set[str] = set()  # set by a deletion; cleared when the learner has a record again
        self._luck: dict[str, dict] = {}   # a member's wait and misses; the owner's are in self.state
        self._due: dict[str, float] = {}   # a member's next turn (monotonic seconds)
        self._turn = 0                     # round-robin position over the learners
        self.wake = threading.Event()      # team mode: tells the BackgroundScheduler to give the pool a turn now

    def start(self, delay: int = 300) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, args=(delay,), name="exam-pool", daemon=True).start()

    def _loop(self, delay: int) -> None:
        time.sleep(delay)
        while True:
            try:
                wait = self.step()
            except Exception:  # noqa: BLE001 - never let the background loop die
                log.exception("topping up the question pool failed")
                wait = self._back_off("the keeper itself failed")
            time.sleep(max(5, wait))

    def _gen(self, learner: Learner) -> int:
        return self._gens.get(learner.key, 0)

    @contextmanager
    def paused(self, learner: Learner | None = None):
        """Hold the keeper still for one learner (the owner when none is named). A deletion takes this: it
        makes that learner's batch in flight stale first, then waits a short while for any batch to finish,
        so nothing lands under a record that is being removed. Afterwards the keeper leaves that learner
        alone until they have a record again; the other learners carry on."""
        key = (learner or self.owner).key
        self._paused.add(key)
        self._gens[key] = self._gens.get(key, 0) + 1
        got = self._batch.acquire(timeout=POOL_PAUSE_WAIT)
        try:
            yield
            self._forgotten.add(key)
        finally:
            if got:
                self._batch.release()
            self._paused.discard(key)

    def _busy(self, learner: Learner) -> bool:
        return any(j.get("state") in ("queued", "running") for j in self.room.dojo.jobs.for_learner(learner))

    def _has_record(self, learner: Learner) -> bool:
        return self.room.store.path("learners", learner.key).is_dir()

    def _recent(self, learner: Learner) -> bool:
        """A member who wrote to their record in the last POOL_RECENT_DAYS days. Read from the member's own
        folder; nothing about it is stored centrally."""
        try:
            changed = self.room.store.path("learners", learner.key, "events.jsonl").stat().st_mtime
        except (OSError, ValueError):
            return False
        return time.time() - changed < POOL_RECENT_DAYS * 86400

    def learners(self) -> list[Learner]:
        """Whose pools are kept: the owner, then the recently active members."""
        try:
            members = [m for m in self.members() if m.key != self.owner.key and self._recent(m)]
        except Exception:  # noqa: BLE001 - members.json unreadable: the owner alone
            members = []
        return [self.owner] + members

    def _budget(self, learner: Learner) -> dict:
        """What the pool has already spent today, as written on the share. A new day starts at zero."""
        doc = self.room.store.read("learners", learner.key, "pool-budget", default=None) or {}
        day = iso()[:10]
        return doc if doc.get("day") == day else {"day": day, "batches": 0}

    def _limit(self, learner: Learner) -> int:
        return POOL_DAILY_BATCHES if learner.key == self.owner.key else POOL_MEMBER_DAILY_BATCHES

    def _members_budget(self) -> dict:
        """Team mode: what all members' pools have spent today together. One count, no learner in it."""
        doc = self.room.store.read("team", "pool-budget", default=None) or {}
        day = iso()[:10]
        return doc if doc.get("day") == day else {"day": day, "batches": 0}

    def _refusal(self, learner: Learner) -> str | None:
        """Why no batch may be paid for this learner today, or None. The owner's own allowance is as before;
        a member's pool has its own and, with the other members, the shared daily total (ADR 0009)."""
        if self._budget(learner)["batches"] >= self._limit(learner):
            return f"the daily limit of {self._limit(learner)} batches is reached"
        if learner.key != self.owner.key and self._members_budget()["batches"] >= POOL_MEMBERS_DAILY_BATCHES:
            return f"the members' daily limit of {POOL_MEMBERS_DAILY_BATCHES} batches together is reached"
        return None

    def _spent(self, learner: Learner) -> bool:
        """The daily cap on paid model calls for the background pool."""
        return self._refusal(learner) is not None

    def _charge(self, learner: Learner) -> str | None:
        """Write the batch down before paying for it, so a crash or a restart cannot buy it twice.

        Answers with why the batch may not go ahead - the day's allowance is gone, or the learner has
        no record, because the keeper must never be the thing that creates one - or None to go ahead."""
        with self.room.store.lock:
            if not self._has_record(learner):
                return "there is no record to keep questions for"
            refused = self._refusal(learner)
            if refused:
                return refused
            if learner.key != self.owner.key:
                # The shared count first: a crash between the two writes over-counts, never under-counts.
                shared = self._members_budget()
                shared["batches"] += 1
                shared["updated"] = iso()
                self.room.store.write("team", "pool-budget", value=shared)
            doc = self._budget(learner)
            doc["batches"] += 1
            doc["updated"] = iso()
            self.room.store.write("learners", learner.key, "pool-budget", value=doc)
            return None

    def _pay(self, learner: Learner, pid: str, kind: str) -> tuple[str | None, Any]:
        """Team mode: the batch's money first, then its place in the day's batch counts, in one step under
        the store lock (the budget's lock too). A batch the money does not cover leaves the counters as they
        were; a batch the counters refuse gives its reservation back. Answers (why not, None) or (None, the
        reservation, None outside team mode)."""
        budget = self.budget if self.budget is not None and self.budget.active else None
        with self.room.store.lock:
            if not self._has_record(learner):
                return "there is no record to keep questions for", None
            refused = self._refusal(learner)
            if refused:
                return refused, None
            hold = None
            if budget is not None:
                try:
                    hold = budget.reserve(learner.key, "pool", pid, budget.calls_amount(writer_batch(kind)))
                except Refused as e:
                    return str(e), None
            try:
                refused = self._charge(learner)
            except BaseException:
                if hold is not None:
                    hold.release(failed=True)
                raise
            if refused:
                if hold is not None:
                    hold.release(failed=True)
                return refused, None
            return None, hold

    def _next_package(self, learner: Learner | None = None) -> str | None:
        """The first exam whose pool is short: the learner's active exam first, then the others they study
        (the owner studies every exam)."""
        learner = learner or self.owner
        active = self.room.dojo.profile(learner)["active_package"]
        mine = list(self.room.packages.ids()) if learner.key == self.owner.key else self.room.dojo.enrolled(learner)
        for pid in [active] + [p for p in mine if p != active]:
            if pid not in mine:
                continue
            pkg = self.room.packages.by_id.get(pid)
            # An added exam with no practice exam (no stated duration, or a domain without a checked
            # source) has nothing to draw on the pool.
            if pkg and not blocker(pkg) and questions_in(self.room.pool(learner, pid)) < POOL_TARGET:
                return pid
        return None

    def _back_off(self, note: str, pid: str | None = None, gen: int | None = None, learner: Learner | None = None) -> int:
        """Nothing came of the last batch: wait twice as long, up to six hours. The first miss of a run
        is written to the quality log; the ones after it are not, so a bad day is one line, not fifty.

        The line is learner data, so it is not written for a generation that a deletion has overtaken,
        nor for a learner who no longer has a record."""
        learner = learner or self.owner
        luck = self._luck_of(learner)
        luck["misses"] += 1
        luck["wait"] = min(POOL_MAX_WAIT, max(POOL_MIN_WAIT, luck["wait"] * 2))
        self._note(learner, note=note)
        stale = gen is not None and gen != self._gen(learner)
        if luck["misses"] == 1 and pid and not stale and self._has_record(learner):
            self.room.dojo.quality(learner.key, "exam", {
                "package": pid, "part": "question pool", "text": "",
                "reason": f"{note}. The background pool waits longer before trying again."})
        return luck["wait"]

    def _luck_of(self, learner: Learner) -> dict:
        if learner.key == self.owner.key:
            return self.state
        return self._luck.setdefault(learner.key, {"wait": POOL_MIN_WAIT, "misses": 0})

    def _note(self, learner: Learner, **values: Any) -> None:
        """The keeper's status line is the owner's (Studio); a member's turn changes only their luck."""
        if learner.key == self.owner.key:
            self.state.update(values)
        else:
            luck = self._luck_of(learner)
            for k in ("wait", "misses"):
                if k in values:
                    luck[k] = values[k]

    def nudge(self, learner: Learner | None = None) -> None:
        """Something changed what a pool should hold (enrollment, an added exam): the learner's turn comes
        now instead of after a long wait (ADR 0009, team mode). Without a learner, everyone's turn comes now."""
        if learner is None:
            self._due.clear()
        else:
            self._due.pop(learner.key, None)
        self.wake.set()

    def _idle_wait(self) -> int:
        """How long to wait when there is nothing to do: six hours for one learner, as always; in team mode
        at most half an hour, so a new member or a new exam is never left with an empty pool for hours."""
        return POOL_TEAM_LOOK if self.scheduled else POOL_MAX_WAIT

    def step(self) -> int:
        """One turn of the keeper. Returns how many seconds to wait before the next one.

        With one learner this is that learner's turn, as it always was. With members, the learners take
        turns: the first whose own wait is over gets this turn, and the next turn starts after them."""
        people = self.learners()
        if len(people) == 1:
            return self._step_for(people[0])
        now = time.monotonic()
        n = len(people)
        start = self._turn % n
        waits = []
        for k in range(n):
            who = people[(start + k) % n]
            due = self._due.get(who.key, 0.0)
            if due > now:
                waits.append(due - now)
                continue
            before = self.state.get("batches", 0)
            wait = self._step_for(who)
            self._due[who.key] = now + wait
            if self.state.get("batches", 0) != before:   # a batch was paid for: the turn moves on
                self._turn = (start + k + 1) % n
                return POOL_MIN_WAIT
            waits.append(wait)
        return int(max(5, min(waits))) if waits else POOL_MIN_WAIT

    def _step_for(self, learner: Learner) -> int:
        """One learner's turn: today's single-learner step."""
        key = learner.key
        if key in self._paused:
            return POOL_MIN_WAIT
        if not self._has_record(learner):
            # Never the one to create a learner tree: after "delete everything" there is nothing to fill.
            self._note(learner, running=False, current=None,
                       note="your record was deleted, so the pool stays empty" if key in self._forgotten
                            else "there is no record to keep questions for")
            return self._idle_wait()
        self._forgotten.discard(key)   # the learner has a record again, so questions may wait for them again
        if self._busy(learner):
            self._note(learner, running=False, current=None, note="your own work comes first")
            return POOL_MIN_WAIT
        if self._spent(learner):
            self._note(learner, running=False, current=None, note=self._refusal(learner) or "the daily limit is reached")
            return self._idle_wait()
        pid = self._next_package(learner)
        if not pid:
            wait = self._idle_wait()
            self._note(learner, running=False, current=None, wait=wait, misses=0, note="every pool is full")
            return wait
        gen = self._gen(learner)
        # Everything a batch writes - the questions, and the quality line about a bad one - happens
        # inside this lock, so a deletion waiting on it cannot be overtaken by a late write.
        # writing_as: if the record is deleted while the batch runs, its writes are dropped, and the
        # batch's model calls are attributed to this learner (ADR 0009).
        with self._batch, self.room.store.writing_as(key):
            if key in self._paused or gen != self._gen(learner) or not self._has_record(learner):
                return POOL_MIN_WAIT
            # One of the three job slots, so the pool waits its turn with the learner's own work
            # instead of adding a fourth model call beside it. Scheduled, the scheduler holds it.
            slots = None if self.scheduled else self.room.dojo.jobs.slots
            if slots is not None and not slots.acquire(timeout=POOL_SLOT_WAIT):
                self._note(learner, running=False, current=None, note="the work slots are busy")
                return POOL_MIN_WAIT
            try:
                kind = kind_needed(self.room.pool(learner, pid))
                refused, hold = self._pay(learner, pid, kind)
                if refused:
                    self._note(learner, running=False, current=None, note=refused)
                    return self._idle_wait()
                self.state["batches"] = self.state.get("batches", 0) + 1
                self.state.update(running=True, current=pid if key == self.owner.key else None)
                # Stale covers both ways a write could land where it must not: a deletion that bumped
                # the generation, and a record that is no longer there at all.
                try:
                    with spending(Spend(key, "pool", pid, hold)):
                        out = self.room.top_up(learner, pid, report=False, kind=kind,
                                               stale=lambda: gen != self._gen(learner) or not self._has_record(learner))
                finally:
                    if hold is not None:
                        hold.release()
            except Abandoned:
                return POOL_MIN_WAIT
            except Exception as e:  # noqa: BLE001 - a failed batch is logged, not fatal
                log.warning("pool top-up for %s failed: %s", pid, e)
                return self._back_off(f"the last batch failed: {e}", pid, gen, learner)
            finally:
                if slots is not None:
                    slots.release()
                self.state.update(running=False, current=None, last=iso())
            if out["added"]:
                if key == self.owner.key:
                    self.state["added"] += out["added"]
                self._note(learner, wait=POOL_MIN_WAIT, misses=0, note="")
                return POOL_MIN_WAIT
            if out["state"] == "stale":
                return POOL_MIN_WAIT
            return self._back_off(f"the last batch added no new question ({out['blocked']} blocked by the checker)",
                                  pid, gen, learner)

    def status(self) -> dict:
        """The owner's pool, for Studio. Members' pools are never shown (ADR 0009)."""
        budget = self._budget(self.owner)
        return {"running": self.state["running"], "added": self.state["added"], "last": self.state["last"],
                "pool": {pid: questions_in(self.room.pool(self.owner, pid)) for pid in self.room.packages.ids()},
                "mix": {pid: kinds_in(self.room.pool(self.owner, pid)) for pid in self.room.packages.ids()},
                "target_mix": dict(exam_items.POOL_MIX),
                "target": POOL_TARGET, "batches_today": budget["batches"], "daily_limit": POOL_DAILY_BATCHES,
                "wait_seconds": self.state["wait"], "misses": self.state["misses"], "note": self.state["note"]}
