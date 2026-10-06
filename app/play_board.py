"""Play, the effort board (ADR 0010 section 5): weekly bands of effort, with no order.

Part of the social layer: team mode only, behind the owner's switch and the member's opt-in, and then its own
opt-in. Once a week, at the first read after Monday 00:00 Europe/Berlin, it is published from last week's
capped study points, for the members who joined it by then. What is stored:
- `learners/<key>/social/play.json`, under "social" -> "board": that the member joined, and "just my points";
- `play/board.json`: the week, and each member's band (a number 0-3) by learner key. No points, no names, no
  order; replaced the next Monday, so no history is kept. Leaving takes the member out of it at once.
A member sees their own exact points and band, and the display names of the others in their band only, in
alphabetical order, and only when at least 3 others share it. Nothing about the other bands.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from .core import Learner, iso, parse_iso, utcnow
from .play import last_week, local_day, monday
from .play_social import MONTHS, NOTICE_VERSION

BOARD_MIN = 10          # the board is shown only when at least 10 members have joined it
NAMES_MIN = 3           # names in your band only when at least 3 others share it
BANDS = (("Getting going", 0, 99), ("Building", 100, 199), ("Steady", 200, 299), ("Strong week", 300, 400))


def band_of(points: int) -> int:
    return min(len(BANDS) - 1, max(0, int(points)) // 100)


class Board:
    def __init__(self, social: Any):
        self.social = social
        self.store = social.store
        social.parts.append(self)

    # ---------------------------------------------------------------- who is on it

    def _setting(self, key: str) -> dict | None:
        s = self.social._social(key)
        if not s or s.get("notice") != NOTICE_VERSION:
            return None
        b = s.get("board")
        return b if isinstance(b, dict) else None

    def _joined(self, rows: dict[str, dict]) -> list[str]:
        return [k for k in self.social._everyone(rows)
                if not self.social._is_owner(k) and self.social._alive(k, rows) and self._setting(k) is not None]

    def _points(self, key: str, today: date, upto: str | None = None, published: str | None = None) -> int:
        """Last week's capped points. With `upto` (the hash of the member's last Record event when the board
        was published, "" for an empty Record) only the Record as it was then counts, so the exact points a
        member sees always lie in the band they were published in: an answer graded after publication
        cannot move them. A mark no longer found falls back to the events up to the publication time."""
        events = self.store.events(key)
        if upto == "":
            events = []
        elif upto and (at := next((i for i in range(len(events) - 1, -1, -1)
                                   if events[i].get("hash") == upto), None)) is not None:
            events = events[:at + 1]
        elif published:
            cut = parse_iso(published)
            events = [e for e in events if e.get("at") and parse_iso(e["at"]) <= cut]
        return last_week(events, self.social._doc(key), today)

    def _mark(self, key: str) -> str:
        events = self.store.events(key)
        return str(events[-1].get("hash") or "") if events else ""

    # ---------------------------------------------------------------- the weekly snapshot

    def _snapshot(self, now: datetime, rows: dict[str, dict]) -> dict:
        """This week's board: published once, from last week's points, for the members on it at that moment."""
        today = local_day(now)
        this = monday(today).isoformat()
        with self.store.lock:
            snap = self.store.read("play", "board", default=None)
            if isinstance(snap, dict) and snap.get("week") == this:
                return snap
            keys = self._joined(rows)
            on = len(keys) >= BOARD_MIN
            # Per member: the band, and the Record position it was read from (a hash: no points, no activity).
            marks = {k: self._mark(k) for k in keys} if on else {}
            snap = {"week": this, "published": iso(now), "open": on,
                    "bands": {k: band_of(self._points(k, today, marks[k])) for k in keys} if on else {},
                    "upto": marks}
            self.store.write("play", "board", value=snap)
            return snap

    def _live(self, snap: dict, rows: dict[str, dict]) -> dict[str, int]:
        """The snapshot without anyone who left the board, Play or Dojo since it was published."""
        joined = set(self._joined(rows))
        return {k: b for k, b in (snap.get("bands") or {}).items() if k in joined}

    def _drop(self, key: str) -> None:
        with self.store.lock:
            snap = self.store.read("play", "board", default=None)
            if isinstance(snap, dict) and (key in (snap.get("bands") or {}) or key in (snap.get("upto") or {})):
                (snap.get("bands") or {}).pop(key, None)
                (snap.get("upto") or {}).pop(key, None)
                self.store.write("play", "board", value=snap)

    # ---------------------------------------------------------------- the view

    def view(self, learner: Learner) -> dict:
        self.social.gate()
        key = learner.key
        out: dict[str, Any] = {
            "bands": [{"name": n, "low": lo, "high": hi} for n, lo, hi in BANDS],
            "min": BOARD_MIN, "names_min": NAMES_MIN, "owner": self.social._is_owner(key),
            "opted": self.social._opted(key), "joined": False, "mine_only": False, "state": None, "text": None,
        }
        if out["owner"]:
            out["text"] = "The person who runs this Dojo is not on the board, and cannot see it."
            return out
        if not out["opted"]:
            return out
        setting = self._setting(key)
        now = utcnow()
        rows = self.social._rows()
        snap = self._snapshot(now, rows)
        joined = self._joined(rows)
        next_monday = monday(local_day(now)) + timedelta(weeks=1)
        if not snap.get("open"):
            out["state"] = "closed"
            out["count"] = len(joined)
            out["text"] = (f"The board opens when {BOARD_MIN} people have joined. {len(joined)} have joined so far."
                           if len(joined) < BOARD_MIN else
                           f"{BOARD_MIN} people have joined. The board is published on Monday {next_monday.day} {MONTHS[next_monday.month - 1]}.")
        if setting is None:
            return out
        out["joined"], out["mine_only"] = True, bool(setting.get("mine_only"))
        if out["state"] == "closed":
            return out
        live = self._live(snap, rows)
        if len(live) < BOARD_MIN:
            out["state"], out["text"] = "paused", "Fewer than 10 people are on this week's board, so it is paused until Monday."
            return out
        if key not in live:
            out["state"] = "soon"
            out["text"] = f"You are on the board from Monday {next_monday.day} {MONTHS[next_monday.month - 1]}, with this week's points."
            return out
        mine = live[key]
        name, lo, hi = BANDS[mine]
        week = date.fromisoformat(snap["week"]) - timedelta(weeks=1)
        out["state"] = "shown"
        out["week"] = week.isoformat()
        # The same week and the same Record cutoff as the band: published once, never recalculated.
        points = self._points(key, date.fromisoformat(snap["week"]), (snap.get("upto") or {}).get(key),
                              snap.get("published"))
        out["me"] = {"points": points, "band": {"name": name, "low": lo, "high": hi}}
        if out["mine_only"]:
            return out
        others = [k for k, b in live.items() if b == mine and k != key]
        if len(others) >= NAMES_MIN:
            out["names"] = sorted((self.social._spoken(self.social._person(k, rows)) for k in others), key=str.lower)
        else:
            out["others"] = len(others)
        return out

    # ---------------------------------------------------------------- joining and leaving

    def _write_setting(self, learner: Learner, value: dict | None) -> None:
        with self.store.lock:
            doc = self.social._doc(learner.key)
            if value is None:
                doc["social"].pop("board", None)
            else:
                doc["social"]["board"] = value
            self.store.write("learners", learner.key, "social", "play", value=doc)

    def join(self, learner: Learner) -> dict:
        self.social._need_opted(learner)
        if self.social._is_owner(learner.key):
            raise HTTPException(403, "The person who runs this Dojo is not on the board.")
        with self.store.lock:
            self.social._need_opted(learner)   # a switch-off or Leave circles while this waited wins
            if self._setting(learner.key) is None:
                self._write_setting(learner, {"joined": iso(), "mine_only": False})
        return self.view(learner)

    def leave(self, learner: Learner) -> dict:
        self.social._need_opted(learner)
        with self.store.lock:
            self.social._need_opted(learner)
            if self._setting(learner.key) is not None:
                self._write_setting(learner, None)
            self._drop(learner.key)
        return self.view(learner)

    def set_mine_only(self, learner: Learner, mine_only: bool) -> dict:
        self.social._need_opted(learner)
        with self.store.lock:
            self.social._need_opted(learner)
            setting = self._setting(learner.key)
            if setting is None:
                raise HTTPException(409, "Join the board first.")
            self._write_setting(learner, {**setting, "mine_only": bool(mine_only)})
        return self.view(learner)

    # ---------------------------------------------------------------- the social layer's hooks

    def forget(self, learner: Learner) -> None:
        self._drop(learner.key)

    def not_invitable(self, key: str) -> None:
        return None

    def wipe(self) -> None:
        self.store.delete("play", "board")

    def sweep(self) -> None:
        """Publishes the week's board even when nobody looks, so the stored copy is never older than a week."""
        if self.social.enabled():
            self._snapshot(utcnow(), self.social._rows())
