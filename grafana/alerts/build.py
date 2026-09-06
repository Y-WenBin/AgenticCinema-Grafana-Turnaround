"""Build the Turnaround alert rules and write grafana/alerts/rules.json.

Four rules, each carrying a *lever* in its annotations -- an alert with no
attached remediation is just pressure (PROJECT.md section 7):

* two on the ``turnaround_*`` series (crew crunch, render waste);
* two on the agent's own ``gen_ai.evaluation.result`` stream in Loki
  (``agent/evaluation.py``): grounding drift, and a hard privacy-floor breach.
  These are the "quality drift" alert the proposal calls for.

    uv run python -m grafana.alerts.build
"""

from __future__ import annotations

import json
from pathlib import Path

PROM = "grafanacloud-prom"
LOKI = "grafanacloud-logs"
FOLDER = "turnaround"
OUT = Path(__file__).parent / "rules.json"
_EVAL_EVENTS = '{service_name="turnaround-agent"} | json'


def _query(ref: str, expr: str) -> dict:
    # last_over_time so a single seed keeps the rule evaluable for the whole
    # window -- on the compressed time base the freshest points are still only a
    # few wall-clock minutes wide, and without this the rule flaps to NoData
    # between seeds.
    wrapped = f"last_over_time(({expr})[3h:1m])"
    return {
        "refId": ref,
        "relativeTimeRange": {"from": 10800, "to": 0},
        "datasourceUid": PROM,
        "model": {
            "refId": ref, "expr": wrapped, "instant": True, "range": False,
            "editorMode": "code", "intervalMs": 1000, "maxDataPoints": 43200,
            "datasource": {"type": "prometheus", "uid": PROM},
        },
    }


def _threshold(ref: str, on: str, value: float, *, op: str = "gt") -> dict:
    return {
        "refId": ref,
        "datasourceUid": "__expr__",
        "model": {
            "refId": ref, "type": "threshold", "expression": on,
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "conditions": [{"type": "query", "evaluator": {"type": op, "params": [value]}}],
        },
    }


def _loki_query(ref: str, expr: str) -> dict:
    """A LogQL metric query for an alert -- the eval events live in Loki, not Mimir."""
    return {
        "refId": ref,
        "relativeTimeRange": {"from": 3600, "to": 0},
        "datasourceUid": LOKI,
        "model": {
            "refId": ref, "expr": expr, "queryType": "range",
            "editorMode": "code", "intervalMs": 1000, "maxDataPoints": 43200,
            "datasource": {"type": "loki", "uid": LOKI},
        },
    }


def _reduce(ref: str, on: str, fn: str = "last") -> dict:
    return {
        "refId": ref,
        "datasourceUid": "__expr__",
        "model": {
            "refId": ref, "type": "reduce", "expression": on, "reducer": fn,
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "settings": {"mode": "dropNN"},
        },
    }


def _rule(uid, title, *, expr, gt, for_, labels, summary, lever) -> dict:
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "for": for_,
        "folderUID": FOLDER,
        "ruleGroup": "turnaround",
        "noDataState": "OK",
        "execErrState": "Error",
        "data": [_query("A", expr), _threshold("C", "A", gt)],
        "labels": labels,
        "annotations": {"summary": summary, "lever": lever},
    }


def _loki_rule(uid, title, *, expr, op, value, for_, no_data, labels, summary, lever) -> dict:
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "for": for_,
        "folderUID": FOLDER,
        "ruleGroup": "turnaround-evalops",
        "noDataState": no_data,
        "execErrState": "Error",
        "data": [_loki_query("A", expr), _reduce("B", "A"), _threshold("C", "B", value, op=op)],
        "labels": labels,
        "annotations": {"summary": summary, "lever": lever},
    }


CREW_CRUNCH = _rule(
    "turnaround-crew-crunch",
    "Comp pool heading past 60 h/week",
    # The floor join: a pool of fewer than 3 rostered artists is never alertable,
    # so di-pool-1 (two people) cannot trip this even when its two people are buried.
    expr=(
        '(max by (pool) (turnaround_artist_hours_logged{department="comp"})) '
        "and on(pool) (turnaround_pool_headcount >= 3)"
    ),
    gt=60,
    for_="0s",
    labels={"severity": "warning", "team": "production", "signal": "crew-load"},
    summary="{{ $labels.pool }} is at {{ printf \"%.0f\" $values.A.Value }} h per rostered artist this week.",
    lever=(
        "The cause is upstream: every comp iteration on SEQ0420 is burning ~4x "
        "the farm core-hours it should, because frame 118 keeps failing. Fix the "
        "lighting cache regression and the rework that is filling this pool's "
        "week stops being necessary."
    ),
)

RENDER_WASTE = _rule(
    "turnaround-render-waste",
    "A sequence is concentrating render waste",
    expr="sum by (sequence) (turnaround_render_waste_core_hours_total)",
    gt=20,
    for_="0s",
    labels={"severity": "warning", "team": "pipeline", "signal": "render-waste"},
    summary=(
        "{{ $labels.sequence }} has wasted {{ printf \"%.0f\" $values.A.Value }} "
        "farm core-hours on renders that produced no accepted frame."
    ),
    lever=(
        "Pull the failing frames for this sequence from Loki "
        "(`{service_name=\"turnaround-bridge\"} |= \"frame 118\"`), identify the "
        "cache key, and invalidate it. This waste predates the director note."
    ),
)


EVAL_GROUNDING_DRIFT = _loki_rule(
    "turnaround-eval-grounding-drift",
    "Agent answers are drifting off their evidence",
    # LLM judge: fraction of quantitative claims backed by a tool call in the
    # timeline it was shown. Averaged over the last hour of runs.
    expr=(f'avg_over_time({_EVAL_EVENTS} | name="hallucination" | unwrap score [1h])'),
    op="lt", value=0.8, for_="0s",
    # No eval events in the window is "nobody asked", not "healthy" -- but it also
    # must not page at 3am, so treat missing data as OK.
    no_data="OK",
    labels={"severity": "warning", "team": "ai-eng", "signal": "eval-drift"},
    summary=("Mean grounding score over the last hour is {{ printf \"%.2f\" "
             "$values.B.Value }} (< 0.80): the agent is asserting numbers the "
             "tool timeline does not support."),
    lever=("Open Turnaround · EvalOps, sort the events table by score, and "
           "follow the response_id link to the run's trace. The failing claim is "
           "in the synthesis step's chat span; tighten the analyst prompt that "
           "fed it or the recipe it used."),
)

EVAL_PRIVACY_BREACH = _loki_rule(
    "turnaround-eval-privacy-breach",
    "A sub-floor pool was named in an answer",
    expr=(f'sum(count_over_time({_EVAL_EVENTS} | name="privacy_floor_respected" '
          f'| label="fail" [1h]))'),
    op="gt", value=0, for_="0s",
    no_data="OK",
    labels={"severity": "critical", "team": "ai-eng", "signal": "privacy-floor"},
    summary=("{{ printf \"%.0f\" $values.B.Value }} answer(s) in the last hour "
             "named a pool below the aggregation floor of 3. This is the one "
             "invariant the product cannot break."),
    lever=("Stop demoing. Pull the run from Turnaround · EvalOps, confirm "
           "which pool leaked, and check bridge/privacy.py plus the "
           "CrunchGuardian prompt -- the floor join in vocabulary.py should have "
           "made this impossible."),
)


def main() -> None:
    # Shape required by PUT /api/v1/provisioning/folder/{uid}/rule-groups/{group}:
    # `title` is the group name and `interval` is an integer number of seconds.
    groups = [
        {"title": "turnaround", "folderUid": FOLDER, "interval": 60,
         "rules": [CREW_CRUNCH, RENDER_WASTE]},
        {"title": "turnaround-evalops", "folderUid": FOLDER, "interval": 60,
         "rules": [EVAL_GROUNDING_DRIFT, EVAL_PRIVACY_BREACH]},
    ]
    OUT.write_text(json.dumps(groups, indent=2) + "\n")
    n = sum(len(g["rules"]) for g in groups)
    print(f"wrote {OUT.relative_to(Path(__file__).parents[2])}  ({n} rules in {len(groups)} groups)")


if __name__ == "__main__":
    main()
