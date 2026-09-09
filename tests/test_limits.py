"""The public endpoint's guardrails.

Every test here drives a fake clock. A rate limiter is defined entirely by what
it does at the edges of its windows -- one second before, one second after, and
under simultaneous arrivals -- and none of that is observable by sleeping.
"""

from __future__ import annotations

import threading

from agent.limits import HOUR, Decision, Gatekeeper, client_id


class Clock:
    """A hand-cranked monotonic clock."""

    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _keeper(clock: Clock, **over) -> Gatekeeper:
    base = {"per_client": 3, "daily": 10, "concurrent": 2}
    return Gatekeeper(clock=clock, **{**base, **over})


def _drain(keeper: Gatekeeper, client: str, n: int) -> None:
    """Take and immediately give back ``n`` slots, so only the windows move."""
    for _ in range(n):
        assert keeper.admit(client).allowed
        keeper.release()


# --------------------------------------------------------------------------- #
# per-visitor window
# --------------------------------------------------------------------------- #


def test_a_visitor_gets_their_quota_and_then_a_reason():
    clock = Clock()
    keeper = _keeper(clock)
    _drain(keeper, "1.2.3.4", 3)

    refused = keeper.admit("1.2.3.4")
    assert refused.allowed is False
    assert refused.reason == "per_visitor"
    assert refused.retry_after > 0, "a refusal must say when, not just no"
    assert "3 questions" in refused.message


def test_one_visitor_exhausting_their_quota_does_not_block_anyone_else():
    """The failure that would make the demo useless: the first judge to arrive
    locking out the second."""
    clock = Clock()
    keeper = _keeper(clock)
    _drain(keeper, "first", 3)

    assert keeper.admit("first").allowed is False
    assert keeper.admit("second").allowed is True


def test_the_window_rolls_rather_than_resetting_on_a_cliff():
    """Slots come back one at a time, as each ask ages out -- not all at once on
    the hour, which would just move the stampede to a predictable moment."""
    clock = Clock()
    keeper = _keeper(clock)
    _drain(keeper, "v", 1)
    clock.advance(600)
    _drain(keeper, "v", 2)

    assert keeper.admit("v").allowed is False

    clock.advance(HOUR - 600 + 1)      # only the first ask has aged out
    assert keeper.admit("v").allowed is True
    keeper.release()
    assert keeper.admit("v").allowed is False, "the other two are still in window"


def test_retry_after_is_when_the_slot_actually_opens():
    clock = Clock()
    keeper = _keeper(clock, per_client=1)
    _drain(keeper, "v", 1)
    clock.advance(HOUR - 30)

    refused = keeper.admit("v")
    assert refused.allowed is False
    assert 30 <= refused.retry_after <= 32

    clock.advance(refused.retry_after)
    assert keeper.admit("v").allowed is True, "the advertised wait must be enough"


def test_remaining_counts_down_so_the_ui_can_warn_before_the_wall():
    clock = Clock()
    keeper = _keeper(clock)
    seen = []
    for _ in range(3):
        seen.append(keeper.admit("v").remaining)
        keeper.release()
    assert seen == [2, 1, 0]


# --------------------------------------------------------------------------- #
# the daily budget -- the one that actually holds the bill
# --------------------------------------------------------------------------- #


def test_the_daily_budget_is_global_and_outranks_a_fresh_visitor():
    """A new IP is free to obtain; the budget must not be."""
    clock = Clock()
    keeper = _keeper(clock, per_client=100, daily=5)
    _drain(keeper, "someone", 5)

    refused = keeper.admit("a-brand-new-address")
    assert refused.allowed is False
    assert refused.reason == "daily_budget"


def test_a_spent_budget_refuses_before_it_talks_about_slots():
    """Ordering matters: 'busy, retry in 20s' is a lie once the day is gone."""
    clock = Clock()
    keeper = _keeper(clock, daily=1, concurrent=1, per_client=100)
    assert keeper.admit("v").allowed is True      # slot held, budget spent

    refused = keeper.admit("other")
    assert refused.reason == "daily_budget"
    assert refused.retry_after > HOUR


def test_a_refused_request_does_not_spend_budget():
    """Otherwise a bot that only ever gets 429s still drains the day for
    everyone -- the limiter would become the denial of service."""
    clock = Clock()
    keeper = _keeper(clock, per_client=1, daily=10)
    _drain(keeper, "v", 1)
    for _ in range(50):
        assert keeper.admit("v").allowed is False

    assert keeper.snapshot()["asks_today"] == 1


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_runs_are_capped_and_the_slot_comes_back():
    clock = Clock()
    keeper = _keeper(clock, concurrent=2, per_client=100)
    assert keeper.admit("a").allowed is True
    assert keeper.admit("b").allowed is True

    busy = keeper.admit("c")
    assert busy.allowed is False and busy.reason == "busy"
    assert busy.retry_after > 0

    keeper.release()
    assert keeper.admit("c").allowed is True


def test_a_double_release_cannot_mint_capacity():
    """A retry path that releases twice must not hand out a free slot."""
    clock = Clock()
    keeper = _keeper(clock, concurrent=1, per_client=100)
    assert keeper.admit("a").allowed is True
    keeper.release()
    keeper.release()
    keeper.release()

    assert keeper.admit("b").allowed is True
    assert keeper.admit("c").allowed is False, "still exactly one slot"


def test_simultaneous_arrivals_cannot_all_observe_the_same_count():
    """Check-then-increment as two steps is a race that lets a burst through.
    Admission decides and records under one lock, so exactly `daily` win."""
    clock = Clock()
    keeper = _keeper(clock, per_client=1000, daily=10, concurrent=1000)
    verdicts: list[Decision] = []
    start = threading.Barrier(16)

    def go() -> None:
        start.wait()
        verdicts.append(keeper.admit("swarm"))

    threads = [threading.Thread(target=go) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(verdicts) == 16
    assert sum(v.allowed for v in verdicts) == 10, "exactly the budget, no more"
    assert keeper.snapshot()["asks_today"] == 10


# --------------------------------------------------------------------------- #
# housekeeping
# --------------------------------------------------------------------------- #


def test_tracking_does_not_grow_without_bound():
    """Keying on a client-supplied address makes the limiter its own memory
    target. Aged-out visitors are forgotten."""
    clock = Clock()
    keeper = _keeper(clock, per_client=100, daily=100_000)
    for i in range(500):
        keeper.admit(f"ip-{i}")
        keeper.release()
    assert len(keeper._clients) == 500

    clock.advance(HOUR + 1)
    keeper.admit("someone-new")
    assert len(keeper._clients) == 1, "aged-out visitors are dropped"


def test_headers_tell_a_script_what_the_page_shows():
    allowed = Decision(allowed=True, remaining=4)
    assert allowed.headers == {"X-RateLimit-Remaining": "4"}

    refused = Decision(allowed=False, reason="busy", retry_after=20)
    assert refused.headers["Retry-After"] == "20"


def test_the_visitor_id_is_the_client_not_the_proxy():
    """Cloud Run appends the caller to X-Forwarded-For, so the first entry is
    the visitor and the rest are hops."""
    assert client_id("203.0.113.7, 130.211.0.1", "10.0.0.1") == "203.0.113.7"
    assert client_id("  198.51.100.4  ", "10.0.0.1") == "198.51.100.4"
    assert client_id("", "10.0.0.1") == "10.0.0.1", "local runs have no proxy"
    assert client_id("", "") == "unknown"
