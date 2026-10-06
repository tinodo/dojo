"""Budgets and metering for team mode (ADR 0009, "Money").

Every paid call reserves its bounded maximum before it is made and settles the real cost after. A learner
action (a graded answer, an Ask, a check...) first reserves its expected calls as one hold, at the route,
before anything is recorded; each call then draws its exact bound from that hold, and tops it up against
the members' daily cap if the hold does not cover it. All of it happens under the store lock, so two
reservations can never both fit into the same remaining money.

What is kept, and where (no per-member number outside the member's own folder):
- learners/<key>/allowance.json: that learner's counts, spend and open reservations, today and yesterday;
- team/cap-today.json: today's members' spend as one sum, and the open reservations under random ids;
- team/meter.json: pooled sums per day and month, written under the 5-member rule;
- team/budget.json: the allowances and the cap the owner set;
- team/budget-flags.json: "Reservation bound exceeded", by role and call kind only.

With team mode off nothing here runs: no file is written, no call is changed, and the owner's costs stay
exactly as they are (the Budget is not wired into the AI and speech clients then).
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator

from .core import Abandoned, Spend, UserError, current_spend, current_writer, iso, new_id, spending, utcnow

log = logging.getLogger("dojo.budget")

# action -> (what the owner reads on the Members page, the words in a refusal, the default count per day)
ACTIONS: dict[str, tuple[str, str, int]] = {
    "ask": ("Ask the coach", "questions to the coach", 20),
    "answer": ("Graded answers (submit, regrade and judge again)", "graded answers", 25),
    "hint": ("Hints", "hints", 40),
    "item": ("New practice items", "new practice items", 25),
    "check": ("Spoken checks", "spoken checks", 4),
    "rehearsal": ("Rehearsals", "rehearsals", 3),
    "exam": ("Practice exams started", "practice exams", 2),
    "transcribe": ("Transcriptions", "transcriptions", 60),
    "lesson": ("First lesson for a skill without one", "first lessons", 3),
    "audio": ("Lesson audio", "lesson audio", 5),
    "lab": ("Lab runs", "lab runs", 150),
    "pool": ("Practice-exam pool batches made for them", "practice-exam questions made for you", 6),
}
# What the learner reads on their own page, where the owner's wording would not fit.
LEARNER_LABELS = {"pool": "Practice-exam questions made for you (batches)"}
CAP_USD = 5.0
MAX_COUNT = 1000
MAX_CAP_USD = 500.0
# The pool keeper keeps counting its batches itself (6 a member, 24 for all members together, ADR 0009),
# so its reservations do not count a second time.
UNCOUNTED = frozenset({"pool"})

SHARED_REFUSAL = "Dojo has used today's shared budget. It starts again at midnight UTC."
# Spent money is still below the cap, but what is set aside for work running now fills it: soon free again.
SHARED_BUSY = ("Today's shared budget is set aside for work that is running now. Try again in a few minutes; "
               "what that work does not use comes back when it ends.")
# A practice-exam batch that today's reservation could not cover is left out, never paid for on credit.
BATCH_SKIPPED = "Left out: today's budget for this action could not cover another batch of questions."
# Why an answer that is saved is not judged yet, in the learner's words (the pending card shows them).
GRADE_WAITS = ("Your answer is saved. Today's shared budget is used, so judging waits until after midnight UTC, "
               "when \"Judge my answer now\" opens again.")
GRADE_WAITS_OWN = ("Your answer is saved. You have used today's allowance for graded answers, so judging waits until "
                   "after midnight UTC, when \"Judge my answer now\" opens again.")
GRADE_BUSY = ("Your answer is saved. Today's shared budget is set aside for work that is running now: press "
              "\"Judge my answer now\" again in a few minutes.")
SECOND_TRY_SKIPPED = "Not tried a second time: today's budget for this action could not cover another try."

# ------------------------------------------------------------------ bounds

TOKENS_PER_MESSAGE = 16      # chat-template tokens per message, above what the providers document
TOKENS_PER_REQUEST = 64      # and per request
# max_completion_tokens, per role and call kind: set generously above the longest answers seen, because
# a reasoning model spends them on reasoning and the visible answer together. Raised in a PR if Studio
# ever shows "Reservation bound exceeded" or an answer is cut short.
MAX_OUT_ROLE = {"author": 16000, "gate": 12000, "grader": 8000}
MAX_OUT = {
    ("author", "lesson"): 32000, ("author", "deep_narration"): 24000, ("author", "item_case"): 24000,
    ("author", "answer"): 8000, ("gate", "gate_elements"): 16000,
    ("author", "ping"): 2000, ("gate", "ping"): 2000, ("grader", "ping"): 2000,
}
# The worst case of each action, reserved together at the route before anything is recorded: every model
# call the workflow can make (ADR 0009, "Money"). TRIES: each ask_json may ask twice. The input of a call
# is not known yet at the route, so each call is reserved at PRE_INPUT tokens, above the largest prompt
# the excerpt sizes allow (learning.GROUNDING_CHARS, MCQ_CHARS, itemwriter.ITEM_CHARS and CASE_CHARS) in
# bytes, with the template, the learner's text and, for the lesson gate, 40 elements of up to 1,400
# characters; the call itself then draws its exact bound (UTF-8 bytes) from the hold. Only a call past
# both (a page in a script of several bytes a character, or a transport retry the service charged) tops
# the hold up, and a refusal then ends the action the way a model failure does: an answer stays saved
# and is judged when the learner presses "Judge my answer now" after midnight UTC; nothing else is kept.
#
# A second try - make_item writing again, make_lesson rewriting what did not hold, a practice exam's
# second round, any writer batch - is not part of the route's reservation: it runs only if what is left
# of the reservation covers its own worst case (Budget.claim), and otherwise it is left out, as when it
# fails. So no action is ever stopped half-way for lack of money with ordinary inputs.
TRIES = 2
PRE_INPUT_TOKENS = 48000
PRE_INPUT = {("grader", "grade"): 64000, ("gate", "gate_judgement"): 64000, ("gate", "gate_elements"): 80000}
# A lesson has at most 171 elements to check (the caps of learning._clean_lesson: title, summary, 6
# sections of 1 + 6, 4 misconceptions, example title, situation and 8 steps, 5 questions, 9 slides of
# 1 + 5 + 6), sent to the gate 40 at a time.
LESSON_GATE_CHUNKS = 5
# One round of make_item, and of make_lesson: the second round claims the same again from what is left.
ITEM_ROUND = (("author", "item", TRIES), ("gate", "gate_item", TRIES))
LESSON_ROUND = (("author", "lesson", TRIES), ("gate", "gate_elements", LESSON_GATE_CHUNKS * TRIES))
# The writers of practice-exam questions: per batch, the author once and the gate once, each ask_json.
WRITER_TASKS = {"single": ("mcq", "gate_mcq"), "multi": ("item_multi", "gate_items"),
                "sequence": ("item_sequence", "gate_items"), "match": ("item_match", "gate_items"),
                "hotarea": ("item_hotarea", "gate_items"), "yesno": ("item_yesno", "gate_items"),
                "case": ("item_case", "gate_items")}


def writer_batch(kind: str) -> tuple[tuple[str, str, int], ...]:
    """The worst case of one batch of practice-exam questions of `kind`."""
    author, gate = WRITER_TASKS.get(kind, WRITER_TASKS["case"])
    return (("author", author, TRIES), ("gate", gate, TRIES))


def pre_input(role: str, task: str) -> int:
    return PRE_INPUT.get((role, task), PRE_INPUT_TOKENS)


ACTION_CALLS: dict[str, tuple[tuple[str, str, int], ...]] = {
    # The tutor's answer, then the gate over its points (at most 8 and "not covered": one chunk).
    "ask": (("author", "answer", TRIES), ("gate", "gate_elements", TRIES)),
    # The grader once, and once more with the quotes that did not verify; then the gate on what is shown.
    "answer": (("grader", "grade", 2 * TRIES), ("gate", "gate_judgement", TRIES)),
    "item": ITEM_ROUND,
    # A check writes one item per question (make_item), so its route passes the questions as batches.
    "check": ITEM_ROUND,
    "rehearsal": writer_batch("single"),
    "lesson": LESSON_ROUND,
    # Per batch of questions: a practice exam reserves its first round (ExamRoom.first_round), a pool batch
    # the batch of the kind it fills (PoolKeeper._pay).
    "exam": writer_batch("single"),
    "pool": writer_batch("single"),
}
# Actions whose reservation is first rounds (make_item, make_lesson): kept for them (Hold.owed), so that
# a second try of one question never takes the money of a question still to be written.
FIRST_ROUNDS = frozenset({"item", "check", "lesson"})
ACTION_CHARACTERS = {"check": 6000, "audio": 20000}   # text to speech, characters

# Used only when the list price cannot be read: ceilings above every list price of the deployed models
# and speech meters, so a reservation is never below what the call can cost.
CEILING = {"input": 10.0 / 1e6, "output": 60.0 / 1e6, "voice": 30.0 / 1e6,
           "transcription": 3.0 / 3600, "video": 1.0 / 60}
PRICE_CACHE_S = 600
# The stub's pretend list price (no money is spent): with the stub and no price list, budgets behave as
# they would with a typical deployed model instead of refusing everything at the ceilings.
STUB = {"input": 1.25 / 1e6, "output": 10.0 / 1e6, "voice": 16.0 / 1e6,
        "transcription": 1.0 / 3600, "video": 0.5 / 60}

# An open reservation in the cap file that no hold of this process owns, older than the longest a paid
# call can take (the model client's 480 s read timeout) plus 15 minutes, was left by a process that ended:
# what it had drawn stays spent, the rest is freed (ADR 0009, "Money", crash safety).
STALE_S = 480 + 15 * 60

METER_DAYS = 62
METER_MONTHS = 13
GROUP = 5          # the 5-member rule


class Refused(UserError):
    """A reservation that does not fit: a calm 429 before anything is recorded."""


class _Claim:
    """Money one batch of an action set aside inside its hold before its first call (Budget.batch)."""

    def __init__(self, hold: "Hold", amount: float):
        self.hold, self.left = hold, amount


_CLAIM: contextvars.ContextVar[_Claim | None] = contextvars.ContextVar("dojo_claim", default=None)


def too_large(amount: float, cap: float) -> str:
    return (f"This needs up to ${amount:.2f} set aside before it starts, more than the members' daily cap of "
            f"${cap:.2f}. A shorter practice exam needs less; the owner can raise the cap.")


def personal_refusal(action: str) -> str:
    noun = ACTIONS.get(action, (action, action, 0))[1]
    return f"You have used today's allowance for {noun}. It starts again at midnight UTC."


def _day(now: Any = None) -> str:
    return (now or utcnow()).date().isoformat()


def _entry(value: Any) -> dict:
    """An open entry of the cap file: what it set aside, when, and what its calls drew."""
    if isinstance(value, dict):
        amount = float(value.get("amount") or 0.0)
        return {"amount": amount, "at": str(value.get("at") or ""), "drawn": float(value["drawn"] or 0.0) if "drawn" in value else amount}
    amount = float(value or 0.0)
    return {"amount": amount, "at": "", "drawn": amount}   # an entry without a record of its calls: all of it


def _age_s(at: str, now: Any) -> float:
    try:
        return (now - datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def chat_input_bound(body: dict) -> int:
    """A ceiling on the input tokens of one chat request: the UTF-8 bytes of everything sent (a byte-level
    BPE token covers at least one byte), plus the template's tokens per message and per request."""
    messages = body.get("messages") or []
    sent = json.dumps(messages, ensure_ascii=False)
    if body.get("response_format") is not None:
        sent += json.dumps(body["response_format"], ensure_ascii=False)
    return len(sent.encode("utf-8")) + TOKENS_PER_MESSAGE * len(messages) + TOKENS_PER_REQUEST


def max_out(role: str, task: str) -> int:
    return MAX_OUT.get((role, task), MAX_OUT_ROLE.get(role, 16000))


class Hold:
    """One reservation: money set aside for a learner's action, spent by its calls, settled when the last
    holder lets go. A job adopts its route's hold, so the money stays reserved until the job ends."""

    def __init__(self, budget: "Budget", learner: str, action: str, package: str | None, day: str,
                 amount: float, member: bool, counted: bool, rid: str, cap_id: str | None):
        self.budget, self.learner, self.action, self.package = budget, learner, action, package
        self.day, self.member, self.counted, self.id = day, member, counted, rid
        self.reserved = amount     # set aside, in USD
        self.inflight = 0.0        # bounds of the calls running now
        self.claimed = 0.0         # set aside by batches for their calls still to come
        self.owed = 0.0            # kept for first rounds not yet started: a second try cannot take it
        self.spent = 0.0           # what the settled calls cost
        self.calls = 0
        self.refs = 1
        self.adopted = False
        self.failed = False        # a holder ended in failure (the route, or the job that adopted it)
        self.voided = False
        self.closed = False
        # The cap entries of a member's hold, one per budget day it reserved on: [day, cap id, amount].
        # Each is settled in its own day only; a day that is over is never added to the next (ADR 0009).
        self.parts: list[list] = [[day, cap_id, amount]] if cap_id else []

    @property
    def cap_id(self) -> str | None:
        return self.parts[0][1] if self.parts else None

    def free(self) -> float:
        """What a call outside a claim, or a new claim, may still take."""
        return self.reserved - self.spent - self.inflight - self.claimed - self.owed

    def adopt(self) -> "Hold":
        with self.budget.lock:
            self.refs += 1
            self.adopted = True
        return self

    def void(self) -> None:
        """The action turned out to start nothing new (a check already open): give the count back."""
        self.voided = True

    def release(self, failed: bool = False) -> None:
        """One holder lets go; `failed` when its part of the action failed. The count is given back when the
        action failed (or started nothing) before any paid call ran, whether a job adopted it or not."""
        with self.budget.lock:
            self.failed = self.failed or failed
            self.refs -= 1
            if self.refs > 0 or self.closed:
                return
            self.closed = True
            self.budget._close(self, refund=(self.failed or self.voided) and self.calls == 0)


class Ticket:
    def __init__(self, hold: Hold | None, own: bool, bound: float, kind: str, bounded: bool,
                 learner: str | None, package: str | None):
        self.hold, self.own, self.bound, self.kind, self.bounded = hold, own, bound, kind, bounded
        self.learner, self.package = learner, package
        self.settled = False


class Budget:
    """Team mode's money (ADR 0009): reserves each paid call's worst case before it is made, settles the
    real cost after, and refuses work that would pass a member's allowance or the members' daily cap.
    With team mode off it is not wired in."""
    def __init__(self, settings: Any, store: Any, owner_key: str, costs: Any = None,
                 members: Callable[[], list] | None = None,
                 enrolled_counts: Callable[[], dict[str, int]] | None = None):
        self.settings = settings
        self.store = store
        self.owner_key = owner_key
        self.costs = costs
        self.members = members or (lambda: [])
        self.enrolled_counts = enrolled_counts or (lambda: {})
        self.lock = store.lock          # one lock with the store: the pool keeper charges under it too
        self._prices: tuple[float, dict] | None = None
        self._warned: set[str] = set()
        # The cap entries owned by holds of this process: never expired, whatever their age.
        self._live: set[str] = set()

    @property
    def active(self) -> bool:
        return bool(getattr(self.settings, "team", False)) and not getattr(self.settings, "local", False)

    # ------------------------------------------------------------ prices

    def prices(self) -> dict:
        if self._prices and time.monotonic() - self._prices[0] < PRICE_CACHE_S:
            return self._prices[1]
        doc: dict = {}
        if self.costs is not None:
            try:
                cached = self.store.read("content", "prices", default=None)
                if self.costs._ours(cached):
                    doc = cached
                elif getattr(self.settings, "real_ai", False):
                    doc = self.costs.prices()
            except Exception as e:  # noqa: BLE001 - no price list: the ceilings below apply
                log.warning("prices for budgets could not be read: %s", type(e).__name__)
        self._prices = (time.monotonic(), doc)
        return doc

    def rates(self, role: str) -> tuple[float, float]:
        """USD per input token and per output token for a role's model, at list price; the ceilings when
        the price is not known."""
        try:
            m = self.costs.model_price(self.prices() or self.costs._nothing(), role) if self.costs else None
            if m and m.get("found"):
                return float(m["input"]["per_token"]), float(m["output"]["per_token"])
        except Exception:  # noqa: BLE001 - an unreadable price: the ceiling
            pass
        fallback = CEILING if getattr(self.settings, "real_ai", False) else STUB
        return fallback["input"], fallback["output"]

    def meter_rate(self, key: str) -> float:
        """USD per character (voice), per second (transcription, video)."""
        unit = {"voice": "per_character", "transcription": "per_second", "video": "per_second"}[key]
        try:
            entry = ((self.prices() or {}).get("meters") or {}).get(key)
            if isinstance(entry, dict) and entry.get(unit) is not None:
                return float(entry[unit])
        except (TypeError, ValueError):
            pass
        return (CEILING if getattr(self.settings, "real_ai", False) else STUB)[key]

    def chat_cost(self, role: str, tokens_in: int, tokens_out: int) -> float:
        rin, rout = self.rates(role)
        return tokens_in * rin + tokens_out * rout

    def action_amount(self, action: str, batches: int = 1) -> float:
        total = batches * self.calls_amount(ACTION_CALLS.get(action, ()))
        total += ACTION_CHARACTERS.get(action, 0) * self.meter_rate("voice")
        return round(total, 6)

    # ------------------------------------------------------------ settings the owner sets

    def limits(self) -> dict:
        doc = self.store.read("team", "budget", default=None) or {}
        saved = doc.get("allowances") if isinstance(doc.get("allowances"), dict) else {}
        allowances = {}
        for action, (_, _, default) in ACTIONS.items():
            value = saved.get(action, default)
            allowances[action] = value if isinstance(value, int) and 0 <= value <= MAX_COUNT else default
        cap = doc.get("cap")
        cap = float(cap) if isinstance(cap, (int, float)) and 0 <= cap <= MAX_CAP_USD else CAP_USD
        return {"allowances": allowances, "cap": cap}

    def set_limits(self, allowances: dict | None, cap: float | None) -> dict:
        current = self.limits()
        for action, value in (allowances or {}).items():
            if action not in ACTIONS:
                raise UserError(f"Unknown action: {action}.")
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_COUNT:
                raise UserError(f"An allowance is a whole number from 0 to {MAX_COUNT}.")
            current["allowances"][action] = value
        if cap is not None:
            if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 <= cap <= MAX_CAP_USD:
                raise UserError(f"The daily cap is an amount from 0 to {MAX_CAP_USD:.0f} USD.")
            current["cap"] = round(float(cap), 2)
        with self.lock:
            self.store.write("team", "budget", value=current)
        return self.admin_view()

    # ------------------------------------------------------------ files

    def _allowance(self, learner: str) -> dict:
        doc = self.store.read("learners", learner, "allowance", default=None)
        days = doc.get("days") if isinstance(doc, dict) and isinstance(doc.get("days"), dict) else {}
        keep = {_day(), _day(utcnow() - timedelta(days=1))}
        return {"days": {d: v for d, v in days.items() if d in keep and isinstance(v, dict)}}

    @staticmethod
    def _today(doc: dict, day: str) -> dict:
        d = doc["days"].setdefault(day, {})
        d.setdefault("counts", {})
        d.setdefault("spent", 0.0)
        d.setdefault("open", {})
        return d

    def _save_allowance(self, learner: str, doc: dict) -> bool:
        try:
            self.store.write("learners", learner, "allowance", value=doc)
            return True
        except Abandoned:
            return False   # the learner left meanwhile: nothing of theirs is written again

    def _cap(self) -> dict:
        """Today's cap file. An open entry left by a process that ended (no live hold owns it, older than
        STALE_S) is folded in: what it drew stays spent, the rest is free again. The caller writes it."""
        doc = self.store.read("team", "cap-today", default=None)
        if not isinstance(doc, dict) or doc.get("day") != _day():
            return {"day": _day(), "spent": 0.0, "open": {}}
        spent = float(doc.get("spent") or 0.0)
        opened = doc.get("open") if isinstance(doc.get("open"), dict) else {}
        now, kept = utcnow(), {}
        for cid, value in opened.items():
            entry = _entry(value)
            if cid not in self._live and _age_s(entry["at"], now) > STALE_S:
                spent += entry["drawn"]
                continue
            kept[cid] = entry
        return {"day": doc["day"], "spent": round(spent, 6), "open": kept}

    @staticmethod
    def _cap_total(cap: dict) -> float:
        return float(cap.get("spent") or 0.0) + sum(_entry(v)["amount"] for v in (cap.get("open") or {}).values())

    def recover(self, learners: list[str]) -> None:
        """At startup, before anything is served: no hold can exist yet, so every open reservation was left
        by the process that ended. In the cap file, what each had drawn (its calls' bounds, or their real
        cost once settled) stays spent and the rest is freed; the learners' own open entries go (they enforce
        nothing). Under the store lock."""
        with self.lock:
            doc = self.store.read("team", "cap-today", default=None)
            if isinstance(doc, dict) and doc.get("day") == _day() and doc.get("open"):
                opened = doc["open"] if isinstance(doc["open"], dict) else {}
                spent = float(doc.get("spent") or 0.0) + sum(_entry(v)["drawn"] for v in opened.values())
                self.store.write("team", "cap-today", value={"day": doc["day"], "spent": round(spent, 6), "open": {}})
            for key in learners:
                doc = self.store.read("learners", key, "allowance", default=None)
                days = doc.get("days") if isinstance(doc, dict) and isinstance(doc.get("days"), dict) else {}
                if any(isinstance(d, dict) and d.get("open") for d in days.values()):
                    for d in days.values():
                        if isinstance(d, dict):
                            d["open"] = {}
                    self._save_allowance(key, doc)

    # ------------------------------------------------------------ reserve, draw, settle

    def reserve(self, learner: str, action: str, package: str | None = None, amount: float = 0.0,
                count: bool = True) -> Hold:
        """Set money aside for a learner's action, or refuse. The owner is counted and metered, never
        refused. Writes the cap file first, so a crash between the two over-counts, never under-counts."""
        member = learner != self.owner_key
        counted = count and action not in UNCOUNTED
        amount = max(0.0, float(amount))
        with self.lock:
            day = _day()
            limits = self.limits()
            doc = self._allowance(learner)
            today = self._today(doc, day)
            if member and counted and today["counts"].get(action, 0) >= limits["allowances"].get(action, 0):
                raise Refused(personal_refusal(action))
            cap_id = None
            if member and limits["cap"] > 0 and amount > limits["cap"] + 1e-9:
                raise Refused(too_large(amount, limits["cap"]))
            if member and amount > 0:
                cap = self._cap()
                if self._cap_total(cap) + amount > limits["cap"] + 1e-9:
                    raise Refused(self._why_full(cap, amount, limits["cap"]))
                cap_id = new_id("c")
                cap["open"][cap_id] = {"amount": round(amount, 6), "at": iso(utcnow()), "drawn": 0.0}
                self.store.write("team", "cap-today", value=cap)
                self._live.add(cap_id)
            rid = new_id("r")
            if counted:
                today["counts"][action] = today["counts"].get(action, 0) + 1
            today["open"][rid] = {"amount": round(amount, 6), "action": action, "at": iso()}
            if not self._save_allowance(learner, doc):
                if cap_id:
                    cap = self._cap()
                    cap["open"].pop(cap_id, None)
                    self.store.write("team", "cap-today", value=cap)
                    self._live.discard(cap_id)
                raise Abandoned("the learner's data was deleted")
            return Hold(self, learner, action, package, day, amount, member, counted, rid, cap_id)

    def _why_full(self, cap: dict, amount: float, limit: float) -> str:
        spent = float(cap.get("spent") or 0.0)
        return SHARED_BUSY if spent + amount <= limit + 1e-9 else SHARED_REFUSAL

    def _add_to_cap(self, hold: Hold, more: float, check: bool) -> None:
        """Book `more` for a member's hold in today's cap file, in today's entry of the hold: a hold made
        before midnight gets a new entry for the new day, so yesterday's money never lands in today's sum.
        Under the lock."""
        cap = self._cap()
        if check and self._cap_total(cap) + more > self.limits()["cap"] + 1e-9:
            raise Refused(self._why_full(cap, more, self.limits()["cap"]))
        day = cap["day"]
        part = hold.parts[-1] if hold.parts and hold.parts[-1][0] == day else None
        if part is None:
            part = [day, new_id("c"), 0.0]
            hold.parts.append(part)
            self._live.add(part[1])
        part[2] = round(float(part[2]) + more, 6)
        entry = cap["open"].setdefault(part[1], {"amount": 0.0, "at": iso(utcnow()), "drawn": 0.0})
        entry["amount"] = round(entry["amount"] + more, 6)
        self.store.write("team", "cap-today", value=cap)

    @staticmethod
    def _shares(hold: Hold, total: float) -> list[float]:
        """How `total` falls on the hold's parts: the earliest takes first, up to what it set aside; the
        last takes the rest."""
        out, left = [], total
        for i, (_, _, amount) in enumerate(hold.parts):
            share = left if i == len(hold.parts) - 1 else min(left, float(amount))
            left -= share
            out.append(share)
        return out

    def _mark_drawn(self, hold: Hold) -> None:
        """Keep in the cap file what a member's hold has drawn today (its settled cost plus the bounds of the
        calls running now), before a call is sent: after a crash, that much stays spent. Under the lock."""
        if not hold.member or not hold.parts or hold.closed:
            return
        cap = self._cap()
        changed = False
        for (day, cid, _), share in zip(hold.parts, self._shares(hold, hold.spent + hold.inflight)):
            entry = cap["open"].get(cid)
            if day == cap["day"] and entry is not None and abs(entry["drawn"] - share) > 1e-12:
                entry["drawn"] = round(share, 6)
                changed = True
        if changed:
            self.store.write("team", "cap-today", value=cap)

    def _grow(self, hold: Hold, more: float) -> None:
        """Top a hold up by `more`, against the cap for a member. Under the lock."""
        if hold.member:
            self._add_to_cap(hold, more, check=True)
        hold.reserved += more
        doc = self._allowance(hold.learner)
        entry = self._today(doc, hold.day)["open"].get(hold.id)
        if entry is not None:
            entry["amount"] = round(hold.reserved - hold.spent, 6)
            self._save_allowance(hold.learner, doc)

    def _excess(self, hold: Hold, more: float) -> None:
        """A call cost more than its bound: the excess is booked even past the cap (it was spent)."""
        if hold.member:
            self._add_to_cap(hold, more, check=False)
        hold.reserved += more

    def open_call(self, kind: str, bound: float, bounded: bool = True) -> Ticket:
        """Before a paid call: draw its bound from the current action's hold (topping it up if needed), or
        reserve it on its own. A refusal is raised before anything is sent."""
        spend = current_spend()
        learner = spend.learner if spend else current_writer()
        package = spend.package if spend else None
        hold = spend.hold if spend and spend.hold is not None and not spend.hold.closed else None
        own = False
        if hold is None and learner:
            if self.active and learner != self.owner_key and kind not in self._warned:
                self._warned.add(kind)
                log.warning("a member's %s call had no reservation for its action; reserved on its own", kind)
            hold = self.reserve(learner, spend.action if spend else "other", package, bound, count=False)
            own = True
            with self.lock:
                hold.inflight += bound
                hold.calls += 1
                self._mark_drawn(hold)
        elif hold is not None:
            claim = _CLAIM.get()
            with self.lock:
                # A batch's call draws first from what its batch set aside (Budget.batch).
                take = min(bound, claim.left) if claim is not None and claim.hold is hold else 0.0
                short = bound - take - hold.free()
                if short > 1e-12:
                    self._grow(hold, short)
                if take:
                    claim.left -= take
                    hold.claimed -= take
                hold.inflight += bound
                hold.calls += 1
                self._mark_drawn(hold)
            package = hold.package
            learner = hold.learner
        elif self.active and kind not in self._warned:
            self._warned.add(kind)
            log.warning("a %s call ran with no learner attribution; booked to content", kind)
        return Ticket(hold, own, bound, kind, bounded, learner, package)

    def settle(self, ticket: Ticket, actual: float) -> None:
        """After the call: book what it really cost, and give back what it did not use."""
        if ticket.settled:
            return
        ticket.settled = True
        actual = max(0.0, float(actual))
        exceeded = ticket.bounded and actual > ticket.bound * (1 + 1e-9) + 1e-12
        hold = ticket.hold
        if hold is not None:
            with self.lock:
                hold.inflight = max(0.0, hold.inflight - ticket.bound)
                hold.spent += actual
                over = hold.spent + hold.inflight + hold.claimed + hold.owed - hold.reserved
                if over > 1e-12:
                    self._excess(hold, over)
                self._mark_drawn(hold)
            if ticket.own:
                hold.release()
        else:
            with self.lock:
                self._meter(None, None, False, actual)
        if exceeded:
            self._flag(ticket.kind)

    def _close(self, hold: Hold, refund: bool) -> None:
        """The last holder let go: the reservation becomes what was spent. Under the lock.

        A member's spend is settled in the budget day each part of it was reserved on: the earliest part
        takes the spend first, up to what it set aside, and the last takes the rest. Only today's part
        reaches today's cap file. A part reserved before midnight belongs to a day whose file was emptied,
        so nothing of it is added to the new day (ADR 0009, "Money")."""
        spent = hold.spent + hold.inflight
        if hold.member:
            cap = self._cap()
            changed = False
            for (day, cid, _), share in zip(hold.parts, self._shares(hold, spent)):
                self._live.discard(cid)
                if day != cap["day"]:
                    continue
                cap["open"].pop(cid, None)
                cap["spent"] = round(float(cap.get("spent") or 0.0) + share, 6)
                changed = True
            if not hold.parts and spent > 0 and hold.day == cap["day"]:
                cap["spent"] = round(float(cap.get("spent") or 0.0) + spent, 6)
                changed = True
            if changed:
                self.store.write("team", "cap-today", value=cap)
        self._meter(hold.learner, hold.package, hold.member, spent)
        doc = self._allowance(hold.learner)
        today = doc["days"].get(hold.day)
        if today is not None:
            today.setdefault("open", {}).pop(hold.id, None)
            today["spent"] = round(float(today.get("spent") or 0.0) + spent, 6)
            if refund and hold.counted and spent == 0:
                counts = today.setdefault("counts", {})
                counts[hold.action] = max(0, counts.get(hold.action, 0) - 1)
            self._save_allowance(hold.learner, doc)

    def calls_amount(self, calls: Any) -> float:
        """The worst case of a list of (role, task, calls) at list price, each call at its pre-input."""
        return round(sum(n * self.chat_cost(role, pre_input(role, task), max_out(role, task)) for role, task, n in calls), 6)

    def batch(self, kind: str):
        """Budget.claim for one batch of a practice-exam writer (exam, rehearsal, pool)."""
        return self.claim(writer_batch(kind))

    @contextmanager
    def claim(self, calls: Any, first: bool = False) -> Iterator[bool]:
        """Around one batch or round of an action: set its worst case aside inside the action's hold before
        its first call, or answer False, and it is left out: a member's batch never tops the hold up, so
        a practice exam is never refused half-way, only shorter, and a second try is skipped like a failed
        one. A first round (`first`) takes what the route kept for it (Hold.owed) and always goes ahead;
        a second try can never take that, so the questions of a check that are still to be written keep
        their money. The owner, and work with no hold, always go ahead."""
        spend = current_spend()
        hold = spend.hold if spend and spend.hold is not None and not spend.hold.closed else None
        if hold is None or not hold.member:
            yield True
            return
        amount = self.calls_amount(calls)
        with self.lock:
            if first:
                kept = min(hold.owed, amount)
                hold.owed -= kept
                amount = kept + max(0.0, min(amount - kept, hold.free()))
                fits = True
            else:
                fits = hold.free() + 1e-9 >= amount
            if fits:
                hold.claimed += amount
        if not fits:
            yield False
            return
        claim = _Claim(hold, amount)
        token = _CLAIM.set(claim)
        try:
            yield True
        finally:
            _CLAIM.reset(token)
            with self.lock:
                hold.claimed = max(0.0, hold.claimed - claim.left)
                claim.left = 0.0

    @contextmanager
    def action(self, learner: Any, action: str, package: str | None = None,
               amount: float | None = None, batches: int = 1) -> Iterator[Hold | None]:
        """Around a learner's action at the route: reserve before anything is recorded, attribute every
        call inside (and every job it starts) to it, and settle when the last of them ends. A refusal is a
        429 with nothing recorded. With team mode off this does nothing."""
        if not self.active:
            yield None
            return
        key = getattr(learner, "key", learner)
        hold = self.reserve(key, action, package, self.action_amount(action, batches) if amount is None else amount)
        if amount is None and action in FIRST_ROUNDS and hold.member:
            with self.lock:
                hold.owed = round(batches * self.calls_amount(ACTION_CALLS[action]), 6)
        failed = True
        try:
            with spending(Spend(key, action, package, hold)):
                yield hold
            failed = False
        finally:
            hold.release(failed=failed)

    # ------------------------------------------------------------ the meter (pooled sums only)

    def _buckets(self, learner: str | None, package: str | None, member: bool) -> list[str]:
        try:
            members = len(self.members())
        except Exception:  # noqa: BLE001 - unknown: the safest bucket
            members = 0
        if members < GROUP:
            return ["total"]
        if learner is None:
            return ["content"]
        if not member:
            return ["owner"]
        try:
            enrolled = (self.enrolled_counts() or {}).get(package or "", 0)
        except Exception:  # noqa: BLE001
            enrolled = 0
        return ["members", f"exam:{package}" if package and enrolled >= GROUP else "members_other"]

    def _meter(self, learner: str | None, package: str | None, member: bool, usd: float) -> None:
        if usd <= 0:
            return
        now = utcnow()
        day, month = _day(now), now.strftime("%Y-%m")
        doc = self.store.read("team", "meter", default=None)
        if not isinstance(doc, dict):
            doc = {}
        days = doc.get("days") if isinstance(doc.get("days"), dict) else {}
        months = doc.get("months") if isinstance(doc.get("months"), dict) else {}
        for bucket in self._buckets(learner, package, member):
            for period, key in ((days, day), (months, month)):
                sums = period.setdefault(key, {})
                sums[bucket] = round(float(sums.get(bucket) or 0.0) + usd, 6)
        oldest = _day(now - timedelta(days=METER_DAYS))
        days = {d: v for d, v in days.items() if d >= oldest}
        months = dict(sorted(months.items())[-METER_MONTHS:])
        self.store.write("team", "meter", value={"days": days, "months": months})

    def _flag(self, kind: str) -> None:
        log.warning("a %s call cost more than its reservation bound; the table needs a higher entry", kind)
        with self.lock:
            doc = self.store.read("team", "budget-flags", default=None) or {}
            kinds = sorted(set(doc.get("kinds") or []) | {kind})
            self.store.write("team", "budget-flags", value={"day": _day(), "kinds": kinds})

    def flags(self) -> dict | None:
        doc = self.store.read("team", "budget-flags", default=None)
        return doc if isinstance(doc, dict) and doc.get("kinds") else None

    # ------------------------------------------------------------ views

    def answer_waits(self, learner: Any) -> str | None:
        """Why judging a saved answer would be refused for the rest of today - the learner's allowance used,
        the shared budget spent, or an answer's worst case above the cap - or None. Reads only, so it opens
        again by itself at midnight UTC or when the owner raises the cap. Money set aside for work running
        now is not a reason: that comes back within minutes."""
        key = getattr(learner, "key", learner)
        if not self.active or key == self.owner_key:
            return None
        limits = self.limits()
        today = self._allowance(key)["days"].get(_day()) or {}
        if int((today.get("counts") or {}).get("answer", 0)) >= limits["allowances"].get("answer", 0):
            return GRADE_WAITS_OWN
        amount = self.action_amount("answer")
        if limits["cap"] > 0 and amount > limits["cap"] + 1e-9:
            return "Your answer is saved. " + too_large(amount, limits["cap"])
        with self.lock:
            spent = float(self._cap().get("spent") or 0.0)
        return GRADE_WAITS if spent + amount > limits["cap"] + 1e-9 else None

    def left_today(self, learner: Any) -> dict:
        """A learner's own allowances: what is left today. Only the learner sees this."""
        key = getattr(learner, "key", learner)
        owner = key == self.owner_key
        limits = self.limits()
        today = self._allowance(key)["days"].get(_day()) or {}
        counts = today.get("counts") or {}
        rows = []
        for action, (label, _, _) in ACTIONS.items():
            used = int(counts.get(action, 0))
            allowance = None if owner else limits["allowances"][action]
            rows.append({"action": action, "label": LEARNER_LABELS.get(action, label), "used": used, "allowance": allowance,
                         "left": None if owner else max(0, allowance - used)})
        spent = float(today.get("spent") or 0.0) + sum(float(o.get("amount") or 0.0) for o in (today.get("open") or {}).values())
        return {"team": self.active, "owner": owner, "day": _day(), "actions": rows,
                "spent_usd": round(spent, 4), "resets": "midnight UTC"}

    def totals(self) -> dict:
        """The pooled sums, as written (the 5-member rule was applied when they were)."""
        doc = self.store.read("team", "meter", default=None) or {}
        now = utcnow()
        day = ((doc.get("days") or {}).get(_day(now)) or {})
        month = ((doc.get("months") or {}).get(now.strftime("%Y-%m")) or {})
        return {"today": {k: round(float(v), 4) for k, v in day.items()},
                "month": {k: round(float(v), 4) for k, v in month.items()}}

    def admin_view(self) -> dict:
        limits = self.limits()
        return {"team": self.active, "cap_usd": limits["cap"],
                # What each action sets aside before it starts, at today's list prices (a practice exam: per
                # batch of questions it still has to write). An action above the cap can never start.
                "set_aside": {a: self.action_amount(a) for a in ACTIONS},
                "allowances": [{"action": a, "label": label, "value": limits["allowances"][a], "default": d}
                               for a, (label, _, d) in ACTIONS.items()],
                "totals": self.totals(), "flags": self.flags()}

    # ------------------------------------------------------------ housekeeping

    def prune(self, learners: list[str]) -> None:
        """Hourly: allowance days before yesterday go, and the cap file starts the new day empty."""
        with self.lock:
            cap = self.store.read("team", "cap-today", default=None)
            if isinstance(cap, dict):
                fresh = self._cap()   # a new day starts empty; entries left by an ended process are folded in
                if fresh != cap:
                    self.store.write("team", "cap-today", value=fresh)
            for key in learners:
                doc = self.store.read("learners", key, "allowance", default=None)
                if not isinstance(doc, dict):
                    continue
                kept = self._allowance(key)
                if kept != doc:
                    self._save_allowance(key, kept)


def writer_slot(ai: Any, kind: str):
    """Budget.batch for a writer that only has the model client: a no-op outside team mode."""
    meter = getattr(ai, "meter", None)
    return meter.batch(kind) if meter is not None else nullcontext(True)


def claim_slot(ai: Any, calls: Any, first: bool = False):
    """Budget.claim for code that only has the model client: a no-op outside team mode."""
    meter = getattr(ai, "meter", None)
    return meter.claim(calls, first) if meter is not None else nullcontext(True)
