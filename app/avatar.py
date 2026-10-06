"""Avatar video for lesson narration, from Azure AI Speech batch avatar synthesis.

A slide's narration is a conversation: the guide explains and the coach, a fellow learner, asks. One
presenter reading both sides sounds like someone answering himself, so each speaker has their own
face. Consecutive lines by the same speaker are one *turn*.

The quota decides the shape of everything else. Creating a batch avatar synthesis is limited to
**two calls per minute** on an AI Services S0 resource, and that limit only moves by a support
request. Filming a turn per job (eighteen jobs for a normal lesson) therefore fails within a minute,
and several inputs in one job are refused without a storage account
("Multiple inputs are only supported when destinationContainerUrl is specified"), which tenant
policy does not allow us to have. So a lesson is **two jobs, one per speaker**: every turn that
speaker has in the lesson, in speaking order, in one SSML input, separated by a long break.

To find each turn again inside that one file we ask for ``subtitleType: "external_file"``. The
service then also returns an SRT with sentence-level cue times, which are matched back to the turns
by walking their text, and cross-checked against the long silence between turns. The front end plays
one file from a turn's start to its end and swaps the presenter tile between turns.

The video is a transparent WebM (VP9) cropped to an upper-body portrait, so the presenter appears to
stand on the slide with no box around them.

Authentication is Microsoft Entra only; the resource has local (key) authentication disabled.
The result of a finished job is a short-lived SAS URL, downloaded without the bearer token and
never written to the log.

Size, measured: an 18.35 second clip with these settings is 2,503,286 bytes, about 8.2 MB per minute
(VP9 with an alpha channel runs at roughly double its nominal bitrate). A seven-slide lesson with
about a minute of narration per slide is therefore roughly 57 MB, and the BUDGET_BYTES cap holds
about six hours of film. Videos are capped at BUDGET_BYTES in total and the least recently served
one is deleted first, with its timing sidecar; audio and lessons are never touched.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable
from xml.sax.saxutils import escape

import httpx

from .core import Store, Tokens, UserError, canonical, sha256

log = logging.getLogger("dojo.avatar")

SCOPE = "https://cognitiveservices.azure.com/.default"
API_VERSION = "2024-08-01"
MEDIA_ID = re.compile(r"^v[0-9a-f]{39}$")
SUFFIX = ".webm"
CONTENT_TYPE = "video/webm"

# The two people in a lesson's narration, matching the two narration voices. "guide" teaches, so he
# is Harry, the coach of the design; "coach" is the fellow learner who asks the questions.
PRESENTERS: dict[str, dict[str, str]] = {
    "guide": {"character": "harry", "style": "business", "voice": "en-US-AndrewMultilingualNeural"},
    "coach": {"character": "meg", "style": "business", "voice": "en-US-AvaMultilingualNeural"},
}
DEFAULT_SPEAKER = "guide"

# Property names and allowed values come from the batch synthesis property reference:
# https://learn.microsoft.com/azure/ai-services/speech-service/text-to-speech-avatar/batch-synthesis-avatar-properties
# A transparent background needs webm + vp9 + backgroundColor #00000000. The crop keeps the middle
# 640 columns of the 1920x1080 frame, which is the presenter from the waist up. The external
# subtitle file is what tells us where each turn starts and ends inside the one video.
VIDEO_CONFIG: dict[str, Any] = {
    "videoFormat": "webm",
    "videoCodec": "vp9",
    "backgroundColor": "#00000000",
    "subtitleType": "external_file",
    "bitrateKbps": 500,
    "videoCrop": {"topLeft": {"x": 640, "y": 0}, "bottomRight": {"x": 1280, "y": 1080}},
}
TIME_TO_LIVE_HOURS = 6  # the file is stored here; the service only has to keep it long enough to download

TURN_BREAK_MS = 1500  # silence between two turns: long enough to find the seam in the subtitles
LINE_BREAK_MS = 300   # silence between two lines of the same turn
MIN_TURN_GAP_S = 1.2  # a real seam measured at 1.6 s; anything shorter is not a turn boundary

# An AI Services S0 resource allows two "create batch avatar synthesis" calls per minute, for the
# whole resource, and only a support request changes that. Every create in this process therefore
# queues behind one pacer. Reads are not documented as unlimited either, so polling is unhurried.
CREATE_INTERVAL_S = 31.0
CREATE_ATTEMPTS = 5
RATE_WAIT_S = 60.0
POLL_INTERVAL_S = 10.0
RATE_LIMIT_STEP = "Waiting for the video service (limit: 2 per minute)"

BUDGET_BYTES = 3 * 1024 * 1024 * 1024  # the data dir is a 10 GB share shared with lessons and audio
MAX_SSML = 40000  # the whole job payload must stay well under the service limit of 500 KB
RENDER_TIMEOUT_S = 1800  # one job now holds a whole lesson's worth of one speaker
PROBE_TIMEOUT_S = 180  # the probe may have to wait its turn at the pacer first
TRANSIENT = (408, 429, 500, 502, 503, 504)

Progress = Callable[[str], None]


def presenter(speaker: str) -> dict[str, str]:
    return PRESENTERS.get(speaker) or PRESENTERS[DEFAULT_SPEAKER]


def avatar_config(speaker: str) -> dict:
    """The avatar settings for one speaker: who is filmed, plus how every clip is filmed."""
    who = presenter(speaker)
    return {"talkingAvatarCharacter": who["character"], "talkingAvatarStyle": who["style"], **VIDEO_CONFIG}


def turns(lines: list[dict]) -> list[tuple[str, list[str]]]:
    """The narration split into speaker turns, keeping the order it is spoken in. Consecutive lines
    by the same speaker become one clip, so a presenter is not cut off mid-thought."""
    merged: list[tuple[str, list[str]]] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        speaker = str(line.get("voice") or "")
        speaker = speaker if speaker in PRESENTERS else DEFAULT_SPEAKER
        if merged and merged[-1][0] == speaker:
            merged[-1][1].append(text)
        else:
            merged.append((speaker, [text]))
    return merged


def build_ssml(speaker: str, turn_texts: list[list[str]]) -> str:
    """One job for one speaker: all of their turns, in speaking order, in a single input.

    Turns are separated by a long break so the seam is unmistakable in the subtitles and on screen;
    lines inside a turn get a short one."""
    turn_break = f' <break time="{TURN_BREAK_MS}ms"/> '
    line_break = f' <break time="{LINE_BREAK_MS}ms"/> '
    spoken = [line_break.join(escape(t) for t in (str(x).strip() for x in texts) if t) for texts in turn_texts]
    body = turn_break.join(t for t in spoken if t)
    return (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="en-US">'
            f'<voice name="{presenter(speaker)["voice"]}">{body}</voice></speak>')


def media_id_for(speaker: str, turn_texts: list[list[str]]) -> str:
    """Same words, same presenter and same settings always give the same file, so an unchanged
    lesson is never filmed again. The speaker is inside the hash through their avatar settings."""
    return "v" + sha256(build_ssml(speaker, turn_texts) + "\n" + canonical(avatar_config(speaker)))[:39]


def lesson_turns(slides_lines: list[list[dict]]) -> tuple[dict[str, list[list[str]]], list[list[tuple[str, int]]]]:
    """Split a whole lesson into what each speaker has to film.

    Returns the turns per speaker in speaking order, plus where every turn sits in the lesson: one
    list per slide of (speaker, position in that speaker's job)."""
    per_speaker: dict[str, list[list[str]]] = {}
    layout: list[list[tuple[str, int]]] = []
    for lines in slides_lines:
        row: list[tuple[str, int]] = []
        for speaker, texts in turns(lines):
            bucket = per_speaker.setdefault(speaker, [])
            bucket.append(texts)
            row.append((speaker, len(bucket) - 1))
        layout.append(row)
    return per_speaker, layout


def normalise(text: str) -> str:
    """Letters and digits only, lowercased: what a subtitle cue and its narration have in common."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


_CUE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _at(m: "re.Match[str]", first: int) -> float:
    h, mi, s, frac = (m.group(first + i) for i in range(4))
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[dict]:
    """The cues of an SRT subtitle file, in order, as {"start", "end", "text"} in seconds."""
    cues: list[dict] = []
    for block in re.split(r"\r?\n[ \t]*\r?\n", text.replace("\ufeff", "").strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if _CUE.search(ln)), None)
        if timing is None:
            continue
        m = _CUE.search(lines[timing])
        body = " ".join(lines[timing + 1:]).strip()
        if body:
            cues.append({"start": _at(m, 1), "end": _at(m, 5), "text": body})
    return cues


def map_cues(cues: list[dict], turn_texts: list[list[str]]) -> list[dict]:
    """Where each turn starts and ends inside the one video.

    The service cuts its subtitles per sentence, not per turn ("Yes." is its own cue), so the cues
    are walked in order against the narration text with a cursor. A cue that does not continue the
    turn it should be in means the two have drifted apart, and a guess there would show the wrong
    presenter saying the wrong words, so it is an error instead. The long silence between turns is
    checked afterwards as a second opinion."""
    wanted = [normalise(" ".join(texts)) for texts in turn_texts]
    if not wanted or not all(wanted):
        raise UserError("Dojo cannot film narration without words in it.")
    spans: list[dict] = []
    current: dict | None = None
    turn, cursor = 0, 0
    for cue in cues:
        spoken = normalise(cue["text"])
        if not spoken:
            continue
        while turn < len(wanted) and not wanted[turn].startswith(spoken, cursor):
            if cursor < len(wanted[turn]):
                log.warning("subtitle cue %d does not continue turn %d", len(spans) + 1, turn + 1)
                raise UserError("Dojo could not follow the narration in the finished video.")
            if current:
                spans.append(current)
                current = None
            turn, cursor = turn + 1, 0
        if turn >= len(wanted):
            raise UserError("Dojo could not follow the narration in the finished video.")
        cursor += len(spoken)
        if current is None:
            current = {"start": round(cue["start"], 3), "end": round(cue["end"], 3)}
        else:
            current["end"] = round(cue["end"], 3)
    if current:
        spans.append(current)
    if len(spans) != len(wanted) or cursor != len(wanted[-1]):
        raise UserError("The finished video does not cover the whole narration.")
    for before, after in zip(spans, spans[1:]):
        if after["start"] - before["end"] < MIN_TURN_GAP_S:
            raise UserError("Dojo could not tell the presenter's turns apart in the finished video.")
    return spans


def safe_url(url: str) -> str:
    """A result URL carries a shared access signature; only the path may be written down."""
    return url.split("?", 1)[0]


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d},{round((seconds - whole) * 1000):03d}"


def retry_after(response: httpx.Response, default: float) -> float:
    """How long the service asked us to wait, within reason."""
    try:
        asked = float(response.headers.get("retry-after", ""))
    except ValueError:
        return default
    return min(max(asked, 1.0), 300.0)


class Pacer:
    """One create call every `interval` seconds for the whole process, whoever asks.

    The quota belongs to the Speech resource, not to a job or a thread, so this is deliberately a
    single queue: a caller holds the lock while it waits, and the next one starts its wait from the
    moment the previous call actually went out."""

    def __init__(self, interval: float, clock: Any = None):
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._last = 0.0

    def _time(self) -> Any:
        return self.clock or time

    def wait(self, notify: Progress | None = None) -> float:
        """Block until the next create may go out and return how long that took."""
        with self._lock:
            clock = self._time()
            waited = 0.0
            if self._last:
                waited = max(0.0, self._last + self.interval - clock.monotonic())
                if waited:
                    if notify:
                        notify(RATE_LIMIT_STEP)
                    clock.sleep(waited)
            self._last = clock.monotonic()
            return waited


PACER = Pacer(CREATE_INTERVAL_S)


class Avatar:
    """Azure AI Speech batch avatar synthesis: films a lesson as one video per presenter, paced to the
    service's limit on new jobs, and keeps the videos within their storage budget."""
    def __init__(self, settings: Any, tokens: Tokens, store: Store, http: httpx.Client | None = None,
                 pacer: Pacer | None = None):
        self.settings = settings
        self.tokens = tokens
        self.store = store
        self.http = http or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        self.pacer = pacer or PACER
        self._reached = False
        self._space = threading.Lock()  # both speakers render side by side; the budget is counted once at a time

    # ------------------------------------------------------------ stored files

    def route(self) -> str | None:
        return f"{self._base()} (bearer)" if self._reached else None

    def exists(self, media_id: str) -> bool:
        """A video counts as present only with its timings: without them nothing can be played."""
        return self.timings(media_id) is not None

    def timings(self, media_id: str) -> list[dict] | None:
        """Where every turn of this video starts and ends, or None if it has not been filmed."""
        if not MEDIA_ID.match(media_id) or not self.store.exists("media", media_id + SUFFIX):
            return None
        spans = self.store.read("media", media_id)
        return spans if isinstance(spans, list) and spans else None

    def touch(self, media_id: str) -> None:
        """Remember that this video was just served, so eviction can start with the coldest one."""
        if MEDIA_ID.match(media_id):
            self.store.touch("media", media_id + SUFFIX)
            self.store.touch("media", media_id + ".json")

    def videos(self) -> list[tuple[str, int, float]]:
        return self.store.file_stats("media", suffix=SUFFIX)

    def usage(self) -> dict:
        files = self.videos()
        return {"videos": len(files), "bytes": sum(size for _, size, _ in files), "budget_bytes": BUDGET_BYTES}

    def evict(self, incoming: int = 0) -> list[str]:
        """Delete the least recently served videos until the incoming one fits in the budget.

        Only files ending in .webm under "media" are considered, so narration audio and lessons can
        never be removed by this. A video's timing sidecar goes with it."""
        files = sorted(self.videos(), key=lambda f: f[2])
        total = sum(size for _, size, _ in files)
        removed = []
        while files and total + incoming > BUDGET_BYTES:
            name, size, _ = files.pop(0)
            media_id = name[: -len(SUFFIX)]
            self.store.delete_file("media", name)
            self.store.delete_file("media", media_id + ".json")
            total -= size
            removed.append(media_id)
        if removed:
            log.info("evicted %d video(s) to stay inside the %d MB budget", len(removed), BUDGET_BYTES // (1024 * 1024))
        return removed

    # ------------------------------------------------------------ rendering

    def synthesize(self, speaker: str, turn_texts: list[list[str]], progress: Progress | None = None) -> str:
        """Film every turn of one speaker in a single job and return its media id."""
        ssml = build_ssml(speaker, turn_texts)
        if len(ssml) > MAX_SSML:
            raise UserError("This lesson has too much narration for one video.")
        media_id = media_id_for(speaker, turn_texts)
        if self.exists(media_id):
            return media_id
        step = progress or (lambda note: None)
        meter = getattr(self, "meter", None)   # team mode: metered (admin only, so not limited)
        ticket = meter.open_call("speech/avatar", 0.0, bounded=False) if meter is not None else None
        cost = 0.0
        try:
            data, subtitles = self._render(media_id, ssml, avatar_config(speaker), turn_texts, step)
            spans = map_cues(parse_srt(subtitles), turn_texts)
            if meter is not None:
                seconds = max((float(s.get("end") or 0.0) for s in spans if isinstance(s, dict)), default=0.0)
                cost = seconds * meter.meter_rate("video")
        finally:
            if ticket is not None:
                meter.settle(ticket, cost)
        with self._space:
            self.evict(len(data))
            self.store.write_bytes("media", media_id + SUFFIX, data=data)
            self.store.write("media", media_id, value=spans)
        return media_id

    def lesson_clips(self, slides_lines: list[list[dict]]) -> list[list[dict]]:
        """What the lesson view shows: one list of turns per slide, in speaking order, with the
        video and the seconds to play once that speaker has been filmed."""
        per_speaker, layout = lesson_turns(slides_lines)
        media = {speaker: media_id_for(speaker, texts) for speaker, texts in per_speaker.items()}
        spans = {speaker: self.timings(media_id) for speaker, media_id in media.items()}
        out = []
        for row in layout:
            clips = []
            for speaker, position in row:
                found = spans.get(speaker) or []
                span = found[position] if position < len(found) else None
                clips.append({"speaker": speaker,
                              "media": media[speaker] if span else None,
                              "start": span["start"] if span else None,
                              "end": span["end"] if span else None})
            out.append(clips)
        return out

    def lesson_films(self, slides_lines: list[list[dict]]) -> list[tuple[str, list[list[str]]]]:
        """The jobs a lesson needs: one per speaker who says anything, the guide first."""
        per_speaker, _ = lesson_turns(slides_lines)
        return [(speaker, per_speaker[speaker]) for speaker in PRESENTERS if speaker in per_speaker]

    def probe(self) -> dict:
        """Prove the avatar route works without keeping a video: submit a very short job through the
        same pacer every lesson uses, and watch it long enough to see the service accept it. Nothing
        is downloaded and nothing is stored; the job expires on its own."""
        synthesis_id = "dojo-probe-" + sha256(str(time.time()))[:16]
        started = time.monotonic()
        self._create(synthesis_id, build_ssml(DEFAULT_SPEAKER, [["Dojo is ready."]]),
                     avatar_config(DEFAULT_SPEAKER), lambda note: None)
        status = "NotStarted"
        while time.monotonic() - started < PROBE_TIMEOUT_S:
            job = self._get(synthesis_id)
            status = str(job.get("status") or "")
            if status in ("Succeeded", "Failed"):
                break
            time.sleep(POLL_INTERVAL_S)
        return {"ok": status != "Failed", "status": status, "seconds": round(time.monotonic() - started, 1),
                "route": self.route()}

    def _render(self, synthesis_id: str, ssml: str, config: dict, turn_texts: list[list[str]],
                progress: Progress) -> tuple[bytes, str]:
        """The video bytes and the subtitle text of one finished job."""
        self._create(synthesis_id, ssml, config, progress)
        video_url, subtitle_url = self._wait(synthesis_id, progress)
        if not subtitle_url:
            raise UserError("The video service did not say when each turn is spoken.")
        progress("Collecting the film")
        return self._download(video_url, "video"), self._download(subtitle_url, "subtitles").decode("utf-8", "replace")

    def _base(self) -> str:
        endpoint = (self.settings.speech_endpoint or "").rstrip("/")
        if not endpoint:
            raise UserError("Video is not configured for this Dojo.")
        return f"{endpoint}/avatar/batchsyntheses"

    def _url(self, synthesis_id: str) -> str:
        return f"{self._base()}/{synthesis_id}?api-version={API_VERSION}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.tokens.get(SCOPE)}", "User-Agent": "dojo"}

    def _call(self, method: str, synthesis_id: str, what: str, **kwargs: Any) -> httpx.Response:
        """One request, retried while the service says it is busy or briefly broken."""
        last = "not tried"
        for attempt in range(4):
            try:
                r = self.http.request(method, self._url(synthesis_id), headers=self._headers(), **kwargs)
            except httpx.HTTPError as e:
                last = f"network: {type(e).__name__}"
            except Exception as e:  # noqa: BLE001 - a token failure must read like everything else
                raise UserError(f"Dojo could not sign in to the video service ({type(e).__name__}).") from e
            else:
                if r.status_code < 400:
                    self._reached = True
                    return r
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code not in TRANSIENT:
                    break
                if r.status_code == 429:
                    time.sleep(retry_after(r, RATE_WAIT_S))
                    continue
            time.sleep(3 * (attempt + 1))
        log.warning("avatar %s failed: %s", what, last)
        raise UserError(f"The video service could not {what} ({last}).")

    def _create(self, synthesis_id: str, ssml: str, config: dict, progress: Progress) -> None:
        """Start one job, waiting for this process's turn first and for the service's if it says so.

        The id is the content hash, so a job left over from an interrupted attempt is the job we
        want: 409 means it is already running and there is nothing to pay for again."""
        body = {
            "inputKind": "SSML",
            "inputs": [{"content": ssml}],
            "avatarConfig": config,
            "properties": {"timeToLiveInHours": TIME_TO_LIVE_HOURS},
        }
        last = "not tried"
        for attempt in range(CREATE_ATTEMPTS):
            if self.pacer.wait(progress):
                progress("Starting the film")
            try:
                r = self.http.put(self._url(synthesis_id), headers=self._headers(), json=body)
            except httpx.HTTPError as e:
                last = f"network: {type(e).__name__}"
            except Exception as e:  # noqa: BLE001 - a token failure must read like everything else
                raise UserError(f"Dojo could not sign in to the video service ({type(e).__name__}).") from e
            else:
                if r.status_code < 400:
                    self._reached = True
                    return
                if r.status_code == 409:
                    log.info("avatar job %s was already started; waiting for it", synthesis_id)
                    self._reached = True
                    return
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code == 429:
                    # Two creates a minute for the whole resource: somebody else got there first.
                    progress(RATE_LIMIT_STEP)
                    time.sleep(retry_after(r, RATE_WAIT_S))
                    continue
                if r.status_code not in TRANSIENT:
                    break
            time.sleep(3 * (attempt + 1))
        log.warning("avatar create failed: %s", last)
        raise UserError(f"The video service could not start a video ({last}).")

    def _get(self, synthesis_id: str) -> dict:
        r = self._call("GET", synthesis_id, "check a video")
        try:
            payload = r.json()
        except ValueError:
            raise UserError("The video service sent an answer Dojo could not read.")
        return payload if isinstance(payload, dict) else {}

    def _wait(self, synthesis_id: str, progress: Progress) -> tuple[str, str]:
        """Poll until the job is done and return the video and subtitle URLs."""
        started = time.monotonic()
        while True:
            job = self._get(synthesis_id)
            status = str(job.get("status") or "")
            if status == "Succeeded":
                outputs = job.get("outputs") or {}
                url = str(outputs.get("result") or "")
                if not url:
                    raise UserError("The video service finished without giving Dojo a video.")
                seconds = round((job.get("properties") or {}).get("durationInMilliseconds", 0) / 1000)
                log.info("avatar video ready: %s (%s s)", safe_url(url).rsplit("/", 1)[-1], seconds)
                return url, str(outputs.get("subtitle") or "")
            if status == "Failed":
                reason = str((job.get("properties") or {}).get("error") or "")[:200]
                raise UserError(f"The video could not be made. {reason}".strip())
            if time.monotonic() - started > RENDER_TIMEOUT_S:
                raise UserError("The video is taking too long. Try again later.")
            progress(f"Filming the presenter ({status or 'waiting'})")
            time.sleep(POLL_INTERVAL_S)

    def _download(self, url: str, what: str) -> bytes:
        """The result URL already carries its own signature, so no bearer token is sent with it."""
        last = "not tried"
        smallest = 100 if what == "video" else 8
        for attempt in range(3):
            try:
                r = self.http.get(url, headers={"User-Agent": "dojo"})
            except httpx.HTTPError as e:
                last = f"network: {type(e).__name__}"
            else:
                if r.status_code == 200 and len(r.content) >= smallest:
                    return r.content
                last = f"HTTP {r.status_code}"
                if r.status_code not in TRANSIENT:
                    break
            time.sleep(3 * (attempt + 1))
        log.warning("could not download %s %s: %s", what, safe_url(url), last)
        raise UserError(f"Dojo could not download the finished {what} ({last}).")


class StubAvatar(Avatar):
    """Writes a placeholder instead of calling the avatar service (tests and local work offline).

    The subtitles are made up the way the service makes them: one cue per line, a short gap inside a
    turn and a long one between turns, so the mapping from cues to turns is exercised offline too."""

    def _render(self, synthesis_id: str, ssml: str, config: dict, turn_texts: list[list[str]],
                progress: Progress) -> tuple[bytes, str]:
        progress("Filming the presenter (stub)")
        cues, at = [], 0.05
        for texts in turn_texts:
            for line in (str(t).strip() for t in texts):
                if not line:
                    continue
                end = at + max(1.0, len(line) / 15)
                cues.append((at, end, line))
                at = end + LINE_BREAK_MS / 1000
            at += (TURN_BREAK_MS - LINE_BREAK_MS) / 1000
        srt = "\n\n".join(f"{i}\n{_stamp(s)} --> {_stamp(e)}\n{text}" for i, (s, e, text) in enumerate(cues, 1))
        return b"\x1a\x45\xdf\xa3" + sha256(ssml)[:32].encode() + bytes(1024), srt + "\n"

    def probe(self) -> dict:
        return {"ok": True, "status": "Succeeded", "seconds": 0.0, "route": "stub"}

    def route(self) -> str:
        return "stub"
