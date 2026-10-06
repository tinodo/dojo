"""What the learner's record shows, per skill. Pure functions over the event log: no I/O, no models.

Rules (Interaction-to-Evidence Contract v4 and the learning-integrity review):
- Every attempt is listed with its conditions. Nothing is ranked, scored or averaged into a verdict,
  and nothing here predicts an exam result.
- Help is what the learner saw for that item before answering: hints. A hint whose content could
  not be recorded counts as help of unknown size.
- Teaching is anything that taught the skill: a lesson, an answered question, feedback after an
  attempt, the rationale after a rehearsal question. "Time since teaching" counts from the latest.
  In a timed practice exam the rationales are held back until the end, so the clock starts when they
  are shown, not when the question was answered.
- Items are never repeated, so every attempt is on a new item.
- A disputed judgement is taken out of what the record claims while its dispute is open. It never upgrades
  anything: the standing state is never better than the judgements as they were made. A dispute is reviewed by the
  referee (ADR 0017): upheld, its judgement replaces the disputed one (answer.rejudged, as below); not upheld, the
  judgement counts again (dispute.reviewed).
- An answer judged again (answer.rejudged, ADR 0015) keeps its place: it was answered when it was answered,
  under the same conditions. The new judgement replaces the earlier one in what the record claims; the
  earlier one is listed with the attempt and no longer counts. A dispute is about the judgement that
  counted when it was filed: the dispute of an earlier judgement stays on record and does not carry over
  to the new one, which can be disputed in turn. The new feedback is teaching at the moment it is shown.
- Only probe and check items can show "without help"; practice offers hints, so it never counts as unaided.
- Multiple-choice rehearsal and practice-exam answers are listed separately, because they can be
  guesses. They fill no check.
- How the answer was given - typed, or said out loud and written down by a machine - is a condition,
  not help. It changes nothing about what counts (EG-30). "Spoken" is only recorded when Dojo's own
  transcription handed out a receipt for this very item before the answer arrived, so the record says
  no more than that: a recording for this question was written down. A claim it cannot confirm says so
  in plain words.
- A lesson view (read, listen, present or watch) is exposure, never evidence. It is
  recorded when it starts and counts as teaching until VIEW_MINUTES (30) after that start, the longest
  plausible play: a play may still be going, or be paused and resumed, well after it started. Further
  starts of the same lesson and way of viewing within those 30 minutes are not recorded again. An event
  that states a longer window counts until that window ends: a listen to a lesson's Deeper Listen, about
  six minutes of audio, states 45 minutes (ADR 0007). So, with the default 20 hours for "later",
  after a view at 09:00 a check at 05:15 the next day is not later, and one at 05:31 is. Views recorded
  before events stated a window get the same 30 minutes. A "podcast" view, which an earlier version
  accepted, counts the same way.
- Dojo cannot see what a podcast app plays. So an answer that could count as "later" (or, with no teaching
  at all, as "in a new situation") first asks whether the learner listened to that skill's lesson in a
  podcast app since the last teaching, when the podcast feed has ever listed or served that skill's episode.
  A downloaded episode stays on the phone when a newer lesson or new narration takes its place and when the
  feed is turned off, so this holds until the record is deleted (app/podcast.py, AIRED). What they say is
  recorded before the answer (podcast.declared). "Yes"
  is teaching at that moment, extent unknown: that answer counts as right after teaching. "No" changes
  nothing and is listed as a condition. Anything but an explicit "no" counts as "yes". A declared
  answer's time since teaching is measured to the declaration, just before sending, and never comes out
  longer than it would without one.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from .core import parse_iso

CHECKS = {
    "unaided": "Answered a new item fully, without help",
    "later": "Did it again without help, {hours}+ hours after the last teaching",
    "changed": "Handled a scenario with a changed condition, without help and not right after teaching",
}
STATE_LABELS = {
    "untested": "Not tested yet",
    "not_met": "Latest: not yet",
    "met_with_help": "Latest: met with help available",
    "met_unaided": "Latest: met without help",
    "met_unaided_later": "Latest: met without help, later",
}
RANK = {"untested": 0, "not_met": 1, "met_with_help": 2, "met_unaided": 3, "met_unaided_later": 4}
MODE_LABELS = {"probe": "Probe (no help offered)", "practice": "Practice (hints available)", "check": "Check (no help offered)"}
VIEW_MINUTES = 30


def duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def conditions(a: dict) -> list[str]:
    out = [MODE_LABELS.get(a.get("mode") or "", str(a.get("mode")))]
    if a["help_unknown"]:
        out.append("Help of unknown size")
    elif a["hints"]:
        out.append(f"{a['hints']} hint{'s' if a['hints'] > 1 else ''} before answering")
    elif a.get("mode") == "practice":
        out.append("No hints opened (hints were available)")
    else:
        out.append("No help")
    m = a["minutes_since_teaching"]
    if m is None:
        out.append("No teaching before")
    elif m < 15:
        out.append("Right after teaching")
    else:
        out.append(f"{duration(m)} after teaching")
    if a.get("podcast_declared") is False:
        out.append("Declared: no podcast listening " + ("since the last teaching" if m is not None else "before this answer"))
    elif a.get("podcast_declared"):
        out.append("Declared: listened in a podcast app before answering")
    out.append("New item (checked against earlier ones)")
    if a["changed_condition"]:
        out.append("Changed condition")
    if a.get("input") == "spoken":
        out.append("Spoken: Dojo wrote down a recording for this question before the answer was sent"
                   + (", and it was corrected before sending" if a.get("edited_before_send") else ""))
    elif a.get("input") == "spoken_unconfirmed":
        out.append("Sent as spoken, but Dojo has no record of writing down a recording for this question")
    if a["unverified_points"]:
        out.append(f"{a['unverified_points']} judgement(s) could not be verified and count as not met")
    for e in a.get("earlier") or []:
        out.append(f"Judged again with newer judging rules: the earlier judgement ({e['met']} of {e['total']}) no longer counts"
                   + (". Your dispute of it stays on record" if e["disputed"] else ""))
    if a["disputed"]:
        out.append("Disputed: not counted either way")
    return out


def taught(teach: dict[str, datetime], sid: str, at: datetime) -> datetime:
    """Record that this skill was taught at this moment, and answer with the latest teaching so far.

    The clock only ever moves forward. Teaching does not always arrive in the order it lands in the
    log - a lesson can go on covering a skill after a later event was written - so the latest teaching
    is the latest of all of them. Writing an earlier moment over a later one would make a check look
    more "later" than it is, which is exactly the way a missing fact must never be read."""
    if sid not in teach or at > teach[sid]:
        teach[sid] = at
    return teach[sid]


def derive(events: list[dict], package: dict, later_hours: int = 20) -> dict[str, dict]:
    pid = package["meta"]["id"]
    skills = package["skills"]
    out: dict[str, dict] = {
        sid: {"attempts": [], "rehearsal": [], "exposures": [], "last_teaching": None, "last_lesson": None, "lessons_viewed": 0, "asked": 0}
        for sid in skills
    }
    items: dict[str, dict] = {}
    hints: dict[str, int] = {}
    given: dict[str, dict] = {}  # how each answer was put in: typed, or spoken and written down
    said: dict[str, dict] = {}  # what the learner declared about podcast listening before answering
    unknown_help: set[str] = set()
    teach: dict[str, datetime] = {}
    until: dict[str, datetime] = {}
    # Which judgement of an item each dispute is about: 0 is the first, and every answer.rejudged adds one. A dispute
    # whose review found the judgement right (dispute.reviewed, not upheld, ADR 0017) no longer takes it out.
    disputed: dict[str, set[int]] = {}
    seen: Counter = Counter()
    for ev in events:
        item_id = (ev.get("data") or {}).get("item")
        if ev.get("type") == "answer.rejudged":
            seen[item_id] += 1
        elif ev.get("type") == "dispute.filed":
            disputed.setdefault(item_id, set()).add(seen[item_id])
        elif ev.get("type") == "dispute.reviewed" and (ev.get("data") or {}).get("outcome") == "not_upheld":
            disputed.get(item_id, set()).discard(seen[item_id])
    judgement: dict[str, int] = {}  # which judgement of each item counts, so far in the Record
    for ev in events:
        d = ev.get("data") or {}
        sid = d.get("skill")
        if ev.get("type") == "exam.rationales_shown" and d.get("package") == pid:
            # One moment, many skills: the answers and rationales of a timed exam are shown together.
            at = parse_iso(ev["at"])
            for s in d.get("skills") or []:
                if s in out:
                    taught(teach, s, at)
                    out[s]["last_teaching"] = _latest(teach[s], until.get(s)).isoformat(timespec="seconds")
            continue
        if d.get("package") != pid or sid not in out:
            continue
        at, kind, rec = parse_iso(ev["at"]), ev.get("type"), out[sid]
        if kind == "lesson.viewed":
            taught(teach, sid, at)
            # The play may still be going, and repeats within the event's window were not recorded.
            window = max(VIEW_MINUTES, int(d.get("repeats_within_min") or 0))
            until[sid] = max(until.get(sid, at), at + timedelta(minutes=window))
            rec["last_lesson"] = ev["at"]
            rec["lessons_viewed"] += 1
            rec["exposures"].append({"at": ev["at"], "modality": d.get("modality"), "lesson": d.get("lesson"),
                                     "extent": d.get("extent") or "unknown",
                                     **({"narration": d["narration"]} if d.get("narration") in ("deep", "slides") else {})})
        elif kind == "ask.answered":
            taught(teach, sid, at)
            rec["asked"] += 1
        elif kind in ("lab.opened", "lab.hint", "lab.ran", "lab.checked"):
            taught(teach, sid, at)
        elif kind == "podcast.declared":
            heard = d.get("heard") is not False
            said[d.get("item")] = {"at": at, "heard": heard, "prior": _latest(teach.get(sid), until.get(sid))}
            if heard:
                taught(teach, sid, at)
                rec["last_lesson"] = ev["at"]
                rec["exposures"].append({"at": ev["at"], "modality": "podcast", "lesson": d.get("lesson"),
                                         "extent": "unknown", "declared": True})
        elif kind == "item.issued":
            items[d.get("item")] = d
        elif kind == "hint.shown":
            hints[d.get("item")] = hints.get(d.get("item"), 0) + 1
            if not d.get("described", True):
                unknown_help.add(d.get("item"))
        elif kind == "answer.submitted":
            given[d.get("item")] = {"input": d.get("input") or "typed", "edited_before_send": bool(d.get("edited_before_send"))}
        elif kind == "answer.judged":
            item_id = d.get("item")
            issued = items.get(item_id, {})
            prior = _latest(teach.get(sid), until.get(sid))
            how = given.get(item_id) or {}
            total, met = int(d.get("total") or 0), int(d.get("met") or 0)
            minutes = [] if prior is None else [max(0, int((at - prior).total_seconds() // 60))]
            decl = said.get(item_id)
            if decl:
                # Measured to the declaration, just before sending, so a slow or repeated judgement cannot stretch it.
                before = decl["at"] if decl["heard"] else decl["prior"]
                minutes += [] if before is None else [max(0, int((decl["at"] - before).total_seconds() // 60))]
            attempt = {
                "item": item_id, "at": ev["at"], "mode": d.get("mode") or issued.get("mode"), "kind": issued.get("kind"),
                "changed_condition": bool(issued.get("changed_condition")), "met": met, "total": total,
                "all_met": total > 0 and met == total, "hints": hints.get(item_id, 0),
                "help_unknown": item_id in unknown_help,
                "input": how.get("input", "typed"), "edited_before_send": bool(how.get("edited_before_send")),
                "minutes_since_teaching": min(minutes) if minutes else None,
                "podcast_declared": decl["heard"] if decl else None,
                "disputed": judgement.get(item_id, 0) in disputed.get(item_id, ()), "feedback_shown": bool(d.get("feedback_shown")),
                "unverified_points": int(d.get("unverified") or 0),
            }
            attempt["unaided"] = attempt["mode"] in ("probe", "check") and attempt["hints"] == 0 and not attempt["help_unknown"]
            attempt["conditions"] = conditions(attempt)
            rec["attempts"].append(attempt)
            taught(teach, sid, at)  # rubric, model answer and feedback are shown right after judging
        elif kind == "answer.rejudged":
            item_id = d.get("item")
            n = judgement[item_id] = judgement.get(item_id, 0) + 1
            attempt = next((a for a in reversed(rec["attempts"]) if a["item"] == item_id), None)
            if attempt is not None:
                total, met = int(d.get("total") or 0), int(d.get("met") or 0)
                attempt.setdefault("earlier", []).append({"at": attempt.get("rejudged_at") or attempt["at"], "met": attempt["met"],
                                                          "total": attempt["total"], "disputed": attempt["disputed"]})
                attempt.update({"met": met, "total": total, "all_met": total > 0 and met == total,
                                "unverified_points": int(d.get("unverified") or 0), "feedback_shown": bool(d.get("feedback_shown")),
                                "disputed": n in disputed.get(item_id, ()), "rejudged_at": ev["at"]})
                attempt["conditions"] = conditions(attempt)
                taught(teach, sid, at)  # the new judgement's feedback is shown when it is made
        elif kind == "mcq.answered":
            rec["rehearsal"].append({"at": ev["at"], "correct": bool(d.get("correct")), "rehearsal": d.get("rehearsal")})
            taught(teach, sid, at)  # the rationale is shown right after answering
        elif kind == "exam.answered":
            # A timed exam shows no rationale at answer time, so this teaches nothing yet and the
            # teaching clock is left alone. It fills no check either: it is selected response, whatever its
            # kind (single choice, or a kind of ADR 0012 such as a case study or a yes/no series).
            rec["rehearsal"].append({"at": ev["at"], "correct": bool(d.get("correct")), "exam": d.get("attempt"),
                                     "conditions": d.get("conditions")})
        latest = _latest(teach.get(sid), until.get(sid))
        if latest:
            rec["last_teaching"] = latest.isoformat(timespec="seconds")
    for sid, rec in out.items():
        rec.update(summarize(rec, skills[sid], later_hours))
    return out


def _latest(*times: datetime | None) -> datetime | None:
    return max((t for t in times if t), default=None)


def _state_of(attempt: dict | None, later_minutes: int) -> str:
    if attempt is None:
        return "untested"
    if not attempt["all_met"]:
        return "not_met"
    if not attempt["unaided"]:
        return "met_with_help"
    m = attempt["minutes_since_teaching"]
    return "met_unaided_later" if m is not None and m >= later_minutes else "met_unaided"


def summarize(rec: dict, skill: dict, later_hours: int) -> dict:
    attempts = rec["attempts"]
    valid = [a for a in attempts if not a["disputed"]]
    unaided_met = [a for a in valid if a["unaided"] and a["all_met"]]
    later_minutes = later_hours * 60

    def not_right_after(a: dict) -> bool:
        return a["minutes_since_teaching"] is None or a["minutes_since_teaching"] >= later_minutes

    checks = {
        "unaided": bool(unaided_met),
        "later": any(a["minutes_since_teaching"] is not None and a["minutes_since_teaching"] >= later_minutes for a in unaided_met),
    }
    if skill["kind"] == "scenario":
        checks["changed"] = any(a["changed_condition"] and not_right_after(a) for a in unaided_met)
    counted = _state_of(valid[-1] if valid else None, later_minutes)
    as_judged = _state_of(attempts[-1] if attempts else None, later_minutes)
    if RANK[counted] <= RANK[as_judged]:
        state, latest = counted, (valid[-1] if valid else None)
    else:
        state, latest = as_judged, attempts[-1]
    labels = {k: CHECKS[k].format(hours=later_hours) for k in checks}
    return {
        "state": state,
        "state_label": STATE_LABELS[state],
        "latest": latest,
        "checks": checks,
        "shown": [labels[k] for k, v in checks.items() if v],
        "not_yet_shown": [labels[k] for k, v in checks.items() if not v],
        "disputed": sum(1 for a in attempts if a["disputed"]),
        "rehearsal_correct": sum(1 for r in rec["rehearsal"] if r["correct"]),
        "rehearsal_total": len(rec["rehearsal"]),
    }


def coverage(package: dict, evidence: dict[str, dict]) -> dict:
    skills = package["skills"]
    not_shown = sum(s["share"] for sid, s in skills.items() if not evidence[sid]["checks"]["unaided"])
    states = Counter(e["state"] for e in evidence.values())
    return {
        "skills": len(skills),
        "weight_not_shown_unaided": round(not_shown * 100),
        "states": {k: states.get(k, 0) for k in STATE_LABELS},
        "state_labels": STATE_LABELS,
    }


def suggestions(package: dict, evidence: dict[str, dict], now: datetime, later_hours: int, limit: int = 5) -> list[dict]:
    """Next steps, most useful first. Only a suggestion: the learner can pick anything."""
    skills = package["skills"]
    later = timedelta(hours=later_hours)
    ranked: list[tuple[int, float, str, str, str]] = []
    for sid, e in evidence.items():
        if not skills[sid]["sources"]:
            continue  # an added exam's skill with no verified source is not taught or asked about
        share, latest = skills[sid]["share"], e["latest"]
        last_teaching = parse_iso(e["last_teaching"]) if e["last_teaching"] else None
        studied_since = bool(latest and e["last_lesson"] and parse_iso(e["last_lesson"]) > parse_iso(latest["at"]))
        if latest is None and e["disputed"]:
            ranked.append((4, -share, sid, "probe", "Your only attempt is disputed and not counted. Try a new item without help, or study the lesson first."))
        elif latest is None:
            ranked.append((4, -share, sid, "probe", "Not tested yet. Try one item without help; if you miss it, study the lesson next."))
        elif not latest["all_met"]:
            if studied_since:
                ranked.append((2, -share, sid, "practice", "You studied it after your last miss. Practise on a new item, with hints available."))
            else:
                ranked.append((1, -share, sid, "lesson", f"Your latest attempt met {latest['met']} of {latest['total']} points. Study the lesson next."))
        elif not latest["unaided"]:
            ranked.append((3, -share, sid, "check", "You met it in practice. Next: a new item without help."))
        elif not e["checks"]["later"]:
            if last_teaching and now - last_teaching >= later:
                ranked.append((2, -share, sid, "check", "You met it without help soon after teaching. A later check is due now."))
        elif e["checks"].get("changed") is False:
            ranked.append((5, -share, sid, "check", "Not yet shown on a scenario with a changed condition."))
    ranked.sort()
    return [
        {"skill": sid, "text": skills[sid]["text"], "domain": skills[sid]["domain_title"], "action": action, "why": why}
        for _, _, sid, action, why in ranked[:limit]
    ]


def due_later(package: dict, evidence: dict[str, dict], now: datetime, later_hours: int) -> list[dict]:
    out = []
    for sid, e in evidence.items():
        if e["checks"]["unaided"] and not e["checks"]["later"] and e["last_teaching"]:
            due = parse_iso(e["last_teaching"]) + timedelta(hours=later_hours)
            out.append({"skill": sid, "text": package["skills"][sid]["text"], "due": due.isoformat(timespec="minutes"), "due_now": now >= due})
    return sorted(out, key=lambda x: x["due"])


def plan(package: dict, evidence: dict[str, dict], exam_date: date | None, today: date) -> dict[str, Any]:
    skills = package["skills"]
    attempted = [e for e in evidence.values() if any(not a["disputed"] for a in e["attempts"])]
    not_unaided = sum(1 for e in evidence.values() if not e["checks"]["unaided"])
    not_later = sum(1 for e in evidence.values() if not e["checks"]["later"])
    days_left = (exam_date - today).days if exam_date else None
    weeks = days_left / 7 if days_left and days_left > 0 else None
    return {
        "exam_date": exam_date.isoformat() if exam_date else None,
        "days_left": days_left,
        "skills_total": len(skills),
        "never_attempted": len(skills) - len(attempted),
        "skills_not_shown_unaided": not_unaided,
        "attempted_without_later": sum(1 for e in attempted if not e["checks"]["later"]),
        "later_checks_per_week": math.ceil(not_later / weeks) if weeks else None,
        "note": "Plain arithmetic to help you pace yourself. It is not a prediction of your exam result.",
    }
