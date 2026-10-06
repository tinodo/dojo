"""The kinds of practice-exam item, their answers and their points (ADR 0012).

One place decides what an item looks like to the learner, what an answer to it may be, and how many
points an answer earns, so the exam room, the results and the readiness advice can never disagree.

Scoring follows Microsoft's score page: one point for each correctly answered component, no points
taken off for a wrong one, and the points shown in the question. A build list (sequence) is all or
nothing. An item without a `kind` is a single-choice question written before ADR 0012 and scores
exactly as it always did.
"""
from __future__ import annotations

from typing import Any

KINDS = ("single", "multi", "sequence", "match", "hotarea", "yesno", "case")
NEW_KINDS = KINDS[1:]
CASE_KINDS = ("single", "multi")      # the questions inside a case study
LOCKING = ("yesno", "case")           # sections that lock once the learner leaves them
LABELS = {
    "single": "Multiple choice", "multi": "Multiple answers", "sequence": "Build list",
    "match": "Drag and drop", "hotarea": "Hot area", "yesno": "Yes/No series", "case": "Case study",
}
# Everything that gives the answer away. Held back until the answers are shown, in the page and in the export.
KEY_FIELDS = ("answer", "answers", "order", "pairs", "blank_answers", "yes", "rationale", "quote", "source", "quotes")


def kind_of(it: dict) -> str:
    k = it.get("kind")
    return k if k in KINDS else "single"


def size(it: dict) -> int:
    """How many exam questions the item is: a yes/no statement and a case-study question each count as one."""
    k = kind_of(it)
    if k == "yesno":
        return len(it.get("statements") or [])
    if k == "case":
        return len(it.get("questions") or [])
    return 1


def max_points(it: dict) -> int:
    k = kind_of(it)
    if k == "multi":
        return int(it.get("choose") or len(it.get("answers") or []))
    if k == "match":
        return len(it.get("targets") or [])
    if k == "hotarea":
        return len(it.get("blanks") or [])
    if k == "yesno":
        return len(it.get("statements") or [])
    if k == "case":
        return sum(max_points(q) for q in it.get("questions") or [])
    return 1


def _response(given: dict | None) -> dict:
    if not isinstance(given, dict):
        return {}
    r = given.get("response")
    return r if isinstance(r, dict) else {}


def points(it: dict, given: dict | None) -> int:
    """Points earned: one per correctly answered component, nothing taken off for a wrong one."""
    if not given:
        return 0
    k = kind_of(it)
    if k == "single":
        return 1 if given.get("choice") is not None and given.get("choice") == it.get("answer") else 0
    r = _response(given)
    if k == "multi":
        picked = [c for c in r.get("choices") or [] if isinstance(c, int)]
        return len(set(picked[: max_points(it)]) & set(it.get("answers") or []))
    if k == "sequence":
        return 1 if list(r.get("order") or []) == list(it.get("order") or []) else 0
    if k == "match":
        key, got = it.get("pairs") or [], r.get("pairs") or []
        return sum(1 for t, want in enumerate(key) if t < len(got) and got[t] is not None and got[t] == want)
    if k == "hotarea":
        key, got = it.get("blank_answers") or [], r.get("picks") or []
        return sum(1 for b, want in enumerate(key) if b < len(got) and got[b] is not None and got[b] == want)
    if k == "yesno":
        key, got = it.get("yes") or [], r.get("yes") or []
        return sum(1 for s, want in enumerate(key) if s < len(got) and isinstance(got[s], bool) and got[s] == want)
    if k == "case":
        parts = r.get("parts") or {}
        return sum(points(q, {"response": parts[str(n)]} if kind_of(q) != "single" else
                          {"choice": (parts.get(str(n)) or {}).get("choice")})
                   for n, q in enumerate(it.get("questions") or []) if isinstance(parts.get(str(n)), dict))
    return 0


def full(it: dict, given: dict | None) -> bool:
    return bool(given) and points(it, given) >= max_points(it)


def answered_parts(it: dict, given: dict | None) -> int:
    """How many of the item's questions have an answer (a yes/no statement or a case question each count)."""
    if not given:
        return 0
    k = kind_of(it)
    r = _response(given)
    if k == "yesno":
        return sum(1 for v in r.get("yes") or [] if isinstance(v, bool))
    if k == "case":
        return sum(1 for n in range(len(it.get("questions") or [])) if (r.get("parts") or {}).get(str(n)))
    return 1


# ---------------------------------------------------------------- answers the page may send

def _index(value: Any, upto: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < upto:
        raise ValueError(f"No such {what}.")
    return value


def _sub_response(q: dict, raw: dict) -> dict:
    """An answer to a single-choice or multiple-answer question (alone or inside a case study)."""
    k = kind_of(q)
    if k == "single":
        return {"choice": _index(raw.get("choice"), len(q["options"]), "option")}
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Choose at least one option.")
    picked = [_index(c, len(q["options"]), "option") for c in choices]
    if len(set(picked)) != len(picked):
        raise ValueError("An option was chosen twice.")
    if len(picked) > max_points(q):
        raise ValueError(f"Choose no more than {max_points(q)}.")
    return {"choices": sorted(picked)}


def _cleared(k: str, raw: dict) -> bool:
    """The page took every choice back: no option ticked, no step in the list, no value picked."""
    if k == "multi":
        return raw.get("choices") == []
    if k == "sequence":
        return raw.get("order") == []
    # A matching or hot-area answer is cleared only by a full-length list with nothing picked (checked in apply).
    return False


def apply(it: dict, given: dict | None, raw: dict, part: int | None = None) -> dict:
    """The stored answer after this one: `given` is what was stored, `raw` what the page sent. An empty
    dict means no answer is left. Raises ValueError (said in plain words) for an answer the item cannot
    take, and LookupError for a yes/no statement that can no longer be answered."""
    k = kind_of(it)
    if not isinstance(raw, dict):
        raise ValueError("The answer is missing.")
    if k == "single":
        return {"choice": _index(raw.get("choice"), len(it["options"]), "option")}
    old = dict(_response(given))
    if _cleared(k, raw):
        # Everything taken back: no answer, as before one was given. A yes/no statement cannot be taken back.
        return {}
    if k == "multi":
        return {"response": _sub_response(it, raw)}
    if k == "sequence":
        order = raw.get("order")
        if not isinstance(order, list) or not order:
            raise ValueError("Put at least one step in the list.")
        steps = [_index(s, len(it["options"]), "step") for s in order]
        if len(set(steps)) != len(steps):
            raise ValueError("A step is in the list twice.")
        return {"response": {"order": steps}}
    if k == "match":
        pairs = raw.get("pairs")
        if not isinstance(pairs, list) or len(pairs) != len(it["targets"]):
            raise ValueError("Give one value (or none) for every target.")
        if all(p is None for p in pairs):
            return {}
        return {"response": {"pairs": [None if p is None else _index(p, len(it["options"]), "value") for p in pairs]}}
    if k == "hotarea":
        picks = raw.get("picks")
        if not isinstance(picks, list) or len(picks) != len(it["blanks"]):
            raise ValueError("Give one value (or none) for every drop-down.")
        if all(p is None for p in picks):
            return {}
        return {"response": {"picks": [None if p is None else _index(p, len(b["options"]), "value")
                                       for p, b in zip(picks, it["blanks"])]}}
    if k == "yesno":
        n = _index(part, len(it["statements"]), "statement")
        if not isinstance(raw.get("yes"), bool):
            raise ValueError("Answer Yes or No.")
        yes = list(old.get("yes") or [None] * len(it["statements"]))
        done = sum(1 for v in yes if isinstance(v, bool))
        if n < done:
            raise LookupError("You have moved past this statement, so its answer cannot be changed.")
        if n > done:
            # One at a time, in order: skipping ahead would lock the statements before it unanswered.
            raise ValueError(f"Answer statement {done + 1} first: the statements are answered in order.")
        yes[n] = raw["yes"]
        return {"response": {"yes": yes}}
    if k == "case":
        n = _index(part, len(it["questions"]), "question")
        parts = dict(old.get("parts") or {})
        if raw.get("choices") == [] and kind_of(it["questions"][n]) == "multi":
            parts.pop(str(n), None)
        else:
            parts[str(n)] = _sub_response(it["questions"][n], raw)
        return {"response": {"parts": parts}} if parts else {}
    raise ValueError("This question cannot be answered.")


# ---------------------------------------------------------------- what the learner may see

def public(it: dict) -> dict:
    """The item without its answer key, for a page where it can still be answered (and for the export)."""
    out = {k: v for k, v in it.items() if k not in KEY_FIELDS}
    if kind_of(it) == "case":
        out["questions"] = [public(q) for q in it.get("questions") or []]
    return out


def shown(it: dict) -> dict:
    """The fields a page draws for an item, without its key."""
    k = kind_of(it)
    out: dict = {"kind": k, "label": LABELS[k], "stem": it.get("stem", ""), "points": max_points(it), "size": size(it)}
    if k in ("single", "multi", "sequence", "match"):
        out["options"] = it.get("options") or []
    if k == "multi":
        out["choose"] = max_points(it)
    if k == "sequence":
        out["steps"] = len(it.get("order") or [])
    if k == "match":
        out["targets"] = it.get("targets") or []
    if k == "hotarea":
        out["text"] = it.get("text", "")
        out["blanks"] = [{"options": b.get("options") or []} for b in it.get("blanks") or []]
    if k == "yesno":
        out["statements"] = it.get("statements") or []
    if k == "case":
        out["title"] = it.get("title", "")
        out["exhibits"] = it.get("exhibits") or []
        out["questions"] = [shown(q) for q in it.get("questions") or []]
    return out


def key(it: dict) -> dict:
    """The answer key of an item, for the page once the answers are shown."""
    k = kind_of(it)
    out: dict = {"rationale": it.get("rationale", ""), "quote": it.get("quote", ""), "quotes": quotes(it)}
    if k == "single":
        out["answer"] = it.get("answer")
    elif k == "multi":
        out["answers"] = it.get("answers") or []
    elif k == "sequence":
        out["order"] = it.get("order") or []
    elif k == "match":
        out["pairs"] = it.get("pairs") or []
    elif k == "hotarea":
        out["blank_answers"] = it.get("blank_answers") or []
    elif k == "yesno":
        out["yes"] = it.get("yes") or []
    elif k == "case":
        out["questions"] = [key(q) for q in it.get("questions") or []]
    return out


def response_view(it: dict, given: dict | None) -> dict | None:
    """The learner's answer, as the page draws it."""
    if not given:
        return None
    if kind_of(it) == "single":
        return {"choice": given.get("choice")}
    return _response(given)


def quotes(it: dict) -> list[dict]:
    """Every quote an item's key rests on, with the page it is from: the first is `quote`/`source`."""
    out: list[dict] = []
    if kind_of(it) == "case":
        # A case study's key is its questions' keys; each quote already says which question it is for.
        for n, q in enumerate(it.get("questions") or []):
            pre = f"question {n + 1}"
            out += [dict(x, part=x["part"] if x["part"].startswith(pre) else f"{pre}: {x['part']}") for x in quotes(q)]
        return out
    for q in it.get("quotes") or []:
        if isinstance(q, dict) and q.get("quote") and isinstance(q.get("source"), dict):
            out.append({"part": q.get("part", ""), "quote": q["quote"], "source": q["source"]})
    if not out and it.get("quote") and isinstance(it.get("source"), dict):
        out.append({"part": "answer", "quote": it["quote"], "source": it["source"]})
    return out


def unit_text(it: dict) -> str:
    """What makes an item the same item: its stem, and for a case study its title too."""
    if kind_of(it) == "case":
        return f'{it.get("title", "")} {it.get("stem", "")}'
    return it.get("stem", "")


# ---------------------------------------------------------------- the mix (ADR 0012)

def mix(count: int) -> dict[str, int]:
    """How many items of each kind an exam of `count` questions holds. A series counts 3 questions and a
    case study 4. Below 10 questions the exam stays single choice."""
    if count < 10:
        return {"single": count}
    m = {"multi": round(0.10 * count), "sequence": max(1, round(0.06 * count)),
         "match": max(1, round(0.06 * count)), "hotarea": max(1, round(0.06 * count)),
         "yesno": 1 if count >= 12 else 0, "case": 1 if count >= 30 else 0}
    used = m["multi"] + m["sequence"] + m["match"] + m["hotarea"] + 3 * m["yesno"] + 4 * m["case"]
    return {"single": max(0, count - used), **m}


YESNO_SIZE = 3
CASE_SIZE = 4
UNIT_SIZE = {"yesno": YESNO_SIZE, "case": CASE_SIZE}
# The background pool keeps 24 questions ready per exam, shared over the kinds like a full exam's mix.
POOL_MIX = {"single": 12, "multi": 2, "sequence": 1, "match": 1, "hotarea": 1, "yesno": 1, "case": 1}


def questions_in(mix_: dict[str, int]) -> int:
    return sum(n * UNIT_SIZE.get(k, 1) for k, n in mix_.items())
