"""Model calls. All AI runs in Azure AI Foundry, as three model families with separate duties:

- author (OpenAI GPT-5.5) teaches: lessons, items, hints, rehearsal questions, answers to questions.
- gate (xAI Grok) is an independent checker: it decides whether each thing shown to the learner
  holds against the official sources.
- grader (DeepSeek) judges finished answers against a rubric and must quote the learner's words.

StubAI is a deterministic stand-in used by the tests. It quotes the source text it is given, so
the real quote checks still run.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import threading
import time
from typing import Any, Callable

import httpx

from .budget import chat_input_bound, max_out
from .core import Tokens

log = logging.getLogger("dojo.ai")
ROLES = ("author", "gate", "grader")


class AIError(Exception):
    """A model call that failed: no endpoint, a refusal, an error, or an answer Dojo cannot use."""
    pass


def _content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


class Foundry:
    """The three model roles (author, gate, grader) on Azure AI Foundry, called through its
    OpenAI-compatible endpoint with Entra tokens. In team mode each call is metered by the Budget."""
    def __init__(self, settings: Any, tokens: Tokens, http: httpx.Client | None = None):
        self.settings = settings
        self.tokens = tokens
        self.http = http or httpx.Client(timeout=httpx.Timeout(480.0, connect=15.0), headers={"User-Agent": "dojo/1"})
        self._routes: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()
        self.meter: Any = None   # the Budget, in team mode (app/budget.py)
        self._limit_key: dict[str, str] = {}   # a model that wants max_tokens instead of max_completion_tokens

    def _candidates(self, role: str) -> list[tuple[str, str]]:
        with self._lock:
            if role in self._routes:
                return [self._routes[role]]
        base = self.settings.ai_endpoint
        host = base.split("/openai/")[0]
        return [
            (f"{base}/chat/completions", "https://cognitiveservices.azure.com/.default"),
            (f"{base}/chat/completions", "https://ai.azure.com/.default"),
            (f"{host}/models/chat/completions?api-version=2024-05-01-preview", "https://cognitiveservices.azure.com/.default"),
        ]

    def route(self, role: str) -> str | None:
        with self._lock:
            r = self._routes.get(role)
        return f"{r[0]} ({r[1]})" if r else None

    def _post(self, url: str, scope: str, body: dict) -> httpx.Response:
        return self.http.post(url, json=body, headers={"Authorization": f"Bearer {self.tokens.get(scope)}"})

    def chat(self, role: str, system: str, user: str, json_mode: bool = True) -> str:
        if role not in ROLES:
            raise ValueError(f"unknown role {role}")
        if not self.settings.ai_endpoint:
            raise AIError("No model endpoint is configured.")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        meter = self.meter   # team mode: every POST is reserved before it is sent, and settled after
        task = task_of(system)
        last = "no route was tried"
        for attempt in range(4):
            transient = False
            for url, scope in self._candidates(role):
                body: dict[str, Any] = {"model": role, "messages": messages}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                if meter is not None:
                    body[self._limit_key.get(role, "max_completion_tokens")] = max_out(role, task)
                ticket = (meter.open_call(f"{role}/{task}", meter.chat_cost(role, chat_input_bound(body), max_out(role, task)))
                          if meter is not None else None)
                cost: float | None = None   # what the call cost; None: not known, so its bound
                try:
                    try:
                        r = self._post(url, scope, body)
                        if r.status_code == 400 and "response_format" in body and "response_format" in r.text:
                            body.pop("response_format")
                            r = self._post(url, scope, body)
                        if r.status_code == 400 and meter is not None and "max_completion_tokens" in body \
                                and "max_completion_tokens" in r.text:
                            body["max_tokens"] = body.pop("max_completion_tokens")
                            r = self._post(url, scope, body)
                            if r.status_code != 400:
                                with self._lock:
                                    self._limit_key[role] = "max_tokens"
                    except httpx.ConnectError as e:
                        cost = 0.0   # nothing reached the service
                        last, transient = f"network: {type(e).__name__}", True
                        continue
                    except httpx.HTTPError as e:
                        last, transient = f"network: {type(e).__name__}", True
                        continue
                    except Exception as e:  # noqa: BLE001 - token failures: try the next scope
                        cost = 0.0
                        last = f"token: {type(e).__name__}: {str(e)[:200]}"
                        continue
                    if r.status_code != 200:
                        cost = 0.0
                    if r.status_code == 200:
                        try:
                            payload = r.json()
                        except ValueError:
                            payload = None
                        if meter is not None:
                            cost = _usage_cost(meter, role, payload)
                        text = _content(payload)
                        if text.strip():
                            with self._lock:
                                self._routes[role] = (url, scope)
                            return text
                        last, transient = "the model returned an empty answer", True
                        break
                    last = f"HTTP {r.status_code}: {r.text[:300]}"
                    if r.status_code in (408, 429, 500, 502, 503, 504):
                        transient = True
                        break
                    if r.status_code in (401, 403, 404):
                        continue
                    raise AIError(f"{role} model refused the request: {last}")
                finally:
                    if ticket is not None:
                        meter.settle(ticket, ticket.bound if cost is None else cost)
            if not transient:
                break
            time.sleep(min(30, 4 * 2 ** attempt))
        raise AIError(f"{role} model: {last}")


def task_of(system: str) -> str:
    found = re.search(r"TASK: (\w+)", system or "")
    return found.group(1) if found else "chat"


def _usage_cost(meter: Any, role: str, payload: Any) -> float | None:
    """The cost of a call from the usage the service reported; None (its bound) when there is none."""
    try:
        usage = payload["usage"]
        tokens_in, tokens_out = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    if tokens_in < 0 or tokens_out < 0:
        return None
    # Some services (grok) count reasoning outside completion_tokens, and total_tokens holds it. Reasoning is
    # billed as output, so the output is whatever the total holds beyond the prompt, never less.
    try:
        total = int(usage["total_tokens"])
    except (KeyError, TypeError, ValueError):
        total = 0
    return meter.chat_cost(role, tokens_in, max(tokens_out, total - tokens_in))


def parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        value = json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("the answer contained no JSON object")
        value = json.loads(t[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("the answer was not a JSON object")
    return value


def ask_json(ai: Any, role: str, system: str, user: str, check: Callable[[dict], None] | None = None, tries: int = 2) -> dict:
    """Ask for one JSON object; if it is malformed or fails `check`, ask once more with the reason."""
    prompt, problem = user, ""
    for _ in range(tries):
        text = ai.chat(role, system, prompt)
        try:
            data = parse_json(text)
            if check:
                check(data)
            return data
        except (ValueError, KeyError, TypeError) as e:
            problem = str(e) or type(e).__name__
            prompt = (f"{user}\n\nYour previous answer could not be used ({problem}). "
                      "Answer again with only the JSON object in the required shape.")
    raise AIError(f"The {role} model's answer could not be used: {problem}")


# ---------------------------------------------------------------- deterministic stand-in for tests

_SOURCE = re.compile(r'<source n="(\d+)"[^>]*>\n?(.*?)\n?</source>', re.S)
_SKILL = re.compile(r'<skill id="([^"]+)"[^>]*>(.*?)</skill>', re.S)
_ANSWER = re.compile(r"<answer>\n?(.*?)\n?</answer>", re.S)
_ELEMENT = re.compile(r"^\[([^\]\n]+)\]", re.M)
_RUBRIC = re.compile(r'^- \[(r\d+)\]', re.M)
_CANDIDATE = re.compile(r'<candidate n="(\d+)" found-for="([^"]*)"')
_PART = re.compile(r"^(P\d+) (.+?) \(about (\d+) words\):", re.M)
_ONLY = re.compile(r"^Write ONLY these parts again: ([P\d, ]+)\.", re.M)


def _quotes(text: str) -> list[tuple[int, str]]:
    out = []
    for n, body in _SOURCE.findall(text):
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
            words = sentence.split()
            if 8 <= len(words) <= 40 and not sentence.lstrip().startswith(("#", "-", "*", ">", "|")):
                out.append((int(n), sentence.strip()))
        if not out:
            words = body.split()
            if len(words) >= 8:
                out.append((int(n), " ".join(words[:12])))
    return out or [(1, "no source text")]


class StubAI:
    """A deterministic stand-in for Foundry, used by the tests and with DOJO_LOCAL=1. It answers each
    task in the shape the real models must, quoting the source text it is given."""
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._batches = itertools.count(1)
        self.meter: Any = None   # the Budget, in team mode: the stub is reserved and settled like the service
        # (role, task, system, user, answer) -> (prompt_tokens, completion_tokens): what the stub reports
        self.usage: Callable[..., tuple[int, int]] = lambda role, task, system, user, answer: (
            len((system + user).encode("utf-8")) // 4 + 8, len(answer.encode("utf-8")) // 4 + 1)

    def route(self, role: str) -> str:
        return "stub"

    def chat(self, role: str, system: str, user: str, json_mode: bool = True) -> str:
        task = re.search(r"TASK: (\w+)", system).group(1)
        meter = self.meter
        if meter is None:
            return self._reply(role, task, user)
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"} if json_mode else None}
        ticket = meter.open_call(f"{role}/{task}", meter.chat_cost(role, chat_input_bound(body), max_out(role, task)))
        cost = 0.0
        try:
            answer = self._reply(role, task, user)
            tokens_in, tokens_out = self.usage(role, task, system, user, answer)
            cost = meter.chat_cost(role, tokens_in, tokens_out)
            return answer
        finally:
            meter.settle(ticket, cost)

    def _reply(self, role: str, task: str, user: str) -> str:
        self.calls.append((role, task))
        if task == "ping":
            return "ok"
        return json.dumps(getattr(self, "_" + task)(user))

    def _lesson(self, user: str) -> dict:
        q = _quotes(user)
        pick = lambda i: q[i % len(q)]  # noqa: E731
        return {
            "title": "Stub lesson",
            "summary": "A short lesson built from the sources.",
            "sections": [
                {"heading": "Key idea", "points": [
                    {"text": "First point.", "quote": pick(0)[1], "source": pick(0)[0]},
                    {"text": "Second point.", "quote": pick(1)[1], "source": pick(1)[0]}]},
                {"heading": "Details", "points": [
                    {"text": "Third point.", "quote": pick(2)[1], "source": pick(2)[0]},
                    {"text": "Invented point.", "quote": "this sentence is not in any source at all", "source": 1}]},
            ],
            "misconceptions": [{"wrong": "A wrong belief.", "right": "The correction.", "quote": pick(0)[1], "source": pick(0)[0]}],
            "example": {"title": "Example", "situation": "A team adopts the feature.", "steps": ["Step one.", "Step two."]},
            "self_check": [{"question": "What is the key idea?", "answer": "The first point.", "quote": pick(1)[1], "source": pick(1)[0]}],
            "slides": [
                {"title": "Slide one", "bullets": ["Bullet A", "Bullet B"],
                 "narration": [{"voice": "guide", "text": "Here is the idea."}, {"voice": "coach", "text": "Why does it matter?"}]},
                {"title": "Slide two", "bullets": ["Bullet C"], "narration": [{"voice": "guide", "text": "That is all."}]},
            ],
        }

    def _gate_elements(self, user: str) -> dict:
        return {"verdicts": [{"id": eid, "holds": True, "reason": ""} for eid in _ELEMENT.findall(user)]}

    COACH = ["What does that mean in practice?", "Why would that matter on the exam?", "How could a question test that?",
             "Can you say that another way?", "What should I hold on to here?"]

    def _deep_narration(self, user: str) -> dict:
        """Deeper Listen: the coach and the guide take turns over every part of the outline, about as long
        as it asks, with the guide saying the source sentences. A rewrite writes only the parts it names."""
        q = _quotes(user)
        only = _ONLY.search(user)
        wanted = set(re.findall(r"P\d+", only.group(1))) if only else None
        segments, k = [], 0
        for pid, title, words in _PART.findall(user):
            if wanted is not None and pid not in wanted:
                continue
            lines = [{"voice": "coach", "text": f"Let's take {title.lower()} next. Where do we start?"}]
            said = len(lines[0]["text"].split())
            while said < int(words) and len(lines) < 11:
                n, sentence = q[k % len(q)]
                k += 1
                lines.append({"voice": "guide", "text": f"The source puts it like this. {sentence}"})
                lines.append({"voice": "coach", "text": self.COACH[k % len(self.COACH)]})
                said += len(lines[-1]["text"].split()) + len(lines[-2]["text"].split())
            if title == "Summary":
                lines.append({"voice": "guide", "text": "That covers it. Now try the self-check in Dojo."})
            segments.append({"part": pid, "lines": lines})
        return {"segments": segments}

    STEMS = [
        "A retail bank wants its developers to stop sharing customer spreadsheets through chat. What would you do, and why?",
        "An airline maintenance crew moves every nightly report into one shared folder. What would you change first, and why?",
        "A hospital pharmacy asks whether tablets can replace paper checklists during audits. How would you respond, and why?",
        "A film studio archives raw footage on removable drives kept in a locked cabinet. What risk stands out, and why?",
        "A farming cooperative tracks tractor repairs by text message between mechanics. What would you set up instead, and why?",
    ]

    def _item(self, user: str) -> dict:
        q = _quotes(user)
        stem = self.STEMS[sum(1 for _, task in self.calls if task == "item") % len(self.STEMS)]
        return {
            "kind": "explanation" if "Item type: explanation" in user else "scenario",
            "stem": stem,
            "changed_condition": "The team works offline.",
            "rubric": [
                {"id": "r1", "point": "Names the first idea.", "quote": q[0][1], "source": q[0][0]},
                {"id": "r2", "point": "Names the second idea.", "quote": q[-1][1], "source": q[-1][0]},
            ],
            "hints": ["Think about the first idea.", "Consider the second idea."],
            "model_answer": "Mention both ideas.",
        }

    def _gate_item(self, user: str) -> dict:
        return {"holds": True, "issues": [], "rubric": [{"id": r, "holds": True, "reason": ""} for r in _RUBRIC.findall(user)],
                "changed_ok": True, "hints": [{"index": int(i), "holds": True, "reason": ""} for i in re.findall(r"^\[h(\d+)\]", user, re.M)]}

    def _gate_judgement(self, user: str) -> dict:
        return self._gate_elements(user)

    def _grade(self, user: str) -> dict:
        m = _ANSWER.search(user)
        answer = m.group(1) if m else ""
        quote = " ".join(answer.split()[:5])
        points = []
        for rid in _RUBRIC.findall(user):
            met = rid in answer or "everything" in answer
            points.append({"id": rid, "met": met, "learner_quote": quote if met else "", "why": "stub"})
        return {"points": points, "feedback": "Stub feedback.", "misconception": ""}

    def _mcq(self, user: str) -> dict:
        items = []
        batch = next(self._batches)  # a real author writes a different question every time
        for sid, body in _SKILL.findall(user):
            n, quote = _quotes(body)[0]
            items.append({"skill": sid, "stem": f"Question {batch}: which statement about {sid} is correct?",
                          "options": ["Correct option", "Wrong option one", "Wrong option two", "Wrong option three"],
                          "answer": 0, "rationale": "The source says so.", "quote": quote, "source": n})
        return {"items": items}

    def _gate_mcq(self, user: str) -> dict:
        count = len(re.findall(r"^\[q\d+\]", user, re.M))
        return {"verdicts": [{"index": i, "holds": True, "reason": ""} for i in range(count)]}

    # ---- the exam item types of ADR 0012: every part of the key quotes a real source sentence

    def _skills(self, user: str):
        batch = next(self._batches)
        for sid, body in _SKILL.findall(user):
            q = _quotes(body)
            yield batch, sid, (lambda i, q=q: {"quote": q[i % len(q)][1], "source": q[i % len(q)][0]})

    def _item_multi(self, user: str) -> dict:
        return {"items": [{"skill": sid, "stem": f"Item {b}: which two statements about {sid} are true?",
                           "options": ["First true statement", "Second true statement", "A false statement",
                                       "Another false statement", "A third false statement"],
                           "correct": [{"option": 0, **pick(0)}, {"option": 1, **pick(1)}],
                           "rationale": "The sources say both."} for b, sid, pick in self._skills(user)]}

    def _item_sequence(self, user: str) -> dict:
        return {"items": [{"skill": sid, "stem": f"Item {b}: in which order do you set up {sid}?",
                           "steps": [{"text": f"Step {name}", **pick(k)} for k, name in enumerate(("one", "two", "three"))],
                           "distractors": ["A step that does not belong"], "rationale": "The source gives this order."}
                          for b, sid, pick in self._skills(user)]}

    def _item_match(self, user: str) -> dict:
        return {"items": [{"skill": sid, "stem": f"Item {b}: match each need in {sid} to what meets it.",
                           "pairs": [{"target": f"Need {k + 1}", "value": f"Feature {'ABC'[k]}", **pick(k)} for k in range(3)],
                           "extra_values": ["Feature D"], "rationale": "Each source sentence names its feature."}
                          for b, sid, pick in self._skills(user)]}

    def _item_hotarea(self, user: str) -> dict:
        return {"items": [{"skill": sid, "stem": f"Item {b}: complete the setting for {sid}.",
                           "text": "setting:\n  mode: [[1]]\n  scope: [[2]]",
                           "blanks": [{"options": ["enabled", "disabled", "audit"], "answer": 0, **pick(0)},
                                      {"options": ["user", "organization", "repository"], "answer": 1, **pick(1)}],
                           "rationale": "The sources name both values."} for b, sid, pick in self._skills(user)]}

    def _item_yesno(self, user: str) -> dict:
        return {"items": [{"skill": sid, "stem": f"Series {b}: a team needs {sid} in place. Does each solution meet the goal?",
                           "statements": [{"text": f"Solution: proposal {k + 1}.", "answer": k != 1, **pick(k)} for k in range(3)],
                           "rationale": "The sources decide each one."} for b, sid, pick in self._skills(user)]}

    def _item_case(self, user: str) -> dict:
        out = []
        for b, sid, pick in self._skills(user):
            single = lambda k: {"kind": "single", "stem": f"Case question {k + 1}: what should Contoso do?",  # noqa: E731
                                "options": ["The right step", "A wrong step", "Another wrong step", "A third wrong step"],
                                "answer": 0, **pick(k), "rationale": "The source says so."}
            out.append({"skill": sid, "title": f"Contoso, Ltd. {b}", "stem": "Contoso is a fictional company. Read the exhibits.",
                        "exhibits": [{"title": "Overview", "text": "Contoso builds software for retail stores."},
                                     {"title": "Requirements", "text": "Contoso must keep its code private."}],
                        "questions": [single(0), single(1),
                                      {"kind": "multi", "stem": "Case question 3: which two steps meet the requirement?",
                                       "options": ["Right one", "Right two", "Wrong one", "Wrong two", "Wrong three"],
                                       "correct": [{"option": 0, **pick(2)}, {"option": 1, **pick(3)}], "rationale": "Both."},
                                      single(3)]})
        return {"items": out}

    def _gate_items(self, user: str) -> dict:
        count = len(re.findall(r"^\[q\d+\]", user, re.M))
        return {"verdicts": [{"index": i, "holds": True, "reason": ""} for i in range(count)]}

    def _answer(self, user: str) -> dict:
        n, quote = _quotes(user)[0]
        return {"points": [{"text": "The sources say this.", "quote": quote, "source": n}], "not_covered": ""}

    def _pick_sources(self, user: str) -> dict:
        """Adding an exam: the first two candidate pages the search found for each skill."""
        skills = [int(k) for k in re.findall(r"^\[(\d+)\] ", user.split("<candidates>")[0], re.M)]
        picks: dict[int, list[int]] = {k: [] for k in skills}
        for n, found_for in _CANDIDATE.findall(user):
            for k in (int(x) for x in re.findall(r"\d+", found_for)):
                if k in picks and len(picks[k]) < 2:
                    picks[k].append(int(n))
        return {"picks": [{"skill": k, "pages": v} for k, v in picks.items()]}

    def _verify_sources(self, user: str) -> dict:
        """Adding an exam: a source teaches the skill when it has a sentence to quote; the quote is its first."""
        first: dict[int, str] = {}
        for n, sentence in _quotes(user):
            first.setdefault(n, sentence)
        return {"sources": [{"n": int(n), "teaches": int(n) in first, "quote": first.get(int(n), ""), "reason": "stub"}
                            for n in re.findall(r'<source n="(\d+)"', user)]}
