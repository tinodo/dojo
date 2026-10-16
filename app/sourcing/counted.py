"""The models of one run, counted (ADR 0018): every attempt to ask a model draws on the exam's model calls of the day,
a repeat of a malformed answer and the repair of a quote included, so a day's cap can stop the work."""
from __future__ import annotations

from typing import Any, Callable


class Counted:
    """`ai` with every `chat` and `web_search` counted first. `count` raises when the day's calls are used."""

    def __init__(self, ai: Any, count: Callable[[], None]):
        self._ai = ai
        self._count = count

    def chat(self, role: str, system: str, user: str, json_mode: bool = True) -> str:
        self._count()
        return self._ai.chat(role, system, user, json_mode)

    def web_search(self, query: str, domains: tuple[str, ...] = (), max_searches: int = 1) -> dict:
        self._count()
        return self._ai.web_search(query, domains=domains, max_searches=max_searches)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ai, name)
