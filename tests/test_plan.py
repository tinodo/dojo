"""The plan (screen 08) and its private calendar feed: the learner's rules, the scheduler, "done" read from the
Record, the iCalendar output (RFC 5545), the secret link and the open /cal routes."""
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import plan as P  # noqa: E402
from app.checks import pick  # noqa: E402
from app.core import Learner, Store, UserError, canonical, iso, learner_key, new_id, sha256, utcnow  # noqa: E402
from app.evidence import derive  # noqa: E402
from app.exam import spec as exam_spec  # noqa: E402
from app.labs import LABS  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_api import POST, principal, settings  # noqa: E402
from tests.test_podcast import call  # noqa: E402

GH, DP = "gh-300", "dp-800"
BASE = "https://dojo.example"


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def berlin(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return P.to_utc(datetime(y, mo, d, h, mi))


MON = berlin(2026, 10, 5, 7)  # a Monday, 07:00 in Berlin


def ahead() -> datetime:
    """A Monday 07:00 in Berlin at least eight days from now, so every made-up event lies after the ones the
    app writes with the real clock, and the Record stays in time order."""
    today = P.to_local(utcnow()).date()
    return P.to_utc(datetime.combine(today + timedelta(days=14 - today.weekday()), time(7)))


_GH: list[dict] = []


def gh_package() -> dict:
    if not _GH:
        _GH.append(create_app(settings(tempfile.mkdtemp(prefix="dojo-plan-"))).state.dojo.packages.get(GH))
    return _GH[0]


def rules(**changes) -> dict:
    r = deepcopy(P.DEFAULT_RULES)
    r.update(changes)
    return P.check_rules(r)


def lesson_need(key: str, minutes: int = 15, **kw) -> P.Need:
    return P.Need(key, GH, "d1.g1.s1", [P.Alt("lesson", minutes)], **kw)


def local(hit) -> datetime:
    return P.to_local(hit[1])


def event(etype: str, at: datetime, **data) -> dict:
    return {"id": new_id("ev"), "at": iso(at), "type": etype, "data": data}


def session(kind: str, start: datetime | None, created: datetime = MON, pkg: str = GH, skill: str | None = "d1.g1.s1",
            minutes: int = 5, **kw) -> dict:
    n = P.Need(f"{kind}:{pkg}:{skill}:{new_id('k')}", pkg, skill, [P.Alt(kind, minutes)], **kw)
    s = P._session(n, n.alts[0], start or created, created)
    s["start"] = iso(start) if start else None
    return s


def unfold(body: bytes) -> list[str]:
    text = body.decode("utf-8")
    lines: list[str] = []
    for raw in text.split("\r\n")[:-1]:
        if raw.startswith(" "):
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def blocks(lines: list[str], name: str) -> list[list[str]]:
    """Each component with this name, as its lines without BEGIN and END (nested components included)."""
    out, cur, depth = [], None, 0
    for line in lines:
        if line == f"BEGIN:{name}":
            cur, depth = [], 0
            continue
        if cur is None:
            continue
        if line == f"END:{name}" and depth == 0:
            out.append(cur)
            cur = None
            continue
        depth += line.startswith("BEGIN:") - line.startswith("END:")
        cur.append(line)
    return out


def own(lines: list[str]) -> dict[str, list[str]]:
    """The properties of a component itself, by name without parameters, leaving nested components out."""
    props: dict[str, list[str]] = {}
    depth = 0
    for line in lines:
        if line.startswith("BEGIN:"):
            depth += 1
        elif line.startswith("END:"):
            depth -= 1
        elif depth == 0:
            name, value = line.split(":", 1)
            props.setdefault(name.split(";")[0], []).append(line)
    return props


def value(line: str) -> str:
    return line.split(":", 1)[1]


def untext(v: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


CONTENT_LINE = re.compile(r"^[A-Z][A-Z0-9-]*(;[A-Z0-9-]+=[^;:\"]*)*:.*$")


class Record:
    """Writes the learner's hash-chained Record at chosen times, the way Store.append_event does."""

    def __init__(self, store: Store, key: str):
        self.store, self.key = store, key

    def add(self, etype: str, at: datetime, **data) -> dict:
        with self.store.lock:
            prev = self.store._heads.get(self.key)
            if prev is None:
                events = self.store.events(self.key)
                prev = events[-1]["hash"] if events else "0" * 64
            e = {"id": new_id("ev"), "at": iso(at), "type": etype, "data": data, "prev": prev}
            e["hash"] = sha256(canonical(e))
            self.store.append_line("learners", self.key, "events.jsonl", value=e)
            self.store._heads[self.key] = e["hash"]
        return e

    def attempt(self, pkg: str, sid: str, at: datetime, mode: str, met: int, total: int = 3, hints: int = 0,
                text: str = "my answer") -> str:
        item = new_id("item")
        self.add("item.issued", at, package=pkg, skill=sid, item=item, mode=mode, kind="short", changed_condition=False,
                 novelty_checked=True)
        for _ in range(hints):
            self.add("hint.shown", at + timedelta(minutes=1), package=pkg, skill=sid, item=item)
        self.add("answer.submitted", at + timedelta(minutes=3), package=pkg, skill=sid, item=item, mode=mode, text=text,
                 elapsed_s=100, hints_seen=hints, input="typed", edited_before_send=False)
        self.add("answer.judged", at + timedelta(minutes=4), package=pkg, skill=sid, item=item, mode=mode, met=met, total=total)
        return item


class World:
    """One Dojo in local mode with an empty Record."""

    def __init__(self, local: bool = True):
        self.tmp = tempfile.mkdtemp(prefix="dojo-plan-")
        self.app = create_app(settings(self.tmp, local=local))
        self.dojo, self.planner = self.app.state.dojo, self.app.state.plan
        self.store = self.dojo.store
        self.me = self.planner.links.owner
        self.record = Record(self.store, self.me.key)

    def skills(self, pid: str) -> list[str]:
        pkg = self.dojo.packages.get(pid)
        return [s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]]

    def exam_dates(self, **dates: date | None) -> None:
        profile = self.dojo.profile(self.me)
        profile["exam_dates"] = {pid.replace("_", "-"): d.isoformat() if d else None for pid, d in dates.items()}
        self.store.write("learners", self.me.key, "profile", value=profile)

    def plan_doc(self) -> dict:
        return self.store.read("learners", self.me.key, "plan")

    def files(self, leave_out: tuple[str, ...] = ()) -> dict[str, bytes]:
        root = Path(self.tmp)
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
                if p.is_file() and p.name not in leave_out}


# ---------------------------------------------------------------- Berlin time

class TimeTests(unittest.TestCase):
    def test_summer_time_follows_the_eu_rule(self):
        self.assertEqual(P._last_sunday(2026, 3), date(2026, 3, 29))
        self.assertEqual(P._last_sunday(2026, 10), date(2026, 10, 25))
        self.assertEqual(P._last_sunday(2027, 3), date(2027, 3, 28))
        self.assertEqual(P._last_sunday(2027, 10), date(2027, 10, 31))
        self.assertEqual(P.to_local(utc(2026, 3, 29, 0, 59)), datetime(2026, 3, 29, 1, 59))
        self.assertEqual(P.to_local(utc(2026, 3, 29, 1, 0)), datetime(2026, 3, 29, 3, 0))
        self.assertEqual(P.to_local(utc(2026, 10, 25, 0, 59)), datetime(2026, 10, 25, 2, 59))
        self.assertEqual(P.to_local(utc(2026, 10, 25, 1, 0)), datetime(2026, 10, 25, 2, 0))

    def test_wall_clock_times_in_the_spring_gap_and_the_autumn_overlap(self):
        self.assertEqual(P.to_utc(datetime(2026, 3, 29, 2, 30)), utc(2026, 3, 29, 1, 30))
        self.assertEqual(P.to_utc(datetime(2026, 10, 25, 2, 30)), utc(2026, 10, 25, 0, 30))
        t = utc(2026, 1, 1)
        while t < utc(2028, 1, 1):
            loc = P.to_local(t)
            self.assertEqual(P.to_local(P.to_utc(loc)), loc, t)
            t += timedelta(minutes=30)
        # On 28 March 2027 the clocks go from 02:00 to 03:00: the hour between does not exist.
        self.assertEqual([P.exists(datetime(2027, 3, 28, h, m)) for h, m in ((1, 59), (2, 0), (2, 30), (2, 59), (3, 0))],
                         [True, False, False, False, True])
        self.assertTrue(P.exists(datetime(2027, 10, 31, 2, 30)), "in autumn it exists twice")

    def test_the_same_as_the_time_zone_database(self):
        """Dojo does not need the time zone database: the EU rule above is all it uses. This checks the rule
        against it. Linux, where CI and App Service run, has one; Windows has none without the tzdata package."""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Berlin")
        except Exception:
            if sys.platform.startswith("linux"):
                raise
            self.skipTest("this machine has no time zone database")
        t = utc(2026, 1, 1)
        while t < utc(2031, 1, 1):
            self.assertEqual(P.to_local(t), t.astimezone(tz).replace(tzinfo=None), t)
            t += timedelta(minutes=30)
        # Wall-clock times: one in the spring gap reads as winter time, one in the autumn hour as its first moment
        # (fold=0), and a time exists only if it comes back unchanged.
        local = datetime(2026, 1, 1)
        while local < datetime(2031, 1, 1):
            moment = local.replace(tzinfo=tz).astimezone(timezone.utc)
            self.assertEqual(P.to_utc(local), moment, local)
            self.assertEqual(P.exists(local), moment.astimezone(tz).replace(tzinfo=None) == local, local)
            local += timedelta(minutes=30)


# ---------------------------------------------------------------- the learner's rules

class RulesTests(unittest.TestCase):
    def test_the_defaults_are_the_owners_week(self):
        r = P.check_rules(deepcopy(P.DEFAULT_RULES))
        self.assertEqual(P.rules_summary(r), "at most 60 min on weekdays · 2 h on Saturday · Sunday off · nothing before 08:00"
                                             " · the 08:30 commute is audio only · at most 7 h a week")

    def test_rules_that_cannot_work_are_refused_in_plain_words(self):
        window = {"label": "x", "days": [0], "start": "10:00", "end": "11:00", "audio_only": False}
        cases = [
            ({"day_minutes": [60] * 6}, "each day"),
            ({"day_minutes": [601] + [60] * 6}, "0 to 600"),
            ({"day_minutes": [True] + [60] * 6}, "0 to 600"),
            ({"week_minutes": 4201}, "weekly limit"),
            ({"week_minutes": "420"}, "weekly limit"),
            ({"earliest": "8:00"}, "08:30"),
            ({"reminder": 121}, "reminder"),
            ({"windows": [{**window, "days": []}]}, "at least one day"),
            ({"windows": [{**window, "days": [7]}]}, "at least one day"),
            ({"windows": [{**window, "end": "10:04"}]}, "at least 5 minutes"),
            ({"windows": [{**window, "start": "24:00", "end": "24:30"}]}, "08:30"),
            ({"windows": [{**window, "label": "A", "days": [0, 1]}, {**window, "label": "B", "days": [1], "start": "10:30", "end": "12:00"}]},
             "overlap on the same day: A and B"),
            ({"windows": [{**window, "start": f"{i:02d}:00", "end": f"{i:02d}:30"} for i in range(13)]}, "at most 12"),
        ]
        for change, words in cases:
            with self.assertRaises(UserError, msg=change) as e:
                rules(**change)
            self.assertIn(words, str(e.exception), change)
        with self.assertRaises(UserError):
            P.check_rules(["not", "rules"])

    def test_free_times_are_tidied_not_trusted(self):
        r = rules(windows=[{"label": "Evening\x07 walk " + "x" * 60, "days": [4, 0, 0], "start": "18:00", "end": "19:00", "audio_only": "yes"},
                           {"label": "", "days": [0], "start": "07:00", "end": "07:30", "audio_only": True}])
        self.assertEqual([w["start"] for w in r["windows"]], ["07:00", "18:00"])
        evening = r["windows"][1]
        self.assertEqual(evening["days"], [0, 4])
        self.assertTrue(evening["label"].startswith("Evening walk "))
        self.assertEqual(len(evening["label"]), 40)
        self.assertFalse(evening["audio_only"], "only true itself means audio only")
        self.assertIn("Mon 07:00 to 07:30 is audio only", P.rules_summary(r))


# ---------------------------------------------------------------- placing sessions

class BoardTests(unittest.TestCase):
    def place(self, needs: list[P.Need], r: dict | None = None, now: datetime = MON, taken: tuple = ()) -> list:
        board = P.Board(r or rules(), now)
        for start, minutes in taken:
            board.take(start, minutes)
        hits = []
        for n in needs:
            hit = board.find(n)
            if hit:
                board.take(hit[1], hit[0].minutes)
            hits.append(hit)
        return hits

    def test_audio_only_free_times_take_listening_and_nothing_else(self):
        needs = []
        for i in range(30):
            alts = [[P.Alt("lesson", 15)], [P.Alt("listen", 12, audio=True)], [P.Alt("lesson", 15), P.Alt("listen", 7, audio=True)]][i % 3]
            needs.append(P.Need(f"k{i}", GH, "d1.g1.s1", alts))
        hits = self.place(needs)
        self.assertTrue(any(h and h[0].kind == "listen" for h in hits))
        for n, hit in zip(needs, hits):
            if not hit:
                continue
            at = local(hit)
            m = at.hour * 60 + at.minute
            commute = at.weekday() < 5 and 8 * 60 + 30 <= m < 9 * 60
            self.assertEqual(hit[0].kind == "listen", commute, (n.key, at, hit[0]))
            if commute:
                self.assertLessEqual(m + hit[0].minutes, 9 * 60)

    def test_the_day_limit_and_the_most_sessions_a_day(self):
        hits = self.place([lesson_need(f"l{i}") for i in range(5)])
        days = [local(h).date() for h in hits]
        self.assertEqual(days.count(date(2026, 10, 5)), 4, "4 x 15 min fill the 60 minutes of a weekday")
        self.assertEqual(days[4], date(2026, 10, 6))
        self.assertEqual({local(h).strftime("%H:%M") for h in hits[:4]}, {"12:30", "12:45", "18:00", "18:15"},
                         "spread over the day's free times")
        hits = self.place([P.Need(f"c{i}", GH, "d1.g1.s1", [P.Alt("check", 5)]) for i in range(7)])
        days = [local(h).date() for h in hits]
        self.assertEqual(days.count(date(2026, 10, 5)), P.MAX_A_DAY)
        self.assertEqual(days[-1], date(2026, 10, 6))

    def test_the_weekly_limit(self):
        hits = self.place([lesson_need(f"l{i}") for i in range(4)], rules(week_minutes=45))
        self.assertEqual([local(h).date() for h in hits], [date(2026, 10, 5)] * 3 + [date(2026, 10, 12)])

    def test_a_day_off_stays_off(self):
        everyday = [{"label": "Late morning", "days": list(range(7)), "start": "10:00", "end": "12:00", "audio_only": False}]
        hits = self.place([P.Need(f"c{i}", GH, "d1.g1.s1", [P.Alt("check", 5)]) for i in range(10)],
                          rules(windows=everyday), now=berlin(2026, 10, 10, 7))
        days = [local(h).date() for h in hits]
        self.assertEqual(days.count(date(2026, 10, 10)), 6)
        self.assertNotIn(date(2026, 10, 11), days, "Sunday off")
        self.assertEqual(days[6:], [date(2026, 10, 12)] * 4)

    def test_nothing_before_the_earliest_hour_or_before_now(self):
        hits = self.place([lesson_need("a"), P.Need("b", GH, "d1.g1.s1", [P.Alt("listen", 10, audio=True)])], rules(earliest="13:00"))
        self.assertEqual(local(hits[0]), datetime(2026, 10, 5, 13, 0))
        self.assertIsNone(hits[1], "the commute ends before 13:00")
        hits = self.place([lesson_need("a")], now=berlin(2026, 10, 5, 12, 40))
        self.assertEqual(local(hits[0]), datetime(2026, 10, 5, 12, 45))

    def test_a_later_check_waits_for_its_time_and_an_exam_for_its_week(self):
        later = P.Need("later", GH, "d1.g1.s1", [P.Alt("later", 5)], not_before=berlin(2026, 10, 6, 14))
        self.assertEqual(local(self.place([later])[0]), datetime(2026, 10, 6, 18, 0))
        full, short = (exam_spec(gh_package(), n)["minutes"] for n in ("full", "short"))
        self.assertTrue(60 < full <= 120 and short <= 60, (full, short))
        exam = P.Need("exam", GH, None, [P.Alt("exam", full, length="full"), P.Alt("exam", short, length="short")],
                      not_before=P.day_start(date(2026, 10, 5)), not_after=P.day_start(date(2026, 10, 12)), in_order=True)
        alt, at = self.place([exam])[0]
        self.assertEqual((alt.length, P.to_local(at)), ("full", datetime(2026, 10, 10, 10, 0)), "only Saturday has room for it")
        alt, at = self.place([exam], rules(day_minutes=[60] * 6 + [0]))[0]
        self.assertEqual((alt.length, P.to_local(at)), ("short", datetime(2026, 10, 5, 12, 30)))
        # A week with no room for either stays without one: the exam of that week is not pushed into the next.
        blocked = [P.Need(f"b{i}", GH, "d1.g1.s1", [P.Alt("lesson", 15)]) for i in range(24)]
        hits = self.place(blocked + [exam], rules(day_minutes=[60] * 6 + [0]))
        self.assertIsNone(hits[-1])
        self.assertTrue(all(hits[:-1]))

    @staticmethod
    def night(start: str, end: str) -> dict:
        return rules(day_minutes=[0] * 6 + [180], earliest="00:00",
                     windows=[{"label": "Night", "days": [6], "start": start, "end": end, "audio_only": False}])

    def test_nothing_in_the_hour_the_clocks_skip_in_spring(self):
        """28 March 2027, the switch before the exams: at 02:00 the clocks go to 03:00, so that night a free time
        from 02:00 to 03:00 does not exist, and a session over the change ends an hour later on the clock."""
        check = lambda i, minutes: P.Need(f"c{i}", GH, "d1.g1.s1", [P.Alt("check", minutes)])  # noqa: E731
        saturday = berlin(2027, 3, 27, 12)
        hits = self.place([check(0, 5), check(1, 55)], self.night("02:00", "03:00"), now=saturday)
        self.assertEqual([local(h) for h in hits], [datetime(2027, 4, 4, 2, 0), datetime(2027, 4, 4, 2, 5)],
                         "not on 28 March, whose 02:00 is 03:00: a week later")
        r = self.night("01:00", "04:00")
        hits = self.place([check(0, 45), check(1, 45), check(2, 10), check(3, 45)], r, now=saturday)
        self.assertEqual([local(h) for h in hits], [datetime(2027, 3, 28, 1, 0), datetime(2027, 3, 28, 1, 45),
                                                    datetime(2027, 3, 28, 3, 30), datetime(2027, 4, 4, 1, 0)])
        self.assertEqual(P.to_local(hits[1][1] + timedelta(minutes=45)), datetime(2027, 3, 28, 3, 30), "45 minutes, over the change")
        board = P.Board(r, saturday)
        for alt, at in hits:
            self.assertTrue(board.fits({"start": iso(at), "minutes": alt.minutes, "kind": "check"}), P.to_local(at))
        for a, b in zip(hits, hits[1:]):
            self.assertLessEqual(a[1] + timedelta(minutes=a[0].minutes), b[1], "no two sessions at once")
        # From 01:45, 45 minutes end at 03:30: outside a free time that ends at 03:00.
        self.assertFalse(P.Board(self.night("01:00", "03:00"), saturday).fits(
            {"start": iso(berlin(2027, 3, 28, 1, 45)), "minutes": 45, "kind": "check"}))
        self.assertEqual(local(self.place([check(0, 45), check(1, 45)], self.night("01:00", "03:00"), now=saturday)[1]),
                         datetime(2027, 4, 4, 1, 0), "the second one would end at 03:30 or later on 28 March")
        # 02:00 read as a time is 03:00, so a session placed "at 02:00" would sit on one that is at 03:00.
        hits = self.place([check(0, 30), check(1, 5)], self.night("02:00", "04:00"), now=saturday)
        self.assertEqual([local(h) for h in hits], [datetime(2027, 3, 28, 3, 0), datetime(2027, 3, 28, 3, 30)])

    def test_the_hour_that_repeats_in_autumn(self):
        """31 October 2027: at 03:00 the clocks go back to 02:00. An hour from the first 02:00 ends at the second
        02:00, so it is inside a free time from 02:00 to 03:00."""
        saturday = berlin(2027, 10, 30, 12)
        hits = self.place([P.Need(f"c{i}", GH, "d1.g1.s1", [P.Alt("check", 60)]) for i in range(2)], self.night("02:00", "03:00"),
                          now=saturday)
        self.assertEqual(hits[0][1], utc(2027, 10, 31, 0, 0), "the first 02:00, in summer time")
        self.assertEqual(local(hits[1]), datetime(2027, 11, 7, 2, 0), "the wall clock's hour is taken once")
        self.assertTrue(P.Board(self.night("02:00", "03:00"), saturday).fits({"start": iso(hits[0][1]), "minutes": 60, "kind": "check"}))
        # From the first 02:30 an hour ends at the second 02:30, but it runs past 02:45 before the clocks go back.
        self.assertFalse(P.Board(self.night("02:00", "02:45"), saturday).fits({"start": iso(utc(2027, 10, 31, 0, 30)), "minutes": 60, "kind": "check"}))
        # A wall-clock time in that hour is its first moment. In the second hour those are past, and before a
        # check that comes due in the second hour.
        five = P.Need("c", GH, "d1.g1.s1", [P.Alt("check", 5)])
        self.assertEqual(local(self.place([five], self.night("02:00", "03:00"), now=utc(2027, 10, 31, 1, 10))[0]), datetime(2027, 11, 7, 2, 0))
        later = P.Need("later", GH, "d1.g1.s1", [P.Alt("later", 5)], not_before=utc(2027, 10, 31, 1, 10))
        self.assertEqual(local(self.place([later], self.night("02:00", "03:00"), now=saturday)[0]), datetime(2027, 11, 7, 2, 0))
        later.not_before = utc(2027, 10, 31, 0, 10)
        self.assertEqual(self.place([later], self.night("02:00", "03:00"), now=saturday)[0][1], utc(2027, 10, 31, 0, 10))

    @staticmethod
    def check(i: int, minutes: int, **kw) -> P.Need:
        return P.Need(f"c{i}", GH, "d1.g1.s1", [P.Alt("check", minutes)], **kw)

    def assert_apart(self, hits: list) -> None:
        spans = sorted((at, at + timedelta(minutes=alt.minutes)) for alt, at in hits)
        for a, b in zip(spans, spans[1:]):
            self.assertLessEqual(a[1], b[0], "no two sessions at once")

    def test_a_session_may_run_on_into_the_hour_that_repeats(self):
        """31 October 2027: 45 minutes from the first 02:30 end at the second 02:15, before 03:00. A free time from
        02:00 to 03:00 ends at the one 03:00, an hour after the change, so they fit in it."""
        saturday = berlin(2027, 10, 30, 12)
        night = self.night("02:00", "03:00")
        hits = self.place([self.check(0, 30), self.check(1, 45), self.check(2, 5)], night, now=saturday)
        self.assertEqual([h[1] for h in hits[:2]], [utc(2027, 10, 31, 0, 0), utc(2027, 10, 31, 0, 30)])
        self.assertEqual(P.to_local(hits[1][1] + timedelta(minutes=45)), datetime(2027, 10, 31, 2, 15))
        self.assertEqual(local(hits[2]), datetime(2027, 11, 7, 2, 0), "a start in that hour is its first moment, and those are taken")
        self.assert_apart(hits)

        def fits(start: datetime, minutes: int, r: dict = night) -> bool:
            return P.Board(r, saturday).fits({"start": iso(start), "minutes": minutes, "kind": "check"})
        self.assertTrue(all(fits(at, alt.minutes) for alt, at in hits[:2]))
        self.assertTrue(fits(utc(2027, 10, 31, 0, 30), 90), "from the first 02:30, 90 minutes end at 03:00")
        self.assertFalse(fits(utc(2027, 10, 31, 0, 30), 95))
        # A free time that ends in the hour that repeats ends the first time round.
        self.assertTrue(fits(utc(2027, 10, 31, 0, 30), 15, self.night("02:00", "02:45")))
        self.assertFalse(fits(utc(2027, 10, 31, 0, 30), 45, self.night("02:00", "02:45")))
        # One that starts in it starts the first time round, and runs on over the change.
        self.assertTrue(fits(utc(2027, 10, 31, 0, 30), 60, self.night("02:30", "04:00")))
        # After a session over the change the next one may start at 03:00: the wall clock was there just before it.
        hits = self.place([self.check(0, 30), self.check(1, 45), self.check(2, 60)], self.night("02:00", "04:00"), now=saturday)
        self.assertEqual([h[1] for h in hits], [utc(2027, 10, 31, 0, 0), utc(2027, 10, 31, 0, 30), utc(2027, 10, 31, 2, 0)])
        self.assert_apart(hits)

    def test_no_two_sessions_at_once_over_the_spring_change(self):
        """28 March 2027: from 01:30 an hour ends at 03:30 on the clock, so it runs over a check at 03:00."""
        saturday = berlin(2027, 3, 27, 12)
        r = self.night("01:00", "05:00")
        hits = self.place([self.check(0, 5, not_before=utc(2027, 3, 28, 1, 0)), self.check(1, 30), self.check(2, 60)], r, now=saturday)
        self.assertEqual([local(h) for h in hits], [datetime(2027, 3, 28, 3, 0), datetime(2027, 3, 28, 1, 0), datetime(2027, 3, 28, 3, 5)])
        self.assert_apart(hits)
        board = P.Board(r, saturday)
        for alt, at in hits:
            self.assertTrue(board.fits({"start": iso(at), "minutes": alt.minutes, "kind": "check"}), P.to_local(at))
        # An hour from 01:00 ends as the clocks jump, the first moment they reach 02:00: it fits a free time to 02:00.
        self.assertEqual(self.place([self.check(0, 60)], self.night("01:00", "02:00"), now=saturday)[0][1], utc(2027, 3, 28, 0, 0))

    def test_a_session_over_midnight_holds_its_time_on_the_next_day(self):
        """Two hours put at 23:30 on Monday run to 01:30 on Tuesday. Nothing else goes there, and the 90 minutes
        after midnight count in Tuesday's time, not Monday's. From 23:30 on a Sunday they count in the next week."""
        lunch = {"label": "Lunch", "days": [0], "start": "12:00", "end": "13:00", "audio_only": False}
        night = {"label": "Night", "days": [1], "start": "00:00", "end": "04:00", "audio_only": False}
        pinned = berlin(2026, 10, 5, 23, 30)
        r = rules(earliest="00:00", day_minutes=[60, 120, 0, 0, 0, 0, 0], windows=[lunch, night])
        hits = self.place([self.check(i, 30) for i in range(3)], r, taken=[(pinned, 120)])
        self.assertEqual([local(h) for h in hits], [datetime(2026, 10, 5, 12, 0), datetime(2026, 10, 6, 1, 30), datetime(2026, 10, 12, 12, 0)],
                         "30 minutes left on Monday, and 30 on Tuesday after it")
        self.assert_apart(hits + [(P.Alt("exam", 120), pinned)])
        r = rules(earliest="00:00", day_minutes=[60, 0, 0, 0, 0, 0, 60], week_minutes=60, windows=[lunch])
        hits = self.place([self.check(i, 30) for i in range(3)], r, taken=[(berlin(2026, 10, 11, 23, 30), 60)])
        self.assertEqual([local(h) if h else None for h in hits], [datetime(2026, 10, 5, 12, 0), datetime(2026, 10, 12, 12, 0), None],
                         "30 minutes left in each week")
        # Into the night the clocks go back: three hours from 23:30 on Saturday end at the first 02:30.
        hits = self.place([self.check(0, 30)], self.night("02:00", "03:00"), now=berlin(2027, 10, 30, 12),
                          taken=[(berlin(2027, 10, 30, 23, 30), 180)])
        self.assertEqual(hits[0][1], utc(2027, 10, 31, 0, 30))


# ---------------------------------------------------------------- done is read from the Record

class DoneTests(unittest.TestCase):
    now = MON + timedelta(hours=12)

    def done(self, s: dict, *events: dict) -> bool:
        P.mark_done([s], list(events), self.now)
        return s["state"] == "done"

    def test_only_a_matching_event_after_planning_marks_a_session_done(self):
        at = MON + timedelta(hours=6)
        self.assertFalse(self.done(session("lesson", at)), "a missing event is never read as done")
        self.assertFalse(self.done(session("lesson", at),
                                   event("item.issued", at, package=GH, skill="d1.g1.s1", item=new_id("item"), mode="probe"),
                                   event("lesson.viewed", at, package=GH, skill="d1.g1.s2"),
                                   event("lesson.viewed", at, package=DP, skill="d1.g1.s1"),
                                   event("lesson.viewed", MON - timedelta(minutes=1), package=GH, skill="d1.g1.s1"),
                                   event("lesson.viewed", self.now + timedelta(minutes=1), package=GH, skill="d1.g1.s1"),
                                   event("podcast.declared", at, package=GH, skill="d1.g1.s1", heard=False)))
        s = session("listen", at)
        viewed = event("lesson.viewed", at, package=GH, skill="d1.g1.s1")
        self.assertTrue(self.done(s, viewed))
        self.assertEqual((s["done_at"], s["done_event"]), (viewed["at"], viewed["id"]))
        self.assertTrue(self.done(session("lesson", at), event("podcast.declared", at, package=GH, skill="d1.g1.s1", heard=True)))

    def test_practice_needs_an_answer_not_just_a_question(self):
        at = MON + timedelta(hours=6)
        issued = event("item.issued", at, package=GH, skill="d1.g1.s1", item=new_id("item"), mode="practice")
        self.assertFalse(self.done(session("practice", at), issued))
        self.assertTrue(self.done(session("practice", at), issued, event("answer.submitted", at, package=GH, skill="d1.g1.s1", mode="practice")))

    def test_a_check_needs_an_answer_without_help_and_a_later_check_one_the_evidence_counts_as_later(self):
        due = MON + timedelta(hours=5)
        answer = lambda at, mode="probe", item=None: event("answer.submitted", at, package=GH, skill="d1.g1.s1", mode=mode, item=item)  # noqa: E731
        self.assertFalse(self.done(session("check", due), answer(due, "practice")))
        self.assertTrue(self.done(session("check", due), answer(due - timedelta(hours=1))))
        # Whether an answer is a later check is the evidence's call (later_answers), not the planned time's.
        item = new_id("item")
        on_time = answer(due + timedelta(minutes=1), "check", item)
        for later in (None, set(), {(GH, new_id("item"))}, {(DP, item)}):
            s = session("later", due, not_before=due)
            P.mark_done([s], [on_time], self.now, later)
            self.assertEqual(s["state"], "planned", later)
        s = session("later", due, not_before=due)
        P.mark_done([s], [answer(due + timedelta(minutes=1), "practice", item)], self.now, {(GH, item)})
        self.assertEqual(s["state"], "planned", "practice is not a check")
        P.mark_done([s], [on_time], self.now, {(GH, item)})
        self.assertEqual((s["state"], s["done_event"]), ("done", on_time["id"]))

    def test_a_three_minute_check_is_one_run_of_answers(self):
        first, second = session("check3", MON + timedelta(hours=6), skill=None), session("check3", MON + timedelta(days=1), skill=None)
        other = session("check3", MON + timedelta(hours=6), skill=None, pkg=DP)
        at = MON + timedelta(hours=6)
        answers = [event("answer.submitted", at + timedelta(minutes=i), package=GH, skill=f"d1.g1.s{i + 1}", mode="check") for i in range(3)]
        P.mark_done([first, second, other], answers, self.now)
        self.assertEqual([s["state"] for s in (first, second, other)], ["done", "planned", "planned"])
        P.mark_done([first, second, other], answers + [event("answer.submitted", at + timedelta(hours=2), package=GH, skill="d1.g1.s1",
                                                             mode="check")], self.now)
        self.assertEqual(second["state"], "done", "a run two hours later is the next check")

    def test_one_event_marks_one_session(self):
        a, b = session("practice", MON + timedelta(hours=6)), session("practice", MON + timedelta(days=1))
        P.mark_done([b, a], [event("answer.submitted", MON + timedelta(hours=2), package=GH, skill="d1.g1.s1", mode="practice")], self.now)
        self.assertEqual((a["state"], b["state"]), ("done", "planned"), "the earliest planned one")

    def test_a_practice_exam_counts_when_it_was_started(self):
        at = MON + timedelta(hours=6)
        self.assertFalse(self.done(session("exam", at, skill=None), event("exam.started", at, package=DP)))
        self.assertTrue(self.done(session("exam", at, skill=None), event("exam.started", at, package=GH)))

    def test_a_lab_is_done_when_it_was_checked_in_the_database(self):
        at, sid = MON + timedelta(hours=6), LABS["vector"]["skill"]

        def lab() -> dict:
            return session("lab", at, pkg=DP, skill=sid, minutes=P.LAB_MIN, lab="vector")

        work = [event(t, at, package=DP, skill=sid, lab="vector") for t in ("lab.opened", "lab.ran", "lab.hint", "lab.reset")]
        self.assertFalse(self.done(lab(), *work), "opened, run and hinted, but not checked")
        self.assertFalse(self.done(lab(), event("lab.checked", at, package=DP, skill=LABS["search"]["skill"], lab="search", passed=True)),
                         "another lab")
        self.assertFalse(self.done(lab(), event("lab.checked", at, package=GH, skill=sid, lab="vector", passed=True)))
        self.assertTrue(self.done(lab(), event("lab.checked", at, package=DP, skill=sid, lab="vector", passed=False)), "passed or not")


# ---------------------------------------------------------------- keeping the plan current

class ReconcileTests(unittest.TestCase):
    ACTIVE = {GH: None, DP: None}

    def doc(self, r: dict | None = None) -> dict:
        return {"rules": r or rules(), "sessions": [], "moves": [], "skips": {}, "unplaced": 0}

    def at(self, doc: dict, key: str) -> datetime | None:
        s = next(s for s in doc["sessions"] if s["key"] == key)
        return P.to_local(P._start(s)) if s["start"] else None

    def test_nothing_changes_while_nothing_happens(self):
        doc, needs = self.doc(), [lesson_need(f"l{i}") for i in range(6)]
        P.reconcile(doc, needs, [], MON, self.ACTIVE)
        before = deepcopy(doc["sessions"])
        P.reconcile(doc, needs, [], MON + timedelta(minutes=30), self.ACTIVE)
        self.assertEqual(doc["sessions"], before)
        self.assertEqual(doc["moves"], [])

    def test_a_missed_session_moves_to_the_next_free_slot_and_keeps_its_event(self):
        doc, needs = self.doc(), [lesson_need("a")]
        P.reconcile(doc, needs, [], MON, self.ACTIVE)
        s = doc["sessions"][0]
        self.assertEqual(self.at(doc, "a"), datetime(2026, 10, 5, 12, 30))
        P.reconcile(doc, needs, [], berlin(2026, 10, 5, 12, 50), self.ACTIVE)
        self.assertEqual(doc["sessions"], [s])
        self.assertEqual((self.at(doc, "a"), s["seq"], s["state"]), (datetime(2026, 10, 5, 12, 55), 1, "planned"))
        self.assertEqual([(m["session"], m["why"]) for m in doc["moves"]], [(s["id"], "missed")])
        # Done a little later: done stays done, and nothing moves again.
        events = [event("lesson.viewed", berlin(2026, 10, 5, 13, 0), package=GH, skill="d1.g1.s1")]
        P.reconcile(doc, needs, events, berlin(2026, 10, 5, 14, 0), self.ACTIVE)
        self.assertEqual((s["state"], s["seq"], len(doc["moves"])), ("done", 1, 1))

    def test_a_missed_session_waits_when_no_slot_is_left(self):
        r = rules(day_minutes=[15, 0, 0, 0, 0, 0, 0], windows=[{"label": "", "days": [0], "start": "12:30", "end": "12:45"}])
        doc, needs = self.doc(r), [lesson_need("a")]
        P.reconcile(doc, needs, [], MON, self.ACTIVE)
        for week in (1, 2):
            mine = session("lesson", berlin(2026, 10, 5 + 7 * week, 12, 30), minutes=15)
            mine.update(by="you", pinned=True)
            doc["sessions"].append(mine)
        P.reconcile(doc, needs, [], berlin(2026, 10, 5, 13), self.ACTIVE)
        s = next(s for s in doc["sessions"] if s["key"] == "a")
        self.assertEqual((s["start"], s["state"]), (None, "planned"))
        self.assertEqual(doc["moves"][-1]["to"], None)

    def test_the_next_day_keeps_its_times_and_later_days_are_placed_again(self):
        doc, needs = self.doc(), [lesson_need(k) for k in "abcde"]
        P.reconcile(doc, needs, [], MON, self.ACTIVE)
        before = {s["key"]: (s["id"], s["start"]) for s in doc["sessions"]}
        self.assertEqual(self.at(doc, "e"), datetime(2026, 10, 6, 12, 30))
        P.reconcile(doc, [lesson_need("z")] + needs, [], MON + timedelta(minutes=5), self.ACTIVE)
        after = {s["key"]: (s["id"], s["start"], s["seq"]) for s in doc["sessions"]}
        for k in "abcd":
            self.assertEqual(after[k], (*before[k], 0), "within a day: kept")
        self.assertEqual(self.at(doc, "z"), datetime(2026, 10, 6, 12, 30))
        self.assertEqual((after["e"][0], after["e"][2], self.at(doc, "e")), (before["e"][0], 1, datetime(2026, 10, 6, 18, 0)),
                         "the same event, moved")
        self.assertEqual(doc["moves"], [], "Dojo re-planning ahead is not a move to report")

    def test_what_is_no_longer_needed_leaves_the_plan(self):
        doc = self.doc()
        P.reconcile(doc, [lesson_need("a"), lesson_need("b")], [], MON, self.ACTIVE)
        P.reconcile(doc, [lesson_need("b")], [], MON + timedelta(minutes=5), self.ACTIVE)
        self.assertEqual([s["key"] for s in doc["sessions"]], ["b"])
        P.reconcile(doc, [lesson_need("b")], [], MON + timedelta(minutes=5), {DP: None})
        self.assertEqual([s["key"] for s in doc["sessions"]], ["b"], "an exam's sessions go only once they are missed")

    def test_what_the_learner_placed_keeps_its_time(self):
        doc = self.doc()
        mine = session("lesson", berlin(2026, 10, 5, 22, 0), minutes=15)
        mine.update(by="you", pinned=True)
        doc["sessions"].append(mine)
        P.reconcile(doc, [], [], MON, self.ACTIVE)
        self.assertEqual(doc["sessions"], [mine], "outside every free time and no longer asked for: the learner's choice")

    def test_a_session_a_source_change_leaves_leading_nowhere_goes_even_when_the_learner_placed_it(self):
        """ADR 0006: pinned, moved or missed, a session of a skill whose every cited page is gone goes, except
        its lab, and so do the Lesson and Listen of a lesson being written again. The move says why. What was
        done stays, and so does a session of the learner's that is only no longer asked for."""
        gone, held, lab_skill = "d1.g1.s1", "d1.g1.s2", LABS["vector"]["skill"]
        doc, at = self.doc(), berlin(2026, 10, 6, 21, 0)

        def mine(kind: str, skill: str, hours: int, pkg: str = GH, **kw) -> dict:
            s = session(kind, at + timedelta(hours=hours), pkg=pkg, skill=skill, **kw)
            s.update(by="you", pinned=True)
            return s
        practice, later, moved, missed = (mine("practice", gone, 0), mine("later", gone, 1), mine("check", gone, 2),
                                          mine("practice", gone, -34))
        moved["pinned"] = False   # moved there by the learner, not pinned
        lab = mine("lab", lab_skill, 3, pkg=DP, minutes=P.LAB_MIN, lab="vector")
        lesson, listen, check = mine("lesson", held, 4), mine("listen", held, 5), mine("check", held, 6)
        kept = mine("lesson", "d1.g2.s1", 7)
        done = session("lesson", MON + timedelta(hours=1), skill=gone)
        doc["sessions"] = [practice, later, moved, missed, lab, lesson, listen, check, kept, done]
        events = [event("lesson.viewed", MON + timedelta(hours=1, minutes=5), package=GH, skill=gone)]
        off = {(GH, gone): "gone", (DP, lab_skill): "gone", (GH, held): "rewriting"}
        P.reconcile(doc, [], events, MON + timedelta(hours=5), self.ACTIVE, None, None, off)
        self.assertEqual({s["id"]: s["state"] for s in doc["sessions"]},
                         {lab["id"]: "planned", check["id"]: "planned", kept["id"]: "planned", done["id"]: "done"})
        self.assertEqual((missed["state"], missed["dropped_reason"]), ("dropped", "gone"), "missed, and not moved on")
        self.assertEqual({m["session"]: (m["why"], m["reason"], m["to"]) for m in doc["moves"]},
                         {**{s["id"]: ("removed", "gone", None) for s in (practice, later, moved, missed)},
                          **{s["id"]: ("removed", "rewriting", None) for s in (lesson, listen)}})

    def test_a_change_of_rules_moves_what_no_longer_fits_unless_it_is_pinned(self):
        doc, needs = self.doc(), [lesson_need("a"), lesson_need("b")]
        P.reconcile(doc, needs, [], MON, self.ACTIVE)
        self.assertEqual((self.at(doc, "a"), self.at(doc, "b")), (datetime(2026, 10, 5, 12, 30), datetime(2026, 10, 5, 18, 0)))
        next(s for s in doc["sessions"] if s["key"] == "b")["pinned"] = True
        doc["rules"] = rules(windows=[w for w in P.DEFAULT_RULES["windows"] if w["label"] not in ("Lunch", "Evening")]
                             + [{"label": "Late", "days": P.WEEKDAYS, "start": "19:00", "end": "21:00", "audio_only": False}])
        P.reconcile(doc, needs, [], MON + timedelta(minutes=5), self.ACTIVE)
        self.assertEqual((self.at(doc, "a"), self.at(doc, "b")), (datetime(2026, 10, 5, 19, 0), datetime(2026, 10, 5, 18, 0)))
        self.assertEqual([m["why"] for m in doc["moves"]], ["rules"])

    def test_done_sessions_use_up_the_day(self):
        doc = self.doc()
        P.reconcile(doc, [lesson_need("a", minutes=45)], [], MON, self.ACTIVE)
        done_at = berlin(2026, 10, 5, 12, 40)
        P.reconcile(doc, [lesson_need("a", minutes=45), lesson_need("b", minutes=20)],
                    [event("lesson.viewed", done_at, package=GH, skill="d1.g1.s1")], berlin(2026, 10, 5, 13, 40), self.ACTIVE)
        self.assertEqual(self.at(doc, "b"), datetime(2026, 10, 6, 12, 30), "45 + 20 is more than the 60 minutes of Monday")

    def test_a_missed_three_minute_check_is_not_carried_over(self):
        def daily(d: date) -> P.Need:
            return P.Need(f"check3:{GH}:{d}", GH, None, [P.Alt("check3", 3)], not_before=P.day_start(d),
                          not_after=P.day_start(d + timedelta(days=1)))
        doc = self.doc()
        P.reconcile(doc, [daily(date(2026, 10, 5 + i)) for i in range(3)], [], MON, self.ACTIVE)
        before = {s["key"]: (s["id"], s["start"]) for s in doc["sessions"]}
        self.assertEqual(len(before), 3)
        P.reconcile(doc, [daily(date(2026, 10, 6 + i)) for i in range(3)], [], berlin(2026, 10, 5, 23), self.ACTIVE)
        after = {s["key"]: (s["id"], s["start"]) for s in doc["sessions"]}
        self.assertNotIn(f"check3:{GH}:2026-10-05", after, "one comes every day; the next day's stays as it was")
        for k in (f"check3:{GH}:2026-10-06", f"check3:{GH}:2026-10-07"):
            self.assertEqual(after[k], before[k])
        self.assertEqual(doc["moves"], [])

    def test_a_three_minute_check_the_learner_placed_moves_as_the_check_of_its_new_day(self):
        doc = self.doc()
        mine = session("check3", berlin(2026, 10, 5, 22, 0), skill=None, minutes=3)
        mine.update(by="you", pinned=True)
        doc["sessions"].append(mine)
        P.reconcile(doc, [], [], berlin(2026, 10, 5, 23), self.ACTIVE)
        self.assertEqual((mine["state"], mine["key"], P.to_local(P._start(mine))),
                         ("planned", f"check3:{GH}:2026-10-06", datetime(2026, 10, 6, 12, 30)))
        wed, late = (session("check3", at, skill=None, minutes=3) for at in (berlin(2026, 10, 7, 12, 30), berlin(2026, 10, 6, 21, 0)))
        for s in (wed, late):
            s.update(by="you", pinned=True)
        doc["sessions"] += [wed, late]
        P.reconcile(doc, [], [], berlin(2026, 10, 6, 21, 30), self.ACTIVE)
        self.assertEqual((mine["state"], wed["state"], late["state"]), ("dropped", "planned", "dropped"),
                         "missed again, and Wednesday has its check already")

    @staticmethod
    def later(due: datetime) -> P.Need:
        return P.Need(f"later:{GH}:d1.g1.s1", GH, "d1.g1.s1", [P.Alt("later", 5)], not_before=due)

    @staticmethod
    def attempt(at: datetime, mode: str = "check", met: int = 3, judged: bool = True) -> list[dict]:
        """An item without hints: issued at `at`, answered 3 minutes later and judged a minute after that."""
        item, sid = new_id("item"), "d1.g1.s1"
        out = [event("item.issued", at, package=GH, skill=sid, item=item, mode=mode, kind="short"),
               event("answer.submitted", at + timedelta(minutes=3), package=GH, skill=sid, item=item, mode=mode, hints_seen=0)]
        if judged:
            out.append(event("answer.judged", at + timedelta(minutes=4), package=GH, skill=sid, item=item, mode=mode, met=met, total=3))
        return out

    def reconcile(self, doc: dict, due: datetime, events: list[dict], now: datetime) -> None:
        """One refresh as the planner does it: the answers counted as later checks come from the evidence."""
        later = P.later_answers(GH, derive(events, gh_package(), 20), 20)
        P.reconcile(doc, [self.later(due)], events, now, self.ACTIVE, later)

    # Met without help at 16:00 on Sunday: the later check comes due at 12:00 on Monday and is planned at 12:30.
    TAUGHT = berlin(2026, 10, 4, 15, 56)

    def test_a_later_check_follows_its_due_time_when_the_skill_is_worked_on_again(self):
        doc, first = self.doc(), self.attempt(self.TAUGHT, "probe")
        self.reconcile(doc, berlin(2026, 10, 5, 12), first, MON)
        s = doc["sessions"][0]
        self.assertEqual((self.at(doc, s["key"]), s["not_before"]), (datetime(2026, 10, 5, 12, 30), iso(berlin(2026, 10, 5, 12))))
        # A question answered in Ask at 09:00 restarts the skill's clock: the check comes due at 05:00 on Tuesday.
        asked = first + [event("ask.answered", berlin(2026, 10, 5, 9), package=GH, skill="d1.g1.s1")]
        self.reconcile(doc, berlin(2026, 10, 6, 5), asked, berlin(2026, 10, 5, 9, 5))
        self.assertEqual(doc["sessions"], [s])
        self.assertEqual((s["seq"], self.at(doc, s["key"]), s["not_before"]), (1, datetime(2026, 10, 6, 12, 30), iso(berlin(2026, 10, 6, 5))))
        self.assertEqual([m["why"] for m in doc["moves"]], ["due"], "it was less than a day away, so the move is shown")
        # An answer at the old time is too soon after that to be a later check, so it does not make the session done.
        self.reconcile(doc, berlin(2026, 10, 6, 8, 32), asked + self.attempt(berlin(2026, 10, 5, 12, 28)), berlin(2026, 10, 5, 13))
        self.assertEqual((s["state"], self.at(doc, s["key"])), ("planned", datetime(2026, 10, 6, 12, 30)))

    def test_an_answer_soon_after_new_teaching_does_not_do_the_later_check_in_the_same_refresh(self):
        """The reviewers' case: the lesson is read again at 09:00 and the check answered at 12:31, and the plan sees
        both at once. The later check planned at 12:30 (due 12:00) is not done: the evidence does not count that
        answer as a later check, as the last teaching before it was that morning. It moves to when it comes due."""
        doc, first = self.doc(), self.attempt(self.TAUGHT, "probe")
        self.reconcile(doc, berlin(2026, 10, 5, 12), first, MON)
        s = doc["sessions"][0]
        self.assertEqual((self.at(doc, s["key"]), s["not_before"], s["seq"]), (datetime(2026, 10, 5, 12, 30), iso(berlin(2026, 10, 5, 12)), 0))
        viewed = event("lesson.viewed", berlin(2026, 10, 5, 9), package=GH, skill="d1.g1.s1", lesson=new_id("lesson"), modality="read")
        answer = self.attempt(berlin(2026, 10, 5, 12, 28), judged=False)
        self.assertEqual(answer[-1]["at"], iso(berlin(2026, 10, 5, 12, 31)))
        self.reconcile(doc, berlin(2026, 10, 6, 5), first + [viewed] + answer, berlin(2026, 10, 5, 13))
        self.assertEqual((s["state"], s["done_event"], s["not_before"]), ("planned", None, iso(berlin(2026, 10, 6, 5))))
        self.assertEqual((self.at(doc, s["key"]), s["seq"]), (datetime(2026, 10, 6, 12, 30), 1), "its calendar event moves")
        self.assertEqual([(m["why"], m["from"]) for m in doc["moves"]], [("due", iso(berlin(2026, 10, 5, 12, 30)))])
        # Its judgement arrives, and with it feedback that restarts the clock: still not a later check.
        judged = event("answer.judged", berlin(2026, 10, 5, 12, 32), package=GH, skill="d1.g1.s1", item=answer[0]["data"]["item"],
                       mode="check", met=3, total=3)
        self.reconcile(doc, berlin(2026, 10, 6, 8, 32), first + [viewed] + answer + [judged], berlin(2026, 10, 5, 13, 5))
        self.assertEqual((s["state"], self.at(doc, s["key"])), ("planned", datetime(2026, 10, 6, 12, 30)))

    def test_a_later_check_answered_in_time_is_done_although_its_feedback_restarts_the_clock(self):
        doc, first = self.doc(), self.attempt(self.TAUGHT, "probe")
        self.reconcile(doc, berlin(2026, 10, 5, 12), first, MON)
        s = doc["sessions"][0]
        issued, submitted, judged = self.attempt(berlin(2026, 10, 5, 12, 28), met=2)
        # Answered 20 hours 31 minutes after the last teaching, but not judged yet: a missing event does not count.
        self.reconcile(doc, berlin(2026, 10, 5, 12), first + [issued, submitted], berlin(2026, 10, 5, 12, 32))
        self.assertEqual((s["state"], self.at(doc, s["key"])), ("planned", datetime(2026, 10, 5, 12, 30)))
        # Judged, with not all points met: the Record asks for another later check, 20 hours after the feedback.
        self.reconcile(doc, berlin(2026, 10, 6, 8, 32), first + [issued, submitted, judged], berlin(2026, 10, 5, 13))
        self.assertEqual((s["state"], s["done_event"], s["done_at"], doc["moves"]),
                         ("done", submitted["id"], iso(berlin(2026, 10, 5, 12, 31)), []))

    def test_a_disputed_answer_does_not_do_the_later_check(self):
        """The reviewers' case: the check is answered at the planned time and judged 20 hours 32 minutes after the
        teaching, and then disputed before the plan refreshes. The evidence no longer counts that answer, so the
        later check is not done: it moves to when the check comes due, 20 hours after that answer's feedback."""
        doc, first = self.doc(), self.attempt(self.TAUGHT, "probe")
        self.reconcile(doc, berlin(2026, 10, 5, 12), first, MON)
        s = doc["sessions"][0]
        answer = self.attempt(berlin(2026, 10, 5, 12, 28))
        kept = deepcopy(doc)
        self.reconcile(kept, berlin(2026, 10, 6, 8, 32), first + answer, berlin(2026, 10, 5, 13))
        self.assertEqual(kept["sessions"][0]["state"], "done", "the same answer, not disputed, does the later check")
        disputed = first + answer + [event("dispute.filed", berlin(2026, 10, 5, 12, 40), package=GH, skill="d1.g1.s1",
                                           item=answer[0]["data"]["item"], reason="The answer names both settings.")]
        self.assertFalse(derive(disputed, gh_package(), 20)["d1.g1.s1"]["checks"]["later"])
        self.reconcile(doc, berlin(2026, 10, 6, 8, 32), disputed, berlin(2026, 10, 5, 13))
        self.assertEqual((s["state"], s["done_event"], self.at(doc, s["key"])), ("planned", None, datetime(2026, 10, 6, 12, 30)))
        self.assertEqual([(m["why"], m["from"]) for m in doc["moves"]], [("due", iso(berlin(2026, 10, 5, 12, 30)))])

    def test_a_lab_waits_for_a_desk_and_never_takes_an_audio_only_time(self):
        lab = P.Need(f"lab:{DP}:vector", DP, LABS["vector"]["skill"], [P.Alt("lab", P.LAB_MIN)], lab="vector")
        commute = [w for w in P.DEFAULT_RULES["windows"] if w["audio_only"]]
        doc = self.doc(rules(windows=commute))
        P.reconcile(doc, [lab], [], MON, self.ACTIVE)
        self.assertEqual((doc["sessions"], doc["unplaced"]), ([], 1), "only the commute is free: the lab waits for a desk")
        doc["rules"] = rules(windows=commute + [{"label": "Late", "days": [1], "start": "19:00", "end": "20:00"}])
        P.reconcile(doc, [lab], [], MON + timedelta(minutes=5), self.ACTIVE)
        s = doc["sessions"][0]
        self.assertEqual((s["kind"], s["lab"], s["minutes"], self.at(doc, lab.key)), ("lab", "vector", 20, datetime(2026, 10, 6, 19, 0)))
        # Missed, with no desk time left that day: it moves to the next desk time a week later, past the commutes in between.
        P.reconcile(doc, [lab], [], berlin(2026, 10, 6, 19, 45), self.ACTIVE)
        self.assertEqual((self.at(doc, lab.key), [m["why"] for m in doc["moves"]]), (datetime(2026, 10, 13, 19, 0), ["missed"]))

    def test_nothing_of_an_exam_is_planned_on_its_exam_day_or_after(self):
        doc = self.doc()
        P.reconcile(doc, [lesson_need(k) for k in "abcdefgh"], [], MON, self.ACTIVE)
        self.assertEqual({self.at(doc, k).date() for k in "efgh"}, {date(2026, 10, 6)})
        mine = session("lesson", berlin(2026, 10, 6, 22, 0), minutes=15)
        mine.update(by="you", pinned=True)
        doc["sessions"].append(mine)
        # The exam is now on Tuesday: Tuesday's sessions go, and one missed on Monday moves within Monday or waits.
        tuesday = {GH: P.day_start(date(2026, 10, 6)), DP: None}
        needs = [lesson_need(k, not_after=tuesday[GH]) for k in "abcdefgh"]
        P.reconcile(doc, needs, [], berlin(2026, 10, 5, 12, 50), tuesday)
        self.assertEqual({s["key"] for s in doc["sessions"]}, set("abcd") | {mine["key"]}, "the learner's own session stays")
        self.assertEqual((self.at(doc, "a"), doc["unplaced"]), (datetime(2026, 10, 5, 13, 0), 4))
        P.reconcile(doc, needs, [], berlin(2026, 10, 5, 21, 30), tuesday)
        self.assertEqual([self.at(doc, k) for k in "abcd"], [None] * 4, "missed, and no free time is left before the exam")

    def test_a_session_put_over_midnight_moves_dojos_one_from_under_it(self):
        """Two hours the learner puts at 23:30 on Monday run to 01:30 on Tuesday. Dojo's lesson at 00:00 on Tuesday
        is less than a day away, but it cannot stay under them: it moves to right after them, and nothing new goes there."""
        night = {"label": "Night", "days": [1], "start": "00:00", "end": "02:00", "audio_only": False}
        doc = self.doc(rules(earliest="00:00", day_minutes=[60, 150, 60, 60, 60, 0, 0], windows=[night]))
        P.reconcile(doc, [lesson_need("a")], [], MON, self.ACTIVE)
        self.assertEqual(self.at(doc, "a"), datetime(2026, 10, 6, 0, 0))
        mine = session("exam", berlin(2026, 10, 5, 23, 30), skill=None, minutes=120)
        mine.update(by="you", pinned=True)
        doc["sessions"].append(mine)
        P.reconcile(doc, [lesson_need("a"), lesson_need("b")], [], berlin(2026, 10, 5, 12), self.ACTIVE)
        self.assertEqual((self.at(doc, "a"), self.at(doc, "b")), (datetime(2026, 10, 6, 1, 30), datetime(2026, 10, 6, 1, 45)))
        self.assertEqual([m["why"] for m in doc["moves"]], ["taken"])
        spans = sorted((P._start(s), P._start(s) + timedelta(minutes=s["minutes"])) for s in doc["sessions"])
        self.assertTrue(all(x[1] <= y[0] for x, y in zip(spans, spans[1:])), "no two sessions at once")

    def test_dojo_moves_its_session_from_under_one_the_learner_put_there(self):
        """Within a day Dojo keeps its sessions at their time, but not under one the learner put there."""
        doc = self.doc()
        P.reconcile(doc, [lesson_need("a")], [], MON, self.ACTIVE)
        self.assertEqual(self.at(doc, "a"), datetime(2026, 10, 5, 12, 30))
        mine = session("lesson", berlin(2026, 10, 5, 12, 35), minutes=15)
        mine.update(by="you", pinned=True)
        doc["sessions"].append(mine)
        P.reconcile(doc, [lesson_need("a")], [], MON + timedelta(minutes=5), self.ACTIVE)
        self.assertEqual((self.at(doc, "a"), [m["why"] for m in doc["moves"]]), (datetime(2026, 10, 5, 12, 50), ["taken"]))


class FairShareTests(unittest.TestCase):
    def test_the_exam_with_more_to_show_per_week_gets_more_time(self):
        queue = lambda pid: [P.Need(f"{pid}{i}", pid, "s", [P.Alt("lesson", 15)]) for i in range(10)]  # noqa: E731
        order = [n.package for n in P._fair({"a": (queue("a"), 2.0), "b": (queue("b"), 1.0)})[:9]]
        self.assertEqual((order.count("a"), order.count("b")), (6, 3))
        self.assertEqual(len(P._fair({"a": (queue("a"), 2.0), "b": ([], 1.0)})), 10)


# ---------------------------------------------------------------- the planner on a real Record

class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.now = ahead()
        self.today = P.to_local(self.now).date()
        self.gh, self.dp = self.w.skills(GH), self.w.skills(DP)
        self.w.exam_dates(gh_300=self.today + timedelta(days=19), dp_800=self.today + timedelta(days=150))

    def record(self) -> None:
        """Both exams under way: lessons, practice, checks and later checks asked for."""
        rec, now, g, d = self.w.record, self.now, self.gh, self.dp
        rec.attempt(GH, g[0], now - timedelta(days=2), "probe", 1)
        rec.attempt(GH, g[1], now - timedelta(days=2) + timedelta(minutes=10), "practice", 3, hints=1)
        rec.attempt(GH, g[2], now - timedelta(hours=30), "probe", 3)
        rec.attempt(DP, d[0], now - timedelta(days=1), "probe", 1)
        for i, sid in enumerate(d[1:4], start=1):
            rec.attempt(DP, sid, now - timedelta(days=1) + timedelta(minutes=10 * i), "probe", 0)
        rec.attempt(GH, g[3], now - timedelta(hours=2), "probe", 3)

    def view(self, now: datetime | None = None, week: str | None = None) -> dict:
        return self.w.planner.view(self.w.me, week, now or self.now)

    def assert_within_rules(self, v: dict) -> None:
        r = v["rules"]
        days: dict[date, list[dict]] = {}
        for s in v["sessions"]:
            at, end = datetime.fromisoformat(s["at"]), datetime.fromisoformat(s["end"])
            days.setdefault(at.date(), []).append(s)
            if s["state"] == "done" or s["by"] == "you":
                continue
            fits = [w for w in r["windows"] if at.weekday() in w["days"] and w["start"] <= f"{at:%H:%M}" and f"{end:%H:%M}" <= w["end"]]
            self.assertEqual(len(fits), 1, s)
            self.assertEqual(fits[0]["audio_only"], s["kind"] == "listen", s)
            self.assertGreaterEqual(f"{at:%H:%M}", r["earliest"])
        for d, own in days.items():
            self.assertLessEqual(sum(s["minutes"] for s in own), r["day_minutes"][d.weekday()], d)
            self.assertLessEqual(len(own), P.MAX_A_DAY, d)
        self.assertLessEqual(v["week"]["planned"], r["week_minutes"])
        self.assertEqual(v["week"]["planned"], sum(p["minutes"] for p in v["week"]["packages"]))

    def test_both_exams_share_the_week_within_the_rules(self):
        self.record()
        v = self.view()
        self.assert_within_rules(v)
        self.assert_within_rules(self.view(week=(self.today + timedelta(days=7)).isoformat()))
        kinds = {(s["package"], s["kind"]) for s in v["sessions"]}
        for pk in ((GH, "lesson"), (GH, "check"), (GH, "later"), (GH, "check3"), (GH, "exam"), (DP, "lesson"), (DP, "check3"), (DP, "lab")):
            self.assertIn(pk, kinds)
        self.assertTrue(all(p["minutes"] > 0 for p in v["week"]["packages"]))
        per_day: dict[tuple, int] = {}
        for s in v["sessions"]:
            if s["kind"] == "check3":
                per_day[(s["package"], s["at"][:10])] = per_day.get((s["package"], s["at"][:10]), 0) + 1
        self.assertEqual(set(per_day.values()), {1}, "one 3-minute check a day for each exam")
        self.assertEqual(v["unplaced"], 0)
        self.assertEqual({h["package"]: h["days_left"] for h in v["horizon"]}, {GH: 19, DP: 150})

    def test_a_later_check_is_planned_from_when_it_comes_due(self):
        self.record()
        v = self.view()
        later = {s["skill"]: s["at"] for s in v["sessions"] if s["kind"] == "later"}
        self.assertIn(self.gh[3], later)
        due = P.to_local(self.now - timedelta(hours=2) + timedelta(minutes=4) + timedelta(hours=20))
        self.assertGreaterEqual(later[self.gh[3]], f"{due:%Y-%m-%dT%H:%M}")

    def test_work_in_a_lab_moves_a_later_check_to_when_it_comes_due(self):
        sid = LABS["vector"]["skill"]
        self.w.record.attempt(DP, sid, self.now - timedelta(hours=30), "probe", 3)
        first = next(s for s in self.view()["sessions"] if s["kind"] == "later")
        self.assertEqual((first["package"], first["skill"], first["at"][:10]), (DP, sid, self.today.isoformat()))
        self.w.record.add("lab.opened", self.now + timedelta(minutes=1), package=DP, skill=sid, lab="vector")
        v = self.view(self.now + timedelta(minutes=2))
        again = next(s for s in v["sessions"] + v["waiting"] if s["id"] == first["id"])
        due = P.to_local(self.now + timedelta(hours=20, minutes=1))
        self.assertGreaterEqual(again["at"], f"{due:%Y-%m-%dT%H:%M}")
        self.assertEqual([m["why"] for m in v["moved"] if m["session"] == first["id"]],
                         ["You worked on the skill since, so it comes due later"])

    def test_only_an_answer_the_evidence_counts_as_later_does_the_later_check(self):
        """On a real Record: the lesson is read again in the morning and the check answered at the planned time,
        both seen in one refresh. The evidence does not count that answer as later, so the session is not done:
        it moves to when the check comes due, in the calendar too. The next answer in time does it, although
        its own feedback restarts the clock."""
        sid, rec = self.gh[3], self.w.record
        rec.attempt(GH, sid, self.now - timedelta(hours=15, minutes=4), "probe", 3)   # judged at 16:00 on Sunday
        first = next(s for s in self.view()["sessions"] if s["kind"] == "later" and s["skill"] == sid)
        self.assertEqual(first["at"], f"{self.today}T12:30", "due at 12:00, planned at lunch")
        start = P.to_utc(datetime.fromisoformat(first["at"]))
        rec.add("lesson.viewed", self.now + timedelta(hours=2), package=GH, skill=sid, lesson=new_id("lesson"), modality="read")
        rec.attempt(GH, sid, start - timedelta(minutes=2), "check", 3)   # answered at 12:31, judged at 12:32
        v = self.view(start + timedelta(minutes=30))
        again = next(s for s in v["sessions"] + v["waiting"] if s["id"] == first["id"])
        due = P.to_local(start + timedelta(minutes=2, hours=20))
        self.assertEqual(again["state"], "planned")
        self.assertGreaterEqual(again["at"], f"{due:%Y-%m-%dT%H:%M}")
        self.assertEqual([m["why"] for m in v["moved"] if m["session"] == first["id"]],
                         ["You worked on the skill since, so it comes due later"])
        cal = {value(own(e)["UID"][0]): own(e) for e in blocks(unfold(self.w.planner.calendar_ics(self.w.me, BASE, start + timedelta(minutes=31))), "VEVENT")}
        new = P.to_utc(datetime.fromisoformat(again["at"]))
        self.assertEqual((cal[first["id"] + "@dojo"]["DTSTART"], cal[first["id"] + "@dojo"]["SEQUENCE"]),
                         ([f"DTSTART:{new:%Y%m%dT%H%M%SZ}"], ["SEQUENCE:1"]))
        # Answered a day after that feedback, with one point missed: done, and a new later check is asked for.
        rec.attempt(GH, sid, new - timedelta(minutes=2), "check", 2)
        v = self.view(new + timedelta(minutes=30))
        done = next(s for s in v["sessions"] if s["id"] == first["id"])
        self.assertEqual((done["state"], done["at"]), ("done", again["at"]))
        evidence = derive(self.w.store.events(self.w.me.key), self.w.dojo.packages.get(GH), 20)[sid]
        self.assertEqual((evidence["attempts"][-1]["minutes_since_teaching"], evidence["checks"]["later"]),
                         ((new - start) // timedelta(minutes=1), False))

    def test_nothing_is_planned_for_a_skill_with_no_verified_source(self):
        """An added exam can keep skills that no official page passed the check for (ADR 0005). Dojo does not
        teach them or ask about them, so the plan has no step for them either: no later check, and no daily
        3-minute check once only they are left untried, as that check would have nothing to ask. The test
        models such an exam on GH-300."""
        rec, now, g = self.w.record, self.now, self.gh
        rec.attempt(GH, g[0], now - timedelta(days=2), "probe", 1)
        rec.attempt(GH, g[1], now - timedelta(hours=30), "probe", 3)   # met on its own, so its later check is due
        pkg = deepcopy(self.w.dojo.packages.get(GH))
        for sid in g[1:]:
            pkg["skills"][sid]["sources"] = []
        self.w.dojo.packages.by_id = {**self.w.dojo.packages.by_id, GH: pkg}
        ev = derive(self.w.store.events(self.w.me.key), pkg, 20)
        self.assertEqual(pick(pkg, ev, now, 20, limit=len(ev)), [], "the 3-minute check has nothing to ask")
        self.view()
        sessions = self.w.plan_doc()["sessions"]
        self.assertEqual({s["skill"] for s in sessions if s["package"] == GH}, {g[0]})
        self.assertIn((DP, "check3"), {(s["package"], s["kind"]) for s in sessions})

    def test_a_removed_exam_takes_its_planned_sessions_with_it(self):
        """An added exam can be removed (ADR 0005). Its planned sessions leave the plan and the calendar, also
        one the learner pinned, as the exam they lead to is gone. What was done stays, and still counts in the
        week. A built-in exam cannot be removed, so the test removes GH-300 the way Packages.remove does."""
        self.record()
        gh = [s for s in self.view()["sessions"] if s["package"] == GH and s["state"] == "planned"]
        lesson = next(s for s in gh if s["kind"] == "lesson")
        pinned = next(s for s in gh if s["at"][:10] > lesson["at"][:10])
        self.w.planner.pin(self.w.me, pinned["id"], True, self.now)
        start = P.to_utc(datetime.fromisoformat(lesson["at"]))
        self.w.record.add("lesson.viewed", start + timedelta(minutes=5), package=GH, skill=lesson["skill"],
                          lesson=new_id("lesson"), modality="read")
        self.w.dojo.packages.by_id = {pid: p for pid, p in self.w.dojo.packages.by_id.items() if pid != GH}
        now = start + timedelta(minutes=30)
        v = self.view(now)
        self.assertEqual([(s["id"], s["state"]) for s in self.w.plan_doc()["sessions"] if s["package"] == GH],
                         [(lesson["id"], "done")])
        self.assert_within_rules(v)
        self.assertIn({"package": GH, "exam": "GH-300", "minutes": lesson["minutes"], "done": lesson["minutes"]},
                      v["week"]["packages"])
        self.assertEqual([h["package"] for h in v["horizon"]], [DP])
        uids = {value(own(e)["UID"][0]) for e in blocks(unfold(self.w.planner.calendar_ics(self.w.me, BASE, now)), "VEVENT")}
        self.assertIn(lesson["id"] + "@dojo", uids)
        self.assertNotIn(pinned["id"] + "@dojo", uids)

    def test_a_dp800_lab_a_week_at_a_desk_until_it_is_passed(self):
        sid, rec = LABS["vector"]["skill"], self.w.record
        v = self.view()
        self.assert_within_rules(v)
        lab = next(s for s in v["sessions"] if s["kind"] == "lab")
        self.assertEqual([(s["package"], s["skill"], s["label"], s["title"], s["minutes"], s["link"]) for s in v["sessions"] if s["kind"] == "lab"],
                         [(DP, sid, "Lab", self.w.dojo.packages.get(DP)["skills"][sid]["text"], P.LAB_MIN, "#/lab/vector")])
        # Opened, run and a hint, but not checked: not done, so it moves on.
        end = P.to_utc(datetime.fromisoformat(lab["end"]))
        for t in ("lab.opened", "lab.ran", "lab.hint"):
            rec.add(t, end - timedelta(minutes=10), package=DP, skill=sid, lab="vector")
        v = self.view(end + timedelta(minutes=1))
        again = next(s for s in v["sessions"] + v["waiting"] if s["id"] == lab["id"])
        self.assertEqual((again["state"], again["at"] > lab["end"]), ("planned", True))
        # Checked but not passed: done, and the same lab comes again next week, not this week.
        checked = P.to_utc(datetime.fromisoformat(again["at"])) + timedelta(minutes=10)
        rec.add("lab.checked", checked, package=DP, skill=sid, lab="vector", passed=False)
        v = self.view(checked + timedelta(minutes=1))
        self.assertEqual([(s["id"], s["state"]) for s in v["sessions"] if s["kind"] == "lab"], [(lab["id"], "done")])
        monday, tomorrow = self.today + timedelta(days=7), checked + timedelta(days=1)
        self.assertEqual([(s["id"], s["state"]) for s in self.view(tomorrow)["sessions"] if s["kind"] == "lab"], [(lab["id"], "done")])
        week2 = [s for s in self.view(tomorrow, week=monday.isoformat())["sessions"] if s["kind"] == "lab"]
        self.assertEqual([s["link"] for s in week2], ["#/lab/vector"])
        # Passed: the next lab, the week after.
        passed = P.to_utc(datetime.fromisoformat(week2[0]["at"])) + timedelta(minutes=10)
        rec.add("lab.checked", passed, package=DP, skill=sid, lab="vector", passed=True)
        after = passed + timedelta(minutes=1)
        self.assertEqual([s["state"] for s in self.view(after, week=monday.isoformat())["sessions"] if s["kind"] == "lab"], ["done"])
        week3 = [s for s in self.view(after, week=(monday + timedelta(days=7)).isoformat())["sessions"] if s["kind"] == "lab"]
        self.assertEqual([s["link"] for s in week3], ["#/lab/search"])

    def test_no_lab_for_a_skill_that_waits_for_its_later_check_and_none_after_the_exam(self):
        self.w.record.attempt(DP, LABS["vector"]["skill"], self.now - timedelta(hours=2), "probe", 3)
        self.assertEqual([s["link"] for s in self.view()["sessions"] if s["kind"] == "lab"], ["#/lab/search"],
                         "the vector lab would restart the clock of the later check still to come")
        self.w.exam_dates(gh_300=self.today + timedelta(days=19), dp_800=self.today)
        v = self.view()
        self.assertEqual([s for s in v["sessions"] + v["waiting"] if s["package"] == DP], [])

    def test_nothing_is_planned_on_the_exam_day_or_after(self):
        self.record()
        exam_day = self.today + timedelta(days=3)
        self.w.exam_dates(gh_300=exam_day, dp_800=self.today + timedelta(days=150))
        v = self.view()
        self.assert_within_rules(v)
        both = v["sessions"] + self.view(week=(self.today + timedelta(days=7)).isoformat())["sessions"]
        gh = [s for s in both if s["package"] == GH]
        self.assertTrue(gh)
        self.assertLess(max(s["end"] for s in gh), exam_day.isoformat())
        self.assertTrue([s for s in both if s["package"] == DP and s["at"] >= exam_day.isoformat()], "the other exam goes on")

    def test_practice_exams_in_the_last_four_weeks_one_a_week(self):
        self.record()
        full = exam_spec(self.w.dojo.packages.get(GH), "full")["minutes"]
        for week in (0, 7):
            v = self.view(week=(self.today + timedelta(days=week)).isoformat())
            exams = [s for s in v["sessions"] if s["kind"] == "exam"]
            self.assertEqual([(s["package"], s["label"], s["at"], s["minutes"]) for s in exams],
                             [(GH, "Practice exam (full length)", f"{self.today + timedelta(days=week + 5)}T10:00", full)])
            self.assertEqual(exams[0]["link"], f"#/rehearsal/for/{GH}")
        # One started this week counts for this week, whenever it was planned.
        self.w.record.add("exam.started", self.now - timedelta(hours=1), package=GH, attempt=new_id("exam"), length="short")
        self.assertEqual([s for s in self.view()["sessions"] if s["kind"] == "exam"], [])
        self.assertEqual(len([s for s in self.view(week=(self.today + timedelta(days=7)).isoformat())["sessions"] if s["kind"] == "exam"]), 1)

    def test_an_exam_that_has_been_taken_gets_nothing_more(self):
        self.record()
        self.w.exam_dates(gh_300=self.today, dp_800=self.today + timedelta(days=150))
        v = self.view()
        self.assertEqual({s["package"] for s in v["sessions"] + v["waiting"]}, {DP})

    def test_a_missed_session_moves_until_the_record_shows_it_done(self):
        self.record()
        v = self.view()
        lesson = next(s for s in v["sessions"] if s["kind"] == "lesson" and s["package"] == GH)
        end = P.to_utc(datetime.fromisoformat(lesson["end"]))
        v2 = self.view(end + timedelta(minutes=1))
        again = next(s for s in v2["sessions"] + v2["waiting"] if s["id"] == lesson["id"])
        self.assertEqual(again["state"], "planned")
        self.assertGreater(again["at"], lesson["end"])
        moved = [m for m in v2["moved"] if m["session"] == lesson["id"]]
        self.assertEqual([(m["why"], m["from"][-5:], m["to"][-5:]) for m in moved],
                         [("Not done at the planned time", lesson["at"][-5:], again["at"][-5:])])
        # Missed again: listed once, from the time it had last.
        end2 = P.to_utc(datetime.fromisoformat(again["end"]))
        v3 = self.view(end2 + timedelta(minutes=1))
        moved = [m for m in v3["moved"] if m["session"] == lesson["id"]]
        self.assertEqual([m["from"][-5:] for m in moved], [again["at"][-5:]])
        self.w.record.add("lesson.viewed", end2 + timedelta(minutes=2), package=GH, skill=lesson["skill"], lesson=new_id("lesson"), modality="read")
        v4 = self.view(end2 + timedelta(minutes=3))
        done = next(s for s in v4["sessions"] if s["id"] == lesson["id"])
        self.assertEqual((done["state"], done["at"]), ("done", again["end"]), "shown when it was done")
        self.assertEqual([m["to"] for m in v4["moved"] if m["session"] == lesson["id"]], [f"Done, {P._short(end2)}"])

    def test_skip_pin_and_move(self):
        self.record()
        planner, me, now = self.w.planner, self.w.me, self.now
        v = self.view()
        first, second, third = [s for s in v["sessions"] if s["state"] == "planned"][:3]
        tuesday = self.today + timedelta(days=1)
        planner.move(me, first["id"], f"{tuesday}T22:00", now=now)
        planner.pin(me, second["id"], True, now=now)
        planner.skip(me, third["id"], now=now)
        v = self.view()
        by_id = {s["id"]: s for s in v["sessions"]}
        self.assertEqual((by_id[first["id"]]["at"], by_id[first["id"]]["by"], by_id[first["id"]]["pinned"]), (f"{tuesday}T22:00", "you", True))
        self.assertTrue(by_id[second["id"]]["pinned"])
        self.assertNotIn(third["id"], by_id)
        again = [s for s in v["sessions"] if (s["package"], s["kind"], s["skill"]) == (third["package"], third["kind"], third["skill"])
                 and s["at"][:10] == third["at"][:10]]
        self.assertEqual(again, [], "a skipped step is not planned again that day")
        with self.assertRaises(UserError):
            planner.move(me, second["id"], f"{self.today}T06:00", now=now)
        with self.assertRaises(UserError):
            planner.move(me, second["id"], f"{self.today + timedelta(days=61)}T10:00", now=now)
        with self.assertRaises(UserError):
            planner.move(me, second["id"], "tomorrow", now=now)
        with self.assertRaisesRegex(UserError, "does not exist in Berlin"):
            planner.move(me, second["id"], "2027-03-28T02:30", now=now)
        with self.assertRaises(HTTPException) as e:
            planner.skip(me, "plan-" + "0" * 20, now=now)
        self.assertEqual(e.exception.status_code, 404)
        with self.assertRaises(HTTPException) as e:
            planner.pin(me, third["id"], True, now=now)
        self.assertEqual(e.exception.status_code, 409, "a skipped session cannot be pinned")
        self.assertEqual([m["why"] for m in self.w.plan_doc()["moves"]], ["you"])
        self.assertEqual(v["moved"], [], "the learner's own moves are not reported back")

    def test_a_session_over_midnight_counts_on_both_days(self):
        """Moved to 23:55, a session runs on into the next day. Its first five minutes count today and the rest
        tomorrow, on the Plan page and on Today, as they do in Dojo's own limits. It is listed on the day it starts."""
        self.record()
        planner, me, now = self.w.planner, self.w.me, self.now
        s = next(s for s in self.view()["sessions"] if s["state"] == "planned" and s["minutes"] > 5)
        planner.move(me, s["id"], f"{self.today}T23:55", now=now)
        v = self.view()
        tomorrow = self.today + timedelta(days=1)
        self.assertIn(s["id"], [x["id"] for x in v["sessions"] if x["at"][:10] == self.today.isoformat()])
        starts = {d: sum(x["minutes"] for x in v["sessions"] if x["at"][:10] == d.isoformat()) for d in (self.today, tomorrow)}
        days = {d["date"]: d["minutes"] for d in v["week"]["days"]}
        self.assertEqual((days[self.today.isoformat()], days[tomorrow.isoformat()]),
                         (starts[self.today] - s["minutes"] + 5, starts[tomorrow] + s["minutes"] - 5))
        self.assertEqual(planner.today(me, now)["minutes"], days[self.today.isoformat()])
        self.assertEqual(v["week"]["planned"], sum(d["minutes"] for d in v["week"]["days"]))
        self.assertEqual(v["week"]["planned"], sum(p["minutes"] for p in v["week"]["packages"]))

    def test_moving_a_session_onto_dojos_one_moves_that_one(self):
        """Dojo's sessions of the next 24 hours keep their time, but not under one the learner moves there."""
        self.record()
        planner, me, now = self.w.planner, self.w.me, self.now
        v = self.view()
        first = next(s for s in v["sessions"] if s["state"] == "planned" and s["at"][:10] == self.today.isoformat() and s["kind"] != "check3")
        other = next(s for s in v["sessions"] if s["state"] == "planned" and s["at"][:10] > self.today.isoformat())
        planner.move(me, other["id"], first["at"], now=now)
        v = self.view()
        self.assertNotEqual(next(s for s in v["sessions"] + v["waiting"] if s["id"] == first["id"])["at"], first["at"])
        self.assertEqual([m["why"] for m in v["moved"] if m["session"] == first["id"]], ["You put another session at that time"])

    def test_rules_from_the_page_take_effect_at_once(self):
        self.record()
        r = deepcopy(self.view()["rules"])
        r["windows"] = [w for w in r["windows"] if w["label"] != "Lunch"]
        self.w.planner.set_rules(self.w.me, r, now=self.now)
        v = self.view()
        self.assertNotIn("Lunch", [w["label"] for w in v["rules"]["windows"]])
        self.assertFalse([s for s in v["sessions"] if "12:30" <= s["at"][-5:] < "13:30"])
        self.assert_within_rules(v)
        with self.assertRaises(UserError):
            self.w.planner.set_rules(self.w.me, {**r, "week_minutes": -1}, now=self.now)

    def test_today_holds_todays_sessions(self):
        self.record()
        t = self.w.planner.today(self.w.me, self.now)
        self.assertEqual(t["date"], self.today.isoformat())
        self.assertTrue(t["sessions"])
        self.assertTrue(all(s["at"].startswith(self.today.isoformat()) for s in t["sessions"]))
        self.assertEqual(t["minutes"], sum(s["minutes"] for s in t["sessions"]))
        self.assertEqual(t["limit"], 60)

    def test_listening_goes_to_the_commute_and_opens_the_lesson_to_listen(self):
        sid = self.gh[5]
        lesson = new_id("lesson")
        slides = [{"title": f"Slide {i}", "bullets": ["one two three"], "narration": [{"voice": "guide", "text": "word " * 400}]} for i in range(3)]
        self.w.store.write("content", "lessons", lesson, value={"id": lesson, "package": GH, "skill": sid, "title": "T",
                                                                "created": iso(self.now - timedelta(days=3)), "slides": slides})
        for m in self.w.dojo.slide_media(self.w.dojo.lesson(lesson)):
            self.w.store.write_bytes("media", m + ".mp3", data=b"ID3" + bytes(64))
        self.assertEqual(self.w.planner.listen_minutes(lesson), 8, "1200 words at 150 a minute")
        self.w.record.attempt(GH, sid, self.now - timedelta(days=1), "probe", 0)
        v = self.view()
        listen = [s for s in v["sessions"] if s["kind"] == "listen"]
        self.assertEqual([(s["skill"], s["at"][-5:], s["link"]) for s in listen], [(sid, "08:30", f"#/lesson/{lesson}/listen")])
        self.assert_within_rules(v)
        # Without an audio-only time the same step is a lesson at a desk.
        r = deepcopy(v["rules"])
        r["windows"] = [w for w in r["windows"] if not w["audio_only"]]
        self.w.planner.set_rules(self.w.me, r, now=self.now)
        v = self.view()
        self.assertEqual([(s["kind"], s["link"]) for s in v["sessions"] if s["skill"] == sid], [("lesson", f"#/lesson/{lesson}")])

    def test_the_planner_writes_no_evidence(self):
        self.record()
        before = self.w.store.events(self.w.me.key)
        s = [s for s in self.view()["sessions"] if s["state"] == "planned"]
        self.w.planner.pin(self.w.me, s[0]["id"], True, now=self.now)
        self.w.planner.skip(self.w.me, s[1]["id"], now=self.now)
        self.w.planner.calendar_ics(self.w.me, BASE, self.now)
        self.w.planner.today(self.w.me, self.now + timedelta(days=1))
        self.assertEqual(self.w.store.events(self.w.me.key), before)
        self.assertTrue(self.w.store.verify_chain(self.w.me.key)["ok"])
        self.assertEqual(self.w.dojo.export(self.w.me)["plan"], self.w.plan_doc(), "the plan is the learner's own data")


# ---------------------------------------------------------------- the calendar feed

class IcsTests(unittest.TestCase):
    def test_folding_at_75_octets_never_inside_a_character(self):
        self.assertEqual(P.fold("X" * 75), "X" * 75)
        self.assertEqual(P.fold("X" * 76), "X" * 75 + "\r\n X")
        line = "SUMMARY:" + "Grüße · 😀 données " * 20
        parts = P.fold(line).split("\r\n")
        self.assertGreater(len(parts), 3)
        for i, p in enumerate(parts):
            self.assertLessEqual(len(p.encode("utf-8")), 75)
            self.assertEqual(p.startswith(" "), i > 0)
        self.assertEqual(parts[0] + "".join(p[1:] for p in parts[1:]), line)

    def test_text_is_escaped(self):
        self.assertEqual(P.ics_text("a\\b;c,d\ne\r\nf\rg\x07h\x00"), "a\\\\b\\;c\\,d\\ne\\nf\\ngh")

    def test_a_valid_calendar(self):
        now = berlin(2026, 10, 5, 7)
        url = BASE + "/?go=skill/gh-300/d1.g1.s1"
        events = [{"uid": "plan-1@dojo", "start": berlin(2026, 10, 5, 12, 30), "minutes": 15,
                   "summary": "GH-300 · Lesson · Commas, semicolons; and back\\slashes", "url": url,
                   "description": f"15 min. Open it in Dojo: {url}\nMove or skip it on Dojo's Plan page.",
                   "seq": 2, "created": now - timedelta(days=1), "changed": now, "alarm": 10},
                  {"uid": "plan-2@dojo", "start": berlin(2026, 10, 26, 18, 0), "minutes": 100, "summary": "DP-800 · Practice exam (full length)",
                   "description": "x", "url": BASE + "/?go=rehearsal/for/dp-800", "seq": 0, "created": now, "changed": now, "alarm": None}]
        body = P.write_ics(events, now)
        self.assertTrue(body.endswith(b"\r\n"))
        self.assertNotIn(b"\n", body.replace(b"\r\n", b""))
        self.assertNotIn(b"\r", body.replace(b"\r\n", b""))
        for raw in body.split(b"\r\n"):
            self.assertLessEqual(len(raw), 75)
            raw.decode("utf-8")
        lines = unfold(body)
        for line in lines:
            self.assertRegex(line, CONTENT_LINE)
        self.assertEqual((lines[0], lines[-1]), ("BEGIN:VCALENDAR", "END:VCALENDAR"))
        cal = own(blocks(lines, "VCALENDAR")[0])
        self.assertEqual({k: [value(x) for x in v] for k, v in cal.items() if k in ("VERSION", "CALSCALE", "METHOD", "X-WR-CALNAME")},
                         {"VERSION": ["2.0"], "CALSCALE": ["GREGORIAN"], "METHOD": ["PUBLISH"], "X-WR-CALNAME": ["Dojo plan"]})
        self.assertEqual(len(cal["PRODID"]), 1)
        self.assertEqual(blocks(lines, "VTIMEZONE"), [], "times are moments in UTC, so no time zone is needed")
        self.assertNotIn(b"TZID", body)
        first, second = blocks(lines, "VEVENT")
        p = own(first)
        for name in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "URL", "SEQUENCE", "CREATED", "LAST-MODIFIED", "CLASS"):
            self.assertEqual(len(p[name]), 1, name)
        self.assertEqual(p["UID"], ["UID:plan-1@dojo"])
        self.assertEqual(p["DTSTAMP"], ["DTSTAMP:20261005T050000Z"])
        self.assertEqual(p["DTSTART"], ["DTSTART:20261005T103000Z"], "12:30 in Berlin, summer time")
        self.assertEqual(p["DTEND"], ["DTEND:20261005T104500Z"])
        self.assertEqual(p["SUMMARY"], ["SUMMARY:GH-300 · Lesson · Commas\\, semicolons\\; and back\\\\slashes"])
        self.assertEqual(untext(value(p["DESCRIPTION"][0])), events[0]["description"])
        self.assertEqual((p["SEQUENCE"], p["CLASS"]), (["SEQUENCE:2"], ["CLASS:PRIVATE"]))
        alarm = blocks(first, "VALARM")
        self.assertEqual(len(alarm), 1)
        self.assertEqual({k: [value(x) for x in v] for k, v in own(alarm[0]).items()},
                         {"ACTION": ["DISPLAY"], "DESCRIPTION": ["GH-300 · Lesson · Commas\\, semicolons\\; and back\\\\slashes"],
                          "TRIGGER": ["-PT10M"]})
        p = own(second)
        self.assertEqual(p["DTSTART"], ["DTSTART:20261026T170000Z"], "18:00 in Berlin: winter time again after 25 October")
        self.assertEqual(p["DTEND"], ["DTEND:20261026T184000Z"])
        self.assertEqual(blocks(second, "VALARM"), [])

    @staticmethod
    def times(start: datetime, minutes: int) -> tuple[str, str]:
        body = P.write_ics([{"uid": "plan-1@dojo", "start": start, "minutes": minutes, "summary": "GH-300 · Check", "description": "x",
                             "url": BASE + "/?go=check/for/gh-300", "seq": 0, "created": start, "changed": start, "alarm": 10}], start)
        p = own(blocks(unfold(body), "VEVENT")[0])
        return value(p["DTSTART"][0]), value(p["DTEND"][0])

    def test_an_event_keeps_its_length_over_a_clock_change(self):
        """In the hour that repeats in autumn, an hour from the first 02:30 ends at the second 02:30. In Berlin
        wall-clock time both would read 02:30; as moments in UTC the event keeps its hour."""
        for day in (date(2026, 10, 25), date(2027, 10, 31)):
            first = P.to_utc(datetime.combine(day, time(2, 30)))
            self.assertEqual(P.to_local(first + timedelta(hours=1)), datetime.combine(day, time(2, 30)))
            self.assertEqual(self.times(first, 60), (f"{day:%Y%m%d}T003000Z", f"{day:%Y%m%d}T013000Z"), day)
            self.assertEqual(self.times(first + timedelta(hours=1), 45), (f"{day:%Y%m%d}T013000Z", f"{day:%Y%m%d}T021500Z"),
                             "from the second 02:30")
        # And over the spring change: 01:30 plus an hour is 03:30 on the wall clock.
        self.assertEqual(self.times(P.to_utc(datetime(2027, 3, 28, 1, 30)), 60), ("20270328T003000Z", "20270328T013000Z"))
        for start, end in (self.times(P.to_utc(datetime(2027, 3, 28, 1, 30)), 60), self.times(utc(2026, 10, 25, 0, 30), 60)):
            parse = lambda v: datetime.strptime(v, "%Y%m%dT%H%M%SZ")  # noqa: E731
            self.assertEqual(parse(end) - parse(start), timedelta(hours=1))


class FeedContentTests(unittest.TestCase):
    """What the feed carries, on a real Record with an answer, hints and feedback in it."""

    def setUp(self):
        self.w = World()
        self.now = ahead()
        self.today = P.to_local(self.now).date()
        g = self.w.skills(GH)
        self.secret = "MY-ANSWER-" + new_id("x")
        rec = self.w.record
        self.items = [rec.attempt(GH, g[0], self.now - timedelta(days=2), "probe", 1, text=self.secret),
                      rec.attempt(GH, g[1], self.now - timedelta(days=2) + timedelta(minutes=10), "practice", 3, hints=2, text=self.secret),
                      rec.attempt(GH, g[2], self.now - timedelta(hours=30), "probe", 3, text=self.secret)]
        self.w.exam_dates(gh_300=self.today + timedelta(days=19), dp_800=None)

    def feed(self, now: datetime) -> bytes:
        return self.w.planner.calendar_ics(self.w.me, BASE, now)

    def events(self, body: bytes) -> dict[str, dict[str, list[str]]]:
        return {value(own(e)["UID"][0]): own(e) for e in blocks(unfold(body), "VEVENT")}

    def test_an_event_says_what_how_long_and_where_and_nothing_else(self):
        self.w.planner.view(self.w.me, None, self.now)
        v = self.w.planner.view(self.w.me, None, self.now + timedelta(hours=8))   # misses the morning, so moves are logged
        self.assertTrue(v["moved"])
        body = self.feed(self.now + timedelta(hours=8))
        text = body.decode("utf-8")
        plain = "\n".join(untext(line) for line in unfold(body))
        for secret in [self.secret, "MY-ANSWER", self.w.me.key, self.w.me.id, "local:dev", "Not done", "waiting", "planned time",
                       *self.items, *(e["id"] for e in self.w.store.events(self.w.me.key))]:
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, plain)
        # Skill names are the exam's published study guide; everything else must say nothing about results.
        for pid in (GH, DP):
            for skill in self.w.dojo.packages.get(pid)["skills"].values():
                plain = plain.replace(skill["text"], "")
        self.assertNotRegex(plain, r"(?i)\b(met|probe|unaided|hints?|evidence|score|judged|correct|wrong|failed|passed|ready)\b")
        labels = "|".join(re.escape(x) for x in ("Listen", "Lesson", "Practise", "Check", "Later check", "3-minute check",
                                                 "Practice exam (full length)", "Practice exam (short)", "Lab"))
        sessions = {s["id"] + "@dojo": s for s in self.w.plan_doc()["sessions"]}
        self.assertIn(f"{BASE}/?go=lab/vector", {value(p["URL"][0]) for p in self.events(body).values()}, "a DP-800 lab too")
        for uid, p in self.events(body).items():
            self.assertEqual(set(p), {"UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "URL", "SEQUENCE", "CREATED",
                                      "LAST-MODIFIED", "STATUS", "TRANSP", "CLASS"})
            s = sessions[uid]
            pkg = self.w.dojo.packages.get(s["package"])
            summary = untext(value(p["SUMMARY"][0]))
            m = re.fullmatch(rf"{re.escape(pkg['meta']['exam'])} · ({labels})(?: · (.+))?", summary)
            self.assertTrue(m, summary)
            self.assertEqual(m.group(2), pkg["skills"][s["skill"]]["text"] if s["skill"] else None)
            url = value(p["URL"][0])
            # Easy Auth loses a "#" route when it asks to sign in, so an event opens "/?go=<route>"; the Plan page keeps "#/".
            self.assertEqual(url, f"{BASE}/?go={self.w.planner.route(s)}")
            self.assertEqual(self.w.planner.link(s), "#/" + self.w.planner.route(s))
            self.assertEqual(untext(value(p["DESCRIPTION"][0])), f"{s['minutes']} min. Open it in Dojo: {url}\nMove or skip it on Dojo's Plan page.")

    def test_the_same_session_is_the_same_event_so_outlook_updates_it(self):
        planner, me = self.w.planner, self.w.me
        a = self.events(self.feed(self.now))
        b = self.events(self.feed(self.now + timedelta(minutes=10)))
        self.assertEqual(set(a), set(b))
        for uid in a:
            self.assertEqual([a[uid][k] for k in ("SEQUENCE", "DTSTART", "SUMMARY")], [b[uid][k] for k in ("SEQUENCE", "DTSTART", "SUMMARY")])
            self.assertNotEqual(a[uid]["DTSTAMP"], b[uid]["DTSTAMP"])
        uid = sorted(a, key=lambda u: a[u]["DTSTART"][0])[-1]
        when = self.today + timedelta(days=2)
        planner.move(me, uid.split("@")[0], f"{when}T21:15", now=self.now + timedelta(minutes=11))
        c = self.events(self.feed(self.now + timedelta(minutes=12)))
        self.assertEqual(c[uid]["DTSTART"], [f"DTSTART:{P.to_utc(datetime.combine(when, time(21, 15))):%Y%m%dT%H%M%SZ}"])
        self.assertEqual(int(value(c[uid]["SEQUENCE"][0])), int(value(a[uid]["SEQUENCE"][0])) + 1)
        planner.skip(me, uid.split("@")[0], now=self.now + timedelta(minutes=13))
        self.assertNotIn(uid, self.events(self.feed(self.now + timedelta(minutes=14))), "a skipped session leaves the calendar")

    def test_reminders_only_for_what_is_still_to_come(self):
        r = deepcopy(P.DEFAULT_RULES)
        r["reminder"] = 25
        self.w.planner.set_rules(self.w.me, r, now=self.now)
        body = self.feed(self.now)
        cal = blocks(unfold(body), "VEVENT")
        self.assertTrue(cal)
        for e in cal:
            self.assertEqual([x for x in e if x.startswith("TRIGGER")], ["TRIGGER:-PT25M"])
        later = self.now + timedelta(days=1, hours=6)
        for e in blocks(unfold(self.feed(later)), "VEVENT"):
            start = datetime.strptime(value(own(e)["DTSTART"][0]), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            self.assertEqual(bool(blocks(e, "VALARM")), start > later)

    def test_a_session_done_early_frees_its_time(self):
        v = self.w.planner.view(self.w.me, None, self.now)
        lesson = next(s for s in v["sessions"] if s["kind"] == "lesson")
        self.w.record.add("lesson.viewed", self.now + timedelta(minutes=30), package=GH, skill=lesson["skill"], lesson=new_id("lesson"), modality="read")
        after = self.now + timedelta(hours=1)
        self.assertNotIn(lesson["id"] + "@dojo", self.events(self.feed(after)))
        done = next(s for s in self.w.planner.view(self.w.me, None, after)["sessions"] if s["id"] == lesson["id"])
        self.assertEqual(done["state"], "done")


# ---------------------------------------------------------------- the secret link

class CalendarLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-cal-link-")
        self.store = Store(Path(self.tmp))
        self.owner = Learner("t:o", learner_key("t:o"), "Owner")
        self.links = P.CalendarLinks(self.store, self.owner)

    def files(self) -> bytes:
        return b"".join(p.read_bytes() for p in Path(self.tmp).rglob("*") if p.is_file())

    def test_the_token_is_long_random_and_only_its_hash_is_kept(self):
        token = self.links.create()
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")  # 32 random bytes
        self.assertNotIn(token.encode(), self.files())
        doc = self.store.read("learners", self.owner.key, "calendar")
        self.assertEqual(set(doc), {"hash", "created", "fetched"})
        self.assertEqual(doc["hash"], hashlib.sha256(b"dojo-cal:" + token.encode()).hexdigest())
        self.assertEqual(self.links.owner_for(token), self.owner)
        self.assertNotEqual(self.links.create(replace=True), token)

    def test_a_second_link_needs_a_new_link_and_the_old_one_dies_at_once(self):
        old = self.links.create()
        with self.assertRaises(HTTPException) as e:
            self.links.create()
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual(self.links.owner_for(old), self.owner)
        new = self.links.create(replace=True)
        self.assertIsNone(self.links.owner_for(old))
        self.assertEqual(self.links.owner_for(new), self.owner)
        self.links.delete()
        self.assertIsNone(self.links.owner_for(new))
        self.assertEqual(self.links.status(), {"active": False, "created": None, "fetched": None})

    def test_wrong_tokens_are_refused_after_the_same_constant_time_comparison(self):
        token = self.links.create()
        wrong = ("A" if token[0] != "A" else "B") + token[1:]
        with mock.patch("app.plan.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            for t in (wrong, "", "short", token + "x", token.upper() if token.upper() != token else token.lower(), "../" + token[3:]):
                self.assertIsNone(self.links.owner_for(t), t)
            self.links.delete()
            self.assertIsNone(self.links.owner_for(token))
        self.assertEqual(compare.call_count, 7)
        for c in compare.call_args_list:
            self.assertEqual([len(a) for a in c.args], [64, 64])

    def test_a_podcast_token_is_not_a_calendar_token(self):
        token = self.links.create()
        self.assertNotEqual(P.token_hash(token), hashlib.sha256(b"dojo-feed:" + token.encode()).hexdigest())

    def test_it_keeps_the_link_and_when_it_was_last_fetched_and_nothing_else(self):
        token = self.links.create()
        t0 = utc(2026, 10, 5, 10)
        self.links.fetched(token, t0)
        self.links.fetched(token, t0 + timedelta(minutes=4))
        self.assertEqual(self.links.status()["fetched"], iso(t0), "at most every few minutes")
        self.links.fetched(token, t0 + timedelta(minutes=6))
        self.assertEqual(self.links.status()["fetched"], iso(t0 + timedelta(minutes=6)))
        self.links.fetched(("A" if token[0] != "A" else "B") + token[1:], t0 + timedelta(hours=1))
        self.assertEqual(self.links.status()["fetched"], iso(t0 + timedelta(minutes=6)), "a wrong token notes nothing")
        self.assertEqual([p.name for p in Path(self.tmp).rglob("*") if p.is_file()], ["calendar.json"])
        self.links.delete()
        self.assertEqual([p for p in Path(self.tmp).rglob("*") if p.is_file()], [])


class CalendarFeedRouteTests(unittest.TestCase):
    """Local mode with the real clock: the open /cal route as a calendar app sees it."""

    @classmethod
    def setUpClass(cls):
        cls.w = World()
        cls.app, cls.c = cls.w.app, TestClient(cls.w.app)
        now = utcnow()
        g = cls.w.skills(GH)
        cls.secret = "MY-ANSWER-" + new_id("x")
        cls.w.record.attempt(GH, g[0], now - timedelta(days=2), "probe", 1, text=cls.secret)
        cls.w.record.attempt(GH, g[1], now - timedelta(days=2) + timedelta(minutes=10), "practice", 3, hints=1, text=cls.secret)
        link = cls.c.post("/api/calendar/link", headers=POST).json()
        cls.token = link["url"].split("/")[4]
        cls.link = link

    def feed(self) -> bytes:
        r = self.c.get(f"/cal/{self.token}/dojo.ics")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-type"], "text/calendar; charset=utf-8")
        self.assertEqual(r.headers["x-robots-tag"], "noindex, nofollow")
        self.assertTrue(r.headers["cache-control"].startswith("private"))
        self.assertNotIn("set-cookie", r.headers)
        return r.content

    def test_the_link_is_shown_once_with_a_qr_code(self):
        self.assertEqual(self.link["url"], f"http://testserver/cal/{self.token}/dojo.ics")
        self.assertTrue(self.link["qr"].startswith("data:image/svg+xml;"))
        self.assertTrue(self.link["active"])
        for path in ("/api/calendar", "/api/plan", "/api/plan/today"):
            self.assertNotIn(self.token, self.c.get(path).text, path)

    def test_the_feed_is_a_calendar_of_the_plan(self):
        lines = unfold(self.feed())
        uids = [value(x) for x in lines if x.startswith("UID:")]
        self.assertTrue(uids)
        plan = self.c.get("/api/plan").json()
        shown = {s["id"] + "@dojo" for s in plan["sessions"] if s["state"] != "done"}
        self.assertLessEqual(shown - set(uids), set(), "every session still to come is in the calendar")
        self.assertNotIn(self.secret, "\n".join(lines))
        for url in (value(x) for x in lines if x.startswith("URL:")):
            self.assertRegex(url, r"^http://testserver/\?go=(skill/[a-z0-9-]+/[a-z0-9.]+|lesson/lesson-[0-9a-f]{20}(/listen)?|check/for/[a-z0-9-]+|recall/for/[a-z0-9-]+|rehearsal/for/[a-z0-9-]+|lab/(vector|search|hybrid))$")

    def test_a_get_writes_only_the_plan_and_when_it_was_fetched_and_a_head_nothing(self):
        self.feed()
        before, events = self.w.files(("plan.json", "calendar.json")), len(self.w.store.events(self.w.me.key))
        self.c.get(f"/cal/{self.token}/dojo.ics")
        self.assertEqual(self.w.files(("plan.json", "calendar.json")), before)
        self.assertEqual(len(self.w.store.events(self.w.me.key)), events)
        fetched = self.c.get("/api/calendar").json()["fetched"]
        self.assertTrue(fetched)
        cal = self.w.store.read("learners", self.w.me.key, "calendar")
        self.assertEqual(set(cal), {"hash", "created", "fetched"}, "a time, no address and no app")
        all_files = self.w.files()
        self.assertEqual(self.c.head(f"/cal/{self.token}/dojo.ics").status_code, 200)
        self.assertEqual(self.w.files(), all_files)

    def test_wrong_or_missing_tokens_and_unknown_names_get_an_empty_404(self):
        wrong = ("A" if self.token[0] != "A" else "B") + self.token[1:]
        for path in (f"/cal/{wrong}/dojo.ics", "/cal/dojo.ics", "/cal/", "/cal", f"/cal/{self.token}", f"/cal/{self.token}/",
                     f"/cal/{self.token}/plan.ics", f"/cal/{self.token}/DOJO.ICS", f"/cal/{self.token}/dojo.ics/more",
                     f"/cal/{self.token[:-1]}/dojo.ics"):
            r = self.c.get(path)
            self.assertEqual((r.status_code, r.content), (404, b""), path)
            self.assertEqual(r.headers["x-robots-tag"], "noindex, nofollow", path)
            self.assertNotIn("set-cookie", r.headers, path)

    def test_the_feed_answers_only_get_and_head(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            r = self.c.request(method, f"/cal/{self.token}/dojo.ics", headers=POST)
            self.assertEqual((r.status_code, r.headers.get("allow"), r.content), (405, "GET, HEAD", b""), method)

    def test_the_token_is_never_written_to_a_log(self):
        path = f"/cal/{self.token}/dojo.ics".encode()
        requests = [(path, "GET"), (path, "HEAD"), (f"/cal/{self.token[::-1]}/dojo.ics".encode(), "GET"),
                    (f"/cal/{self.token}/../api/me".encode(), "GET"), (path, "POST"), (f"/cal/{self.token}/x.ics".encode(), "GET")]
        records: list[logging.LogRecord] = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root, level = logging.getLogger(), logging.getLogger().level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            statuses = [call(self.app, p, method=m)[0] for p, m in requests]
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        self.assertEqual(statuses, [200, 200, 404, 400, 405, 404])
        self.assertTrue(any(r.name == "dojo" and "refused" in r.getMessage() for r in records))
        for r in records:
            self.assertNotIn(self.token, r.getMessage() + (r.exc_text or ""), r.name)

    def test_plan_changes_through_the_api(self):
        v = self.c.get("/api/plan").json()
        self.assertEqual(self.c.get("/api/plan?week=soon").status_code, 422)
        self.assertEqual(self.c.get("/api/plan?week=2040-01-02").status_code, 422)
        planned = [s for s in v["sessions"] + self.c.get("/api/plan", params={"week": (date.fromisoformat(v["week"]["start"])
                                                                                        + timedelta(days=7)).isoformat()}).json()["sessions"]
                   if s["state"] == "planned"]
        self.assertGreaterEqual(len(planned), 2)
        a, b = planned[-1], planned[-2]
        when = (P.to_local(utcnow()) + timedelta(days=2)).replace(hour=21, minute=40)
        r = self.c.post(f"/api/plan/sessions/{a['id']}/move", json={"start": f"{when:%Y-%m-%dT%H:%M}"}, headers=POST)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.post(f"/api/plan/sessions/{a['id']}/move", json={"start": "2020-01-01T10:00"}, headers=POST).status_code, 422)
        r = self.c.post(f"/api/plan/sessions/{b['id']}/pin", json={"pinned": True}, headers=POST, params={"week": v["week"]["start"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post(f"/api/plan/sessions/{b['id']}/skip", headers=POST).status_code, 200)
        self.assertEqual(self.c.post(f"/api/plan/sessions/{b['id']}/skip", headers=POST).status_code, 409, "skipped already")
        for bad in ("x", "plan-" + "0" * 20, "PLAN-" + "0" * 20):
            self.assertEqual(self.c.post(f"/api/plan/sessions/{bad}/skip", headers=POST).status_code, 404, bad)
        self.assertEqual(self.c.post(f"/api/plan/sessions/{a['id']}/skip").status_code, 403, "a write needs the X-Dojo header")
        rules = v["rules"]
        overlap = {**rules, "windows": rules["windows"] + [{"label": "More lunch", "days": [0], "start": "13:00", "end": "14:00"}]}
        r = self.c.put("/api/plan/rules", json=overlap, headers=POST)
        self.assertEqual(r.status_code, 422)
        self.assertIn("overlap", r.json()["detail"])
        self.assertEqual(self.c.put("/api/plan/rules", json={**rules, "week_minutes": "lots"}, headers=POST).status_code, 422)
        r = self.c.put("/api/plan/rules", json={**rules, "week_minutes": 300}, headers=POST)
        self.assertEqual((r.status_code, r.json()["rules"]["week_minutes"]), (200, 300))
        self.assertIn("at most 5 h a week", r.json()["summary"])
        self.c.put("/api/plan/rules", json=rules, headers=POST)
        today = self.c.get("/api/plan/today").json()
        self.assertEqual(set(today), {"date", "now", "limit", "minutes", "done", "sessions", "calendar"})


class CalendarLinkLifeTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = TestClient(self.w.app)

    def test_a_new_link_ends_the_old_one_and_turning_it_off_ends_both(self):
        old = self.c.post("/api/calendar/link", headers=POST).json()["url"].split("/")[4]
        self.assertEqual(self.c.post("/api/calendar/link", headers=POST).status_code, 409)
        new = self.c.post("/api/calendar/link/new", headers=POST).json()["url"].split("/")[4]
        self.assertEqual(self.c.get(f"/cal/{old}/dojo.ics").status_code, 404)
        self.assertEqual(self.c.get(f"/cal/{new}/dojo.ics").status_code, 200)
        self.assertEqual(self.c.delete("/api/calendar/link", headers=POST).json(), {"active": False, "created": None, "fetched": None})
        self.assertEqual(self.c.get(f"/cal/{new}/dojo.ics").status_code, 404)
        self.assertNotIn(new.encode(), b"".join(self.w.files().values()))

    def test_deleting_the_record_ends_the_link_and_the_plan(self):
        token = self.c.post("/api/calendar/link", headers=POST).json()["url"].split("/")[4]
        self.assertEqual(self.c.get(f"/cal/{token}/dojo.ics").status_code, 200)
        self.assertIsNotNone(self.w.plan_doc())
        self.assertEqual(self.c.post("/api/record/delete", json={"confirm": "DELETE"}, headers=POST).status_code, 200)
        self.assertEqual(self.c.get(f"/cal/{token}/dojo.ics").status_code, 404)
        self.assertIsNone(self.w.plan_doc())


class CalendarSignInTests(unittest.TestCase):
    """As in Azure: the plan needs sign-in, /cal/* needs only its token."""

    @classmethod
    def setUpClass(cls):
        cls.w = World(local=False)
        cls.app, cls.c = cls.w.app, TestClient(cls.w.app)
        cls.owner = {"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "owner-1")}
        cls.token = cls.w.planner.links.create()

    def test_the_plan_and_the_link_need_sign_in(self):
        for method, path in (("GET", "/api/plan"), ("GET", "/api/plan/today"), ("PUT", "/api/plan/rules"), ("GET", "/api/calendar"),
                             ("POST", "/api/calendar/link"), ("POST", "/api/calendar/link/new"), ("DELETE", "/api/calendar/link"),
                             ("POST", f"/api/plan/sessions/plan-{'0' * 20}/skip")):
            self.assertEqual(self.c.request(method, path, headers=POST).status_code, 401, path)
        self.assertEqual(self.c.get("/api/plan", headers=self.owner).status_code, 200)
        stranger = {"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "someone-else")}
        self.assertEqual(self.c.post("/api/calendar/link/new", headers={**POST, **stranger}).status_code, 403)

    def test_the_feed_needs_its_token_and_ignores_the_sign_in_header(self):
        self.assertEqual(self.c.get(f"/cal/{self.token}/dojo.ics").status_code, 200)
        wrong = ("A" if self.token[0] != "A" else "B") + self.token[1:]
        for token in (wrong, "x"):
            r = self.c.get(f"/cal/{token}/dojo.ics", headers=self.owner)
            self.assertEqual((r.status_code, r.content), (404, b""))

    def test_dot_segments_and_encoded_separators_never_reach_the_api(self):
        signed_in = ((b"x-ms-client-principal", self.owner["X-MS-CLIENT-PRINCIPAL"].encode()),)
        self.assertEqual(call(self.app, b"/api/me", headers=signed_in)[0], 200)
        for raw in (b"/cal/../api/me", b"/cal/%2e%2e/api/me", b"/cal/.%2e/api/me", b"/cal/..%2fapi%2fme", b"/cal/x%2F..%2F..%2Fapi%2Fme",
                    b"/cal/..%5capi/me", b"/cal/./api/me", b"/cal/x/..", b"/api/../cal/x", b"/cal/%252e%252e/api/me",
                    f"/cal/{self.token}/../../api/plan".encode()):
            for headers in ((), signed_in):
                self.assertEqual(call(self.app, raw, headers=headers), (400, b""), raw)
        self.assertEqual(call(self.app, b"/cal/../api/calendar/link/new", method="POST", headers=signed_in + ((b"x-dojo", b"1"),)), (400, b""))


if __name__ == "__main__":
    unittest.main()
