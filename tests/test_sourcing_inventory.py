import tempfile
import threading
import unittest
from unittest import mock
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core import Store
from app.sourcing.inventory import Inventory
from app.sourcing.net import SourcingError
from app.sourcing.state import Lease, LeaseLost


def at(hour: int = 0) -> datetime:
    return datetime(2026, 10, 15, hour, 0, 0, tzinfo=timezone.utc)


def toc_meta(rel: str) -> str:
    return f'<html><head><meta name="toc_rel" content="{rel}"></head><body></body></html>'


class FakeNet:
    def __init__(self):
        self.heads = {}
        self.jsons = {}
        self.head_calls = Counter()
        self.json_calls = Counter()
        self.docs_only = {}
        self.lock = threading.Lock()

    def get_page_head(self, url: str, limit: int = 150_000):
        with self.lock:
            self.head_calls[url] += 1
        step = self.heads[url]
        if isinstance(step, Exception):
            raise step
        return step

    def get_json(self, url: str, docs: bool = False):
        with self.lock:
            self.json_calls[url] += 1
            self.docs_only[url] = docs
        step = self.jsons[url]
        if isinstance(step, list):
            step = step.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class InventoryTests(unittest.TestCase):
    def make_inventory(self, net: FakeNet, now):
        store = Store(Path(tempfile.mkdtemp(prefix="dojo-sourcing-inventory-")))
        return Inventory(store, net, None, "dp-300", now=now), store

    def test_seed_loads_relative_toc_and_persists_entries(self):
        now = [at()]
        clock = lambda: now[0]
        net = FakeNet()
        seed1 = "https://learn.microsoft.com/en-us/sql/topic-one"
        seed2 = "https://learn.microsoft.com/en-us/sql/topic-two"
        toc = "https://learn.microsoft.com/en-us/toc.json"
        net.heads[seed1] = (seed1, toc_meta("../toc.json"))
        net.jsons[toc] = {
            "items": [
                {"toc_title": "SQL", "href": "/en-us/sql/overview", "children": [
                    {"toc_title": "Deploy hybrid SQL Server solutions", "href": "/en-us/sql/deploy-hybrid?view=azuresql#part"},
                    {"toc_title": "Older", "href": "/en-us/previous-versions/sql/old"},
                ]},
                {"toc_title": "Jobs", "children": [
                    {"toc_title": "Troubleshoot jobs", "href": "/en-us/sql/troubleshoot-jobs"},
                ]},
            ]
        }
        inv, store = self.make_inventory(net, clock)
        self.assertEqual(inv.seed([seed1, seed2, "https://learn.microsoft.com/en-us/previous-versions/sql/nope"]), 1)
        self.assertEqual(net.head_calls[seed1], 1)
        self.assertEqual(net.head_calls[seed2], 0)
        self.assertEqual(inv.entries(), [
            {"url": "https://learn.microsoft.com/en-us/sql/overview", "title": "SQL", "trail": ""},
            {"url": "https://learn.microsoft.com/en-us/sql/deploy-hybrid", "title": "Deploy hybrid SQL Server solutions", "trail": "SQL"},
            {"url": "https://learn.microsoft.com/en-us/sql/troubleshoot-jobs", "title": "Troubleshoot jobs", "trail": "Jobs"},
        ])
        again = Inventory(store, net, None, "dp-300", now=clock)
        self.assertEqual(again.entries(), inv.entries())
        self.assertEqual(again.docsets(), [{"toc": toc, "pages": 3, "fetched": inv.docsets()[0]["fetched"], "ok": True}])

    def test_refresh_rules_and_failed_toc_retry(self):
        now = [at()]
        clock = lambda: now[0]
        net = FakeNet()
        seed = "https://learn.microsoft.com/en-us/sql/topic"
        toc = "https://learn.microsoft.com/en-us/sql/toc.json"
        net.heads[seed] = (seed, toc_meta("toc.json"))
        net.jsons[toc] = [
            {"items": [{"toc_title": "One", "href": "/en-us/sql/one"}]},
            SourcingError("down"),
            {"items": [{"toc_title": "Two", "href": "/en-us/sql/two"}]},
        ]
        inv, _ = self.make_inventory(net, clock)
        inv.seed([seed])
        self.assertEqual(net.json_calls[toc], 1)
        inv.seed([seed])
        self.assertEqual(net.json_calls[toc], 1)
        now[0] += timedelta(hours=25)
        inv.seed([seed])
        self.assertEqual(net.json_calls[toc], 2)
        self.assertFalse(inv.docsets()[0]["ok"])
        self.assertEqual(inv.entries()[0]["url"], "https://learn.microsoft.com/en-us/sql/one")
        now[0] += timedelta(minutes=30)
        inv.seed([seed])
        self.assertEqual(net.json_calls[toc], 2)
        now[0] += timedelta(minutes=31)
        inv.seed([seed])
        self.assertEqual(net.json_calls[toc], 3)
        self.assertTrue(inv.docsets()[0]["ok"])
        self.assertEqual(inv.entries()[0]["url"], "https://learn.microsoft.com/en-us/sql/two")

    def test_caps_windows_and_bm25(self):
        now = lambda: at()
        net = FakeNet()
        inv, _ = self.make_inventory(net, now)
        seeds = [
            "https://learn.microsoft.com/en-us/sql/a/topic",
            "https://learn.microsoft.com/en-us/sql/b/topic",
            "https://learn.microsoft.com/en-us/sql/c/topic",
        ]
        toc1 = "https://learn.microsoft.com/en-us/sql/a/toc.json"
        toc2 = "https://learn.microsoft.com/en-us/sql/b/toc.json"
        toc3 = "https://learn.microsoft.com/en-us/sql/c/toc.json"
        net.heads[seeds[0]] = (seeds[0], toc_meta("./toc.json"))
        net.heads[seeds[1]] = (seeds[1], toc_meta("./toc.json"))
        net.heads[seeds[2]] = (seeds[2], toc_meta("./toc.json"))
        net.jsons[toc1] = {"items": [{"toc_title": f"A {n}", "href": f"/en-us/sql/a-{n}"} for n in range(20_100)]}
        net.jsons[toc2] = {"items": [{"toc_title": f"B {n}", "href": f"/en-us/sql/b-{n}"} for n in range(21_000)]}
        net.jsons[toc3] = {"items": [{"toc_title": f"C {n}", "href": f"/en-us/sql/c-{n}"} for n in range(21_000)]}
        self.assertEqual(inv.seed(seeds, max_new=2), 2)
        self.assertEqual(inv.size(), 40_000)
        self.assertEqual([row["pages"] for row in inv.docsets()], [20_000, 20_000])
        self.assertEqual(net.json_calls[toc3], 0)
        self.assertEqual(inv.seed([seeds[2]], max_new=1), 1)
        self.assertEqual(inv.size(), 60_000)
        win = list(inv.windows(size=15_000))
        flat = [row["n"] for chunk in win for row in chunk]
        self.assertEqual(flat[0], 1)
        self.assertEqual(flat[-1], len(flat))
        self.assertEqual(len(flat), len(set(flat)))

        small = [
            {"url": "https://learn.microsoft.com/en-us/sql/deploy-hybrid", "title": "Deploy hybrid SQL Server solutions", "trail": "SQL > Deploy"},
            {"url": "https://learn.microsoft.com/en-us/sql/disaster-recovery-drills", "title": "Disaster recovery drills", "trail": "SQL > High availability"},
            {"url": "https://learn.microsoft.com/en-us/sql/troubleshoot-job", "title": "Troubleshoot SQL Server Agent jobs", "trail": "SQL > Agent"},
        ] + [{"url": f"https://learn.microsoft.com/en-us/sql/noise-{n}", "title": f"Noise {n}", "trail": "Other"} for n in range(27)]
        store2 = Store(Path(tempfile.mkdtemp(prefix="dojo-sourcing-bm25-small-")))
        toc = "https://learn.microsoft.com/en-us/sql/toc.json"
        key = "dp-300-" + __import__("hashlib").sha256(toc.encode("utf-8")).hexdigest()[:12]
        store2.write("inventory", "dp-300", value={"docsets": [{"toc": toc, "pages": len(small), "fetched": "2026-10-15T00:00:00+00:00", "ok": True}],
                                                    "folders": {}})
        store2.write("inventory", key, value={"toc": toc, "entries": small})
        inv2 = Inventory(store2, net, None, "dp-300", now=now)
        self.assertEqual(inv2.search(["deploy hybrid SQL Server solutions"], top=1)[0]["url"],
                         "https://learn.microsoft.com/en-us/sql/deploy-hybrid")
        self.assertEqual(inv2.search(["plan a testing procedure for HA DR solution"], top=1)[0]["url"],
                         "https://learn.microsoft.com/en-us/sql/disaster-recovery-drills")
        self.assertEqual(inv2.search(["troubleshooting SQL Server Agent jobs"], top=1)[0]["url"],
                         "https://learn.microsoft.com/en-us/sql/troubleshoot-job")
        # Several pages come back, best first, each once, however many phrases found it.
        many = inv2.search(["SQL Server Agent jobs", "troubleshoot SQL Server jobs", "SQL Server"], top=5)
        urls = [h["url"] for h in many]
        self.assertGreater(len(urls), 1)
        self.assertEqual(urls[0], "https://learn.microsoft.com/en-us/sql/troubleshoot-job")
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual({"url", "title", "summary"}, set(many[0]))

    def test_a_table_of_contents_is_read_as_documentation_only(self):
        net = FakeNet()
        seed = "https://learn.microsoft.com/en-us/sql/topic"
        toc = "https://learn.microsoft.com/en-us/sql/toc.json"
        net.heads[seed] = (seed, toc_meta("toc.json"))
        net.jsons[toc] = {"items": [{"toc_title": "One", "href": "/en-us/sql/one"}]}
        inv, _ = self.make_inventory(net, lambda: at())
        inv.seed([seed])
        self.assertIs(net.docs_only[toc], True, "the read refuses every redirect that leaves the documentation (EG-39)")

    def test_a_table_of_contents_outside_the_documentation_is_never_asked_for(self):
        net = FakeNet()
        seed = "https://learn.microsoft.com/en-us/sql/topic"
        net.heads[seed] = (seed, toc_meta("/en-us/training/paths/toc.json"))
        inv, _ = self.make_inventory(net, lambda: at())
        inv.seed([seed])
        self.assertEqual(sum(net.json_calls.values()), 0)
        self.assertEqual(inv.size(), 0)

    def seeded(self):
        net = FakeNet()
        seed = "https://learn.microsoft.com/en-us/sql/topic"
        toc = "https://learn.microsoft.com/en-us/sql/toc.json"
        net.heads[seed] = (seed, toc_meta("toc.json"))
        net.jsons[toc] = {"items": [{"toc_title": "One", "href": "/en-us/sql/one"}]}
        store = Store(Path(tempfile.mkdtemp(prefix="dojo-sourcing-inventory-")))
        lease = Lease(store, "dp-300", owner="a", ttl=600, clock=lambda: 1000.0, pause=lambda s: None)
        self.assertTrue(lease.acquire())
        return Inventory(store, net, None, "dp-300", now=lambda: at(), lease=lease), store, lease, seed

    def test_nothing_is_written_for_an_exam_that_was_removed(self):
        inv, store, lease, seed = self.seeded()
        lease.revoke()
        with self.assertRaises(LeaseLost):
            inv.seed([seed])
        self.assertEqual(store.names("inventory"), [])

    def test_a_write_that_lands_while_the_exam_is_removed_takes_itself_back(self):
        inv, store, lease, seed = self.seeded()
        real = store.write

        def removed_meanwhile(*args, **kwargs):
            real(*args, **kwargs)
            lease.revoke()   # the removal ran between the check and the write

        with mock.patch.object(store, "write", removed_meanwhile), self.assertRaises(LeaseLost):
            inv.seed([seed])
        self.assertEqual(store.names("inventory"), [], "what was just written goes, so a removed exam leaves nothing behind")

    def test_thread_safe_seed_reads_one_toc_once(self):
        now = [at()]
        net = FakeNet()
        seed = "https://learn.microsoft.com/en-us/sql/topic"
        toc = "https://learn.microsoft.com/en-us/sql/toc.json"
        net.heads[seed] = (seed, toc_meta("toc.json"))
        net.jsons[toc] = {"items": [{"toc_title": "One", "href": "/en-us/sql/one"}]}
        inv, _ = self.make_inventory(net, lambda: now[0])
        threads = [threading.Thread(target=inv.seed, args=([seed],)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(net.head_calls[seed], 1)
        self.assertEqual(net.json_calls[toc], 1)
