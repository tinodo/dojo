"""Opt-in Web Push nudges (ADR 0013).

At most one calm nudge a day, at the Berlin time and on the days the learner chose, and only when
something is due. Everything a learner chose lives under learners/<key>/push.json, so leaving or
deleting the record removes it. The VAPID key pair is the app's, on the data share (push/vapid.json):
never in git, a log or a response; only the public key is served. No model is ever called.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time as clock
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import HTTPException

from . import plan as planmod
from .core import Abandoned, Learner, Store, iso, parse_iso, sha256, utcnow
from .deeplink import go_link

log = logging.getLogger("dojo.push")

MAX_DEVICES = 5
MAX_FAILS = 5                       # failures in a row before a device is removed
WINDOW = timedelta(hours=2)         # a nudge may go from the chosen time until two hours later
TICK_S = 300                        # the nudger looks every five minutes
DEFAULT_TIME = "08:00"
DEFAULT_DAYS = [0, 1, 2, 3, 4]      # Monday to Friday
TEST_GAP = timedelta(minutes=1)
TTL_S = 4 * 3600                    # a nudge that cannot be delivered within four hours is not worth delivering
SLOT_WAIT_S = 30.0
SEND_TIMEOUT_S = 10.0
VAPID_WAIT_S = 2.0                  # how long to wait for another process that is writing the key file

# Only the push services of the browsers people use (SSRF guard): Chrome and Edge on Android (FCM),
# Edge on Windows (WNS), Firefox (Mozilla autopush) and Safari (Apple).
PUSH_HOSTS = frozenset({"fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com"})
PUSH_SUFFIXES = (".notify.windows.com", ".push.services.mozilla.com", ".push.apple.com")

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][05])$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_LABEL_DROP = re.compile(r"[^A-Za-z0-9 .,()/+-]")
DEVICE_ID = re.compile(r"^dev-[0-9a-f]{20}$")

TEST_MESSAGE = {"title": "Dojo", "body": "This is how a nudge from Dojo looks. Nothing is due because of it.", "url": go_link("phone")}


# ---------------------------------------------------------------- checking what a browser sends

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(text: str) -> bytes:
    if not isinstance(text, str) or not _B64URL.match(text):
        raise ValueError("not base64url")
    text = text.rstrip("=")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_endpoint(url: Any) -> str:
    """An https address on a known push service, with no user, password or odd port."""
    bad = HTTPException(400, "Dojo cannot send nudges to this browser's push service.")
    if not isinstance(url, str) or not 20 <= len(url) <= 2048 or any(c.isspace() for c in url):
        raise bad
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        raise bad
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or port not in (None, 443) or "\\" in url:
        raise bad
    if not (host in PUSH_HOSTS or host.endswith(PUSH_SUFFIXES)):
        raise bad
    return url


def check_keys(p256dh: Any, auth: Any) -> tuple[str, str]:
    """RFC 8291: a 65-byte uncompressed P-256 point and a 16-byte authentication secret."""
    try:
        point, secret = unb64url(p256dh), unb64url(auth)
    except (ValueError, TypeError):
        raise HTTPException(400, "The browser's push keys could not be read.")
    if len(point) != 65 or point[0] != 4 or len(secret) != 16:
        raise HTTPException(400, "The browser's push keys could not be read.")
    return b64url(point), b64url(secret)


def device_id(endpoint: str) -> str:
    """The same in the browser (crypto.subtle), so a page can tell which device it is on."""
    return "dev-" + sha256("dojo-push:" + endpoint)[:20]


def clean_label(label: Any) -> str:
    text = _LABEL_DROP.sub("", label if isinstance(label, str) else "").strip()
    return re.sub(r"\s+", " ", text)[:40] or "A browser"


def check_time(value: Any) -> str:
    if not isinstance(value, str) or not _HHMM.match(value):
        raise HTTPException(400, "Choose a time like 08:00, in five-minute steps.")
    return value


def check_days(value: Any) -> list[int]:
    if not isinstance(value, list) or not value or any(type(d) is not int or not 0 <= d <= 6 for d in value):
        raise HTTPException(400, "Choose at least one day. To stop nudges, turn them off instead.")
    return sorted(set(value))


# ---------------------------------------------------------------- when a nudge may go

def due_day(doc: dict, now: datetime) -> date | None:
    """The Berlin day a nudge would be for, if now is inside that day's window and none was sent for it:
    the chosen time on a chosen day, until two hours later. A window that runs over midnight still
    belongs to the day it started on. A time in the hour the clocks skip in spring is read as winter
    time (one hour later on the clock); a time in the autumn hour that happens twice is the first one."""
    m = _HHMM.match(str(doc.get("time") or ""))
    days = doc.get("days") or []
    if not m or not doc.get("devices"):
        return None
    at = time(int(m.group(1)), int(m.group(2)))
    today = planmod.to_local(now).date()
    for d in (today, today - timedelta(days=1)):
        if d.weekday() not in days or doc.get("last_day") == d.isoformat():
            continue
        start = planmod.to_utc(datetime.combine(d, at))
        if start <= now < start + WINDOW:
            return d
    return None


def as_of(day: date | None, now: datetime) -> datetime:
    """The moment to judge what is due for a nudge that belongs to `day`: now, or, in a window that ran
    over midnight, the last second of the chosen day (Berlin)."""
    if day is None or planmod.to_local(now).date() == day:
        return now
    return min(now, planmod.to_utc(datetime.combine(day, time(23, 59, 59))))


def recall_message(n: int, minutes: int) -> dict:
    noun = "recall is" if n == 1 else "recalls are"
    return {"title": "Dojo", "body": f"{n} {noun} ready, about {minutes} minutes.", "url": go_link("five")}


def plan_message(n: int, minutes: int) -> dict:
    noun = "session" if n == 1 else "sessions"
    return {"title": "Dojo", "body": f"Your plan has {n} {noun} today, about {minutes} minutes.", "url": go_link("today")}


def planned_today(plan_doc: dict | None, now: datetime) -> tuple[int, int]:
    """Sessions of today's Plan that are still planned (not done, dropped or skipped), and their minutes.
    Recall sessions are left out: the recalls are counted from the Record itself."""
    today = planmod.to_local(now).date()
    n = minutes = 0
    for s in (plan_doc or {}).get("sessions") or []:
        if not isinstance(s, dict) or s.get("state") != "planned" or s.get("kind") == "recall":
            continue
        at = planmod.shown_at(s)
        if at is None or planmod.to_local(at).date() != today:
            continue
        n += 1
        minutes += int(s.get("minutes") or 0)
    return n, minutes


# ---------------------------------------------------------------- the app's VAPID key pair

def _new_vapid() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private_pem": pem.decode("ascii"), "public": b64url(point), "created": iso()}


def _read_vapid(path: Any) -> dict | None:
    """The key file: None when there is none, {} when it is there but not (yet) a whole key."""
    try:
        doc = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        return {}
    return doc if isinstance(doc, dict) and doc.get("private_pem") and doc.get("public") else {}


def _create_vapid(path: Any, doc: dict) -> bool:
    """Create the key file exclusively (O_EXCL), so a second creator - another worker process - can never
    replace a key that devices were subscribed with, and readable by the owner only (0600) where the file
    system honours it. False when another process got there first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(doc, indent=1).encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    return True


class VapidKeys:
    """Made once, at first need, and kept on the data share like the feed tokens."""

    def __init__(self, store: Store):
        self.store = store

    def _doc(self) -> dict:
        path = self.store.path("push", "vapid.json")
        with self.store.lock:
            deadline = clock.monotonic() + VAPID_WAIT_S
            while True:
                doc = _read_vapid(path)
                if doc:
                    return doc
                if doc is None:
                    new = _new_vapid()
                    if _create_vapid(path, new):
                        return new
                    continue   # lost the race: read the winner's key
                if clock.monotonic() >= deadline:
                    # Never replaced silently: every device was subscribed with the key that is in it.
                    log.error("push/vapid.json is there but holds no key")
                    raise HTTPException(503, "Nudges are not available just now.")
                clock.sleep(0.05)   # another process is still writing it

    def public(self) -> str:
        return self._doc()["public"]

    def signer(self) -> Any:
        from py_vapid import Vapid02
        return Vapid02.from_pem(self._doc()["private_pem"].encode("ascii"))


def site_subject() -> str:
    """The VAPID contact: the site's own https address, never an e-mail address."""
    host = (os.environ.get("WEBSITE_HOSTNAME") or "").strip().lower()
    return f"https://{host}" if re.match(r"^[a-z0-9.-]+$", host) else "https://localhost"


def webpush_sender(keys: VapidKeys, session: Any = None) -> Callable[[dict, dict], int]:
    """Sends one encrypted payload with pywebpush and returns the push service's status code (0 when it
    could not be reached). Nothing about the device or the payload is logged."""
    def send(device: dict, payload: dict) -> int:
        from pywebpush import WebPushException, webpush
        info = {"endpoint": device["endpoint"], "keys": {"p256dh": device["p256dh"], "auth": device["auth"]}}
        try:
            r = webpush(info, data=json.dumps(payload), vapid_private_key=keys.signer(),
                        vapid_claims={"sub": site_subject()}, ttl=TTL_S, timeout=SEND_TIMEOUT_S,
                        headers={"Urgency": "normal", "Topic": "dojo-nudge"}, requests_session=session)
            return int(getattr(r, "status_code", 0) or 0)
        except WebPushException as e:
            return int(getattr(e.response, "status_code", 0) or 0) if e.response is not None else 0
        except Exception:  # noqa: BLE001 - a push service that cannot be reached is a failure, not a crash
            return 0
    return send


# ---------------------------------------------------------------- per learner

def _blank() -> dict:
    return {"time": DEFAULT_TIME, "days": list(DEFAULT_DAYS), "devices": [], "last_day": None, "last_at": None, "test_at": None}


def for_export(doc: dict | None) -> dict | None:
    """The learner's choices and device labels. The endpoints and keys stay out: only Dojo can use them."""
    if not doc:
        return None
    return {"time": doc.get("time"), "days": doc.get("days"), "last_day": doc.get("last_day"),
            "devices": [{"label": d.get("label"), "added": d.get("added"), "last_ok": d.get("last_ok")} for d in doc.get("devices") or []]}


class Nudges:
    """The nudge settings, the devices and the nudger thread.

    `due(learner, day)` says what is due for the nudge of that Berlin day as a message, or None. `may(key)` returns the Learner if Dojo
    may nudge them (the owner, or an active member who accepted the notice, while not paused).
    `busy(learner)` is true while a timed practice exam is open. `send(device, payload)` returns the
    push service's status code; tests replace it."""

    def __init__(self, store: Store, slots: Any, due: Callable[[Learner, date], dict | None],
                 may: Callable[[str], Learner | None], busy: Callable[[Learner], bool],
                 send: Callable[[dict, dict], int] | None = None):
        self.store = store
        self.slots = slots
        self.keys = VapidKeys(store)
        self.due = due
        self.may = may
        self.busy = busy
        self.send = send or webpush_sender(self.keys)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- the learner's own page

    def _read(self, key: str) -> dict | None:
        doc = self.store.read("learners", key, "push")
        return {**_blank(), **doc} if isinstance(doc, dict) else None

    def view(self, learner: Learner) -> dict:
        doc = self._read(learner.key) or _blank()
        return {"public_key": self.keys.public(), "time": doc["time"], "days": doc["days"], "on": bool(doc["devices"]),
                "devices": [{"id": d["id"], "label": d.get("label") or "A browser", "added": d.get("added"),
                             "last_ok": d.get("last_ok")} for d in doc["devices"]],
                "last_day": doc.get("last_day"), "max_devices": MAX_DEVICES,
                "window_minutes": int(WINDOW.total_seconds() // 60)}

    def save_settings(self, learner: Learner, at: str, days: list[int]) -> dict:
        at, days = check_time(at), check_days(days)
        with self.store.lock:
            doc = self._read(learner.key) or _blank()
            doc.update(time=at, days=days)
            self.store.write("learners", learner.key, "push", value=doc)
        return self.view(learner)

    def add_device(self, learner: Learner, endpoint: str, p256dh: str, auth: str, label: str = "") -> dict:
        endpoint = check_endpoint(endpoint)
        p256dh, auth = check_keys(p256dh, auth)
        public = self.keys.public()
        dev = {"id": device_id(endpoint), "endpoint": endpoint, "p256dh": p256dh, "auth": auth, "label": clean_label(label),
               "added": iso(), "last_ok": None, "fails": 0, "vapid": public}
        with self.store.lock:
            doc = self._read(learner.key) or _blank()
            others = [d for d in doc["devices"] if d.get("id") != dev["id"]]
            if len(others) >= MAX_DEVICES:
                raise HTTPException(409, f"Nudges are on for {MAX_DEVICES} devices already. Turn one off first.")
            doc["devices"] = others + [dev]
            self.store.write("learners", learner.key, "push", value=doc)
        return self.view(learner)

    def remove_device(self, learner: Learner, dev_id: str) -> dict:
        with self.store.lock:
            doc = self._read(learner.key)
            if doc is not None and any(d.get("id") == dev_id for d in doc["devices"]):
                doc["devices"] = [d for d in doc["devices"] if d.get("id") != dev_id]
                self.store.write("learners", learner.key, "push", value=doc)
        return self.view(learner)

    def off(self, learner: Learner) -> dict:
        """Off everywhere: the whole file goes, choices included."""
        self.store.delete("learners", learner.key, "push")
        return self.view(learner)

    def test(self, learner: Learner, dev_id: str) -> dict:
        now = utcnow()
        with self.store.lock:
            doc = self._read(learner.key)
            dev = next((d for d in (doc or {}).get("devices") or [] if d.get("id") == dev_id), None)
            if dev is None:
                raise HTTPException(404, "Nudges are not on for this device.")
            if doc.get("test_at") and now - parse_iso(doc["test_at"]) < TEST_GAP:
                raise HTTPException(429, "A test nudge was sent a moment ago. Wait a minute before the next one.")
            doc["test_at"] = iso(now)
            self.store.write("learners", learner.key, "push", value=doc)
        outcome = self._deliver(dev, TEST_MESSAGE)
        self._record(learner.key, {dev_id: outcome})
        return {"sent": outcome == "ok", "removed": outcome == "gone", **self.view(learner)}

    # ---- sending

    def _deliver(self, dev: dict, payload: dict) -> str:
        if dev.get("vapid") != self.keys.public():
            return "gone"   # subscribed with a key Dojo no longer has: the browser must subscribe again
        status = self.send(dev, payload)
        if 200 <= status < 300:
            return "ok"
        if status in (404, 410):
            return "gone"
        return "failed"

    def _record(self, key: str, outcomes: dict[str, str]) -> None:
        """Read again under the lock: the learner may have changed something while the nudge was on its way."""
        with self.store.lock:
            doc = self._read(key)
            if doc is None:
                return   # turned off everywhere, or the record was deleted, while it was sent
            kept = []
            for d in doc["devices"]:
                out = outcomes.get(d.get("id"))
                if out == "gone":
                    continue
                if out == "ok":
                    d["last_ok"], d["fails"] = iso(), 0
                elif out == "failed":
                    d["fails"] = int(d.get("fails") or 0) + 1
                    if d["fails"] >= MAX_FAILS:
                        continue
                kept.append(d)
            doc["devices"] = kept
            self.store.write("learners", key, "push", value=doc)

    def keys_with_push(self) -> list[str]:
        root = self.store.path("learners")
        with self.store.lock:
            if not root.is_dir():
                return []
            return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "push.json").is_file())

    def nudge_one(self, key: str, now: datetime) -> str:
        """One learner, one look. Returns what happened, for tests and the tick's count.

        At most once: the day is reserved in push.json before the first message goes out, so a restart
        between a push service accepting it and the outcomes being written can never send it again. A send
        that fails loses that day's nudge, which is the accepted price."""
        doc = self._read(key)
        if doc is None:
            return "off"
        day = due_day(doc, now)
        if day is None:
            return "not_now"
        learner = self.may(key)
        if learner is None:
            return "not_allowed"
        if self.busy(learner):
            return "exam"
        if self.due(learner, day) is None:
            return "nothing_due"
        if not self.slots.acquire(timeout=SLOT_WAIT_S):
            return "no_slot"   # the window leaves room: the next tick tries again
        try:
            with self.store.writing_as(key):
                # Waiting for the slot took time: look again at everything right before sending.
                learner = self.may(key)
                if learner is None:
                    return "not_allowed"
                if self.busy(learner):
                    return "exam"
                message = self.due(learner, day)
                if message is None:
                    return "nothing_due"
                with self.store.lock:
                    doc = self._read(key)
                    if doc is None:
                        return "off"
                    if due_day(doc, now) != day:
                        return "not_now"   # the settings changed, or a parallel look already sent it
                    doc["last_day"], doc["last_at"] = day.isoformat(), iso()
                    self.store.write("learners", key, "push", value=doc)
                outcomes: dict[str, str] = {}
                stopped = None
                for d in doc["devices"]:
                    stopped = self._stop_sending(key)
                    if stopped:
                        break   # the day stays reserved: at most once, never a nudge into an exam
                    outcomes[d["id"]] = self._deliver(d, message)
                self._record(key, outcomes)
        finally:
            self.slots.release()
        if "ok" in outcomes.values():
            return "sent"
        return stopped or "failed"

    def _stop_sending(self, key: str) -> str | None:
        """Right before each device: an exam opened, a pause or a removal stops the rest. The app-wide
        store lock is not held across a push POST (up to SEND_TIMEOUT_S each), so the window left is one POST."""
        learner = self.may(key)
        if learner is None:
            return "not_allowed"
        if self.busy(learner):
            return "exam"
        return None

    def tick(self, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        counts: dict[str, int] = {}
        for key in self.keys_with_push():
            try:
                what = self.nudge_one(key, now)
            except Abandoned:
                what = "deleted"
            except Exception:  # noqa: BLE001 - one learner's problem must not stop the others' nudges
                log.exception("a nudge could not be prepared")
                what = "error"
            counts[what] = counts.get(what, 0) + 1
        if counts.get("sent") or counts.get("failed"):
            log.info("nudges: %d sent, %d failed", counts.get("sent", 0), counts.get("failed", 0))
        return counts

    # ---- the thread

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="push-nudger", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("the nudger tick failed")

    def stop(self) -> None:
        self._stop.set()
