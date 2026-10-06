"""Startup self-test: can Dojo reach its three models, Speech both ways and the sign-in credential
exchange? /healthz reports only pass/fail; details are available to the signed-in owner at /api/diag.
The avatar video route is slow and costs a rendering job, so it is only probed when the owner asks."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from .ai import ROLES
from .core import Store, Tokens, iso

log = logging.getLogger("dojo.selftest")


class SelfTest:
    """Runs the self-test in a background thread and repeats it: every 6 hours after a pass, every 5
    minutes after a failure."""
    def __init__(self, settings: Any, store: Store, ai: Any, speech: Any, avatar: Any, tokens: Tokens | None):
        self.settings = settings
        self.store = store
        self.ai = ai
        self.speech = speech
        self.avatar = avatar
        self.tokens = tokens
        self.result: dict = {"state": "pending", "checks": {}, "details": {}, "at": None, "build": settings.build}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="selftest", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.run()
            except Exception:  # noqa: BLE001
                log.exception("self-test crashed")
            ok = all(self.result.get("checks", {}).values())
            time.sleep(6 * 3600 if ok else 300)

    def run(self, avatar: bool = False) -> dict:
        """The speaking check says a sentence and writes it down again: the round trip a spoken answer
        makes. It takes about a second, so the background loop runs it and the deploy gate sees it.
        `avatar` films a one-sentence clip to prove the video route works. That takes a minute and
        costs a rendering job, so only /api/diag/run asks for it."""
        checks: dict[str, bool] = {}
        details: dict[str, Any] = {}
        try:
            self.store.write("selftest-probe", value={"at": iso()})
            checks["data"] = bool(self.store.read("selftest-probe"))
        except Exception as e:  # noqa: BLE001
            checks["data"], details["data"] = False, f"{type(e).__name__}: {e}"
        for role in ROLES:
            started = time.monotonic()
            try:
                reply = self.ai.chat(role, "Reply with the single word ok. TASK: ping", "ping", json_mode=False)
                checks[role] = bool(reply.strip())
                details[role] = {"route": self.ai.route(role), "seconds": round(time.monotonic() - started, 1), "reply": reply.strip()[:30]}
            except Exception as e:  # noqa: BLE001
                checks[role], details[role] = False, f"{type(e).__name__}: {str(e)[:400]}"
        try:
            media = self.speech.synthesize([{"voice": "guide", "text": "Dojo is ready."}, {"voice": "coach", "text": "Let's train."}])
            checks["speech"] = self.speech.exists(media)
            details["speech"] = {"route": self.speech.route(), "media": media}
        except Exception as e:  # noqa: BLE001
            checks["speech"], details["speech"] = False, f"{type(e).__name__}: {str(e)[:400]}"
        try:
            probe = self.speech.probe_transcription()
            checks["stt"] = bool(probe.get("ok"))
            details["stt"] = probe
        except Exception as e:  # noqa: BLE001
            checks["stt"], details["stt"] = False, f"{type(e).__name__}: {str(e)[:200]}"
        if avatar:
            try:
                probe = self.avatar.probe()
                checks["avatar"] = bool(probe.get("ok"))
                details["avatar"] = probe
            except Exception as e:  # noqa: BLE001
                checks["avatar"], details["avatar"] = False, f"{type(e).__name__}: {str(e)[:400]}"
        try:
            details["video_storage"] = self.avatar.usage()
        except Exception as e:  # noqa: BLE001
            details["video_storage"] = f"{type(e).__name__}: {str(e)[:200]}"
        if self.settings.in_azure:
            checks["signin"], details["signin"] = self._signin()
        self.result = {"state": "done", "checks": checks, "details": details, "at": iso(), "build": self.settings.build}
        log.info("self-test: %s", {k: v for k, v in checks.items()})
        return self.result

    def _signin(self) -> tuple[bool, str]:
        """The web app signs users in without a secret: its managed identity's token is the client
        assertion for the sign-in app registration. Exchange it once to prove the trust is in place."""
        s = self.settings
        try:
            assertion = self.tokens.get("api://AzureADTokenExchange/.default")
            for scope in (f"{s.auth_client_id}/.default", "https://graph.microsoft.com/.default"):
                r = httpx.post(f"https://login.microsoftonline.com/{s.owner_tid}/oauth2/v2.0/token", timeout=30, data={
                    "client_id": s.auth_client_id, "grant_type": "client_credentials", "scope": scope,
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer", "client_assertion": assertion,
                })
                if r.status_code == 200:
                    return True, f"credential exchange works ({scope})"
                detail = r.json().get("error_description", "")[:300] if r.headers.get("content-type", "").startswith("application/json") else r.text[:300]
            return False, detail
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:300]}"

    def public(self) -> dict:
        """What /healthz shows anyone: "ok" only once every check of a finished run passed, otherwise
        "degraded" (also while the first run is pending), and the build the deploy smoke test waits for."""
        r = self.result
        ok = r["state"] == "done" and bool(r["checks"]) and all(r["checks"].values())
        return {"status": "ok" if ok else "degraded", "build": self.settings.build}
