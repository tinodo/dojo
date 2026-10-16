"""The books of the search for official pages (app/sourcing/state.py): the lease with its epoch, the ledger of steps
and the fenced documents. Nothing here touches the network."""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DOJO_LOCAL", "1")

from app.core import Store, iso, parse_iso, utcnow  # noqa: E402
from app.sourcing.state import (MAX_TRIES, Books, DeadStep, Lease, LeaseLost, begin, dead, due, finish, fingerprint,  # noqa: E402
                                new_skill, skill_key)
from app.sources import normalize  # noqa: E402


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def leases(store: Store, name: str = "xy-110"):
    clock = Clock()
    mine = Lease(store, name, owner="a", ttl=600, clock=clock, pause=lambda s: None)
    other = Lease(store, name, owner="b", ttl=600, clock=clock, pause=lambda s: None)
    return clock, mine, other


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp(prefix="dojo-lease-")))

    def test_one_owner_at_a_time(self):
        _, a, b = leases(self.store)
        self.assertTrue(a.acquire())
        self.assertEqual(a.epoch, 1)
        self.assertFalse(b.acquire(), "the lease is held and has not expired")
        self.assertTrue(a.acquire(), "the owner takes it again and so renews it")
        self.assertEqual(a.epoch, 1)
        a.check()
        with self.assertRaises(LeaseLost):
            b.check()

    def test_a_released_or_expired_lease_is_taken_over_with_a_new_epoch(self):
        clock, a, b = leases(self.store)
        self.assertTrue(a.acquire())
        a.release()
        self.assertTrue(b.acquire())
        self.assertEqual(b.epoch, 2)
        clock.now += 601   # b stops renewing: its lease expires
        self.assertTrue(a.acquire())
        self.assertEqual(a.epoch, 3)
        with self.assertRaises(LeaseLost):
            b.check()
        with self.assertRaises(LeaseLost):
            b.renew()

    def test_a_lease_file_that_cannot_be_read_is_taken_over(self):
        _, a, b = leases(self.store)
        a.path.parent.mkdir(parents=True, exist_ok=True)
        a.path.write_text("not json", "utf-8")
        self.assertTrue(b.acquire())
        self.assertEqual(b.epoch, 1)

    def test_a_concurrent_writer_that_wins_is_noticed(self):
        clock, a, b = leases(self.store)

        def intruder(seconds):   # another process writes between a's write and a's second look
            a.path.write_text(json.dumps({"owner": "b", "epoch": 1, "expires": clock.now + 600}), "utf-8")

        a.pause = intruder
        self.assertFalse(a.acquire())
        self.assertEqual(a.epoch, 0)
        with self.assertRaises(LeaseLost):
            a.check()

    def test_a_stale_owner_cannot_write_a_document(self):
        clock, a, b = leases(self.store)
        self.assertTrue(a.acquire())
        books_a = Books(self.store, "xy-110", a)
        doc = new_skill("k1")
        books_a.save_skill(doc)
        self.assertEqual(doc["epoch"], 1)
        clock.now += 601
        self.assertTrue(b.acquire())
        with self.assertRaises(LeaseLost):
            books_a.save_skill(doc)
        books_b = Books(self.store, "xy-110", b)
        again = books_b.skill("k1")
        books_b.save_skill(again)
        self.assertEqual(again["epoch"], 2)

    def test_a_document_of_a_newer_owner_is_never_written_over(self):
        _, a, b = leases(self.store)
        self.assertTrue(a.acquire())
        books = Books(self.store, "xy-110", a)
        doc = new_skill("k1")
        doc["epoch"] = 5
        with self.assertRaises(LeaseLost):
            books.save_skill(doc)


class LedgerTests(unittest.TestCase):
    def test_a_done_step_is_not_repeated_but_a_changed_input_is(self):
        doc = new_skill("k")
        fp = fingerprint("verify", 2, ["u1"], ["gate", "author"])
        self.assertTrue(begin(doc, "verify.0", fp))
        finish(doc, "verify.0")
        self.assertFalse(begin(doc, "verify.0", fp))
        self.assertTrue(begin(doc, "verify.0", fingerprint("verify", 3, ["u1"], ["gate", "author"])))
        self.assertEqual(doc["steps"]["1.1.verify.0"]["tries"], 1, "a changed fingerprint starts the count again")

    def test_a_step_without_a_result_is_tried_until_it_is_dead(self):
        doc = new_skill("k")
        fp = fingerprint("retrieve", ["q"])
        for n in range(MAX_TRIES):
            self.assertTrue(begin(doc, "retrieve", fp))
            self.assertEqual(doc["steps"]["1.1.retrieve"]["tries"], n + 1)
        with self.assertRaises(DeadStep):
            begin(doc, "retrieve", fp)
        self.assertEqual(dead(doc), ["1.1.retrieve"])

    def test_a_new_cycle_has_fresh_tries(self):
        doc = new_skill("k")
        fp = fingerprint("retrieve", ["q"])
        for _ in range(MAX_TRIES):
            begin(doc, "retrieve", fp)
        doc["cycle"] += 1
        self.assertTrue(begin(doc, "retrieve", fp))
        self.assertEqual(dead(doc), [])

    def test_the_round_is_part_of_the_key(self):
        doc = new_skill("k")
        fp = fingerprint("plan", "x")
        begin(doc, "plan", fp)
        finish(doc, "plan")
        doc["round"] = 1
        self.assertTrue(begin(doc, "plan", fp), "the same step of the next round is a new step")

    def test_the_fingerprint_does_not_depend_on_order_of_keys(self):
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))
        self.assertNotEqual(fingerprint("a"), fingerprint("b"))


class BooksTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp(prefix="dojo-books-")))
        _, self.lease, _ = leases(self.store)
        self.assertTrue(self.lease.acquire())
        self.books = Books(self.store, "xy-110", self.lease)

    def test_a_skill_without_a_document_starts_searching(self):
        doc = self.books.skill("abc")
        self.assertEqual((doc["state"], doc["round"], doc["cycle"]), ("searching", 0, 1))
        self.assertEqual(self.books.keys(), [], "nothing is kept until it is saved")

    def test_documents_and_side_documents_are_kept_apart(self):
        doc = self.books.skill("abc")
        self.books.save_skill(doc)
        self.books.save_side("scan", {"windows": {}})
        self.assertEqual(self.books.keys(), ["abc"])
        self.assertEqual(self.books.side("scan"), {"windows": {}})
        self.assertEqual(self.store.names("sourcing", "xy-110"), ["abc", "side-scan"])

    def test_the_days_model_calls_add_up_and_old_days_go(self):
        self.books.add_calls(3)
        self.books.add_calls(4)
        self.assertEqual(self.books.calls_today(), 7)
        index = self.books.index()
        index["calls"]["2020-01-01"] = 99
        self.books.save_index(index)
        self.books.add_calls(1)
        self.assertNotIn("2020-01-01", self.books.index()["calls"])
        self.assertEqual(self.books.calls_today(), 8)

    def test_due(self):
        doc = new_skill("k")
        self.assertTrue(due(doc))
        doc["next"] = iso(utcnow().replace(year=utcnow().year + 1))
        self.assertFalse(due(doc))
        doc["next"] = iso(utcnow().replace(year=utcnow().year - 1))
        self.assertTrue(due(doc))


class KeyTests(unittest.TestCase):
    def test_a_skill_key_is_the_one_the_build_document_always_used(self):
        from app.core import sha256
        self.assertEqual(skill_key("Plan", "Group", "Do the thing"),
                         sha256(f"{normalize('Plan')}|{normalize('Group')}|{normalize('Do the thing')}")[:16])
        self.assertEqual(skill_key("A", "B", "Text  here"), skill_key("a", "b", "text here"))
        self.assertEqual(parse_iso(iso()).tzinfo is not None, True)


if __name__ == "__main__":
    unittest.main()
