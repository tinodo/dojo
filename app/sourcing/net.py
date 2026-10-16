from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..core import UserError
from ..sources import ALLOWED_HOSTS, check_url, retry_after

_RETRY = {408, 429, 500, 502, 503, 504}


class SourcingError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Net:
    def __init__(
        self,
        sources,
        per_second: float = 5.0,
        tries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.sources = sources
        self.per_second = max(0.01, float(per_second))
        self.tries = max(1, int(tries))
        self.sleep = sleep
        self.clock = clock
        self._lock = threading.RLock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def get_json(self, url: str, docs: bool = False) -> Any:
        try:
            url = check_url(url)
        except UserError as e:
            raise SourcingError(str(e)) from e
        self._host(url)
        return self._retry(url, lambda: self.sources.data(url, docs=docs))

    def get_page_head(self, url: str, limit: int = 150_000) -> tuple[str, str]:
        try:
            url = check_url(url)
        except UserError as e:
            raise SourcingError(str(e)) from e
        self._host(url)
        final, text = self._retry(url, lambda: self.sources.page(url, docs=True))
        return final, text[:limit]

    def post_json(self, url: str, body: dict, headers: dict | None = None) -> tuple[str, dict]:
        try:
            url = check_url(url)
        except UserError as e:
            raise SourcingError(str(e)) from e
        self._host(url)

        def call() -> tuple[str, dict]:
            response = self.sources.post(url, body, headers=headers)
            if response.status_code in _RETRY:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if 400 <= response.status_code < 500:
                raise SourcingError(f"HTTP {response.status_code}: {response.text}", status=response.status_code)
            if response.status_code >= 500:
                raise SourcingError(f"HTTP {response.status_code}: {response.text}", status=response.status_code)
            return response.text, {str(k).lower(): v for k, v in response.headers.items()}

        return self._retry(url, call)

    def _host(self, url: str) -> str:
        host = urlsplit(url).hostname or ""
        if host not in ALLOWED_HOSTS:
            raise SourcingError(f"Not an allowed source: {url}")
        return host

    def _retry(self, url: str, call: Callable[[], Any]) -> Any:
        host = self._host(url)
        for attempt in range(1, self.tries + 1):
            self._take(host)
            try:
                return call()
            except SourcingError as e:
                if attempt >= self.tries or e.status not in _RETRY:
                    raise
                self.sleep(self._backoff(attempt))
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status not in _RETRY or attempt >= self.tries:
                    raise SourcingError(f"HTTP {status}: {e.response.text}", status=status) from e
                self.sleep(self._backoff(attempt, e.response))
            except httpx.TransportError as e:
                if attempt >= self.tries:
                    raise SourcingError(f"Transport error: {e}") from e
                self.sleep(self._backoff(attempt))
            except UserError as e:
                raise SourcingError(str(e)) from e
            except ValueError as e:
                raise SourcingError("The site answered with something Dojo cannot read.") from e
            except json.JSONDecodeError as e:
                raise SourcingError("The site answered with something Dojo cannot read.") from e
        raise SourcingError("The request failed.")

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        wait = retry_after(response) if response is not None else None
        if wait is None:
            wait = min(30.0, float(2**attempt))
        return min(60.0, max(0.0, wait))

    def _take(self, host: str) -> None:
        rate = self.per_second
        cap = max(1.0, rate)
        while True:
            now = self.clock()
            with self._lock:
                tokens, seen = self._buckets.get(host, (cap, now))
                tokens = min(cap, tokens + max(0.0, now - seen) * rate)
                if tokens >= 1.0:
                    self._buckets[host] = (tokens - 1.0, now)
                    return
                wait = (1.0 - tokens) / rate
                self._buckets[host] = (tokens, now)
            self.sleep(wait)
