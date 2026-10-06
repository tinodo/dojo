"""The private podcast feed: joined episodes, the secret link, and the open /feed routes, which record nothing."""
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlparse

os.environ.setdefault("DOJO_LOCAL", "1")
os.environ.setdefault("DOJO_DATA_DIR", tempfile.mkdtemp(prefix="dojo-import-"))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import APP_DIR, Learner, Store, iso, learner_key, parse_iso, utcnow  # noqa: E402
from app.main import create_app  # noqa: E402
from app.podcast import EPISODE_KEY, AudioError, FeedLinks, episode_key, join_mp3  # noqa: E402
from tests.test_api import POST, principal, settings, web  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "atom": "http://www.w3.org/2005/Atom",
      "content": "http://purl.org/rss/1.0/modules/content/"}


def frame(fill: int = 0, header: str = "FFF3A4C4") -> bytes:
    """One MPEG-2 Layer III frame: 96 kbit/s, 24 kHz, mono, 288 bytes, 24 ms (the narration's encoding)."""
    head = bytes.fromhex(header)
    size = 72 * {"A4": 96, "84": 64}[header[4:6]] * 1000 // 24000
    return head + bytes([fill]) * (size - 4)


def id3v2(payload: bytes = b"TIT2 slide") -> bytes:
    n = len(payload)
    return b"ID3\x04\x00\x00" + bytes([(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127]) + payload


def info_frame() -> bytes:
    f = bytearray(frame(9))
    f[13:17] = b"Info"  # after the 4-byte header and 9 bytes of MPEG-2 mono side information
    return bytes(f)


def mp3(frames: int, fill: int = 1) -> bytes:
    return b"".join(frame((fill + i) % 250 + 1) for i in range(frames))


class JoinTests(unittest.TestCase):
    def test_frames_follow_each_other_and_tags_and_info_frames_are_dropped(self):
        a = id3v2() + info_frame() + mp3(3, fill=10) + b"TAG" + bytes(125)
        b = mp3(2, fill=50)
        audio, seconds = join_mp3([a, b])
        self.assertEqual(audio, mp3(3, fill=10) + mp3(2, fill=50))
        self.assertAlmostEqual(seconds, 5 * 0.024)
        self.assertEqual(len(audio) * 8 / 96000, seconds)

    def test_a_cut_off_last_frame_is_left_out(self):
        audio, seconds = join_mp3([mp3(2) + frame()[:100]])
        self.assertEqual((len(audio), seconds), (576, 0.048))

    def test_anything_that_is_not_one_constant_encoding_is_refused(self):
        stub = b"ID3" + bytes(1024)  # what the stub speech writes
        for parts in ([], [stub], [b"not audio at all"], [mp3(2) + frame(header="FFF384C4")],
                      [mp3(2), frame(header="FFF384C4") * 2], [id3v2()], [b"ID3\x04\x00\x00\x80\x00\x00\x00"]):
            with self.assertRaises(AudioError):
                join_mp3(parts)

    def test_new_narration_makes_a_new_file_name(self):
        a, b = "a" + "1" * 39, "a" + "2" * 39
        self.assertRegex(episode_key([a, b]), r"^e[0-9a-f]{39}$")
        self.assertNotEqual(episode_key([a, b]), episode_key([b, a]))
        self.assertNotEqual(episode_key([a]), episode_key([a, b]))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-link-")
        self.store = Store(Path(self.tmp))
        self.owner = Learner("t:o", learner_key("t:o"), "Owner")
        self.links = FeedLinks(self.store, self.owner)

    def files(self) -> bytes:
        return b"".join(p.read_bytes() for p in Path(self.tmp).rglob("*") if p.is_file())

    def test_the_token_is_long_random_and_only_its_hash_is_kept(self):
        token = self.links.create()
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")  # 32 random bytes
        self.assertNotIn(token.encode(), self.files())
        doc = self.store.read("learners", self.owner.key, "feed")
        self.assertEqual(set(doc), {"hash", "created"})
        self.assertEqual(doc["hash"], hashlib.sha256(b"dojo-feed:" + token.encode()).hexdigest())
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
        self.assertEqual(self.links.status(), {"active": False, "created": None})

    def test_wrong_tokens_are_refused_after_the_same_constant_time_comparison(self):
        token = self.links.create()
        wrong = ("A" if token[0] != "A" else "B") + token[1:]
        with mock.patch("app.podcast.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            for t in (wrong, "", "short", token + "x", token.upper() if token.upper() != token else token.lower(), "../" + token[3:]):
                self.assertIsNone(self.links.owner_for(t), t)
            self.links.delete()
            self.assertIsNone(self.links.owner_for(token))
        self.assertEqual(compare.call_count, 7)
        for call in compare.call_args_list:
            self.assertEqual([len(a) for a in call.args], [64, 64])

    def test_the_link_is_all_it_keeps(self):
        """Nothing about when or how the feed was used: what the feed listed is kept apart, under podcast/."""
        self.links.create()
        self.links.create(replace=True)
        self.assertEqual([p.name for p in Path(self.tmp).rglob("*") if p.is_file()], ["feed.json"])
        self.links.delete()
        self.assertEqual([p for p in Path(self.tmp).rglob("*") if p.is_file()], [])
        self.links.delete()
        self.assertFalse(self.links.active())


class FeedTests(unittest.TestCase):
    """Local mode, the owner signed in: lessons are made with the stub models, then given real-looking audio."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-feed-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.dojo, cls.me, cls.podcast = cls.app.state.dojo, cls.app.state.preparer.owner, cls.app.state.podcast
        cls.podcast.prune_after_s = 3600  # pruning is tested on its own (ReplacedEpisodeTests)
        cls.addClassCleanup(cls.podcast.close)
        cls.store = cls.dojo.store
        pkg = cls.dojo.packages.get("gh-300")
        cls.order = [s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]]
        make = lambda sid: cls.dojo.make_lesson(lambda step: None, cls.me, "gh-300", sid)["lesson"]  # noqa: E731
        s1, s2, s3, s4 = cls.order[:4]
        # Made out of study-guide order, and s1 twice: only the latest lesson of a skill is its episode.
        cls.third = make(s3)
        cls.old_first = make(s1)
        cls.first = make(s1)
        cls.half = make(s2)
        cls.stub = make(s4)
        # The stub model narrates every lesson alike; real narration differs, and so does each slide's audio.
        for day, lesson in enumerate((cls.third, cls.old_first, cls.first, cls.half, cls.stub), start=1):
            doc = cls.store.read("content", "lessons", lesson)
            doc["created"] = f"2026-03-0{day}T10:00:00+00:00"
            for i, slide in enumerate(doc["slides"]):
                slide["narration"] = [{"voice": "guide", "text": f"Slide {i + 1} of {lesson}."}]
            cls.store.write("content", "lessons", lesson, value=doc)
        cls.dojo._lesson_meta.clear()
        for lesson, fill in ((cls.third, 30), (cls.old_first, 10), (cls.first, 20), (cls.half, 40)):
            for i, m in enumerate(cls.dojo.slide_media(cls.dojo.lesson(lesson))):
                cls.store.write_bytes("media", m + ".mp3", data=id3v2() + mp3(40 + i, fill=fill + i))
        cls.store.delete_file("media", cls.dojo.slide_media(cls.dojo.lesson(cls.half))[-1] + ".mp3")
        for m in cls.dojo.slide_media(cls.dojo.lesson(cls.stub)):
            cls.store.write_bytes("media", m + ".mp3", data=b"ID3" + bytes(1024))
        cls.token = cls.c.post("/api/feed/link/new", headers=POST).json()["feeds"][0]["url"].split("/")[4]

    def feed(self, pid: str = "gh-300") -> bytes:
        r = self.c.get(f"/feed/{self.token}/{pid}.xml")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-type"], "application/rss+xml; charset=utf-8")
        self.assertEqual(r.headers["x-robots-tag"], "noindex, nofollow")
        self.assertTrue(r.headers["cache-control"].startswith("private"))
        return r.content

    def items(self) -> list[ET.Element]:
        return ET.fromstring(self.feed()).findall("channel/item")

    def local(self, url: str) -> str:
        return urlparse(url).path

    def test_the_feed_is_rss_2_with_what_podcast_apps_need(self):
        channel = ET.fromstring(self.feed()).find("channel")
        self.assertEqual(ET.fromstring(self.feed()).get("version"), "2.0")
        self.assertEqual(channel.findtext("title"), "Dojo · GH-300 GitHub Copilot")
        self.assertEqual(channel.findtext("link"), "http://testserver/")
        self.assertEqual(channel.findtext("language"), "en-us")
        self.assertEqual(channel.findtext("itunes:author", namespaces=NS), "Dojo")
        self.assertEqual(channel.findtext("itunes:explicit", namespaces=NS), "false")
        self.assertEqual(channel.findtext("itunes:type", namespaces=NS), "serial")
        self.assertEqual(channel.findtext("itunes:block", namespaces=NS), "Yes")
        self.assertEqual(channel.find("itunes:category", NS).get("text"), "Education")
        self.assertEqual(channel.find("itunes:image", NS).get("href"), f"http://testserver/feed/{self.token}/gh-300.png")
        self.assertEqual(channel.find("atom:link", NS).get("href"), f"http://testserver/feed/{self.token}/gh-300.xml")
        self.assertLessEqual(len(channel.findtext("description").encode()), 4000)
        parsedate_to_datetime(channel.findtext("lastBuildDate"))
        for item in channel.findall("item"):
            self.assertTrue(item.findtext("title").endswith(")"))
            self.assertEqual(item.find("guid").get("isPermaLink"), "false")
            self.assertRegex(item.findtext("guid"), r"^lesson-[0-9a-f]{20}$")
            self.assertEqual(item.find("enclosure").get("type"), "audio/mpeg")
            self.assertRegex(item.findtext("itunes:duration", namespaces=NS), r"^[1-9][0-9]*$")
            self.assertEqual(item.findtext("itunes:episodeType", namespaces=NS), "full")
            self.assertEqual(parsedate_to_datetime(item.findtext("pubDate")).tzname(), "UTC")
            self.assertLessEqual(len(item.findtext("description")), 10000)
            self.assertEqual(item.findtext("description"), item.findtext("content:encoded", namespaces=NS))

    def test_one_episode_per_skill_in_study_guide_order_from_its_latest_fully_narrated_lesson(self):
        items = self.items()
        self.assertEqual([i.findtext("guid") for i in items], [self.first, self.third])
        self.assertEqual([i.findtext("itunes:episode", namespaces=NS) for i in items], ["1", "3"])
        skill = self.dojo.packages.get("gh-300")["skills"][self.order[0]]
        self.assertEqual(items[0].findtext("title"), f"{skill['text']} ({self.order[0]})")
        self.assertEqual(parsedate_to_datetime(items[0].findtext("pubDate")), parse_iso(self.dojo.lesson(self.first)["created"]))
        description = ET.fromstring(self.feed()).findtext("channel/description")
        total = len(self.order)
        self.assertIn(f"2 of {total} episodes are ready; {total - 2} are not ready yet", description)
        self.assertIn("Listening is not evidence", description)
        status = self.c.get("/api/feed").json()
        self.assertEqual(next(p for p in status["packages"] if p["package"] == "gh-300"), {"package": "gh-300", "exam": "GH-300", "ready": 2, "total": total})
        for lesson in (self.old_first, self.half, self.stub):
            self.assertEqual(self.c.get(f"/feed/{self.token}/ep/{lesson}.mp3").status_code, 404)

    def test_episode_notes_say_what_it_is_where_it_comes_from_and_what_listening_does_not_prove(self):
        notes = self.items()[0].findtext("description")
        doc = self.dojo.lesson(self.first)
        for slide in doc["slides"]:
            self.assertIn(f"<li>{slide['title']}</li>", notes)
        self.assertIn("AI-generated voices", notes)
        for source in doc["sources"]:
            self.assertIn(f'<a href="{source["url"]}">{source["title"]}</a>, {source["publisher"]}, licensed '
                          '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>', notes)
        self.assertIn("Listening is not evidence.", notes)
        page = f"http://testserver/?go=skill/gh-300/{self.order[0]}"   # Easy Auth loses a "#" route at sign-in, so the link is /?go=
        self.assertIn(f'<a href="{page}">Check this skill in Dojo</a>', notes)
        self.assertEqual(self.items()[0].findtext("link"), page)

    def test_the_enclosure_is_the_joined_audio_with_the_length_it_says(self):
        item = self.items()[0]
        enclosure = item.find("enclosure")
        url = self.local(enclosure.get("url"))
        self.assertEqual(url, f"/feed/{self.token}/ep/{self.first}.mp3")
        body = self.c.get(url)
        self.assertEqual(body.status_code, 200)
        self.assertEqual(body.headers["content-type"], "audio/mpeg")
        self.assertEqual(len(body.content), int(enclosure.get("length")))
        self.assertEqual(body.content, mp3(40, fill=20) + mp3(41, fill=21))
        self.assertEqual(item.findtext("itunes:duration", namespaces=NS), str(round(81 * 0.024)))
        self.assertEqual(body.headers["cache-control"], "private, max-age=86400")
        self.assertEqual(body.headers["x-robots-tag"], "noindex, nofollow")
        self.assertEqual(body.headers["accept-ranges"], "bytes")

    def test_head_and_ranges(self):
        url = f"/feed/{self.token}/ep/{self.third}.mp3"
        size = len(mp3(40)) + len(mp3(41))
        head = self.c.head(url)
        self.assertEqual((head.status_code, head.headers["content-length"], head.content), (200, str(size), b""))
        self.assertIn("last-modified", head.headers)
        part = self.c.get(url, headers={"Range": "bytes=0-99"})
        self.assertEqual((part.status_code, part.headers["content-range"], len(part.content)), (206, f"bytes 0-99/{size}", 100))
        tail = self.c.get(url, headers={"Range": f"bytes={size - 10}-"})
        self.assertEqual((tail.status_code, len(tail.content)), (206, 10))
        beyond = self.c.get(url, headers={"Range": f"bytes={size}-"})
        self.assertEqual((beyond.status_code, beyond.headers["content-range"]), (416, f"bytes */{size}"))
        self.assertEqual(self.c.head(f"/feed/{self.token}/gh-300.xml").status_code, 200)

    def test_the_feed_writes_nothing_into_any_learners_data(self):
        """No event, no log, nothing about who fetched what. The only writes allowed are the joined audio under
        podcast/, and once per lesson that the feed listed or served it, in that learner's aired.json (ADR 0009),
        which holds no request, count or app."""
        root = Path(self.tmp)
        aired_path = root / "learners" / self.me.key / "aired.json"

        def data() -> dict[str, bytes]:
            return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
                    if p.is_file() and p.relative_to(root).parts[0] != "podcast" and p != aired_path}

        self.feed()
        before, events = data(), len(self.store.events(self.me.key))
        aired = aired_path.read_bytes()
        ep = f"/feed/{self.token}/ep/{self.first}.mp3"
        wrong = ("A" if self.token[0] != "A" else "B") + self.token[1:]
        for _ in range(2):
            for method, path, headers in (("GET", f"/feed/{self.token}/gh-300.xml", {}), ("HEAD", f"/feed/{self.token}/gh-300.xml", {}),
                                          ("GET", f"/feed/{self.token}/gh-300.png", {}), ("GET", ep, {}), ("HEAD", ep, {}),
                                          ("GET", ep, {"Range": "bytes=0-99"}), ("GET", f"/feed/{self.token}/ep/{self.third}.mp3", {}),
                                          ("GET", f"/feed/{wrong}/ep/{self.first}.mp3", {}), ("POST", ep, POST),
                                          ("GET", f"/feed/{self.token}/ep/{self.old_first}.mp3", {}),
                                          ("GET", f"/feed/{self.token}/ep/{self.half}.mp3", {})):
                self.c.request(method, path, headers=headers)
        self.assertEqual(data(), before)
        self.assertEqual(len(self.store.events(self.me.key)), events)
        exposures = self.c.get(f"/api/skills/gh-300/{self.order[0]}").json()["evidence"]["exposures"]
        self.assertNotIn("podcast", [x["modality"] for x in exposures])
        self.assertEqual(aired_path.read_bytes(), aired, "written once per lesson, not per request")
        listed = json.loads(aired)
        self.assertEqual({pid: {sid: set(lessons) for sid, lessons in skills.items()} for pid, skills in listed.items()},
                         {"gh-300": {self.order[0]: {self.first}, self.order[2]: {self.third}}}, "only lessons it listed or served")
        for lessons in listed["gh-300"].values():
            for at in lessons.values():
                parse_iso(at)
        self.assertEqual({p.name for p in (root / "podcast").iterdir()
                          if not p.name.startswith(".") and not EPISODE_KEY.match(p.name.split(".")[0])}, set())

    def test_the_feed_holds_no_learner_data(self):
        self.c.post(f"/api/lessons/{self.first}/viewed", json={"modality": "listen"}, headers=POST)
        me = self.c.get("/api/me").json()
        text = b"".join(self.feed(pid) for pid in self.dojo.packages.ids()).decode()
        ev = [e for e in self.store.events(self.me.key)]
        self.assertTrue(ev)
        for secret in [me["name"], self.me.id, self.me.key, "lesson.viewed", "modality", "exposure", *(e["id"] for e in ev), *(e["hash"] for e in ev)]:
            self.assertNotIn(secret, text)

    def test_the_token_is_never_written_to_a_log(self):
        """EG-29: the link is a bearer credential. Dojo logs /api paths only, and a refused path without the path."""
        feed, ep = f"/feed/{self.token}/gh-300".encode(), f"/feed/{self.token}/ep/{self.third}.mp3".encode()
        requests = [(feed + b".xml", "GET"), (feed + b".png", "HEAD"), (ep, "HEAD"), (ep, "GET"),
                    (f"/feed/{self.token[::-1]}/gh-300.xml".encode(), "GET"), (f"/feed/{self.token}/../api/me".encode(), "GET"),
                    (feed + b".xml", "POST"), (f"/feed/{self.token}/ep/lesson-{'0' * 20}.mp3".encode(), "GET")]
        records: list[logging.LogRecord] = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root, level = logging.getLogger(), logging.getLogger().level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            statuses = [call(self.app, path, method=method)[0] for path, method in requests]
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        self.assertEqual(statuses, [200, 200, 200, 200, 404, 400, 405, 404])
        self.assertTrue(any(r.name == "dojo" and "refused" in r.getMessage() for r in records))
        for r in records:
            self.assertNotIn(self.token, r.getMessage() + (r.exc_text or ""), r.name)

    def test_wrong_or_missing_tokens_and_unknown_names_get_an_empty_404(self):
        wrong = ("A" if self.token[0] != "A" else "B") + self.token[1:]
        for path in (f"/feed/{wrong}/gh-300.xml", f"/feed/{wrong}/ep/{self.first}.mp3", f"/feed/{wrong}/gh-300.png",
                     "/feed/gh-300.xml", "/feed/", "/feed", f"/feed/{self.token}", f"/feed/{self.token}/",
                     f"/feed/{self.token}/xx-000.xml", f"/feed/{self.token}/gh-300.json", f"/feed/{self.token}/ep/{self.first}.wav",
                     f"/feed/{self.token}/ep/lesson-{'0' * 20}.mp3", f"/feed/{self.token}/gh-300.xml/more"):
            r = self.c.get(path)
            self.assertEqual((r.status_code, r.content), (404, b""), path)
            self.assertEqual(r.headers["x-robots-tag"], "noindex, nofollow", path)
            self.assertNotIn("set-cookie", r.headers, path)

    def test_the_feed_answers_only_get_and_head(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            r = self.c.request(method, f"/feed/{self.token}/gh-300.xml", headers=POST)
            self.assertEqual((r.status_code, r.headers.get("allow"), r.content), (405, "GET, HEAD", b""), method)

    def test_a_new_link_ends_the_old_one_and_turning_it_off_ends_both(self):
        feed = self.c.post("/api/feed/link/new", headers=POST).json()
        new = feed["feeds"][0]["url"].split("/")[4]
        self.assertEqual(self.c.post("/api/feed/link", headers=POST).status_code, 409)
        self.assertEqual(self.c.get(f"/feed/{self.token}/gh-300.xml").status_code, 404)
        self.assertEqual(self.c.get(f"/feed/{new}/gh-300.xml").status_code, 200)
        self.assertTrue(all(f["qr"].startswith("data:image/svg+xml;") for f in feed["feeds"]))
        self.assertEqual({f["package"] for f in feed["feeds"]}, set(self.dojo.packages.ids()))
        self.assertNotIn(new, json.dumps(self.c.get("/api/feed").json()))
        self.assertTrue(self.c.delete("/api/feed/link", headers=POST).json()["active"] is False)
        self.assertEqual(self.c.get(f"/feed/{new}/gh-300.xml").status_code, 404)
        self.assertNotIn(new.encode(), b"".join(p.read_bytes() for p in Path(self.tmp).rglob("*") if p.is_file()))
        FeedTests.token = self.c.post("/api/feed/link", headers=POST).json()["feeds"][0]["url"].split("/")[4]

    def test_the_cover_art_is_a_square_rgb_png_podcast_apps_accept(self):
        for pid in self.dojo.packages.ids():
            r = self.c.get(f"/feed/{self.token}/{pid}.png")
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"), pid)
            self.assertIn("last-modified", self.c.head(f"/feed/{self.token}/{pid}.png").headers)
            png = r.content
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            width, height, depth, colour = struct.unpack(">IIBB", png[16:26])
            self.assertEqual(width, height, pid)
            self.assertTrue(1400 <= width <= 3000, pid)
            self.assertEqual((depth, colour), (8, 2), f"{pid}: 8-bit RGB without an alpha channel")
            self.assertLess(len(png), 512 * 1024, pid)

    def test_old_joined_files_are_removed_when_nothing_uses_them(self):
        self.feed()
        stale = "e" + "0" * 39
        self.store.write_bytes("podcast", stale + ".mp3", data=b"x")
        self.store.write("podcast", stale, value={"bytes": 1})
        current = episode_key(self.dojo.slide_media(self.dojo.lesson(self.first)))
        with mock.patch.object(self.podcast, "unused_grace_s", 0):  # the grace has its own test (ReplacedEpisodeTests)
            self.podcast.prune()
        self.assertFalse(self.store.exists("podcast", stale + ".mp3"))
        self.assertFalse(self.store.exists("podcast", stale + ".json"))
        self.assertTrue(self.store.exists("podcast", current + ".mp3"))
        self.assertTrue(self.store.exists("learners", self.me.key, "aired.json"), "what the feed listed is never pruned")


class PodcastQuestionTests(unittest.TestCase):
    """The signed-in side. Dojo cannot see a podcast app, so it asks before an answer that could count as "later"
    about a skill whose episode the feed has listed or served, with the feed on or off. The reply is recorded
    before the answer, in the signed-in request."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dojo-podq-")
        cls.app = create_app(settings(cls.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        cls.c = TestClient(cls.app)
        cls.dojo, cls.me, cls.podcast = cls.app.state.dojo, cls.app.state.preparer.owner, cls.app.state.podcast
        cls.podcast.prune_after_s = 3600  # pruning is tested on its own (ReplacedEpisodeTests)
        cls.addClassCleanup(cls.podcast.close)
        pkg = cls.dojo.packages.get("gh-300")
        cls.order = [s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]]
        cls.scenarios = iter(s for s in cls.order if pkg["skills"][s]["kind"] == "scenario")
        cls.plain = next(s for s in cls.order if pkg["skills"][s]["kind"] != "scenario")

    def setUp(self):
        """The feed is on, and a podcast app uses this link."""
        self.token = self.podcast.links.create(replace=True)

    def fetch(self) -> list[str]:
        """What a podcast app does: fetch the feed. The lessons it lists."""
        r = self.c.get(f"/feed/{self.token}/gh-300.xml")
        self.assertEqual(r.status_code, 200)
        return [i.findtext("guid") for i in ET.fromstring(r.content).findall("channel/item")]

    def dated(self, created: dict[str, str]) -> None:
        """Lessons made in the same second: say when each was written."""
        for lesson, at in created.items():
            doc = self.dojo.store.read("content", "lessons", lesson)
            doc["created"] = at
            self.dojo.store.write("content", "lessons", lesson, value=doc)
        self.dojo._lesson_meta.clear()

    def wait(self, job: dict) -> dict:
        self.assertIn("id", job, job)
        for _ in range(200):
            job = self.c.get(f"/api/jobs/{job['id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job.get("error"))
        return job["result"]

    def narrate(self, lesson: str, audio: bool = True) -> None:
        """The stub narrates every lesson alike; give this one its own narration, with its audio unless told otherwise."""
        doc = self.dojo.store.read("content", "lessons", lesson)
        for i, slide in enumerate(doc["slides"]):
            slide["narration"] = [{"voice": "guide", "text": f"Slide {i + 1} of {lesson}."}]
        self.dojo.store.write("content", "lessons", lesson, value=doc)
        if audio:
            for i, m in enumerate(self.dojo.slide_media(self.dojo.lesson(lesson))):
                self.dojo.store.write_bytes("media", m + ".mp3", data=mp3(40 + i, fill=60 + i))

    def taught(self, sid: str | None = None, viewed: bool = True, narrated: bool = True, fetched: bool = True) -> tuple[str, str]:
        """A skill with a lesson, narrated, listed by the feed to a podcast app, and viewed now, unless told otherwise."""
        sid = sid or next(self.scenarios)
        lesson = self.dojo.make_lesson(lambda step: None, self.me, "gh-300", sid)["lesson"]
        self.narrate(lesson, audio=narrated)
        if fetched:
            self.assertEqual(lesson in self.fetch(), narrated)
        if viewed:
            self.assertEqual(self.c.post(f"/api/lessons/{lesson}/viewed", json={"modality": "read"}, headers=POST).status_code, 200)
        return sid, lesson

    def item(self, sid: str, mode: str = "check") -> str:
        return self.wait(self.c.post("/api/items", json={"package": "gh-300", "skill": sid, "mode": mode}, headers=POST).json())["item"]

    @contextlib.contextmanager
    def clock(self, **delta):
        """Everything in Dojo reads the time through utcnow; move it forward for all of them at once."""
        at = utcnow() + timedelta(**delta)
        with contextlib.ExitStack() as stack:
            for where in ("app.core.utcnow", "app.learning.utcnow", "app.podcast.utcnow"):
                stack.enter_context(mock.patch(where, return_value=at))
            yield at

    def asked(self, item: str) -> dict | None:
        view = self.c.get(f"/api/items/{item}").json()
        self.assertIn("podcast_question", view)
        return view["podcast_question"]

    def answer(self, item: str, **extra) -> httpx.Response:
        return self.c.post(f"/api/items/{item}/answer", json={"text": "Both r1 and r2 apply here.", "elapsed_s": 30, **extra}, headers=POST)

    def events(self, kind: str, item: str) -> list[dict]:
        return [e for e in self.dojo.store.events(self.me.key) if e["type"] == kind and e["data"].get("item") == item]

    def attempt(self, sid: str, item: str) -> dict:
        e = self.c.get(f"/api/skills/gh-300/{sid}").json()["evidence"]
        return e, next(a for a in e["attempts"] if a["item"] == item)

    def last_teaching(self, sid: str) -> str | None:
        return self.c.get(f"/api/skills/gh-300/{sid}").json()["evidence"]["last_teaching"]

    def test_it_asks_only_when_the_answer_could_count_as_later(self):
        sid, lesson = self.taught()
        item = self.item(sid)
        self.assertIsNone(self.asked(item), "right after the lesson, a check cannot count as later")
        with self.clock(hours=18, minutes=58):
            self.assertIsNone(self.asked(item))
        with self.clock(hours=19, minutes=31):  # 60 minutes before "later", counted from the end of the view's 30 minutes
            q = self.asked(item)
        self.assertEqual(q, {"since": self.last_teaching(sid), "lesson": lesson, "episode": self.order.index(sid) + 1, "feed_on": True})

    def test_an_answer_the_question_applies_to_is_refused_without_a_reply(self):
        sid, _ = self.taught()
        item = self.item(sid)
        with self.clock(hours=21):
            r = self.answer(item)
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "Answer the podcast question first: did you listen to this lesson in a podcast app?"))
        self.assertIsNone(self.c.get(f"/api/items/{item}").json()["answer"])
        self.assertEqual(self.events("answer.submitted", item) + self.events("podcast.declared", item), [])

    def test_yes_is_teaching_at_that_moment_so_the_answer_is_not_later(self):
        sid, lesson = self.taught()
        item = self.item(sid)
        with self.clock(hours=21):
            since = self.asked(item)["since"]
            self.wait(self.answer(item, podcast_heard=True).json())
            e, a = self.attempt(sid, item)
        self.assertEqual(a["minutes_since_teaching"], 0)
        self.assertFalse(e["checks"]["later"])
        self.assertIn("Declared: listened in a podcast app before answering", a["conditions"])
        self.assertEqual([x["modality"] for x in e["exposures"]], ["read", "podcast"])
        [said] = self.events("podcast.declared", item)
        self.assertEqual(said["data"], {"package": "gh-300", "skill": sid, "item": item, "lesson": lesson, "heard": True,
                                        "since": since, "asked": True})
        order = [ev["type"] for ev in self.dojo.store.events(self.me.key) if ev["data"].get("item") == item]
        self.assertEqual(order, ["item.issued", "podcast.declared", "answer.submitted", "answer.judged"])
        self.assertTrue(self.dojo.store.verify_chain(self.me.key)["ok"])

    def test_no_leaves_later_standing_and_the_attempt_says_so(self):
        sid, _ = self.taught()
        item = self.item(sid)
        with self.clock(hours=21):
            self.assertIsNotNone(self.asked(item))
            self.wait(self.answer(item, podcast_heard=False).json())
            e, a = self.attempt(sid, item)
        self.assertGreaterEqual(a["minutes_since_teaching"], 20 * 60)
        self.assertTrue(e["checks"]["later"])
        self.assertIn("Declared: no podcast listening since the last teaching", a["conditions"])
        self.assertEqual([x["modality"] for x in e["exposures"]], ["read"])
        self.assertIs(self.events("podcast.declared", item)[0]["data"]["heard"], False)

    def test_nothing_is_asked_while_no_episode_can_be_on_the_phone(self):
        """The feed is off and has never listed anything, as after "Delete my record"."""
        sid, _ = self.taught(fetched=False)
        item = self.item(sid)
        self.c.delete("/api/feed/link", headers=POST)
        self.podcast.forget(self.me)
        self.assertFalse(self.c.get("/api/feed").json()["on_phone"])
        with self.clock(hours=21):
            self.assertIsNone(self.asked(item))
            self.wait(self.answer(item).json())
            e, a = self.attempt(sid, item)
        self.assertTrue(e["checks"]["later"])
        self.assertIsNone(a["podcast_declared"])
        self.assertEqual(self.events("podcast.declared", item), [])

    def test_a_lesson_the_feed_has_not_listed_yet_is_not_asked_about(self):
        """Narrated while the feed is on, but no podcast app has fetched the feed since, so none can have it."""
        sid, lesson = self.taught(fetched=False)
        item = self.item(sid)
        with self.clock(hours=21):
            self.assertIsNone(self.asked(item))
        self.assertIn(lesson, self.fetch())
        with self.clock(hours=21):
            self.assertEqual(self.asked(item)["lesson"], lesson)

    def test_serving_an_episode_is_enough_to_ask_about_it(self):
        """A podcast app can download an episode whose address it already has without fetching the feed again."""
        sid, lesson = self.taught(fetched=False)
        item = self.item(sid)
        self.assertEqual(self.c.head(f"/feed/{self.token}/ep/{lesson}.mp3").status_code, 200)
        with self.clock(hours=21):
            self.assertEqual(self.asked(item)["lesson"], lesson)

    def test_turning_the_feed_off_keeps_asking_about_what_may_be_on_the_phone(self):
        """Episodes downloaded while the feed was on still play after it is turned off."""
        sid, lesson = self.taught()
        item = self.item(sid)
        off = self.c.delete("/api/feed/link", headers=POST).json()
        self.assertEqual((off["active"], off["on_phone"]), (False, True), "the page says Dojo keeps asking")
        with self.clock(hours=21):
            q = self.asked(item)
            self.assertEqual((q["lesson"], q["episode"], q["feed_on"]), (lesson, None, False))
            r = self.answer(item)
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "Answer the podcast question first: did you listen to this lesson in a podcast app?"))
            self.wait(self.answer(item, podcast_heard=False).json())
            e, a = self.attempt(sid, item)
        self.assertTrue(e["checks"]["later"])
        self.assertIn("Declared: no podcast listening since the last teaching", a["conditions"])
        [said] = self.events("podcast.declared", item)
        self.assertEqual((said["data"]["lesson"], said["data"]["heard"], said["data"]["asked"]), (lesson, False, True))

    def test_new_narration_keeps_asking_about_the_episode_already_on_the_phone(self):
        """New voices give every slide a new audio file, so the lesson has no episode until it is narrated again,
        and a podcast app keeps the one it downloaded."""
        sid, lesson = self.taught()
        item = self.item(sid)
        self.addCleanup(self.podcast._lessons.clear)
        with mock.patch.dict("app.speech.VOICES", {"guide": "en-US-SomeOtherVoiceNeural"}):
            self.podcast._lessons.clear()
            self.assertNotIn(lesson, self.fetch(), "out of the feed until it is narrated again")
            with self.clock(hours=21):
                q = self.asked(item)
                self.assertEqual((q["lesson"], q["episode"], q["feed_on"]), (lesson, None, True))
                r = self.answer(item)
                self.assertEqual((r.status_code, r.json()["detail"]), (409, "Answer the podcast question first: did you listen to this lesson in a podcast app?"))
                self.wait(self.answer(item, podcast_heard=False).json())
                e, a = self.attempt(sid, item)
        self.assertTrue(e["checks"]["later"])
        self.assertIn("Declared: no podcast listening since the last teaching", a["conditions"])
        [said] = self.events("podcast.declared", item)
        self.assertEqual((said["data"]["lesson"], said["data"]["heard"], said["data"]["asked"]), (lesson, False, True))

    def test_new_narration_with_the_feed_off_still_asks(self):
        sid, lesson = self.taught()
        item = self.item(sid)
        self.c.delete("/api/feed/link", headers=POST)
        self.addCleanup(self.podcast._lessons.clear)
        with mock.patch.dict("app.speech.VOICES", {"guide": "en-US-SomeOtherVoiceNeural"}), self.clock(hours=21):
            self.podcast._lessons.clear()
            self.assertEqual(self.asked(item), {"since": self.last_teaching(sid), "lesson": lesson, "episode": None, "feed_on": False})
            self.assertEqual(self.answer(item).status_code, 409)
            self.wait(self.answer(item, podcast_heard=True).json())
            e, a = self.attempt(sid, item)
        self.assertEqual(a["minutes_since_teaching"], 0)
        self.assertFalse(e["checks"]["later"])
        self.assertIn("Declared: listened in a podcast app before answering", a["conditions"])
        [said] = self.events("podcast.declared", item)
        self.assertEqual((said["data"]["lesson"], said["data"]["heard"], said["data"]["asked"]), (lesson, True, True))

    def test_a_new_link_keeps_asking_about_what_the_old_one_listed(self):
        """A lesson listed under the first link and replaced before the second may still be on the phone."""
        sid, old = self.taught()
        newer = self.dojo.make_lesson(lambda step: None, self.me, "gh-300", sid)["lesson"]
        self.narrate(newer, audio=False)
        self.dated({old: "2026-03-01T10:00:00+00:00", newer: "2026-03-02T10:00:00+00:00"})
        self.c.delete("/api/feed/link", headers=POST)
        self.token = self.c.post("/api/feed/link", headers=POST).json()["feeds"][0]["url"].split("/")[4]
        self.assertNotIn(old, self.fetch())
        item = self.item(sid)
        with self.clock(hours=21):
            self.assertEqual(self.asked(item)["lesson"], old)

    def test_a_lesson_written_after_the_feed_was_turned_off_is_not_asked_about(self):
        """It was never in the feed, so no podcast app can have it."""
        self.c.delete("/api/feed/link", headers=POST)
        sid, lesson = self.taught(fetched=False)
        self.assertEqual(self.c.get(f"/feed/{self.token}/ep/{lesson}.mp3").status_code, 404)
        item = self.item(sid)
        with self.clock(hours=21):
            self.assertIsNone(self.asked(item))
            self.wait(self.answer(item).json())
            e, a = self.attempt(sid, item)
        self.assertTrue(e["checks"]["later"])
        self.assertEqual(self.events("podcast.declared", item), [])

    def test_a_lesson_replaced_before_the_feed_listed_it_is_not_asked_about(self):
        """The newer lesson took the skill's place before any podcast app fetched the feed, and it is not narrated
        yet, so the feed never listed or served either of them."""
        sid, old = self.taught(fetched=False)
        newer = self.dojo.make_lesson(lambda step: None, self.me, "gh-300", sid)["lesson"]
        self.narrate(newer, audio=False)
        self.dated({old: "2026-03-01T10:00:00+00:00", newer: "2026-03-02T10:00:00+00:00"})
        self.assertNotIn(old, self.fetch())
        self.assertEqual(self.c.get(f"/feed/{self.token}/ep/{old}.mp3").status_code, 404)
        item = self.item(sid)
        with self.clock(hours=21):
            self.assertIsNone(self.asked(item))

    def test_a_reply_nobody_asked_for_is_still_the_learners_word(self):
        sid, _ = self.taught()
        item = self.item(sid)
        self.assertIsNone(self.asked(item))
        self.wait(self.answer(item, podcast_heard=True).json())
        self.assertEqual(self.events("podcast.declared", item)[0]["data"]["asked"], False)

    def test_practice_and_answered_items_are_never_asked(self):
        sid, _ = self.taught()
        practice, check = self.item(sid, "practice"), self.item(sid, "check")
        with self.clock(hours=21):
            self.assertIsNone(self.asked(practice))
            self.wait(self.answer(practice).json())
            self.wait(self.answer(check, podcast_heard=False).json())
            self.assertIsNone(self.asked(check))

    def test_with_no_teaching_it_asks_only_where_a_changed_condition_could_count(self):
        sid, lesson = self.taught(viewed=False)
        self.assertEqual(self.asked(self.item(sid, "probe")), {"since": None, "lesson": lesson, "episode": self.order.index(sid) + 1,
                                                               "feed_on": True})
        plain, _ = self.taught(self.plain, viewed=False)
        self.assertIsNone(self.asked(self.item(plain, "probe")))

    def test_a_skill_without_a_lesson_is_not_asked_about(self):
        sid = next(self.scenarios)
        self.assertIsNone(self.asked(self.item(sid, "probe")))

    def test_a_skill_that_never_had_an_episode_is_not_asked_about(self):
        """Only a lesson with every slide narrated becomes an episode, so without one the skill was never in the feed."""
        sid, _ = self.taught(narrated=False)
        item = self.item(sid)
        with self.clock(hours=21):
            self.assertIsNone(self.asked(item))
            self.wait(self.answer(item).json())
            e, a = self.attempt(sid, item)
        self.assertTrue(e["checks"]["later"])
        self.assertIsNone(a["podcast_declared"])
        self.assertEqual(self.events("podcast.declared", item), [])

    def test_an_episode_that_left_the_feed_is_still_asked_about(self):
        """A newer lesson takes the skill's place in the feed only once it is narrated. Until then the skill has
        no episode in the feed, but the older one may still be on the phone."""
        sid, heard = self.taught()
        newer = self.dojo.make_lesson(lambda step: None, self.me, "gh-300", sid)["lesson"]
        self.narrate(newer, audio=False)
        self.dated({heard: "2026-03-01T10:00:00+00:00", newer: "2026-03-02T10:00:00+00:00"})
        self.assertEqual([x["id"] for x in self.dojo.lessons_for("gh-300", sid)], [newer, heard])
        item = self.item(sid)
        with self.clock(hours=21):
            q = self.asked(item)
            self.assertEqual((q["lesson"], q["episode"]), (heard, None))
            self.assertEqual(self.answer(item).status_code, 409)
            self.wait(self.answer(item, podcast_heard=True).json())
        [said] = self.events("podcast.declared", item)
        self.assertEqual((said["data"]["lesson"], said["data"]["heard"], said["data"]["asked"]), (heard, True, True))

    def test_the_episode_number_is_given_when_the_lesson_is_in_the_feed(self):
        sid, _ = self.taught(viewed=False)
        self.assertEqual(self.asked(self.item(sid, "probe"))["episode"], self.order.index(sid) + 1)


def narrated_lesson(app, sid: str, fill: int, created: str) -> str:
    """A stub lesson for this gh-300 skill with its own narration and real-looking audio, written at this time."""
    dojo, podcast = app.state.dojo, app.state.podcast
    lesson = dojo.make_lesson(lambda step: None, app.state.preparer.owner, "gh-300", sid)["lesson"]
    doc = dojo.store.read("content", "lessons", lesson)
    doc["created"] = created
    for i, slide in enumerate(doc["slides"]):
        slide["narration"] = [{"voice": "guide", "text": f"Slide {i + 1} of {lesson}."}]
    dojo.store.write("content", "lessons", lesson, value=doc)
    dojo._lesson_meta.clear()
    podcast._lessons.pop(lesson, None)
    for i, m in enumerate(dojo.slide_media(dojo.lesson(lesson))):
        dojo.store.write_bytes("media", m + ".mp3", data=mp3(40 + i, fill=fill + i))
    return lesson


class AiredRecordTests(unittest.TestCase):
    """What the feed keeps for the signed-in side: when it first listed or served each lesson. It outlives the
    link and the lesson's narration, and goes only with "Delete my record"."""

    def setUp(self):
        self.app = create_app(settings(tempfile.mkdtemp(prefix="dojo-aired-")), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.c = TestClient(self.app)
        self.podcast = self.app.state.podcast
        self.podcast.prune_after_s = 3600
        self.addCleanup(self.podcast.close)
        pkg = self.app.state.dojo.packages.get("gh-300")
        self.sid = next(s for d in pkg["domains"] for g in d["groups"] for s in g["skills"])
        self.lesson = narrated_lesson(self.app, self.sid, fill=20, created="2026-03-01T10:00:00+00:00")
        self.token = self.podcast.links.create()

    def aired(self) -> dict | None:
        return self.app.state.dojo.store.read(*self.parts)

    @property
    def parts(self) -> tuple[str, ...]:
        return ("learners", self.podcast.links.owner.key, "aired")

    def listed(self) -> list[str]:
        r = self.c.get(f"/feed/{self.token}/gh-300.xml")
        self.assertEqual(r.status_code, 200)
        return [i.findtext("guid") for i in ET.fromstring(r.content).findall("channel/item")]

    def test_the_first_download_or_listing_is_kept_until_the_record_is_deleted(self):
        self.assertIsNone(self.aired(), "nothing is noted before a podcast app asks")
        at = (utcnow() - timedelta(days=3)).replace(microsecond=0)
        with mock.patch("app.core.utcnow", return_value=at), mock.patch("app.podcast.utcnow", return_value=at):
            self.assertEqual(self.c.get(f"/feed/{self.token}/ep/{self.lesson}.mp3").status_code, 200)
        first = {"gh-300": {self.sid: {self.lesson: iso(at)}}}
        self.assertEqual(self.aired(), first)
        self.assertEqual(self.listed(), [self.lesson])
        self.assertEqual(self.aired(), first, "noted once, at the first download or listing")
        self.assertEqual(self.c.post("/api/feed/link/new", headers=POST).status_code, 200)
        off = self.c.delete("/api/feed/link", headers=POST).json()
        self.assertEqual((off["active"], off["on_phone"]), (False, True))
        self.assertEqual(self.aired(), first, "a new link and turning the feed off keep it")
        self.assertEqual(self.c.post("/api/record/delete", json={"confirm": "DELETE"}, headers=POST).status_code, 200)
        self.assertIsNone(self.aired())
        self.assertFalse(self.c.get("/api/feed").json()["on_phone"])

    def test_an_episode_dojo_could_not_note_is_neither_listed_nor_served(self):
        store, write = self.podcast.store, self.podcast.store.write

        def failing(*parts, **kw):
            if parts == self.parts:
                raise OSError("no space left on the share")
            return write(*parts, **kw)

        with mock.patch.object(store, "write", side_effect=failing), self.assertLogs("dojo.podcast", "WARNING") as logs:
            self.assertEqual(self.listed(), [])
            self.assertEqual(self.c.get(f"/feed/{self.token}/ep/{self.lesson}.mp3").status_code, 404)
            self.assertEqual(self.c.head(f"/feed/{self.token}/ep/{self.lesson}.mp3").status_code, 404)
        self.assertTrue(all("podcast episode left out: Dojo could not note that the feed lists it" in line for line in logs.output))
        self.assertIsNone(self.aired())
        self.assertEqual(self.listed(), [self.lesson], "listed once it can be noted")

    def test_one_feed_request_notes_all_its_new_episodes_in_one_write(self):
        pkg = self.app.state.dojo.packages.get("gh-300")
        second = [s for d in pkg["domains"] for g in d["groups"] for s in g["skills"]][1]
        other = narrated_lesson(self.app, second, fill=60, created="2026-03-02T10:00:00+00:00")
        store, write, writes = self.podcast.store, self.podcast.store.write, []

        def counting(*parts, **kw):
            if parts == self.parts:
                writes.append(kw["value"])
            return write(*parts, **kw)

        with mock.patch.object(store, "write", side_effect=counting):
            self.assertEqual(self.listed(), [self.lesson, other])
            self.assertEqual(len(writes), 1, "both new episodes in one write")
            self.assertEqual(set(writes[0]["gh-300"]), {self.sid, second})
            self.listed()
            self.assertEqual(len(writes), 1, "nothing new, nothing written")


class ReplacedEpisodeTests(unittest.TestCase):
    """A joined file is removed soon after a newer lesson takes its place, and at startup and when a link is made."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dojo-prune-")
        self.app = create_app(settings(self.tmp), http=httpx.Client(transport=httpx.MockTransport(web)))
        self.podcast = self.app.state.podcast
        self.podcast.prune_after_s = 0.05
        self.podcast.unused_grace_s = 0
        self.addCleanup(self.podcast.close)
        pkg = self.app.state.dojo.packages.get("gh-300")
        self.sid = next(s for d in pkg["domains"] for g in d["groups"] for s in g["skills"])

    def files(self) -> set[str]:
        folder = Path(self.tmp) / "podcast"
        return {p.name for p in folder.iterdir() if not p.name.startswith(".")} if folder.exists() else set()

    def until(self, done, why: str) -> None:
        deadline = time.monotonic() + 5
        while not done():
            if time.monotonic() > deadline:
                self.fail(f"{why}: {sorted(self.files())}")
            time.sleep(0.02)

    def key(self, lesson: str) -> str:
        return episode_key(self.podcast._lesson(lesson)["media"])

    def test_the_replaced_lessons_file_is_removed_soon_after_the_feed_lists_the_new_one(self):
        c, token = TestClient(self.app), self.podcast.links.create()
        old = narrated_lesson(self.app, self.sid, fill=10, created="2026-03-01T10:00:00+00:00")
        self.assertIn(old, c.get(f"/feed/{token}/gh-300.xml").text)
        self.assertLessEqual({self.key(old) + ".mp3", self.key(old) + ".json"}, self.files())
        self.until(lambda: self.podcast._pruner is None or not self.podcast._pruner.is_alive(), "the first prune did not run")
        self.assertLessEqual({self.key(old) + ".mp3", self.key(old) + ".json"}, self.files(), "still the episode")
        new = narrated_lesson(self.app, self.sid, fill=90, created="2026-03-02T10:00:00+00:00")
        feed = c.get(f"/feed/{token}/gh-300.xml").text
        self.assertIn(new, feed)
        self.assertNotIn(old, feed)
        self.until(lambda: self.files() == {self.key(new) + ".mp3", self.key(new) + ".json"}, "the old file was left")
        owner = self.podcast.links.owner.key
        self.assertIn(old, self.app.state.dojo.store.read("learners", owner, "aired")["gh-300"][self.sid], "what aired is kept")

    def test_a_replaced_file_stays_while_a_download_could_still_open_it(self):
        """A download handed the old file just before a newer lesson took its place opens it a moment later.
        So a replaced file goes only once it has been unused for the grace time."""
        self.podcast.prune_after_s, self.podcast.unused_grace_s = 3600, 600  # this test runs every prune itself
        c, token = TestClient(self.app), self.podcast.links.create()
        old = narrated_lesson(self.app, self.sid, fill=10, created="2026-03-01T10:00:00+00:00")
        self.assertEqual(c.get(f"/feed/{token}/ep/{old}.mp3").status_code, 200)
        new = narrated_lesson(self.app, self.sid, fill=90, created="2026-03-02T10:00:00+00:00")
        self.assertIn(new, c.get(f"/feed/{token}/gh-300.xml").text)
        replaced = self.key(old) + ".mp3"
        for seconds, there in ((0, True), (599, True), (600, False)):
            with mock.patch("app.podcast.time.monotonic", return_value=5000.0 + seconds):
                self.podcast.prune()
            self.assertEqual(replaced in self.files(), there, f"{seconds} s after it was first found unused")
        self.assertIn(self.key(new) + ".mp3", self.files(), "the current episode is never removed")

    def test_a_file_no_episode_uses_is_removed_at_startup_and_when_a_link_is_made(self):
        store, stale = self.app.state.dojo.store, "e" + "0" * 39

        def leave() -> None:
            store.write_bytes("podcast", stale + ".mp3", data=mp3(3))
            store.write("podcast", stale, value={"bytes": 3 * 288, "seconds": 0.072})

        leave()
        with TestClient(self.app):
            self.until(lambda: not {stale + ".mp3", stale + ".json"} & self.files(), "left at startup")
        leave()
        self.assertEqual(TestClient(self.app).post("/api/feed/link", headers=POST).status_code, 200)
        self.until(lambda: not {stale + ".mp3", stale + ".json"} & self.files(), "left when the link was made")


def call(app, raw_path: bytes, method: str = "GET", headers: tuple = ()) -> tuple[int, bytes]:
    """Sends a request with this exact raw path, as uvicorn would pass it on. HTTP clients tidy up
    dot segments before sending, so they cannot test this."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": unquote(raw_path.decode("latin-1")), "raw_path": raw_path, "query_string": b"", "root_path": "",
             "headers": [(b"host", b"testserver"), *headers], "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


class OpenFeedSignInTests(unittest.TestCase):
    """As in Azure: everything except /healthz and /feed/* needs the sign-in header that App Service sets."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings(tempfile.mkdtemp(prefix="dojo-feed-auth-"), local=False))
        cls.c = TestClient(cls.app)
        cls.owner = {"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "owner-1")}
        cls.token = cls.app.state.podcast.links.create()

    def test_the_api_still_needs_sign_in(self):
        for method, path in (("GET", "/api/me"), ("GET", "/api/feed"), ("POST", "/api/feed/link"), ("POST", "/api/feed/link/new"),
                             ("DELETE", "/api/feed/link"), ("GET", "/api/media/a" + "0" * 39)):
            self.assertEqual(self.c.request(method, path, headers=POST).status_code, 401, path)
        self.assertEqual(self.c.get("/api/feed", headers=self.owner).status_code, 200)
        stranger = {"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "someone-else")}
        self.assertEqual(self.c.post("/api/feed/link/new", headers={**POST, **stranger}).status_code, 403)

    def test_the_feed_needs_its_token_and_ignores_the_sign_in_header(self):
        self.assertEqual(self.c.get(f"/feed/{self.token}/gh-300.xml").status_code, 200)
        wrong = ("A" if self.token[0] != "A" else "B") + self.token[1:]
        for token in (wrong, "x"):
            r = self.c.get(f"/feed/{token}/gh-300.xml", headers=self.owner)
            self.assertEqual((r.status_code, r.content), (404, b""))
        self.c.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL": principal("tenant-1", "owner-1")})
        self.assertNotIn(b"Alex", self.c.get(f"/feed/{self.token}/gh-300.xml").content)

    def test_dot_segments_and_encoded_separators_never_reach_the_api(self):
        signed_in = ((b"x-ms-client-principal", self.owner["X-MS-CLIENT-PRINCIPAL"].encode()),)
        self.assertEqual(call(self.app, b"/api/me", headers=signed_in)[0], 200)
        for raw in (b"/feed/../api/me", b"/feed/%2e%2e/api/me", b"/feed/%2E%2E/api/me", b"/feed/.%2e/api/me",
                    b"/feed/..%2fapi%2fme", b"/feed/x%2F..%2F..%2Fapi%2Fme", b"/feed/..%5capi/me", b"/feed/..\\api\\me",
                    b"/feed/./api/me", b"/feed/x/..", b"/api/../feed/x", b"/feed/%252e%252e/api/me", b"/feed/%25/x",
                    b"/static/..%2f..%2fapi/me", b"/%2e%2e/api/me"):
            for headers in ((), signed_in):
                status, body = call(self.app, raw, headers=headers)
                self.assertEqual((status, body), (400, b""), raw)
        self.assertEqual(call(self.app, b"/feed/../api/feed/link", method="POST", headers=signed_in + ((b"x-dojo", b"1"),)), (400, b""))

    def test_only_healthz_and_feed_are_open_and_nothing_logs_request_urls(self):
        bicep = (ROOT / "infra" / "main.bicep").read_text("utf-8")
        excluded = re.search(r"excludedPaths:\s*\[(.*?)\]", bicep, re.S).group(1)
        # Plus the two static files the browser fetches outside any page (ADR 0013, tests/test_phone.py).
        self.assertEqual(re.findall(r"'([^']*)'", excluded),
                         ["/healthz", "/feed/*", "/cal/*", "/static/sw.js", "/static/icon-192.png"])
        # On Linux the file-system httpLogs switch keeps the container's console output, not request logs.
        # Request URLs would need an access log or an exported HTTP log, and there is neither.
        self.assertIn("linuxFxVersion", bicep)
        for setting in ("--access-log", "accesslog", "GUNICORN_CMD_ARGS", "diagnosticSettings", "AppServiceHTTPLogs"):
            self.assertNotIn(setting, bicep)
        # The App Service authentication module logs every request URL at Information (seen live on 29 Sep 2026).
        levels = re.search(r"applicationLogs:\s*\{\s*fileSystem:\s*\{\s*level:\s*'(\w+)'", bicep).group(1)
        self.assertIn(levels, ("Warning", "Error", "Off"), "request URLs, feed tokens included, would reach /home/LogFiles")
        self.assertFalse((ROOT / "gunicorn.conf.py").exists())
        self.assertTrue((APP_DIR / "static" / "podcast").is_dir())


if __name__ == "__main__":
    unittest.main()
