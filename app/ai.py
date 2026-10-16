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
# Web search (Foundry's Responses API with the web_search tool, ADR 0018). The tool puts what it found into the
# prompt, so a call reads about 10,000 tokens per search; Grounding with Bing is billed per search on top.
WEB_SEARCH_FEE = 0.04      # USD, a ceiling for one search
WEB_PRE_INPUT = 60000      # tokens reserved for one call before it is made
WEB_MAX_OUT = 2500
WEB_TRIES = 5
WEB_WAIT_S = 60            # the longest wait between two tries
WEB_INPUT = ("Find official documentation pages for this certification exam skill. Run exactly {n} web search{s}, then "
             "reply with one short line. Skill and context: {query}")


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
        self._web = threading.Lock()           # one web search at a time: the author deployment's token rate limit is shared

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

    def web_search(self, query: str, domains: tuple[str, ...] = (), max_searches: int = 1) -> dict:
        """Foundry's web search tool through the Responses API, without an agent (ADR 0018, R4).

        Returns {"urls": [...], "queries": [...]}: the addresses the tool read (its source list first, then the
        pages the model cited) and the searches it ran. The model's own words are dropped: only addresses leave
        this method, and Dojo reads and judges every page itself (EG-14). `query` holds public strings only (an
        exam, a skill, product names). One search at a time, because the author deployment's token rate limit is
        shared; a 429 is waited out. In team mode the call is reserved before it is sent and settled after."""
        if not self.settings.ai_endpoint:
            raise AIError("No model endpoint is configured.")
        role = "author"
        tool: dict[str, Any] = {"type": "web_search"}
        if domains:
            tool["filters"] = {"allowed_domains": list(domains)}
        body = {"model": role, "tools": [tool], "tool_choice": "required", "include": ["web_search_call.action.sources"],
                "max_tool_calls": max_searches, "max_output_tokens": WEB_MAX_OUT,
                "input": WEB_INPUT.format(n=max_searches, s="" if max_searches == 1 else "es", query=query[:600])}
        url = f"{self.settings.ai_endpoint}/responses"
        meter = self.meter
        last = "no route was tried"
        with self._web:
            for attempt in range(WEB_TRIES):
                wait = min(WEB_WAIT_S, 5 * 2 ** attempt)
                for scope in self._web_scopes(role):
                    ticket = (meter.open_call(f"{role}/web_search", meter.chat_cost(role, WEB_PRE_INPUT, WEB_MAX_OUT)
                                              + WEB_SEARCH_FEE * max_searches) if meter is not None else None)
                    cost: float | None = 0.0   # nothing was charged unless a call got an answer
                    try:
                        try:
                            r = self._post(url, scope, body)
                        except httpx.ConnectError as e:
                            last = f"network: {type(e).__name__}"
                            continue
                        except httpx.HTTPError as e:
                            cost = None   # the request may have reached the service
                            last = f"network: {type(e).__name__}"
                            continue
                        except Exception as e:  # noqa: BLE001 - token failures: try the next scope
                            last = f"token: {type(e).__name__}: {str(e)[:200]}"
                            continue
                        if r.status_code == 200:
                            try:
                                payload = r.json()
                            except ValueError:
                                cost, last = None, "the web search answered with something that is not JSON"
                                break
                            cost = self._web_cost(meter, role, payload)
                            if isinstance(payload, dict) and payload.get("status") in ("failed", "cancelled"):
                                last = f"the web search {payload['status']}: {str(payload.get('error'))[:200]}"
                                break
                            return self._web_found(payload)
                        last = f"HTTP {r.status_code}: {r.text[:300]}"
                        if r.status_code == 429:
                            try:
                                wait = min(WEB_WAIT_S, max(wait, float(r.headers.get("retry-after") or 0)))
                            except ValueError:
                                pass
                            break
                        if r.status_code in (408, 500, 502, 503, 504):
                            break
                        if r.status_code in (401, 403, 404):
                            continue
                        raise AIError(f"web search refused the request: {last}")
                    finally:
                        if ticket is not None:
                            meter.settle(ticket, ticket.bound if cost is None else cost)
                time.sleep(wait)
        raise AIError(f"web search: {last}")

    def _web_scopes(self, role: str) -> list[str]:
        with self._lock:
            cached = self._routes.get(role)
        both = ["https://cognitiveservices.azure.com/.default", "https://ai.azure.com/.default"]
        return ([cached[1]] + [s for s in both if s != cached[1]]) if cached else both

    @staticmethod
    def _web_cost(meter: Any, role: str, payload: Any) -> float | None:
        """What a web search call cost: its tokens at list price and a fee for each search it ran."""
        if meter is None:
            return None
        try:
            usage = payload["usage"]
            searches = sum(1 for i in payload.get("output") or [] if isinstance(i, dict) and i.get("type") == "web_search_call")
            return meter.chat_cost(role, int(usage["input_tokens"]), int(usage["output_tokens"])) + WEB_SEARCH_FEE * max(1, searches)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _web_found(payload: Any) -> dict:
        """The addresses and searches of a Responses API answer (no address when the search found nothing)."""
        urls: list[str] = []
        cited: list[str] = []
        queries: list[str] = []
        for item in (payload.get("output") if isinstance(payload, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call":
                action = item.get("action") if isinstance(item.get("action"), dict) else {}
                asked = action.get("queries") if isinstance(action.get("queries"), list) else [action.get("query")]
                queries += [str(q)[:300] for q in asked if q][:6]
                for s in action.get("sources") or []:
                    if isinstance(s, dict) and isinstance(s.get("url"), str):
                        urls.append(s["url"])
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    for a in (part.get("annotations") if isinstance(part, dict) else None) or []:
                        if isinstance(a, dict) and a.get("type") == "url_citation" and isinstance(a.get("url"), str):
                            cited.append(a["url"])
        found = list(dict.fromkeys(urls + cited))
        return {"urls": found, "queries": queries}


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
_SLIDE = re.compile(r'^SLIDE (\d+): "', re.M)
_ROLE_TASKS = frozenset({"verify_graded", "pick_sentences", "complete_set", "rank_sources"})   # stub handlers that need to know the model asked
_TITLE = re.compile(r'<source n="(\d+)" title="([^"]*)"')
_SKILL_LINE = re.compile(r"^\[(\d+)\] (.+)$", re.M)
_PAGE_LINE = re.compile(r"^\[(\d+)\] (.+)$", re.M)


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
        # Source discovery (ADR 0018): what a test decides. Web search finds these addresses for a query; the grade
        # a model gives a page (role, title, text); and whether a model finds an accepted set complete.
        self.search_urls: Callable[[str], list[str]] = lambda query: []
        self.grades: Callable[[str, str, str], str] = lambda role, title, text: "teaches"
        self.set_answer: Callable[[str], str] = lambda role: "yes"
        # The order, the drops and the direct pages a model gives accepted pages: (role, titles) -> (numbers best first,
        # numbers dropped[, numbers direct]); every page is direct unless a test says otherwise.
        self.ranking: Callable[[str, list[str]], tuple] = lambda role, titles: (list(range(1, len(titles) + 1)), [])

    def web_search(self, query: str, domains: tuple[str, ...] = (), max_searches: int = 1) -> dict:
        self.calls.append(("author", "web_search"))
        found = {"urls": list(self.search_urls(query)), "queries": [query]}
        meter = self.meter
        if meter is None:
            return found
        ticket = meter.open_call("author/web_search", meter.chat_cost("author", WEB_PRE_INPUT, WEB_MAX_OUT) + WEB_SEARCH_FEE * max_searches)
        meter.settle(ticket, 0.0)
        return found

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
        handler = getattr(self, "_" + task)
        return json.dumps(handler(user, role) if task in _ROLE_TASKS else handler(user))

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

    def _deck(self, user: str) -> dict:
        """Present's visual deck: three outcomes, two parts, a flow with a tip quoting a source on the first
        slide, a contrast on the second, and a recap."""
        n = len(_SLIDE.findall(user))
        q = _quotes(user)
        src, quote = next((x for x in q if len(x[1]) <= 240), q[0])
        half = max(1, n // 2)
        slides = []
        if n:
            slides.append({"slide": 0, "visual": {"type": "flow", "steps": [
                {"label": "Plan the change", "note": "", "icon": "plan"}, {"label": "Make the change", "note": ""},
                {"label": "Check the result", "note": "", "icon": "check"}]},
                "tip": {"kind": "tip", "text": "Remember how the source puts it.", "quote": quote, "source": src}})
        if n > 1:
            slides.append({"slide": 1, "visual": {"type": "contrast",
                           "left": {"title": "Do", "tone": "good", "points": ["Follow the steps in order."]},
                           "right": {"title": "Avoid", "tone": "bad", "points": ["Skipping the check."]}}})
        return {"outcomes": ["Explain the key idea of this skill.", "Choose the setting that fits a task.", "Avoid the common trap."],
                "sections": [{"title": "The idea", "slides": list(range(half))}, {"title": "In practice", "slides": list(range(half, n))}]
                if n >= 2 else [],
                "slides": slides,
                "recap": ["The key idea, in short.", "The setting that fits.", "The trap to avoid."]}

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

    def _referee(self, user: str) -> dict:
        """Decides the listed points the way the stub grader judges them."""
        return {"points": [{**p, "why": "stub referee"} for p in self._grade(user)["points"]]}

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

    # ---- source discovery (ADR 0018)

    def _plan_queries(self, user: str) -> dict:
        """Two search phrases for each skill: its words without the verb, and with "how to"."""
        block = user.split("<skills>")[-1].split("</skills>")[0]
        out = []
        for k, text in _SKILL_LINE.findall(block):
            words = text.split()
            base = " ".join(words[1:]) if len(words) >= 4 else text
            out.append({"skill": int(k), "queries": [base, f"{base} how to"]})
        return {"skills": out}

    def _verify_graded(self, user: str, role: str) -> dict:
        """A source gets the grade a test decides (`grades`) when it has a sentence to quote, else `no`."""
        first: dict[int, str] = {}
        for n, sentence in _quotes(user):
            first.setdefault(n, sentence)
        titles = {int(n): t for n, t in _TITLE.findall(user)}
        out = []
        for n in sorted(titles):
            grade = self.grades(role, titles[n], first.get(n, "")) if n in first else "no"
            out.append({"n": n, "grade": grade, "quote": first.get(n, "") if grade != "no" else "", "reason": "stub",
                        "kind": "other-task"})
        return {"sources": out}

    def _pick_sentences(self, user: str, role: str) -> dict:
        """The first numbered sentence of every source."""
        return {"sources": [{"n": int(n), "sentence": 1} for n in re.findall(r'<source n="(\d+)"', user)]}

    def _complete_set(self, user: str, role: str) -> dict:
        """One obligation for each source, supported by that source's first sentence; the answer a test decides."""
        first: dict[int, str] = {}
        for n, sentence in _quotes(user):
            first.setdefault(n, sentence)
        return {"obligations": [{"text": f"Part {n}", "source": n, "quote": q} for n, q in sorted(first.items())],
                "answer": self.set_answer(role), "reason": "stub"}

    def _rank_sources(self, user: str, role: str) -> dict:
        """The order and the drops a test decides (`ranking`), from the titles of the sources."""
        titles = [t for _, t in sorted((int(n), t) for n, t in _TITLE.findall(user))]
        said = self.ranking(role, titles)
        direct = said[2] if len(said) > 2 else list(range(1, len(titles) + 1))
        return {"order": said[0], "drop": said[1], "direct": direct, "reason": "stub"}

    def _scan_titles(self, user: str) -> dict:
        """For each skill, up to two pages whose title shares a word of five letters or more with it."""
        skills_block = user.split("<skills>")[-1].split("</skills>")[0]
        pages_block = user.split("<pages>")[-1].split("</pages>")[0]
        pages = [(int(n), line) for n, line in _PAGE_LINE.findall(pages_block)]
        matches = []
        for k, text in _SKILL_LINE.findall(skills_block):
            words = {w for w in re.findall(r"[a-z]{5,}", text.lower())}
            hit = [n for n, line in pages if words & set(re.findall(r"[a-z]{5,}", line.lower()))][:2]
            matches.append({"skill": int(k), "pages": hit})
        return {"matches": matches}
