"""The public backend view, and the privacy floor as a property of the query.

The control room is the one surface that hands a stranger real numbers out of
the studio's Grafana stack. Three things have to stay true for that to be safe,
and each has a test here rather than a comment:

  * a visitor cannot steer a query -- there is no parameter to steer with;
  * a crew number cannot describe a pool below ``MIN_POOL_SIZE``, because the
    floor is in the PromQL, not in the caller's discipline;
  * one viewer and a thousand viewers cost Grafana the same.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent import backend
from agent.backend import BOARD, BackendUnavailable, BoardCache, Tile, build, view
from agent.config import REPO_ROOT, Settings
from bridge.privacy import MIN_POOL_SIZE

CFG = Settings(
    grafana_url="https://stack.grafana.net",
    grafana_token="glsa_secret",
    gcp_project="proj",
    gcp_location="us-central1",
    mcp_grafana_bin="/bin/mcp-grafana",
)


class Clock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _fake(results: dict[str, list] | None = None, fail: set[str] | None = None):
    """A stand-in for the Grafana round trip that records what it was asked."""
    calls: list[str] = []

    def run(cfg: Settings, tile: Tile) -> list:
        calls.append(tile.key)
        if fail and tile.key in fail:
            raise BackendUnavailable("boom")
        return (results or {}).get(tile.key, [{"metric": {}, "value": [0, "1"]}])

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- #
# there is nothing to steer
# --------------------------------------------------------------------------- #


def test_the_endpoint_accepts_no_input_at_all():
    """The strongest version of an allowlist is not validating a request -- it is
    having nowhere to put one. If a query parameter ever appears on this route,
    someone has opened a door that this module's docstring says is not there."""
    from agent.serve import app

    spec = app.openapi()["paths"]["/api/backend"]["get"]
    assert not spec.get("parameters"), spec.get("parameters")
    assert "requestBody" not in spec


def test_no_tile_query_interpolates_anything():
    """Every query is a literal. A `{}` or an f-string hole reaching PromQL is
    how a fixed board quietly becomes a query API."""
    for tile in BOARD:
        assert "{{" not in tile.query
        assert "%s" not in tile.query


# --------------------------------------------------------------------------- #
# the privacy floor, in the query
# --------------------------------------------------------------------------- #

FLOOR_CLAUSE = f"turnaround_pool_headcount[{backend.LOOKBACK}])) >= {MIN_POOL_SIZE}"


def test_every_crew_query_carries_the_floor():
    """`bridge/privacy.py` forbids the *agent* from naming a pool of two. This
    asserts the public board cannot render one either -- not because the code
    remembers to filter, but because the pool is not in the result set."""
    crew = [t for t in BOARD
            if "artist_hours_logged" in t.query or "pool_headcount" in t.query]
    assert crew, "no crew tile found -- did a rename skip this test?"
    for tile in crew:
        assert FLOOR_CLAUSE in tile.query, tile.key


def test_the_floor_in_the_query_tracks_the_floor_in_the_code():
    """If someone raises MIN_POOL_SIZE to 5, a board still filtering at 3 would
    keep publishing pools the rest of the system has decided are too small."""
    assert f">= {MIN_POOL_SIZE}" in FLOOR_CLAUSE
    assert any(FLOOR_CLAUSE in t.query for t in BOARD)


def test_the_response_never_names_the_stack():
    """A public endpoint handing out the stack hostname is a free pointer at the
    login page. The token and the datasource uids are worse."""
    board = build(CFG, query=_fake())
    blob = json.dumps(board)
    for secret in (CFG.grafana_url, CFG.grafana_token, CFG.ds_prom, CFG.ds_loki,
                   "grafana.net", "grafanacloud"):
        assert secret not in blob, secret


# --------------------------------------------------------------------------- #
# empty is not the same as zero
# --------------------------------------------------------------------------- #


def test_no_privacy_breaches_reads_as_zero_not_as_no_data():
    """Loki returns an empty result when nothing matched, and "nothing matched"
    for the breach counter is the single best fact on the page. Rendering it as
    "--" would hide a clean record behind what looks like a broken panel."""
    board = build(CFG, query=_fake(results={"privacy_breaches": []}))
    tile = next(t for t in board["tiles"] if t["key"] == "privacy_breaches")
    assert tile["value"] == 0
    assert tile["display"] == "0"


def test_a_score_with_no_data_is_not_faked_into_a_zero():
    """The opposite case: a mean grounding score of 0.00 would read as "the
    judges failed everything", which is a very different claim from "no runs
    have been scored yet"."""
    board = build(CFG, query=_fake(results={"grounding": []}))
    tile = next(t for t in board["tiles"] if t["key"] == "grounding")
    assert tile["value"] is None
    assert tile["display"] == "--"


def test_every_metric_tile_outlives_a_gap_between_seed_runs():
    """The show re-seeds every 15 minutes and Prometheus drops a series 5 minutes
    after its last sample, so a bare `sum(metric)` is empty for two thirds of
    every cycle. Reading back over a window wider than the cycle is what keeps
    the board from blinking out between runs."""
    for tile in (t for t in BOARD if t.source == "prom"):
        assert "max_over_time(" in tile.query, tile.key
        assert f"[{backend.LOOKBACK}]" in tile.query, tile.key


# --------------------------------------------------------------------------- #
# one query, however many viewers
# --------------------------------------------------------------------------- #


def test_a_thousand_viewers_cost_grafana_one_query():
    clock = Clock()
    cache = BoardCache(ttl=30.0, clock=clock)
    run = _fake()
    for _ in range(1_000):
        view(CFG, cache=cache, query=run)
    assert cache.builds == 1
    # One build, and one round trip per tile within it -- not 1_000 x 9.
    assert len(run.calls) == len(BOARD)


def test_the_cache_lets_go_after_its_ttl():
    clock = Clock()
    cache = BoardCache(ttl=30.0, clock=clock)
    calls = []
    cache.get(lambda: (calls.append(1), {"tiles": []})[1])
    clock.advance(29.9)
    assert cache.get(lambda: (calls.append(1), {"tiles": []})[1])["cached"] is True
    clock.advance(0.2)
    assert cache.get(lambda: (calls.append(1), {"tiles": []})[1])["cached"] is False
    assert len(calls) == 2


def test_simultaneous_first_viewers_still_only_spend_one_query():
    """The build happens under the lock precisely so that a cold cache hit by
    twenty threads at once does not fan out into twenty upstream queries."""
    cache = BoardCache(ttl=30.0)
    start = threading.Barrier(20)
    builds = []

    def build_once():
        builds.append(1)
        return {"tiles": []}

    def worker():
        start.wait()
        cache.get(build_once)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(builds) == 1


def test_a_grafana_blip_serves_the_last_good_board_rather_than_a_hole():
    clock = Clock()
    cache = BoardCache(ttl=30.0, clock=clock)
    cache.get(lambda: {"tiles": [{"key": "x"}]})
    clock.advance(31)

    def boom():
        raise BackendUnavailable("grafana said no")

    out = cache.get(boom)
    assert out["stale"] is True
    assert out["tiles"] == [{"key": "x"}]


def test_a_blip_before_the_first_success_is_an_honest_failure():
    """With nothing cached there is no last-good board to fall back to, and the
    endpoint must say so rather than invent an empty one."""
    cache = BoardCache()
    with pytest.raises(BackendUnavailable):
        cache.get(lambda: (_ for _ in ()).throw(BackendUnavailable("cold")))


# --------------------------------------------------------------------------- #
# degrading, not collapsing
# --------------------------------------------------------------------------- #


def test_one_slow_datasource_does_not_blank_the_others():
    board = build(CFG, query=_fake(fail={"grounding", "judge_pass_rate"}))
    by_key = {t["key"]: t for t in board["tiles"]}
    assert by_key["grounding"]["error"] == "unavailable"
    assert "error" not in by_key["shots_approved"]


def test_a_board_that_failed_completely_is_not_served_as_an_empty_one():
    with pytest.raises(BackendUnavailable):
        build(CFG, query=_fake(fail={t.key for t in BOARD}))


def test_an_unconfigured_deployment_says_so_instead_of_calling_nowhere():
    bare = Settings(grafana_url="", grafana_token="", gcp_project="p",
                    gcp_location="us-central1", mcp_grafana_bin="/bin/x")
    with pytest.raises(BackendUnavailable):
        build(bare)


# --------------------------------------------------------------------------- #
# the dashboards a human looks at
# --------------------------------------------------------------------------- #

DASHBOARDS = REPO_ROOT / "grafana" / "dashboards"


def _panels(name: str) -> list[dict]:
    return json.loads((DASHBOARDS / name).read_text())["panels"]


def _exprs(panel: dict) -> list[str]:
    return [t.get("expr", "") for t in (panel.get("targets") or [])]


def test_the_crew_board_aggregates_only_pools_above_the_floor():
    """One panel is allowed to show the small pools, and it has to say in its own
    title that it is internal -- so that anyone reaching for the share button has
    been told, on the board itself, what they would be sharing."""
    for panel in _panels("turnaround-crew.json"):
        internal = "internal only" in (panel.get("title") or "").lower()
        for expr in _exprs(panel):
            if "by (pool)" not in expr:
                continue
            floored = f"turnaround_pool_headcount) >= {MIN_POOL_SIZE}" in expr
            subfloor = f"< {MIN_POOL_SIZE}" in expr
            assert floored or (subfloor and internal), (panel.get("title"), expr)


def test_exactly_one_crew_panel_may_show_the_small_pools():
    marked = [p for p in _panels("turnaround-crew.json")
              if "internal only" in (p.get("title") or "").lower()]
    assert len(marked) == 1


def test_no_dashboard_renders_an_unfiltered_log_stream():
    """A bare stream selector is an open channel: it publishes whatever this
    service logs next, including text a stranger typed into `/ask`. Every log
    panel has to name the event it means."""
    for path in DASHBOARDS.glob("*.json"):
        for panel in json.loads(path.read_text())["panels"]:
            for expr in _exprs(panel):
                if expr.strip().startswith("{service_name="):
                    assert "event_name=" in expr, (path.name, expr)


def test_the_public_board_reads_the_same_narrowed_stream():
    assert 'event_name="gen_ai.evaluation.result"' in backend.EVAL_STREAM
    for tile in (t for t in BOARD if t.source == "loki"):
        assert 'event_name="gen_ai.evaluation.result"' in tile.query, tile.key


# --------------------------------------------------------------------------- #
# the page, and what it stopped saying
# --------------------------------------------------------------------------- #

# The playground is two files now -- the markup, and the script the CSP made
# a separate file (agent/serve.py::SECURITY_HEADERS). Scan both: which half a
# given string lives in is an implementation detail of the page.
PAGE = ((REPO_ROOT / "web" / "index.html").read_text()
        + (REPO_ROOT / "web" / "app.js").read_text())


def test_the_answer_no_longer_ships_the_stack_hostname():
    """`/ask` used to return `grafana_url` to every caller. Nothing rendered it,
    and a public endpoint naming the stack is a pointer at its login page."""
    serve_src = (REPO_ROOT / "agent" / "serve.py").read_text()
    assert '"grafana_url": outcome.settings.grafana_url' not in serve_src


def test_the_page_shows_the_query_beside_the_number():
    """"We query Grafana" and "here is the PromQL that produced 52.7" are
    different claims, and only the second one is checkable by a judge."""
    assert "qBody" in PAGE
    assert "t.query" in PAGE


# --------------------------------------------------------------------------- #
# The board is built concurrently
# --------------------------------------------------------------------------- #


def test_the_board_queries_every_tile_at_once_not_one_after_another():
    """Nine independent instant queries run in series made the board's worst
    case `len(BOARD) * timeout` -- nearly a minute -- spent inside BoardCache's
    lock with every viewer queued behind it. Timed rather than asserted on the
    implementation: what matters is the wall clock a reader waits."""
    import time as _time

    delay = 0.05

    def slow(_cfg, _tile):
        _time.sleep(delay)
        return [{"value": [0, "1"]}]

    started = _time.monotonic()
    board = backend.build(CFG, query=slow)
    elapsed = _time.monotonic() - started

    assert len(board["tiles"]) == len(backend.BOARD)
    serial = delay * len(backend.BOARD)
    assert elapsed < serial / 2, (
        f"{elapsed:.2f}s for {len(backend.BOARD)} tiles that sleep {delay}s each "
        f"-- serial would be {serial:.2f}s, so these did not run concurrently"
    )


def test_tiles_stay_in_board_order_however_fast_each_one_answers():
    """Concurrency must not reorder the page. The first tile to answer is
    whichever datasource happened to be warm, which is not a layout."""
    import time as _time

    order = {tile.key: i for i, tile in enumerate(backend.BOARD)}

    def staggered(_cfg, tile):
        # deliberately inverted: the last tile answers first
        _time.sleep(0.001 * (len(backend.BOARD) - order[tile.key]))
        return [{"value": [0, "1"]}]

    board = backend.build(CFG, query=staggered)
    assert [t["key"] for t in board["tiles"]] == [t.key for t in backend.BOARD]


def test_one_slow_tile_does_not_take_the_others_down():
    """Per-tile isolation has to survive the move to threads: a failure in one
    worker is that tile's `error`, not an exception out of `build`."""
    def flaky(_cfg, tile):
        if tile.source == "loki":
            raise backend.BackendUnavailable("loki is having a moment")
        return [{"value": [0, "42"]}]

    board = backend.build(CFG, query=flaky)
    by_source = {}
    for tile in board["tiles"]:
        by_source.setdefault(tile["source"], []).append(tile)
    assert all(t.get("error") == "unavailable" for t in by_source["loki"])
    assert all("error" not in t for t in by_source["prom"])
