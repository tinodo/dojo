"""Play, the social layer (ADR 0010 sections 4, 7 and 8; PR 2): circles by invitation, the banded bar, the
witness room (one moment or one pass shared with one circle) and pass celebrations, and the privacy rules
P-1 to P-13 that apply to them.

Every test runs the app as it runs in Azure with team mode on (local=False, X-MS-CLIENT-PRINCIPAL) on its
own data folder, with one frozen clock for every module that reads the time."""
import json
import logging
import os
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

from fastapi.routing import APIRoute  # noqa: E402

from app import play_social as ps  # noqa: E402
from app.core import new_id  # noqa: E402
from app.play import DAY_CAP, NOT_A_CERTIFICATE, week_before_today  # noqa: E402
from app.team import _expiry as team_expiry  # noqa: E402
from tests.test_team import OWNER, OWNER_KEY, TeamCase, classes, make, sample_path, who  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)   # a Monday, 10:00 in Berlin
SOCIAL_ROUTES = [
    ("GET", "/api/play/social"), ("POST", "/api/play/social/join"), ("PUT", "/api/play/social"),
    ("POST", "/api/play/social/leave"), ("POST", "/api/play/circles"),
    ("POST", "/api/play/circles/circle-0123456789abcdef0123/invite"),
    ("POST", "/api/play/circles/circle-0123456789abcdef0123/leave"),
    ("PUT", "/api/play/circles/circle-0123456789abcdef0123/goal"),
    ("POST", "/api/play/circles/circle-0123456789abcdef0123/preview"),
    ("POST", "/api/play/circles/circle-0123456789abcdef0123/share"),
    ("DELETE", "/api/play/shares/share-0123456789abcdef0123"),
    ("POST", "/api/play/invitations/inv-0123456789abcdef0123/accept"),
    ("POST", "/api/play/invitations/inv-0123456789abcdef0123/decline"),
]


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def to(self, when: datetime) -> None:
        self.now = when

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class SocialCase(TeamCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock(START)
        for target in ("app.core.utcnow", "app.play.utcnow", "app.play_social.utcnow", "app.team.utcnow"):
            p = mock.patch(target, self.clock)
            p.start()
            self.addCleanup(p.stop)
        self.people: dict[str, dict] = {}

    @property
    def social(self):
        return self.app.state.social

    # ---- people

    def member(self, oid: str, name: str, play: bool = True, opt: bool = True, invitable: bool = True) -> dict:
        h = self.admit(oid, name=f"Entra {oid}", email=f"{oid}@corp.example")
        self.ok("PUT", "/api/me/display-name", h, {"name": name})
        if play:
            self.ok("POST", "/api/play/join", h, {})
        if opt:
            self.opt(h, invitable)
        self.people[name] = h
        return h

    def opt(self, h: dict, invitable: bool = True) -> dict:
        return self.ok("POST", "/api/play/social/join", h, {"version": ps.NOTICE_VERSION, "invitable": invitable})

    def switch(self, on: bool = True) -> dict:
        return self.ok("PUT", "/api/team/play", OWNER, {"on": on})

    def view(self, h: dict) -> dict:
        return self.ok("GET", "/api/play/social", h)

    def pid(self, h: dict) -> str:
        return self.view(h)["me"]["id"]

    def circle(self, h: dict, name: str = "Morning circle") -> dict:
        return next(c for c in self.view(h)["circles"] if c["name"] == name)

    def start_circle(self, creator: dict, others: list[dict], name: str = "Morning circle") -> str:
        self.ok("POST", "/api/play/circles", creator, {"name": name, "people": [self.pid(h) for h in others]})
        for h in others:
            self.accept(h, name)
        return self.circle(creator, name)["id"]

    def accept(self, h: dict, name: str = "Morning circle") -> None:
        inv = next(i for i in self.view(h)["invitations"] if i["circle"] == name)
        self.ok("POST", f"/api/play/invitations/{inv['id']}/accept", h)

    def crowd(self, n: int) -> list[dict]:
        self.switch(True)
        return [self.member(f"m-{i}", f"Learner {chr(65 + i)}") for i in range(n)]

    # ---- the Record

    def at(self, when: datetime, fn) -> None:
        keep = self.clock.now
        self.clock.to(when)
        try:
            fn()
        finally:
            self.clock.to(keep)

    def study(self, h: dict, when: datetime, answers: int = 10, lessons: int = 0, labs: int = 0) -> None:
        """Open answers (3 each, 30 a day at most), lessons and labs, the first one making a study day."""
        key = self.key_of(h)
        store = self.store

        def write():
            t = self.clock.now
            for i in range(answers):
                item = new_id("it")
                base = {"package": "gh-300", "skill": "s1"}
                self.clock.to(t + timedelta(minutes=i, seconds=0))
                store.append_event(key, "item.issued", {"item": item, "mode": "probe", "kind": "x", "changed_condition": False, **base})
                self.clock.to(t + timedelta(minutes=i, seconds=30))
                store.append_event(key, "answer.submitted", {"item": item, "elapsed_s": 30, "input": "typed", **base})
                store.append_event(key, "answer.judged", {"item": item, "met": 1, "total": 3, "mode": "probe", **base})
            for i in range(lessons):
                self.clock.to(t + timedelta(hours=1, minutes=i))
                store.append_event(key, "lesson.viewed", {"package": "gh-300", "skill": f"s{i}", "lesson": f"le-{i}", "modality": "read"})
            for i in range(labs):
                self.clock.to(t + timedelta(hours=2, minutes=i))
                store.append_event(key, "lab.checked", {"lab": f"lab-{i}", "runs": 1, "passed": True, "package": "gh-300", "skill": "s1"})
        self.at(when, write)

    def key_of(self, h: dict) -> str:
        return self.row(h["X-MS-CLIENT-PRINCIPAL-ID"])["key"]

    def report_pass(self, h: dict, earned: str = "2026-10-01") -> dict:
        return self.ok("POST", "/api/passes", h, {"package": "gh-300", "earned": earned})

    def moment(self, h: dict, kind: str) -> dict:
        return next(m for m in self.ok("GET", "/api/play", h)["moments"] if m["kind"] == kind)

    def share(self, h: dict, cid: str, kind: str = "passed") -> dict:
        m = self.moment(h, kind)
        preview = self.ok("POST", f"/api/play/circles/{cid}/preview", h, {"moment": m["id"]})
        self.ok("POST", f"/api/play/circles/{cid}/share", h, {"moment": m["id"]})
        return preview


# ---------------------------------------------------------------- P-1: off unless team mode and the switch

class SwitchTests(SocialCase):
    def test_off_by_default_and_404_everywhere(self):
        h = self.member("m-1", "Ada", opt=False)
        self.assertEqual(self.ok("GET", "/api/team/play", OWNER), {"on": False, "team": True})
        for method, path in SOCIAL_ROUTES:
            for who_ in (h, OWNER):
                r = self.call(method, path, who_, {"version": 1, "invitable": True, "people": [], "name": "x"})
                self.assertEqual(r.status_code, 404, f"{method} {path}: {r.text}")
        self.assertFalse((self.folder(self.key_of(h)) / "social" / "play.json").read_text("utf-8").count('"social"'))

    def test_team_mode_off_gives_404_on_every_social_route(self):
        app, c, tmp = make(team=False)
        app.state.dojo.store.write("team", "play", value={"on": True, "round": 1})
        for method, path in SOCIAL_ROUTES:
            r = c.request(method, path, headers={**OWNER, "X-Dojo": "1"}, json={})
            self.assertEqual(r.status_code, 404, f"{method} {path}: {r.text}")
        self.assertEqual(c.request("PUT", "/api/team/play", headers={**OWNER, "X-Dojo": "1"}, json={"on": True}).status_code, 409)

    def test_a_malformed_body_gets_404_while_off_before_validation(self):
        h = self.member("m-1", "Ada", opt=False)
        bodies = (b"{", b"[1, 2", b'{"version": "x", "people": 7, "name": [], "points": "many", "moment": 5}')
        for method, path in SOCIAL_ROUTES:
            if method == "GET":
                continue
            for raw in bodies:
                r = self.c.request(method, path, headers={**h, "X-Dojo": "1", "Content-Type": "application/json"}, content=raw)
                self.assertEqual(r.status_code, 404, f"{method} {path} {raw!r}: {r.text}")
        # Switched on, the same malformed body reaches validation: the 404 above came from the gate.
        self.switch(True)
        r = self.c.request("POST", "/api/play/circles", headers={**h, "X-Dojo": "1", "Content-Type": "application/json"}, content=b"{")
        self.assertEqual(r.status_code, 422, r.text)

    def test_the_class_refusal_never_depends_on_the_body(self):
        self.member("m-9", "Ada", opt=False)
        pending = self.admit("m-1", ack=False)
        stranger = who("stranger")
        for on in (False, True):
            self.switch(on)
            for h, want in ((stranger, "not_member"), (pending, "notice")):
                for method, path in SOCIAL_ROUTES:
                    if method == "GET":
                        r = self.call(method, path, h)
                        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, want), f"{method} {path}")
                        continue
                    for raw in (b"{", b'{"people": 7}', b"{}"):
                        r = self.c.request(method, path, headers={**h, "X-Dojo": "1", "Content-Type": "application/json"}, content=raw)
                        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, want), f"on={on} {method} {path} {raw!r}")

    def test_only_the_admin_role_switches(self):
        h = self.member("m-1", "Ada", opt=False)
        self.refused("PUT", "/api/team/play", h, "admin_only", body={"on": True})
        self.refused("GET", "/api/team/play", h, "admin_only")

    def test_switching_off_deletes_every_circle_and_asks_for_a_fresh_opt_in(self):
        a, b, c = self.crowd(3)
        cid = self.start_circle(a, [b, c])
        self.switch(False)
        self.assertEqual(self.store.names("play", "circles"), [])
        self.switch(True)
        self.assertFalse(self.view(a)["opted"])
        self.ok("POST", f"/api/play/circles/{cid}/leave", a, status=409)

    def test_opt_in_needs_play_and_the_current_notice(self):
        self.switch(True)
        h = self.member("m-1", "Ada", play=False, opt=False)
        self.assertFalse(self.view(h)["play"])
        self.ok("POST", "/api/play/social/join", h, {"version": ps.NOTICE_VERSION}, status=409)
        self.ok("POST", "/api/play/join", h, {})
        self.ok("POST", "/api/play/social/join", h, {"version": ps.NOTICE_VERSION - 1}, status=409)
        v = self.opt(h, invitable=False)
        self.assertTrue(v["opted"])
        self.assertFalse(v["invitable"])   # "Others can invite me" is off unless the member turns it on


# ---------------------------------------------------------------- P-12: the notice

class NoticeTests(SocialCase):
    def test_two_parts_and_the_residual_risk(self):
        self.switch(True)
        h = self.member("m-1", "Ada", opt=False)
        n = self.view(h)["notice"]
        self.assertEqual(n["version"], ps.NOTICE_VERSION)
        inside = " ".join(p["text"] for p in n["in_app"])
        outside = " ".join(p["text"] for p in n["outside"])
        self.assertIn("if every other member of your circle pools their own numbers, they can learn that you "
                      "studied substantially in a period", inside)
        self.assertIn("effort only, never results", inside.lower())
        self.assertIn('"runs this Dojo"', inside)
        self.assertIn("No page in Dojo shows anyone's Play data to the owner's admin role", inside)
        self.assertIn("Global Administrator", outside)
        self.assertIn("can technically read everything", outside)
        self.assertIn("never used for performance reviews, staffing or HR", outside)
        for text in (inside, outside):
            low = text.lower()
            self.assertNotIn("the owner sees nothing", low)
            self.assertNotIn("cannot reveal", low)
            self.assertNotIn("nobody can tell", low)

    def test_privacy_doc_has_the_same_words(self):
        doc = Path(ps.__file__).resolve().parent.parent / "docs" / "privacy.md"
        text = " ".join(doc.read_text(encoding="utf-8").split())
        for part in ps.notice("Alex")["in_app"][2:]:
            if "{operator}" in part["text"] or part["title"] == "The person who runs this Dojo":
                continue
            self.assertIn(" ".join(part["text"].split()), text, part["title"])
        self.assertIn(" ".join(ps.notice("Alex")["outside"][0]["text"].split()), text)


# ---------------------------------------------------------------- circles and invitations

class CircleTests(SocialCase):
    def test_size_three_to_twelve_and_three_circles_each(self):
        people = self.crowd(13)
        a = people[0]
        pids = [self.pid(h) for h in people[1:]]
        self.ok("POST", "/api/play/circles", a, {"name": "Too small", "people": pids[:1]}, status=400)
        self.ok("POST", "/api/play/circles", a, {"name": "Too big", "people": pids[:12]}, status=400)
        self.ok("POST", "/api/play/circles", a, {"name": "  ", "people": pids[:2]}, status=400)
        for i in range(3):
            self.ok("POST", "/api/play/circles", a, {"name": f"C{i}", "people": pids[:2]})
        self.ok("POST", "/api/play/circles", a, {"name": "C4", "people": pids[:2]}, status=409)
        self.assertFalse(self.view(a)["can_start"])

    def test_only_invitable_people_can_be_invited(self):
        self.switch(True)
        a = self.member("m-1", "Ada")
        b = self.member("m-2", "Bo", invitable=False)
        c = self.member("m-3", "Cy")
        d = self.member("m-4", "Di")
        names = [p["name"] for p in self.view(a)["people"]]
        self.assertNotIn("Bo", names)
        self.assertEqual(sorted(names), ["Cy", "Di"])   # the owner has not opted in
        self.ok("PUT", "/api/play/social", b, {"invitable": True})
        bpid = self.pid(b)
        self.ok("PUT", "/api/play/social", b, {"invitable": False})
        self.ok("POST", "/api/play/circles", a, {"name": "X", "people": [bpid, self.pid(c)]}, status=400)
        self.ok("POST", "/api/play/circles", a, {"name": "X", "people": [self.pid(c), self.pid(d)]})

    def test_the_inviter_never_sees_pending_declined_or_lapsed(self):
        a, b, c, d, e = self.crowd(5)
        self.ok("POST", "/api/play/circles", a, {"name": "Morning circle",
                                                 "people": [self.pid(h) for h in (b, c, d, e)]})
        alone = self.circle(a)
        self.assertEqual([m["name"] for m in alone["members"]], ["Learner A"])
        self.assertNotIn("invites", alone)
        inv_b = self.view(b)["invitations"][0]
        self.assertEqual(inv_b["members"], ["Learner A"])
        self.accept(c)
        self.accept(e)
        pending = self.circle(a)   # b and d still invited
        self.assertEqual([m["name"] for m in pending["members"]], ["Learner A", "Learner C", "Learner E"])
        self.ok("POST", f"/api/play/invitations/{inv_b['id']}/decline", b)
        self.assertEqual(self.circle(a), pending)
        self.clock.advance(days=7)
        self.assertEqual(self.view(d)["invitations"], [])   # lapsed
        self.assertEqual(self.circle(a), pending)
        # Declined is recorded nowhere: inviting again is allowed, as after a lapse.
        self.ok("POST", f"/api/play/circles/{pending['id']}/invite", a, {"people": [self.pid(b)]})
        self.assertEqual(len(self.view(b)["invitations"]), 1)
        self.assertEqual(self.circle(a), pending)
        doc = json.dumps(self.store.read("play", "circles", pending["id"]))
        self.assertNotIn("declin", doc)
        self.assertNotIn('"by"', doc)

    def test_a_circle_nobody_joined_goes_when_its_invitations_lapse(self):
        a, b, c = self.crowd(3)
        self.ok("POST", "/api/play/circles", a, {"name": "Morning circle", "people": [self.pid(b), self.pid(c)]})
        self.clock.advance(days=6, hours=23)
        self.assertEqual(len(self.view(a)["circles"]), 1)
        self.clock.advance(hours=1)
        self.assertEqual(self.view(a)["circles"], [])

    def test_a_circle_left_under_three_goes_after_a_week(self):
        a, b, c = self.crowd(3)
        cid = self.start_circle(a, [b, c])
        self.ok("POST", f"/api/play/circles/{cid}/leave", c)
        self.assertEqual(self.circle(a)["size"], 2)
        self.clock.advance(days=7, minutes=1)
        self.assertEqual(self.view(a)["circles"], [])
        self.assertEqual(self.store.names("play", "circles"), [])

    def test_renewed_invitations_do_not_keep_a_small_circle(self):
        a, b, c, d, e = self.crowd(5)
        self.ok("POST", "/api/play/circles", a, {"name": "Morning circle", "people": [self.pid(b), self.pid(c)]})
        cid = self.store.names("play", "circles")[0]
        self.clock.advance(days=6)
        self.ok("POST", f"/api/play/circles/{cid}/invite", a, {"people": [self.pid(d), self.pid(e)]})
        self.clock.advance(days=1, minutes=1)   # 7 days after the start; d and e are still invited
        self.assertEqual(self.view(a)["circles"], [])
        self.assertEqual(self.store.names("play", "circles"), [])
        self.assertEqual(self.view(d)["invitations"], [])

    def _between_check_and_lock(self, then):
        """Run `then` the way a concurrent request would: after create's first check, before its locked step."""
        real = self.social._no_owner

        def meanwhile(learner):
            real(learner)
            then(learner)
        return mock.patch.object(self.social, "_no_owner", meanwhile)

    def test_a_switch_off_while_create_waits_wins(self):
        a, b, c = self.crowd(3)
        people = [self.pid(b), self.pid(c)]
        with self._between_check_and_lock(lambda _l: self.social.set_switch(False)):
            r = self.call("POST", "/api/play/circles", a, {"name": "Late", "people": people})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.store.names("play", "circles"), [])

    def test_leaving_circles_while_create_waits_wins(self):
        a, b, c = self.crowd(3)
        people = [self.pid(b), self.pid(c)]
        with self._between_check_and_lock(self.social.leave_social):
            r = self.call("POST", "/api/play/circles", a, {"name": "Late", "people": people})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.store.names("play", "circles"), [])

    def test_the_week_starts_when_forget_drops_a_member(self):
        a, b, c = self.crowd(3)
        cid = self.start_circle(a, [b, c])
        self.assertIsNone(self.store.read("play", "circles", cid)["short_since"])
        self.clock.advance(hours=2)
        self.ok("POST", "/api/play/leave", c)   # forget(), not a read: the clock must start here
        self.assertEqual(self.store.read("play", "circles", cid)["short_since"], ps.iso(self.clock.now))
        self.clock.advance(days=7, minutes=1)
        self.social.sweep()
        self.assertEqual(self.store.names("play", "circles"), [])

    def test_another_members_circle_is_not_found(self):
        a, b, c, d = self.crowd(4)
        cid = self.start_circle(a, [b, c])
        for method, path, body in (("POST", f"/api/play/circles/{cid}/invite", {"people": [self.pid(a)]}),
                                   ("POST", f"/api/play/circles/{cid}/leave", {}),
                                   ("PUT", f"/api/play/circles/{cid}/goal", {"points": 300}),
                                   ("POST", f"/api/play/circles/{cid}/preview", {"moment": "mo-0123456789abcdef0123"})):
            self.ok(method, path, d, body, status=404)


# ---------------------------------------------------------------- the owner in Play (section 0)

class OwnerTests(SocialCase):
    def owner_in(self):
        a, b, c, d = self.crowd(4)
        self.ok("POST", "/api/play/join", OWNER, {})
        self.opt(OWNER)
        return a, b, c, d

    def test_the_owner_can_change_others_can_invite_me(self):
        a, b, c, d = self.owner_in()
        self.assertTrue(self.view(OWNER)["invitable"])
        self.assertTrue(any(p["label"] for p in self.view(a)["people"]))
        self.assertFalse(self.ok("PUT", "/api/play/social", OWNER, {"invitable": False})["invitable"])
        self.assertFalse(any(p["label"] for p in self.view(a)["people"]))
        self.assertTrue(self.ok("PUT", "/api/play/social", OWNER, {"invitable": True})["invitable"])
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text("utf-8")
        start = script.index("function circleSettings(")
        body = script[start:script.index("\n}\n", start)]
        self.assertIn('for: "pl-invitable-on"', body)
        self.assertNotIn('soc.owner ? null : h("label", { class: "pl-check", for: "pl-invitable-on"', body)

    def test_the_owner_never_starts_or_invites(self):
        a, b, c, d = self.owner_in()
        self.assertFalse(self.view(OWNER)["can_start"])
        self.ok("POST", "/api/play/circles", OWNER, {"name": "Mine", "people": [self.pid(a), self.pid(b)]}, status=403)
        cid = self.start_circle(a, [b, c])
        self.ok("POST", f"/api/play/circles/{cid}/invite", a, {"people": [self.pid(OWNER)]})
        self.accept(OWNER)
        self.ok("POST", f"/api/play/circles/{cid}/invite", OWNER, {"people": [self.pid(d)]}, status=403)
        self.assertFalse(self.circle(OWNER)["can_invite"])

    def test_labelled_everywhere_and_sees_what_a_member_sees(self):
        a, b, c, d = self.owner_in()
        owner_entry = next(p for p in self.view(a)["people"] if p["label"])
        self.assertEqual(owner_entry["label"], "runs this Dojo")
        cid = self.start_circle(a, [b, c])
        self.ok("POST", f"/api/play/circles/{cid}/invite", a, {"people": [owner_entry["id"]]})
        inv = self.view(OWNER)["invitations"][0]
        self.accept(OWNER)
        self.report_pass(b)
        self.share(b, cid)
        seen_by_member = self.circle(c)
        seen_by_owner = self.circle(OWNER)

        def common(v):
            v = json.loads(json.dumps(v))
            v.pop("can_invite")
            for m in v["members"]:
                m.pop("you")
            for s in v["shared"]:
                s.pop("mine"), s.pop("moment")
            return v
        self.assertEqual(common(seen_by_owner), common(seen_by_member))
        labelled = [m for m in seen_by_member["members"] if m["label"]]
        self.assertEqual([m["label"] for m in labelled], ["runs this Dojo"])
        self.assertIn("Learner A", inv["members"])
        # The owner's Entra name never reaches a peer.
        self.assertNotIn("Alex", json.dumps(self.view(c)))

    def test_admin_role_gets_nothing_of_a_member(self):
        """Table-driven over the route inventory: no admin route and no GET the owner can make shows anything
        of member B's Play data while the owner is not in B's circle (P-6)."""
        a, b, c, d = self.owner_in()
        cid = self.start_circle(a, [b, c], name="Secret garden")
        self.report_pass(b)
        self.share(b, cid)
        share_text = self.circle(b, "Secret garden")["shared"][0]["text"]
        secrets = [cid, "Secret garden", share_text, self.moment(b, "passed")["id"],
                   self.circle(b, "Secret garden")["shared"][0]["id"]]   # B's display name and person id are in the people list by B's choice
        checked = 0
        for r in self.app.routes:
            if not isinstance(r, APIRoute) or "GET" not in r.methods:
                continue
            if classes(r) not in ({"admin"}, {"learner"}, {"learner_exit"}):
                continue
            resp = self.call("GET", sample_path(r.path), OWNER)
            body = resp.text
            for s in secrets:
                self.assertNotIn(s, body, f"GET {r.path}")
            checked += 1
        self.assertGreater(checked, 40)
        sw = self.ok("GET", "/api/team/play", OWNER)
        self.assertEqual(set(sw), {"on", "team"})
        members = json.dumps(self.ok("GET", "/api/team/members", OWNER))
        self.assertNotIn("circle", members.lower())


# ---------------------------------------------------------------- the bar (section 4, P-7)

class BarTests(SocialCase):
    def five(self, extra: int = 0) -> tuple[list[dict], str]:
        people = self.crowd(5 + extra)
        cid = self.start_circle(people[0], people[1:5])
        return people, cid

    def test_the_bar_cache_is_only_touched_under_its_lock(self):
        social = self.social
        test = self

        class Guarded(dict):
            def _held(self):
                test.assertTrue(social._bars_lock.locked(), "the bar cache was touched without its lock")

            def get(self, *a):
                self._held()
                return super().get(*a)

            def setdefault(self, *a):
                self._held()
                return super().setdefault(*a)

            def __setitem__(self, *a):
                self._held()
                return super().__setitem__(*a)

            def __iter__(self):
                self._held()
                return super().__iter__()

            def pop(self, *a):
                self._held()
                return super().pop(*a)

            def clear(self):
                self._held()
                return super().clear()

        people, cid = self.five()
        social._bars = Guarded()
        self.clock.to(START + timedelta(days=7))
        for _ in range(3):   # a new day each time: the old bands are evicted
            self.assertEqual(self.circle(people[0])["bar"]["state"], "shown")
            self.clock.advance(days=1)
        self.assertEqual(len(social._bars), 1)
        self.switch(False)
        self.assertEqual(len(social._bars), 0)

    def test_no_bar_and_no_goal_met_under_five(self):
        a, b, c, d = self.crowd(4)
        cid = self.start_circle(a, [b, c])
        for size in (3, 4):
            if size == 4:
                self.ok("POST", f"/api/play/circles/{cid}/invite", a, {"people": [self.pid(d)]})
                self.accept(d)
            for h in (a, b, c):
                self.study(h, self.clock.now - timedelta(hours=1), answers=10, lessons=5, labs=5)
            for days in range(8):
                v = self.circle(a)
                self.assertEqual(v["size"], size)
                self.assertEqual(v["bar"], {"state": "small", "text": "The bar appears when the circle has 5 members."})
                self.assertNotIn("met", json.dumps(v).lower().replace("members", ""))
                self.clock.advance(days=1)

    def test_bar_with_five_is_present_every_day_and_only_a_band(self):
        people, cid = self.five()
        self.assertEqual(self.circle(people[0])["bar"]["state"], "soon")   # members count from next Monday
        self.clock.to(START + timedelta(days=7))
        for day in range(7):
            v = self.circle(people[0])
            self.assertEqual(v["bar"]["state"], "shown", day)
            self.assertEqual(set(v["bar"]), {"state", "band", "goal", "met", "text"})
            self.assertEqual(v["bar"]["band"], 0)
            self.assertEqual(v["bar"]["goal"], 500)
            self.clock.advance(days=1)

    def test_bands_from_days_before_today_at_most_once_a_day(self):
        people, cid = self.five()
        monday = START + timedelta(days=7)
        self.clock.to(monday)
        for h in people:
            self.study(h, monday - timedelta(hours=1), answers=10)   # 40 points each, Monday
        self.assertEqual(self.circle(people[0])["bar"]["band"], 0)   # today's study does not count yet
        self.clock.to(monday + timedelta(days=1))
        self.assertEqual(self.circle(people[0])["bar"]["band"], 25)   # 200 of 500
        for h in people:
            self.study(h, self.clock.now - timedelta(minutes=30), answers=10, lessons=5, labs=5)
        self.assertEqual(self.circle(people[0])["bar"]["band"], 25)   # unchanged until tomorrow
        self.clock.to(monday + timedelta(days=2))
        bar = self.circle(people[0])["bar"]
        self.assertEqual(bar["band"], 100)
        self.assertTrue(bar["met"])
        self.assertEqual(bar["text"], "Your circle met this week's goal.")
        self.assertNotIn("points", json.dumps(bar))

    def test_one_day_of_one_member_never_moves_five_off_zero(self):
        people, cid = self.five()
        monday = START + timedelta(days=7)
        self.clock.to(monday)
        self.study(people[2], monday + timedelta(minutes=5), answers=40, lessons=20, labs=20)
        self.clock.to(monday + timedelta(days=1))
        events = self.store.events(self.key_of(people[2]))
        doc = self.store.read("learners", self.key_of(people[2]), "social", "play")
        self.assertEqual(week_before_today(events, doc, date(2026, 10, 13)), DAY_CAP)
        self.assertEqual(self.circle(people[0])["bar"]["band"], 0)

    def test_goal_floor_fixed_for_the_week_and_a_raise_applies_next_week(self):
        people, cid = self.five()
        a = people[0]
        self.clock.to(START + timedelta(days=7, hours=1))
        self.ok("PUT", f"/api/play/circles/{cid}/goal", a, {"points": 499}, status=400)
        self.ok("PUT", f"/api/play/circles/{cid}/goal", a, {"points": 2001}, status=400)
        self.ok("PUT", f"/api/play/circles/{cid}/goal", people[3], {"points": 700})
        v = self.circle(a)
        self.assertEqual((v["bar"]["goal"], v["next_goal"]), (500, 700))
        self.assertNotIn("Learner D", json.dumps({k: v[k] for k in ("bar", "next_goal", "goal_range")}))
        self.clock.to(START + timedelta(days=14))
        self.assertEqual(self.circle(a)["bar"]["goal"], 700)

    def test_a_join_counts_from_monday_and_a_leave_hides_the_bar_for_the_week(self):
        people, cid = self.five(extra=1)
        a, f = people[0], people[5]
        self.clock.to(START + timedelta(days=9))   # Wednesday of week two
        self.ok("POST", f"/api/play/circles/{cid}/invite", a, {"people": [self.pid(f)]})
        self.accept(f)
        v = self.circle(a)
        self.assertEqual((v["size"], v["bar"]["state"], v["bar"]["goal"]), (6, "shown", 500))
        self.clock.to(START + timedelta(days=14))
        self.assertEqual(self.circle(a)["bar"]["goal"], 600)
        self.clock.advance(days=2)
        self.ok("POST", f"/api/play/circles/{cid}/leave", people[1])
        for _ in range(5):
            self.assertEqual(self.circle(a)["bar"]["state"], "paused")
            self.clock.advance(days=1)
        self.assertEqual(self.circle(a)["bar"]["state"], "shown")   # the next Monday
        self.assertEqual(self.circle(a)["bar"]["goal"], 500)

    def test_leaving_play_hides_the_bar_too(self):
        people, cid = self.five(extra=1)
        self.ok("POST", f"/api/play/circles/{cid}/invite", people[0], {"people": [self.pid(people[5])]})
        self.accept(people[5])
        self.clock.to(START + timedelta(days=8))
        self.assertEqual(self.circle(people[0])["bar"]["state"], "shown")
        self.ok("POST", "/api/play/leave", people[5])
        self.assertEqual(self.circle(people[0])["bar"]["state"], "paused")


# ---------------------------------------------------------------- the witness room and passes

class ShareTests(SocialCase):
    def three(self):
        a, b, c = self.crowd(3)
        return a, b, c, self.start_circle(a, [b, c])

    def test_the_preview_is_what_the_circle_sees_and_a_pass_has_no_date_or_score(self):
        a, b, c, cid = self.three()
        self.report_pass(b)
        preview = self.share(b, cid)
        self.assertEqual(preview["text"], "Learner B reports passing GH-300.")
        self.assertIsNone(preview["line"])
        seen = self.circle(c)["shared"]
        self.assertEqual([(s["text"], s["line"]) for s in seen], [(preview["text"], preview["line"])])
        self.assertIsNone(seen[0]["moment"])
        self.assertFalse(seen[0]["mine"])
        self.assertNotIn("2026-10-01", json.dumps(self.view(c)))
        self.assertNotIn("1 Oct", json.dumps(self.view(c)))

    def test_a_listed_pass_reads_the_same(self):
        s = {"kind": "passed", "exam": "GH-300", "at": "2026-10-01"}
        self.assertEqual(ps.share_text("Ada", s), ps.share_text("Ada", {**s, "source": "learn_transcript"}))
        self.assertEqual(ps.share_text("Ada", s)[0], "Ada reports passing GH-300.")

    def test_a_moment_says_not_a_certification(self):
        for kind in ("later", "changed", "relearned", "domain", "exam"):
            text, line = ps.share_text("Ada", {"kind": kind, "exam": "GH-300", "subject": "Use Copilot",
                                               "at": "2026-10-02T09:00:00+00:00"})
            self.assertEqual(line, NOT_A_CERTIFICATE, kind)
            self.assertIn("2 Oct", text)
            self.assertTrue(text.startswith("Ada "))

    def test_a_shared_moment_from_the_record(self):
        a, b, c, cid = self.three()
        skills = self.app.state.dojo.packages.by_id["gh-300"]["skills"]
        sid = next(k for k, v in skills.items() if v.get("kind") == "scenario")
        key = self.key_of(b)

        def write():
            item = new_id("it")
            base = {"package": "gh-300", "skill": sid}
            self.store.append_event(key, "item.issued", {"item": item, "mode": "probe", "kind": "x", "changed_condition": True, **base})
            self.clock.advance(seconds=40)
            self.store.append_event(key, "answer.submitted", {"item": item, "elapsed_s": 40, "input": "typed", **base})
            self.store.append_event(key, "answer.judged", {"item": item, "met": 3, "total": 3, "mode": "probe", **base})
        self.at(self.clock.now + timedelta(minutes=5), write)
        preview = self.share(b, cid, kind="changed")
        self.assertEqual(preview["line"], NOT_A_CERTIFICATE)
        self.assertIn("Learner B showed", preview["text"])
        self.assertEqual(self.circle(a)["shared"][0]["text"], preview["text"])

    def test_one_circle_per_moment_and_sharing_needs_three(self):
        a, b, c, d = self.crowd(4)
        cid = self.start_circle(a, [b, c])
        self.ok("POST", "/api/play/circles", b, {"name": "Second", "people": [self.pid(a), self.pid(d)]})
        self.report_pass(b)
        m = self.moment(b, "passed")
        second = self.circle(b, "Second")["id"]
        self.ok("POST", f"/api/play/circles/{second}/share", b, {"moment": m["id"]}, status=409)   # 1 member yet
        self.ok("POST", f"/api/play/circles/{cid}/share", b, {"moment": m["id"]})
        self.accept(a, "Second")
        self.accept(d, "Second")
        self.ok("POST", f"/api/play/circles/{second}/share", b, {"moment": m["id"]}, status=409)
        self.ok("POST", f"/api/play/circles/{cid}/share", c, {"moment": m["id"]}, status=404)   # not c's moment

    def test_withdraw_and_four_weeks(self):
        a, b, c, cid = self.three()
        self.report_pass(b)
        self.share(b, cid)
        sid = self.circle(b)["shared"][0]["id"]
        self.ok("DELETE", f"/api/play/shares/{sid}", c, status=404)
        self.ok("DELETE", f"/api/play/shares/{sid}", b)
        self.assertEqual(self.circle(c)["shared"], [])
        self.share(b, cid)
        self.clock.advance(days=27, hours=23)
        self.assertEqual(len(self.circle(c)["shared"]), 1)
        self.clock.advance(hours=1)
        self.assertEqual(self.circle(c)["shared"], [])
        self.assertEqual(self.store.read("play", "circles", cid)["shares"], [])

    def test_a_copy_whose_support_fails_is_deleted_at_once(self):
        a, b, c, cid = self.three()
        self.report_pass(b, "2026-10-01")
        self.share(b, cid)
        self.report_pass(b, "2026-09-30")   # the pass changed: the shared copy no longer matches the Record
        # Deleted by the change itself: the circle document holds no copy before anyone reads it.
        self.assertEqual(self.store.read("play", "circles", cid)["shares"], [])
        self.assertEqual(self.circle(c)["shared"], [])

    def test_a_dispute_that_takes_the_support_deletes_the_copy_at_once(self):
        a, b, c, cid = self.three()
        skills = self.app.state.dojo.packages.by_id["gh-300"]["skills"]
        sid = next(k for k, v in skills.items() if v.get("kind") == "scenario")
        key = self.key_of(b)
        item = new_id("it")
        base = {"package": "gh-300", "skill": sid}

        def write():
            self.store.append_event(key, "item.issued", {"item": item, "mode": "probe", "kind": "x", "changed_condition": True, **base})
            self.clock.advance(seconds=40)
            self.store.append_event(key, "answer.submitted", {"item": item, "elapsed_s": 40, "input": "typed", **base})
            self.store.append_event(key, "answer.judged", {"item": item, "met": 3, "total": 3, "mode": "probe", **base})
        self.at(self.clock.now + timedelta(minutes=5), write)
        self.share(b, cid, kind="changed")
        self.assertEqual(len(self.store.read("play", "circles", cid)["shares"]), 1)
        self.store.append_event(key, "dispute.filed", {"item": item, **base})
        self.assertEqual(self.store.read("play", "circles", cid)["shares"], [])

    def test_the_hook_ignores_people_without_circles_and_never_fails_a_write(self):
        a, b, c, cid = self.three()
        self.report_pass(b)
        self.share(b, cid)
        with mock.patch.object(self.social, "_supported", side_effect=RuntimeError("boom")), self.assertLogs("dojo", "ERROR"):
            self.store.append_event(self.key_of(b), "lesson.viewed", {"package": "gh-300", "skill": "s1", "lesson": "le-1", "modality": "read"})
        self.assertEqual(self.store.events(self.key_of(b))[-1]["type"], "lesson.viewed")
        with mock.patch.object(self.social, "_supported") as checked:
            self.store.append_event(self.key_of(a), "lesson.viewed", {"package": "gh-300", "skill": "s1", "lesson": "le-1", "modality": "read"})
        checked.assert_not_called()   # a has nothing shared

    def test_the_sweeper_deletes_unsupported_copies_too(self):
        a, b, c, cid = self.three()
        self.report_pass(b)
        self.share(b, cid)
        self.report_pass(b, "2026-09-30")
        self.social.sweep()
        self.assertEqual(self.store.read("play", "circles", cid)["shares"], [])

    def test_leaving_the_circle_takes_the_copies(self):
        a, b, c, d = self.crowd(4)
        cid = self.start_circle(a, [b, c, d])
        self.report_pass(b)
        self.share(b, cid)
        self.ok("POST", f"/api/play/circles/{cid}/leave", b)
        self.assertEqual(self.circle(a)["shared"], [])
        self.assertNotIn("Learner B", json.dumps(self.store.read("play", "circles", cid)))


# ---------------------------------------------------------------- P-4: leaving deletes everything at once

class LeaveTests(SocialCase):
    def setup_shared(self):
        a, b, c, d = self.crowd(4)
        cid = self.start_circle(a, [b, c, d])
        self.ok("POST", "/api/play/circles", b, {"name": "Second", "people": [self.pid(a), self.pid(c)]})
        self.report_pass(b)
        self.share(b, cid)
        self.ok("POST", "/api/play/circles", a, {"name": "Third", "people": [self.pid(b), self.pid(c)]})
        return a, b, c, d, cid

    def gone(self, key: str) -> None:
        for cid in self.store.names("play", "circles"):
            doc = json.dumps(self.store.read("play", "circles", cid))
            self.assertNotIn(key, doc, cid)

    def test_leave_play(self):
        a, b, c, d, cid = self.setup_shared()
        key = self.key_of(b)
        self.ok("POST", "/api/play/leave", b)
        self.gone(key)
        self.assertFalse((self.folder(key) / "social" / "play.json").exists())
        self.assertEqual(self.circle(a)["shared"], [])
        # Rejoining starts empty: no circle, no invitation, no opt-in.
        self.ok("POST", "/api/play/join", b, {})
        v = self.view(b)
        self.assertFalse(v["opted"])
        self.opt(b)
        v = self.view(b)
        self.assertEqual((v["circles"], v["invitations"]), ([], []))

    def test_leave_circles_keeps_play(self):
        a, b, c, d, cid = self.setup_shared()
        key = self.key_of(b)
        self.ok("POST", "/api/play/social/leave", b)
        self.gone(key)
        self.assertTrue(self.ok("GET", "/api/play", b)["on"])
        self.assertFalse(self.view(b)["opted"])

    def test_delete_my_record(self):
        a, b, c, d, cid = self.setup_shared()
        key = self.key_of(b)
        self.ok("POST", "/api/record/delete", b, {"confirm": "DELETE"})
        self.gone(key)

    def test_leave_dojo(self):
        a, b, c, d, cid = self.setup_shared()
        key = self.key_of(b)
        self.ok("POST", "/api/team/leave", b, {"confirm": "LEAVE"})
        self.gone(key)

    def test_removal_drops_the_member_from_every_circle(self):
        a, b, c, d, cid = self.setup_shared()
        key = self.key_of(b)
        self.ok("POST", f"/api/team/members/{self.row(b['X-MS-CLIENT-PRINCIPAL-ID'])['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.gone(key)   # at the removal itself, before any read or sweep (the data waits 30 days; this does not)
        self.assertNotIn("Learner B", json.dumps(self.view(a)["circles"]))

    def test_an_expired_membership_leaves_every_circle_at_expiry(self):
        a, b, c, d, cid = self.setup_shared()
        key = self.key_of(b)
        row = self.row(b["X-MS-CLIENT-PRINCIPAL-ID"])
        self.team.sweep(now=team_expiry(row["renewed"]) + timedelta(hours=1))
        row = self.row(b["X-MS-CLIENT-PRINCIPAL-ID"])
        self.assertEqual((row["status"], row.get("deleted_at")), ("removed", None))   # not purged yet
        self.gone(key)

    def test_the_team_ending_takes_everyone_out_of_circles(self):
        a, b, c, d, cid = self.setup_shared()
        keys = [self.key_of(h) for h in (a, b, c, d)]
        with self.store.lock:
            for h in (a, b, c, d):
                self.team._set_removed(self.row(h["X-MS-CLIENT-PRINCIPAL-ID"]), "team-ended", self.clock.now)
        for k in keys:
            self.gone(k)

    def test_turning_off_invitable_withdraws_open_invitations(self):
        a, b, c, d = self.crowd(4)
        self.ok("POST", "/api/play/circles", a, {"name": "X", "people": [self.pid(b), self.pid(c)]})
        self.ok("PUT", "/api/play/social", b, {"invitable": False})
        self.assertEqual(self.view(b)["invitations"], [])
        self.gone(self.key_of(b))


# ---------------------------------------------------------------- P-5, P-8, P-11, P-13

class PrivacyTests(SocialCase):
    def test_display_names_only_and_no_keys(self):
        a, b, c = self.crowd(3)
        self.start_circle(a, [b, c])
        self.report_pass(b)
        self.share(b, self.circle(a)["id"])
        for h in (a, b, c):
            body = json.dumps(self.view(h))
            for other in ("m-0", "m-1", "m-2"):
                self.assertNotIn(f"Entra {other}", body)
                self.assertNotIn(f"{other}@corp.example", body)
                self.assertNotIn(self.key(other), body)
            self.assertNotIn(OWNER_KEY, body)

    def test_isolation_of_points_moments_and_settings(self):
        a, b, c = self.crowd(3)
        cid = self.start_circle(a, [b, c])
        self.report_pass(b)
        self.study(b, self.clock.now - timedelta(hours=1), answers=10)
        v = json.dumps(self.view(a))
        self.assertNotIn(self.moment(b, "passed")["id"], v)
        self.assertNotIn("reports passing", v)   # nothing until B shares it
        for word in ("points", "study_days", "weeks_met", "rhythm", "hidden"):
            self.assertNotIn(f'"{word}"', v)
        self.assertNotIn('"joined"', json.dumps(self.circle(a)))

    def test_no_names_or_ids_in_the_logs(self):
        a, b, c = self.crowd(3)
        with self.assertLogs("dojo", level="INFO") as logs:
            cid = self.start_circle(a, [b, c], name="Secret garden")
            self.report_pass(b)
            self.share(b, cid)
            sid = self.circle(b, "Secret garden")["shared"][0]["id"]
            self.ok("DELETE", f"/api/play/shares/{sid}", b)
            self.ok("POST", f"/api/play/circles/{cid}/leave", c)
        text = "\n".join(logs.output)
        self.assertIn("/api/play/circles/-/share", text)
        for s in ("Secret garden", cid, sid, "Learner A", "Learner B", "reports passing", self.pid(a), self.pid(b)):
            self.assertNotIn(s, text)
        self.assertIsNone(re.search(r"(circle|inv|share|mo|p)-[0-9a-f]{20}", text))

    def test_export_holds_only_my_own_opt_in(self):
        a, b, c = self.crowd(3)
        cid = self.start_circle(a, [b, c], name="Secret garden")
        export = self.c.get("/api/record/export", headers=a)
        self.assertEqual(export.status_code, 200)
        self.assertNotIn("Secret garden", export.text)
        self.assertNotIn(self.key_of(b), export.text)


class ShareBoxTests(unittest.TestCase):
    """EG-25: Share is pressed only for the preview on screen, for the circle now selected."""

    def setUp(self):
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text("utf-8")
        start = script.index("function shareBox(")
        self.body = script[start:script.index("\n}\n", start)]

    def test_share_waits_for_the_selected_circles_preview(self):
        show = self.body[self.body.index("const show = async"):self.body.index('pick.addEventListener("change", show)')]
        self.assertLess(show.index("go.disabled = true"), show.index("await playApi("))
        self.assertLess(show.index("if (mine !== token) return;"), show.index("shown = cid;"))
        self.assertLess(show.index("shown = cid;"), show.index("go.disabled = false"))
        self.assertIn("playApi(`/circles/${shown}/share`", self.body)
        self.assertNotIn("/circles/${pick.value}/share", self.body)
        self.assertIn("token++", self.body)   # Cancel drops a preview still on its way


if __name__ == "__main__":
    unittest.main()
