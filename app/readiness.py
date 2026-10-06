"""Should you book the exam? Dojo's advice — not a prediction.

The advice is Dojo's reading of the learner's own Record and their own practice exams. It never maps
anything to Microsoft's scaled score, never states a probability of passing, and never uses telemetry.
Every rule shows its definition and its current value, so the learner can disagree with the reasoning
rather than with a number. A rule Dojo cannot measure honestly says so instead of guessing, and it
never counts as met.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import items as exam_items
from .core import Learner, canonical, iso, new_id, parse_iso, sha256, utcnow

MOST = 0.5          # "most skills in a domain" - more than half
NEW_SHARE = 0.9     # rule 2 wants exams that are practically all new questions
ANSWERED_SHARE = 0.9  # ...and exams you actually sat: an exam left blank proves nothing
WEAK_GAP = 20       # a domain is weak when it sits this many points below the attempt's own average
WEAK_MIN = 5        # ...and only when that domain had at least this many questions to judge it on
RECENT_DAYS = 30    # "recent" practice exams


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def rule_one(package: dict, evidence: dict[str, dict], later_hours: int) -> dict:
    """Most skills in every domain shown on your own, and again later without help."""
    skills = package["skills"]
    domains = []
    for d in package["domains"]:
        sids = [sid for g in d["groups"] for sid in g["skills"]]
        later = [sid for sid in sids if evidence[sid]["checks"].get("later")]
        unaided = [sid for sid in sids if evidence[sid]["checks"].get("unaided")]
        domains.append({"id": d["id"], "title": d["title"], "skills": len(sids), "unaided": len(unaided),
                        "later": len(later), "met": len(later) > MOST * len(sids),
                        "missing": [{"skill": sid, "text": skills[sid]["text"]} for sid in sids if sid not in later][:3]})
    met = [d for d in domains if d["met"]]
    return {
        "id": "rule1", "title": "Most skills shown on your own in every domain",
        "definition": (f"More than half the skills in a domain answered fully on your own, with no help, and again at "
                       f"least {later_hours} hours after the last teaching. Written answers only: multiple choice "
                       f"fills no pip."),
        "met": len(met) == len(domains) and bool(domains),
        "value": f"{len(met)} of {len(domains)} domains", "domains": domains,
    }


def withdrawn_questions(attempt: dict) -> set[int]:
    """The questions withdrawn when the attempt closed because their official source changed while it ran
    (ADR 0006). They are not scored and not counted: not in the totals, not in any domain."""
    return {int(i) for i in attempt.get("withdrawn") or []}


def _maybe_new(items: list, i: int) -> bool:
    """False only for a question the attempt recorded as seen before (`new` is false)."""
    return not (0 <= i < len(items) and isinstance(items[i], dict) and items[i].get("new") is False)


def _size(items: list, i: int) -> int:
    return exam_items.size(items[i]) if 0 <= i < len(items) and isinstance(items[i], dict) else 1


def new_scored(attempt: dict) -> int:
    """How many of the scored questions were new. Each question records, when the exam is built, whether
    it was new, so a withdrawn question seen before takes nothing off the count. A withdrawn question
    without that record (an attempt built before it was kept) is taken to have been new: a missing fact
    is never read in the learner's favour. A yes/no series or a case study counts as the questions in it."""
    items = attempt.get("items") or []
    out = withdrawn_questions(attempt)
    lost = sum(_size(items, i) for i in out if _maybe_new(items, i))
    # Never more than the scored questions not recorded as seen before.
    most = sum(_size(items, i) for i in range(len(items)) if i not in out and _maybe_new(items, i))
    return max(0, min(int(attempt.get("new_questions") or 0) - lost, most))


def same_blueprint(attempt: dict, package: dict) -> bool:
    """Whether a practice exam followed the blueprint the package has now. An exam's skills and weights
    change when its study guide does (or when an added exam is removed and added again), and an attempt
    on an older blueprint says nothing about the current one. Attempts made before Dojo kept the mark
    have none: on a built-in package they are read as the current blueprint, because those blueprints
    did not change in the meantime; on an added package they never count."""
    mark = attempt.get("blueprint")
    if mark is None:
        return bool(package["meta"].get("builtin"))
    return mark == package["meta"]["blueprint"]


def _domain_scores(attempt: dict, package: dict) -> list[dict]:
    """Every domain of the current blueprint, including one the attempt never asked about: its scored
    questions and the points earned of the points possible (one per question before ADR 0012, when every
    question was single choice). A question on a skill the package does not have counts for
    nothing. A domain whose questions were all withdrawn stays in, with none, so it is never judged."""
    out = withdrawn_questions(attempt)
    by = {d["id"]: {"id": d["id"], "title": d["title"], "questions": 0, "correct": 0, "points": 0, "withdrawn": 0}
          for d in package["domains"]}
    for i, it in enumerate(attempt["items"]):
        d = by.get((package["skills"].get(it["skill"]) or {}).get("domain", ""))
        if d is None:
            continue
        if i in out:
            d["withdrawn"] += exam_items.size(it)
            continue
        d["questions"] += exam_items.size(it)
        d["points"] += exam_items.max_points(it)
        d["correct"] += exam_items.points(it, attempt["answers"].get(str(i)))
    for d in by.values():
        d["score"] = _pct(d["correct"], d["points"])
        # Too few to judge only because questions were withdrawn: the result says so.
        d["thinned"] = d["questions"] < WEAK_MIN <= d["questions"] + d["withdrawn"]
    return sorted(by.values(), key=lambda d: d["id"])


def read_attempt(attempt: dict, package: dict) -> dict:
    out = withdrawn_questions(attempt)
    scored = [i for i in range(len(attempt["items"])) if i not in out]
    total = sum(exam_items.size(attempt["items"][i]) for i in scored)
    answered = sum(exam_items.answered_parts(attempt["items"][i], attempt["answers"].get(str(i))) for i in scored)
    correct = sum(exam_items.points(attempt["items"][i], attempt["answers"].get(str(i))) for i in scored)
    possible = sum(exam_items.max_points(attempt["items"][i]) for i in scored)
    domains = _domain_scores(attempt, package)
    overall = _pct(correct, possible)
    judged = [d for d in domains if d["questions"] >= WEAK_MIN]
    too_few = [d["title"] for d in domains if d["questions"] < WEAK_MIN]
    weak = [d for d in judged if d["score"] <= overall - WEAK_GAP]
    new = new_scored(attempt)
    return {"id": attempt["id"], "closed_at": attempt.get("closed_at") or attempt["created"], "questions": total,
            "correct": correct, "points": possible, "score": overall, "new_questions": new, "new_share": _pct(new, total),
            "answered": answered, "answered_share": _pct(answered, total),
            "enough_answered": total > 0 and answered >= ANSWERED_SHARE * total,
            "conditions": attempt.get("conditions"), "length": attempt.get("length"), "domains": domains,
            "weak": [{"id": d["id"], "title": d["title"], "score": d["score"]} for d in weak],
            "too_few": too_few, "every_domain_judged": not too_few and bool(domains),
            "withdrawn": len(out), "thinned": [d["title"] for d in domains if d["thinned"]],
            "all_new": new >= NEW_SHARE * total, "exam_conditions": attempt.get("conditions") == "exam",
            "current_blueprint": same_blueprint(attempt, package)}


def rule_two(package: dict, attempts: list[dict], now: Any) -> dict:
    """Two recent practice exams under exam conditions, sat through, with new questions and no weak domain."""
    read = [read_attempt(a, package) for a in attempts]
    recent = [r for r in read if parse_iso(r["closed_at"]) >= now - timedelta(days=RECENT_DAYS)]
    good = [r for r in recent if r["current_blueprint"] and r["exam_conditions"] and r["all_new"]
            and r["enough_answered"] and r["every_domain_judged"] and not r["weak"]]
    latest = sorted(read, key=lambda r: r["closed_at"], reverse=True)[:3]
    return {
        "id": "rule2", "title": "Two recent practice exams with new questions and no weak domain",
        "definition": (f"Two practice exams in the last {RECENT_DAYS} days, taken under exam conditions (timed, no "
                       f"hints, no coach), where at least {round(NEW_SHARE * 100)}% of the questions were ones you had "
                       f"not seen, you answered at least {round(ANSWERED_SHARE * 100)}% of them, every domain had at "
                       f"least {WEAK_MIN} questions, and no domain scored {WEAK_GAP} points or more below your own "
                       f"average in that exam. An exam left blank scores zero everywhere and proves nothing, so it "
                       f"does not count. A domain with fewer than {WEAK_MIN} questions is not judged: too few "
                       f"questions to tell. A question withdrawn because its official source changed during the exam "
                       f"is not scored and does not count towards its domain; if that leaves a domain with fewer than "
                       f"{WEAK_MIN} questions, the exam does not count, and its result says so. Only exams on the "
                       f"exam's current blueprint count: when its skills or weights change, older practice exams no "
                       f"longer do."),
        "met": len(good) >= 2, "value": f"{len(good)} of 2", "attempts": latest, "counted": [r["id"] for r in good],
        "note": "Multiple-choice answers can be guesses, so these results are read apart from the pips and never mapped to a score.",
    }


def rule_three(items: list[dict], later_hours: int) -> dict:
    """Wrong ideas found in your own answers, re-checked later.

    Dojo can only count what it actually stores. A named misconception is stored on the item it was
    found in (checked by the gate before the learner ever read it), and items are never repeated, so
    the same wrong idea is never put to the learner twice. What Dojo can honestly say is whether the
    skill where it was found was later answered fully, on your own. Where nothing is stored, the rule
    says "not measured yet" - it never counts as met."""
    found = []
    for it in items:
        result = it.get("result") or {}
        if result.get("misconception"):
            found.append({"item": it["id"], "skill": it["skill"], "at": result.get("judged_at") or it["created"],
                          "text": result["misconception"]})
    rule = {
        "id": "rule3", "title": "The wrong ideas in your answers re-checked later, and gone",
        "definition": ("A wrong idea is one the grader named in your own answer and the independent checker let "
                       "through. Dojo never asks the same item twice, so it cannot re-ask the exact idea: it counts "
                       "the skill it was found in as cleared when a later answer on that skill was fully right, on "
                       "your own, at least " + str(later_hours) + " hours after the last teaching."),
        "met": False, "value": "not measured yet", "measured": bool(found), "found": len(found), "cleared": 0,
        "open": [],
    }
    if not found:
        rule["note"] = ("No wrong idea has been named and stored in your record yet. Dojo will not invent a number, "
                        "so this rule is not counted for or against you.")
        return rule
    return rule


def close_rule_three(rule: dict, found_by_skill: dict[str, list[dict]], evidence: dict[str, dict], later_hours: int) -> dict:
    cleared, still_open = [], []
    for sid, found in found_by_skill.items():
        newest = max(f["at"] for f in found)
        e = evidence.get(sid) or {}
        attempts = [a for a in e.get("attempts", []) if not a["disputed"] and a["at"] > newest]
        ok = any(a["all_met"] and a["unaided"] and (a["minutes_since_teaching"] or 0) >= later_hours * 60 for a in attempts)
        (cleared if ok else still_open).append({"skill": sid, "wrong": found[-1]["text"], "at": newest,
                                                "tries_since": len(attempts)})
    rule.update(met=bool(cleared) and not still_open, cleared=len(cleared), open=still_open[:5],
                value=f"{len(cleared)} of {len(cleared) + len(still_open)} cleared")
    return rule


def rule_four(package: dict, events: list[dict]) -> dict:
    """Microsoft's own practice assessment taken. It never blocks the advice."""
    meta = package["meta"]
    official = meta.get("official_practice_assessment")
    pid = meta["id"]
    taken = [ev for ev in events if ev.get("type") == "official_assessment.taken" and (ev.get("data") or {}).get("package") == pid]
    rule = {
        "id": "rule4", "title": "Microsoft's own practice assessment taken",
        "definition": "Dojo only links a practice assessment it has opened and verified on learn.microsoft.com. You tell Dojo when you have taken it; Dojo cannot see your result and does not want it.",
        "available": bool(official), "met": bool(official) and bool(taken), "blocking": False,
        "taken_at": taken[-1]["at"] if taken else None,
    }
    if official:
        rule.update(value="taken" if taken else "not taken yet", url=official.get("url"),
                    checked=official.get("checked"), source=official.get("found_on"),
                    note=f"Verified on {official.get('checked', 'the date recorded in the package')} from {official.get('found_on', 'the certification page')}.")
    else:
        rule.update(value="none verified",
                    note=(f"Dojo found no official practice assessment for {meta['exam']} on Microsoft Learn. "
                          + " ".join(n for n in (meta.get("notes") or []) if "practice assessment" in n.lower())).strip())
    return rule


def how_sure(rules: list[dict], evidence: dict[str, dict], attempts: int) -> dict:
    """How much evidence there is, in plain words. Never a probability, never a score."""
    tested = sum(1 for e in evidence.values() if e["checks"].get("unaided"))
    share = tested / max(len(evidence), 1)
    strong = sum(1 for r in rules if r["met"])
    if share < 0.3 or attempts < 1:
        level, words = "low", "There is little to go on yet: most skills have not been answered on your own, or you have taken no practice exam."
    elif share < 0.6 or attempts < 2:
        level, words = "some", "There is some evidence: part of the blueprint has been shown on your own, but not enough of it, or not often enough, to be sure."
    elif strong >= 2:
        level, words = "good", "There is a lot to go on: most of the blueprint has been shown on your own, and recent practice exams agree."
    else:
        level, words = "some", "There is plenty of evidence, but the rules do not agree yet."
    return {"level": level, "words": words, "skills_tested": tested, "skills": len(evidence), "attempts": attempts}


def moves(package: dict, rules: list[dict], evidence: dict[str, dict], lesson_skills: set[str]) -> list[dict]:
    """What this week can move: the shortest way to change a rule's value. Links, no calendar."""
    out: list[dict] = []
    r1, r2, r3 = rules[0], rules[1], rules[2]
    for d in sorted((d for d in r1["domains"] if not d["met"]), key=lambda d: d["later"] - d["skills"]):
        for m in d["missing"][:2]:
            e = evidence[m["skill"]]
            if not e["checks"].get("unaided"):
                what, why = ("probe", "Not answered on your own yet.")
            else:
                what, why = ("check", "Answered once on your own. Do it again, at least a day after the teaching.")
            out.append({"move": what, "skill": m["skill"], "text": m["text"], "domain": d["title"], "why": why,
                        "rule": "rule1", "lesson": m["skill"] in lesson_skills})
    if not r2["met"]:
        need = 2 - len(r2["counted"])
        out.append({"move": "exam", "why": f"{need} more practice exam{'s' if need != 1 else ''} with new questions and no weak domain.",
                    "rule": "rule2", "text": "Take a timed practice exam"})
    for o in (r3.get("open") or [])[:2]:
        out.append({"move": "check", "skill": o["skill"], "text": package["skills"][o["skill"]]["text"],
                    "why": f"A wrong idea was found here: {o['wrong']}", "rule": "rule3",
                    "lesson": o["skill"] in lesson_skills})
    return out[:6]


def withdrawn_note(n: int) -> str:
    return (f"{n} question{'s were' if n != 1 else ' was'} withdrawn because {'their' if n != 1 else 'its'} official "
            f"source changed during the exam.")


def _why_not(last: dict) -> str:
    """Why the last practice exam does not count for rule 2."""
    if not last["current_blueprint"]:
        return "it followed an earlier blueprint of this exam"
    if not last["enough_answered"]:
        return f"only {last['answered_share']}% of it answered"
    if last["weak"]:
        return f"a weak domain ({last['weak'][0]['title']})"
    if not last["every_domain_judged"]:
        if last.get("thinned"):
            n = last["withdrawn"]
            return (f"{n} question{'s' if n != 1 else ''} withdrawn because a source changed during it left too few "
                    f"questions to judge {', '.join(last['thinned'])}")
        return "too few questions per domain to judge"
    return "not enough new questions" if not last["all_new"] else "no exam conditions"


def reasons(rules: list[dict], evidence: dict[str, dict], package: dict) -> list[str]:
    """Why Dojo says what it says, in numbers taken from the learner's own record."""
    r1, r2, r3, r4 = rules
    out: list[str] = []
    untested = [sid for sid, e in evidence.items() if not e["checks"].get("unaided")]
    big = max(r1["domains"], key=lambda d: d["skills"]) if r1["domains"] else None
    if untested and big:
        here = sum(1 for sid in untested if package["skills"][sid].get("domain") == big["id"])
        out.append(f"{len(untested)} of {len(evidence)} skills have not been answered on your own yet, "
                   f"including {here} in the largest domain ({big['title']}).")
    elif not untested:
        out.append(f"All {len(evidence)} skills have been answered on your own at least once.")
    cold = [d for d in r1["domains"] if not d["later"]]
    thin = [d for d in r1["domains"] if d["later"] and not d["met"]]
    if cold:
        out.append(f"Nothing shown later yet in {len(cold)} of {len(r1['domains'])} domains — you have not had "
                   f"to remember it a day on.")
    if thin:
        out.append(f"{len(thin)} domain{'s have' if len(thin) != 1 else ' has'} some later evidence, but not on "
                   f"most of {'their' if len(thin) != 1 else 'its'} skills.")
    counted, seen = len(r2["counted"]), len(r2["attempts"])
    if counted >= 2:
        out.append(f"{counted} recent practice exams count: new questions, exam conditions, and no weak domain.")
    elif not seen:
        out.append("No practice exam finished yet, so there is nothing to read across domains.")
    elif counted == 1:
        out.append("One practice exam counts so far. One result is not a trend.")
    else:
        last = r2["attempts"][0]
        out.append(f"{seen} practice exam{'s' if seen != 1 else ''} finished, none counting yet — the last one: {_why_not(last)}.")
    if r3["measured"] and not r3["met"]:
        out.append(f"Wrong ideas found in your own answers: {r3['value']}.")
    elif not r3["measured"]:
        out.append("No wrong idea has been named in your answers yet, so rule 3 is not measured.")
    out.append(f"Microsoft's own practice assessment: {r4['value']}.")
    return out


def verdict(rules: list[dict], sure: dict) -> dict:
    """Dojo says what the evidence supports. It never refuses to advise, and it never predicts."""
    r1, r2, r3, r4 = rules
    core = [r1, r2] + ([r3] if r3["measured"] else [])
    met = [r for r in core if r["met"]]
    if len(met) == len(core):
        say, head = "book", "The evidence supports booking the exam."
        why = "Every rule Dojo can measure is met."
    elif r1["met"] or r2["met"]:
        say, head = "close", "You are close, but some capabilities remain unproven."
        why = "Some rules are met and others are not, so parts of the blueprint are still untested."
    else:
        say, head = "not-yet", "These capabilities remain unproven."
        why = "The rules Dojo can measure are not met yet."
    scope = ["This is Dojo's advice from your own record and Dojo's own questions, not a prediction of your result.",
             "Dojo does not know Microsoft's questions, its scale or its pass mark, and never maps anything to them.",
             "You can book whenever you like. If you book and it goes well, this advice was cautious; if it goes badly, you lose the fee and the wait."]
    if not r3["measured"]:
        scope.append("Rule 3 is not measured yet, so it counts neither way.")
    if not r4["available"]:
        scope.append(f"{r4['value'].capitalize()}: rule 4 cannot be met for this exam and does not block the advice.")
    elif not r4["met"]:
        scope.append("Microsoft's own practice assessment is still untaken. It is free, and it is the one check Dojo did not write.")
    return {"say": say, "headline": head, "why": why, "sure": sure, "scope": scope}


def advise(dojo: Any, room: Any, learner: Learner, pid: str) -> dict:
    """Everything the readiness screen shows, computed from the Record and the learner's practice exams."""
    package = dojo.packages.get(pid)
    profile = dojo.profile(learner)
    later_hours = int(profile.get("later_hours") or 20)
    events = dojo.store.events(learner.key)
    from .evidence import derive
    evidence = derive(events, package, later_hours)
    attempts = room.closed_attempts(learner, pid)
    current = [a for a in attempts if same_blueprint(a, package)]
    items = [d for d in (dojo.store.read("learners", learner.key, "items", n)
                         for n in dojo.store.names("learners", learner.key, "items"))
             if d and d.get("package") == pid]
    r1 = rule_one(package, evidence, later_hours)
    r2 = rule_two(package, attempts, utcnow())
    r3 = rule_three(items, later_hours)
    if r3["measured"]:
        by_skill: dict[str, list[dict]] = {}
        for it in items:
            result = it.get("result") or {}
            if result.get("misconception"):
                by_skill.setdefault(it["skill"], []).append(
                    {"at": result.get("judged_at") or it["created"], "text": result["misconception"]})
        r3 = close_rule_three(r3, by_skill, evidence, later_hours)
    r4 = rule_four(package, events)
    rules = [r1, r2, r3, r4]
    sure = how_sure(rules, evidence, len(current))
    out = {
        "package": pid, "exam": package["meta"]["exam"], "title": package["meta"]["title"], "at": iso(),
        "rules": rules, "moves": moves(package, rules, evidence, dojo.lesson_skills(pid)),
        "later_hours": later_hours, "attempts": len(current), "earlier_attempts": len(attempts) - len(current),
        **verdict(rules, sure), "reasons": reasons(rules, evidence, package),
    }
    out["fingerprint"] = sha256(canonical([out["say"], [r["value"] for r in rules], sure["level"]]))[:16]
    return out


def store_advice(dojo: Any, learner: Learner, advice: dict) -> dict:
    """Keep what was shown, so it can be compared later with a result the learner chooses to share."""
    today = advice["at"][:10]
    for name in dojo.store.names("learners", learner.key, "advice"):
        old = dojo.store.read("learners", learner.key, "advice", name)
        if old and old["package"] == advice["package"] and old["at"][:10] == today and old["fingerprint"] == advice["fingerprint"]:
            return old
    doc = {"id": new_id("adv"), "package": advice["package"], "at": advice["at"], "say": advice["say"],
           "headline": advice["headline"], "sure": advice["sure"]["level"], "fingerprint": advice["fingerprint"],
           "rules": [{"id": r["id"], "title": r["title"], "met": r["met"], "value": r["value"],
                      "definition": r["definition"]} for r in advice["rules"]],
           "inputs": {"attempts": advice["attempts"], "skills_tested": advice["sure"]["skills_tested"],
                      "skills": advice["sure"]["skills"], "later_hours": advice["later_hours"]},
           "outcome": None}
    dojo.store.write("learners", learner.key, "advice", doc["id"], value=doc)
    dojo.store.append_event(learner.key, "advice.given", {
        "package": advice["package"], "advice": doc["id"], "say": advice["say"], "sure": advice["sure"]["level"],
        "rules": {r["id"]: r["value"] for r in advice["rules"]}, "fingerprint": advice["fingerprint"]})
    return doc


def advice_log(dojo: Any, learner: Learner, pid: str | None = None) -> list[dict]:
    docs = (dojo.store.read("learners", learner.key, "advice", n) for n in dojo.store.names("learners", learner.key, "advice"))
    return sorted((d for d in docs if d and (pid is None or d["package"] == pid)), key=lambda d: d["at"], reverse=True)
