"""Narration audio from Azure AI Speech (part of the Foundry resource), in two neural voices, and
writing down what the learner said (fast transcription).
Authentication is Microsoft Entra only; the resource has local (key) authentication disabled.

Spoken answers: the audio is sent once, used once and thrown away. Dojo never writes the audio or the
text it comes back with to disk, and never puts either in the log - not even in an error. The answer
the learner sends afterwards is kept exactly like a typed one.

Dojo's own narration and presenter videos go through the same transcription for the delivered-meaning
check (`delivery`). What comes back there is content-quality data about shared study material; it is
kept with the check's results, never in a learner's record, and never in the log."""
from __future__ import annotations

import io
import logging
import re
import threading
import time
import wave
from typing import Any
from xml.sax.saxutils import escape

import httpx

from .avatar import PRESENTERS
from .core import Store, Tokens, UserError, sha256

log = logging.getLogger("dojo.speech")
# The same person in Listen and in Watch: a role's narration voice is the voice its presenter is
# filmed with. `avatar` holds the one table and is never changed from here.
VOICES = {role: who["voice"] for role, who in PRESENTERS.items()}
OUTPUT_FORMAT = "audio-24khz-96kbitrate-mono-mp3"
MEDIA_ID = re.compile(r"^a[0-9a-f]{39}$")
SCOPE = "https://cognitiveservices.azure.com/.default"

# Fast transcription: one request, answer in about a second for a short clip.
# https://learn.microsoft.com/azure/ai-services/speech-service/fast-transcription-create
TRANSCRIBE_VERSION = "2024-11-15"
# What a browser can record. MediaRecorder gives webm/opus in Edge and Chrome, mp4 in Safari.
AUDIO_TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 200  # the page stops recording at three minutes; this is the same limit with a little slack
# The delivered-meaning check writes down Dojo's own narration and presenter videos. A presenter's
# video holds every turn of a lesson, so its limit is its own; the service takes files up to 500 MB.
MAX_CHECK_BYTES = 100 * 1024 * 1024
CHECK_TIMEOUT_S = 180.0
PROBE_TEXT = "Dojo writes down what you say."
PROBE_WORDS = ("dojo", "writes", "down", "say")
PROBE_FORMAT = "webm-24khz-16bit-mono-opus"
# A failure is told in plain words and never carries the service's answer, because that answer may
# contain what the learner said.
TRANSCRIBE_ERRORS = {
    400: "Dojo could not read that recording. Record it again, or type your answer.",
    401: "Dojo may not use Speech right now. Try again later, or type your answer.",
    403: "Dojo may not use Speech right now. Try again later, or type your answer.",
    413: "That recording is too long. Keep it under three minutes.",
    415: "Dojo cannot read that kind of audio. Type your answer instead.",
    429: "Speech is busy. Try again in a few seconds.",
}

# Team mode: Dojo decodes every learner recording itself and sends Speech its own copy, so the length it
# reserves for comes from the samples it counted, never from a field the client wrote (ADR 0009, "Money").
NORMALIZE_MAX_BYTES = 2 * 1024 * 1024
NORMALIZE_RATE = 16000                     # 16 kHz mono 16-bit PCM: 32,000 bytes a second
WAV_HEADER = 44
DECODE_TIMEOUT_S = 10.0
CONTAINERS = {"webm": "webm", "ogg": "ogg", "mp4": "mp4", "mp3": "mp3", "wav": "wav"}
CODECS = frozenset({"opus", "libopus", "vorbis", "libvorbis", "aac", "mp3", "mp3float",
                    "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8", "pcm_alaw", "pcm_mulaw"})
UNREADABLE = "Dojo could not read this recording; try again."
_DECODE = threading.Lock()   # one decode at a time


class AudioRefused(UserError):
    """A recording refused before anything is paid for: 415 (unreadable) or 413 (too long)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def normalize(data: bytes, content_type: str) -> tuple[bytes, float]:
    """Decode a learner's recording and write it again as a 16 kHz mono 16-bit PCM WAV. Returns the WAV and
    its exact length in seconds, from the samples Dojo counted. No duration, rate or size field in the
    upload is trusted. Refuses with 415 what cannot be decoded, has a video stream or a codec outside the
    allow-list, and with 413 more than MAX_AUDIO_SECONDS; decoding stops as soon as that is passed."""
    import av   # noqa: PLC0415 - only team mode decodes; PyAV bundles FFmpeg (EG-43, ADR 0009)

    fmt = CONTAINERS.get(AUDIO_TYPES.get(content_type, ""))
    if fmt is None:
        raise AudioRefused(415, TRANSCRIBE_ERRORS[415])
    if not data:
        raise AudioRefused(415, UNREADABLE)
    if len(data) > NORMALIZE_MAX_BYTES:
        raise AudioRefused(413, TRANSCRIBE_ERRORS[413])
    limit = MAX_AUDIO_SECONDS * NORMALIZE_RATE * 2
    pcm = bytearray()
    with _DECODE:
        deadline = time.monotonic() + DECODE_TIMEOUT_S
        try:
            with av.open(io.BytesIO(data), mode="r", format=fmt) as container:
                if container.streams.video or not container.streams.audio:
                    raise AudioRefused(415, UNREADABLE)
                stream = container.streams.audio[0]
                if stream.codec_context.name not in CODECS:
                    raise AudioRefused(415, UNREADABLE)
                resampler = av.AudioResampler(format="s16", layout="mono", rate=NORMALIZE_RATE)

                def take(frames: Any) -> None:
                    for out in frames:
                        pcm.extend(bytes(out.planes[0])[: out.samples * 2])
                    if len(pcm) > limit:
                        raise AudioRefused(413, TRANSCRIBE_ERRORS[413])
                    if time.monotonic() > deadline:
                        raise AudioRefused(415, UNREADABLE)

                for packet in container.demux(stream):
                    for frame in packet.decode():
                        take(resampler.resample(frame))
                take(resampler.resample(None))
        except AudioRefused:
            raise
        except Exception as e:  # noqa: BLE001 - anything FFmpeg cannot read is refused, never sent
            log.warning("a recording could not be decoded: %s", type(e).__name__)
            raise AudioRefused(415, UNREADABLE) from None
    if not pcm:
        raise AudioRefused(415, UNREADABLE)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(NORMALIZE_RATE)
        w.writeframes(bytes(pcm))
    wav = out.getvalue()
    return wav, (len(wav) - WAV_HEADER) / (NORMALIZE_RATE * 2)


def build_ssml(lines: list[dict]) -> str:
    merged: list[tuple[str, list[str]]] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        voice = VOICES.get(str(line.get("voice")), VOICES["guide"])
        if merged and merged[-1][0] == voice:
            merged[-1][1].append(text)
        else:
            merged.append((voice, [text]))
    body = "".join(
        f'<voice name="{voice}">' + ' <break time="300ms"/> '.join(escape(t) for t in texts) + "</voice>"
        for voice, texts in merged
    )
    return f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">{body}</speak>'


def media_id_for(lines: list[dict]) -> str:
    return "a" + sha256(build_ssml(lines))[:39]


class Speech:
    """Azure AI Speech: text to speech for narration, cached under media/ by content hash, and fast
    transcription for spoken answers. Signed in with Entra tokens; metered in team mode."""
    def __init__(self, settings: Any, tokens: Tokens, store: Store, http: httpx.Client | None = None):
        self.settings = settings
        self.tokens = tokens
        self.store = store
        self.http = http or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        self._route: tuple[str, str] | None = None
        self.meter: Any = None   # the Budget, in team mode (app/budget.py)

    def route(self) -> str | None:
        return f"{self._route[0]} ({self._route[1]})" if self._route else None

    def _candidates(self) -> list[tuple[str, str]]:
        if self._route:
            return [self._route]
        s = self.settings
        custom = f"{s.speech_endpoint}/tts/cognitiveservices/v1"
        regional = f"https://{s.speech_region}.tts.speech.microsoft.com/cognitiveservices/v1"
        return [(custom, "bearer"), (custom, "aad"), (regional, "aad")]

    def _authorization(self, style: str) -> str:
        token = self.tokens.get(SCOPE)
        return f"Bearer {token}" if style == "bearer" else f"Bearer aad#{self.settings.ai_resource_id}#{token}"

    def exists(self, media_id: str) -> bool:
        return bool(MEDIA_ID.match(media_id)) and self.store.exists("media", media_id + ".mp3")

    def synthesize(self, lines: list[dict], folder: tuple[str, ...] = ("media",)) -> str:
        """The audio's id: a hash of what is said, so the same words are only ever made once. `folder` is
        where it is kept: the shared media, or one learner's own folder for their private questions."""
        ssml = build_ssml(lines)
        if len(ssml) > 60000:
            raise UserError("This narration is too long for one audio file.")
        media_id = "a" + sha256(ssml)[:39]
        if not self.store.exists(*folder, media_id + ".mp3"):
            self.store.write_bytes(*folder, media_id + ".mp3", data=self._tts(ssml))
        return media_id

    def speak(self, text: str, output_format: str) -> bytes:
        """One spoken line as bytes, kept nowhere: the self-test's own voice for its round trip."""
        return self._tts(build_ssml([{"voice": "guide", "text": text}]), output_format)

    def _tts(self, ssml: str, output_format: str = OUTPUT_FORMAT) -> bytes:
        """Text to speech, reserved first in team mode: billed by characters, all known before the call."""
        meter = self.meter
        if meter is None:
            return self._call(ssml, output_format)
        bound = len(ssml) * meter.meter_rate("voice")
        ticket = meter.open_call("speech/tts", bound)
        try:
            return self._call(ssml, output_format)
        finally:
            meter.settle(ticket, bound)   # a failed attempt may still have been billed: the bound

    def _stt(self, audio: bytes, content_type: str, seconds: float | None = None, timeout: float = 60.0) -> dict:
        """Speech to text, reserved first in team mode. `seconds` is the length of audio Dojo wrote itself
        (normalize); without it (Dojo's own media, admin only) the call is metered, not bounded."""
        meter = self.meter
        if meter is None:
            return self._transcribe(audio, content_type, timeout=timeout)
        rate = meter.meter_rate("transcription")
        bounded = seconds is not None
        bound = (seconds + 1.0) * rate if bounded else 0.0
        ticket = meter.open_call("speech/transcription", bound, bounded=bounded)
        cost: float | None = None
        try:
            data = self._transcribe(audio, content_type, timeout=timeout)
            ms = data.get("durationMilliseconds") if isinstance(data, dict) else None
            cost = ms / 1000 * rate if isinstance(ms, (int, float)) and ms >= 0 else None
            return data
        finally:
            meter.settle(ticket, (bound if bounded else 0.0) if cost is None else cost)

    def transcribe(self, audio: bytes, content_type: str, seconds: float | None = None) -> dict:
        """Write down what the learner said. Nothing here is stored or logged: not the audio, not the
        text. The learner reads the text, corrects it if they want, and decides what to send. In team
        mode `audio` is the WAV Dojo wrote itself (normalize) and `seconds` its exact length."""
        if content_type not in AUDIO_TYPES:
            raise UserError(TRANSCRIBE_ERRORS[415])
        if not audio:
            raise UserError("There was no sound in that recording. Try again, or type your answer.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise UserError(TRANSCRIBE_ERRORS[413])
        data = self._stt(audio, content_type, seconds)
        phrases = data.get("combinedPhrases")
        first = phrases[0] if isinstance(phrases, list) and phrases and isinstance(phrases[0], dict) else {}
        text = str(first.get("text") or "").strip()
        ms = data.get("durationMilliseconds")
        seconds = round(ms / 1000, 1) if isinstance(ms, (int, float)) else 0.0
        if seconds > MAX_AUDIO_SECONDS:
            raise UserError(TRANSCRIBE_ERRORS[413])
        if not text:
            raise UserError("Dojo heard nothing in that recording. Check your microphone and try again.")
        return {"text": text, "seconds": seconds}

    def transcribe_words(self, audio: bytes, content_type: str) -> dict:
        """Write down Dojo's own audio or video for the delivered-meaning check: the same request as a
        learner's recording, with the checker's own size limit, and every word with where it is
        spoken, so one presenter's video can be split into turns. Nothing is logged."""
        if content_type not in AUDIO_TYPES:
            raise UserError("Speech cannot read this kind of file.")
        if not audio:
            raise UserError("The file is empty.")
        if len(audio) > MAX_CHECK_BYTES:
            raise UserError(f"The file is larger than {MAX_CHECK_BYTES // (1024 * 1024)} MB.")
        data = self._stt(audio, content_type, timeout=CHECK_TIMEOUT_S)
        phrases = data.get("combinedPhrases")
        first = phrases[0] if isinstance(phrases, list) and phrases and isinstance(phrases[0], dict) else {}
        ms = data.get("durationMilliseconds")
        found = []
        for phrase in data.get("phrases") or []:
            for w in (phrase.get("words") or []) if isinstance(phrase, dict) else []:
                if not isinstance(w, dict) or not w.get("text"):
                    continue
                start, length = w.get("offsetMilliseconds"), w.get("durationMilliseconds")
                if isinstance(start, (int, float)) and isinstance(length, (int, float)):
                    found.append({"text": str(w["text"]), "start": start / 1000, "end": (start + length) / 1000})
        return {"text": str(first.get("text") or "").strip(),
                "seconds": round(ms / 1000, 3) if isinstance(ms, (int, float)) else 0.0, "words": found}

    def probe_transcription(self) -> dict:
        """Prove the spoken-answer path works: say a fixed sentence, then write it down again through
        the very function a learner's recording goes through. The audio only lives in memory."""
        started = time.monotonic()
        audio = self.speak(PROBE_TEXT, PROBE_FORMAT)
        heard = self.transcribe(audio, "audio/webm")
        found = [w for w in PROBE_WORDS if w in heard["text"].lower()]
        return {"ok": len(found) >= 3, "route": self.route(), "seconds": round(time.monotonic() - started, 1),
                "said": PROBE_TEXT, "heard": heard["text"][:120], "words": f"{len(found)}/{len(PROBE_WORDS)}",
                "audio_bytes": len(audio)}

    def _transcribe(self, audio: bytes, content_type: str, timeout: float = 60.0) -> dict:
        url = f"{self.settings.speech_endpoint}/speechtotext/transcriptions:transcribe?api-version={TRANSCRIBE_VERSION}"
        # No phrase list: Learn documents one only from api-version 2025-10-15, and a list built from
        # the item would hand the learner words they did not say (EG-17).
        files = {
            "audio": (f"answer.{AUDIO_TYPES[content_type]}", audio, content_type),
            "definition": (None, '{"locales":["en-US"]}', "application/json"),
        }
        last = "not tried"
        for attempt in range(3):
            try:
                r = self.http.post(url, files=files, timeout=timeout,
                                   headers={"Authorization": self._authorization("bearer"), "User-Agent": "dojo"})
            except httpx.HTTPError as e:
                last = f"network: {type(e).__name__}"
            except Exception as e:  # noqa: BLE001 - token failure
                raise UserError("Dojo could not sign in to Speech. Try again later, or type your answer.") from e
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        raise UserError("Speech sent back something Dojo could not read. Try again.") from None
                # The body is never read, logged or shown: it can contain what the learner said.
                if r.status_code not in (408, 429, 500, 502, 503, 504):
                    raise UserError(TRANSCRIBE_ERRORS.get(r.status_code, f"Writing down what you said did not work (HTTP {r.status_code})."))
                last = f"HTTP {r.status_code}"
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        log.warning("transcription failed: %s", last)
        raise UserError(TRANSCRIBE_ERRORS[429] if last == "HTTP 429" else "Writing down what you said did not work. Try again, or type your answer.")

    def _call(self, ssml: str, output_format: str = OUTPUT_FORMAT) -> bytes:
        last = "not tried"
        for attempt in range(3):
            transient = False
            for url, style in self._candidates():
                try:
                    r = self.http.post(url, content=ssml.encode("utf-8"), headers={
                        "Authorization": self._authorization(style),
                        "Content-Type": "application/ssml+xml",
                        "X-Microsoft-OutputFormat": output_format,
                        "User-Agent": "dojo",
                    })
                except httpx.HTTPError as e:
                    last, transient = f"network: {type(e).__name__}", True
                    continue
                except Exception as e:  # noqa: BLE001 - token failure
                    last = f"token: {type(e).__name__}"
                    continue
                if r.status_code == 200 and len(r.content) > 500:
                    self._route = (url, style)
                    return r.content
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (429, 500, 502, 503, 504):
                    transient = True
                    break
            if not transient:
                break
            time.sleep(3 * (attempt + 1))
        raise UserError(f"Speech could not produce audio ({last}).")


class StubSpeech(Speech):
    """Writes a placeholder instead of calling Speech (tests and local work without models)."""

    STUB_TRANSCRIPT = "This is the local stub writing down what you said."

    def _call(self, ssml: str, output_format: str = OUTPUT_FORMAT) -> bytes:
        return b"ID3" + bytes(1024)

    def _transcribe(self, audio: bytes, content_type: str, timeout: float = 60.0) -> dict:
        return {"durationMilliseconds": 4200, "combinedPhrases": [{"text": self.STUB_TRANSCRIPT}]}

    def probe_transcription(self) -> dict:
        return {"ok": True, "route": "stub", "seconds": 0.0, "said": PROBE_TEXT, "heard": self.STUB_TRANSCRIPT,
                "words": f"{len(PROBE_WORDS)}/{len(PROBE_WORDS)}", "audio_bytes": 0}

    def route(self) -> str:
        return "stub"
