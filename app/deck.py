"""Present as a deck (ADR 0014).

The Present tab plays a deck of scenes. Every lesson gets a derived deck at no cost, built here from its
already checked content: a title, what the lesson covers, its parts, one scene per slide with the bullets
building as the narration reaches them, the worked example as steps, the misconceptions as traps, a recap
and the lesson's own self-check questions.

A lesson may also get a visual deck. ONE author-model call turns the checked lesson into a structured spec:
what the learner will be able to do, named parts, a diagram spec for the slides where one helps, and exam
tips. The independent gate model checks every element against the official excerpts, and whatever does not
hold is dropped. The browser draws each diagram from its spec; nothing in a picture is free text the gate
did not see, and no picture carries a claim of its own.

This module is pure: it validates, words the elements for the gate, applies the verdicts and composes the
scenes the browser plays. app/learning.py makes the model calls and stores the result.
"""
from __future__ import annotations

import re
from typing import Any, Callable

DECK_FORMAT = 1
VISUALS = ("flow", "cycle", "stack", "timeline", "hub", "compare", "contrast", "matrix", "keynum", "code")
ORDERED = frozenset({"flow", "cycle", "stack", "timeline"})   # one statement each: the order is the claim
TONES = ("good", "bad", "neutral")
# The icons of app/static/icons.svg a diagram may use. An icon is decoration and never carries a claim.
ICONS = frozenset(("today course live practice record plan studio search mic bell read listen watch people lab quiz "
                   "deck chat clock check x arrow flag sparkle help shield link phone play pause hand cc download "
                   "lock eye down right refresh code db moon alert share volume back skipb skipf file git more "
                   "video stop rss wand scale target home left pin").split())
CODE_LANGS = frozenset({"yaml", "json", "text", "shell", "powershell", "sql", "python", "csharp", "javascript",
                        "typescript", "http", "bicep", "xml", "ini", "markdown"})

# How long a piece of text on a slide may be, in characters. A longer one is not shortened: it is left out,
# because a cut sentence can say something the checked one did not.
LABEL, NOTE, HEAD, CELL, WHEN, UNIT, VALUE = 40, 90, 28, 64, 16, 18, 8
TITLE, OUTCOME, RECAP, TIP, POINT, KEYLABEL, QUOTE = 60, 110, 120, 170, 100, 110, 240
CODE_LINE, CODE_LINES, MARKS = 80, 14, 4
PART_WORDS = {"ideas": "The ideas", "practice": "In practice", "traps": "Watch out", "wrap": "Wrap-up"}
AGENDA_SLIDES = 6     # a derived agenda lists at most this many slide titles
RECAP_MAX = 5
CHECK_MAX = 3
_NUMBER = re.compile(r"^(?:[0-9][0-9,.]*%?|[0-9]+(?:\.[0-9]+)?\s?[kKmMbB])$")
_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*")
_SMALL = frozenset("a an and are as at be by can do does for from has have how if in into is it its not of on "
                   "or so than that the their them then there these they this to was what when where which who "
                   "why will with you your".split())

Normalize = Callable[[str], str]
QuoteOk = Callable[[str, int], bool]
CodeOk = Callable[[list[str], int], bool]


def _text(value: Any, cap: int) -> str | None:
    """One line of text within `cap` characters, or None."""
    if not isinstance(value, str):
        return None
    t = " ".join(value.split())
    return t if 0 < len(t) <= cap else None


def _opt(value: Any, cap: int) -> str | None:
    """An optional note: "" when absent, None when present but unusable."""
    if value is None or value == "":
        return ""
    return _text(value, cap)


def _icon(value: Any) -> str | None:
    return value if isinstance(value, str) and value in ICONS else None


def _int(value: Any, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return default


def _cue(value: Any) -> int | None:
    v = _int(value)
    return v if v >= 0 else None


def _items(raw: Any, cap: int, lo: int, hi: int) -> list[dict] | None:
    """The labelled parts of a flow, cycle or stack: all usable and between lo and hi of them, or None."""
    if not isinstance(raw, list) or not lo <= len(raw) <= hi:
        return None
    out = []
    for it in raw:
        if not isinstance(it, dict):
            return None
        label, note = _text(it.get("label"), cap), _opt(it.get("note"), NOTE)
        if label is None or note is None:
            return None
        out.append({"label": label, "note": note, "icon": _icon(it.get("icon")), "cue": _cue(it.get("cue"))})
    return out


def _side(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("title"), HEAD)
    points = [p for p in (_text(x, POINT) for x in (raw.get("points") or [])[:4]) if p] if isinstance(raw.get("points"), list) else []
    if title is None or not points:
        return None
    tone = raw.get("tone") if raw.get("tone") in TONES else "neutral"
    return {"title": title, "tone": tone, "icon": _icon(raw.get("icon")), "points": points}


def _bare(line: str) -> str:
    return " ".join(line.split())


def code_found(lines: list[str], text: str) -> bool:
    """Whether a snippet is the page's own code: every line that is not blank is a whole line of the page,
    word for word (spacing aside), in the page's order. Lines of the page may be left out between them, such
    as comments; none may be changed."""
    page = [_bare(x) for x in (text or "").split("\n")]
    wanted = [_bare(x) for x in lines if _bare(x)]
    at = 0
    for want in wanted:
        while at < len(page) and page[at] != want:
            at += 1
        if at == len(page):
            return False
        at += 1
    return bool(wanted)


def clean_visual(raw: Any, quote_ok: QuoteOk, code_ok: CodeOk, normalize: Normalize) -> tuple[dict | None, str]:
    """A diagram spec the browser can draw, or None and why not. Only the shapes below are drawn; a part that
    does not fit is left out, and a diagram left with too few parts is not drawn at all."""
    if not isinstance(raw, dict):
        return None, "not a diagram"
    kind = raw.get("type")
    if kind not in VISUALS:
        return None, f"unknown diagram type {str(kind)[:20]!r}"
    if kind in ("flow", "cycle"):
        steps = _items(raw.get("steps"), LABEL, 3, 6)
        return ({"type": kind, "steps": steps}, "") if steps else (None, f"a {kind} needs 3 to 6 short steps")
    if kind == "stack":
        layers = _items(raw.get("layers"), LABEL, 2, 5)
        return ({"type": kind, "layers": layers}, "") if layers else (None, "a stack needs 2 to 5 short layers")
    if kind == "timeline":
        events = raw.get("events")
        if not isinstance(events, list) or not 3 <= len(events) <= 6:
            return None, "a timeline needs 3 to 6 events"
        out = []
        for ev in events:
            when = _text(ev.get("when"), WHEN) if isinstance(ev, dict) else None
            items = _items([ev], LABEL, 1, 1) if when else None
            if not items:
                return None, "a timeline event needs a short time and label"
            out.append({**items[0], "when": when})
        return {"type": kind, "events": out}, ""
    if kind == "hub":
        center = _text(raw.get("center"), LABEL)
        spokes = [s for s in ((_items([x], LABEL, 1, 1) or [None])[0] for x in (raw.get("spokes") or [])[:6]) if s] \
            if isinstance(raw.get("spokes"), list) else []
        if center is None or len(spokes) < 3:
            return None, "a hub needs a centre and at least 3 short spokes"
        return {"type": kind, "center": center, "icon": _icon(raw.get("icon")), "spokes": spokes}, ""
    if kind == "compare":
        cols = raw.get("columns")
        if not isinstance(cols, list) or not 2 <= len(cols) <= 3 or any(_text(c, HEAD) is None for c in cols):
            return None, "a comparison needs 2 or 3 short column names"
        cols = [_text(c, HEAD) for c in cols]
        rows = []
        for r in (raw.get("rows") or [])[:5] if isinstance(raw.get("rows"), list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("cells"), list) or len(r["cells"]) != len(cols):
                continue
            label, cells = _text(r.get("label"), HEAD), [_text(c, CELL) for c in r["cells"]]
            if label and all(cells):
                rows.append({"label": label, "cells": cells, "cue": _cue(r.get("cue"))})
        if len(rows) < 2:
            return None, "a comparison needs at least 2 complete rows"
        return {"type": kind, "columns": cols, "rows": rows}, ""
    if kind == "contrast":
        left, right = _side(raw.get("left")), _side(raw.get("right"))
        if not left or not right:
            return None, "a contrast needs two sides, each with a title and points"
        return {"type": kind, "left": left, "right": right}, ""
    if kind == "matrix":
        axes = {}
        for a in ("x", "y"):
            ax = raw.get(a)
            vals = [_text(ax.get(k), HEAD) for k in ("label", "low", "high")] if isinstance(ax, dict) else [None]
            if not all(vals):
                return None, "a matrix needs two named axes with a low and a high end"
            axes[a] = dict(zip(("label", "low", "high"), vals))
        cells, seen = [], set()
        for c in raw.get("cells") or [] if isinstance(raw.get("cells"), list) else []:
            pos = (_int(c.get("x")), _int(c.get("y"))) if isinstance(c, dict) else (-1, -1)
            label, note = (_text(c.get("label"), LABEL), _opt(c.get("note"), NOTE)) if isinstance(c, dict) else (None, None)
            if pos in seen or pos[0] not in (0, 1) or pos[1] not in (0, 1) or label is None or note is None:
                return None, "a matrix needs exactly one short label in each of its four cells"
            seen.add(pos)
            cells.append({"x": pos[0], "y": pos[1], "label": label, "note": note, "cue": _cue(c.get("cue"))})
        if len(cells) != 4:
            return None, "a matrix needs exactly one short label in each of its four cells"
        return {"type": kind, "x": axes["x"], "y": axes["y"], "cells": sorted(cells, key=lambda c: (c["y"], c["x"]))}, ""
    if kind == "keynum":
        value, unit, label = _text(raw.get("value"), VALUE), _opt(raw.get("unit"), UNIT), _text(raw.get("label"), KEYLABEL)
        quote, n = _text(raw.get("quote"), QUOTE), _int(raw.get("source"))
        if not value or not _NUMBER.match(value) or unit is None or not label:
            return None, "a key number needs a short number, unit and label"
        if not quote or normalize(value) not in normalize(quote) or not quote_ok(quote, n):
            return None, "a key number needs a short exact quote from the page that contains the number"
        return {"type": kind, "value": value, "unit": unit, "label": label, "quote": quote, "source": n}, ""
    # code: lines copied from the page, word for word; the marks are notes on single lines
    lines, n = raw.get("lines"), _int(raw.get("source"))
    if not isinstance(lines, list) or not 1 <= len(lines) <= CODE_LINES or not all(isinstance(x, str) for x in lines):
        return None, f"a code snippet needs 1 to {CODE_LINES} lines"
    lines = [x.replace("\t", "  ").rstrip() for x in lines]
    if any(len(x) > CODE_LINE for x in lines) or not code_ok(lines, n):
        return None, "a code snippet must be copied word for word from the page"
    marks = []
    for m in (raw.get("marks") or [])[:MARKS] if isinstance(raw.get("marks"), list) else []:
        at, note = (_int(m.get("line")), _text(m.get("note"), NOTE)) if isinstance(m, dict) else (-1, None)
        if 0 <= at < len(lines) and lines[at].strip() and note and all(x["line"] != at for x in marks):
            marks.append({"line": at, "note": note, "cue": _cue(m.get("cue"))})
    lang = raw.get("lang") if raw.get("lang") in CODE_LANGS else "text"
    return {"type": kind, "lang": lang, "lines": lines, "source": n, "marks": marks}, ""


def clean_spec(raw: Any, lesson: dict, quote_ok: QuoteOk, code_ok: CodeOk, normalize: Normalize) -> tuple[dict, list[dict], int]:
    """What the author model proposed, reduced to what can be drawn and checked: (spec, blocked, proposed).
    `blocked` holds what was left out before the gate and why; `proposed` counts everything that was offered,
    so the share that held is measured against all of it."""
    raw = raw if isinstance(raw, dict) else {}
    n_slides = len(lesson.get("slides") or [])
    blocked: list[dict] = []
    proposed = 0

    def drop(part: str, text: str, reason: str) -> None:
        blocked.append({"part": part, "text": str(text)[:300], "reason": reason})

    outcomes = []
    for k, o in enumerate(raw.get("outcomes") or [] if isinstance(raw.get("outcomes"), list) else []):
        proposed += 1
        t = _text(o, OUTCOME)
        if t and len(outcomes) < 4:
            outcomes.append(t)
        else:
            drop(f"outcome {k + 1}", o, "too long, empty or one too many")

    sections = []
    raw_sections = raw.get("sections") if isinstance(raw.get("sections"), list) else []
    if raw_sections:
        proposed += len(raw_sections)
        order: list[int] = []
        for s in raw_sections:
            slides = [_int(x) for x in s.get("slides") or []] if isinstance(s, dict) and isinstance(s.get("slides"), list) else []
            sections.append({"title": _text(s.get("title"), TITLE) if isinstance(s, dict) else None, "slides": slides})
            order.extend(slides)
        if not 2 <= len(sections) <= 4 or order != list(range(n_slides)) or any(not s["slides"] for s in sections):
            drop("sections", [s.get("title") for s in raw_sections if isinstance(s, dict)],
                 "the parts must take the slides in order, every slide once, in 2 to 4 parts")
            sections = []
        for k, s in enumerate(sections):
            if s["title"] is None:
                drop(f"section {k + 1}", "", "no usable title")
                s["title"] = ""

    slides: dict[int, dict] = {}
    seen: set[int] = set()
    for entry in raw.get("slides") or [] if isinstance(raw.get("slides"), list) else []:
        if not isinstance(entry, dict):
            continue
        i = _int(entry.get("slide"))
        if not 0 <= i < n_slides or i in seen:
            for k in ("visual", "tip"):
                if entry.get(k) is not None:
                    proposed += 1
                    drop(f"slide {i + 1} {'diagram' if k == 'visual' else 'tip'}", entry.get(k), "no such slide, or a second entry for it")
            continue
        seen.add(i)
        out: dict = {"visual": None, "tip": None}
        if entry.get("visual") is not None:
            proposed += 1
            v, why = clean_visual(entry.get("visual"), quote_ok, code_ok, normalize)
            if v:
                out["visual"] = v
            else:
                drop(f"slide {i + 1} diagram", entry.get("visual"), why)
        tip = entry.get("tip")
        if tip is not None:
            proposed += 1
            t = tip if isinstance(tip, dict) else {}
            text, quote, n = _text(t.get("text"), TIP), _text(t.get("quote"), QUOTE), _int(t.get("source"))
            if text and quote and quote_ok(quote, n):
                out["tip"] = {"kind": "trap" if t.get("kind") == "trap" else "tip", "text": text,
                              "quote": quote, "source": n, "cue": _cue(t.get("cue"))}
            else:
                drop(f"slide {i + 1} tip", tip, "a tip needs a short text and a short exact quote from the page")
        if out["visual"] or out["tip"]:
            slides[i] = out

    recap = []
    for k, r in enumerate(raw.get("recap") or [] if isinstance(raw.get("recap"), list) else []):
        proposed += 1
        t = _text(r, RECAP)
        if t and len(recap) < RECAP_MAX:
            recap.append(t)
        else:
            drop(f"recap {k + 1}", r, "too long, empty or one too many")

    spec = {"format": DECK_FORMAT, "outcomes": outcomes, "sections": sections,
            "slides": {str(i): v for i, v in sorted(slides.items())}, "recap": recap}
    return spec, blocked, proposed


def _words_of(*texts: str) -> str:
    return "; ".join(t for t in texts if t)


def visual_statements(v: dict, where: str) -> list[tuple[str, str]]:
    """The visual as sentences for the gate, each with a suffix naming its part. The words are exactly what
    the slide shows, so a verdict on the sentence is a verdict on the picture."""
    kind = v["type"]
    if kind in ORDERED:
        items = v.get("steps") or v.get("layers") or v.get("events")
        said = "; ".join(f"{k + 1}. " + (f"{it['when']}: " if it.get("when") else "") + _words_of(it["label"], it["note"])
                         for k, it in enumerate(items))
        lead = {"flow": "These steps, in this order", "cycle": "A repeating cycle, in this order, then back to step 1",
                "stack": "These layers, from the top to the bottom", "timeline": "These moments, in time order"}[kind]
        return [("", f"{where}: {lead}: {said}.")]
    if kind == "hub":
        return [(f".{k}", f"{where}: about \"{v['center']}\": {_words_of(s['label'], s['note'])}.") for k, s in enumerate(v["spokes"])]
    if kind == "compare":
        return [(f".{k}", f"{where}: on \"{r['label']}\", " + "; ".join(f"{c}: {cell}" for c, cell in zip(v["columns"], r["cells"])) + ".")
                for k, r in enumerate(v["rows"])]
    if kind == "contrast":
        out = []
        for side in ("left", "right"):
            s = v[side]
            out += [(f".{side[0]}{k}", f"{where}: under \"{s['title']}\": {p}") for k, p in enumerate(s["points"])]
        return out
    if kind == "matrix":
        return [(f".{c['y']}{c['x']}", f"{where}: where {v['x']['label']} is {v['x']['high' if c['x'] else 'low']} and "
                 f"{v['y']['label']} is {v['y']['high' if c['y'] else 'low']}: {_words_of(c['label'], c['note'])}.") for c in v["cells"]]
    if kind == "keynum":
        return [("", f"{where}: {v['value']} {v['unit']}".rstrip() + f": {v['label']} (the page says: \"{v['quote']}\")")]
    return [(f".{m['line']}", f"{where}: in this snippet from the page, the line `{v['lines'][m['line']].strip()}`: {m['note']}")
            for m in v["marks"]]


def statements(spec: dict, lesson: dict) -> list[tuple[str, str]]:
    """Every element of the spec, worded for the gate: (element id, sentence)."""
    slides = lesson.get("slides") or []
    out: list[tuple[str, str]] = [(f"o{k}", f"After this lesson the learner can: {o}") for k, o in enumerate(spec["outcomes"])]
    for k, s in enumerate(spec["sections"]):
        if s["title"]:
            names = ", ".join(f"\"{slides[i]['title']}\"" for i in s["slides"] if i < len(slides))
            out.append((f"s{k}", f"A part titled \"{s['title']}\" covers the slides {names}."))
    for key, entry in spec["slides"].items():
        i = int(key)
        where = f"On the slide \"{slides[i]['title']}\"" if i < len(slides) else f"On slide {i + 1}"
        if entry.get("visual"):
            out += [(f"v{i}{suffix}", text) for suffix, text in visual_statements(entry["visual"], where + ", a diagram says")]
        if entry.get("tip"):
            t = entry["tip"]
            out.append((f"t{i}", f"{where}, an exam {'trap' if t['kind'] == 'trap' else 'tip'}: {t['text']} "
                                 f"(the page says: \"{t['quote']}\")"))
    out += [(f"r{k}", f"In the recap: {r}") for k, r in enumerate(spec["recap"])]
    return out


def shown_texts(spec: dict) -> list[tuple[str, str]]:
    """The words each element puts on the screen, by the ids of `statements`: what must not give away a
    self-check answer. The lesson's own words (slide titles) are left out; they are the lesson."""
    out: list[tuple[str, str]] = [(f"o{k}", o) for k, o in enumerate(spec["outcomes"])]
    out += [(f"s{k}", s["title"]) for k, s in enumerate(spec["sections"]) if s["title"]]
    for key, entry in spec["slides"].items():
        v, t = entry.get("visual"), entry.get("tip")
        if v:
            out += [(f"v{key}{suffix}", text) for suffix, text in _visual_words(v)]
        if t:
            out.append((f"t{key}", f"{t['text']} {t['quote']}"))
    out += [(f"r{k}", r) for k, r in enumerate(spec["recap"])]
    return out


def _visual_words(v: dict) -> list[tuple[str, str]]:
    kind = v["type"]
    if kind in ORDERED:
        items = v.get("steps") or v.get("layers") or v.get("events")
        return [("", ". ".join(_words_of(it.get("when", ""), it["label"], it["note"]) for it in items))]
    if kind == "hub":
        return [(f".{k}", _words_of(v["center"], s["label"], s["note"])) for k, s in enumerate(v["spokes"])]
    if kind == "compare":
        return [(f".{k}", _words_of(r["label"], *r["cells"])) for k, r in enumerate(v["rows"])]
    if kind == "contrast":
        return [(f".{side[0]}{k}", _words_of(v[side]["title"], p)) for side in ("left", "right") for k, p in enumerate(v[side]["points"])]
    if kind == "matrix":
        return [(f".{c['y']}{c['x']}", _words_of(c["label"], c["note"])) for c in v["cells"]]
    if kind == "keynum":
        return [("", _words_of(f"{v['value']} {v['unit']}".strip(), v["label"], v["quote"]))]
    return [(f".{m['line']}", _words_of(v["lines"][m["line"]].strip(), m["note"])) for m in v["marks"]]


def apply_verdicts(spec: dict, verdicts: dict[str, tuple[bool, str]], blocked: list[dict] | None = None) -> tuple[dict, int]:
    """Keeps what held: (spec, elements that held). A part that did not hold is left out, a diagram left with
    too few parts goes, and an ordered diagram goes whole, because its order is one claim. A missing
    verdict is a "does not hold"."""
    blocked = blocked if blocked is not None else []

    def ok(eid: str, part: str, text: str) -> bool:
        holds, reason = verdicts.get(eid, (False, "no verdict"))
        if not holds:
            blocked.append({"part": part, "text": text[:300], "reason": reason or "did not hold"})
        return holds

    held = 0
    outcomes = []
    for k, o in enumerate(spec["outcomes"]):
        if ok(f"o{k}", f"outcome {k + 1}", o):
            outcomes.append(o)
    held += len(outcomes)
    outcomes = outcomes if len(outcomes) >= 2 else []

    sections = []
    for k, s in enumerate(spec["sections"]):
        if s["title"] and ok(f"s{k}", f"section {k + 1}", s["title"]):
            held += 1
            sections.append(dict(s))
        else:
            sections.append({**s, "title": ""})   # the part stays; it is named "Part N" instead

    slides = {}
    for key, entry in spec["slides"].items():
        i, out = int(key), {"visual": None, "tip": None}
        v = entry.get("visual")
        if v:
            kept = _apply_visual(v, f"v{i}", ok, f"slide {i + 1} diagram")
            if kept:
                held += 1
                out["visual"] = kept
        t = entry.get("tip")
        if t and ok(f"t{i}", f"slide {i + 1} tip", t["text"]):
            held += 1
            out["tip"] = t
        if out["visual"] or out["tip"]:
            slides[key] = out

    recap = [r for k, r in enumerate(spec["recap"]) if ok(f"r{k}", f"recap {k + 1}", r)]
    held += len(recap)
    recap = recap if len(recap) >= 3 else []
    return {**spec, "outcomes": outcomes, "sections": sections, "slides": slides, "recap": recap}, held


def _apply_visual(v: dict, eid: str, ok: Callable[[str, str, str], bool], part: str) -> dict | None:
    kind = v["type"]
    if kind in ORDERED or kind == "keynum":
        return v if ok(eid, part, kind) else None
    if kind == "hub":
        spokes = [s for k, s in enumerate(v["spokes"]) if ok(f"{eid}.{k}", f"{part} spoke", s["label"])]
        return {**v, "spokes": spokes} if len(spokes) >= 3 else None
    if kind == "compare":
        rows = [r for k, r in enumerate(v["rows"]) if ok(f"{eid}.{k}", f"{part} row", r["label"])]
        return {**v, "rows": rows} if len(rows) >= 2 else None
    if kind == "contrast":
        sides = {}
        for side in ("left", "right"):
            pts = [p for k, p in enumerate(v[side]["points"]) if ok(f"{eid}.{side[0]}{k}", f"{part} point", p)]
            if not pts:
                return None
            sides[side] = {**v[side], "points": pts}
        return {**v, **sides}
    if kind == "matrix":
        return v if all([ok(f"{eid}.{c['y']}{c['x']}", f"{part} cell", c["label"]) for c in v["cells"]]) else None
    marks = [m for m in v["marks"] if ok(f"{eid}.{m['line']}", f"{part} note", m["note"])]
    return {**v, "marks": marks}


# ------------------------------------------------------------ timing: when each part appears

def _keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if len(w) > 2 and w not in _SMALL}


def cues(parts: list[str], lines: list[str], given: list[int | None] | None = None) -> list[int]:
    """For each part of a slide, the narration line that brings it in. The author's cue when it names a
    line: a part may then come in out of order, as a stack builds from its base. Otherwise the line near
    the part's place in the order that shares the most words with it, else its even share; these never go
    back, so such parts build in order."""
    n, p = len(lines), len(parts)
    if not p:
        return []
    if not n:
        return [0] * p
    words = [_keywords(x) for x in lines]
    out, floor = [], 0
    for k, text in enumerate(parts):
        g = given[k] if given and k < len(given) else None
        if g is not None and 0 <= g < n:
            out.append(g)
            continue
        even = min(n - 1, k * n // p)
        want, best, at = _keywords(text), 0, max(floor, even)
        for j in range(floor, min(n - 1, even + 1) + 1):
            score = len(want & words[j])
            if score > best:
                best, at = score, j
        floor = max(floor, at)
        out.append(floor)
    return out


def _visual_parts(v: dict) -> tuple[list[str], list[int | None]]:
    kind = v["type"]
    if kind in ("flow", "cycle"):
        items = v["steps"]
    elif kind == "stack":
        items = v["layers"]
    elif kind == "timeline":
        items = v["events"]
    elif kind == "hub":
        items = v["spokes"]
    elif kind == "compare":
        return [r["label"] + " " + " ".join(r["cells"]) for r in v["rows"]], [r.get("cue") for r in v["rows"]]
    elif kind == "contrast":
        pts = [(v["left"]["title"] + " " + p) for p in v["left"]["points"]] + [(v["right"]["title"] + " " + p) for p in v["right"]["points"]]
        return pts, [None] * len(pts)
    elif kind == "matrix":
        items = v["cells"]
    elif kind == "keynum":
        return [v["label"]], [None]
    else:
        return [v["lines"][m["line"]] + " " + m["note"] for m in v["marks"]], [m.get("cue") for m in v["marks"]]
    return [it["label"] + " " + it.get("note", "") for it in items], [it.get("cue") for it in items]


# ------------------------------------------------------------ the scenes the browser plays

def _slide_scene(i: int, sl: dict, entry: dict | None) -> dict:
    lines = [ln.get("text", "") for ln in sl.get("narration") or [] if isinstance(ln, dict)]
    scene = {"kind": "slide", "slide": i, "title": sl.get("title", ""), "bullets": list(sl.get("bullets") or []),
             "visual": None, "tip": None}
    scene["cues"] = {"bullets": cues(scene["bullets"], lines)}
    if entry and entry.get("visual"):
        v = entry["visual"]
        parts, given = _visual_parts(v)
        scene["visual"] = v
        scene["cues"]["visual"] = cues(parts, lines, given)
    if entry and entry.get("tip"):
        t = entry["tip"]
        scene["tip"] = {k: t[k] for k in ("kind", "text", "quote", "source")}
        last = max(0, len(lines) - 1)
        scene["cues"]["tip"] = t["cue"] if isinstance(t.get("cue"), int) and 0 <= t["cue"] <= last else last
    return scene


def compose(lesson: dict, spec: dict | None = None) -> dict:
    """The scenes of the deck, in playing order, and its parts. With no spec, or none left after the checks,
    the derived deck: the same story told from the checked lesson alone.

    Every part opens with a divider. The author's parts keep their checked titles; the rest are named in
    Dojo's own words (PART_WORDS). The traps divider names how many traps follow, never the tempting
    beliefs themselves: a wrong belief is only shown with its correction."""
    slides = lesson.get("slides") or []
    spec = spec or {}
    entries = spec.get("slides") or {}
    visual = bool(spec.get("outcomes") or spec.get("sections") or entries or spec.get("recap"))
    groups = [(s["title"], s["slides"]) for s in spec.get("sections") or []] or ([("", list(range(len(slides))))] if slides else [])
    named = [t or (PART_WORDS["ideas"] if len(groups) == 1 else f"Part {k + 1}") for k, (t, _) in enumerate(groups)]
    example = lesson.get("example") or {}
    has_example = bool(example.get("steps"))
    example_title = example.get("title") or "A worked example"
    traps = [m for m in lesson.get("misconceptions") or [] if m.get("wrong") and m.get("right")]
    checks = [q for q in lesson.get("self_check") or [] if q.get("question") and q.get("answer")][:CHECK_MAX]
    wrap = ["Recap"] + (["Check yourself"] if checks else [])

    # The parts in playing order: (role, title, what the divider lists)
    plan = [("ideas", title, [slides[i].get("title", "") for i in idx]) for title, (_, idx) in zip(named, groups)]
    if has_example:
        plan.append(("practice", PART_WORDS["practice"], [example_title]))
    if traps:
        plan.append(("traps", PART_WORDS["traps"], []))
    plan.append(("wrap", PART_WORDS["wrap"], wrap))

    scenes: list[dict] = []
    parts: list[dict] = []

    def add(scene: dict) -> None:
        scenes.append({**scene, "part": len(parts) - 1 if parts else None})

    def part(k: int) -> None:
        role, title, items = plan[k]
        parts.append({"title": title, "role": role, "first": len(scenes)})
        add({"kind": "part", "role": role, "n": k + 1, "of": len(plan), "title": title,
             "items": items[:AGENDA_SLIDES], "more": max(0, len(items) - AGENDA_SLIDES), "count": len(traps) if role == "traps" else None})

    add({"kind": "title", "title": lesson.get("title", ""), "summary": lesson.get("summary", ""), "skill": lesson.get("skill_text", "")})
    agenda = [{"title": title, "role": role, "items": items[:AGENDA_SLIDES], "more": max(0, len(items) - AGENDA_SLIDES)}
              for role, title, items in plan]
    for a in agenda:
        if a["role"] == "traps":
            a["items"] = [f"{len(traps)} common trap{'' if len(traps) == 1 else 's'}"]
    if spec.get("outcomes"):
        add({"kind": "agenda", "title": "You will be able to", "items": list(spec["outcomes"]), "outcomes": True, "parts": agenda})
    else:
        add({"kind": "agenda", "title": "In this lesson", "items": [], "outcomes": False, "parts": agenda})
    k = 0
    for _, idx in groups:
        part(k)
        k += 1
        for i in idx:
            add(_slide_scene(i, slides[i], entries.get(str(i))))
    if has_example:
        part(k)
        k += 1
        add({"kind": "process", "title": example_title, "situation": example.get("situation", ""),
             "steps": list(example.get("steps") or [])})
    if traps:
        part(k)
        k += 1
        for n, m in enumerate(traps):
            add({"kind": "trap", "n": n + 1, "of": len(traps), "wrong": m["wrong"], "right": m["right"],
                 "quote": m.get("quote", ""), "source": m.get("source")})
    part(k)
    recap = list(spec.get("recap") or []) or [t for t in (sl.get("title", "") for sl in _spread(slides, RECAP_MAX)) if t]
    add({"kind": "recap", "title": "Recap", "items": recap, "own": bool(spec.get("recap"))})
    if checks:
        add({"kind": "check", "title": "Check yourself",
             "items": [{f: q.get(f) for f in ("question", "answer", "quote", "source")} for q in checks]})
    add({"kind": "end", "title": "Your turn"})
    return {"format": DECK_FORMAT, "level": "visual" if visual else "derived", "parts": parts, "scenes": scenes}


def _spread(items: list, most: int) -> list:
    if len(items) <= most:
        return list(items)
    return [items[round(k * (len(items) - 1) / (most - 1))] for k in range(most)]


def visual_count(spec: dict | None) -> int:
    return sum(1 for e in (spec or {}).get("slides", {}).values() if e.get("visual"))
