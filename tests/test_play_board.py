"""Play PR 3: the effort board and duels (ADR 0010 sections 5, 6 and 8)."""
from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.test_play_social import START, UTC, SocialCase  # noqa: F401 - START documents the clock
from tests.test_team import OWNER, OWNER_KEY, make, who

from app import play_board as pb
from app import play_duel as pd
from app import play_social as ps

DUEL = "duel-0123456789abcdef0123"
ROUTES = [
    ("GET", "/api/play/board"), ("POST", "/api/play/board/join"), ("POST", "/api/play/board/leave"),
    ("PUT", "/api/play/board"), ("GET", "/api/play/duels"), ("POST", "/api/play/duels"),
    ("POST", f"/api/play/duels/{DUEL}/accept"), ("POST", f"/api/play/duels/{DUEL}/decline"),
    ("POST", f"/api/play/duels/{DUEL}/open"),
]
NEXT_MONDAY = START + timedelta(weeks=1)


class PlayCase(SocialCase):
    def setUp(self):
        super().setUp()
        for target in ("app.play_board.utcnow", "app.play_duel.utcnow"):
            p = mock.patch(target, self.clock)
            p.start()
            self.addCleanup(p.stop)

    # ---- the board

    def board(self, h: dict) -> dict:
        return self.ok("GET", "/api/play/board", h)

    def join_board(self, *hs: dict) -> None:
        for h in hs:
            self.ok("POST", "/api/play/board/join", h)

    def week_of(self, h: dict, days: int, extra: bool = False) -> None:
        """Study on `days` days of the week that starts at START: 40 points a day, 50 with a lesson burst,
        plus 3 labs once with `extra` (7 x 50 + 30 = 380, a "Strong week")."""
        for d in range(days):
            self.study(h, START + timedelta(days=d, hours=1), answers=10,
                       lessons=5 if extra else 0, labs=3 if extra and d == 0 else 0)

    # ---- duels

    def duels(self, h: dict) -> dict:
        return self.ok("GET", "/api/play/duels", h)

    def invite(self, a: dict, b: dict, domain: str | None = None) -> str:
        res = self.ok("POST", "/api/play/duels", a, {"person": self.pid(b), "package": "gh-300", "domain": domain})
        job = self.wait_job(res["job"], a)
        self.assertEqual(job["state"], "done", job)
        return res["duel"]

    def duel(self, h: dict, did: str) -> dict:
        return next(d for d in self.duels(h)["duels"] if d["id"] == did)

    def play_all(self, h: dict, did: str, right: int = 10) -> str:
        rid = self.ok("POST", f"/api/play/duels/{did}/open", h)["rehearsal"]
        doc = self.store.read("learners", self.key_of(h), "rehearsals", rid)
        for i, it in enumerate(doc["items"]):
            choice = it["answer"] if i < right else (it["answer"] + 1) % 4
            self.clock.advance(seconds=20)
            self.ok("POST", f"/api/rehearsals/{rid}/answer", h, {"index": i, "choice": choice})
        return rid

    def stems(self, did: str) -> list[str]:
        return [it["stem"] for it in self.store.read("play", "duels", did)["items"]]


# ---------------------------------------------------------------- P-1: behind team mode and the switch

class GateTests(PlayCase):
    def test_404_unless_team_mode_and_the_switch(self):
        h = self.member("m-1", "Ada", opt=False)
        for method, path in ROUTES:
            r = self.call(method, path, h, {"mine_only": True})
            self.assertEqual(r.status_code, 404, f"{method} {path}: {r.text}")
        app, c, _ = make(team=False)
        app.state.dojo.store.write("team", "play", value={"on": True, "round": 1})
        for method, path in ROUTES:
            r = c.request(method, path, headers={**OWNER, "X-Dojo": "1"}, json={})
            self.assertEqual(r.status_code, 404, f"{method} {path}: {r.text}")

    def test_a_malformed_body_gets_404_while_off(self):
        h = self.member("m-1", "Ada", opt=False)
        stranger = who("stranger")
        for method, path in ROUTES:
            if method == "GET":
                continue
            r = self.c.request(method, path, headers={**h, "X-Dojo": "1", "Content-Type": "application/json"}, content=b"{")
            self.assertEqual(r.status_code, 404, f"{method} {path}: {r.text}")
            r = self.c.request(method, path, headers={**stranger, "X-Dojo": "1", "Content-Type": "application/json"}, content=b"{")
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "not_member"), f"{method} {path}")

    def test_a_switch_off_while_joining_the_board_waits_wins(self):
        a, = self.crowd(1)
        real = self.social._is_owner

        def meanwhile(key):   # between join's first check and its locked step
            self.social.set_switch(False)
            return real(key)
        with mock.patch.object(self.social, "_is_owner", meanwhile):
            r = self.call("POST", "/api/play/board/join", a)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertNotIn("board", json.dumps(self.store.read("learners", self.key_of(a), "social", "play")))

    def test_a_removal_takes_the_board_place_and_duels_at_once(self):
        a, b, c = self.crowd(3)
        self.join_board(a, b)
        self.board(a)
        did = self.invite(a, b)
        key = self.key_of(b)
        self.ok("POST", f"/api/team/members/{self.row(b['X-MS-CLIENT-PRINCIPAL-ID'])['id']}/remove", OWNER, {"confirm": "REMOVE"})
        self.assertIsNone(self.store.read("play", "duels", did))
        self.assertNotIn(key, json.dumps(self.store.read("play", "board", default={})))

    def test_switching_off_deletes_the_board_and_every_duel(self):
        people = self.crowd(3)
        self.join_board(people[0])
        self.invite(people[0], people[1])
        self.board(people[0])
        self.switch(False)
        self.assertIsNone(self.store.read("play", "board", default=None))
        self.assertEqual(self.store.names("play", "duels"), [])

    def test_the_notice_covers_the_board_and_duels(self):
        self.switch(True)
        h = self.member("m-1", "Ada", opt=False)
        n = self.ok("GET", "/api/play/social", h)["notice"]
        self.assertEqual(n["version"], 2)
        titles = [p["title"] for p in n["in_app"]]
        self.assertIn("The effort board", titles)
        self.assertIn("Duels", titles)
        text = " ".join(p["text"] for p in n["in_app"])
        self.assertIn("looks exactly like one that lapsed", text)
        self.assertIn("never of results", text)


# ---------------------------------------------------------------- the effort board (ADR 0010 section 5)

class BoardTests(PlayCase):
    def test_off_by_default_and_closed_below_ten(self):
        people = self.crowd(10)
        v = self.board(people[0])
        self.assertFalse(v["joined"])
        self.assertEqual(v["state"], "closed")
        self.join_board(*people[:9])
        v = self.board(people[0])
        self.assertEqual((v["state"], v["count"]), ("closed", 9))
        self.assertIn("The board opens when 10 people have joined. 9 have joined so far.", v["text"])
        self.join_board(people[9])
        v = self.board(people[0])
        self.assertEqual(v["state"], "closed")   # published once a week: it opens on Monday
        self.assertIn("published on Monday 12 Oct", v["text"])
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        self.assertEqual(self.board(people[0])["state"], "shown")

    def test_bands_names_in_your_band_only_and_your_own_points(self):
        people = self.crowd(12)
        strong, steady, building = people[:4], people[4:6], people[6:8]
        for h in strong:
            self.week_of(h, 7, extra=True)
        for h in steady:
            self.week_of(h, 6)
        for h in building:
            self.week_of(h, 3)
        self.join_board(*people)
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        v = self.board(strong[0])
        self.assertEqual(v["state"], "shown")
        self.assertEqual(v["me"]["band"]["name"], "Strong week")
        last = self.ok("GET", "/api/play", strong[0])["points"]["weeks"][-2]
        self.assertEqual(v["me"]["points"], last["total"])
        self.assertEqual(v["names"], ["Learner B", "Learner C", "Learner D"])   # alphabetical, never by points
        body = json.dumps(v)
        for h in people[4:]:
            self.assertNotIn(self.view(h)["me"]["name"], body)
        for word in ("position", "rank", "order", "total", "top", "bottom"):
            self.assertNotIn(f'"{word}"', body)
        v = self.board(steady[0])
        self.assertEqual(v["me"]["band"]["name"], "Steady")
        self.assertNotIn("names", v)
        self.assertEqual(v["others"], 1)   # fewer than 3 others: a number, no names
        v = self.board(people[11])
        self.assertEqual(v["me"]["band"]["name"], "Getting going")
        self.assertEqual(v["names"], ["Learner I", "Learner J", "Learner K"])

    def test_the_snapshot_holds_bands_only_and_never_changes_in_the_week(self):
        people = self.crowd(10)
        self.join_board(*people)
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        before = self.board(people[0])
        snap = self.store.read("play", "board")
        self.assertEqual(set(snap), {"week", "published", "open", "bands", "upto"})
        self.assertTrue(all(isinstance(b, int) and 0 <= b <= 3 for b in snap["bands"].values()))
        # The Record position each band was read from: a hash (or "" for an empty Record), never a number.
        self.assertEqual(set(snap["upto"]), set(snap["bands"]))
        self.assertTrue(all(isinstance(m, str) and re.fullmatch(r"[0-9a-f]*", m) for m in snap["upto"].values()))
        for d in range(3):
            self.study(people[1], NEXT_MONDAY + timedelta(days=d, hours=2), answers=10, lessons=5, labs=3)
        self.clock.to(NEXT_MONDAY + timedelta(days=4))
        self.assertEqual(self.store.read("play", "board"), snap)
        self.assertEqual(self.board(people[0])["names"], before["names"])

    def test_one_week_kept_and_replaced_next_monday(self):
        people = self.crowd(10)
        self.join_board(*people)
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        self.board(people[0])
        first = self.store.read("play", "board")
        self.clock.to(NEXT_MONDAY + timedelta(weeks=1, hours=1))
        self.social.sweep()
        second = self.store.read("play", "board")
        self.assertNotEqual(first["week"], second["week"])
        self.assertEqual(self.store.names("play"), ["board"])   # no history beside it

    def test_your_points_are_read_as_published_like_your_band(self):
        """GPT #45-5: an answer sent on Sunday and judged after Monday's publication must not move the shown
        points (out of the band they were published in)."""
        people = self.crowd(10)
        h = people[0]
        self.week_of(h, 2)   # 80 points: "Getting going"
        key = self.key_of(h)
        items = []

        def sunday():
            for i in range(10):
                item = f"it-late-{i}"
                base = {"package": "gh-300", "skill": "s1"}
                self.store.append_event(key, "item.issued", {"item": item, "mode": "probe", "kind": "x", "changed_condition": False, **base})
                self.clock.advance(seconds=30)
                self.store.append_event(key, "answer.submitted", {"item": item, "elapsed_s": 30, "input": "typed", **base})
                items.append(item)
        self.at(START + timedelta(days=6, hours=10), sunday)   # Sunday 20:00 in Berlin
        self.join_board(*people)
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        v = self.board(h)
        self.assertEqual((v["me"]["points"], v["me"]["band"]["name"]), (80, "Getting going"))
        self.clock.to(NEXT_MONDAY + timedelta(hours=2))
        for item in items:   # judged after publication: these answers were sent on Sunday
            self.store.append_event(key, "answer.judged", {"item": item, "met": 1, "total": 3, "mode": "probe", "package": "gh-300", "skill": "s1"})
        self.assertEqual(self.ok("GET", "/api/play", h)["points"]["weeks"][-2]["total"], 120)   # Building, now
        v = self.board(h)
        self.assertEqual((v["me"]["points"], v["me"]["band"]["name"]), (80, "Getting going"))

    def test_owner_cannot_join_and_is_never_on_it(self):
        people = self.crowd(10)
        self.ok("POST", "/api/play/join", OWNER, {})
        self.opt(OWNER)
        self.ok("POST", "/api/play/board/join", OWNER, status=403)
        self.join_board(*people)
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        self.assertNotIn(OWNER_KEY, json.dumps(self.store.read("play", "board")))
        v = self.board(OWNER)
        self.assertTrue(v["owner"])
        self.assertNotIn("me", v)
        self.assertNotIn("names", v)
        for h in people:
            self.assertNotIn(ps.OWNER_LABEL, json.dumps(self.board(h)))

    def test_leaving_takes_you_off_at_once_and_pauses_below_ten(self):
        people = self.crowd(11)
        self.join_board(*people)
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        self.assertEqual(len(self.board(people[0])["names"]), 10)
        self.ok("POST", "/api/play/board/leave", people[10])
        self.assertNotIn(self.key_of(people[10]), self.store.read("play", "board")["bands"])
        v = self.board(people[0])
        self.assertNotIn("Learner K", v["names"])
        self.ok("POST", "/api/play/leave", people[9], {})   # Leave Play
        self.assertNotIn(self.key_of(people[9]), self.store.read("play", "board")["bands"])
        v = self.board(people[0])
        self.assertEqual(v["state"], "paused")
        self.assertNotIn("names", v)

    def test_just_my_points(self):
        people = self.crowd(10)
        self.join_board(*people)
        self.ok("PUT", "/api/play/board", people[0], {"mine_only": True})
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        v = self.board(people[0])
        self.assertTrue(v["mine_only"])
        self.assertIn("me", v)
        self.assertNotIn("names", v)
        self.assertNotIn("others", v)

    def test_joining_mid_week_waits_for_monday(self):
        people = self.crowd(11)
        self.join_board(*people[:10])
        self.clock.to(NEXT_MONDAY + timedelta(hours=1))
        self.board(people[0])
        self.join_board(people[10])
        v = self.board(people[10])
        self.assertEqual(v["state"], "soon")
        self.assertNotIn("me", v)
        self.assertNotIn("Learner K", json.dumps(self.board(people[0])))


# ---------------------------------------------------------------- duels (ADR 0010 section 6)

class DuelTests(PlayCase):
    def test_written_once_and_charged_to_the_inviter(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b, domain="d1")
        jobs = [self.store.read("jobs", j) for j in self.store.names("jobs")]
        mine = [j for j in jobs if j["learner"] == self.key_of(a) and j["kind"] == "rehearsal"]
        self.assertEqual(len(mine), 1)
        self.assertFalse([j for j in jobs if j["learner"] == self.key_of(b)])
        doc = self.store.read("play", "duels", did)
        self.assertEqual(len(doc["items"]), pd.QUESTIONS)
        pkg = self.app.state.dojo.packages.get("gh-300")
        self.assertTrue(all(pkg["skills"][it["skill"]]["domain"] == "d1" for it in doc["items"]))

    def test_seen_only_by_a_player_who_opens_and_never_in_a_pool(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        stems = self.stems(did)
        from app.exam import question_key, seen_keys
        keys = {question_key(s) for s in stems}
        for h in (a, b):
            self.assertFalse(keys & seen_keys(self.store, self.key_of(h), "gh-300"))
        self.ok("POST", f"/api/play/duels/{did}/open", a)
        self.assertEqual(keys, keys & seen_keys(self.store, self.key_of(a), "gh-300"))
        self.ok("POST", f"/api/play/duels/{did}/open", b, status=409)   # not accepted yet
        self.ok("POST", f"/api/play/duels/{did}/decline", b)
        self.assertFalse(keys & seen_keys(self.store, self.key_of(b), "gh-300"))
        root = Path(self.tmp)
        for path in root.rglob("*.json*"):
            text = path.read_text("utf-8", errors="ignore")
            if any(s in text for s in stems):
                rel = path.relative_to(root).as_posix()
                self.assertTrue(rel.startswith("play/duels/") or rel.startswith(f"learners/{self.key_of(a)}/rehearsals/"), rel)

    def test_result_only_for_the_two_once_both_have_finished(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        self.play_all(a, did, right=8)
        v = self.duel(a, did)
        self.assertEqual(v["state"], "waiting")
        self.assertIsNone(v["result"])
        self.ok("POST", f"/api/play/duels/{did}/accept", b)
        self.play_all(b, did, right=6)
        va, vb = self.duel(a, did), self.duel(b, did)
        self.assertEqual(va["result"], {"mine": 8, "mine_of": 10, "theirs": 6, "theirs_of": 10})
        self.assertEqual(vb["result"], {"mine": 6, "mine_of": 10, "theirs": 8, "theirs_of": 10})
        self.assertEqual(self.duels(c)["duels"], [])
        for verb in ("accept", "decline", "open"):
            self.ok("POST", f"/api/play/duels/{did}/{verb}", c, status=404)
        body = json.dumps([va, vb]).lower()
        for word in ("won", "lost", "win", "loss", "winner", "seconds", "answered_at", '"at"', "created", "invited"):
            self.assertNotIn(word, body)

    def test_each_reviews_only_their_own_misses(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        ra = self.play_all(a, did, right=7)
        self.ok("POST", f"/api/play/duels/{did}/accept", b)
        rb = self.play_all(b, did, right=5)
        self.ok("GET", f"/api/rehearsals/{rb}", a, status=404)
        self.ok("GET", f"/api/rehearsals/{ra}", b, status=404)
        mine = self.ok("GET", f"/api/rehearsals/{ra}", a)
        self.assertEqual(sum(1 for x in mine["items"] if x["correct"] is False), 3)
        self.assertEqual(mine["origin"], "duel")   # the page names it a duel and links back to Play
        body = json.dumps(self.duel(a, did))
        self.assertNotIn("items", body)
        self.assertNotIn("missed", body)

    def test_a_decline_looks_exactly_like_a_lapse(self):
        a, b, c = self.crowd(3)
        d1 = self.invite(a, b)
        d2 = self.invite(a, c)
        self.ok("POST", f"/api/play/duels/{d1}/decline", b)
        self.assertNotIn("declin", json.dumps(self.store.read("play", "duels", d1)))
        self.assertEqual(self.duels(b)["duels"], [])

        def same(v: dict) -> dict:
            return {k: x for k, x in v.items() if k not in ("id", "with")}
        self.assertEqual(same(self.duel(a, d1)), same(self.duel(a, d2)))
        self.clock.advance(hours=49)
        v1, v2 = self.duel(a, d1), self.duel(a, d2)
        self.assertEqual(same(v1), same(v2))
        self.assertEqual(v1["state"], "closed")
        self.assertEqual(v1["with"]["name"], "Learner B")

    def test_closes_after_48_hours_with_your_own_result_only(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        self.play_all(a, did, right=9)
        self.ok("POST", f"/api/play/duels/{did}/accept", b)
        rid = self.ok("POST", f"/api/play/duels/{did}/open", b)["rehearsal"]
        self.ok("POST", f"/api/rehearsals/{rid}/answer", b, {"index": 0, "choice": 0})
        self.clock.advance(hours=48)
        va, vb = self.duel(a, did), self.duel(b, did)
        self.assertEqual((va["state"], vb["state"]), ("closed", "closed"))
        self.assertEqual(va["result"], {"mine": 9, "mine_of": 10})
        self.assertEqual(set(vb["result"]), {"mine", "mine_of"})
        self.assertEqual(va["text"], "The duel closed. Here is your result.")

    def test_deleted_after_seven_days_and_the_record_names_no_opponent(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        ra = self.play_all(a, did)
        self.ok("POST", f"/api/play/duels/{did}/accept", b)
        rb = self.play_all(b, did)
        self.clock.advance(days=7)
        self.social.sweep()
        self.assertEqual(self.store.names("play", "duels"), [])
        for h, rid, other in ((a, ra, b), (b, rb, a)):
            doc = json.dumps(self.store.read("learners", self.key_of(h), "rehearsals", rid))
            events = json.dumps(self.store.events(self.key_of(h)))
            for text in (doc, events):
                self.assertNotIn(self.key_of(other), text)
                self.assertNotIn(self.pid(other), text)
                self.assertNotIn(did, text)
            self.assertEqual(json.loads(doc)["origin"], "duel")
            self.assertEqual(sum(1 for e in self.store.events(self.key_of(h)) if e["type"] == "mcq.answered"), 10)

    def test_leaving_play_deletes_the_duel_at_once(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        self.ok("POST", "/api/play/leave", b, {})
        self.assertEqual(self.store.names("play", "duels"), [])
        did = self.invite(a, c)
        self.ok("POST", "/api/play/social/leave", a)
        self.assertIsNone(self.store.read("play", "duels", did))

    def test_a_declined_duel_goes_when_the_decliner_leaves_play(self):
        """GPT #45-2: a decline clears `b` but keeps the opaque `b_id`; leaving Play must still find it."""
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        self.ok("POST", f"/api/play/duels/{did}/decline", b)
        self.assertEqual(self.store.read("play", "duels", did)["b_id"], self.pid(b))
        self.ok("POST", "/api/play/leave", b, {})
        self.assertIsNone(self.store.read("play", "duels", did))
        self.assertNotIn(did, json.dumps(self.duels(a)))
        d2 = self.invite(a, c)
        self.ok("POST", f"/api/play/duels/{d2}/decline", c)
        self.ok("POST", "/api/play/social/leave", c)   # leaving circles too
        self.assertIsNone(self.store.read("play", "duels", d2))

    def test_the_export_holds_no_key_of_a_duel_question_not_answered(self):
        """GPT #45-1: opening a duel writes a quick practice with the keys; the export must not hand them out."""
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        rid = self.ok("POST", f"/api/play/duels/{did}/open", a)["rehearsal"]
        stored = self.store.read("learners", self.key_of(a), "rehearsals", rid)

        def exported() -> dict:
            out = self.ok("GET", "/api/record/export", a)
            return next(r for r in out["rehearsals"] if r["id"] == rid)
        doc = exported()
        self.assertEqual(len(doc["items"]), 10)
        for it in doc["items"]:
            for field in ("answer", "rationale", "quote"):
                self.assertNotIn(field, it)
        self.assertIn("10 duel questions", doc["duel_withheld"])
        self.clock.advance(seconds=20)
        self.ok("POST", f"/api/rehearsals/{rid}/answer", a, {"index": 0, "choice": stored["items"][0]["answer"]})
        doc = exported()
        self.assertEqual(doc["items"][0]["answer"], stored["items"][0]["answer"])   # answered: all of it
        self.assertIn("rationale", doc["items"][0])
        self.assertTrue(all("answer" not in it for it in doc["items"][1:]))
        self.assertIn("9 duel questions", doc["duel_withheld"])
        for it in self.ok("GET", f"/api/rehearsals/{rid}", a)["items"][1:]:   # the page, as before
            self.assertNotIn("answer", it)
        for i, it in enumerate(stored["items"][1:], start=1):
            self.clock.advance(seconds=20)
            self.ok("POST", f"/api/rehearsals/{rid}/answer", a, {"index": i, "choice": it["answer"]})
        doc = exported()
        self.assertNotIn("duel_withheld", doc)
        self.assertEqual([it["answer"] for it in doc["items"]], [it["answer"] for it in stored["items"]])

    def test_a_duel_whose_writing_job_is_gone_is_given_up(self):
        """GPT #45-3: after a restart (or a job lost before it began) a duel stuck in `writing` must not hold
        the inviter's limits for a week."""
        people = self.crowd(5)
        a = people[0]
        jobs = self.app.state.dojo.jobs

        def no_job(*args, **kwargs):   # the job never runs, as after a restart
            return {"id": "job-gone", "state": "interrupted"}
        with mock.patch.object(jobs, "start", no_job):
            stuck = [self.ok("POST", "/api/play/duels", a, {"person": self.pid(h), "package": "gh-300"})["duel"]
                     for h in people[1:4]]
        self.assertTrue(all(self.store.read("play", "duels", d)["state"] == "writing" for d in stuck))
        self.ok("POST", "/api/play/duels", a, {"person": self.pid(people[4]), "package": "gh-300"}, status=409)
        self.clock.advance(seconds=60)   # inside the grace: a job may be about to start
        self.assertEqual(len(self.store.names("play", "duels")), 3)
        with mock.patch.object(jobs, "busy", lambda *args: True):   # a job still queued or running: kept
            self.clock.advance(minutes=5)
            self.duels(a)
            self.assertEqual(len(self.store.names("play", "duels")), 3)
        self.duels(a)
        self.assertEqual(self.store.names("play", "duels"), [])
        self.invite(a, people[1])   # the limits are free again
        self.invite(a, people[4])

    def test_the_owner_gets_no_invitation_candidates(self):
        """GPT #45-4: the owner never invites, so he must not learn who studies which exam."""
        a, b, c = self.crowd(3)
        self.ok("POST", "/api/play/join", OWNER, {})
        self.opt(OWNER)
        v = self.duels(OWNER)
        self.assertTrue(v["owner"])
        self.assertFalse(v["can_invite"])
        self.assertNotIn("exams", v)
        body = json.dumps(v)
        self.assertNotIn('"people"', body)
        for h in (a, b, c):
            self.assertNotIn(self.pid(h), body)
            self.assertNotIn(self.view(h)["me"]["name"], body)
        self.assertTrue(self.duels(a)["exams"][0]["people"])   # a member still gets them

    def test_who_can_be_invited(self):
        a, b, c = self.crowd(3)
        self.ok("PUT", "/api/play/social", c, {"invitable": False})
        people = {p["name"] for e in self.duels(a)["exams"] if e["id"] == "gh-300" for p in e["people"]}
        self.assertEqual(people, {"Learner B"})
        self.ok("POST", "/api/play/duels", a, {"person": self.pid(c), "package": "gh-300"}, status=400)
        others = [p["id"] for p in self.ok("GET", "/api/me", b)["packages"] if p["id"] != "gh-300"]
        self.ok("PUT", "/api/settings", b, {"enrolled": others[:1]})
        self.ok("POST", "/api/play/duels", a, {"person": self.pid(b), "package": "gh-300"}, status=400)
        self.ok("POST", "/api/play/duels", a, {"person": self.pid(a), "package": "gh-300"}, status=400)

    def test_turning_invitations_off_ends_open_invitations_like_a_decline(self):
        a, b, c = self.crowd(3)
        did = self.invite(a, b)
        self.ok("PUT", "/api/play/social", b, {"invitable": False})
        self.assertIsNone(self.store.read("play", "duels", did)["b"])
        self.assertEqual(self.duel(a, did)["state"], "open")

    def test_the_owner_never_invites_and_plays_only_when_invited(self):
        a, b, c = self.crowd(3)
        self.ok("POST", "/api/play/join", OWNER, {})
        self.opt(OWNER)
        self.ok("POST", "/api/play/duels", OWNER, {"person": self.pid(a), "package": "gh-300"}, status=403)
        owner = next(p for e in self.duels(a)["exams"] if e["id"] == "gh-300" for p in e["people"]
                     if p["label"] == ps.OWNER_LABEL)
        res = self.ok("POST", "/api/play/duels", a, {"person": owner["id"], "package": "gh-300"})
        self.wait_job(res["job"], a)
        v = self.duel(OWNER, res["duel"])
        self.assertEqual(v["state"], "invited")
        self.assertIn("runs this Dojo", self.duel(a, res["duel"])["with"]["label"])
        self.ok("POST", f"/api/play/duels/{res['duel']}/accept", OWNER)
        self.ok("POST", f"/api/play/duels/{res['duel']}/open", OWNER)

    def test_at_most_three_running_and_one_per_person(self):
        people = self.crowd(5)
        a = people[0]
        self.invite(a, people[1])
        self.ok("POST", "/api/play/duels", a, {"person": self.pid(people[1]), "package": "gh-300"}, status=409)
        self.invite(a, people[2])
        self.invite(a, people[3])
        self.ok("POST", "/api/play/duels", a, {"person": self.pid(people[4]), "package": "gh-300"}, status=409)

    def test_no_names_or_ids_in_the_logs(self):
        a, b, c = self.crowd(3)
        with self.assertLogs("dojo", level="INFO") as logs:
            did = self.invite(a, b)
            self.ok("POST", f"/api/play/duels/{did}/accept", b)
            self.ok("POST", f"/api/play/duels/{did}/open", b)
        text = "\n".join(logs.output)
        for s in (did, "Learner A", "Learner B", self.pid(a), self.pid(b)):
            self.assertNotIn(s, text)
        self.assertIsNone(re.search(r"duel-[0-9a-f]{20}", text))


if __name__ == "__main__":
    unittest.main()
