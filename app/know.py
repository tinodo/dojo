"""Know what you know: how sure the learner was, next to how often they were right (ADR 0011).

Confidence is a self-report. It is asked after an answer is committed and before any feedback, and it is
kept in the answer's own event (`answer.submitted`, `mcq.answered`). Nothing here fills a pip, changes a
grade or feeds readiness: these are counts shown back to the learner, nothing more.

Pure functions over the Record, so two learners never share anything."""
from __future__ import annotations

import re
from typing import Any

LEVELS = ("certain", "fair", "guess")
LEVEL_WORDS = {"certain": "Certain", "fair": "Fairly sure", "guess": "Guessing"}
CONFIDENCE = {"guess", "fair", "certain"}
MIN_ANSWERS = 5          # below this a rate is not shown: one answer would move it by more than 20 points
MAX_ERRORS = 10
MATCH = ("yes", "partly", "no")
_WORD = re.compile(r"[a-z0-9]{4,}")


def confidence_of(value: Any) -> str | None:
    return value if value in CONFIDENCE else None


def answers(events: list[dict], package: dict, evidence: dict[str, dict]) -> list[dict]:
    """Every judged answer in this exam that came with a confidence, in time order.

    Open answers: right means every point met; disputed judgements count neither way. Rehearsal
    questions: right means the correct option. Skipped confidence, and answers from before it was asked,
    are left out. Timed exam answers never carry it."""
    pid = package["meta"]["id"]
    asked: dict[str, str | None] = {}
    judged = _judged(events)
    out: list[dict] = []
    for i, ev in enumerate(events):
        d = ev.get("data") or {}
        if d.get("package") != pid or "confidence" not in d:
            continue
        if ev.get("type") == "answer.submitted":
            asked[d.get("item")] = confidence_of(d.get("confidence"))
        elif ev.get("type") == "mcq.answered" and confidence_of(d.get("confidence")) and d.get("skill") in package["skills"]:
            out.append({"skill": d["skill"], "at": ev["at"], "kind": "mcq", "confidence": d["confidence"],
                        "right": bool(d.get("correct")), "rehearsal": d.get("rehearsal"), "index": d.get("index"), "seq": i})
    for sid, e in evidence.items():
        for a in e["attempts"]:
            level = asked.get(a["item"])
            if level and not a["disputed"] and a["total"] > 0:
                out.append({"skill": sid, "at": a["at"], "kind": "open", "confidence": level, "right": bool(a["all_met"]),
                            "item": a["item"], "mode": a["mode"], "seq": judged.get((a["item"], a["at"]), 0)})
    return sorted(out, key=order)


def _judged(events: list[dict]) -> dict[tuple[str, str], int]:
    """Where each judgement sits in the Record, by item and time: Record times are to the second."""
    return {((ev.get("data") or {}).get("item"), ev.get("at")): i
            for i, ev in enumerate(events) if ev.get("type") == "answer.judged"}


def order(row: dict) -> tuple[str, int]:
    """Time order; two answers in the same second go in the order the Record wrote them."""
    return row["at"], row.get("seq", 0)


def table(rows: list[dict], right_key: str = "right") -> dict:
    """Per confidence level: right out of total, and a rate only from MIN_ANSWERS on."""
    out = {}
    for level in LEVELS:
        mine = [r for r in rows if r["confidence"] == level]
        right = sum(1 for r in mine if r[right_key])
        enough = len(mine) >= MIN_ANSWERS
        out[level] = {"label": LEVEL_WORDS[level], "right": right, "total": len(mine), "enough": enough,
                      "rate": round(100 * right / len(mine)) if enough else None}
    return out


def last_right(events: list[dict], package: dict, evidence: dict[str, dict]) -> dict[str, tuple[str, int]]:
    """When each skill was last answered right, whether or not a confidence came with it: every point
    met in an open answer that is not disputed, or the correct option of a quick question. As (time,
    place in the Record), so an answer in the same second as an error still counts as after it."""
    pid = package["meta"]["id"]
    out: dict[str, tuple[str, int]] = {}
    judged = _judged(events)

    def seen(sid: str, when: tuple[str, int]) -> None:
        out[sid] = max(out.get(sid, NEVER), when)

    for i, ev in enumerate(events):
        d = ev.get("data") or {}
        if (ev.get("type") == "mcq.answered" and d.get("package") == pid and d.get("correct") is True
                and d.get("skill") in package["skills"]):
            seen(d["skill"], (ev["at"], i))
    for sid, e in evidence.items():
        for a in e["attempts"]:
            if a["all_met"] and a["total"] > 0 and not a["disputed"]:
                seen(sid, (a["at"], judged.get((a["item"], a["at"]), 0)))
    return out


NEVER = ("", -1)


def confident_errors(rows: list[dict], right_at: dict[str, tuple[str, int]] | None = None) -> list[dict]:
    """Wrong answers given as Certain (first) or Fairly sure, newest first, until a later answer on the
    same skill is right. `right_at` (last_right) counts every right answer, also one whose confidence
    was skipped: skipping keeps an answer out of the rates, never keeps an error on the list. Later
    means (time, place in the Record), because Record times are to the second."""
    last_right: dict[str, tuple[str, int]] = dict(right_at or {})
    for r in rows:
        if r["right"]:
            last_right[r["skill"]] = max(last_right.get(r["skill"], NEVER), order(r))
    errors = [r for r in rows if not r["right"] and r["confidence"] in ("certain", "fair")
              and last_right.get(r["skill"], NEVER) <= order(r)]
    errors.sort(key=order, reverse=True)
    errors.sort(key=lambda r: r["confidence"] != "certain")
    return errors[:MAX_ERRORS]


def calibration(package: dict, rows: list[dict], selfchecks: list[dict]) -> dict:
    skills = package["skills"]
    domains = []
    for d in package["domains"]:
        ids = {sid for g in d["groups"] for sid in g["skills"]}
        mine = [r for r in rows if r["skill"] in ids]
        domains.append({"id": d["id"], "title": d["title"], "answers": len(mine), "levels": table(mine)})
    by_skill = {}
    for sid in {r["skill"] for r in rows}:
        mine = [r for r in rows if r["skill"] == sid]
        by_skill[sid] = {"text": skills[sid]["text"], "answers": len(mine), "levels": table(mine)}
    own = [{**s, "right": s.get("matched") == "yes"} for s in selfchecks if confidence_of(s.get("confidence"))]
    return {"min_answers": MIN_ANSWERS, "answers": len(rows), "levels": table(rows), "domains": domains,
            "skills": by_skill, "selfchecks": {"answers": len(own), "levels": table(own)}}


def words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def best_section(lesson: dict, quotes: list[str]) -> int | None:
    """The lesson section whose points' quotes share the most words with the given quotes; None if none
    shares any."""
    want = set().union(*(words(q) for q in quotes)) if quotes else set()
    if not want:
        return None
    best, score = None, 0
    for i, sec in enumerate(lesson.get("sections") or []):
        have = set().union(*(words(p.get("quote", "")) | words(p.get("text", "")) for p in sec.get("points") or [])) \
            if sec.get("points") else set()
        n = len(want & have)
        if n > score:
            best, score = i, n
    return best
