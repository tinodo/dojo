"""One coordinator for all background work in team mode (ADR 0009, "Spare slots, not 'nobody is busy'").

With one learner, each background service runs its own loop and waits until the owner has no job at all.
With many members someone is nearly always busy, so that rule would starve shared content work. In team
mode the services stop running their own loops; this one thread calls each service's `step()` (one small
batch) in a fixed rotation. The services still decide *what* to do next; this decides *when*:

- At most one background batch runs at a time.
- A batch starts only in a spare slot: `Jobs.take_spare()` checks, inside the jobs lock, that two of the
  three work slots stay free for learners and nobody is waiting for one, and takes one in the same step.
  Interactive work therefore always has two slots, and never waits behind a background batch.
- The rotation is real: after a batch, or a service with nothing due, the turn passes to the next service.
  While no slot is spare, the same service keeps its turn and the thread waits for a slot to come back.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("dojo.background")

SPARE_WAIT_S = 5      # how long to wait for a slot to come back before checking again
ERROR_WAIT_S = 600    # a step that crashed is tried again after ten minutes
IDLE_WAIT_S = 1800    # the longest the thread sleeps when nothing is due


class Service:
    """One background service as the scheduler sees it."""

    def __init__(self, name: str, step: Callable[[], Any], delay: float, now: float,
                 wait: Callable[[Any], float] | None = None, wake: threading.Event | None = None):
        self.name = name
        self.step = step
        self.wait = wait or (lambda result: float(result))
        self.wake = wake            # the service asks to be looked at now (the Preparer, when an exam is added)
        self.due = now + delay      # the first step waits as long as the service's own loop would
        self.runs = 0
        self.last: float | None = None


class BackgroundScheduler:
    """Team mode's one background loop: one batch of one service at a time, in a fixed rotation, and only
    in a spare job slot."""
    def __init__(self, jobs: Any, clock: Callable[[], float] = time.monotonic):
        self.jobs = jobs
        self.clock = clock
        self.services: list[Service] = []
        self.state: dict = {"current": None, "note": "", "turns": 0}
        self._turn = 0
        self._one = threading.Lock()   # at most one background batch at a time
        self._started = False

    def add(self, name: str, step: Callable[[], Any], delay: float = 0, wait: Callable[[Any], float] | None = None,
            wake: threading.Event | None = None, service: Any = None) -> None:
        """Adds a service to the rotation. `service` is told it is scheduled, so it no longer waits for an
        idle owner, takes no slot of its own, and its own loop is never started."""
        if service is not None:
            service.scheduled = True
            service._started = True
        self.services.append(Service(name, step, delay, self.clock(), wait, wake))

    def start(self) -> None:
        if self._started or not self.services:
            return
        self._started = True
        threading.Thread(target=self._loop, name="background", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                wait = self.turn()
            except Exception:  # noqa: BLE001 - never let the coordinator die
                log.exception("the background scheduler crashed")
                wait = ERROR_WAIT_S
            if wait is None:
                self.jobs.slots.wait_release(SPARE_WAIT_S)
            elif wait > 0:
                self._sleep(wait)

    def _sleep(self, seconds: float) -> None:
        """Sleeps until the next service is due, waking early when a service asks to be looked at."""
        end = self.clock() + seconds
        while (left := end - self.clock()) > 0:
            if any(s.wake is not None and s.wake.is_set() for s in self.services):
                return
            time.sleep(min(1.0, left))

    def _woken(self, now: float) -> None:
        for s in self.services:
            if s.wake is not None and s.wake.is_set():
                s.wake.clear()
                s.due = min(s.due, now)

    def turn(self) -> float | None:
        """One turn of the rotation. Returns the seconds until a service is due (0: go again now), or None
        when the service whose turn it is waits for a spare slot; it keeps its turn."""
        if not self.services:
            return IDLE_WAIT_S
        now = self.clock()
        self._woken(now)
        n = len(self.services)
        for k in range(n):
            service = self.services[(self._turn + k) % n]
            if service.due <= now:
                break
        else:
            self.state.update(current=None, note="nothing is due")
            return max(1.0, min(min(s.due for s in self.services) - now, IDLE_WAIT_S))
        self._turn = (self._turn + k) % n
        if not self.jobs.take_spare():
            self.state.update(current=None, note="waiting for a spare work slot")
            return None
        with self._one:
            self.state.update(current=service.name, note="")
            try:
                wait = service.wait(service.step())
            except Exception:  # noqa: BLE001 - one service's crash never stops the others
                log.exception("background step %s crashed", service.name)
                wait = ERROR_WAIT_S
            finally:
                self.jobs.slots.release()
                self.state["current"] = None
        done = self.clock()
        service.due = done + max(0.0, float(wait))
        service.runs += 1
        service.last = done
        self.state["turns"] += 1
        self._turn = (self._turn + 1) % n
        return 0

    def status(self) -> dict:
        return {"started": self._started, "current": self.state["current"], "note": self.state["note"],
                "services": [{"name": s.name, "runs": s.runs, "due_in": max(0, round(s.due - self.clock()))}
                             for s in self.services]}
