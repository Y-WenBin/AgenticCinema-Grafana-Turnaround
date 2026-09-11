"""The control room: what the agent reads, for people who cannot log in.

`/ask` shows a judge the *front* of the product -- a question, an answer, a
scorecard. This module is the back of it: the live Grafana Cloud signal the
agents actually query, rendered on the public page by a service that holds the
credential so the browser never has to.

Three properties make that safe to expose, and they are worth stating plainly
because each one is a decision, not an accident:

**There is no input.** `BOARD` below is the entire query surface. The browser
sends no parameters -- not a metric, not a range, not a filter -- so this is not
an allowlist that validates a request, it is a fixed board that ignores one.
There is no string a visitor can supply that reaches PromQL or LogQL.

**The privacy floor is in the query, not in a convention.** `bridge/privacy.py`
sets `MIN_POOL_SIZE = 3`, and the crew tile carries
`and on(pool) (... >= 3)` inline. A pool of two cannot appear in a public
number here even if someone later forgets why the floor exists, because the
floor is what the query selects on. The internal dashboard still shows
`di-pool-1` for context, behind auth, which is where that belongs.

**One upstream query serves every viewer.** Results are cached for
`BoardCache.ttl` seconds and built while holding the lock, so a thousand
simultaneous readers cost Grafana one query, not a thousand. That is the whole
reason this exists rather than a public dashboard: a natively shared board runs
every panel for every anonymous visitor, against the owner's quota, with no
per-viewer limit -- which turns a shared link into a way to spend someone else's
Grafana bill.

Nothing here returns `grafana_url`, a datasource UID, or a token. What a viewer
gets is a number, a caption, and the query that produced it -- and the queries
are already public, in `grafana/dashboards/`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from agent.config import Settings

#: How far back an instant query looks for a value.
#:
#: The show is re-seeded every 15 minutes (Cloud Scheduler
#: `turnaround-seed-every-15m`) and Prometheus stops carrying a sample forward
#: after 5, so a bare `sum(metric)` decays as individual series go stale between
#: runs and returns *nothing at all* just before the next one lands. Every
#: metric tile therefore reads `max_over_time(metric[20m])` -- a window wide
#: enough to always contain one full seed cycle.
LOOKBACK = "20m"

#: The judge tier writes one log event per evaluation; a day is a big enough
#: sample for a rate to mean something and small enough to still be *this* build.
WINDOW = "24h"

#: `service_name` is the only real stream label -- everything else the exporter
#: sets (`event_name`, `gen_ai_evaluation_name`, ...) arrives as structured
#: metadata, so it filters *after* the selector, never inside `{}`.
#:
#: Narrowing to the evaluation event is deliberate: today this service logs
#: nothing else, so the filter changes no number. It is here so that the day
#: someone logs a stack trace under the same `service_name`, it does not appear
#: on a public page without anyone having decided that it should.
EVAL_STREAM = '{service_name="turnaround-agent"} | event_name="gen_ai.evaluation.result"'


@dataclass(frozen=True, slots=True)
class Tile:
    """One number on the board, and the query that is allowed to produce it."""

    key: str
    title: str
    caption: str
    source: str  # "prom" | "loki"
    query: str
    unit: str = ""
    precision: int = 0
    #: An instant query that matches no series returns an empty result, which is
    #: not the same as zero -- except when the tile counts things, where "no
    #: matching lines" *is* the answer and is usually the good one. The
    #: privacy-breach tile is exactly this case: empty means none happened, and
    #: rendering it as "--" would hide the single best fact on the page.
    zero_when_empty: bool = False
    kind: str = "stat"  # "stat" | "bars"


BOARD: tuple[Tile, ...] = (
    Tile(
        key="shots_approved",
        title="Shots through DI",
        caption="Signed off across the tracked sequences.",
        source="prom",
        query=f"sum(max_over_time(turnaround_shots_approved_total[{LOOKBACK}]))",
    ),
    Tile(
        key="render_waste",
        title="Render core-hours wasted",
        caption="Frames rendered for a version that was superseded before review.",
        source="prom",
        query=f"sum(max_over_time(turnaround_render_waste_core_hours_total[{LOOKBACK}]))",
        unit=" h",
        precision=1,
    ),
    Tile(
        key="crunch_pools",
        title="Pools past 55 h/week",
        caption="In overtime. Pools under three people are excluded by the query itself.",
        source="prom",
        query=(
            f"count((max by (pool) (max_over_time(turnaround_artist_hours_logged[{LOOKBACK}])) > 55)"
            f" and on(pool) (max by (pool) (max_over_time(turnaround_pool_headcount[{LOOKBACK}])) >= 3))"
        ),
        zero_when_empty=True,
    ),
    Tile(
        key="frame_failure",
        title="Frame failure rate",
        caption="Farm-wide, across every department pass.",
        source="prom",
        query=(
            f"sum(max_over_time(turnaround_render_frames_failed_total[{LOOKBACK}]))"
            f" / clamp_min(sum(max_over_time(turnaround_render_frames_total[{LOOKBACK}])), 1)"
        ),
        unit="%",
        precision=1,
    ),
    Tile(
        key="evaluations",
        title=f"Judgements recorded ({WINDOW})",
        caption="Every answer this service gave was scored, and the scores are here.",
        source="loki",
        query=f"sum(count_over_time({EVAL_STREAM} [{WINDOW}]))",
        zero_when_empty=True,
    ),
    Tile(
        key="grounding",
        title="Mean grounding score",
        caption="How much of each answer the tool output actually supports.",
        source="loki",
        query=(
            f'avg(avg_over_time({EVAL_STREAM} | json | name="hallucination" '
            f"| unwrap score [{WINDOW}]))"
        ),
        precision=2,
    ),
    Tile(
        key="judge_pass_rate",
        title="Judge pass rate",
        caption="Across all six dimensions, deterministic and LLM alike.",
        source="loki",
        query=(
            f'sum(count_over_time({EVAL_STREAM} | json | label="pass" [{WINDOW}]))'
            f" / sum(count_over_time({EVAL_STREAM} | json [{WINDOW}]))"
        ),
        unit="%",
        precision=1,
    ),
    Tile(
        key="privacy_breaches",
        title="Privacy-floor breaches",
        caption=(
            "Answers that named a crew pool below the floor of three. "
            "A number other than zero is a bug, and this is where it would show."
        ),
        source="loki",
        query=(
            f'sum(count_over_time({EVAL_STREAM} | json | name="privacy_floor_respected" '
            f'| label="fail" [{WINDOW}]))'
        ),
        zero_when_empty=True,
    ),
    Tile(
        key="waste_by_sequence",
        title="Where the waste is",
        caption="Wasted render core-hours by sequence -- the join, in one query.",
        source="prom",
        query=(
            f"topk(5, sum by (sequence) "
            f"(max_over_time(turnaround_render_waste_core_hours_total[{LOOKBACK}])))"
        ),
        unit=" h",
        precision=1,
        kind="bars",
    ),
)


# -- talking to Grafana ------------------------------------------------ #

#: Datasource-proxy paths. Prometheus and Loki disagree about where an instant
#: query lives, and that is the only difference between the two sources here.
_PATHS = {
    "prom": "api/v1/query",
    "loki": "loki/api/v1/query",
}


class BackendUnavailable(RuntimeError):
    """Grafana did not answer. The page degrades; it does not 500."""


def _query(cfg: Settings, tile: Tile, *, timeout: float) -> list[dict[str, Any]]:
    uid = cfg.ds_prom if tile.source == "prom" else cfg.ds_loki
    url = f"{cfg.grafana_url}/api/datasources/proxy/uid/{uid}/{_PATHS[tile.source]}"
    try:
        r = requests.get(
            url,
            params={"query": tile.query},
            headers={"Authorization": f"Bearer {cfg.grafana_token}"},
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as exc:
        # The message may quote the URL, which carries the stack hostname --
        # keep the detail in the exception for the log, never in the response.
        raise BackendUnavailable(str(exc)) from exc
    if payload.get("status") != "success":
        raise BackendUnavailable(f"{tile.key}: {payload.get('error', 'query failed')}")
    return payload.get("data", {}).get("result", []) or []


def _scalar(result: list[dict[str, Any]]) -> float | None:
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _bars(result: list[dict[str, Any]], tile: Tile) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for series in result:
        value = _scalar([series])
        if value is None:
            continue
        metric = series.get("metric") or {}
        label = metric.get("sequence") or metric.get("pool") or metric.get("department")
        rows.append({"label": label or "?", "value": value,
                     "display": _display(value, tile)})
    rows.sort(key=lambda row: row["value"], reverse=True)
    return rows


def _display(value: float | None, tile: Tile) -> str:
    if value is None:
        return "--"
    if tile.unit == "%":
        return f"{value * 100:.{tile.precision}f}%"
    whole = f"{value:,.{tile.precision}f}"
    return f"{whole}{tile.unit}"


def _render(tile: Tile, result: list[dict[str, Any]]) -> dict[str, Any]:
    if tile.kind == "bars":
        rows = _bars(result, tile)
        return {"bars": rows, "value": None,
                "display": "--" if not rows else rows[0]["display"]}
    value = _scalar(result)
    if value is None and tile.zero_when_empty:
        value = 0.0
    return {"bars": [], "value": value, "display": _display(value, tile)}


def build(cfg: Settings, *, timeout: float = 6.0,
          query: Callable[[Settings, Tile], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Run every tile on the board once and shape it for the page.

    A tile that fails carries its own `error` rather than taking the board down
    with it: Loki being slow should not blank the metric tiles beside it.
    """
    if not cfg.grafana_ready:
        raise BackendUnavailable("Grafana is not configured for this deployment")
    run = query or (lambda c, t: _query(c, t, timeout=timeout))
    tiles: list[dict[str, Any]] = []
    failures = 0
    for tile in BOARD:
        entry: dict[str, Any] = {
            "key": tile.key,
            "title": tile.title,
            "caption": tile.caption,
            "source": tile.source,
            "query": tile.query,
            "kind": tile.kind,
        }
        try:
            entry.update(_render(tile, run(cfg, tile)))
        except BackendUnavailable:
            failures += 1
            entry.update({"value": None, "display": "--", "bars": [],
                          "error": "unavailable"})
        tiles.append(entry)
    if failures == len(BOARD):
        raise BackendUnavailable("every tile failed")
    return {
        "tiles": tiles,
        "lookback": LOOKBACK,
        "window": WINDOW,
        "generated_at": time.time(),
    }


# -- one query, however many viewers ----------------------------------- #


@dataclass
class BoardCache:
    """A single shared result, rebuilt at most once every `ttl` seconds.

    The build happens *while holding the lock*, which is the unusual choice and
    the deliberate one. Building outside it would keep readers moving during a
    slow rebuild, at the cost of letting N threads that all miss at the same
    moment fire N upstream queries -- precisely the amplification this module
    exists to prevent. Holding it makes "one query per interval" a guarantee
    instead of a typical case. Readers block for as long as Grafana takes, and
    `build()`'s own timeout bounds that.

    On failure the last good board is served with `stale: true`. A judge
    refreshing during a Grafana blip should see a number and a caveat, not an
    empty panel that reads as a broken product.
    """

    ttl: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _value: dict[str, Any] | None = field(default=None, repr=False)
    _at: float = field(default=0.0, repr=False)
    #: upstream round trips actually spent -- asserted on in the tests
    builds: int = 0

    def get(self, build_fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            fresh_enough = self._value is not None and (now - self._at) < self.ttl
            if fresh_enough:
                return dict(self._value, cached=True, stale=False)
            try:
                value = build_fn()
            except BackendUnavailable:
                if self._value is None:
                    raise
                # Do not touch `_at`: keep retrying at the same cadence rather
                # than pinning a stale board for a full ttl after every blip.
                return dict(self._value, cached=True, stale=True)
            self.builds += 1
            self._value, self._at = value, now
            return dict(value, cached=False, stale=False)


#: Process-wide, like `agent.serve.gate` -- and per instance for the same
#: reason, which for a cache is a feature: more instances means more capacity,
#: not more upstream load per viewer.
CACHE = BoardCache()


def view(cfg: Settings, *, cache: BoardCache | None = None, timeout: float = 6.0,
         query: Callable[[Settings, Tile], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """The public board. Never returns a URL, a datasource UID, or a token.

    `query` is the same injection point `build` takes, carried through so the
    cache can be exercised over the real code path rather than over a stub of
    it -- "one query per interval" is a claim about *this* function.
    """
    return (cache or CACHE).get(lambda: build(cfg, timeout=timeout, query=query))
