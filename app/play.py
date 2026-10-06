"""Play, the personal layer (ADR 0010): mastery moments, a weekly rhythm and study points.

Everything shown is derived from the learner's own Record each time it is viewed. Play stores only the
learner's choices (`learners/<key>/social/play.json`): when Play was turned on, the rhythm's targets and
the moments the learner chose to hide. Play writes nothing to the Record except a reported pass, which
is the learner's own statement about an external award.

The rules that keep it honest:
- Points and study days depend only on the activities done. No rule reads a judgement, a score, a
  confidence, a pip or any schedule a result produced, so a miss can never earn more than a success.
- Nothing here feeds evidence, readiness, the plan or the suggestions; none of those import this module.
- Days are calendar days in Europe/Berlin and weeks start Monday 00:00 Europe/Berlin.
- A missing event is never read in the learner's favour: an answer with no matching issue time, or an
  exam with no start, earns nothing.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from .core import Learner, iso, parse_iso, sha256, utcnow
from .evidence import derive
from .plan import to_local

# ---------------------------------------------------------------- the points table

STUDY_DAY = 10          # once a day, for the first retrieval activity of the day
OPEN_ANSWER = 3         # a judged open answer, written or spoken, met or not
CHOICE_ANSWER = 1       # a multiple-choice answer (rehearsal or practice exam), right or not
ANSWER_CAP = 30         # open and multiple-choice answers together, per day
LESSON_VIEW = 2
LESSON_VIEWS = 5        # per day
LAB_CHECK = 10          # once per lab a day
LABS_A_WEEK = 3
DAY_CAP = 80
WEEK_CAP = 400
FLOOR_S = 5             # an answer sent sooner than this after its question earns nothing
CHOICES_FOR_A_DAY = 5   # multiple-choice answers that make a study day
EXAM_FOR_A_DAY = 10     # answered practice-exam questions that make a study day
TARGETS = (3, 4, 5, 6)
DEFAULT_TARGET = 3
WEEKS_SHOWN = 8

NOT_A_CERTIFICATE = "From Dojo's own questions. Not a certification."
SELF_REPORTED = "Self-reported. Dojo did not check it."


def local_day(at: str | datetime) -> date:
    return to_local(parse_iso(at) if isinstance(at, str) else at).date()


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------- activities from the Record

def activities(events: list[dict]) -> list[dict]:
    """Every activity Play counts, in the order of the Record: {"at", "day", "kind", "n", ...}.

    kind is "open" (a judged open answer), "choice" (a rehearsal answer), "exam" (a closed practice
    exam: n answers that earn, and whether it makes a study day), "lesson" or "lab". Judgements are
    never read beyond "was it judged": met or not, right or wrong, disputed or not, the result is the same.
    """
    issued: dict[str, datetime] = {}
    submitted: dict[str, datetime] = {}
    judged: set[str] = set()
    created: dict[str, datetime] = {}
    last_choice: dict[str, datetime] = {}
    started: dict[str, datetime] = {}
    exam_answers: dict[str, int] = {}
    out: list[dict] = []
    order: list[tuple[str, datetime, int]] = []  # open answers, placed by when they were sent
    for seq, ev in enumerate(events):
        kind, d = ev.get("type"), ev.get("data") or {}
        try:
            at = parse_iso(ev["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind == "item.issued":
            issued.setdefault(d.get("item"), at)
        elif kind == "answer.submitted":
            item = d.get("item")
            if item not in submitted:  # one answer per item; a resubmission earns nothing
                submitted[item] = at
                order.append((item, at, seq))
        elif kind == "answer.judged":
            judged.add(d.get("item"))  # a regrade is a second judgement of the same item: counted once
        elif kind == "rehearsal.created":
            created.setdefault(d.get("rehearsal"), at)
        elif kind == "mcq.answered":
            rid = d.get("rehearsal")
            before = last_choice.get(rid) or created.get(rid)
            last_choice[rid] = at
            if before is not None and (at - before).total_seconds() >= FLOOR_S:
                out.append({"at": at, "seq": seq, "kind": "choice", "n": 1})
        elif kind == "exam.started":
            started.setdefault(d.get("attempt"), at)
        elif kind == "exam.answered":
            exam_answers[d.get("attempt")] = exam_answers.get(d.get("attempt"), 0) + 1
        elif kind == "exam.closed":
            aid = d.get("attempt")
            begun = started.get(aid)
            if begun is None:
                continue
            seconds = max(0.0, (at - begun).total_seconds())
            answered = int(d.get("answered") or 0)
            # Answers reach the Record at the close, without their own times, so the floor applies to
            # the attempt: at most one point per 5 seconds between the start and the close.
            earning = min(exam_answers.pop(aid, 0), int(seconds // FLOOR_S))
            day = answered >= EXAM_FOR_A_DAY and seconds >= FLOOR_S * answered
            out.append({"at": at, "seq": seq, "kind": "exam", "n": earning, "study": day})
        elif kind == "lesson.viewed":
            out.append({"at": at, "seq": seq, "kind": "lesson", "n": 1})
        elif kind == "lab.checked":
            # Written only for the real sandbox (never the local demo); it must follow a run in that lab.
            if int(d.get("runs") or 0) >= 1 and not d.get("local_demo"):
                out.append({"at": at, "seq": seq, "kind": "lab", "n": 1, "lab": d.get("lab")})
    for item, at, seq in order:
        if item in judged and item in issued and (at - issued[item]).total_seconds() >= FLOOR_S:
            out.append({"at": at, "seq": seq, "kind": "open", "n": 1})
    out.sort(key=lambda a: a["at"])
    for a in out:
        a["day"] = to_local(a["at"]).date()
    return out


def boundary(events: list[dict], settings: dict | None) -> int | None:
    """The position in the Record from which this Play counts: just after the last event that existed when
    Play was turned on (its hash is kept as `after`). None means "Include my Record so far". A position,
    not a time: event times are whole seconds, so a time cannot separate a leave and a rejoin in one second."""
    if not settings or not settings.get("since"):
        return None
    after = settings.get("after")
    if after == "":  # the Record was empty when Play was turned on: every event counts
        return 0
    if after:
        for i in range(len(events) - 1, -1, -1):
            if events[i].get("hash") == after:
                return i + 1
    # Fail closed: no mark (a play.json from before the mark existed) or a mark that is no longer in the
    # Record (restored or changed while play.json survived) counts only what is strictly later than `since`.
    start = parse_iso(settings["since"])
    return next((i for i, ev in enumerate(events) if parse_iso(ev["at"]) > start), len(events))


def _after(acts: list[dict], since: str | None, start: int | None = None) -> list[dict]:
    if start is not None:
        return [a for a in acts if a["seq"] >= start]
    if not since:
        return acts
    begin = parse_iso(since)
    return [a for a in acts if a["at"] >= begin]


# ---------------------------------------------------------------- study days and points

def study_days(acts: list[dict]) -> set[date]:
    """The days that hold at least one retrieval activity. Lessons are exposure, not retrieval."""
    out: set[date] = set()
    choices: dict[date, int] = {}
    for a in acts:
        if a["kind"] in ("open", "lab") or (a["kind"] == "exam" and a["study"]):
            out.add(a["day"])
        elif a["kind"] == "choice":
            choices[a["day"]] = choices.get(a["day"], 0) + a["n"]
            if choices[a["day"]] >= CHOICES_FOR_A_DAY:
                out.add(a["day"])
    return out


def day_points(acts: list[dict]) -> dict[date, dict[str, int]]:
    """Points per day and kind, each with its own cap, then the day cap. Lab checks also have a weekly cap."""
    days: dict[date, dict[str, Any]] = {}
    labs_in_week: dict[date, int] = {}
    for a in acts:
        d = days.setdefault(a["day"], {"answers": 0, "lessons": 0, "labs": 0, "views": 0, "lab_ids": set()})
        if a["kind"] in ("open", "choice", "exam"):
            worth = OPEN_ANSWER if a["kind"] == "open" else CHOICE_ANSWER
            d["answers"] = min(ANSWER_CAP, d["answers"] + worth * a["n"])
        elif a["kind"] == "lesson":
            if d["views"] < LESSON_VIEWS:
                d["views"] += 1
                d["lessons"] += LESSON_VIEW
        elif a["kind"] == "lab":
            week = monday(a["day"])
            if a["lab"] not in d["lab_ids"] and labs_in_week.get(week, 0) < LABS_A_WEEK:
                d["lab_ids"].add(a["lab"])
                labs_in_week[week] = labs_in_week.get(week, 0) + 1
                d["labs"] += LAB_CHECK
    studied = study_days(acts)
    out: dict[date, dict[str, int]] = {}
    for day, d in days.items():
        parts = {"study_day": STUDY_DAY if day in studied else 0, "answers": d["answers"],
                 "lessons": d["lessons"], "labs": d["labs"]}
        out[day] = {**parts, "total": min(DAY_CAP, sum(parts.values()))}
    return out


def points(events: list[dict], today: date, since: str | None = None, start: int | None = None) -> dict:
    """This week's study points, a breakdown by kind, and the last weeks. Never a lifetime total."""
    acts = _after(activities(events), since, start)
    per_day = day_points(acts)
    studied = study_days(acts)
    this = monday(today)

    def week(start: date) -> dict:
        days = [per_day[d] for d in per_day if monday(d) == start]
        raw = sum(x["total"] for x in days)
        return {"week": start.isoformat(), "total": min(WEEK_CAP, raw),
                "by_kind": {k: sum(x[k] for x in days) for k in ("study_day", "answers", "lessons", "labs")},
                "study_days": sum(1 for d in studied if monday(d) == start)}

    first = monday(local_day(since)) if since else min((monday(d) for d in per_day), default=this)
    weeks = [week(this - timedelta(weeks=n)) for n in range(WEEKS_SHOWN - 1, -1, -1)]
    weeks = [w for w in weeks if date.fromisoformat(w["week"]) >= first]
    return {**week(this), "weeks": weeks,
            "caps": {"day": DAY_CAP, "week": WEEK_CAP, "answers": ANSWER_CAP}}


def week_before_today(events: list[dict], settings: dict | None, today: date) -> int:
    """This week's capped study points from the days before today only (events before 00:00 Europe/Berlin):
    a circle's bar reads this, so it can change at most once a day (ADR 0010 section 4). Never shown alone."""
    if not settings:
        return 0
    acts = _after(activities(events), settings.get("since"), boundary(events, settings))
    this = monday(today)
    return min(WEEK_CAP, sum(v["total"] for d, v in day_points(acts).items() if monday(d) == this and d < today))


def last_week(events: list[dict], settings: dict | None, today: date) -> int:
    """Last week's capped study points, Monday to Sunday, the same number the weeks chart shows: what the
    effort board groups by (ADR 0010 section 5). Never shown to anyone but the learner."""
    if not settings:
        return 0
    acts = _after(activities(events), settings.get("since"), boundary(events, settings))
    prev = monday(today) - timedelta(weeks=1)
    return min(WEEK_CAP, sum(v["total"] for d, v in day_points(acts).items() if monday(d) == prev))


# ---------------------------------------------------------------- the weekly rhythm

def target_for(targets: list[dict], start: date) -> int:
    """The target of the week that starts on this Monday: the latest one that took effect by its Sunday."""
    end = start + timedelta(days=6)
    chosen = targets[0]["days"]
    for t in targets:
        if date.fromisoformat(t["from"]) <= end:
            chosen = t["days"]
    return chosen


def rhythm(events: list[dict], settings: dict, today: date, start: int | None = None) -> dict | None:
    """Weeks met, from the Record. It never goes down: there is no run, no freeze and nothing to lose."""
    r = settings.get("rhythm") or {}
    targets = r.get("targets") or []
    if not targets:
        return None
    since = settings.get("since")
    studied = study_days(_after(activities(events), since, start))
    turned_on = date.fromisoformat(targets[0]["from"])
    # "Include my Record so far" (no since): every week the Record holds counts, at the first target.
    start = turned_on if since else min([turned_on, *studied])
    met = 0
    this_week = None
    week = monday(start)
    while week <= monday(today):
        target = target_for(targets, week)
        end = week + timedelta(days=6)
        needed = target
        if since and week == monday(turned_on) and turned_on != week:
            needed = math.ceil(target * ((end - turned_on).days + 1) / 7)
        days = sum(1 for d in studied if max(week, start) <= d <= end)
        if days >= needed:
            met += 1
        if week == monday(today):
            this_week = {"days": days, "needed": needed, "met": days >= needed}
        week += timedelta(weeks=1)
    last = monday(today) - timedelta(weeks=1)
    last_met = None
    if last >= monday(start):
        target = target_for(targets, last)
        needed = target
        if since and last == monday(turned_on) and turned_on != last:
            needed = math.ceil(target * ((last + timedelta(days=6) - turned_on).days + 1) / 7)
        last_met = sum(1 for d in studied if max(last, start) <= d <= last + timedelta(days=6)) >= needed
    nxt = monday(today) + timedelta(weeks=1)
    pending = target_for(targets, nxt)
    return {"on": bool(r.get("on")), "weeks_met": met, "this_week": this_week, "last_week_met": last_met,
            "target": target_for(targets, monday(today)),
            "next_target": pending if pending != target_for(targets, monday(today)) else None,
            "studied_today": today in studied}


# ---------------------------------------------------------------- moments

def moment_id(kind: str, pid: str, sid: str = "") -> str:
    return "mo-" + sha256(f"play-moment:{kind}:{pid}:{sid}")[:20]


def _qualifies(a: dict, kind: str, later_minutes: int) -> bool:
    if a["disputed"] or not a["unaided"] or not a["all_met"]:
        return False
    m = a["minutes_since_teaching"]
    if kind == "later":
        return m is not None and m >= later_minutes
    return a["changed_condition"] and (m is None or m >= later_minutes)


def skill_moments(pkg: dict, evidence: dict[str, dict], later_hours: int, rechecks: dict[str, str] | None = None) -> list[dict]:
    """The "later" and "new situation" moments of one exam, from the evidence as it stands now. Every
    supporting condition is re-checked on each call: a disputed answer moves the moment to the next one
    that qualifies, or removes it."""
    later_minutes = later_hours * 60
    rechecks = rechecks or {}
    meta = pkg["meta"]
    out = []
    for sid, s in pkg["skills"].items():
        attempts = evidence[sid]["attempts"]
        for kind in ("later", "changed"):
            if kind == "changed" and s.get("kind") != "scenario":
                continue
            first = next((a for a in attempts if _qualifies(a, kind, later_minutes)), None)
            if first is None:
                continue
            at = parse_iso(first["at"])
            refresh = None
            misses = [a for a in attempts if parse_iso(a["at"]) > at and a["total"] and not a["all_met"]]
            if misses:
                last_miss = parse_iso(misses[-1]["at"])
                if not any(parse_iso(a["at"]) > last_miss and _qualifies(a, kind, later_minutes) for a in attempts):
                    refresh = f"An answer on {to_local(last_miss).date().isoformat()} was not yet met."
            changed = rechecks.get(sid)
            if refresh is None and changed and parse_iso(changed) > at and not any(
                    parse_iso(a["at"]) > parse_iso(changed) and _qualifies(a, kind, later_minutes) for a in attempts):
                refresh = f"The official page for this skill changed on {to_local(parse_iso(changed)).date().isoformat()}."
            out.append({
                "id": moment_id(kind, meta["id"], sid), "kind": kind, "package": meta["id"], "exam": meta["exam"],
                "skill": sid, "skill_text": s["text"], "domain": s.get("domain_title", ""), "at": first["at"],
                "title": ("On your own, later" if kind == "later" else "In a new situation"),
                "made_by": ("An answer with no help, every point met, "
                            + ("at least {h} hours after the last teaching" if kind == "later"
                               else "in a scenario with a changed condition").format(h=later_hours) + "."),
                "line": NOT_A_CERTIFICATE, "refresh": refresh, "_item": first["item"],
            })
    return out


RELEARNED_BY = ("Three answers with no help and every point met, each on time on its own day, since the last "
                "answer not yet met.")


def relearned_moments(pkg: dict, evidence: dict[str, dict], gone: set[str] | None = None,
                      rechecks: dict[str, str] | None = None) -> list[dict]:
    """The "relearned" moment of each skill, then "every skill relearned" for a domain and "every domain
    relearned" for the exam. Know's schedule is re-derived from the skill's whole sequence of attempts on
    each call, so a dispute or regrade of any of the three successes (not only the last) moves or removes
    the moment, and the domain and exam moments above it. Know's ladder is scheduling, never evidence: Play
    reads it only to mark what already happened (ADR 0010 section 1, ADR 0011)."""
    from . import relearn  # moments only: points and the rhythm never read Know's schedule

    gone = gone or set()
    rechecks = rechecks or {}
    meta = pkg["meta"]
    skills_out: dict[str, dict] = {}
    for sid, s in pkg["skills"].items():
        attempts = sorted(evidence[sid]["attempts"], key=lambda a: a["at"])
        sch = relearn.schedule(attempts)
        if not sch["relearned_at"]:
            continue
        at = sch["relearned_at"]
        refresh = None
        if not sch["relearned"]:
            misses = [a for a in attempts if parse_iso(a["at"]) > parse_iso(at) and relearn.is_miss(a)]
            when = to_local(parse_iso(misses[-1]["at"])).date().isoformat() if misses else ""
            refresh = (f"An answer on {when} was not yet met. It is back in your recalls until it is relearned "
                       "again.")
        changed = rechecks.get(sid)
        # After a page change the tag goes only when the skill is relearned again (ADR 0010 section 1):
        # RELEARNED_DAYS on-time successes on different days after the change, none of them disputed.
        if refresh is None and changed and parse_iso(changed) > parse_iso(at) and sum(
                1 for t in sch["on_time_at"] if parse_iso(t) > parse_iso(changed)) < relearn.RELEARNED_DAYS:
            refresh = (f"The official page for this skill changed on {to_local(parse_iso(changed)).date().isoformat()}. "
                       "The tag goes once it is relearned again after the change.")
        skills_out[sid] = {
            "id": moment_id("relearned", meta["id"], sid), "kind": "relearned", "package": meta["id"],
            "exam": meta["exam"], "skill": sid, "skill_text": s["text"], "domain": s.get("domain_title", ""),
            "at": at, "title": "Relearned", "made_by": RELEARNED_BY,
            # The exact answer that first reached it (Know names its item), never a match by time: two
            # answers can share a whole second, and the Record boundary must see the right one.
            "line": NOT_A_CERTIFICATE, "refresh": refresh, "_item": sch["relearned_item"],
        }
    out = list(skills_out.values())
    domains_out = []
    for d in pkg.get("domains") or []:
        # Askable: a skill with a verified page whose pages are not all gone (ADR 0005/0006). A skill added
        # to the domain takes the moment away until it is relearned too.
        askable = [sid for sid, s in pkg["skills"].items()
                   if s.get("domain") == d["id"] and s.get("sources") and sid not in gone]
        if not askable or any(sid not in skills_out for sid in askable):
            continue
        last = max((skills_out[sid] for sid in askable), key=lambda m: m["at"])
        domains_out.append({
            "id": moment_id("domain", meta["id"], d["id"]), "kind": "domain", "package": meta["id"],
            "exam": meta["exam"], "skill": None, "skill_text": d["title"], "domain": "", "at": last["at"],
            "title": "Every skill relearned",
            "made_by": f"Every skill of this domain that Dojo can ask about is relearned ({len(askable)} "
                       + ("skill" if len(askable) == 1 else "skills") + ").",
            "line": NOT_A_CERTIFICATE,
            "refresh": ("One of its skills is worth a refresh." if any(skills_out[sid]["refresh"] for sid in askable)
                        else None),
            "_items": [skills_out[sid]["_item"] for sid in askable],
        })
    if pkg.get("domains") and len(domains_out) == len(pkg["domains"]):
        last = max(domains_out, key=lambda m: m["at"])
        out.append({
            "id": moment_id("exam", meta["id"]), "kind": "exam", "package": meta["id"], "exam": meta["exam"],
            "skill": None, "skill_text": "", "domain": "", "at": last["at"], "title": "Every domain relearned",
            "made_by": f"Every domain of {meta['exam']} has its moment: every skill Dojo can ask about is relearned.",
            "line": NOT_A_CERTIFICATE,
            "refresh": "One of its skills is worth a refresh." if any(m["refresh"] for m in domains_out) else None,
            "_items": [i for m in domains_out for i in m["_items"]],
        })
    return out + domains_out


def pass_moments(events: list[dict], packages: dict[str, dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for seq, ev in enumerate(events):
        if ev.get("type") == "credential.reported":
            d = ev.get("data") or {}
            if d.get("package"):
                latest[d["package"]] = {**d, "recorded": ev["at"], "_seq": seq}
    out = []
    for pid, d in latest.items():
        exam = d.get("exam") or (packages.get(pid) or {}).get("meta", {}).get("exam", pid)
        out.append({"id": moment_id("passed", pid), "kind": "passed", "package": pid, "exam": exam,
                    "skill": None, "skill_text": d.get("title") or "", "domain": "", "at": d.get("earned"),
                    "recorded": d["recorded"], "title": "Passed", "made_by": f"You reported passing {exam}.",
                    "line": SELF_REPORTED, "refresh": None, "_seq": d["_seq"]})
    return out


def moments(events: list[dict], packages: dict[str, dict], later_hours: int, since: str | None = None,
            rechecks: dict[str, dict[str, str]] | None = None, start: int | None = None,
            gone: dict[str, set[str]] | None = None) -> list[dict]:
    """Every moment whose support holds now, newest first. With a start (a position in the Record) or a
    since, only moments made from then on: by an answer sent, or a pass reported, at or after it."""
    out: list[dict] = []
    for pid, pkg in packages.items():
        ev = derive(events, pkg, later_hours)
        out += skill_moments(pkg, ev, later_hours, (rechecks or {}).get(pid))
        out += relearned_moments(pkg, ev, (gone or {}).get(pid), (rechecks or {}).get(pid))
    out += pass_moments(events, packages)
    if start is not None:
        sent: dict[str, int] = {}
        for seq, ev in enumerate(events):
            if ev.get("type") in ("answer.submitted", "answer.judged"):
                sent.setdefault((ev.get("data") or {}).get("item"), seq)
        # A domain or exam moment is made by the last of its supporting answers in the Record.
        out = [m for m in out if (m["_seq"] if "_seq" in m
                                  else max(sent.get(i, -1) for i in m.get("_items") or [m["_item"]])) >= start]
    elif since:
        begin = parse_iso(since)
        out = [m for m in out if parse_iso(m.get("recorded") or m["at"]) >= begin]
    out = [{k: v for k, v in m.items() if not k.startswith("_")} for m in out]
    return sorted(out, key=lambda m: m["at"], reverse=True)


# ---------------------------------------------------------------- the learner's Play (storage and routes)

class PlayRoom:
    """Reads and writes only `learners/<key>/social/play.json` and `play-offer.json`, plus the one Record
    event Play adds: a reported pass."""

    def __init__(self, dojo: Any):
        self.dojo = dojo
        self.store = dojo.store
        self.social: Any = None   # the social layer (app/play_social.py): Leave Play deletes its entries too

    def today(self) -> date:
        return local_day(utcnow())

    def settings(self, learner: Learner) -> dict | None:
        return self.store.read("learners", learner.key, "social", "play")

    def _save(self, learner: Learner, doc: dict) -> None:
        self.store.write("learners", learner.key, "social", "play", value=doc)

    def current_moments(self, learner: Learner) -> list[dict] | None:
        """Every moment on this learner's shelf now, hidden ones included; None while Play is off. The social
        layer re-checks a shared copy against this on every read."""
        doc = self.settings(learner)
        if doc is None:
            return None
        events = self.store.events(learner.key)
        later = self.dojo.profile(learner)["later_hours"]
        return moments(events, self.dojo.packages.by_id, later, doc.get("since"), self._rechecks(),
                       boundary(events, doc), self._gone())

    def _rechecks(self) -> dict[str, dict[str, str]]:
        drift = getattr(self.dojo, "drift", None)
        if drift is None:
            return {}
        out = {}
        for pid in self.dojo.packages.by_id:
            try:
                out[pid] = {sid: v["since"] for sid, v in drift.skill_states(pid).items() if v.get("since")}
            except Exception:  # a drift problem must never hide the shelf
                out[pid] = {}
        return out

    def _gone(self) -> dict[str, set[str]]:
        out = {}
        for pid in self.dojo.packages.by_id:
            try:
                out[pid] = set(self.dojo.gone_skills(pid))
            except Exception:  # noqa: BLE001 - a drift problem must never hide the shelf
                out[pid] = set()
        return out

    def view(self, learner: Learner) -> dict:
        doc = self.settings(learner)
        if doc is None:
            offer = self.store.read("learners", learner.key, "social", "play-offer", default={}) or {}
            return {"on": False, "offer": not offer.get("dismissed")}
        events = self.store.events(learner.key)
        today = self.today()
        later = self.dojo.profile(learner)["later_hours"]
        start = boundary(events, doc)
        found = moments(events, self.dojo.packages.by_id, later, doc.get("since"), self._rechecks(), start, self._gone())
        hidden = set(doc.get("hidden") or [])
        r = rhythm(events, doc, today, start)
        return {
            "on": True, "since": doc.get("since"), "today": today.isoformat(),
            "rhythm": r, "targets": list(TARGETS),
            "points": points(events, today, doc.get("since"), start),
            "moments": [m for m in found if m["id"] not in hidden],
            "hidden": sum(1 for m in found if m["id"] in hidden),
            "shelf_hint": bool(r and r["on"] and today.weekday() == 0 and r["last_week_met"] is False),
        }

    def join(self, learner: Learner, include_record: bool, days: int | None) -> dict:
        with self.store.lock:
            if self.settings(learner) is not None:
                raise HTTPException(409, "Play is already on.")
            if days is not None and days not in TARGETS:
                raise HTTPException(400, "Pick 3, 4, 5 or 6 study days a week.")
            today = self.today()
            events = self.store.events(learner.key)
            # The mark is the last Record event now; only later events count (see `boundary`).
            self._save(learner, {
                "version": 1, "since": None if include_record else iso(),
                "after": None if include_record else (events[-1]["hash"] if events else ""),
                "rhythm": {"on": days is not None, "targets": [{"from": today.isoformat(), "days": days}] if days else []},
                "hidden": [],
            })
            self.store.delete("learners", learner.key, "social", "play-offer")
        return self.view(learner)

    def update(self, learner: Learner, days: int | None, rhythm_on: bool | None, show_hidden: bool | None) -> dict:
        with self.store.lock:
            doc = self._on(learner)
            r = doc.setdefault("rhythm", {"on": False, "targets": []})
            today = self.today()
            if days is not None:
                if days not in TARGETS:
                    raise HTTPException(400, "Pick 3, 4, 5 or 6 study days a week.")
                if not r["targets"]:
                    r["targets"] = [{"from": today.isoformat(), "days": days}]
                else:
                    # A change applies from next Monday; this week keeps the target it started with.
                    nxt = (monday(today) + timedelta(weeks=1)).isoformat()
                    kept = [t for t in r["targets"] if t["from"] < nxt]
                    if target_for(kept, monday(today)) != days:
                        kept.append({"from": nxt, "days": days})
                    r["targets"] = kept
                if rhythm_on is None:
                    r["on"] = True  # saving a target turns the weekly target on, also after it was turned off
            if rhythm_on is not None:
                if rhythm_on and not r["targets"]:
                    r["targets"] = [{"from": today.isoformat(), "days": DEFAULT_TARGET}]
                r["on"] = bool(rhythm_on)
            if show_hidden:
                doc["hidden"] = []
            self._save(learner, doc)
        return self.view(learner)

    def hide(self, learner: Learner, moment: str) -> dict:
        with self.store.lock:
            doc = self._on(learner)
            hidden = doc.setdefault("hidden", [])
            if moment not in hidden:
                hidden.append(moment)
            self._save(learner, doc)
        return self.view(learner)

    def leave(self, learner: Learner) -> dict:
        """One click; deletes every Play file and keeps no marker that the learner left. The Record is untouched.
        The learner's circle memberships, shared copies and invitations go in the same request."""
        with self.store.lock:
            if self.social is not None:
                self.social.forget(learner)
            self.store.delete("learners", learner.key, "social", "play")
            self.store.delete("learners", learner.key, "social", "play-offer")
        return {"on": False, "offer": True}

    def dismiss_offer(self, learner: Learner) -> dict:
        if self.settings(learner) is None:
            self.store.write("learners", learner.key, "social", "play-offer", value={"dismissed": True})
        return self.view(learner)

    def report_pass(self, learner: Learner, pid: str, earned: str) -> dict:
        """"I passed": the learner's own statement, recorded in their Record. Never a score."""
        pkg = self.dojo.packages.get(pid)
        try:
            day = date.fromisoformat(earned)
        except ValueError:
            raise HTTPException(400, "The date must look like 2027-03-01.")
        if day > self.today() or day.year < 2015:
            raise HTTPException(400, "Give the day you passed: not in the future.")
        meta = pkg["meta"]
        with self.store.lock:
            events = self.store.events(learner.key)
            at = next((i for i in range(len(events) - 1, -1, -1) if events[i].get("type") == "credential.reported"
                       and (events[i].get("data") or {}).get("package") == pid), None)
            start = boundary(events, self.settings(learner))
            # The same pass again is a no-op, unless the earlier report predates this Play (rejoin starts empty).
            seen = (at is not None and events[at]["data"].get("earned") == day.isoformat()
                    and not (start is not None and at < start))
            if not seen:
                self.store.append_event(learner.key, "credential.reported", {
                    "package": pid, "exam": meta["exam"], "title": meta["title"], "earned": day.isoformat(),
                    "source": "self", "checked_at": None})
        return {"reported": True, "package": pid, "exam": meta["exam"], "earned": day.isoformat()}

    def _on(self, learner: Learner) -> dict:
        doc = self.settings(learner)
        if doc is None:
            raise HTTPException(409, "Play is off. Turn it on first.")
        return doc
