"""Play, the social layer (ADR 0010 sections 4, 7 and 8): study circles with a shared weekly goal, the
witness room (one moment or one pass, shared with one circle) and pass celebrations.

Team mode only, and off until the owner switches it on (`team/play.json`, default off). Each member opts in
on their own, after a notice in two parts. What is stored:
- `learners/<key>/social/play.json`, under "social": the member's opt-in (the notice version, "Others can
  invite me" and the round of the owner's switch it belongs to). Leave Play deletes the whole file;
- `play/circles/<id>.json`: the members (learner keys and join dates), the goal, open invitations and the
  shared copies. No activity data: the bar is computed from the members' Records, from the days before
  today only, and never stored.
Responses carry display names and opaque ids: never a learner key, an Entra name or an email. Nothing here
is logged per person.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from .core import Learner, Settings, Store, iso, new_id, parse_iso, sha256, utcnow
from .play import NOT_A_CERTIFICATE, WEEK_CAP, PlayRoom, local_day, monday, week_before_today

log = logging.getLogger("dojo")

NOTICE_VERSION = 2      # 2: the effort board and duels
CIRCLE_MIN = 3          # the witness room: sharing needs at least 3 members
CIRCLE_MAX = 12
CIRCLES_EACH = 3        # a learner is in at most 3 circles
BAR_MIN = 5             # the bar exists only in circles of 5 or more members
GOAL_PER_MEMBER = 100   # the goal's floor, per member counted that week
BANDS = (0, 25, 50, 75, 100)
INVITE_DAYS = 7
SHARE_DAYS = 28
SHORT_DAYS = 7          # a circle under 3 members goes after this, open invitations or not
NAME_MAX = 40
OWNER_LABEL = "runs this Dojo"
SWEEP_SECONDS = 6 * 3600
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def person_id(key: str) -> str:
    """The opaque id a peer sees for a person: never the learner key."""
    return "p-" + sha256(f"play-person:{key}")[:20]


def short_day(at: str) -> str:
    d = local_day(at)
    return f"{d.day} {MONTHS[d.month - 1]}"


def share_text(who: str, s: dict) -> tuple[str, str | None]:
    """What a circle sees for a shared moment or pass, and the preview the sharer sees first: the same words.
    Dated and historical, so it stays true when the moment later needs a refresh. A pass has no date and no
    score, and reads the same whether or not a transcript listing was found."""
    exam, subject, kind = s["exam"], s.get("subject") or "", s["kind"]
    if kind == "passed":
        return f"{who} reports passing {exam}.", None
    when = short_day(s["at"])
    text = {
        "later": f"{who} showed '{subject}' ({exam}) on their own, later, on {when}.",
        "changed": f"{who} showed '{subject}' ({exam}) on their own in a new situation on {when}.",
        "relearned": f"{who} relearned '{subject}' ({exam}) on {when}.",
        "domain": f"{who} relearned every skill in '{subject}' ({exam}) on {when}.",
        "exam": f"{who} relearned every domain of {exam} on {when}.",
    }[kind]
    return text, NOT_A_CERTIFICATE


def notice(operator: str) -> dict:
    """The one-page notice for circles (P-12), in two separate parts: what the app shows, and the operator's
    technical access outside it. The same words are in docs/privacy.md."""
    in_app = [
        ("What circles are for",
         "A circle is 3 to 12 people who chose each other, to study alongside each other: a shared weekly goal in "
         "study points, and a place to share a moment or a pass when you want to. Nothing in a circle ranks or "
         "compares anyone."),
        ("What is kept, and for how long",
         "In your own folder, until you leave: that you joined, and whether others can invite you. For each circle: "
         "its name, its members and the dates they joined, open invitations (7 days), the goal, and the copies "
         "members shared (4 weeks, or until withdrawn, or at once when your record no longer supports one). The "
         "bar is worked out from members' records at most once a day and is not stored."),
        ("Who sees what",
         "If you turn on \"Others can invite me\", people who opted in to circles see your display name in the "
         "list of people they can invite. Members of a circle see each other's display names, what each chose "
         "to share with that circle, the goal, and the bar. They do not see your points, study days, answers, "
         "results or moments, unless you share a moment. Nobody sees who was invited: an invitation you decline "
         "or let lapse is deleted, and the person who invited you is never told."),
        ("No admin view",
         "No page in Dojo shows anyone's Play data to the owner's admin role: not the Members page, not "
         "diagnostics, not Studio. The admin role sees only whether circles are switched on."),
        ("The person who runs this Dojo",
         (f"{operator} runs this Dojo and may" if operator else "The person who runs this Dojo may") + " use Play as a "
         "learner. They cannot start a circle or invite anyone; "
         "they are in a circle only when a member invites them, and there they are shown as \"runs this Dojo\" "
         "and see what every member of that circle sees."),
        ("What the bar can reveal",
         "The bar shows only 0, 25, 50, 75 or 100% of the goal, only in circles of 5 or more members, and changes "
         "at most once a day. The goal is at least 100 study points per member. It does not hide everything: if "
         "every other member of your circle pools their own numbers, they can learn that you studied "
         "substantially in a period. That is effort only, never results: no score, answer, skill or moment can "
         "be read from the bar. If you do not accept that, stay out of circles, or leave one at any time."),
        ("The effort board",
         "Off unless you join it, and shown only when at least 10 people have joined. Once a week, on Monday, it "
         "puts last week's study points (at most 400) into four bands: Getting going (0-99), Building "
         "(100-199), Steady (200-299) and Strong week (300-400). You see your own points and your band, and the "
         "display names of the others in your band only, in alphabetical order, and only when at least 3 others "
         "share it; otherwise how many others do. Nobody sees another band, an order, a position or anyone "
         "else's points. It is a comparison of effort between colleagues, never of results. The person who "
         "runs this Dojo is never on it. Only this week's board is kept; leaving takes you off it at once."),
        ("Duels",
         "A duel is by invitation only, to someone who turned on \"Others can invite me\" and studies the same "
         "exam, so they see your display name in the duel list for the exams you both study. Both answer the "
         "same 10 new checked questions within 48 hours, in your own time, with no clock. Only the two of you "
         "see the two numbers right, once both have finished; each reviews only their own misses. There is no "
         "win, no loss and no record of results. An invitation you decline is not kept: to the person who "
         "invited you it looks exactly like one that lapsed. Your answers stay in your own record like any "
         "quick questions, without the other person's name. The duel is deleted 7 days after the invitation, or "
         "at once when either of you leaves. The person who runs this Dojo never invites anyone to a duel."),
        ("Leaving",
         "Leave a circle at any time; what you shared there goes with you. Turning circles off, or Leave Play, "
         "deletes your memberships, your shared copies, your invitations, your place on the board and your "
         "duels at once."),
    ]
    outside = [
        ("What the operator can technically access",
         "Outside the app, the operator is Global Administrator of the Microsoft tenant and Owner of the Azure "
         "subscription Dojo runs in. They can technically read everything Dojo stores, including Play's settings, "
         "the circle files, the board and the duels, and you cannot see when they do. Their written commitment: they do not read "
         "members' stored data, and if a support case ever needs it, they ask you first. A commitment is not a "
         "technical control."),
        ("Never for performance",
         "Dojo data is never used for performance reviews, staffing or HR decisions, and nothing is shared with "
         "managers."),
    ]
    return {"version": NOTICE_VERSION,
            "in_app": [{"title": t, "text": x} for t, x in in_app],
            "outside": [{"title": t, "text": x} for t, x in outside]}


class PlaySocial:
    """Circles, invitations, the banded bar and shared copies. Every route answers 404 unless team mode is on
    and the owner switched circles on (P-1)."""

    def __init__(self, settings: Settings, store: Store, team: Any, play: PlayRoom):
        self.settings = settings
        self.store = store
        self.team = team
        self.play = play
        self._bars: dict[tuple, int] = {}   # one band per circle and day, in memory only
        self._bars_lock = threading.Lock()   # every read, eviction and write of _bars (views run in parallel)
        self._started = False
        # The effort board and duels (play_board.py, play_duel.py): each has forget(learner), wipe() and sweep().
        self.parts: list[Any] = []

    # ---------------------------------------------------------------- the owner's switch

    def _team_on(self) -> bool:
        return bool(self.settings.team and not self.settings.local)

    def switch(self) -> dict:
        return self.store.read("team", "play", default=None) or {}

    def enabled(self) -> bool:
        return self._team_on() and bool(self.switch().get("on"))

    def gate(self) -> None:
        if not self.enabled():
            raise HTTPException(404, "Not Found")

    def switch_view(self) -> dict:
        return {"on": self.enabled(), "team": self._team_on()}

    def set_switch(self, on: bool) -> dict:
        """Turning circles off deletes every circle at once; turning them on again asks every member to opt in
        afresh (a new round)."""
        if not self._team_on():
            raise HTTPException(409, "Circles need team mode.")
        with self.store.lock:
            sw = self.switch()
            if bool(sw.get("on")) != on:
                self.store.write("team", "play", value={"on": on, "round": int(sw.get("round") or 0) + (1 if on else 0)})
                if not on:
                    for cid in self.store.names("play", "circles"):
                        self.store.delete("play", "circles", cid)
                    with self._bars_lock:
                        self._bars.clear()
                    for part in self.parts:
                        part.wipe()
        return self.switch_view()

    # ---------------------------------------------------------------- people

    def _rows(self) -> dict[str, dict]:
        return {r["key"]: r for r in self.team.rows() if not r.get("deleted_at")}

    def _is_owner(self, key: str) -> bool:
        return key == self.team.owner_key

    def _alive(self, key: str, rows: dict[str, dict]) -> bool:
        if self._is_owner(key):
            return True
        row = rows.get(key)
        return bool(row) and self.team.status_of(row) == "active"

    def _person(self, key: str, rows: dict[str, dict]) -> dict:
        """The display name a member chose (P-5), never the Entra name; the owner carries "runs this Dojo"."""
        if self._is_owner(key):
            name = (self.team.owner_doc().get("display_name") or "").strip() or "Owner"
            return {"id": person_id(key), "name": name, "label": OWNER_LABEL}
        return {"id": person_id(key), "name": (rows.get(key) or {}).get("display_name") or "Member", "label": None}

    def _everyone(self, rows: dict[str, dict]) -> list[str]:
        return [*rows, *([self.team.owner_key] if self.team.owner_key and self.team.owner_key not in rows else [])]

    @staticmethod
    def _spoken(p: dict) -> str:
        return f"{p['name']} ({p['label']})" if p["label"] else p["name"]

    def _operator(self) -> str:
        return (self.team.owner_doc().get("display_name") or "").strip()

    # ---------------------------------------------------------------- opt-in

    def _doc(self, key: str) -> dict | None:
        return self.store.read("learners", key, "social", "play")

    def _social(self, key: str, doc: dict | None = None) -> dict | None:
        """The member's opt-in for the current round of the owner's switch, or None."""
        doc = self._doc(key) if doc is None else doc
        s = (doc or {}).get("social")
        if not isinstance(s, dict) or s.get("round") != self.switch().get("round"):
            return None
        return s

    def _opted(self, key: str) -> bool:
        s = self._social(key)
        return bool(s) and s.get("notice") == NOTICE_VERSION

    def _invitable(self, key: str) -> bool:
        s = self._social(key)
        return bool(s) and s.get("notice") == NOTICE_VERSION and bool(s.get("invitable"))

    def _need_opted(self, learner: Learner) -> None:
        """Also called again inside every change, under the Store lock: a switch-off or a Leave circles that
        ran while the request waited for the lock wins, and nothing is written after it."""
        self.gate()
        if not self._opted(learner.key):
            raise HTTPException(409, "Join circles first.")

    def opt_in(self, learner: Learner, version: int | None, invitable: bool) -> dict:
        with self.store.lock:
            self.gate()
            doc = self._doc(learner.key)
            if doc is None:
                raise HTTPException(409, "Turn Play on first.")
            if version != NOTICE_VERSION:
                raise HTTPException(409, "The notice changed. Read it again first.")
            doc["social"] = {"round": self.switch().get("round"), "notice": NOTICE_VERSION, "invitable": bool(invitable)}
            self.store.write("learners", learner.key, "social", "play", value=doc)
        return self.view(learner)

    def set_invitable(self, learner: Learner, invitable: bool) -> dict:
        with self.store.lock:
            self._need_opted(learner)
            doc = self._doc(learner.key)
            doc["social"]["invitable"] = bool(invitable)
            self.store.write("learners", learner.key, "social", "play", value=doc)
            if not invitable:
                self._drop_invites_to(learner.key)
                for part in self.parts:
                    part.not_invitable(learner.key)
        return self.view(learner)

    def leave_social(self, learner: Learner) -> dict:
        """Turn circles off for me: memberships, shared copies and invitations go at once, in the same locked
        step as the opt-in, so nothing can be added in between; the shelf stays."""
        with self.store.lock:
            self.gate()
            self.forget(learner)
            doc = self._doc(learner.key)
            if doc is not None and "social" in doc:
                doc.pop("social")
                self.store.write("learners", learner.key, "social", "play", value=doc)
        return self.view(learner)

    # ---------------------------------------------------------------- circle documents

    def _read(self, cid: str) -> dict | None:
        c = self.store.read("play", "circles", cid)
        return c if isinstance(c, dict) and c.get("id") == cid else None

    def _write(self, c: dict) -> None:
        self.store.write("play", "circles", c["id"], value=c)

    def _drop_member(self, c: dict, key: str, now: datetime) -> None:
        """A member leaves: their shared copies go with them, and the bar is hidden for the rest of the week
        (a drop would show what they had added). Under 3 members, the 7-day clock starts now, whichever path
        removed them."""
        c["members"] = [m for m in c["members"] if m["key"] != key]
        c["shares"] = [s for s in c["shares"] if s["key"] != key]
        c["left_week"] = monday(local_day(now)).isoformat()
        if len(c["members"]) < CIRCLE_MIN and not c.get("short_since"):
            c["short_since"] = iso(now)

    def _tidy(self, c: dict, rows: dict[str, dict], now: datetime) -> dict | None:
        """Apply what is due, under the store lock: members who left Play or Dojo, lapsed invitations, copies
        older than 4 weeks, a circle left too small, and the week's goal. Returns None if the circle is gone."""
        changed = False
        for m in list(c["members"]):
            if not self._alive(m["key"], rows) or self._social(m["key"]) is None:
                self._drop_member(c, m["key"], now)
                changed = True
        keys = {m["key"] for m in c["members"]}
        invites = [i for i in c["invites"] if parse_iso(i["at"]) + timedelta(days=INVITE_DAYS) > now
                   and i["key"] not in keys and self._alive(i["key"], rows) and self._invitable(i["key"])]
        shares = [s for s in c["shares"] if parse_iso(s["shared"]) + timedelta(days=SHARE_DAYS) > now and s["key"] in keys]
        if invites != c["invites"] or shares != c["shares"]:
            c["invites"], c["shares"] = invites, shares
            changed = True
        if not c["members"]:
            self.store.delete("play", "circles", c["id"])
            return None
        if len(c["members"]) < CIRCLE_MIN:
            if not c.get("short_since"):
                c["short_since"] = iso(now)
                changed = True
            elif parse_iso(c["short_since"]) + timedelta(days=SHORT_DAYS) <= now:
                # Open invitations do not hold it: renewing them must not keep a small circle forever.
                self.store.delete("play", "circles", c["id"])
                return None
        elif c.get("short_since"):
            c["short_since"] = None
            changed = True
        this = monday(local_day(now))
        if c.get("week") != this.isoformat():
            # Set when the week starts, from the members counted that week, and fixed for the week.
            counted = sum(1 for m in c["members"] if local_day(m["joined"]) < this)
            floor = GOAL_PER_MEMBER * counted
            c["week"], c["goal"] = this.isoformat(), max(floor, min(int(c.get("chosen") or 0), WEEK_CAP * counted))
            changed = True
        if changed:
            self._write(c)
        return c

    def _circles(self, rows: dict[str, dict], now: datetime) -> list[dict]:
        out = []
        with self.store.lock:
            for cid in self.store.names("play", "circles"):
                c = self._read(cid)
                if c is not None:
                    c = self._tidy(c, rows, now)
                if c is not None:
                    out.append(c)
        return out

    def _mine(self, key: str, rows: dict[str, dict], now: datetime) -> list[dict]:
        return [c for c in self._circles(rows, now) if any(m["key"] == key for m in c["members"])]

    def _my_circle(self, learner: Learner, cid: str) -> tuple[dict, dict]:
        rows = self._rows()
        for c in self._mine(learner.key, rows, utcnow()):
            if c["id"] == cid:
                return c, rows
        raise HTTPException(404, "No such circle.")

    def _no_owner(self, learner: Learner) -> None:
        if self._is_owner(learner.key):
            raise HTTPException(403, "The person who runs this Dojo joins circles only when a member invites them, "
                                     "and does not start circles or invite.")

    def _targets(self, people: list[str] | None, me: str, rows: dict[str, dict]) -> list[str]:
        """The people a member picked, by their opaque ids, among those who can be invited right now."""
        by_id = {person_id(k): k for k in self._everyone(rows)
                 if k != me and self._alive(k, rows) and self._invitable(k)}
        out = []
        for pid in people or []:
            if pid not in by_id:
                raise HTTPException(400, "Someone on the list can no longer be invited.")
            if by_id[pid] not in out:
                out.append(by_id[pid])
        return out

    def _drop_invites_to(self, key: str) -> None:
        with self.store.lock:
            for cid in self.store.names("play", "circles"):
                c = self._read(cid)
                if c and any(i["key"] == key for i in c["invites"]):
                    c["invites"] = [i for i in c["invites"] if i["key"] != key]
                    self._write(c)

    # ---------------------------------------------------------------- the bar

    def _week_points(self, key: str, today: date) -> int:
        return week_before_today(self.store.events(key), self._doc(key), today)

    def _bar(self, c: dict, today: date) -> dict:
        """Present whenever the circle has 5 members counted this week, whatever anyone did; only a band."""
        this = monday(today)
        if len(c["members"]) < BAR_MIN:
            return {"state": "small", "text": "The bar appears when the circle has 5 members."}
        if c.get("left_week") == this.isoformat():
            return {"state": "paused", "text": "The circle changed this week. The bar is back on Monday."}
        counted = sorted(m["key"] for m in c["members"] if local_day(m["joined"]) < this)
        if len(counted) < BAR_MIN:
            return {"state": "soon", "text": "A member counts from the Monday after they join. The bar starts once 5 do."}
        goal = max(1, int(c.get("goal") or 0))
        ck = (c["id"], today.isoformat(), goal, tuple(counted))
        with self._bars_lock:
            band = self._bars.get(ck)
        if band is None:
            total = sum(self._week_points(k, today) for k in counted)
            band = BANDS[min(len(BANDS) - 1, total * 4 // goal)]
            with self._bars_lock:
                # The first band of the day stays: a concurrent view that computed it first wins.
                band = self._bars.setdefault(ck, band)
                for old in [k for k in self._bars if k[1] != today.isoformat()]:
                    self._bars.pop(old, None)
        met = band == 100
        return {"state": "shown", "band": band, "goal": goal, "met": met,
                "text": "Your circle met this week's goal." if met else None}

    # ---------------------------------------------------------------- shared copies

    def _moments(self, key: str, cache: dict) -> list[dict] | None:
        if key not in cache:
            cache[key] = self.play.current_moments(Learner("", key, ""))
        return cache[key]

    def _supported(self, s: dict, cache: dict) -> bool:
        ms = self._moments(s["key"], cache)
        return ms is not None and any(m["id"] == s["moment"] and m["at"] == s["at"] for m in ms)

    def _recheck(self, c: dict, cache: dict) -> dict:
        """Every read re-checks each copy against its sharer's Record; one whose support failed is deleted
        before anyone sees it."""
        keep = [s for s in c["shares"] if self._supported(s, cache)]
        if len(keep) != len(c["shares"]):
            with self.store.lock:
                live = self._read(c["id"])
                if live is not None:
                    gone = {s["id"] for s in c["shares"]} - {s["id"] for s in keep}
                    live["shares"] = [s for s in live["shares"] if s["id"] not in gone]
                    self._write(live)
            c["shares"] = keep
        return c

    def record_changed(self, key: str, event: dict) -> None:
        """Called by the Store after every event it adds to a Record, under its lock. A dispute, a later answer,
        or a pass reported again can take away a shared moment's support: the sharer's affected copies are
        deleted in that same step, not at the next read or sweep."""
        if not self.enabled() or self._social(key) is None:
            return
        with self.store.lock:
            cache: dict = {}
            for cid in self.store.names("play", "circles"):
                c = self._read(cid)
                if c is None or not any(s["key"] == key for s in c["shares"]):
                    continue
                keep = [s for s in c["shares"] if s["key"] != key or self._supported(s, cache)]
                if len(keep) != len(c["shares"]):
                    c["shares"] = keep
                    self._write(c)

    # ---------------------------------------------------------------- views

    def view(self, learner: Learner) -> dict:
        self.gate()
        key = learner.key
        doc = self._doc(key)
        s = self._social(key, doc)
        out: dict[str, Any] = {
            "play": doc is not None, "notice": notice(self._operator()), "owner": self._is_owner(key),
            "opted": bool(s) and s.get("notice") == NOTICE_VERSION, "invitable": bool(s and s.get("invitable")),
            "limits": {"min": CIRCLE_MIN, "max": CIRCLE_MAX, "each": CIRCLES_EACH, "bar": BAR_MIN,
                       "goal_per_member": GOAL_PER_MEMBER, "share_days": SHARE_DAYS, "invite_days": INVITE_DAYS},
        }
        if not out["opted"]:
            return out
        now = utcnow()
        today = local_day(now)
        rows = self._rows()
        circles = self._circles(rows, now)
        mine = [c for c in circles if any(m["key"] == key for m in c["members"])]
        out["me"] = self._person(key, rows)
        out["can_start"] = not self._is_owner(key) and len(mine) < CIRCLES_EACH
        out["people"] = sorted(
            (self._person(k, rows) for k in self._everyone(rows)
             if k != key and self._alive(k, rows) and self._invitable(k)),
            key=lambda p: (p["name"].lower(), p["id"]))
        names = lambda c: sorted(self._spoken(self._person(m["key"], rows)) for m in c["members"])  # noqa: E731
        out["invitations"] = [{"id": i["id"], "circle": c["name"], "members": names(c)}
                              for c in circles for i in c["invites"] if i["key"] == key]
        cache: dict = {}
        out["circles"] = [self._circle_view(c, key, rows, today, cache)
                          for c in sorted(mine, key=lambda c: (c["name"].lower(), c["id"]))]
        return out

    def _circle_view(self, c: dict, key: str, rows: dict[str, dict], today: date, cache: dict) -> dict:
        c = self._recheck(c, cache)
        n = len(c["members"])
        members = sorted(({**self._person(m["key"], rows), "you": m["key"] == key} for m in c["members"]),
                         key=lambda p: (p["name"].lower(), p["id"]))
        shared = []
        if n >= CIRCLE_MIN:
            for s in sorted(c["shares"], key=lambda s: s["shared"], reverse=True):
                text, line = share_text(self._spoken(self._person(s["key"], rows)), s)
                shared.append({"id": s["id"], "text": text, "line": line, "mine": s["key"] == key,
                               "moment": s["moment"] if s["key"] == key else None})
        return {
            "id": c["id"], "name": c["name"], "size": n, "members": members,
            "bar": self._bar(c, today),
            "next_goal": max(GOAL_PER_MEMBER * n, min(int(c.get("chosen") or 0), WEEK_CAP * n)),
            "goal_range": [GOAL_PER_MEMBER * n, WEEK_CAP * n],
            "can_share": n >= CIRCLE_MIN, "can_invite": not self._is_owner(key) and n < CIRCLE_MAX,
            "shared": shared,
        }

    # ---------------------------------------------------------------- circles and invitations

    def create(self, learner: Learner, name: str | None, people: list[str] | None) -> dict:
        self._need_opted(learner)
        self._no_owner(learner)
        name = " ".join((name or "").split())[:NAME_MAX]
        if not name:
            raise HTTPException(400, "Give the circle a name.")
        with self.store.lock:
            self._need_opted(learner)
            rows = self._rows()
            targets = self._targets(people, learner.key, rows)
            if not CIRCLE_MIN - 1 <= len(targets) <= CIRCLE_MAX - 1:
                raise HTTPException(400, f"Invite {CIRCLE_MIN - 1} to {CIRCLE_MAX - 1} people: a circle has {CIRCLE_MIN} "
                                         f"to {CIRCLE_MAX} members.")
            now = utcnow()
            if len(self._mine(learner.key, rows, now)) >= CIRCLES_EACH:
                raise HTTPException(409, f"You are in {CIRCLES_EACH} circles already. Leave one first.")
            self._write({"id": new_id("circle"), "name": name, "created": iso(now),
                         "members": [{"key": learner.key, "joined": iso(now)}],
                         "invites": [{"id": new_id("inv"), "key": k, "at": iso(now)} for k in targets],
                         "shares": [], "week": None, "goal": None, "chosen": None, "left_week": None,
                         "short_since": iso(now)})
        return self.view(learner)

    def invite(self, learner: Learner, cid: str, people: list[str] | None) -> dict:
        self._need_opted(learner)
        self._no_owner(learner)
        with self.store.lock:
            self._need_opted(learner)
            c, rows = self._my_circle(learner, cid)
            targets = self._targets(people, learner.key, rows)
            if not targets:
                raise HTTPException(400, "Pick someone to invite.")
            keys = {m["key"] for m in c["members"]}
            if keys & set(targets):
                raise HTTPException(400, "Someone on the list is in this circle already.")
            if len(keys) + len(targets) > CIRCLE_MAX:
                raise HTTPException(400, f"A circle has at most {CIRCLE_MAX} members.")
            # Someone with an open invitation already is skipped without a word: the inviter never learns
            # whether an invitation is pending, declined or lapsed.
            open_ = {i["key"] for i in c["invites"]}
            now = iso()
            c["invites"] += [{"id": new_id("inv"), "key": k, "at": now} for k in targets if k not in open_]
            self._write(c)
        return self.view(learner)

    def _invitation(self, learner: Learner, iid: str) -> dict:
        for c in self._circles(self._rows(), utcnow()):
            if any(i["id"] == iid and i["key"] == learner.key for i in c["invites"]):
                return c
        raise HTTPException(404, "No such invitation.")

    def accept(self, learner: Learner, iid: str) -> dict:
        self._need_opted(learner)
        with self.store.lock:
            self._need_opted(learner)
            rows = self._rows()
            c = self._invitation(learner, iid)
            if len(self._mine(learner.key, rows, utcnow())) >= CIRCLES_EACH:
                raise HTTPException(409, f"You are in {CIRCLES_EACH} circles already. Leave one first.")
            if len(c["members"]) >= CIRCLE_MAX:
                raise HTTPException(409, "This circle is full.")
            c["invites"] = [i for i in c["invites"] if i["key"] != learner.key]
            c["members"].append({"key": learner.key, "joined": iso()})
            if len(c["members"]) >= CIRCLE_MIN:
                c["short_since"] = None
            self._write(c)
        return self.view(learner)

    def decline(self, learner: Learner, iid: str) -> dict:
        """Deleted, and recorded nowhere: the inviter cannot tell it from a lapse."""
        self._need_opted(learner)
        with self.store.lock:
            self._need_opted(learner)
            c = self._invitation(learner, iid)
            c["invites"] = [i for i in c["invites"] if i["id"] != iid]
            self._write(c)
        return self.view(learner)

    def leave_circle(self, learner: Learner, cid: str) -> dict:
        self._need_opted(learner)
        with self.store.lock:
            self._need_opted(learner)
            c, rows = self._my_circle(learner, cid)
            self._drop_member(c, learner.key, utcnow())
            self._write(c)
            self._tidy(c, rows, utcnow())
        return self.view(learner)

    def set_goal(self, learner: Learner, cid: str, points: int | None) -> dict:
        """Next week's goal. Never below the floor; the circle sees the goal, never who changed it."""
        self._need_opted(learner)
        with self.store.lock:
            self._need_opted(learner)
            c, _ = self._my_circle(learner, cid)
            n = len(c["members"])
            low, high = GOAL_PER_MEMBER * n, WEEK_CAP * n
            if not isinstance(points, int) or not low <= points <= high:
                raise HTTPException(400, f"Next week's goal is between {low} and {high} study points.")
            c["chosen"] = points
            self._write(c)
        return self.view(learner)

    # ---------------------------------------------------------------- sharing

    def _moment_to_share(self, learner: Learner, c: dict, moment: str | None) -> dict:
        if len(c["members"]) < CIRCLE_MIN:
            raise HTTPException(409, f"Sharing starts when the circle has {CIRCLE_MIN} members.")
        m = next((m for m in self.play.current_moments(learner) or [] if m["id"] == moment), None)
        if m is None:
            raise HTTPException(404, "No such moment on your shelf.")
        return {"key": learner.key, "moment": m["id"], "kind": m["kind"], "package": m["package"],
                "exam": m["exam"], "subject": m.get("skill_text") or "", "at": m["at"]}

    def preview(self, learner: Learner, cid: str, moment: str | None) -> dict:
        """Exactly what the circle will see (EG-25)."""
        self._need_opted(learner)
        c, rows = self._my_circle(learner, cid)
        s = self._moment_to_share(learner, c, moment)
        text, line = share_text(self._spoken(self._person(learner.key, rows)), s)
        return {"circle": c["name"], "text": text, "line": line, "days": SHARE_DAYS}

    def share(self, learner: Learner, cid: str, moment: str | None) -> dict:
        self._need_opted(learner)
        with self.store.lock:
            self._need_opted(learner)
            c, rows = self._my_circle(learner, cid)
            s = self._moment_to_share(learner, c, moment)
            for other in self._mine(learner.key, rows, utcnow()):
                if any(x["key"] == learner.key and x["moment"] == s["moment"] for x in other["shares"]):
                    raise HTTPException(409, f"Shared with \"{other['name']}\" already. Withdraw it there first.")
            c["shares"].append({"id": new_id("share"), **s, "shared": iso()})
            self._write(c)
        return self.view(learner)

    def withdraw(self, learner: Learner, sid: str) -> dict:
        self._need_opted(learner)
        with self.store.lock:
            self._need_opted(learner)
            for c in self._mine(learner.key, self._rows(), utcnow()):
                if any(s["id"] == sid and s["key"] == learner.key for s in c["shares"]):
                    c["shares"] = [s for s in c["shares"] if s["id"] != sid]
                    self._write(c)
                    return self.view(learner)
        raise HTTPException(404, "No such shared moment.")

    # ---------------------------------------------------------------- leaving and the sweeper

    def forget(self, learner: Learner) -> None:
        """Leave Play, Delete my record, Leave Dojo, and a membership that ends (removal, expiry, the end of
        team mode): the learner's memberships, shared copies and invitations go at once, in every circle.
        Runs in both modes, whatever the switch."""
        now = utcnow()
        with self.store.lock:
            for cid in self.store.names("play", "circles"):
                c = self._read(cid)
                if c is None:
                    continue
                touched = False
                if any(m["key"] == learner.key for m in c["members"]):
                    self._drop_member(c, learner.key, now)
                    touched = True
                if any(i["key"] == learner.key for i in c["invites"]):
                    c["invites"] = [i for i in c["invites"] if i["key"] != learner.key]
                    touched = True
                if touched:
                    if c["members"]:
                        self._write(c)
                    else:
                        self.store.delete("play", "circles", cid)
            for part in self.parts:
                part.forget(learner)

    def sweep(self) -> None:
        """Deletes what is due (lapsed invitations, copies after 4 weeks or whose support failed, members who
        left, empty circles, old duels). Every read applies the same rules, so this only keeps the files small."""
        rows = self._rows()
        cache: dict = {}
        for c in self._circles(rows, utcnow()):
            self._recheck(c, cache)
        for part in self.parts:
            part.sweep()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="play-sweeper", daemon=True).start()

    def _loop(self) -> None:
        time.sleep(60)
        while True:
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("the Play sweeper failed")
            time.sleep(SWEEP_SECONDS)
