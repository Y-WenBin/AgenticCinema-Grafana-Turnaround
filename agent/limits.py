"""Guardrails for the public endpoint: who may ask, how often, how many at once.

The public endpoint is deliberately open — no key, no signup, because someone
with a minute to spare will not create an account to try something once. Open and
*unbounded* is a different thing:
every ``/ask`` spawns a multi-agent run that makes real Gemini calls against a
real billing account, so the endpoint needs a spend ceiling that does not depend
on everyone being well-behaved.

Three limits, because they fail in three different ways:

* **per visitor** — one person hammering the box. Rolling window, so it drains
  gradually instead of resetting on a cliff everyone can queue up against.
* **daily total** — everyone, together. This is the actual budget: the worst
  case for a day is this number of runs, whatever the traffic looks like.
* **concurrent** — a run holds an ``mcp-grafana`` subprocess and a Gemini
  quota slot for ~40s. Unbounded concurrency does not spend more money, it just
  makes every simultaneous run slow enough to look broken.

**These counters live in the process, so they are per instance.** With
``--max-instances N`` the true ceiling is ``N x`` what is configured here, and a
cold start forgets the window early. That is a real weakness and it is stated
rather than papered over: exact global limits need shared state (Firestore,
Redis), which is a lot of machinery for a demo whose worst case is bounded at
two instances anyway. Pick the numbers so ``N x daily`` is a bill you would
still be happy to pay, and the arithmetic stays honest.

Pure and clock-injectable on purpose — the whole point of a rate limiter is the
behaviour at the boundaries, and you cannot test that by sleeping.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

HOUR = 3600
DAY = 86400

#: Stop tracking visitors once this many distinct ones are in memory. A rate
#: limiter keyed on client-supplied identity is itself a memory-exhaustion
#: target: an attacker rotating IPs would otherwise grow the dict forever.
#: Eviction is oldest-first and only ever *forgives* an ask, never invents one.
MAX_TRACKED_CLIENTS = 10_000


@dataclass(frozen=True, slots=True)
class Decision:
    """Why a request was let through or turned away.

    ``reason`` is for machines (tests, metrics, the UI's branching); ``message``
    is the sentence a person reads. A refusal always carries a ``retry_after``
    the caller can act on -- "no" without "when" is not an answer.
    """

    allowed: bool
    reason: str = ""
    message: str = ""
    retry_after: int = 0
    remaining: int = 0

    @property
    def headers(self) -> dict[str, str]:
        """Standard-ish rate-limit headers, for the curl-and-scripts audience."""
        out = {"X-RateLimit-Remaining": str(self.remaining)}
        if not self.allowed:
            out["Retry-After"] = str(self.retry_after)
        return out


@dataclass
class Gatekeeper:
    """Rolling-window admission control for the public ``/ask``.

    ``admit`` both decides *and* records, under one lock, because checking and
    then incrementing in two steps is a race that lets a burst of simultaneous
    requests all observe the same pre-increment count and sail through.
    """

    per_client: int = 8
    per_client_window: int = HOUR
    daily: int = 200
    daily_window: int = DAY
    concurrent: int = 2
    clock: Callable[[], float] = time.monotonic

    _clients: dict[str, deque[float]] = field(default_factory=dict, repr=False)
    _all: deque[float] = field(default_factory=deque, repr=False)
    _in_flight: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ----------------------------------------------------------------- #

    def admit(self, client_id: str) -> Decision:
        """Take a slot for ``client_id``, or explain why not.

        On success the caller **must** call :meth:`release` when the run ends,
        or the concurrency slot leaks and the endpoint bleeds capacity one
        request at a time. Use ``try/finally``.
        """
        with self._lock:
            now = self.clock()
            self._prune(now)
            # Read, do not insert. A refused caller used to leave an empty deque
            # behind: harmless for the window (the next `_prune` drops it) but
            # not for `MAX_TRACKED_CLIENTS`, which evicts from the *front* of an
            # insertion-ordered dict -- so a burst of refusals could forget the
            # oldest genuine visitors and hand them a fresh hourly allowance.
            # The entry is created below, where the slot is actually taken.
            mine = self._clients.get(client_id)
            seen = len(mine) if mine is not None else 0

            # Ordered hardest-first, so the refusal names the limit that will
            # actually keep blocking. Telling someone to retry in 20s for a
            # concurrency slot is a lie if the day's budget is already gone.
            if len(self._all) >= self.daily:
                return Decision(
                    allowed=False,
                    reason="daily_budget",
                    message=(
                        "The demo's daily budget is spent — it runs real Gemini "
                        "calls on a real bill. It refills gradually; the README "
                        "has a one-command local run that has no cap."
                    ),
                    retry_after=self._drains_in(self._all, self.daily_window, now),
                )

            if seen >= self.per_client:
                return Decision(
                    allowed=False,
                    reason="per_visitor",
                    message=(
                        f"That's {self.per_client} questions in an hour from you — "
                        "the cap that keeps the demo up for everyone else. Your "
                        "next slot opens shortly."
                    ),
                    retry_after=self._drains_in(mine, self.per_client_window, now),
                )

            if self._in_flight >= self.concurrent:
                return Decision(
                    allowed=False,
                    reason="busy",
                    message=(
                        "Every agent slot is busy right now. A run takes about a "
                        "minute; try again in a moment."
                    ),
                    retry_after=20,
                    remaining=self.per_client - seen,
                )

            self._clients.setdefault(client_id, deque()).append(now)
            self._all.append(now)
            self._in_flight += 1
            return Decision(allowed=True, remaining=self.per_client - seen - 1)

    def release(self) -> None:
        """Give back a concurrency slot. Never drops below zero: an extra
        release (a retry path calling it twice) must not mint free capacity."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def snapshot(self) -> dict[str, int]:
        """Current state, for ``/health`` and for a human deciding whether the
        service has capacity to show someone right now."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            return {
                "asks_today": len(self._all),
                "daily_budget": self.daily,
                "in_flight": self._in_flight,
                "concurrent_limit": self.concurrent,
                "per_visitor_hourly": self.per_client,
            }

    # ----------------------------------------------------------------- #

    def _prune(self, now: float) -> None:
        """Drop everything that has aged out of its window. Called under lock."""
        _expire(self._all, now - self.daily_window)
        cutoff = now - self.per_client_window
        for key in list(self._clients):
            entries = self._clients[key]
            _expire(entries, cutoff)
            if not entries:
                del self._clients[key]
        if len(self._clients) > MAX_TRACKED_CLIENTS:
            # Insertion-ordered dict: the oldest keys are the ones to forget.
            for key in list(self._clients)[: len(self._clients) - MAX_TRACKED_CLIENTS]:
                del self._clients[key]

    @staticmethod
    def _drains_in(entries: deque[float], window: int, now: float) -> int:
        """Seconds until the oldest entry ages out and a slot reopens."""
        if not entries:
            return 0
        return max(1, int(entries[0] + window - now) + 1)


def _expire(entries: deque[float], cutoff: float) -> None:
    while entries and entries[0] <= cutoff:
        entries.popleft()


def client_id(forwarded_for: str, peer: str) -> str:
    """Identify a visitor behind Cloud Run's proxy.

    Google's frontend appends the real client IP to ``X-Forwarded-For``, so the
    *first* entry is the closest thing to a caller identity. It is trivially
    spoofable — anyone can send the header — which is precisely why the daily
    budget above does not depend on it. The per-visitor window is a politeness
    mechanism for honest traffic; the global cap is the one holding the bill.
    """
    first = forwarded_for.split(",")[0].strip()
    return first or (peer or "unknown")
