"""The data in Azure Blob Storage (docs/adr/0030-data-in-blob-storage.md): what the Store puts into storage, what an
empty folder gets back, and what is never lost on the way. Storage is a small stand-in for the Blob service
(FakeBlobService), reached through the real BlobClient over httpx's mock transport."""
import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote
from xml.sax.saxutils import escape

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import avatar as avatar_mod  # noqa: E402
from app.avatar import StubAvatar  # noqa: E402
from app.blobstore import (JOURNAL, REPORT, RESTORING, BlobClient, BlobError, classify, md5_b64, valid)  # noqa: E402
from app.core import Store, load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

KEY = "l" + "0" * 31
AUDIO = "a" + "1" * 39 + ".mp3"
FILM = "v" + "2" * 39
TOKEN = "test-token"


class FakeBlobService:
    """The calls BlobClient makes, answered from a dict. `down` refuses the connection, `flaky` answers 503 that many
    times, `refuse` answers every request with that status."""

    def __init__(self, page: int = 2):
        self.blobs: dict[str, dict] = {}
        self.page = page
        self.down = False
        self.flaky = 0
        self.refuse = 0
        self.requests: list[tuple[str, str]] = []
        self.connect_timeouts: list[float | None] = []

    def client(self, **kw) -> BlobClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handle))
        return BlobClient("acct", "dojo-data", kw.pop("token", lambda: TOKEN), http=http, endpoint="https://blob.test",
                          sleep=lambda s: None, **kw)

    def put(self, name: str, data: bytes, lm: int = 0) -> None:
        self.blobs[name] = {"data": data, "md5": md5_b64(data), "lm": str(lm), "at": "Wed, 18 Nov 2026 12:00:00 GMT"}

    def data(self, name: str) -> bytes:
        return self.blobs[name]["data"]

    def names(self) -> list[str]:
        return sorted(self.blobs)

    def puts(self) -> int:
        return sum(1 for method, _ in self.requests if method == "PUT")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        self.connect_timeouts.append(request.extensions.get("timeout", {}).get("connect"))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.flaky:
            self.flaky -= 1
            return httpx.Response(503, headers={"x-ms-error-code": "ServerBusy"})
        if self.refuse:
            return httpx.Response(self.refuse, headers={"x-ms-error-code": "AuthorizationPermissionMismatch"})
        assert request.headers["authorization"] == f"Bearer {TOKEN}" and request.headers["x-ms-version"]
        path = request.url.path
        if path == "/dojo-data":
            return self.listing(request)
        name = unquote(path[len("/dojo-data/"):])
        if request.method == "PUT":
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            if request.headers.get("content-md5") != md5_b64(request.content):
                return httpx.Response(400, headers={"x-ms-error-code": "Md5Mismatch"})
            self.put(name, request.content, int(request.headers.get("x-ms-meta-lm", "0")))
            return httpx.Response(201)
        if request.method == "GET":
            blob = self.blobs.get(name)
            if blob is None:
                return httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
            return httpx.Response(200, content=blob["data"], headers={"Content-MD5": blob.get("serve_md5", blob["md5"])})
        if request.method == "DELETE":
            return httpx.Response(202 if self.blobs.pop(name, None) is not None else 404)
        return httpx.Response(405)

    def listing(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        assert q["restype"] == "container" and q["comp"] == "list"
        names = [n for n in self.names() if n.startswith(q.get("prefix", "")) and n > q.get("marker", "")]
        page, rest = names[: self.page], names[self.page:]
        rows = "".join(
            f"<Blob><Name>{escape(n)}</Name><Properties><Last-Modified>{self.blobs[n]['at']}</Last-Modified>"
            f"<Content-Length>{len(self.blobs[n]['data'])}</Content-Length><Content-MD5>{self.blobs[n]['md5']}</Content-MD5></Properties>"
            f"<Metadata><lm>{self.blobs[n]['lm']}</lm></Metadata></Blob>" for n in page)
        marker = f"<NextMarker>{escape(page[-1])}</NextMarker>" if rest else "<NextMarker />"
        return httpx.Response(200, content=f'<?xml version="1.0"?><EnumerationResults><Blobs>{rows}</Blobs>{marker}</EnumerationResults>'.encode())


def connect(tmp: str, service: FakeBlobService, **options) -> tuple[Store, object]:
    store = Store(Path(tmp))
    mirror = store.attach(service.client(), thread=False, connect_attempts=1, connect_pause_s=0, workers=2, **options)
    return store, mirror


class Base(unittest.TestCase):
    def folder(self) -> str:
        tmp = tempfile.mkdtemp(prefix="dojo-blob-")
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    def lay(self, tmp: str, files: dict[str, bytes]) -> None:
        for rel, data in files.items():
            path = Path(tmp, *rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


class NamesTests(unittest.TestCase):
    def test_what_is_put_and_what_waits_for_use(self):
        self.assertEqual(classify("content/lessons/l-1.json"), "hot")
        self.assertEqual(classify(f"learners/{KEY}/events.jsonl"), "hot")
        self.assertEqual(classify("media/" + AUDIO), "cold")
        self.assertEqual(classify("media/" + FILM + ".webm"), "cold")
        self.assertEqual(classify(f"learners/{KEY}/media/" + AUDIO), "cold")
        self.assertEqual(classify("media/" + FILM + ".json"), "hot", "a film's timings are small and always on the disk")

    def test_leases_temporary_files_and_joined_episodes_are_not_kept(self):
        for rel in ("sourcing/gh-300.lease", "sourcing/gh-300.lease.ab12cd34.tmp", "docs/.a.json.1234.tmp", ".blob-report.json",
                    "podcast/" + "e" * 40 + ".mp3", "media/a.mp3.part"):
            with self.subTest(rel=rel):
                self.assertEqual(classify(rel), "skip")

    def test_a_blob_name_cannot_climb_out_of_the_folder(self):
        self.assertTrue(valid("content/lessons/l-1.json"))
        for rel in ("", "../x.json", "a/../x.json", "/x.json", "a//b.json", ".hidden", "a/b c.json", "a\\b.json"):
            with self.subTest(rel=rel):
                self.assertFalse(valid(rel))


class ClientTests(Base):
    def test_the_listing_follows_its_markers(self):
        svc = FakeBlobService(page=2)
        for n in range(5):
            svc.put(f"docs/d{n}.json", b"x" * (n + 1), lm=n)
        got = svc.client().list()
        self.assertEqual(sorted(got), svc.names())
        self.assertEqual((got["docs/d3.json"].size, got["docs/d3.json"].lm, got["docs/d3.json"].md5), (4, 3, md5_b64(b"xxxx")))
        self.assertEqual(sum(1 for m, _ in svc.requests if m == "GET"), 3, "five blobs, two to a page")

    def test_a_put_carries_the_sum_the_kind_and_the_time_of_the_file(self):
        svc = FakeBlobService()
        svc.client().put("media/" + AUDIO, b"audio", md5_b64(b"audio"), lm=42)
        self.assertEqual((svc.data("media/" + AUDIO), svc.blobs["media/" + AUDIO]["lm"]), (b"audio", "42"))

    def test_a_wrong_sum_is_refused_and_nothing_is_kept(self):
        svc = FakeBlobService()
        with self.assertRaises(BlobError) as caught:
            svc.client().put("docs/a.json", b"body", md5_b64(b"other body"))
        self.assertEqual((caught.exception.status, caught.exception.code), (400, "Md5Mismatch"))
        self.assertEqual(svc.names(), [])

    def test_a_busy_service_is_asked_again_and_a_refusal_is_not(self):
        svc = FakeBlobService()
        svc.flaky = 2
        svc.client().put("docs/a.json", b"1", md5_b64(b"1"))
        self.assertEqual((svc.names(), svc.puts()), (["docs/a.json"], 3))
        svc.refuse = 403
        with self.assertRaises(BlobError) as caught:
            svc.client().put("docs/b.json", b"2", md5_b64(b"2"))
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(svc.puts(), 4, "one more try would not make a missing role appear")

    def test_a_download_is_checked_and_arrives_whole_or_not_at_all(self):
        svc = FakeBlobService()
        svc.put("media/" + AUDIO, b"audio bytes")
        tmp = Path(self.folder())
        svc.client().get("media/" + AUDIO, tmp / "x.mp3")
        self.assertEqual((tmp / "x.mp3").read_bytes(), b"audio bytes")
        svc.blobs["media/" + AUDIO]["serve_md5"] = md5_b64(b"something else")
        with self.assertRaises(BlobError):
            svc.client().get("media/" + AUDIO, tmp / "y.mp3")
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["x.mp3"], "no half file, no leftovers")
        with self.assertRaises(BlobError) as caught:
            svc.client().get("media/none.mp3", tmp / "z.mp3")
        self.assertEqual(caught.exception.status, 404)

    def test_deleting_what_is_not_there_is_fine(self):
        svc = FakeBlobService()
        svc.put("docs/a.json", b"1")
        client = svc.client()
        client.delete("docs/a.json")
        client.delete("docs/a.json")
        self.assertEqual(svc.names(), [])

    def test_a_fast_call_is_one_try_with_short_waits(self):
        svc = FakeBlobService()
        svc.flaky = 1
        with self.assertRaises(BlobError):
            svc.client().put("docs/a.json", b"1", md5_b64(b"1"), fast=True)
        self.assertEqual((svc.puts(), svc.connect_timeouts[-1]), (1, 3.0), "a second try would hold the Store's lock longer")
        svc.client().put("docs/a.json", b"1", md5_b64(b"1"), fast=True)
        self.assertEqual(svc.names(), ["docs/a.json"])
        svc.client().delete("docs/a.json", fast=True)
        self.assertEqual(svc.names(), [])

    def test_a_credential_that_fails_is_storage_that_cannot_be_reached(self):
        def broken() -> str:
            raise RuntimeError("no managed identity here")
        with self.assertRaises(BlobError) as caught:
            FakeBlobService().client(token=broken).list()
        self.assertEqual(caught.exception.status, 401)

    def test_whatever_the_http_layer_raises_is_a_blob_error(self):
        def undecodable(request):
            raise httpx.DecodingError("not gzip", request=request)
        client = BlobClient("acct", "dojo-data", lambda: TOKEN, http=httpx.Client(transport=httpx.MockTransport(undecodable)),
                            endpoint="https://blob.test", sleep=lambda s: None)
        with self.assertRaises(BlobError):
            client.put("docs/a.json", b"1", md5_b64(b"1"))


class WriteThroughTests(Base):
    def test_every_kind_of_write_reaches_storage(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, _ = connect(tmp, svc)
        store.write("content", "lessons", "l-1", value={"a": "é"})
        store.write_bytes("media", AUDIO, data=b"audio")
        store.append_line("learners", KEY, "quality.jsonl", value={"n": 1})
        store.append_event(KEY, "viewed", {"x": 1})
        store.append_event(KEY, "viewed", {"x": 2})
        self.assertEqual(svc.data("content/lessons/l-1.json"), store.path("content", "lessons", "l-1.json").read_bytes())
        self.assertEqual(svc.data("media/" + AUDIO), b"audio")
        self.assertEqual(svc.data(f"learners/{KEY}/events.jsonl"), store.path("learners", KEY, "events.jsonl").read_bytes())
        self.assertEqual(len(svc.data(f"learners/{KEY}/events.jsonl").splitlines()), 2)
        self.assertEqual(svc.blobs["content/lessons/l-1.json"]["lm"], str(store.path("content", "lessons", "l-1.json").stat().st_mtime_ns))
        self.assertTrue(store.verify_chain(KEY)["ok"])

    def test_nothing_that_means_something_on_one_machine_only_is_put(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, _ = connect(tmp, svc)
        store.write_bytes("podcast", "e" * 40 + ".mp3", data=b"joined")
        store.path("sourcing").mkdir(parents=True)
        store.path("sourcing", "gh-300.lease").write_text("{}")
        store.publish("sourcing", "gh-300.lease")
        self.assertEqual(svc.names(), [])

    def test_a_write_for_a_deleted_learner_reaches_neither_the_folder_nor_storage(self):
        from app.core import Abandoned
        svc, tmp = FakeBlobService(), self.folder()
        store, _ = connect(tmp, svc)
        store.tombstone(KEY)
        with self.assertRaises(Abandoned):
            store.write("learners", KEY, "x", value=1)
        self.assertEqual(svc.names(), [])

    def test_deletes_reach_storage_and_leave_nothing_owed(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, _ = connect(tmp, svc)
        store.write("docs", "a", value=1)
        store.write_bytes("media", AUDIO, data=b"x")
        store.write("learners", KEY, "x", value=1)
        store.append_event(KEY, "viewed", {})
        store.delete("docs", "a")
        store.delete_file("media", AUDIO)
        self.assertEqual(svc.names(), [f"learners/{KEY}/events.jsonl", f"learners/{KEY}/x.json"])
        store.remove_tree("learners", KEY)
        self.assertEqual(svc.names(), [])
        self.assertEqual(json.loads(Path(tmp, JOURNAL).read_text("utf-8")), {"names": {}})

    def test_a_file_deleted_while_it_was_being_put_is_not_kept(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, mirror = connect(tmp, svc)
        put = mirror.client.put

        def put_then_lose(name, data, md5, lm=0, fast=False):
            put(name, data, md5, lm, fast)
            store.path("media", AUDIO).unlink()   # deleted by someone else, between the put and its bookkeeping

        mirror.client.put = put_then_lose
        store.write_bytes("media", AUDIO, data=b"x")
        self.assertEqual(svc.names(), [])
        self.assertFalse(store.exists("media", AUDIO))
        self.assertEqual(mirror.status()["deleting"], 0)

    def test_a_delete_storage_could_not_do_is_done_later_and_never_undone(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, mirror = connect(tmp, svc)
        store.write("learners", KEY, "x", value=1)
        store.write("docs", "keep", value=1)
        svc.down = True
        store.remove_tree("learners", KEY)
        self.assertFalse(store.path("learners", KEY).exists())
        self.assertIn(f"learners/{KEY}/x.json", svc.blobs, "storage did not get the delete")
        self.assertIn(f"learners/{KEY}/x.json", json.loads(Path(tmp, JOURNAL).read_text("utf-8"))["names"], "but it is written down")
        svc.down = False
        mirror.sync_once()
        self.assertEqual(svc.names(), ["docs/keep.json"])
        self.assertEqual(json.loads(Path(tmp, JOURNAL).read_text("utf-8")), {"names": {}})

    def test_a_restore_does_not_bring_back_what_was_deleted(self):
        svc, tmp = FakeBlobService(), self.folder()
        svc.put(f"learners/{KEY}/x.json", b"1")
        svc.put("docs/keep.json", b"2")
        Path(tmp, JOURNAL).write_text(json.dumps({"names": {f"learners/{KEY}/x.json": "2026-11-18T12:00:00+00:00"}}), "utf-8")
        store, _ = connect(tmp, svc)
        self.assertEqual(store.read("docs", "keep"), 2)
        self.assertFalse(store.path("learners", KEY, "x.json").exists())
        self.assertEqual(svc.names(), ["docs/keep.json"], "and storage did the delete it owed")

    def test_a_file_written_again_after_a_delete_is_not_deleted_by_the_old_one(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, mirror = connect(tmp, svc)
        store.write("docs", "a", value=1)
        svc.down = True
        store.delete("docs", "a")
        store.write("docs", "a", value=2)   # storage is down: this waits too
        svc.down = False
        mirror.sync_once()
        self.assertEqual(json.loads(svc.data("docs/a.json")), 2)


class RestoreTests(Base):
    def fill(self, svc: FakeBlobService) -> Store:
        store, _ = connect(self.folder(), svc)
        store.write("content", "lessons", "l-1", value={"title": "one"})
        store.write("content", "lessons", "l-2", value={"title": "two"})
        store.write_bytes("media", AUDIO, data=b"audio")
        store.write_bytes("media", FILM + ".webm", data=b"film" * 100)
        store.write("media", FILM, value=[{"start": 0, "end": 1}])
        store.append_event(KEY, "viewed", {"x": 1})
        return store

    def test_an_empty_folder_is_filled_with_the_small_files_and_the_audio_and_video_wait(self):
        svc = FakeBlobService()
        first = self.fill(svc)
        tmp = self.folder()
        store, _ = connect(tmp, svc)
        self.assertEqual(store.read("content", "lessons", "l-2"), {"title": "two"})
        self.assertEqual(store.events(KEY), first.events(KEY))
        self.assertTrue(store.verify_chain(KEY)["ok"])
        self.assertEqual(store.read("media", FILM), [{"start": 0, "end": 1}])
        self.assertFalse(store.path("media", AUDIO).exists(), "audio waits in storage")
        self.assertFalse(Path(tmp, RESTORING).exists(), "a finished restore leaves no marker")
        self.assertTrue(store.exists("media", AUDIO) and store.exists("media", FILM + ".webm"))
        self.assertEqual(store.listing("media"), {AUDIO, FILM + ".webm", FILM + ".json"})
        self.assertEqual(sorted((n, s) for n, s, _ in store.file_stats("media", suffix=".webm")), [(FILM + ".webm", 400)])
        self.assertEqual(store.file_stats("media", suffix=".webm", cached_only=True), [])

    def test_audio_and_video_come_onto_the_disk_when_used(self):
        svc = FakeBlobService()
        self.fill(svc)
        store, _ = connect(self.folder(), svc)
        self.assertEqual(store.read_bytes("media", AUDIO), b"audio")
        self.assertTrue(store.path("media", AUDIO).is_file())
        path = store.fetch("media", FILM + ".webm")
        self.assertEqual(path.read_bytes(), b"film" * 100)
        self.assertIsNone(store.fetch("media", "a" + "9" * 39 + ".mp3"))
        self.assertIsNone(store.read_bytes("media", "a" + "9" * 39 + ".mp3"))

    def test_storage_that_is_down_is_an_error_for_what_is_only_there_not_an_empty_answer(self):
        svc = FakeBlobService()
        self.fill(svc)
        store, _ = connect(self.folder(), svc)
        svc.down = True
        with self.assertRaises(BlobError):
            store.fetch("media", AUDIO)
        with self.assertRaises(BlobError):
            store.read_bytes("media", AUDIO)

    def test_an_empty_folder_and_no_storage_is_no_start_at_all(self):
        svc = FakeBlobService()
        svc.down = True
        with self.assertRaises(BlobError):
            connect(self.folder(), svc)

    def test_a_restore_that_stopped_halfway_is_finished_or_the_start_refused(self):
        svc = FakeBlobService()
        self.fill(svc)
        tmp = self.folder()
        self.lay(tmp, {"content/lessons/l-1.json": b'{"title": "one"}'})   # what a first, interrupted restore got to
        Path(tmp, RESTORING).write_text("not finished")
        svc.down = True
        with self.assertRaises(BlobError):
            connect(tmp, svc)
        svc.down = False
        store, _ = connect(tmp, svc)
        self.assertEqual(store.read("content", "lessons", "l-2"), {"title": "two"})
        self.assertTrue(store.verify_chain(KEY)["ok"])
        self.assertFalse(Path(tmp, RESTORING).exists())

    def test_a_file_that_cannot_be_restored_stops_the_start(self):
        svc = FakeBlobService()
        self.fill(svc)
        svc.blobs["content/lessons/l-2.json"]["serve_md5"] = md5_b64(b"not this")
        with self.assertRaises(BlobError):
            connect(self.folder(), svc)

    def test_a_folder_with_data_is_never_overwritten_by_storage(self):
        svc, tmp = FakeBlobService(), self.folder()
        svc.put("docs/a.json", b'{"v": "storage"}')
        svc.put("docs/c.json", b'{"v": "only in storage"}')
        self.lay(tmp, {"docs/a.json": b'{"v": "disk"}', "docs/b.json": b'{"v": "disk too"}'})
        store, mirror = connect(tmp, svc)
        self.assertEqual(store.read("docs", "a"), {"v": "disk"})
        self.assertFalse(store.path("docs", "c.json").exists())
        mirror.sync_once()
        self.assertEqual(svc.data("docs/a.json"), b'{"v": "disk"}')
        self.assertEqual(svc.data("docs/b.json"), b'{"v": "disk too"}')


class MigrationTests(Base):
    def tree(self) -> dict[str, bytes]:
        return {"docs/a.json": b'{"a": 1}', "content/lessons/x.json": b'{"x": 1}', "media/" + AUDIO: b"audio",
                "media/" + FILM + ".webm": b"film" * 50, "media/" + FILM + ".json": b"[]",
                "sourcing/gh-300.lease": b"{}", "podcast/" + "e" * 40 + ".mp3": b"joined"}

    def test_the_first_pass_puts_everything_and_checks_it(self):
        svc, tmp = FakeBlobService(), self.folder()
        self.lay(tmp, self.tree())
        store, mirror = connect(tmp, svc)
        self.assertEqual(mirror.status()["state"], "migrating")
        mirror.sync_once()
        self.assertEqual(svc.names(), sorted(["docs/a.json", "content/lessons/x.json", "media/" + AUDIO, "media/" + FILM + ".webm",
                                              "media/" + FILM + ".json"]))
        for name in svc.names():
            self.assertEqual(svc.data(name), Path(tmp, *name.split("/")).read_bytes())
        report = json.loads(Path(tmp, REPORT).read_text("utf-8"))
        self.assertEqual((report["objects"], report["audio_video_to_send"], report["waiting"]), (5, 0, 0))
        self.assertEqual(report["verification"], {"checked": 5, "missing_count": 0, "missing": [], "size_differs_count": 0,
                                                  "size_differs": [], "md5_differs_count": 0, "md5_differs": []})
        self.assertEqual(mirror.status()["state"], "ok")

    def test_a_second_pass_changes_nothing(self):
        svc, tmp = FakeBlobService(), self.folder()
        self.lay(tmp, self.tree())
        _, mirror = connect(tmp, svc)
        mirror.sync_once()
        puts = svc.puts()
        mirror.sync_once()
        self.assertEqual(svc.puts(), puts)

    def test_what_was_written_around_the_store_is_picked_up(self):
        svc, tmp = FakeBlobService(), self.folder()
        self.lay(tmp, self.tree())
        _, mirror = connect(tmp, svc)
        mirror.sync_once()
        self.lay(tmp, {"docs/a.json": b'{"a": 2}', "docs/new.json": b"{}"})
        mirror.sync_once()
        self.assertEqual(svc.data("docs/a.json"), b'{"a": 2}')
        self.assertIn("docs/new.json", svc.blobs)

    def test_a_pass_stopped_by_an_outage_goes_on_where_it_stopped(self):
        svc, tmp = FakeBlobService(), self.folder()
        files = {f"media/a{n:039x}.mp3": b"x" * (n + 1) for n in range(12)}
        self.lay(tmp, {**files, "docs/a.json": b"{}"})
        _, mirror = connect(tmp, svc)
        real, puts = svc.handle, []

        def drops_after_five_puts(request):
            if request.method == "PUT":
                puts.append(request.url.path)
                if len(puts) > 5:
                    svc.down = True
            return real(request)

        mirror.client._http = httpx.Client(transport=httpx.MockTransport(drops_after_five_puts))
        mirror.workers = 1
        mirror.sync_once()
        self.assertEqual(mirror.status()["state"], "degraded")
        self.assertEqual(len(svc.blobs), 5)
        svc.down = False
        mirror.client._http = httpx.Client(transport=httpx.MockTransport(svc.handle))
        mirror.sync_once()

    def test_a_size_that_differs_on_a_file_that_never_changes_is_reported_not_overwritten(self):
        svc, tmp = FakeBlobService(), self.folder()
        svc.put("media/" + AUDIO, b"the copy in storage")
        self.lay(tmp, {"media/" + AUDIO: b"short", "docs/a.json": b"{}"})
        _, mirror = connect(tmp, svc)
        with self.assertLogs("dojo.blob", "ERROR"):
            mirror.sync_once()
        self.assertEqual(svc.data("media/" + AUDIO), b"the copy in storage")
        self.assertEqual(Path(tmp, "media", AUDIO).read_bytes(), b"short")


class OutageTests(Base):
    def test_a_folder_with_data_starts_without_storage_and_catches_up(self):
        svc, tmp = FakeBlobService(), self.folder()
        self.lay(tmp, {"docs/a.json": b"{}"})
        svc.down = True
        with self.assertLogs("dojo.blob", "ERROR"):
            store, mirror = connect(tmp, svc)
        self.assertEqual(mirror.status()["state"], "degraded")
        store.write("docs", "b", value=1)
        self.assertEqual(mirror.status()["waiting"], 1)
        svc.down = False
        mirror.sync_once()
        self.assertEqual(svc.names(), ["docs/a.json", "docs/b.json"])
        self.assertEqual(mirror.status()["state"], "ok")

    def test_a_small_file_is_put_once_and_quickly_under_the_lock_and_audio_with_patience(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, mirror = connect(tmp, svc)
        svc.flaky = 1
        store.write("docs", "a", value=1)
        self.assertEqual((svc.puts(), svc.connect_timeouts[-1]), (1, 3.0), "one try, short waits: the Store's lock is held")
        self.assertEqual(mirror.status()["waiting"], 1)
        mirror.sync_once()
        self.assertIn("docs/a.json", svc.blobs, "the background thread puts what the write could not")
        svc.flaky = 2
        before = svc.puts()
        store.write_bytes("media", AUDIO, data=b"audio")
        self.assertEqual(svc.puts() - before, 3, "audio and video are written without the lock: the usual three tries")
        self.assertNotEqual(svc.connect_timeouts[-1], 3.0)
        self.assertEqual(svc.data("media/" + AUDIO), b"audio")

    def test_a_write_never_fails_because_of_anything_storage_does(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, mirror = connect(tmp, svc)

        def broken(*args, **kwargs):
            raise RuntimeError("a bug in the client")

        mirror.client.put = broken
        store.write("docs", "a", value=1)
        self.assertEqual(store.read("docs", "a"), 1)
        self.assertEqual(mirror.status()["waiting"], 1)

    def test_a_file_put_while_the_listing_ran_is_not_reported_as_missing(self):
        svc, tmp = FakeBlobService(), self.folder()
        self.lay(tmp, {"docs/a.json": b"{}"})
        store, mirror = connect(tmp, svc)
        listed, late = mirror.client.list, []

        def list_then_write(*args, **kwargs):
            snapshot = listed(*args, **kwargs)
            late.append(1)
            store.write("docs", f"late{len(late)}", value=len(late))   # put after the snapshot, before the listing is used
            return snapshot

        mirror.client.list = list_then_write
        mirror.sync_once()
        verification = json.loads(Path(tmp, REPORT).read_text("utf-8"))["verification"]
        self.assertEqual((verification["missing_count"], verification["size_differs_count"], verification["md5_differs_count"]), (0, 0, 0))

    def test_the_failed_write_does_not_fail_the_request_and_asking_pauses(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, mirror = connect(tmp, svc)
        svc.down = True
        store.write("docs", "a", value=1)
        store.write("docs", "b", value=1)
        asked = len(svc.requests)
        store.write("docs", "c", value=1)
        self.assertEqual(len(svc.requests), asked, "storage failed twice in a row: not asked again for a moment")
        self.assertEqual(store.read("docs", "c"), 1)
        self.assertEqual((mirror.status()["state"], mirror.status()["waiting"]), ("degraded", 3))
        svc.down = False
        mirror.sync_once()
        self.assertEqual(svc.names(), ["docs/a.json", "docs/b.json", "docs/c.json"])
        self.assertEqual(mirror.status()["state"], "ok")

    def test_a_refusal_of_the_role_is_degraded_too(self):
        svc, tmp = FakeBlobService(), self.folder()
        self.lay(tmp, {"docs/a.json": b"{}"})
        svc.refuse = 403
        with self.assertLogs("dojo.blob", "ERROR"):
            _, mirror = connect(tmp, svc)
        self.assertEqual(mirror.status()["state"], "degraded")
        self.assertIn("403", mirror.status()["last_error"])


class EvictionTests(Base):
    def test_only_what_storage_holds_may_leave_the_disk(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, mirror = connect(tmp, svc)
        svc.down = True
        store.write_bytes("media", AUDIO, data=b"only here")
        self.assertFalse(store.evict("media", AUDIO), "storage has no copy yet")
        self.assertTrue(store.path("media", AUDIO).is_file())
        svc.down = False
        mirror.sync_once()
        self.assertTrue(store.evict("media", AUDIO))
        self.assertFalse(store.path("media", AUDIO).exists())
        self.assertTrue(store.exists("media", AUDIO))
        self.assertEqual(store.read_bytes("media", AUDIO), b"only here")

    def test_without_storage_evicting_is_deleting_as_it_always_was(self):
        store = Store(Path(self.folder()))
        store.write_bytes("media", AUDIO, data=b"x")
        self.assertTrue(store.evict("media", AUDIO))
        self.assertFalse(store.exists("media", AUDIO))

    def test_the_video_budget_frees_the_disk_and_keeps_the_films(self):
        svc, tmp = FakeBlobService(), self.folder()
        store, _ = connect(tmp, svc)
        avatar = StubAvatar(dataclasses.replace(load_settings(), data_dir=Path(tmp)), None, store)
        names = ["v" + str(n) * 39 for n in range(3)]
        for age, name in zip((300, 200, 100), names):
            store.write_bytes("media", name + ".webm", data=bytes(400))
            store.write("media", name, value=[{"start": 0.0, "end": 1.0}])
            os.utime(store.path("media", name + ".webm"), (1_000_000 - age, 1_000_000 - age))
        with mock.patch.object(avatar_mod, "BUDGET_BYTES", 1000):
            removed = avatar.evict()
        self.assertEqual(removed, [names[0]])
        self.assertFalse(store.path("media", names[0] + ".webm").exists(), "the disk's copy went")
        self.assertIn(f"media/{names[0]}.webm", svc.blobs, "the film is still in storage")
        self.assertTrue(avatar.exists(names[0]), "and it still counts as filmed: nothing is filmed again, nothing is paid for twice")
        self.assertEqual(avatar.usage()["videos"], 3)
        self.assertEqual(len(avatar.videos(cached_only=True)), 2)


class AppTests(Base):
    def app(self, svc: FakeBlobService, tmp: str):
        conf = dataclasses.replace(load_settings(), data_dir=Path(tmp))
        web = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        return create_app(conf, http=web, blob=svc.client())

    def test_audio_that_is_only_in_storage_plays(self):
        svc, tmp = FakeBlobService(), self.folder()
        svc.put("media/" + AUDIO, b"ID3 narration")
        client = TestClient(self.app(svc, tmp))
        r = client.get(f"/api/media/{AUDIO[:-4]}")
        self.assertEqual((r.status_code, r.content, r.headers["content-type"]), (200, b"ID3 narration", "audio/mpeg"))
        self.assertTrue(Path(tmp, "media", AUDIO).is_file())
        self.assertEqual(client.get("/api/media/a" + "9" * 39).status_code, 404)

    def test_audio_that_storage_cannot_deliver_is_a_wait_not_a_missing_file(self):
        svc, tmp = FakeBlobService(), self.folder()
        svc.put("media/" + AUDIO, b"ID3 narration")
        app = self.app(svc, tmp)
        svc.down = True
        r = TestClient(app).get(f"/api/media/{AUDIO[:-4]}")
        self.assertEqual(r.status_code, 503)

    def test_healthz_names_the_storage_and_degrades_when_it_does_not_answer(self):
        svc, tmp = FakeBlobService(), self.folder()
        app = self.app(svc, tmp)
        app.state.selftest.result = {"state": "done", "checks": {"data": True}, "details": {}, "at": "", "build": "test"}
        client = TestClient(app)
        ok = client.get("/healthz").json()
        self.assertIn(ok["storage"], ("ok", "migrating"))
        self.assertEqual(ok["status"], "ok")
        svc.down = True
        app.state.dojo.store.write("docs", "a", value=1)
        app.state.dojo.store.write("docs", "b", value=1)
        down = client.get("/healthz").json()
        self.assertEqual((down["status"], down["storage"]), ("degraded", "degraded"))
        self.assertEqual(set(down), {"status", "build", "storage"}, "still nothing but the state")

    def test_without_storage_healthz_is_what_it_was(self):
        conf = dataclasses.replace(load_settings(), data_dir=Path(self.folder()), build="test")
        app = create_app(conf, http=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))))
        app.state.selftest.result = {"state": "done", "checks": {"data": True}, "details": {}, "at": "", "build": "test"}
        self.assertEqual(TestClient(app).get("/healthz").json(), {"status": "ok", "build": "test"})


class SettingsTests(unittest.TestCase):
    def test_the_account_is_read_only_in_azure_and_a_wrong_name_stops_the_start(self):
        env = {"WEBSITE_SITE_NAME": "dojo", "DOJO_BLOB_ACCOUNT": "stdojo0123456789", "DOJO_BLOB_CONTAINER": "dojo-data"}
        with mock.patch.dict(os.environ, env):
            conf = load_settings()
            self.assertEqual((conf.blob_account, conf.blob_container), ("stdojo0123456789", "dojo-data"))
        with mock.patch.dict(os.environ, {**env, "DOJO_BLOB_ACCOUNT": "Not_A_Name"}), self.assertRaises(ValueError):
            load_settings()
        with mock.patch.dict(os.environ, {"DOJO_LOCAL": "1", "DOJO_BLOB_ACCOUNT": "stdojo0123456789"}):
            os.environ.pop("WEBSITE_SITE_NAME", None)
            self.assertEqual(load_settings().blob_account, "", "a local run never reaches for Azure storage")
        with mock.patch.dict(os.environ):
            os.environ.pop("DOJO_BLOB_ACCOUNT", None)
            self.assertEqual(load_settings().blob_account, "")


if __name__ == "__main__":
    unittest.main()
