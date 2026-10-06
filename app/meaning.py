"""Did the voice say what was approved? Word error rate between approved narration and what a
transcription heard, with the words that differ and the differences that can change meaning (EG-19).

Both sides are normalised the same way before they are compared: case, punctuation, hyphens and
slashes, contractions ("won't" is "will not"), and numbers, which a transcription writes as digits
("30", "8%") where the narration may have words, or the other way round; both become words. A word
split differently on the two sides ("GitHub" and "Git Hub", "e-mail" and "email") counts as the same.

The word error rate is (substituted + deleted + inserted) / approved words, per slide or per turn.
A difference is flagged when it touches a number, a name (a product, a service, an acronym) or a
negation, because those change what the learner is told; everything else is ordinary noise, most of
it the transcription's own. Pure functions only: nothing here reads, writes or logs anything."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
SCALES = ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (10 ** 3, "thousand"))
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
            "twelve": "twelfth"}
NUMBER_WORDS = (set(ONES) | {t for t in TENS if t} | {s for _, s in SCALES}
                | {"hundred", "point", "percent", "half", "quarter", "dozen", "minus"})  # and every ordinal, below
NEGATIONS = {"not", "no", "never", "none", "nothing", "neither", "nor", "without", "nobody", "nowhere", "cannot"}
# Names a transcription may write in lower case. Anything written with a capital inside the word
# (GitHub, IDE, DevOps) or capitalised in mid-sentence counts as a name without being listed here.
NAMES = {"github", "copilot", "azure", "microsoft", "entra", "fabric", "sql", "tsql", "vscode", "codespaces",
         "dependabot", "openai", "gpt", "windows", "linux", "powershell", "bicep", "terraform", "python", "javascript",
         "typescript", "json", "yaml", "markdown", "git", "cosmos", "synapse", "purview", "foundry", "teams", "outlook",
         "defender", "kubernetes", "docker", "ide", "api", "cli", "sdk", "oauth", "rbac", "mcp", "llm", "ai"}
ABBREVIATIONS = {"e.g.": "for example", "e.g": "for example", "i.e.": "that is", "i.e": "that is", "etc.": "et cetera",
                 "etc": "et cetera", "vs.": "versus", "approx.": "approximately"}
CONTRACTIONS = {"won't": "will not", "can't": "can not", "cannot": "can not", "shan't": "shall not", "ain't": "is not",
                "let's": "let us"}
_SUFFIXES = (("n't", " not"), ("'re", " are"), ("'ve", " have"), ("'ll", " will"), ("'m", " am"), ("'d", " would"))
_SYMBOLS = (("&", " and "), ("+", " plus "), ("@", " at "), ("=", " equals "), ("%", " percent "), ("°", " degrees "))
_CURRENCY = {"$": "dollars", "€": "euros", "£": "pounds"}
_TRANSLATE = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u2032": "'", "\u201c": '"', "\u201d": '"',
                            "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": " ", "\u2015": " ",
                            "\u00a0": " ", "\u2026": " "})
_EDGE = "\"'`([{<>}])*_~,;:!?\u00a1\u00bf"
_ENDS_SENTENCE = re.compile(r"[.!?:][\"')\]]*$")
_CHUNK = re.compile(r"\d+(?:[.,:]\d+)*|[^\W\d_]+")
MERGES = ((1, 2), (2, 1), (1, 3), (3, 1), (2, 3), (3, 2))
MAX_DIFFS = 12      # differences kept per slide or turn
MAX_SHOWN = 400     # written words kept per side, for showing the slide
FLAG_ORDER = ("number", "name", "negation")


@dataclass(frozen=True)
class Word:
    norm: str        # what is compared
    src: int         # the written word it came from
    number: bool
    name: bool


def int_words(n: int) -> list[str]:
    if n < 0:
        return ["minus"] + int_words(-n)
    if n < 20:
        return [ONES[n]]
    if n < 100:
        tens, ones = divmod(n, 10)
        return [TENS[tens]] + ([ONES[ones]] if ones else [])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return [ONES[hundreds], "hundred"] + (int_words(rest) if rest else [])
    for size, name in SCALES:
        if n >= size:
            high, rest = divmod(n, size)
            return int_words(high) + [name] + (int_words(rest) if rest else [])
    return [ONES[int(d)] for d in str(n)]


def _digits(s: str) -> list[str]:
    return [ONES[int(d)] for d in s if d.isdigit()]


def number_words(chunk: str) -> list[str]:
    """Digits as they are spoken: 30 is "thirty", 1,400 is "one thousand four hundred", 3.5 is
    "three point five", 1.2.3 is "one point two point three", 10:30 is "ten thirty"."""
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", chunk):
        chunk = chunk.replace(",", "")
    if "," in chunk:
        return [w for part in chunk.split(",") for w in number_words(part)]
    if ":" in chunk:
        return [w for part in chunk.split(":") if part.strip("0") for w in number_words(part)]
    if "." in chunk:
        whole, *rest = chunk.split(".")
        out = number_words(whole)
        for part in rest:
            out += ["point"] + (_digits(part) if len(rest) == 1 else number_words(part))
        return out
    if len(chunk) > 1 and chunk.startswith("0") or len(chunk) > 15:
        return _digits(chunk)
    return int_words(int(chunk))


def _ordinal(words: list[str]) -> list[str]:
    last = words[-1]
    if last in _ORDINAL:
        last = _ORDINAL[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return words[:-1] + [last]


# An ordinal is a number word whether it was written "13th" or "thirteenth".
NUMBER_WORDS |= {_ordinal([w])[0] for w in (*ONES, *(t for t in TENS if t), "hundred", *(s for _, s in SCALES))}


def _spoken(core: str) -> list[tuple[str, bool]]:
    """One written word, lower-cased and without its edge punctuation, as spoken words; the flag says
    which of them came from digits."""
    if core in ABBREVIATIONS:
        return [(w, False) for w in ABBREVIATIONS[core].split()]
    core = core.rstrip(".")
    if not core:
        return []
    if core in CONTRACTIONS:
        return [(w, False) for w in CONTRACTIONS[core].split()]
    for suffix, spoken in _SUFFIXES:
        if core.endswith(suffix) and len(core) > len(suffix):
            core = core[: -len(suffix)] + spoken
            break
    money = re.fullmatch(r"([$€£])(\d[\d,]*(?:\.\d+)?)([kmb]?)", core)
    if money:
        scale = {"k": ["thousand"], "m": ["million"], "b": ["billion"]}.get(money.group(3), [])
        return [(w, True) for w in number_words(money.group(2)) + scale] + [(_CURRENCY[money.group(1)], False)]
    core = re.sub(r"(?<=[^\W\d_])#", " sharp ", core).replace("#", " number ")
    for symbol, spoken in _SYMBOLS:
        core = core.replace(symbol, spoken)
    if re.fullmatch(r"(?:[^\W\d_]\.)+[^\W\d_]", core):  # u.s, a.m: letters read one by one
        core = core.replace(".", "")
    core = re.sub(r"^\.(?=[^\W\d_])", "dot ", core)                         # .NET, .gitignore
    core = re.sub(r"(?<=[^\W\d_])\.(?=\w)|(?<=\d)\.(?=[^\W\d_])", " dot ", core)  # node.js, but not 3.5
    core = re.sub(r"[-/\\|_]", " ", core).replace("'", "")
    out: list[tuple[str, bool]] = []
    for piece in core.split():
        ordinal = re.fullmatch(r"(\d+)(st|nd|rd|th)", piece)
        if ordinal:
            out += [(w, True) for w in _ordinal(int_words(int(ordinal.group(1))))]
            continue
        for chunk in _CHUNK.findall(piece):
            if chunk[0].isdigit():
                out += [(w, True) for w in number_words(chunk)]
            else:
                out.append((chunk, chunk in NUMBER_WORDS))
    return out


def _is_name(core: str, sentence_start: bool, spoken: list[tuple[str, bool]]) -> bool:
    letters = [c for c in core if c.isalpha()]
    if not letters:
        return False
    if core.lower() == "i" or core.lower().startswith("i'"):
        return False
    if any(c.isupper() for c in letters[1:]):
        return True
    if letters[0].isupper() and not sentence_start:
        return True
    return any(w in NAMES for w, _ in spoken)


def written(text: str) -> list[str]:
    """The words as written, for showing: split on white space and on long dashes."""
    return unicodedata.normalize("NFKC", str(text or "")).translate(_TRANSLATE).split()


def words(text: str) -> tuple[list[str], list[Word]]:
    """The written words, and the normalised words that are compared, each pointing back at the
    written word it came from."""
    raw = written(text)
    out: list[Word] = []
    start = True
    for k, token in enumerate(raw):
        core = token.strip(_EDGE)
        spoken = _spoken(core.lower())
        name = _is_name(core.rstrip("."), start, spoken)
        out += [Word(w, k, number or w in NUMBER_WORDS, name) for w, number in spoken]
        start = bool(_ENDS_SENTENCE.search(token))
    return raw, out


def normalise(text: str) -> list[str]:
    return [w.norm for w in words(text)[1]]


def align(ref: list[str], hyp: list[str]) -> list[tuple[str, int, int, int, int]]:
    """The cheapest edit from the approved words to the heard ones, as (kind, i0, i1, j0, j1) steps over
    ref[i0:i1] and hyp[j0:j1]. Kinds: equal, merge (the same letters split differently; free), sub,
    del (approved but not heard), ins (heard but not approved)."""
    n, m = len(ref), len(hyp)
    big = n + m + 1
    cost = [[big] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0
    for i in range(n + 1):
        row = cost[i]
        for j in range(m + 1):
            c = row[j]
            if c >= big:
                continue
            if i < n and j < m:
                same = ref[i] == hyp[j]
                step = c + (0 if same else 1)
                if step < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1] = step
                    back[i + 1][j + 1] = ("equal" if same else "sub", i, j)
                a, b = ref[i], hyp[j]
                if not same and (a.startswith(b) or b.startswith(a)):
                    for da, db in MERGES:
                        if i + da <= n and j + db <= m and c < cost[i + da][j + db] \
                                and "".join(ref[i:i + da]) == "".join(hyp[j:j + db]):
                            cost[i + da][j + db] = c
                            back[i + da][j + db] = ("merge", i, j)
            if i < n and c + 1 < cost[i + 1][j]:
                cost[i + 1][j] = c + 1
                back[i + 1][j] = ("del", i, j)
            if j < m and c + 1 < row[j + 1]:
                row[j + 1] = c + 1
                back[i][j + 1] = ("ins", i, j)
    ops = []
    i, j = n, m
    while i or j:
        kind, pi, pj = back[i][j]
        ops.append((kind, pi, i, pj, j))
        i, j = pi, pj
    ops.reverse()
    return ops


def _shown(raw: list[str], marks: dict[int, int]) -> list[list]:
    return [[w, marks.get(k, 0)] for k, w in enumerate(raw[:MAX_SHOWN])]


def _whole(toks: list[Word], lo: int, hi: int) -> tuple[int, int]:
    """Stretch a range of compared words to whole written words."""
    if lo < hi:
        while lo > 0 and toks[lo - 1].src == toks[lo].src:
            lo -= 1
        while hi < len(toks) and toks[hi].src == toks[hi - 1].src:
            hi += 1
    return lo, hi


def _widen(same: list[tuple], ref: list[Word], hyp: list[Word], i0: int, i1: int, j0: int, j1: int) -> tuple[int, int, int, int]:
    """What to show for one difference: whole written words on both sides, so a difference inside
    "won't" (will not) shows "won't" against the "will" that was heard, not against nothing. Counting
    is not changed by this."""
    for _ in range(2):
        a0, a1 = _whole(ref, i0, i1)
        b0, b1 = _whole(hyp, j0, j1)
        for _, p0, p1, q0, q1 in same:
            if (p0 < a1 and p1 > a0) or (q0 < b1 and q1 > b0):
                a0, a1, b0, b1 = min(a0, p0), max(a1, p1), min(b0, q0), max(b1, q1)
        i0, i1, j0, j1 = a0, a1, b0, b1
    return i0, i1, j0, j1


def _say(raw: list[str], toks: list[Word]) -> str:
    seen: list[int] = []
    for t in toks:
        if t.src not in seen:
            seen.append(t.src)
    return " ".join(raw[k].strip(_EDGE).rstrip(".") or raw[k] for k in seen)


def compare(approved: str, heard: str) -> dict:
    """How far what was heard is from what was approved: word error rate, the differences (flagged
    when they touch a number, a name or a negation) and both texts with the differing words marked
    (1: differs, 2: differs in a way that can change meaning)."""
    raw_a, ref = words(approved)
    raw_h, hyp = words(heard)
    ops = align([w.norm for w in ref], [w.norm for w in hyp])
    counts = {"sub": 0, "del": 0, "ins": 0}
    groups: list[list[int]] = []
    for kind, i0, i1, j0, j1 in ops:
        if kind in counts:
            counts[kind] += 1
            if groups and groups[-1][1] == i0 and groups[-1][3] == j0:
                groups[-1][1], groups[-1][3] = i1, j1
            else:
                groups.append([i0, i1, j0, j1])
    diffs, flags = [], set()
    marks_a: dict[int, int] = {}
    marks_h: dict[int, int] = {}
    same = [op for op in ops if op[0] in ("equal", "merge")]
    for i0, i1, j0, j1 in groups:
        a, b = ref[i0:i1], hyp[j0:j1]
        both = a + b
        found = set()
        if any(w.number for w in both):
            found.add("number")
        if any(w.name for w in both):
            found.add("name")
        if any(w.norm in NEGATIONS for w in both):
            found.add("negation")
        flags |= found
        mark = 2 if found else 1
        w0, w1, v0, v1 = _widen(same, ref, hyp, i0, i1, j0, j1)
        for w in ref[w0:w1]:
            marks_a[w.src] = max(marks_a.get(w.src, 0), mark)
        for w in hyp[v0:v1]:
            marks_h[w.src] = max(marks_h.get(w.src, 0), mark)
        diffs.append({"approved": _say(raw_a, ref[w0:w1]), "heard": _say(raw_h, hyp[v0:v1]),
                      "flags": [f for f in FLAG_ORDER if f in found]})
    diffs.sort(key=lambda d: not d["flags"])  # meaning first, noise after, each in reading order
    errors = sum(counts.values())
    total = len(ref)
    wer = errors / total if total else (1.0 if hyp else 0.0)
    return {"words": total, "errors": errors, "substituted": counts["sub"], "deleted": counts["del"],
            "inserted": counts["ins"], "wer": round(wer, 4), "flags": [f for f in FLAG_ORDER if f in flags],
            "diffs": diffs[:MAX_DIFFS], "approved": _shown(raw_a, marks_a), "heard": _shown(raw_h, marks_h)}


def by_turn(heard_words: list[dict], spans: list[dict], tolerance: float = 0.3) -> tuple[list[str], int]:
    """Split what was heard in one presenter's video into that presenter's turns, by where the middle
    of each word falls within the stored start and end of a turn. Words that fall outside every turn
    are never played in Watch; they are counted, not compared."""
    groups: list[list[str]] = [[] for _ in spans]
    outside = 0
    for w in heard_words:
        mid = (float(w["start"]) + float(w["end"])) / 2
        for k, span in enumerate(spans):
            if float(span["start"]) - tolerance <= mid <= float(span["end"]) + tolerance:
                groups[k].append(str(w["text"]))
                break
        else:
            outside += 1
    return [" ".join(g) for g in groups], outside


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 4)
