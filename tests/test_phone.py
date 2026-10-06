"""The phone (docs/adr/0013-phone.md): the installable app, opt-in push nudges and the 5-minute session.

The nudger is tested with a fake sender, so no test reaches a real push service. The one test of the real
sender (pywebpush) hands it a fake requests session and checks what would have gone over the wire."""
import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-phone-import-"))

import httpx  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import push  # noqa: E402
from app.core import APP_DIR, Abandoned, Learner, Store, learner_key, sha256, utcnow  # noqa: E402
from app.five import FIVE_RECALLS, REPAIR_MINUTES, FiveMinutes  # noqa: E402
from app.main import create_app  # noqa: E402
from app.packages import Packages  # noqa: E402
from tests.test_api import POST, settings, web  # noqa: E402
from tests.test_team import OWNER, TeamCase, classes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def utc(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def browser_keys() -> tuple[str, str]:
    """What a browser hands over: its P-256 public key (65 bytes) and a 16-byte auth secret."""
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(point), b64u(os.urandom(16))


def endpoint(name: str, host: str = "fcm.googleapis.com") -> str:
    return f"https://{host}/fcm/send/{name}-0123456789abcdefghij"


def device(name: str, host: str = "fcm.googleapis.com", label: str = "Edge on Windows") -> dict:
    p256dh, auth = browser_keys()
    return {"endpoint": endpoint(name, host), "p256dh": p256dh, "auth": auth, "label": label}


class FakeSlots:
    def __init__(self, free: bool = True):
        self.free, self.taken, self.most = free, 0, 0

    def acquire(self, timeout=None):
        if not self.free:
            return False
        self.taken += 1
        self.most = max(self.most, self.taken)
        return True

    def release(self):
        self.taken -= 1


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.status: dict[str, int] = {}

    def __call__(self, dev: dict, payload: dict) -> int:
        self.sent.append((dev["endpoint"], payload))
        return self.status.get(dev["endpoint"], 201)


def learner(name: str) -> Learner:
    return Learner(f"local:{name}", learner_key(f"local:{name}"), name.title())


# ---------------------------------------------------------------- checks on what a browser sends

class EndpointTests(unittest.TestCase):
    def test_only_https_on_a_known_push_service(self):
        for good in (endpoint("a"), "https://wns2-db5p.notify.windows.com/w/?token=" + "x" * 30,
                     "https://updates.push.services.mozilla.com/wpush/v2/" + "x" * 30,
                     "https://web.push.apple.com/" + "x" * 30, "https://fcm.googleapis.com:443/fcm/send/" + "x" * 20):
            self.assertEqual(push.check_endpoint(good), good)
        for bad in ("http://fcm.googleapis.com/fcm/send/" + "x" * 20,            # not https
                    "https://evil.example.com/fcm/send/" + "x" * 20,           # not a push service
                    "https://fcm.googleapis.com.evil.com/fcm/send/" + "x" * 20,
                    "https://www.googleapis.com/fcm/send/" + "x" * 20,          # only fcm.googleapis.com
                    "https://notify.windows.com.evil.com/" + "x" * 20,
                    "https://user:pw@fcm.googleapis.com/fcm/send/" + "x" * 20,  # credentials in the URL
                    "https://fcm.googleapis.com:8443/fcm/send/" + "x" * 20,     # an odd port
                    "https://127.0.0.1/fcm/send/" + "x" * 20, "https://localhost/" + "x" * 20,
                    "https://fcm.googleapis.com/a b" + "x" * 20, "https://fcm.googleapis.com\\@evil.com/" + "x" * 20,
                    "https://x.co", 42, None, "https://fcm.googleapis.com/" + "x" * 3000):
            with self.assertRaises(HTTPException, msg=repr(bad)) as e:
                push.check_endpoint(bad)
            self.assertEqual(e.exception.status_code, 400)

    def test_keys_must_have_the_lengths_rfc_8291_requires(self):
        p256dh, auth = browser_keys()
        self.assertEqual(push.check_keys(p256dh, auth), (p256dh, auth))
        self.assertEqual(push.check_keys(p256dh + "=", auth + "=="), (p256dh, auth), "padding is accepted and dropped")
        for bad in ((p256dh[:-4], auth), (p256dh, auth[:-2]), (b64u(b"\x02" + b"x" * 64), auth),
                    ("not base64!", auth), (p256dh, None), (b64u(b"\x04" + b"x" * 64) + "AA", auth)):
            with self.assertRaises(HTTPException):
                push.check_keys(*bad)

    def test_the_device_id_is_the_one_the_page_computes(self):
        url = endpoint("a")
        self.assertEqual(push.device_id(url), "dev-" + sha256("dojo-push:" + url)[:20])
        self.assertRegex(push.device_id(url), push.DEVICE_ID)
        self.assertNotEqual(push.device_id(url), push.device_id(endpoint("b")))

    def test_labels_times_and_days(self):
        self.assertEqual(push.clean_label("Edge on <b>Windows</b>\n"), "Edge on bWindows/b")
        self.assertEqual(push.clean_label(""), "A browser")
        self.assertEqual(len(push.clean_label("x" * 99)), 40)
        for good in ("00:00", "08:05", "23:55"):
            self.assertEqual(push.check_time(good), good)
        for bad in ("8:00", "08:07", "24:00", "08:00:00", "", None):
            with self.assertRaises(HTTPException):
                push.check_time(bad)
        self.assertEqual(push.check_days([4, 0, 0]), [0, 4])
        for bad in ([], [7], [-1], ["1"], [True], None):
            with self.assertRaises(HTTPException):
                push.check_days(bad)


# ---------------------------------------------------------------- when a nudge may go

class ScheduleTests(unittest.TestCase):
    def doc(self, at="08:00", days=(0, 1, 2, 3, 4), last=None, devices=1):
        return {"time": at, "days": list(days), "last_day": last, "devices": [{"id": "dev-x"}] * devices}

    def test_the_chosen_time_on_the_chosen_days_for_two_hours(self):
        fri = date(2026, 10, 23)   # summer time: 08:00 in Berlin is 06:00 UTC
        self.assertEqual(push.due_day(self.doc(), utc(2026, 10, 23, 6)), fri)
        self.assertEqual(push.due_day(self.doc(), utc(2026, 10, 23, 7, 59)), fri)
        self.assertIsNone(push.due_day(self.doc(), utc(2026, 10, 23, 5, 59)), "not before the chosen time")
        self.assertIsNone(push.due_day(self.doc(), utc(2026, 10, 23, 8)), "the window is two hours")
        self.assertIsNone(push.due_day(self.doc(days=[0]), utc(2026, 10, 23, 6)), "not on a day left out")
        self.assertIsNone(push.due_day(self.doc(), utc(2026, 10, 24, 6)), "Saturday is not a default day")

    def test_at_most_once_a_day(self):
        self.assertIsNone(push.due_day(self.doc(last="2026-10-23"), utc(2026, 10, 23, 6, 30)))
        self.assertEqual(push.due_day(self.doc(last="2026-10-22"), utc(2026, 10, 23, 6, 30)), date(2026, 10, 23))

    def test_nothing_without_a_device(self):
        self.assertIsNone(push.due_day(self.doc(devices=0), utc(2026, 10, 23, 6, 30)))
        self.assertIsNone(push.due_day({**self.doc(), "time": "bad"}, utc(2026, 10, 23, 6, 30)))

    def test_berlin_time_across_both_clock_changes_of_2026(self):
        # Autumn: the clocks go back on Sunday 25 October. 08:00 is 06:00 UTC before, 07:00 UTC after.
        self.assertEqual(push.due_day(self.doc(), utc(2026, 10, 23, 6)), date(2026, 10, 23))
        self.assertIsNone(push.due_day(self.doc(), utc(2026, 10, 26, 6, 30)), "07:30 in Berlin in winter time")
        self.assertEqual(push.due_day(self.doc(), utc(2026, 10, 26, 7)), date(2026, 10, 26))
        # Spring: the clocks go forward on Sunday 29 March. 08:00 is 07:00 UTC before, 06:00 UTC after.
        self.assertIsNone(push.due_day(self.doc(), utc(2026, 3, 27, 6, 30)))
        self.assertEqual(push.due_day(self.doc(), utc(2026, 3, 27, 7)), date(2026, 3, 27))
        self.assertEqual(push.due_day(self.doc(), utc(2026, 3, 30, 6)), date(2026, 3, 30))
        # A time in the hour that does not exist on 29 March is read as winter time; it still comes once.
        gap = self.doc(at="02:30", days=[6])
        self.assertEqual(push.due_day(gap, utc(2026, 3, 29, 1, 30)), date(2026, 3, 29))
        self.assertIsNone(push.due_day(gap, utc(2026, 3, 29, 1, 29)))
        # The hour that happens twice on 25 October: the first of the two.
        twice = self.doc(at="02:30", days=[6])
        self.assertEqual(push.due_day(twice, utc(2026, 10, 25, 0, 30)), date(2026, 10, 25))

    def test_a_window_over_midnight_belongs_to_the_day_it_started(self):
        late = self.doc(at="23:30", days=[4])   # Friday only
        self.assertEqual(push.due_day(late, utc(2026, 10, 23, 22, 15)), date(2026, 10, 23), "00:15 on Saturday")
        self.assertIsNone(push.due_day(late, utc(2026, 10, 23, 21, 29)))
        self.assertIsNone(push.due_day({**late, "last_day": "2026-10-23"}, utc(2026, 10, 23, 22, 15)))

    def test_what_is_due_is_judged_for_the_day_the_nudge_belongs_to(self):
        after = utc(2026, 10, 23, 22, 15)   # 00:15 on Saturday in Berlin
        self.assertEqual(push.as_of(date(2026, 10, 23), after), utc(2026, 10, 23, 21, 59, 59))
        self.assertEqual(push.as_of(date(2026, 10, 24), after), after)
        self.assertEqual(push.as_of(None, after), after)
        winter = utc(2026, 11, 6, 23, 15)   # 00:15 on Saturday 7 November, winter time
        self.assertEqual(push.as_of(date(2026, 11, 6), winter), utc(2026, 11, 6, 22, 59, 59))

    def test_the_words_are_calm_and_count_only(self):
        self.assertEqual(push.recall_message(1, 2)["body"], "1 recall is ready, about 2 minutes.")
        self.assertEqual(push.recall_message(2, 4), {"title": "Dojo", "body": "2 recalls are ready, about 4 minutes.", "url": "/?go=five"})
        self.assertEqual(push.plan_message(1, 25)["body"], "Your plan has 1 session today, about 25 minutes.")
        self.assertEqual(push.plan_message(1, 25)["url"], "/?go=today")
        words = " ".join(m["body"].lower() for m in (push.recall_message(3, 6), push.plan_message(2, 50), push.TEST_MESSAGE))
        for word in ("streak", "lose", "lost", "miss", "overdue", "behind", "don't", "hurry", "last chance", "!"):
            self.assertNotIn(word, words)

    def test_planned_today_counts_only_what_is_still_planned_today(self):
        now = utc(2026, 10, 23, 6)
        today = [{"state": "planned", "kind": "lesson", "start": "2026-10-23T16:00:00+00:00", "minutes": 25},
                 {"state": "planned", "kind": "check3", "start": "2026-10-23T21:30:00+00:00", "minutes": 3},
                 {"state": "done", "kind": "lesson", "start": "2026-10-23T10:00:00+00:00", "minutes": 25},
                 {"state": "planned", "kind": "recall", "start": "2026-10-23T09:00:00+00:00", "minutes": 4},
                 {"state": "planned", "kind": "lesson", "start": "2026-10-23T22:30:00+00:00", "minutes": 25},  # Saturday in Berlin
                 {"state": "planned", "kind": "lesson", "start": "2026-10-22T16:00:00+00:00", "minutes": 25}]
        self.assertEqual(push.planned_today({"sessions": today}, now), (2, 28))
        self.assertEqual(push.planned_today(None, now), (0, 0))


# ---------------------------------------------------------------- the VAPID key pair

class VapidTests(unittest.TestCase):
    def test_made_once_at_first_need_and_kept_on_the_data_share(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-vapid-"))
        store = Store(tmp)
        self.assertFalse((tmp / "push" / "vapid.json").exists(), "nothing is made before it is needed")
        keys = push.VapidKeys(store)
        public = keys.public()
        doc = json.loads((tmp / "push" / "vapid.json").read_text("utf-8"))
        self.assertEqual(doc["public"], public)
        self.assertIn("BEGIN PRIVATE KEY", doc["private_pem"])
        point = base64.urlsafe_b64decode(public + "=" * (-len(public) % 4))
        self.assertEqual((len(point), point[0]), (65, 4))
        self.assertEqual(push.VapidKeys(Store(tmp)).public(), public, "the same pair after a restart")
        signer = keys.signer()
        self.assertEqual(b64u(signer.public_key.public_bytes(serialization.Encoding.X962,
                                                             serialization.PublicFormat.UncompressedPoint)), public)

    def test_the_key_file_is_never_in_git(self):
        ignored = (ROOT / ".gitignore").read_text("utf-8")
        self.assertNotIn("vapid", " ".join(p.name.lower() for p in ROOT.rglob("*vapid*") if ".venv" not in p.parts
                                                and "node_modules" not in p.parts and p.suffix == ".json"))
        self.assertTrue(ignored)  # the data share is outside the repository; nothing named vapid*.json is in it

    def test_created_exclusively_and_readable_by_the_owner_only(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-vapid-"))
        public = push.VapidKeys(Store(tmp)).public()
        path = tmp / "push" / "vapid.json"
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(push._create_vapid(path, push._new_vapid()), "a second creator cannot replace the key")
        self.assertEqual(push.VapidKeys(Store(tmp)).public(), public)

    def test_a_creator_that_loses_the_race_uses_the_winners_key(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-vapid-"))
        path = tmp / "push" / "vapid.json"
        winner = push._new_vapid()
        real = push._new_vapid

        def made_while_another_process_wins():
            mine = real()
            push._create_vapid(path, winner)   # the other process writes its key first
            return mine
        with mock.patch("app.push._new_vapid", made_while_another_process_wins):
            self.assertEqual(push.VapidKeys(Store(tmp)).public(), winner["public"])
        self.assertEqual(json.loads(path.read_text("utf-8"))["public"], winner["public"])

    def test_two_processes_at_once_end_with_one_key(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-vapid-"))
        got: list[str] = []
        go = threading.Barrier(6)

        def one():
            keys = push.VapidKeys(Store(tmp))   # its own Store, so its own lock: like another process
            go.wait()
            got.append(keys.public())
        threads = [threading.Thread(target=one) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(got), 6)
        self.assertEqual(len(set(got)), 1)

    def test_a_key_still_being_written_is_waited_for_and_a_broken_one_never_replaced(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-vapid-"))
        path = tmp / "push" / "vapid.json"
        path.parent.mkdir(parents=True)
        path.write_text("", "utf-8")   # another process has created it and is writing
        winner = push._new_vapid()
        threading.Timer(0.2, lambda: path.write_text(json.dumps(winner), "utf-8")).start()
        self.assertEqual(push.VapidKeys(Store(tmp)).public(), winner["public"])
        path.write_text("{broken", "utf-8")
        with mock.patch.object(push, "VAPID_WAIT_S", 0.1), self.assertLogs("dojo.push", logging.ERROR):
            with self.assertRaises(HTTPException) as e:
                push.VapidKeys(Store(tmp)).public()
        self.assertEqual(e.exception.status_code, 503)
        self.assertEqual(path.read_text("utf-8"), "{broken", "never replaced silently")


# ---------------------------------------------------------------- devices and the nudger

class NudgesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-push-"))
        self.store = Store(self.tmp)
        self.a, self.b = learner("ana"), learner("ben")
        self.allowed = {self.a.key: self.a, self.b.key: self.b}
        self.message = push.recall_message(2, 4)
        self.exam: set[str] = set()
        self.sender = FakeSender()
        self.slots = FakeSlots()
        self.n = push.Nudges(self.store, self.slots, due=lambda l, day: self.message, may=lambda k: self.allowed.get(k),
                             busy=lambda l: l.key in self.exam, send=self.sender)

    def on(self, who: Learner, name: str, **kw) -> dict:
        d = device(name, **kw)
        return self.n.add_device(who, d["endpoint"], d["p256dh"], d["auth"], d["label"])

    def everyday(self, who: Learner, at: str = "08:00") -> None:
        self.n.save_settings(who, at, [0, 1, 2, 3, 4, 5, 6])

    def test_the_view_has_the_public_key_and_nothing_private(self):
        v = self.n.view(self.a)
        self.assertEqual((v["on"], v["devices"], v["time"], v["days"]), (False, [], "08:00", [0, 1, 2, 3, 4]))
        self.assertEqual(v["public_key"], self.n.keys.public())
        self.on(self.a, "a1")
        text = json.dumps(self.n.view(self.a))
        doc = self.store.read("push", "vapid")
        self.assertNotIn(doc["private_pem"].splitlines()[1], text)
        self.assertNotIn("PRIVATE", text)
        for secret in ("endpoint", "p256dh", "auth", "fcm.googleapis.com"):
            self.assertNotIn(secret, text, "a device's address and keys never go back to the page")
        self.assertFalse((self.tmp / "learners" / self.b.key / "push.json").exists(), "reading makes no file")

    def test_add_dedupes_caps_and_removes(self):
        v = self.on(self.a, "a1")
        self.assertEqual((v["on"], len(v["devices"])), (True, 1))
        d = device("a1")
        v = self.n.add_device(self.a, d["endpoint"], d["p256dh"], d["auth"], "Chrome on Android")
        self.assertEqual([x["label"] for x in v["devices"]], ["Chrome on Android"], "the same endpoint is one device")
        for i in range(2, 6):
            self.on(self.a, f"a{i}")
        with self.assertRaises(HTTPException) as e:
            self.on(self.a, "a6")
        self.assertEqual(e.exception.status_code, 409)
        first = v["devices"][0]["id"]
        v = self.n.remove_device(self.a, first)
        self.assertEqual(len(v["devices"]), 4)
        self.assertNotIn(first, [x["id"] for x in v["devices"]])
        self.assertEqual(len(self.n.remove_device(self.a, "dev-00000000000000000000")["devices"]), 4)
        with self.assertRaises(HTTPException):
            self.on(self.a, "bad", host="evil.example.com")
        self.n.off(self.a)
        self.assertFalse((self.tmp / "learners" / self.a.key / "push.json").exists(), "off everywhere deletes the file")
        self.assertEqual(self.n.view(self.a)["devices"], [])

    def test_two_learners_never_see_each_others_devices(self):
        self.on(self.a, "a1")
        self.on(self.b, "b1")
        self.assertEqual(len(self.n.view(self.a)["devices"]), 1)
        b_id = self.n.view(self.b)["devices"][0]["id"]
        self.n.remove_device(self.a, b_id)
        self.assertEqual(len(self.n.view(self.b)["devices"]), 1, "a cannot remove b's device")
        self.n.off(self.a)
        self.assertEqual(len(self.n.view(self.b)["devices"]), 1)
        with self.assertRaises(HTTPException) as e:
            self.n.test(self.a, b_id)
        self.assertEqual(e.exception.status_code, 404)
        self.assertEqual(sorted(self.n.keys_with_push()), [self.b.key])

    def test_once_a_day_at_the_chosen_time_and_only_when_due(self):
        self.on(self.a, "a1")
        self.on(self.a, "a2")
        self.everyday(self.a)
        early, now = utc(2026, 10, 23, 5, 55), utc(2026, 10, 23, 6, 5)
        self.assertEqual(self.n.nudge_one(self.a.key, early), "not_now")
        self.message = None
        self.assertEqual(self.n.nudge_one(self.a.key, now), "nothing_due")
        self.assertEqual(self.sender.sent, [])
        self.message = push.recall_message(2, 4)
        self.assertEqual(self.n.nudge_one(self.a.key, now), "sent")
        self.assertEqual(len(self.sender.sent), 2, "one nudge, to each of the learner's devices")
        self.assertEqual({p["body"] for _, p in self.sender.sent}, {"2 recalls are ready, about 4 minutes."})
        self.assertEqual(self.n.nudge_one(self.a.key, now + timedelta(minutes=5)), "not_now", "at most one a day")
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 24, 6, 5)), "sent", "the next day again")
        self.assertEqual(self.n.view(self.a)["last_day"], "2026-10-24")
        self.assertEqual(self.slots.taken, 0, "the slot is given back")
        self.assertEqual(self.n.nudge_one(self.b.key, now), "off", "b never turned nudges on")

    def test_not_during_an_exam_not_for_a_removed_member_and_not_without_a_slot(self):
        self.on(self.a, "a1")
        self.everyday(self.a)
        now = utc(2026, 10, 23, 6, 5)
        self.exam.add(self.a.key)
        self.assertEqual(self.n.nudge_one(self.a.key, now), "exam")
        self.exam.clear()
        del self.allowed[self.a.key]
        self.assertEqual(self.n.nudge_one(self.a.key, now), "not_allowed")
        self.allowed[self.a.key] = self.a
        self.slots.free = False
        self.assertEqual(self.n.nudge_one(self.a.key, now), "no_slot")
        self.assertIsNone(self.n.view(self.a)["last_day"], "a skipped look does not use up the day")
        self.slots.free = True
        self.assertEqual(self.n.nudge_one(self.a.key, now), "sent")
        self.assertEqual(len(self.sender.sent), 1)

    def test_a_window_over_midnight_asks_what_is_due_for_the_chosen_day(self):
        self.on(self.a, "a1")
        self.n.save_settings(self.a, "23:30", [4])   # Fridays only
        days: list = []
        self.n.due = lambda l, day: days.append(day) or self.message
        # 00:20 on Saturday 24 October in Berlin (summer time): still Friday's nudge.
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 23, 22, 20)), "sent")
        self.assertEqual(set(days), {date(2026, 10, 23)})
        self.assertEqual(self.n.view(self.a)["last_day"], "2026-10-23")

    def test_the_day_is_reserved_before_the_first_message_goes_out(self):
        self.on(self.a, "a1")
        self.on(self.a, "a2")
        self.everyday(self.a)
        now = utc(2026, 10, 23, 6, 5)
        seen: list = []

        def crash_after_the_push_service_took_it(dev, payload):
            seen.append(self.store.read("learners", self.a.key, "push")["last_day"])
            raise SystemExit("the app restarts")
        self.n.send = crash_after_the_push_service_took_it
        with self.assertRaises(SystemExit):
            self.n.nudge_one(self.a.key, now)
        self.assertEqual(seen, ["2026-10-23"], "written before the first POST")
        self.assertEqual(self.slots.taken, 0)
        self.n.send = self.sender
        self.assertEqual(self.n.nudge_one(self.a.key, now + timedelta(minutes=5)), "not_now", "never sent twice")
        self.assertEqual(self.sender.sent, [])
        self.sender.status = {endpoint("a1"): 500, endpoint("a2"): 500}
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 24, 6, 5)), "failed")
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 24, 6, 10)), "not_now", "a failed send loses the day")

    def test_everything_is_looked_at_again_once_the_slot_is_taken(self):
        self.on(self.a, "a1")
        self.everyday(self.a)
        now = utc(2026, 10, 23, 6, 5)
        real = self.slots.acquire
        changes = {
            "exam": lambda: self.exam.add(self.a.key),
            "not_allowed": lambda: self.allowed.pop(self.a.key),
            "nothing_due": lambda: setattr(self, "message", None),
            "off": lambda: self.n.off(self.a),
            "not_now": lambda: self.n.save_settings(self.a, "12:00", [0, 1, 2, 3, 4, 5, 6]),
        }
        for want, change in changes.items():
            with self.subTest(want):
                self.on(self.a, "a1")
                self.everyday(self.a)

                def acquire(timeout=None, change=change):
                    change()   # while the nudger waited for its slot
                    return real(timeout)
                with mock.patch.object(self.slots, "acquire", acquire):
                    self.assertEqual(self.n.nudge_one(self.a.key, now), want)
                self.assertEqual(self.sender.sent, [])
                self.assertEqual(self.slots.taken, 0)
                doc = self.store.read("learners", self.a.key, "push")
                self.assertIsNone((doc or {}).get("last_day"), "the day is not used up")
                self.exam.clear()
                self.allowed[self.a.key] = self.a
                self.message = push.recall_message(2, 4)

    def test_an_exam_or_removal_between_two_devices_stops_the_rest(self):
        changes = {"exam": lambda: self.exam.add(self.a.key), "not_allowed": lambda: self.allowed.pop(self.a.key)}
        for i, (why, change) in enumerate(changes.items()):
            with self.subTest(why):
                self.n.off(self.a)
                for name in ("a1", "a2", "a3"):
                    self.on(self.a, name)
                self.everyday(self.a)
                sent: list = []

                def send(dev, payload, change=change):
                    sent.append(dev["endpoint"])
                    if len(sent) == 1:
                        change()   # opened right after device 1 got its nudge
                    return 201
                self.n.send = send
                self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 23 + i, 6, 5)), "sent")
                self.assertEqual(sent, [endpoint("a1")], "devices 2 and 3 get nothing")
                v = self.n.view(self.a)
                self.assertEqual(v["last_day"], f"2026-10-{23 + i}", "the day stays used: at most once")
                self.assertEqual([bool(d["last_ok"]) for d in v["devices"]], [True, False, False])
                self.assertEqual(len(v["devices"]), 3, "the skipped devices are kept")
                self.assertEqual(self.slots.taken, 0)
                self.exam.clear()
                self.allowed[self.a.key] = self.a
        self.n.send = self.sender
        calls: list = []
        self.n.busy = lambda l: calls.append(1) or len(calls) > 2   # before the slot, after it, then before device 1
        self.n.off(self.a)
        self.on(self.a, "a1")
        self.everyday(self.a)
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 26, 7, 5)), "exam", "nothing went out")
        self.assertEqual(self.sender.sent, [])

    def test_gone_devices_are_removed_and_failing_ones_after_five_tries(self):
        self.on(self.a, "gone")
        self.on(self.a, "flaky")
        self.on(self.a, "fine")
        self.everyday(self.a)
        self.sender.status = {endpoint("gone"): 410, endpoint("flaky"): 500}
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 1, 6, 5)), "sent")
        labels = {d["id"] for d in self.n.view(self.a)["devices"]}
        self.assertNotIn(push.device_id(endpoint("gone")), labels, "410 removes the device at once")
        self.assertEqual(len(labels), 2)
        for day in range(2, 5):
            self.n.nudge_one(self.a.key, utc(2026, 10, day, 6, 5))
        self.assertIn(push.device_id(endpoint("flaky")), {d["id"] for d in self.n.view(self.a)["devices"]}, "four failures")
        self.n.nudge_one(self.a.key, utc(2026, 10, 5, 6, 5))
        self.assertEqual([d["id"] for d in self.n.view(self.a)["devices"]], [push.device_id(endpoint("fine"))])
        self.sender.status = {endpoint("fine"): 404}
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 7, 6, 5)), "failed")
        self.assertEqual(self.n.view(self.a)["devices"], [], "404 is gone too")
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 8, 6, 5)), "not_now", "no device, no nudge")

    def test_a_success_resets_the_failures(self):
        self.on(self.a, "flaky")
        self.everyday(self.a)
        self.sender.status = {endpoint("flaky"): 503}
        for day in range(1, 5):
            self.n.nudge_one(self.a.key, utc(2026, 10, day, 6, 5))
        self.sender.status = {}
        self.n.nudge_one(self.a.key, utc(2026, 10, 5, 6, 5))
        self.sender.status = {endpoint("flaky"): 503}
        for day in range(6, 10):
            self.n.nudge_one(self.a.key, utc(2026, 10, day, 6, 5))
        self.assertEqual(len(self.n.view(self.a)["devices"]), 1)

    def test_a_device_subscribed_with_another_key_is_dropped_without_sending(self):
        self.on(self.a, "old")
        self.everyday(self.a)
        doc = self.store.read("push", "vapid")
        self.store.write("push", "vapid", value={**push._new_vapid(), "created": doc["created"]})
        self.assertEqual(self.n.nudge_one(self.a.key, utc(2026, 10, 23, 6, 5)), "failed")
        self.assertEqual(self.sender.sent, [], "nothing goes out under a key the browser does not know")
        self.assertEqual(self.n.view(self.a)["devices"], [])

    def test_a_test_nudge_at_most_once_a_minute(self):
        dev_id = self.on(self.a, "a1")["devices"][0]["id"]
        out = self.n.test(self.a, dev_id)
        self.assertTrue(out["sent"])
        self.assertEqual(self.sender.sent[0][1], push.TEST_MESSAGE)
        with self.assertRaises(HTTPException) as e:
            self.n.test(self.a, dev_id)
        self.assertEqual(e.exception.status_code, 429)
        self.assertIsNone(self.n.view(self.a)["last_day"], "a test does not use up the day's nudge")

    def test_a_learner_deleted_while_a_nudge_is_on_its_way_leaves_nothing(self):
        self.on(self.a, "a1")
        self.everyday(self.a)

        def send_and_delete(dev, payload):
            self.store.tombstone(self.a.key)
            return 201
        self.n.send = send_and_delete
        with self.assertRaises(Abandoned):
            self.n.nudge_one(self.a.key, utc(2026, 10, 23, 6, 5))
        self.assertEqual(self.slots.taken, 0)
        self.assertEqual(self.n.tick(utc(2026, 10, 24, 6, 5)).get("deleted"), 1, "the tick carries on")

    def test_one_learners_error_does_not_stop_the_others(self):
        for who in (self.a, self.b):
            self.on(who, who.name)
            self.everyday(who)
        real = self.n.due
        self.n.due = lambda l, day: (_ for _ in ()).throw(RuntimeError("boom")) if l.key == self.a.key else real(l, day)
        with self.assertLogs("dojo.push", logging.ERROR):
            counts = self.n.tick(utc(2026, 10, 23, 6, 5))
        self.assertEqual((counts.get("error"), counts.get("sent")), (1, 1))

    def test_the_thread_starts_and_stops(self):
        self.n.start()
        self.assertTrue(self.n._thread.is_alive())
        self.n.stop()
        self.n._thread.join(5)
        self.assertFalse(self.n._thread.is_alive())

    def test_nothing_private_in_the_logs(self):
        self.on(self.a, "a1")
        self.everyday(self.a)
        self.sender.status = {endpoint("a1"): 500}
        with self.assertLogs("dojo", logging.DEBUG) as logs:
            logging.getLogger("dojo").debug("start")
            self.n.tick(utc(2026, 10, 23, 6, 5))
        text = "\n".join(logs.output)
        self.assertNotIn("fcm.googleapis.com", text)
        self.assertNotIn(self.store.read("push", "vapid")["private_pem"].splitlines()[1], text)
        self.assertNotIn(self.a.key, text)


class RealSenderTests(unittest.TestCase):
    """pywebpush itself, with a fake requests session: the payload is encrypted and signed with VAPID."""

    def test_an_encrypted_signed_request_to_the_endpoint(self):
        keys = push.VapidKeys(Store(Path(tempfile.mkdtemp(prefix="dojo-sender-"))))
        calls = []

        class Resp:
            def __init__(self, code):
                self.status_code, self.text, self.headers, self.reason = code, "", {}, "x"

        class Session:
            code = 201

            def post(self, url, data=None, headers=None, timeout=None):
                calls.append((url, data, headers, timeout))
                return Resp(Session.code)
        dev = device("real")
        send = push.webpush_sender(keys, session=Session())
        message = push.recall_message(2, 4)
        with mock.patch.dict(os.environ, {"WEBSITE_HOSTNAME": "dojo.example.net"}):
            self.assertEqual(send(dev, message), 201)
        url, data, headers, timeout = calls[0]
        self.assertEqual(url, dev["endpoint"])
        self.assertEqual(headers["content-encoding"], "aes128gcm")
        self.assertEqual(headers["ttl"], str(push.TTL_S))
        self.assertTrue(headers["authorization"].startswith("vapid t="))
        self.assertIn(f"k={keys.public()}", headers["authorization"])
        self.assertNotIn(b"recalls are ready", data, "the payload is encrypted")
        claims = json.loads(base64.urlsafe_b64decode(headers["authorization"].split("t=")[1].split(",")[0].split(".")[1] + "=="))
        self.assertEqual((claims["aud"], claims["sub"]), ("https://fcm.googleapis.com", "https://dojo.example.net"))
        self.assertLessEqual(timeout, push.SEND_TIMEOUT_S)
        Session.code = 410
        self.assertEqual(send(dev, message), 410, "a refusal comes back as its status code")
        Session.post = lambda self, *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
        self.assertEqual(send(dev, message), 0, "an unreachable service is a failure, not a crash")


# ---------------------------------------------------------------- the 5-minute session, alone

class FakeDojo:
    def __init__(self, store, recalls: dict, errors: dict):
        self.store = store
        self._recalls, self._errors = recalls, errors
        self.packages = mock.Mock()
        self.packages.by_id = {"gh-300": {}, "dp-800": {}}
        self.packages.get = lambda pid: {"meta": {"exam": pid.upper()}}
        self.active = "gh-300"

    def profile(self, who):
        return {"active_package": self.active}

    def recalls(self, who, pid, at=None):
        self.at = at
        return self._recalls.get(pid) or {"due": [], "waiting": 0}

    def calibration(self, who, pid):
        return {"errors": self._errors.get(pid) or []}


def due(*skills: str) -> list[dict]:
    return [{"skill": s, "text": f"Skill {s}", "repair": None, "last_success": None, "missed": False} for s in skills]


def error(skill: str) -> dict:
    return {"skill": skill, "text": f"Skill {skill}", "domain": "D", "label": "Very sure", "stem": "Which?",
            "where": {"lesson": "les-0123456789abcdef0123", "section": 0, "heading": "H"}}


class FiveTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp(prefix="dojo-five-")))
        self.me = learner("ana")
        self.checks = mock.Mock()
        self.checks.open_session.return_value = None
        self.checks.create.return_value = {"id": "check-0123456789abcdef0123"}

    def five(self, recalls=None, errors=None):
        self.dojo = FakeDojo(self.store, recalls or {}, errors or {})
        return FiveMinutes(self.dojo, self.checks)

    def test_two_recalls_then_one_repair_that_is_not_among_them(self):
        f = self.five({"gh-300": {"due": due("1.1", "1.2", "1.3"), "waiting": 2}},
                      {"gh-300": [error("1.1"), error("2.4"), error("3.1")]})
        v = f.view(self.me)
        self.assertEqual((v["package"], v["exam"]), ("gh-300", "GH-300"))
        self.assertEqual([s["skill"] for s in v["recalls"]["skills"]], ["1.1", "1.2"])
        self.assertEqual((v["recalls"]["count"], v["recalls"]["minutes"], v["recalls"]["later"]), (2, 4, 3))
        self.assertEqual(v["repair"]["skill"], "2.4", "the repair is a skill the recalls do not ask")
        self.assertEqual(v["minutes"], 4 + REPAIR_MINUTES)
        self.assertIsNone(v["check"])
        f.start(self.me)
        self.checks.create.assert_called_with(self.me, "gh-300", recall=True, limit=FIVE_RECALLS)

    def test_the_exam_with_something_due_comes_first(self):
        f = self.five({"dp-800": {"due": due("4.1"), "waiting": 0}})
        v = f.view(self.me)
        self.assertEqual((v["package"], v["recalls"]["count"]), ("dp-800", 1))
        self.assertEqual(f.nudge(self.me)["body"], "1 recall is ready, about 2 minutes.")
        f = self.five()
        v = f.view(self.me)
        self.assertEqual((v["package"], v["recalls"]["count"], v["repair"], v["minutes"]), ("gh-300", 0, None, 0))

    def test_an_open_recall_check_is_resumed_not_started_again(self):
        self.checks.open_session.return_value = {"id": "check-aaaaaaaaaaaaaaaaaaaa", "recall": True}
        self.assertEqual(self.five({"gh-300": {"due": due("1.1"), "waiting": 0}}).view(self.me)["check"], "check-aaaaaaaaaaaaaaaaaaaa")
        self.five({"gh-300": {"due": due("1.1"), "waiting": 0}}).start(self.me)
        self.checks.end.assert_not_called()
        self.checks.create.assert_called_with(self.me, "gh-300", recall=True, limit=FIVE_RECALLS)
        self.checks.open_session.return_value = {"id": "check-bbbbbbbbbbbbbbbbbbbb", "recall": False}
        self.assertIsNone(self.five().view(self.me)["check"], "an ordinary check is not the 5 minutes")

    def test_an_open_ordinary_check_is_never_taken_over_unasked(self):
        self.checks.open_session.return_value = {"id": "check-bbbbbbbbbbbbbbbbbbbb", "recall": False, "minutes": 3}
        f = self.five({"gh-300": {"due": due("1.1"), "waiting": 0}})
        self.assertEqual(f.view(self.me)["open_check"], {"id": "check-bbbbbbbbbbbbbbbbbbbb", "minutes": 3})
        self.checks.create.reset_mock()
        with self.assertRaises(HTTPException) as e:
            f.start(self.me)
        self.assertEqual(e.exception.status_code, 409)
        self.assertIn("You have a check open", e.exception.detail)
        self.checks.create.assert_not_called()
        self.checks.end.assert_not_called()
        order = mock.Mock()
        self.checks.end.side_effect = lambda who, cid: order.end(cid)
        self.checks.create.side_effect = lambda *a, **k: (order.create(), {"id": "check-cccccccccccccccccccc"})[1]
        self.assertEqual(f.start(self.me, end_open=True)["id"], "check-cccccccccccccccccccc")
        self.assertEqual([c[0] for c in order.mock_calls], ["end", "create"], "ended first, then the recalls")
        self.checks.end.assert_called_once_with(self.me, "check-bbbbbbbbbbbbbbbbbbbb")
        self.checks.open_session.return_value = None
        self.assertIsNone(self.five().view(self.me)["open_check"])

    def test_the_nudge_judges_the_chosen_day_when_its_window_ran_over_midnight(self):
        f = self.five({"gh-300": {"due": due("1.1"), "waiting": 0}})
        after_midnight = utc(2026, 10, 23, 22, 20)   # 00:20 on 24 October in Berlin (summer time)
        with mock.patch("app.five.utcnow", lambda: after_midnight):
            f.nudge(self.me, date(2026, 10, 23))
            self.assertEqual(self.dojo.at, utc(2026, 10, 23, 21, 59, 59), "the last second of the chosen day")
            f.nudge(self.me, date(2026, 10, 24))
            self.assertEqual(self.dojo.at, after_midnight)
            f.nudge(self.me)
            self.assertEqual(self.dojo.at, after_midnight)
            f = self.five()
            self.store.write("learners", self.me.key, "plan", value={"sessions": [
                {"state": "planned", "kind": "lesson", "start": "2026-10-23T19:00:00+00:00", "minutes": 25},
                {"state": "planned", "kind": "lesson", "start": "2026-10-24T08:00:00+00:00", "minutes": 40}]})
            self.assertEqual(f.nudge(self.me, date(2026, 10, 23)), push.plan_message(1, 25), "Friday's plan, not Saturday's")
            self.assertEqual(f.nudge(self.me, date(2026, 10, 24)), push.plan_message(1, 40))

    def test_the_nudge_says_recalls_first_then_the_plan_then_nothing(self):
        f = self.five({"gh-300": {"due": due("1.1", "1.2", "1.3"), "waiting": 0}})
        self.assertEqual(f.nudge(self.me), push.recall_message(2, 4))
        f = self.five()
        self.assertIsNone(f.nudge(self.me))
        # Noon of the same (UTC) day, so the test does not fail in the half hour before midnight.
        start = utcnow().replace(hour=12, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        with mock.patch("app.push.planmod.to_local", lambda t: t.replace(tzinfo=None)):
            self.store.write("learners", self.me.key, "plan", value={"sessions": [
                {"state": "planned", "kind": "lesson", "start": start, "minutes": 25}]})
            self.assertEqual(f.nudge(self.me), push.plan_message(1, 25))


# ---------------------------------------------------------------- through the app

class PhoneApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-phone-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.dojo = cls.app.state.dojo
        cls.store = cls.dojo.store
        cls.nudges = cls.app.state.nudges
        cls.sender = FakeSender()
        cls.nudges.send = cls.sender
        cls.pid = "gh-300"
        cls.me = Learner("local:dev", learner_key("local:dev"), "Local learner")
        pkg = Packages(APP_DIR / "packages").get(cls.pid)
        cls.sid = next(s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]
                       if pkg["skills"][s]["sources"] and pkg["skills"][s]["kind"] == "scenario")
        # A skill shown on its own three days ago and again two days ago: its recall is due tomorrow.
        for days in (3, 2):
            item = cls.wait(cls.post("/api/items", {"package": cls.pid, "skill": cls.sid, "mode": "check"}))["item"]
            with mock.patch("app.core.utcnow", lambda d=days: utcnow() - timedelta(days=d)):
                cls.wait(cls.post(f"/api/items/{item}/answer", {"text": "Everything: r1 and r2 both apply here.", "elapsed_s": 30}))

    @classmethod
    def post(cls, path: str, body: dict | None = None) -> dict:
        r = cls.c.post(path, json=body, headers=POST)
        assert r.status_code < 300, r.text
        return r.json()

    @classmethod
    def wait(cls, job: dict) -> dict:
        for _ in range(900):
            job = cls.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert job["state"] == "done", job.get("error")
        return job["result"]

    def tomorrow(self):
        later = utcnow() + timedelta(days=1)
        return later, [mock.patch(f"app.{m}.utcnow", lambda: later) for m in ("core", "learning", "checks", "five")]

    def test_01_the_routes_are_learner_routes(self):
        routes = {r.path: r for r in self.app.routes if getattr(r, "path", "").startswith(("/api/push", "/api/five"))}
        self.assertEqual(set(routes), {"/api/five", "/api/five/start", "/api/push", "/api/push/settings", "/api/push/devices",
                                       "/api/push/devices/{device_id}", "/api/push/test"})
        for path, route in routes.items():
            self.assertEqual(classes(route), {"learner"}, path)

    def test_02_settings_devices_and_off_through_the_api(self):
        v = self.c.get("/api/push").json()
        self.assertEqual((v["on"], v["time"], v["max_devices"], v["window_minutes"]), (False, "08:00", 5, 120))
        self.assertNotIn("private", json.dumps(v).lower())
        self.assertEqual(self.c.put("/api/push/settings", json={"time": "07:35", "days": [0, 6]}, headers=POST).json()["days"], [0, 6])
        for bad in ({"time": "7:35", "days": [0]}, {"time": "07:33", "days": [0]}, {"time": "07:35", "days": []},
                    {"time": "07:35", "days": [7]}):
            self.assertEqual(self.c.put("/api/push/settings", json=bad, headers=POST).status_code, 422, bad)
        self.assertEqual(self.c.put("/api/push/settings", json={"time": "07:35", "days": [1]}).status_code, 403, "X-Dojo is required")
        d = device("api")
        v = self.post("/api/push/devices", d)
        dev_id = v["devices"][0]["id"]
        self.assertEqual(dev_id, push.device_id(d["endpoint"]))
        self.assertNotIn(d["endpoint"], json.dumps(v))
        evil = {**device("x"), "endpoint": "https://169.254.169.254/latest/meta-data/" + "x" * 20}
        self.assertEqual(self.c.post("/api/push/devices", json=evil, headers=POST).status_code, 400)
        self.assertEqual(self.c.post("/api/push/devices", json={**d, "p256dh": "A" * 87}, headers=POST).status_code, 400)
        self.assertTrue(self.post("/api/push/test", {"device": dev_id})["sent"])
        self.assertEqual(self.c.post("/api/push/test", json={"device": dev_id}, headers=POST).status_code, 429)
        self.assertEqual(self.c.post("/api/push/test", json={"device": "../etc"}, headers=POST).status_code, 422)
        self.assertEqual(self.c.delete("/api/push/devices/not-an-id", headers=POST).status_code, 404)
        exported = self.c.get("/api/record/export").json()
        self.assertEqual(exported["push"]["devices"][0]["label"], "Edge on Windows")
        self.assertNotIn(d["endpoint"], json.dumps(exported), "the export has the choices, not the addresses")
        self.assertEqual(self.c.delete(f"/api/push/devices/{dev_id}", headers=POST).json()["devices"], [])
        self.assertEqual(self.c.delete("/api/push", headers=POST).status_code, 200)
        self.assertIsNone(self.store.read("learners", self.me.key, "push"))
        self.assertIsNone(self.c.get("/api/record/export").json()["push"])

    def test_03_the_five_minutes_and_the_nudge_on_the_day_a_recall_is_due(self):
        v = self.c.get("/api/five").json()
        self.assertEqual((v["package"], v["recalls"]["count"], v["check"]), (self.pid, 0, None), "nothing due today")
        self.post("/api/push/devices", device("five"))
        later, patches = self.tomorrow()
        local = push.planmod.to_local(later)
        at = f"{local.hour:02d}:{local.minute - local.minute % 5:02d}"
        self.c.put("/api/push/settings", json={"time": at, "days": list(range(7))}, headers=POST)
        with patches[0], patches[1], patches[2], patches[3]:
            v = self.c.get("/api/five").json()
            self.assertEqual(([s["skill"] for s in v["recalls"]["skills"]], v["recalls"]["minutes"]), ([self.sid], 2))
            with mock.patch.object(self.app.state.exams, "open_attempt", return_value={"id": "exam-1"}):
                self.assertEqual(self.c.get("/api/five").status_code, 409)
                self.assertEqual(self.c.post("/api/five/start", headers=POST).status_code, 409)
                self.assertEqual(self.nudges.nudge_one(self.me.key, later), "exam")
            self.sender.sent.clear()
            self.assertEqual(self.nudges.nudge_one(self.me.key, later), "sent")
            self.assertEqual(self.sender.sent[0][1], {"title": "Dojo", "body": "1 recall is ready, about 2 minutes.", "url": "/?go=five"})
            self.assertEqual(self.nudges.nudge_one(self.me.key, later + timedelta(minutes=5)), "not_now")
            started = self.post("/api/five/start")
            self.assertTrue(started["recall"])
            self.assertEqual([q["skill"] for q in started["questions"]], [self.sid])
            self.assertEqual(self.c.get("/api/five").json()["check"], started["id"])
            self.assertEqual(self.post("/api/five/start")["id"], started["id"], "one check at a time")
            for _ in range(900):
                if not self.c.get(f"/api/checks/{started['id']}").json()["coming"]:
                    break
                time.sleep(0.05)
            self.post(f"/api/checks/{started['id']}/end")
        self.c.delete("/api/push", headers=POST)

    def test_04_the_check_limit_asks_fewer_recalls(self):
        later, patches = self.tomorrow()
        with patches[0], patches[1], patches[2], patches[3]:
            real = self.dojo.recalls(self.me, self.pid)["due"]
            self.assertEqual(len(real), 1)
            pkg = Packages(APP_DIR / "packages").get(self.pid)
            others = [s for s in pkg["skills"] if s != self.sid][:3]

            def many(*a, **k):
                return [dict(real[0], skill=s, text=pkg["skills"][s]["text"]) for s in [self.sid] + others]
            with mock.patch("app.checks.relearn.due_recalls", many):
                checks = self.app.state.checks
                self.assertEqual(len(checks._picks(self.me, self.pid, recall=True)[2]), 4)
                self.assertEqual(len(checks._picks(self.me, self.pid, recall=True, limit=2)[2]), 2)
                self.assertEqual(len(checks._picks(self.me, self.pid, recall=True, limit=0)[2]), 0)
                with mock.patch.object(checks, "_picks", wraps=checks._picks) as spy:
                    try:
                        checks.create(self.me, self.pid, recall=True, limit=2)
                    except HTTPException:
                        pass
                    self.assertEqual(spy.call_args.args[3:], (2,), "create passes the limit on")

    def test_04b_start_asks_before_ending_an_open_check(self):
        five = self.app.state.five
        with mock.patch.object(five, "start", return_value={"id": "check-0123456789abcdef0123"}) as start:
            self.post("/api/five/start")
            self.assertEqual(start.call_args.kwargs, {"end_open": False, "pid": "gh-300"}, "no body: never ends a check")
            self.post("/api/five/start", {"end_open": True})
            self.assertEqual(start.call_args.kwargs, {"end_open": True, "pid": "gh-300"}, "the exam the route reserved for")
        self.assertEqual(self.c.post("/api/five/start", json={"end_open": "please"}, headers=POST).status_code, 422)

    def test_04c_the_repair_counts_only_once_its_question_is_answered(self):
        js = (APP_DIR / "static" / "app.js").read_text("utf-8")
        repair = js[js.index('} else if (s.step === "repair") {'):js.index("function fiveBanner")]
        self.assertNotIn('s.step = "done"', repair, "creating the question does not finish the repair")
        self.assertNotIn("s.did += 1", repair)
        self.assertIn("fiveNote = ", repair, "a failed creation stays on the repair step with a calm note")
        answer = js[js.index("const job = await api(`/api/items/${it.id}/answer`"):]
        self.assertLess(answer.index("fiveRepaired(it.id)"), answer.index("catch (e)"), "marked done after the answer was taken")
        done = js[js.index("function fiveRepaired"):js.index("const FIVE_STEPS")]
        self.assertIn('s.step !== "repair" || s.item !== itemId', done)

    def test_05_the_worker_and_the_manifest(self):
        r = self.c.get("/static/sw.js?v=abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.headers["cache-control"], r.headers["service-worker-allowed"]), ("no-cache", "/"))
        self.assertIn("javascript", r.headers["content-type"])
        code = r.text
        self.assertNotIn('addEventListener("fetch"', code, "no fetch handler: the worker never sees a request")
        self.assertNotIn("onfetch", code)
        for call in ("caches.open", ".put(", "cache.add", "indexedDB", "localStorage"):
            self.assertNotIn(call, code, "the worker stores nothing")
        m = self.c.get("/static/manifest.webmanifest")
        self.assertEqual((m.status_code, m.headers["content-type"].split(";")[0], m.headers["cache-control"]),
                         (200, "application/manifest+json", "no-cache"))
        man = m.json()
        self.assertEqual((man["display"], man["start_url"], man["scope"]), ("standalone", man["start_url"], "/"))
        self.assertTrue(man["start_url"].startswith("/"))
        for ic in man["icons"]:
            self.assertEqual(self.c.get(ic["src"]).status_code, 200, ic["src"])
        page = self.c.get("/").text
        self.assertIn('rel="manifest"', page)
        self.assertIn('crossorigin="use-credentials"', page)
        self.assertIn('rel="apple-touch-icon"', page)
        self.assertNotIn("<script>", page, "no inline script: the worker is registered from app.js")
        self.assertIn("serviceWorker.register", (ROOT / "app" / "static" / "app.js").read_text("utf-8"))
        csp = self.c.get("/").headers.get("content-security-policy", "")
        self.assertNotIn("unsafe-inline", csp.split("script-src")[1].split(";")[0] if "script-src" in csp else "")
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        self.assertNotIn("service-worker-allowed", self.c.get("/healthz").headers)
        self.assertNotIn("service-worker-allowed", self.c.get("/static/app.js").headers)

    def test_05b_no_background_request_can_start_a_sign_in(self):
        """Easy Auth answers a request without a session with its sign-in page and a new nonce cookie, which
        replaces the nonce of a sign-in under way: that sign-in fails ("invalid nonce") and starts again,
        endlessly (live on 5 Oct 2026). So a request that does not come from a page the learner opened
        must never get that answer: what the browser fetches by itself is open, and what Dojo sends by
        itself is open or says it is not a page (X-Requested-With: Easy Auth then answers 403)."""
        bicep = (ROOT / "infra" / "main.bicep").read_text("utf-8")
        excluded = re.findall(r"'([^']*)'", re.search(r"excludedPaths:\s*\[(.*?)\]", bicep, re.S).group(1))
        sw = (APP_DIR / "static" / "sw.js").read_text("utf-8")
        # The browser checks the worker's script for a new version at every page load in its scope.
        self.assertIn("/static/sw.js", excluded)
        # A nudge's icon and badge are loaded by the browser when the nudge shows, signed in or not.
        loaded = set(re.findall(r'"(/static/[^"?]+)"', sw))
        self.assertTrue(loaded)
        self.assertLessEqual(loaded, set(excluded), "every file the worker shows must be open")
        for path in sorted(loaded | {"/static/sw.js"}):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        # Every request the worker sends to Dojo says it is not a page.
        calls = re.findall(r"fetch\((.*?)\}\);", sw, re.S)
        self.assertTrue(calls)
        for call in calls:
            self.assertIn('"X-Requested-With": "XMLHttpRequest"', call)
        js = (APP_DIR / "static" / "app.js").read_text("utf-8")
        build = js[js.index("async function serverBuild"):js.index("const buildBusy")]
        self.assertIn('fetch("/healthz"', build, "the 5-minute build check asks an open path")
        self.assertNotIn("/api/", build)
        exam = js[js.index("async function examRunning"):js.index("function showNewBuild")]
        self.assertIn('"X-Requested-With": "XMLHttpRequest"', exam)
        self.assertIn("/healthz", excluded)
        self.assertIn("build", self.c.get("/healthz").json())
        # Every other call goes through api(), which says it is not a page, treats Easy Auth's refusal (401, or
        # 403 with an empty body) as "the sign-in has ended", and never starts a sign-in on its own.
        self.assertEqual(len(re.findall(r"\bfetch\(", js)), 3, "a new fetch must not start a sign-in either")
        call = js[js.index("async function api("):js.index("async function runJob")]
        self.assertIn('"X-Requested-With": "XMLHttpRequest"', call[:call.index("await fetch(path, init)")])
        self.assertIn("if (r.status === 401) throw signInEnded();", call)
        self.assertIn("if (r.status === 403 && !text) throw signInEnded();", call)
        self.assertNotIn("/.auth/login", js)
        banner = js[js.index("function reloadToSignIn"):js.index("async function api(")]
        self.assertIn("location.replace(route ?", banner, "the way back carries the page: Easy Auth loses a # route at sign-in")
        self.assertIn("/?go=${route}", banner)
        self.assertNotIn("location.reload()", banner, "a plain reload would sign in onto the start page")
        follow = js[js.index("function follow("):js.index("function matchingPanel")]
        self.assertIn("e.signedOut) return;", follow, "a poll stops once the sign-in has ended")
        # Dojo's own refusals always carry a body, so an empty 403 can only be Easy Auth's.
        main = (ROOT / "app" / "main.py").read_text("utf-8")
        self.assertNotIn("_bare(403", main)
        self.assertNotIn("_bare(401", main)

    def test_06_the_nudger_starts_only_where_it_should(self):
        # Locally, the nudger thread does not run unless asked (DOJO_PUSH=1); the app itself starts and stops.
        with TestClient(create_app(settings(tempfile.mkdtemp(prefix="dojo-phone-life-")),
                                   http=httpx.Client(transport=httpx.MockTransport(web)))) as c:
            self.assertIsNone(c.app.state.nudges._thread)
        with mock.patch.dict(os.environ, {"DOJO_PUSH": "1"}):
            app = create_app(settings(tempfile.mkdtemp(prefix="dojo-phone-life-")), http=httpx.Client(transport=httpx.MockTransport(web)))
            with TestClient(app):
                self.assertTrue(app.state.nudges._thread.is_alive())
            app.state.nudges._thread.join(5)
            self.assertTrue(app.state.nudges._stop.is_set())


# ---------------------------------------------------------------- with Team Dojo

class PhoneTeamTests(TeamCase):
    def nudger(self):
        sender = FakeSender()
        self.app.state.nudges.send = sender
        self.app.state.nudges.due = lambda l, day: push.recall_message(1, 2)
        return self.app.state.nudges, sender

    def turn_on(self, h: dict) -> str:
        self.ok("PUT", "/api/push/settings", h, {"time": "08:00", "days": list(range(7))})
        return self.ok("POST", "/api/push/devices", h, device(h["X-MS-CLIENT-PRINCIPAL-ID"]))["devices"][0]["id"]

    def test_members_have_their_own_nudges_and_lose_them_on_leaving(self):
        n, sender = self.nudger()
        a = self.admit("m-1")
        b = self.admit("m-2")
        self.turn_on(a)
        self.turn_on(b)
        self.turn_on(OWNER)
        ka, kb = self.key("m-1"), self.key("m-2")
        self.assertTrue((self.folder(ka) / "push.json").is_file())
        self.assertEqual(len(self.ok("GET", "/api/push", a)["devices"]), 1)
        now = utc(2026, 10, 23, 6, 5)
        self.assertEqual(n.tick(now).get("sent"), 3)
        self.ok("POST", "/api/team/leave", a, {"confirm": "LEAVE"})
        self.assertFalse(self.folder(ka).exists(), "leaving deletes the nudges with everything else")
        self.assertNotIn(ka, n.keys_with_push())
        self.assertEqual(n.nudge_one(ka, utc(2026, 10, 24, 6, 5)), "off")
        self.assertEqual(len(self.ok("GET", "/api/push", b)["devices"]), 1, "the other member keeps theirs")

    def test_no_nudge_for_a_removed_member_or_while_the_team_is_paused(self):
        n, sender = self.nudger()
        a = self.admit("m-1")
        self.turn_on(a)
        ka = self.key("m-1")
        self.ok("POST", "/api/team/pause", OWNER)
        self.assertEqual(n.nudge_one(ka, utc(2026, 10, 23, 6, 5)), "not_allowed")
        self.refused("GET", "/api/push", a, "paused")
        self.assertTrue((self.folder(ka) / "push.json").is_file(), "a pause deletes nothing; it only sends nothing")
        self.ok("POST", "/api/team/resume", OWNER)
        self.ok("POST", f"/api/team/members/{self.row('m-1')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.assertEqual(n.nudge_one(ka, utc(2026, 10, 23, 6, 5)), "off")
        self.assertEqual(sender.sent, [])

    def test_removing_a_member_deletes_the_push_file_at_once_and_keeps_the_rest(self):
        n, _ = self.nudger()
        a = self.admit("m-1")
        self.turn_on(a)
        ka = self.key("m-1")
        self.ok("PUT", "/api/settings", a, {"exam_dates": {"gh-300": "2027-01-15"}})
        before = {p.name for p in self.folder(ka).iterdir()}
        self.assertIn("push.json", before)
        self.ok("POST", f"/api/team/members/{self.row('m-1')['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.assertTrue(self.row("m-1").get("status") == "removed" and not self.row("m-1").get("deleted_at"), "in the grace period")
        self.assertEqual({p.name for p in self.folder(ka).iterdir()}, before - {"push.json"},
                         "the endpoints go now; the rest waits for the grace period")
        self.assertNotIn(ka, n.keys_with_push())

    def test_a_membership_that_runs_out_loses_its_push_file_too(self):
        a = self.admit("m-1")
        self.turn_on(a)
        ka = self.key("m-1")
        row = self.row("m-1")
        self.team._expire_if_due(row, utcnow() + timedelta(days=400))
        self.assertEqual(self.row("m-1").get("reason"), "expired")
        self.assertFalse((self.folder(ka) / "push.json").exists())
        self.assertTrue(self.folder(ka).is_dir())

    def test_a_member_who_has_not_accepted_the_notice_gets_no_nudge(self):
        n, sender = self.nudger()
        a = self.admit("m-1")
        self.turn_on(a)
        ka = self.key("m-1")
        with mock.patch.object(self.team, "active_member", return_value=None):
            self.assertEqual(n.nudge_one(ka, utc(2026, 10, 23, 6, 5)), "not_allowed")
        self.assertIsNotNone(self.team.active_member(ka))

    def test_deleting_the_record_deletes_the_nudges(self):
        self.turn_on(OWNER)
        self.assertTrue((self.folder(OWNER_KEY) / "push.json").is_file())
        self.ok("POST", "/api/record/delete", OWNER, {"confirm": "DELETE"})
        self.assertFalse((self.folder(OWNER_KEY) / "push.json").exists())
        self.assertEqual(self.ok("GET", "/api/push", OWNER)["devices"], [])


OWNER_KEY = learner_key("tenant-1:owner-1")


if __name__ == "__main__":
    unittest.main()
