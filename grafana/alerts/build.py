"""Build the Turnaround alert rules and write grafana/alerts/rules.json.

Two rules, both derived from the ontology's series and both carrying a *lever*
in their annotations -- an alert about crunch with no attached remediation is
just pressure (PROJECT.md section 7).

    uv run python -m grafana.alerts.build
"""

from __future__ import annotations

import json
from pathlib import Path

PROM = "grafanacloud-prom"
FOLDER = "turnaround"
OUT = Path(__file__).parent / "rules.json"


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


def _threshold(ref: str, on: str, gt: float) -> dict:
    return {
        "refId": ref,
        "datasourceUid": "__expr__",
        "model": {
            "refId": ref, "type": "threshold", "expression": on,
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "conditions": [{"type": "query", "evaluator": {"type": "gt", "params": [gt]}}],
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


def main() -> None:
    # Shape required by PUT /api/v1/provisioning/folder/{uid}/rule-groups/{group}:
    # `title` is the group name and `interval` is an integer number of seconds.
    groups = [{
        "title": "turnaround",
        "folderUid": FOLDER,
        "interval": 60,
        "rules": [CREW_CRUNCH, RENDER_WASTE],
    }]
    OUT.write_text(json.dumps(groups, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(Path(__file__).parents[2])}  ({len(groups[0]['rules'])} rules)")


if __name__ == "__main__":
    main()
