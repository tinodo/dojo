"""Plan: Dojo proposes when to do what, inside the free times the learner states, and publishes the plan
as a private calendar feed (iCalendar, RFC 5545) that Outlook subscribes to by URL.
See docs/adr/0004-private-calendar-feed.md.

Dojo never reads or writes the learner's calendar. It plans only from the free times and limits set on the
Plan page. What a session asks for comes from the Record, and whether it was done is read from the Record
too: a session without a matching event is not done. The plan is the learner's own data
(learners/<key>/plan.json), not the Record: it is a proposal, never evidence, and nothing here writes an
event. Learning Orchestration proposes; the learner moves, skips or pins, and changes the rules (M-02)."""
from __future__ import annotations

import copy
import hashlib
import hmac
import logging
import math
import re
import secrets
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from fastapi import HTTPException

from .core import Learner, Store, UserError, canonical, iso, new_id, parse_iso, utcnow
from .deeplink import go_link
from .evidence import derive, due_later, suggestions
from .exam import spec as exam_spec
from .labs import LABS
from .podcast import find_by_token, members_of, qr_data_uri

log = logging.getLogger("dojo.plan")

# ---------------------------------------------------------------- Berlin time

TZID = "Europe/Berlin"
_CET, _CEST = timedelta(hours=1), timedelta(hours=2)


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def offset_at(when: datetime) -> timedelta:
    """The EU rule: summer time from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last
    Sunday of October."""
    u = when.astimezone(timezone.utc)
    start = datetime.combine(_last_sunday(u.year, 3), time(1), tzinfo=timezone.utc)
    end = datetime.combine(_last_sunday(u.year, 10), time(1), tzinfo=timezone.utc)
    return _CEST if start <= u < end else _CET


def to_local(when: datetime) -> datetime:
    """Berlin wall-clock time, without a zone."""
    u = when.astimezone(timezone.utc)
    return (u + offset_at(u)).replace(tzinfo=None)


def to_utc(local: datetime) -> datetime:
    """The moment a Berlin wall-clock time stands for. A time in the spring gap is read as winter time,
    and a time in the autumn overlap as the first of the two."""
    for off in (_CEST, _CET):
        u = (local - off).replace(tzinfo=timezone.utc)
        if to_local(u) == local:
            return u
    return (local - _CET).replace(tzinfo=timezone.utc)


def day_start(d: date) -> datetime:
    return to_utc(datetime.combine(d, time()))


def exists(local: datetime) -> bool:
    """False for a Berlin wall-clock time in the hour the clocks skip in spring."""
    return to_local(to_utc(local)) == local


def _local_str(when: datetime) -> str:
    return to_local(when).strftime("%Y-%m-%dT%H:%M")


def _short(when: datetime) -> str:
    loc = to_local(when)
    return f"{DAYS[loc.weekday()][:3]} {loc.day} {MONTHS[loc.month - 1]} {loc:%H:%M}"


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------- the learner's rules

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
STEP = 5  # minutes: sessions start on a five-minute mark and take whole five-minute blocks
WEEKDAYS = [0, 1, 2, 3, 4]
DEFAULT_RULES: dict[str, Any] = {
    "day_minutes": [60, 60, 60, 60, 60, 120, 0],
    "week_minutes": 420,
    "earliest": "08:00",
    "reminder": 10,
    "windows": [
        {"label": "Commute", "days": WEEKDAYS, "start": "08:30", "end": "09:00", "audio_only": True},
        {"label": "Lunch", "days": WEEKDAYS, "start": "12:30", "end": "13:30", "audio_only": False},
        {"label": "Evening", "days": WEEKDAYS, "start": "18:00", "end": "21:00", "audio_only": False},
        {"label": "Saturday morning", "days": [5], "start": "10:00", "end": "13:00", "audio_only": False},
    ],
}
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clock(value: Any) -> int:
    m = _HHMM.match(value) if isinstance(value, str) else None
    if not m:
        raise UserError(f"Times look like 08:30, and {str(value)[:10]!r} does not.")
    return int(m.group(1)) * 60 + int(m.group(2))


def _whole(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def check_rules(raw: Any) -> dict:
    """The rules as the learner set them, checked. Raises UserError with the reason in plain words."""
    if not isinstance(raw, dict):
        raise UserError("The plan rules could not be read.")
    day = raw.get("day_minutes")
    if not isinstance(day, list) or len(day) != 7 or not all(_whole(x, 0, 600) for x in day):
        raise UserError("Give the minutes for each day from Monday to Sunday, each from 0 to 600.")
    week = raw.get("week_minutes")
    if not _whole(week, 0, 4200):
        raise UserError("The weekly limit is between 0 and 4200 minutes (70 hours).")
    earliest = raw.get("earliest")
    _clock(earliest)
    reminder = raw.get("reminder")
    if not _whole(reminder, 0, 120):
        raise UserError("A reminder comes 0 to 120 minutes before a session.")
    wins = raw.get("windows")
    if not isinstance(wins, list) or len(wins) > 12:
        raise UserError("Give at most 12 free times.")
    out = []
    for w in wins:
        if not isinstance(w, dict):
            raise UserError("A free time could not be read.")
        days = w.get("days")
        if not isinstance(days, list) or not days or not all(_whole(d, 0, 6) for d in days):
            raise UserError("Each free time needs at least one day.")
        start, end = _clock(w.get("start")), _clock(w.get("end"))
        if end - start < STEP:
            raise UserError(f"A free time must end at least {STEP} minutes after it starts ({w.get('start')} to {w.get('end')}).")
        label = _CONTROL.sub("", str(w.get("label") or "")).strip()[:40]
        out.append({"label": label, "days": sorted(set(days)), "start": w["start"], "end": w["end"],
                    "audio_only": w.get("audio_only") is True})
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            if (set(a["days"]) & set(b["days"]) and _clock(a["start"]) < _clock(b["end"])
                    and _clock(b["start"]) < _clock(a["end"])):
                raise UserError(f"Two free times overlap on the same day: {a['label'] or a['start']} and "
                                f"{b['label'] or b['start']}. Make them one, or move one of them.")
    out.sort(key=lambda w: (w["days"][0], _clock(w["start"])))
    return {"day_minutes": list(day), "week_minutes": week, "earliest": earliest, "reminder": reminder, "windows": out}


def _dur(minutes: int) -> str:
    if minutes >= 120 and minutes % 60 == 0:
        return f"{minutes // 60} h"
    if minutes > 120:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"


def _days_text(days: list[int]) -> str:
    if days == WEEKDAYS:
        return "weekdays"
    if days == [5, 6]:
        return "weekends"
    if len(days) == 7:
        return "every day"
    return ", ".join(DAYS[d][:3] for d in days)


def rules_summary(rules: dict) -> str:
    """The rules in one line, for the Plan page."""
    day = rules["day_minutes"]
    parts = []
    if len(set(day[:5])) == 1:
        parts.append(f"at most {_dur(day[0])} on weekdays" if day[0] else "weekdays off")
    else:
        parts += [f"{_dur(m)} on {DAYS[i]}" if m else f"{DAYS[i]} off" for i, m in enumerate(day[:5])]
    parts += [f"{_dur(day[i])} on {DAYS[i]}" if day[i] else f"{DAYS[i]} off" for i in (5, 6)]
    parts.append(f"nothing before {rules['earliest']}")
    for w in rules["windows"]:
        if w["audio_only"]:
            name = f"the {w['start']} {w['label'].lower()}" if w["label"] else f"{_days_text(w['days'])} {w['start']} to {w['end']}"
            parts.append(f"{name} is audio only")
    parts.append(f"at most {_dur(rules['week_minutes'])} a week")
    return " · ".join(parts)


# ---------------------------------------------------------------- what can be planned

KINDS = {"listen": "Listen", "lesson": "Lesson", "practice": "Practise", "check": "Check", "later": "Later check",
         "check3": "3-minute check", "exam": "Practice exam", "lab": "Lab", "recall": "Recall"}
CHECKING = {"check", "later", "check3", "exam", "recall"}
DAILY = {"check3", "recall"}   # planned for one day at a time: a missed one is not carried over
SKILL_KINDS = {"listen", "lesson", "practice", "check", "later"}
LESSON_MIN, PRACTICE_MIN, CHECK_MIN, CHECK3_MIN = 15, 10, 5, 3
LAB_MIN = 20          # a lab: a few steps of SQL, then the check in the database
LAB_PACKAGE = "dp-800"   # the labs in app/labs.py are DP-800's
WORDS_PER_MIN = 150
HORIZON_DAYS = 14     # Dojo places sessions this far ahead
MAX_A_DAY = 6         # sessions a day at most, so a day in the calendar stays readable
FREEZE = timedelta(hours=24)   # a session this close keeps its time unless it is missed or no longer needed
REHEARSE_DAYS = 28    # one practice exam a week in the last four weeks before an exam
KEEP_DAYS = 35        # sessions that ended longer ago than this are forgotten
MOVES_KEEP = timedelta(days=14)
FEED_PAST = timedelta(days=7)
CHECK3_TOGETHER = timedelta(minutes=60)   # answers this close together are one 3-minute check
AUDIO_RECHECK_S = 600.0
MAX_AHEAD = timedelta(days=60)


@dataclass
class Alt:
    """One way to do a step: a lesson can be read at a desk or listened to on the way."""
    kind: str
    minutes: int
    audio: bool = False   # only in audio-only free times, and never outside them
    length: str | None = None


@dataclass
class Need:
    key: str
    package: str
    skill: str | None
    alts: list[Alt]
    lesson: str | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None
    in_order: bool = False   # try each alternative everywhere before the next one (a full exam before a short one)
    lab: str | None = None


def _ceil(minute: int) -> int:
    return -(-minute // STEP) * STEP


def _start(s: dict) -> datetime | None:
    return parse_iso(s["start"]) if s.get("start") else None


def shown_at(s: dict) -> datetime | None:
    """Where a session sits: where it was planned, or when it was done if that was earlier."""
    start = _start(s)
    done = parse_iso(s["done_at"]) if s.get("done_at") else None
    if done and (start is None or done < start):
        return done.replace(minute=done.minute - done.minute % STEP, second=0, microsecond=0)
    return start


def _day_parts(start: datetime, minutes: int) -> list[tuple[date, int]]:
    """The Berlin days a session is on, with its real minutes on each. One over midnight is on both days."""
    end, d = start + timedelta(minutes=minutes), to_local(start).date()
    parts = []
    while True:
        cut = min(end, day_start(d + timedelta(days=1)))
        parts.append((d, (cut - start) // timedelta(minutes=1)))
        if cut >= end:
            return parts
        start, d = cut, d + timedelta(days=1)


def _on_days(sessions: list[dict], first: date, days: int) -> list[tuple[date, int, dict]]:
    """(day, minutes, session) for the sessions' minutes on the `days` days from `first`, each on the day it falls on."""
    last = first + timedelta(days=days)
    return [(d, m, s) for s in sessions for d, m in _day_parts(shown_at(s), s["minutes"]) if first <= d < last]


class Board:
    """The free times of the next HORIZON_DAYS, what is already on them, and what is left of each day's
    and week's minutes."""

    def __init__(self, rules: dict, now: datetime):
        self.rules = rules
        self.now = now
        self.local_now = to_local(now)
        self.today = self.local_now.date()
        self.earliest = _clock(rules["earliest"])
        self.windows = sorted(((set(w["days"]), _clock(w["start"]), _clock(w["end"]), w["audio_only"]) for w in rules["windows"]),
                              key=lambda w: w[1])
        self.busy: dict[date, list[tuple[int, int]]] = {}
        self.count: dict[date, int] = {}
        self.day_used: dict[date, int] = {}
        self.week_used: dict[date, int] = {}

    def take(self, start: datetime, minutes: int) -> None:
        """Puts a session on the board. One over midnight is on both days: it keeps its time on each, and its
        minutes count on the day, and in the week, they fall in."""
        for d, part in _day_parts(start, minutes):
            self.busy.setdefault(d, []).append(self._keeps(d, start, minutes))
            self.count[d] = self.count.get(d, 0) + 1
            self.day_used[d] = self.day_used.get(d, 0) + part
            self.week_used[_monday(d)] = self.week_used.get(_monday(d), 0) + part

    @staticmethod
    def _minute(d: date, local: datetime) -> int:
        """A wall-clock time, in minutes from the start of day d."""
        return (local.date() - d).days * 1440 + local.hour * 60 + local.minute

    @classmethod
    def _keeps(cls, d: date, start: datetime, minutes: int) -> tuple[int, int]:
        """The wall-clock minutes of day d a session keeps from others: from its start to the first five-minute
        mark at or after the latest time it is on. That is its end, which over the spring change is an hour later
        on the wall clock than its length says. If the clocks go back on the way, it was on 03:00 just before they
        did. A time in the hour that repeats is its first moment, so the next session then starts at 03:00 at the
        earliest."""
        end = start + timedelta(minutes=minutes)
        last = cls._minute(d, to_local(end))
        if offset_at(end) < offset_at(start):
            # The clocks go back at 01:00 UTC (offset_at), from 03:00 on the wall clock.
            back = datetime.combine(end.astimezone(timezone.utc).date(), time(1)) + offset_at(start)
            last = max(last, cls._minute(d, back))
        return cls._minute(d, to_local(start)), _ceil(last)

    @staticmethod
    def _reach(d: date, minute: int) -> datetime:
        """The first moment on day d the wall clock shows `minute` (from the start of the day) or a later time. In
        spring the clocks jump from 02:00 to 03:00, so they reach 02:30 at 03:00. In autumn they go back from 03:00
        to 02:00, so they reach 02:30 the first time round, and 03:00 an hour after the change."""
        local = datetime.combine(d, time()) + timedelta(minutes=minute)
        if not exists(local):
            local = local.replace(minute=0) + timedelta(hours=1)
        return to_utc(local)

    def budget(self, d: date, minutes: int) -> bool:
        return (self.count.get(d, 0) < MAX_A_DAY
                and self.day_used.get(d, 0) + minutes <= self.rules["day_minutes"][d.weekday()]
                and self.week_used.get(_monday(d), 0) + minutes <= self.rules["week_minutes"])

    def fits(self, s: dict) -> bool:
        """True if a session lies inside a free time that takes its kind, after the earliest hour. A free time runs
        from the first moment the clock reaches its start to the first moment it reaches its end (_reach), and
        the session's moments are compared with those, so a clock change on the way is counted."""
        start = _start(s)
        end = start + timedelta(minutes=s["minutes"])
        loc = to_local(start)
        d = loc.date()
        if not self.rules["day_minutes"][d.weekday()] or loc.hour * 60 + loc.minute < self.earliest:
            return False
        audio = s["kind"] == "listen"
        return any(d.weekday() in days and a == audio and self._reach(d, w0) <= start and end <= self._reach(d, w1)
                   for days, w0, w1, a in self.windows)

    def find(self, need: Need, spread: bool = True) -> tuple[Alt, datetime] | None:
        """The first day with room, and on that day the least-filled free time, so a day's sessions spread
        over its free times. With spread=False, simply the next free slot."""
        for alts in ([[a] for a in need.alts] if need.in_order else [need.alts]):
            hit = self._find(need, alts, spread)
            if hit:
                return hit
        return None

    def _filled(self, d: date, w0: int, w1: int) -> int:
        return sum(max(0, min(b1, w1) - max(b0, w0)) for b0, b1 in self.busy.get(d, []))

    def _find(self, need: Need, alts: list[Alt], spread: bool) -> tuple[Alt, datetime] | None:
        nb = to_local(need.not_before) if need.not_before else None
        na = to_local(need.not_after) if need.not_after else None
        for i in range(HORIZON_DAYS):
            d = self.today + timedelta(days=i)
            if nb and d < nb.date():
                continue
            if na and d > na.date():
                break
            if not self.rules["day_minutes"][d.weekday()]:
                continue
            windows = [w for w in self.windows if d.weekday() in w[0]]
            if spread:
                windows.sort(key=lambda w: (self._filled(d, w[1], w[2]), w[1]))
            for _, w0, w1, audio in windows:
                alt = next((a for a in alts if a.audio == audio), None)
                if alt is None or not self.budget(d, alt.minutes):
                    continue
                low = max(w0, self.earliest)
                if d == self.today:
                    low = max(low, self.local_now.hour * 60 + self.local_now.minute + 1)
                if nb and d == nb.date():
                    low = max(low, nb.hour * 60 + nb.minute)
                close = self._reach(d, w1)
                if need.not_after:
                    close = min(close, need.not_after)
                at = self._gap(d, _ceil(low), alt.minutes, close)
                while at is not None:
                    start = to_utc(datetime.combine(d, time()) + timedelta(minutes=at))
                    if start > self.now and (not need.not_before or start >= need.not_before):
                        return alt, start
                    at = self._gap(d, at + STEP, alt.minutes, close)
        return None

    def _gap(self, d: date, at: int, minutes: int, close: datetime) -> int | None:
        """The first wall-clock time from `at` on day d, on a five-minute mark, where a session keeps clear of the
        ones there (_keeps) and ends by the moment `close`. A time the clocks skip in spring is no time at all,
        and one in the hour that repeats in autumn is its first moment. None if there is no such time."""
        busy = self.busy.get(d, [])
        while True:
            local = datetime.combine(d, time()) + timedelta(minutes=at)
            start = to_utc(local)
            if to_local(start) != local:
                at += STEP
                continue
            if start + timedelta(minutes=minutes) > close:
                return None
            m0, m1 = self._keeps(d, start, minutes)
            ends = [b1 for b0, b1 in busy if b0 < m1 and m0 < b1]
            if not ends:
                return at
            at = _ceil(max(ends))


# ---------------------------------------------------------------- done, read from the Record

def later_answers(pid: str, ev: dict[str, dict], later_hours: int) -> set[tuple[str, str]]:
    """The answers the evidence counts, made as later checks (evidence.derive), as (package, item): not disputed,
    without help, and judged at least later_hours after the skill's last teaching before it. These are the attempt
    fields evidence.summarize reads for its "later" check, except the result: like a check or a lab, a later check
    is done when it was sat, whether every point was met or not. The answer's own feedback comes after it, so it
    cannot undo it. An answer not judged yet is not one."""
    later = later_hours * 60
    return {(pid, a["item"]) for e in ev.values() for a in e["attempts"]
            if not a["disputed"] and a["unaided"]
            and a["minutes_since_teaching"] is not None and a["minutes_since_teaching"] >= later}


def _matches(s: dict, ev: dict, later: set[tuple[str, str]]) -> bool:
    d = ev.get("data") or {}
    if d.get("package") != s["package"]:
        return False
    t, kind = ev.get("type"), s["kind"]
    if kind in ("listen", "lesson"):
        return d.get("skill") == s["skill"] and (t == "lesson.viewed" or (t == "podcast.declared" and d.get("heard") is True))
    if kind == "practice":
        return t == "answer.submitted" and d.get("skill") == s["skill"]
    if kind in ("check", "later"):
        if t != "answer.submitted" or d.get("skill") != s["skill"] or d.get("mode") not in ("probe", "check"):
            return False
        # A later check is done only by an answer the evidence counts as one, judged by the teaching before
        # that answer. The time the session was planned for may be out of date by then.
        return kind == "check" or (s["package"], d.get("item")) in later
    if kind == "check3":
        return t == "answer.submitted" and d.get("mode") == "check" and d.get("recall") is not True
    if kind == "recall":
        # Only that day's recalls: answering several in one sitting must not tick off the next days too.
        return (t == "answer.submitted" and d.get("recall") is True
                and (not s.get("start") or to_local(parse_iso(s["start"])).date() == to_local(parse_iso(ev["at"])).date()))
    if kind == "exam":
        return t == "exam.started"
    if kind == "lab":
        return t == "lab.checked" and d.get("lab") == s.get("lab")
    return False


def mark_done(sessions: list[dict], events: list[dict], now: datetime, later: set[tuple[str, str]] | None = None) -> None:
    """A planned session is done when the Record holds a matching event from after the session was
    planned. Nothing else marks it done, and a missing event never does. Each event marks at most one
    session about a skill and one about a whole exam, the one planned earliest. `later` holds the answers
    the evidence counts as later checks (later_answers); only those make a later check done."""
    later = later or set()
    planned = [s for s in sessions if s["state"] == "planned"]
    if not planned:
        return
    since = min(parse_iso(s["created"]) for s in planned)
    used = {(s["done_event"], s["kind"] in SKILL_KINDS) for s in sessions if s.get("done_event")}
    check3_at = {s["package"]: parse_iso(s["done_at"]) for s in sorted(sessions, key=lambda s: s.get("done_at") or "")
                 if s["kind"] == "check3" and s.get("done_at")}
    for ev in events:
        at = parse_iso(ev["at"])
        if at < since or at > now:
            continue
        for skill_level in (True, False):
            if (ev.get("id"), skill_level) in used:
                continue
            found = [s for s in planned if s["state"] == "planned" and (s["kind"] in SKILL_KINDS) == skill_level
                     and at >= parse_iso(s["created"]) and _matches(s, ev, later)]
            if not found:
                continue
            first = min(found, key=lambda s: (s["start"] is None, s["start"] or ""))
            if first["kind"] == "check3":
                last = check3_at.get(first["package"])
                if last and timedelta(0) <= at - last < CHECK3_TOGETHER:
                    continue
                check3_at[first["package"]] = at
            first.update(state="done", done_at=ev["at"], done_event=ev.get("id"))
            used.add((ev.get("id"), skill_level))


# ---------------------------------------------------------------- the schedule

def _session(need: Need, alt: Alt, start: datetime, now: datetime) -> dict:
    return {"id": new_id("plan"), "key": need.key, "package": need.package, "kind": alt.kind, "skill": need.skill,
            "lesson": need.lesson, "lab": need.lab, "length": alt.length, "minutes": alt.minutes, "start": iso(start),
            "not_before": iso(need.not_before) if need.not_before else None, "created": iso(now), "changed": iso(now),
            "seq": 0, "state": "planned", "pinned": False, "by": "dojo", "done_at": None, "done_event": None}


def _moved(s: dict, old: str | None, why: str, now: datetime) -> dict:
    return {"session": s["id"], "package": s["package"], "kind": s["kind"], "skill": s["skill"], "length": s.get("length"),
            "from": old, "to": s["start"], "why": why, "at": iso(now)}


# Why a source change (ADR 0006) makes a planned session lead nowhere for now, as the Plan says it.
UNAVAILABLE = {"gone": "Its official pages are gone or moved", "rewriting": "This lesson is being written again"}


def unavailable(s: dict, off: dict[tuple[str, str], str] | None) -> str | None:
    """Why a source change makes a session lead nowhere for now, or None. `off` holds, per (exam, skill),
    "gone" when every page the skill cites is gone, moved included: nothing can be written for it, so
    every step of it but its lab leads nowhere (the database checks a lab, not a page). Or "rewriting"
    when the skill's newest lesson is held back and being written again: its Lesson and Listen."""
    why = (off or {}).get((s["package"], s.get("skill")))
    if why == "gone" and s["kind"] != "lab":
        return why
    if why == "rewriting" and s["kind"] in ("lesson", "listen"):
        return why
    return None


def _set_start(s: dict, start: datetime | None, now: datetime) -> bool:
    value = iso(start) if start else None
    if value == s["start"]:
        return False
    s["start"], s["seq"], s["changed"] = value, s["seq"] + 1, iso(now)
    return True


def reconcile(doc: dict, needs: list[Need], events: list[dict], now: datetime, active: dict[str, datetime | None],
              later: set[tuple[str, str]] | None = None, known: set[str] | None = None,
              off: dict[tuple[str, str], str] | None = None) -> None:
    """Brings the plan up to date with the Record, the rules and the clock.

    Done sessions are read from the Record first. A session that ended without being done moves to the
    next free slot, or waits if there is none. What the learner pinned or moved keeps its time. Sessions
    within FREEZE keep their time while they are still needed, still fit the rules and are not under a
    session the learner put there, and a later check whose skill was worked on since moves to when it
    comes due. Everything later is placed again, keeping its id and so its calendar event, and what is
    no longer needed is removed.
    `active` holds the exams still ahead, each with the start of its exam day if it has one: Dojo plans
    nothing of an exam on that day or after it. `later` holds the answers the evidence counts as later
    checks (later_answers). `known` holds the exams Dojo has: the planned sessions of one that was
    removed go, pinned or not, while what was done stays. `off` holds what a source change made lead
    nowhere for now (`unavailable`, ADR 0006): such a planned session goes too, pinned or not, and "Moved
    for you" says why."""
    rules, sessions = doc["rules"], doc["sessions"]
    cutoff = now - timedelta(days=KEEP_DAYS)
    sessions[:] = [s for s in sessions if s["state"] == "planned" or (shown_at(s) or parse_iso(s["changed"])) >= cutoff]
    mark_done(sessions, events, now, later)
    keys = {n.key for n in needs}
    by_key = {n.key: n for n in needs}
    check3 = {n.package for n in needs if n.alts[0].kind == "check3"}
    for s in sessions:
        need = by_key.get(s["key"])
        if s["state"] == "planned" and s["kind"] == "later" and need and need.not_before:
            # Work on the skill since the check was planned restarts its clock, so it comes due later.
            s["not_before"] = iso(need.not_before)

    def wanted(s: dict) -> bool:
        end = active.get(s["package"])
        if end and s["start"] and _start(s) >= end:
            return False
        return s["key"] in keys or (s["kind"] == "check3" and s["package"] in check3)

    board = Board(rules, now)
    today = board.today
    kept: list[dict] = []
    for s in sessions:
        if s["state"] == "done" and shown_at(s):
            board.take(shown_at(s), s["minutes"])
            kept.append(s)
    late, fixed, again = [], [], {}
    for s in sessions:
        if s["state"] != "planned":
            continue
        start, own = _start(s), s["pinned"] or s["by"] == "you"
        nowhere = unavailable(s, off)
        if known is not None and s["package"] not in known:
            s["state"] = "dropped"
        elif nowhere:
            s.update(state="dropped", dropped_reason=nowhere)
            doc["moves"].append({**_moved(s, s["start"], "removed", now), "to": None, "key": s["key"], "reason": nowhere})
        elif start is None or start + timedelta(minutes=s["minutes"]) <= now:
            # A 3-minute check is planned every day, so a missed one is not carried over onto the next.
            if s["package"] in active and (own or (wanted(s) and s["kind"] not in DAILY)):
                late.append(s)
            else:
                s["state"] = "dropped"
        elif own or (start < now + FREEZE and wanted(s)):
            fixed.append(s)
        elif wanted(s):
            again.setdefault(s["key"], []).append(s)
        else:
            s["state"] = "dropped"
    fixed.sort(key=lambda s: (not (s["pinned"] or s["by"] == "you"), s["start"]))
    # A later check whose time has passed because its skill was worked on since was not missed: it came due later.
    floating = [(s, "due" if s["kind"] == "later" and s["start"] and s.get("not_before") and _start(s) < parse_iso(s["not_before"])
                 else "missed") for s in sorted(late, key=lambda s: s["start"] or s["created"])]
    mine: list[tuple[datetime, datetime]] = []   # the learner's sessions, which come first in `fixed`
    for s in fixed:
        start, own = _start(s), s["pinned"] or s["by"] == "you"
        end = start + timedelta(minutes=s["minutes"])
        if not own and s.get("not_before") and start < parse_iso(s["not_before"]):
            floating.append((s, "due"))
        elif not own and any(a < end and start < b for a, b in mine):
            floating.append((s, "taken"))
        elif own or (board.fits(s) and board.budget(to_local(start).date(), s["minutes"])):
            board.take(start, s["minutes"])
            kept.append(s)
            if own:
                mine.append((start, end))
        else:
            floating.append((s, "rules"))

    covered: set[str] = set()
    days: set[tuple[str, date]] = set()
    weeks: set[tuple[str, date]] = set()

    def cover(s: dict) -> None:
        at = shown_at(s)
        if s["state"] == "planned" or (at and to_local(at).date() == today):
            covered.add(s["key"])
        if at and s["kind"] in DAILY:
            days.add((s["kind"], s["package"], to_local(at).date()))
        if at and s["kind"] == "exam":
            weeks.add((s["package"], _monday(to_local(at).date())))

    for s in kept:
        cover(s)
    for s, why in floating:
        # A step still asked for is placed by what it asks for now: a Listen whose audio-only time is gone
        # becomes a Lesson at a desk. A session only the learner wanted keeps its own kind and length.
        need = by_key.get(s["key"])
        own_alt = Alt(s["kind"], s["minutes"], audio=s["kind"] == "listen", length=s.get("length"))
        ends = [x for x in (need.not_after if need and s["kind"] == "exam" else None, active.get(s["package"])) if x]
        hit = board.find(Need(s["key"], s["package"], s["skill"], need.alts if need else [own_alt],
                              not_before=parse_iso(s["not_before"]) if s.get("not_before") else None,
                              not_after=min(ends) if ends else None,
                              in_order=bool(need and need.in_order)), spread=False)
        old = s["start"]
        if hit and s["kind"] in DAILY and (s["kind"], s["package"], to_local(hit[1]).date()) in days:
            s["state"] = "dropped"   # that day has its 3-minute check already
            continue
        if hit:
            alt = hit[0]
            board.take(hit[1], alt.minutes)
            lesson = need.lesson if need else s.get("lesson")
            if (s["kind"], s["minutes"], s.get("length"), s.get("lesson")) != (alt.kind, alt.minutes, alt.length, lesson):
                s.update(kind=alt.kind, minutes=alt.minutes, length=alt.length, lesson=lesson, seq=s["seq"] + 1, changed=iso(now))
            if s["kind"] in DAILY:
                s["key"] = f"{s['kind']}:{s['package']}:{to_local(hit[1]).date().isoformat()}"   # it is that day's session now
        if _set_start(s, hit[1] if hit else None, now):
            doc["moves"].append(_moved(s, old, why, now))
        s["pinned"], s["by"] = False, "dojo"
        cover(s)

    unplaced = 0
    for need in needs:
        kind = need.alts[0].kind
        if need.key in covered:
            continue
        if kind in DAILY and (kind, need.package, to_local(need.not_before).date()) in days:
            continue
        if kind == "exam" and (need.package, _monday(to_local(need.not_before).date())) in weeks:
            continue
        hit = board.find(need)
        if not hit:
            unplaced += kind not in DAILY and any(not a.audio for a in need.alts)
            continue
        alt, start = hit
        board.take(start, alt.minutes)
        pool = again.get(need.key)
        if pool:
            s = pool.pop(0)
            if (s["kind"], s["minutes"], s.get("length"), s.get("lesson")) != (alt.kind, alt.minutes, alt.length, need.lesson):
                s.update(kind=alt.kind, minutes=alt.minutes, length=alt.length, lesson=need.lesson, seq=s["seq"] + 1, changed=iso(now))
            _set_start(s, start, now)
        else:
            s = _session(need, alt, start, now)
            sessions.append(s)
        cover(s)
    for pool in again.values():
        for s in pool:
            s["state"] = "dropped"
    sessions[:] = sorted((s for s in sessions if s["state"] != "dropped"), key=lambda s: (s["start"] is None, s["start"] or "", s["id"]))
    doc["moves"] = [m for m in doc["moves"] if parse_iso(m["at"]) >= now - MOVES_KEEP][-40:]
    doc["unplaced"] = unplaced


# ---------------------------------------------------------------- the calendar feed (RFC 5545)

_NOT_TEXT = re.compile("[\x00-\x08\x0b-\x1f\x7f]")


def ics_text(value: Any) -> str:
    """A TEXT value (RFC 5545 3.3.11): backslash, semicolon, comma and line breaks escaped, other
    control characters removed."""
    v = _NOT_TEXT.sub("", str(value).replace("\r\n", "\n").replace("\r", "\n"))
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Folds a content line after at most 75 octets, never inside a UTF-8 character (RFC 5545 3.1)."""
    out, cur, size, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append("".join(cur))
            cur, size, limit = [], 0, 74   # a continuation line starts with one space
        cur.append(ch)
        size += n
    out.append("".join(cur))
    return "\r\n ".join(out)


def _utc_stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_ics(events: list[dict], now: datetime) -> bytes:
    """events: {uid, start, minutes, summary, description, url, seq, created, changed, alarm (minutes or None)}.
    Start and end are written as moments in UTC (RFC 5545 3.3.5, form 2), and calendar apps show them in
    their own time zone. A Berlin wall-clock time would be ambiguous in the hour that repeats in autumn."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Dojo//Plan//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:Dojo plan",
             "X-WR-CALDESC:" + ics_text("Study sessions planned by Dojo in the free times you set. Change them on Dojo's Plan page."),
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{_utc_stamp(now)}",
                  f"DTSTART:{_utc_stamp(e['start'])}",
                  f"DTEND:{_utc_stamp(e['start'] + timedelta(minutes=e['minutes']))}",
                  "SUMMARY:" + ics_text(e["summary"]), "DESCRIPTION:" + ics_text(e["description"]), "URL:" + e["url"],
                  f"SEQUENCE:{e['seq']}", f"CREATED:{_utc_stamp(e['created'])}", f"LAST-MODIFIED:{_utc_stamp(e['changed'])}",
                  "STATUS:CONFIRMED", "TRANSP:OPAQUE", "CLASS:PRIVATE"]
        if e["alarm"] is not None:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:" + ics_text(e["summary"]),
                      f"TRIGGER:-PT{e['alarm']}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold(line) for line in lines) + "\r\n").encode("utf-8")


# ---------------------------------------------------------------- the secret link

TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_NO_LINK = hashlib.sha256(b"dojo-cal:no link").hexdigest()
FETCH_NOTE = timedelta(minutes=5)


def token_hash(token: str) -> str:
    return hashlib.sha256(("dojo-cal:" + token).encode("utf-8")).hexdigest()


class CalendarLinks:
    """Each learner's one calendar link, the same design as the podcast link (podcast.FeedLinks). Only a
    SHA-256 of its token is stored, with when it was made and when a calendar app last fetched it. A token
    is looked up among the owner (always) and the active members (ADR 0009)."""

    def __init__(self, store: Store, owner: Learner):
        self.store = store
        self.owner = owner
        self.members: Callable[[], list[Learner]] = lambda: []

    def _doc_of(self, key: str) -> dict | None:
        try:
            doc = self.store.read("learners", key, "calendar")
        except ValueError:
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("hash"), str) else None

    def _doc(self, learner: Learner | None = None) -> dict | None:
        return self._doc_of((learner or self.owner).key)

    def status(self, learner: Learner | None = None) -> dict:
        doc = self._doc(learner)
        return {"active": doc is not None, "created": doc.get("created") if doc else None,
                "fetched": doc.get("fetched") if doc else None}

    def create(self, replace: bool = False, learner: Learner | None = None) -> str:
        """A new token. It is returned once and never stored; making a new one ends the old one at once."""
        learner = learner or self.owner
        with self.store.lock:
            if self._doc(learner) and not replace:
                raise HTTPException(409, "You already have a calendar link. Dojo cannot show it again; make a new link instead.")
            token = secrets.token_urlsafe(32)
            self.store.write("learners", learner.key, "calendar", value={"hash": token_hash(token), "created": iso(), "fetched": None})
        return token

    def delete(self, learner: Learner | None = None) -> None:
        """Turns the feed off: every link answers 404 at once, and the calendar app shows no new sessions."""
        self.store.delete("learners", (learner or self.owner).key, "calendar")

    def learner_for(self, token: str) -> Learner | None:
        """Whose link this token is: the owner's or an active member's, else None. The hashes are compared
        in constant time, every candidate every time, and a hash is compared even when there is no link."""
        well_formed = bool(TOKEN.match(token or ""))
        return find_by_token(token_hash(token if well_formed else ""), well_formed,
                             [self.owner] + members_of(self.members), self._doc_of, _NO_LINK)

    owner_for = learner_for

    def fetched(self, token: str, now: datetime | None = None, learner: Learner | None = None) -> None:
        """Notes when a calendar app last fetched the feed: a time and nothing else, at most every few minutes."""
        now = now or utcnow()
        learner = learner or self.owner
        with self.store.lock:
            doc = self._doc(learner)
            if not doc or not hmac.compare_digest(token_hash(token), doc["hash"]):
                return
            if doc.get("fetched") and now - parse_iso(doc["fetched"]) < FETCH_NOTE:
                return
            self.store.write("learners", learner.key, "calendar", value={**doc, "fetched": iso(now)})


# ---------------------------------------------------------------- the planner

class Planner:
    """The study plan (ADR 0004): places each exam's next steps into the learner's free times and limits,
    reads from the Record what was done, moves what was missed, and serves the private calendar feed."""
    def __init__(self, dojo: Any, owner: Learner):
        self.dojo = dojo
        self.store: Store = dojo.store
        self.links = CalendarLinks(dojo.store, owner)
        self.labs_on = True                        # False in Azure without a lab database: no lab sessions
        self._listen: dict[str, int] = {}          # lessons never change once written
        self._unheard: dict[str, float] = {}       # lessons without complete audio, and when that was seen

    # ------------------------------------------------------------ what the Record asks for

    def listen_minutes(self, lesson_id: str) -> int | None:
        """How long listening to a lesson takes, once every slide is narrated; None before that."""
        if lesson_id in self._listen:
            return self._listen[lesson_id]
        seen = self._unheard.get(lesson_id)
        if seen is not None and clock.monotonic() - seen < AUDIO_RECHECK_S:
            return None
        try:
            doc = self.dojo.lesson(lesson_id)
        except HTTPException:
            return None
        slides = doc.get("slides") or []
        if not slides or not all(self.dojo.speech.exists(m) for m in self.dojo.slide_media(doc)):
            self._unheard[lesson_id] = clock.monotonic()
            return None
        words = 0
        for sl in slides:
            lines = sl.get("narration") or [{"text": ". ".join([sl.get("title", "")] + list(sl.get("bullets", [])))}]
            words += sum(len(str(line.get("text", "")).split()) for line in lines)
        self._listen[lesson_id] = max(STEP, math.ceil(words / WORDS_PER_MIN))
        return self._listen[lesson_id]

    def needs(self, profile: dict, events: list[dict], now: datetime, rules: dict, sessions: list[dict] = ()
              ) -> tuple[list[Need], dict[str, datetime | None], set[tuple[str, str]], dict[tuple[str, str], str]]:
        """Everything worth planning, most important first; the exams still ahead with the start of each
        one's exam day, as nothing of an exam is planned on its exam day or after it; the answers the
        evidence counts as later checks; and, per (exam, skill), what a source change makes lead nowhere
        for now (`unavailable`, ADR 0006): none of it is planned, and reconcile removes what was."""
        from . import relearn   # relearn reads the Plan's zone from here
        today = to_local(now).date()
        audio = any(w["audio_only"] for w in rules["windows"])
        latest: dict[tuple[str, str], dict] = {}
        for m in self.dojo.lesson_index():
            k = (m.get("package"), m.get("skill"))
            if k not in latest or (m.get("created") or "") > (latest[k].get("created") or ""):
                latest[k] = m
        exams: list[Need] = []
        later: list[Need] = []
        daily: list[Need] = []
        labs: list[Need] = []
        teach: dict[str, tuple[list[Need], float]] = {}
        active: dict[str, datetime | None] = {}
        answers: set[tuple[str, str]] = set()
        off: dict[tuple[str, str], str] = {}
        recalls: list[tuple[date, float, tuple[str, str, date | None]]] = []   # (due, -share, what)
        drift = getattr(self.dojo, "drift", None)
        enrolled = profile.get("enrolled")
        for pid, pkg in self.dojo.packages.by_id.items():
            if enrolled is not None and pid not in enrolled:
                continue   # nothing is planned for an exam this learner does not study (ADR 0009)
            exam_day = _day(profile["exam_dates"].get(pid))
            ev = derive(events, pkg, profile["later_hours"])
            answers |= later_answers(pid, ev, profile["later_hours"])
            # Dojo does not teach or ask about an added exam's skill with no verified source (ADR 0005), so
            # nothing is planned for it: no later check, and no 3-minute check that would have nothing to ask.
            ev = {sid: e for sid, e in ev.items() if pkg["skills"][sid]["sources"]}
            gone = drift.gone_skills(pid) if drift else set()
            for sid in pkg["skills"]:
                lesson = latest.get((pid, sid))
                if sid in gone:
                    off[(pid, sid)] = "gone"
                elif lesson and drift and drift.withheld(lesson["id"]):
                    off[(pid, sid)] = "rewriting"
            if exam_day and exam_day <= today:
                continue
            end = active[pid] = day_start(exam_day) if exam_day else None
            exams += self._exam_needs(pid, pkg, exam_day, events, today)
            lab = self._lab_need(pid, ev, events, today)
            if lab:
                lab.not_after = end
                labs.append(lab)
            # A skill whose every cited page is gone is left out too, while that lasts (ADR 0006): nothing
            # new can be written for it. Its lab, planned above, stays: the database checks a lab, not a page.
            ev = {sid: e for sid, e in ev.items() if sid not in gone}
            recalls += [(due, neg_share, (pid, sid, exam_day)) for due, neg_share, sid in relearn.upcoming(pkg, ev, gone)]
            for d in due_later(pkg, ev, now, profile["later_hours"]):
                later.append(Need(f"later:{pid}:{d['skill']}", pid, d["skill"], [Alt("later", CHECK_MIN)],
                                  not_before=parse_iso(d["due"]), not_after=end))
            if any(not e["attempts"] and not (e["checks"]["unaided"] or e["checks"]["later"]) for e in ev.values()):
                for i in range(HORIZON_DAYS):
                    d = today + timedelta(days=i)
                    if exam_day and d >= exam_day:
                        break
                    daily.append(Need(f"check3:{pid}:{d.isoformat()}", pid, None, [Alt("check3", CHECK3_MIN)],
                                      not_before=day_start(d), not_after=day_start(d + timedelta(days=1))))
            own = []
            for s in suggestions(pkg, ev, now, profile["later_hours"], limit=len(pkg["skills"])):
                sid, action, e = s["skill"], s["action"], ev[s["skill"]]
                if e["checks"]["unaided"] and not e["checks"]["later"]:
                    continue   # a later check, planned from when it comes due
                lesson = latest.get((pid, sid))
                if action in ("lesson", "probe") and off.get((pid, sid)) == "rewriting":
                    # Held back and out of the podcast feed while it is written again (ADR 0006). The new
                    # lesson is planned once it is there; practice and checks stay planned meanwhile.
                    continue
                minutes = self.listen_minutes(lesson["id"]) if lesson and audio else None
                listen = [Alt("listen", minutes, audio=True)] if minutes else []
                if action == "lesson":
                    own.append(Need(f"teach:{pid}:{sid}", pid, sid, [Alt("lesson", LESSON_MIN)] + listen,
                                    lesson=lesson["id"] if lesson else None))
                elif action == "probe" and listen and not e["exposures"]:
                    own.append(Need(f"teach:{pid}:{sid}", pid, sid, listen, lesson=lesson["id"]))
                elif action == "practice":
                    own.append(Need(f"practice:{pid}:{sid}", pid, sid, [Alt("practice", PRACTICE_MIN)]))
                elif action == "check":
                    own.append(Need(f"check:{pid}:{sid}", pid, sid, [Alt("check", CHECK_MIN)]))
            for n in own:
                n.not_after = end
            to_show = sum(1 for e in ev.values() if not e["checks"]["unaided"]) + sum(1 for e in ev.values() if not e["checks"]["later"])
            weeks = max(1.0, (exam_day - today).days / 7) if exam_day else 26.0
            teach[pid] = (own, max(0.01, to_show / weeks))
        later.sort(key=lambda n: (n.not_before, n.package, n.skill or ""))
        daily.sort(key=lambda n: (n.not_before, n.package))
        return exams + self._recall_needs(recalls, events, today, sessions) + later + daily + labs + _fair(teach), active, answers, off

    @staticmethod
    def _recall_needs(recalls: list[tuple[date, float, tuple[str, str, date | None]]], events: list[dict], today: date,
                      sessions: list[dict] = ()) -> list[Need]:
        """One short Recall session per exam and day with recalls due (ADR 0011), as long as Today says for
        the same recalls (relearn.minutes). At most DAY_CAP a day across all exams, less what today's checks
        already took; what does not fit waits for the next day, so a backlog spreads over a few short days.
        Overdue is simply due today. The order is Today's: oldest due day first, then the heaviest in the
        blueprint (`recalls` carries -share). A recall that can no longer come before its exam day is dropped
        before the places are handed out, so it never pushes another exam's recall later."""
        from . import relearn   # relearn reads the Plan's zone from here

        ordered = sorted(recalls, key=lambda r: (r[0], r[1], r[2][0], r[2][1]))
        # spread() keeps this order among recalls due the same day (its sort is stable).
        due = [(max(d, today), (rank, x)) for rank, (d, _, x) in enumerate(ordered)]
        days = relearn.spread(due, today, HORIZON_DAYS, relearn.left_today(events, today, sessions),
                              before=lambda r: r[1][2])
        out = []
        for d, picked in sorted(days.items()):
            count: dict[str, int] = {}
            for _, (pid, _sid, _exam_day) in picked:
                count[pid] = count.get(pid, 0) + 1
            for pid, n in sorted(count.items()):
                out.append(Need(f"recall:{pid}:{d.isoformat()}", pid, None,
                                [Alt("recall", relearn.minutes(n))],
                                not_before=day_start(d), not_after=day_start(d + timedelta(days=1))))
        return out

    def _lab_need(self, pid: str, ev: dict, events: list[dict], today: date) -> Need | None:
        """One lab a week, the first not yet passed. A lab is done at a desk, never in an audio-only time.
        A lab counts as teaching, so Dojo skips a lab whose skill waits for its later check: the lab would
        restart that check's clock. A lab checked this week, passed or not, is this week's lab."""
        if pid != LAB_PACKAGE or not self.labs_on:
            return None
        checked = [e for e in events if e.get("type") == "lab.checked" and (e.get("data") or {}).get("package") == pid]
        passed = {e["data"].get("lab") for e in checked if e["data"].get("passed") is True}
        monday = _monday(today)
        this_week = any(to_local(parse_iso(e["at"])).date() >= monday for e in checked)
        for lab, spec in LABS.items():
            e = ev.get(spec["skill"])
            if lab in passed or e is None or (e["checks"]["unaided"] and not e["checks"]["later"]):
                continue
            return Need(f"lab:{pid}:{lab}", pid, spec["skill"], [Alt("lab", LAB_MIN)], lab=lab,
                        not_before=day_start(monday + timedelta(days=7)) if this_week else None)
        return None

    def _exam_needs(self, pid: str, pkg: dict, exam_day: date | None, events: list[dict], today: date) -> list[Need]:
        """One practice exam a week in the last REHEARSE_DAYS before the exam, full length where it fits."""
        if not exam_day:
            return []
        try:
            full, short = exam_spec(pkg, "full")["minutes"], exam_spec(pkg, "short")["minutes"]
        except UserError:
            return []
        started = {_monday(to_local(parse_iso(e["at"])).date()) for e in events
                   if e.get("type") == "exam.started" and (e.get("data") or {}).get("package") == pid}
        first = exam_day - timedelta(days=REHEARSE_DAYS)
        out = []
        monday = _monday(today)
        while monday < today + timedelta(days=HORIZON_DAYS):
            low, high = max(monday, first, today), min(monday + timedelta(days=7), exam_day)
            if low < high and monday not in started:
                out.append(Need(f"exam:{pid}:{monday.isoformat()}", pid, None,
                                [Alt("exam", full, length="full"), Alt("exam", short, length="short")],
                                not_before=day_start(low), not_after=day_start(high), in_order=True))
            monday += timedelta(days=7)
        return out

    # ------------------------------------------------------------ keeping the plan current

    def _load(self, learner: Learner, doc: Any = None) -> dict:
        doc = doc if doc is not None else self.store.read("learners", learner.key, "plan")
        doc = doc if isinstance(doc, dict) else {}
        try:
            rules = check_rules(doc.get("rules"))
        except UserError:
            rules = copy.deepcopy(DEFAULT_RULES)
        return {"rules": rules, "sessions": [s for s in doc.get("sessions") or [] if isinstance(s, dict)],
                "moves": [m for m in doc.get("moves") or [] if isinstance(m, dict)],
                "skips": {k: v for k, v in (doc.get("skips") or {}).items() if isinstance(v, str)},
                "unplaced": int(doc.get("unplaced") or 0)}

    def refresh(self, learner: Learner, now: datetime | None = None) -> dict:
        """Reads the Record and brings the plan up to date. Writes plan.json only when something changed."""
        now = now or utcnow()
        with self.store.lock:
            stored = self.store.read("learners", learner.key, "plan")
            doc = self._load(learner, copy.deepcopy(stored) if stored is not None else {})
            doc["skips"] = {k: v for k, v in doc["skips"].items() if parse_iso(v) > now}
            events = self.store.events(learner.key)
            profile = self.dojo.profile(learner)
            known = set(self.dojo.packages.by_id)
            if profile.get("enrolled") is not None:
                known &= set(profile["enrolled"])
            from . import relearn   # relearn reads the Plan's zone from here
            sessions = relearn.check_sessions(self.store, learner.key)
            needs, active, later, off = self.needs(profile, events, now, doc["rules"], sessions)
            reconcile(doc, [n for n in needs if n.key not in doc["skips"]], events, now, active, later, known, off)
            if stored is None or canonical(doc) != canonical(stored):
                self.store.write("learners", learner.key, "plan", value=doc)
        return doc

    def _session_of(self, doc: dict, session_id: str) -> dict:
        for s in doc["sessions"]:
            if s["id"] == session_id:
                return s
        raise HTTPException(404, "That session is no longer in your plan.")

    def move(self, learner: Learner, session_id: str, start: str, now: datetime | None = None) -> None:
        """The learner moves a session. It is pinned there: Dojo keeps it unless it is missed, or it leads
        nowhere now (reconcile: its exam was removed, or a source change took what it needs)."""
        now = now or utcnow()
        try:
            local = datetime.strptime(start, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise UserError("Give the new time as a date and a time, like 2026-10-02T18:30.")
        if not exists(local):
            raise UserError("That time does not exist in Berlin: the clocks go forward an hour that night. Pick another time.")
        at = to_utc(local)
        if at <= now:
            raise UserError("Pick a time that is still to come.")
        if at > now + MAX_AHEAD:
            raise UserError("Dojo plans up to two months ahead. Pick an earlier time.")
        with self.store.lock:
            doc = self.refresh(learner, now)
            s = self._session_of(doc, session_id)
            if s["state"] != "planned":
                raise HTTPException(409, "Only a planned session can be moved.")
            old = s["start"]
            _set_start(s, at, now)
            s["pinned"], s["by"] = True, "you"
            doc["moves"].append(_moved(s, old, "you", now))
            self.store.write("learners", learner.key, "plan", value=doc)
        self.refresh(learner, now)

    def skip(self, learner: Learner, session_id: str, now: datetime | None = None) -> None:
        """Skipping removes the session from the calendar. Dojo does not plan that step again today."""
        now = now or utcnow()
        with self.store.lock:
            doc = self.refresh(learner, now)
            s = self._session_of(doc, session_id)
            if s["state"] != "planned":
                raise HTTPException(409, "Only a planned session can be skipped.")
            s.update(state="skipped", pinned=False, changed=iso(now), seq=s["seq"] + 1)
            day = max(to_local(now).date(), to_local(_start(s)).date() if s["start"] else to_local(now).date())
            doc["skips"][s["key"]] = iso(day_start(day + timedelta(days=1)))
            self.store.write("learners", learner.key, "plan", value=doc)
        self.refresh(learner, now)

    def pin(self, learner: Learner, session_id: str, pinned: bool, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self.store.lock:
            doc = self.refresh(learner, now)
            s = self._session_of(doc, session_id)
            if s["state"] != "planned" or not s["start"]:
                raise HTTPException(409, "Only a session with a time can be pinned.")
            s["pinned"] = bool(pinned)
            if not pinned:
                s["by"] = "dojo"
            s["changed"] = iso(now)
            self.store.write("learners", learner.key, "plan", value=doc)
        self.refresh(learner, now)

    def set_rules(self, learner: Learner, raw: dict, now: datetime | None = None) -> None:
        rules = check_rules(raw)
        with self.store.lock:
            doc = self._load(learner)
            doc["rules"] = rules
            self.store.write("learners", learner.key, "plan", value=doc)
        self.refresh(learner, now)

    # ------------------------------------------------------------ views

    def route(self, s: dict) -> str:
        """Where a session's link opens Dojo, as a page route: exactly that activity."""
        if s["kind"] == "listen" and s.get("lesson"):
            return f"lesson/{s['lesson']}/listen"
        if s["kind"] == "lesson" and s.get("lesson"):
            return f"lesson/{s['lesson']}"
        if s["kind"] == "check3":
            return f"check/for/{s['package']}"
        if s["kind"] == "recall":
            return f"recall/for/{s['package']}"
        if s["kind"] == "exam":
            return f"rehearsal/for/{s['package']}"
        if s["kind"] == "lab" and s.get("lab") in LABS:
            return f"lab/{s['lab']}"
        return f"skill/{s['package']}/{s['skill']}"

    def link(self, s: dict) -> str:
        """The same place as a link inside Dojo, for the Plan page. A link opened later, such as a calendar
        event's, is go_link(self.route(s), base) instead: Easy Auth loses a "#" route when it asks to sign in."""
        return f"#/{self.route(s)}"

    def _names(self, s: dict) -> tuple[str, str, str]:
        """The exam, the activity and what it is about: the only words a calendar event carries."""
        pkg = self.dojo.packages.by_id.get(s["package"])
        exam = pkg["meta"]["exam"] if pkg else s["package"].upper()
        label = KINDS.get(s["kind"], s["kind"])
        if s["kind"] == "exam":
            label += " (short)" if s.get("length") == "short" else " (full length)"
        skill = pkg["skills"].get(s["skill"]) if pkg and s.get("skill") else None
        return exam, label, skill["text"] if skill else ""

    def _public(self, s: dict, now: datetime) -> dict:
        exam, label, title = self._names(s)
        at, start = shown_at(s), _start(s)
        end = at + timedelta(minutes=s["minutes"]) if at else None
        state = s["state"]
        if state == "planned":
            state = "waiting" if start is None else ("now" if start <= now < end else "planned")
        return {"id": s["id"], "package": s["package"], "exam": exam, "kind": s["kind"], "label": label, "title": title,
                "skill": s.get("skill"), "minutes": s["minutes"], "at": _local_str(at) if at else None,
                "end": _local_str(end) if end else None, "state": state, "pinned": s["pinned"], "by": s["by"],
                "link": self.link(s), "group": "check" if s["kind"] in CHECKING else "teach"}

    def _move_text(self, m: dict, s: dict) -> dict:
        """One line of "Moved for you", about where the session is now: it may have moved again or been done.
        A session a source change removed is gone from the plan: its line is read from the move itself."""
        exam, label, title = self._names(s)
        was = _short(parse_iso(m["from"])) if m.get("from") else None
        if m["why"] == "removed":
            return {"session": m["session"], "exam": exam, "label": label, "title": title, "removed": True,
                    "why": UNAVAILABLE.get(m.get("reason"), "It leads nowhere for now"), "from": was, "to": "Removed"}
        at = shown_at(s)
        if s["state"] == "done":
            to = f"Done, {_short(at)}" if at else "Done"
        else:
            to = _short(at) if at else "No free slot yet: it waits for one."
        if m["why"] == "rules":
            why = "No longer fits the times you set"
        elif m["why"] == "taken":
            why = "You put another session at that time"
        elif m["why"] == "due":
            why = "You worked on the skill since, so it comes due later"
        elif was:
            why = "Not done at the planned time"
        else:
            why = "Was waiting for a free slot"
        return {"session": m["session"], "exam": exam, "label": label, "title": title, "why": why, "from": was, "to": to}

    def _moved_list(self, doc: dict, now: datetime) -> list[dict]:
        """Dojo's moves of the last 7 days, newest first, one per session that is still in the plan. A session
        the learner moved or skipped afterwards is the learner's, and no longer listed. A session a source
        change removed (ADR 0006) is listed too, with why, though it has left the plan, until its step is
        planned again."""
        live = {s["id"]: s for s in doc["sessions"]}
        back = {s["key"] for s in doc["sessions"] if s["state"] == "planned"}
        out, seen = [], set()
        for m in reversed(doc["moves"]):
            if m["session"] in seen or parse_iso(m["at"]) < now - timedelta(days=7):
                continue
            seen.add(m["session"])
            if m["why"] == "removed":
                if m.get("key") not in back:
                    out.append(self._move_text(m, m))
                continue
            s = live.get(m["session"])
            if m["why"] != "you" and s and s["state"] in ("planned", "done"):
                out.append(self._move_text(m, s))
        return out

    def view(self, learner: Learner, week: str | None = None, now: datetime | None = None) -> dict:
        now = now or utcnow()
        doc = self.refresh(learner, now)
        rules = doc["rules"]
        today = to_local(now).date()
        try:
            monday = _monday(date.fromisoformat(week) if week else today)
        except ValueError:
            raise UserError("Weeks look like 2026-10-05.")
        if abs((monday - _monday(today)).days) > 26 * 7:
            raise UserError("The plan shows half a year back and ahead at most.")
        sessions = [s for s in doc["sessions"] if s["state"] != "skipped" and shown_at(s)]
        in_week = [s for s in sessions if monday <= to_local(shown_at(s)).date() < monday + timedelta(days=7)]
        # A session is listed on the day it starts, but its minutes count on the days they fall on, as in the limits.
        on = _on_days(sessions, monday, 7)
        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            days.append({"date": d.isoformat(), "name": DAYS[d.weekday()], "limit": rules["day_minutes"][d.weekday()],
                         "minutes": sum(m for day, m, _ in on if day == d), "today": d == today, "past": d < today,
                         "windows": [{k: w[k] for k in ("label", "start", "end", "audio_only")} for w in rules["windows"]
                                     if d.weekday() in w["days"]]})
        packages = self.dojo.packages.by_id
        enrolled = self.dojo.profile(learner).get("enrolled")
        mine = [pid for pid in packages if enrolled is None or pid in enrolled]
        # An exam removed or left out since keeps its line while this week holds sessions of it.
        pids = [*mine, *sorted({s["package"] for _, _, s in on} - set(mine))]
        by_pkg = [{"package": pid, "exam": packages[pid]["meta"]["exam"] if pid in packages else pid.upper(),
                   "minutes": sum(m for _, m, s in on if s["package"] == pid),
                   "done": sum(m for _, m, s in on if s["package"] == pid and s["state"] == "done")}
                  for pid in pids]
        moved = self._moved_list(doc, now)
        return {
            "now": _local_str(now), "today": today.isoformat(), "timezone": TZID,
            "week": {"start": monday.isoformat(), "days": days, "limit": rules["week_minutes"],
                     "planned": sum(m for _, m, _ in on), "done": sum(m for _, m, s in on if s["state"] == "done"),
                     "packages": by_pkg},
            "rules": rules, "summary": rules_summary(rules),
            "sessions": [self._public(s, now) for s in sorted(in_week, key=shown_at)],
            "waiting": [self._public(s, now) for s in doc["sessions"] if s["state"] == "planned" and not s["start"]],
            "moved": moved[:8], "unplaced": doc["unplaced"],
            "horizon": self._horizon(learner, doc, today),
            "calendar": self.links.status(learner),
        }

    def _horizon(self, learner: Learner, doc: dict, today: date) -> list[dict]:
        profile = self.dojo.profile(learner)
        enrolled = profile.get("enrolled")
        out = []
        for pid, p in self.dojo.packages.by_id.items():
            if enrolled is not None and pid not in enrolled:
                continue
            exam_day = _day(profile["exam_dates"].get(pid))
            planned = [s for s in doc["sessions"] if s["package"] == pid and s["state"] == "planned" and s["start"]]
            out.append({"package": pid, "exam": p["meta"]["exam"], "title": p["meta"]["title"],
                        "date": exam_day.isoformat() if exam_day else None,
                        "days_left": (exam_day - today).days if exam_day else None,
                        "rehearse_from": (exam_day - timedelta(days=REHEARSE_DAYS)).isoformat() if exam_day else None,
                        "planned_minutes": sum(s["minutes"] for s in planned),
                        "practice_exams": sum(1 for s in planned if s["kind"] == "exam")})
        return out

    def today(self, learner: Learner, now: datetime | None = None) -> dict:
        now = now or utcnow()
        doc = self.refresh(learner, now)
        today = to_local(now).date()
        shown = [s for s in doc["sessions"] if s["state"] != "skipped" and shown_at(s)]
        own = sorted((s for s in shown if to_local(shown_at(s)).date() == today), key=shown_at)
        on = _on_days(shown, today, 1)
        return {"date": today.isoformat(), "now": _local_str(now), "limit": doc["rules"]["day_minutes"][today.weekday()],
                "minutes": sum(m for _, m, _ in on), "done": sum(m for _, m, s in on if s["state"] == "done"),
                "sessions": [self._public(s, now) for s in own], "calendar": self.links.status(learner)}

    # ------------------------------------------------------------ the feed

    def calendar_ics(self, learner: Learner, base: str, now: datetime | None = None, refresh: bool = True) -> bytes:
        """The plan as an iCalendar feed. An event carries the exam, the activity, the skill, the length
        and a link into Dojo: nothing from the Record, no results and no reasons."""
        now = now or utcnow()
        doc = self.refresh(learner, now) if refresh else self._load(learner)
        events = []
        for s in doc["sessions"]:
            start = _start(s)
            if s["state"] == "skipped" or start is None or start < now - FEED_PAST:
                continue
            if s["state"] == "done" and parse_iso(s["done_at"]) < start:
                continue   # done early: that time is free again
            exam, label, title = self._names(s)
            url = go_link(self.route(s), base)
            events.append({"uid": f"{s['id']}@dojo", "start": start, "minutes": s["minutes"],
                           "summary": " · ".join(x for x in (exam, label, title) if x),
                           "description": f"{s['minutes']} min. Open it in Dojo: {url}\nMove or skip it on Dojo's Plan page.",
                           "url": url, "seq": s["seq"], "created": parse_iso(s["created"]), "changed": parse_iso(s["changed"]),
                           "alarm": doc["rules"]["reminder"] if start > now else None})
        return write_ics(events, now)

    def link_view(self, token: str, base: str, learner: Learner | None = None) -> dict:
        url = f"{base}/cal/{token}/dojo.ics"
        return {**self.links.status(learner), "url": url, "qr": qr_data_uri(url)}

    def feed(self, token: str, base: str, fetch: bool = True, now: datetime | None = None) -> bytes | None:
        """The feed behind a calendar link, or None if the link is not the current one. All of it runs
        under the store lock, so turning the link off or deleting the record cannot land in the middle.
        The plan is that of the learner whose link it is, and its writes are bound to that learner."""
        with self.store.lock:
            learner = self.links.learner_for(token)
            if learner is None:
                return None
            with self.store.writing_as(learner.key):
                try:
                    body = self.calendar_ics(learner, base, now, refresh=fetch)
                except Exception:
                    log.exception("the calendar feed could not bring the plan up to date; it serves the plan as it was")
                    body = self.calendar_ics(learner, base, now, refresh=False)
                if fetch:
                    self.links.fetched(token, now, learner)
            return body


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _fair(teach: dict[str, tuple[list[Need], float]]) -> list[Need]:
    """Interleaves the exams' teaching steps: each exam gets time in proportion to how much it still has
    to show per week left before it."""
    queues = {pid: list(own) for pid, (own, _) in teach.items() if own}
    given = {pid: 0.0 for pid in queues}
    out: list[Need] = []
    while queues:
        pid = min(queues, key=lambda p: (given[p] / teach[p][1], p))
        need = queues[pid].pop(0)
        out.append(need)
        given[pid] += need.alts[0].minutes
        if not queues[pid]:
            del queues[pid]
    return out
