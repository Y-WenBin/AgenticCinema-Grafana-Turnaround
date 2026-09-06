"""The EvalOps dashboard and the two eval alert rules are generated from
``gen_ai.*`` metric and Loki field names that ``observability/`` and
``agent/evaluation.py`` actually emit. Pin the contract so a rename on either
side breaks a test rather than a dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

from grafana.alerts.build import EVAL_GROUNDING_DRIFT, EVAL_PRIVACY_BREACH
from grafana.evalops.build import evalops
from observability.genai import METRIC_OP_DURATION, METRIC_TOKEN_USAGE

RULES_JSON = Path(__file__).parents[1] / "grafana" / "alerts" / "rules.json"


def _prom_name(otel_metric: str) -> str:
    return otel_metric.replace(".", "_")


def _exprs(dash: dict) -> str:
    return "\n".join(t["expr"] for p in dash["panels"] for t in p.get("targets", []))


def test_evalops_dashboard_queries_reference_emitted_signals():
    dash = evalops()
    exprs = _exprs(dash)

    # the Prometheus panels must use the histogram names observability/ emits
    # (the OTLP exporter appends the unit: "...duration" -> "...duration_seconds")
    assert _prom_name(METRIC_TOKEN_USAGE) + "_sum" in exprs
    assert _prom_name(METRIC_TOKEN_USAGE) + "_count" in exprs
    assert _prom_name(METRIC_OP_DURATION) + "_seconds_bucket" in exprs

    # the Loki panels must select the eval stream and unwrap score
    assert '{service_name="turnaround-agent"}' in exprs
    assert "unwrap score" in exprs
    assert 'name="privacy_floor_respected"' in exprs and 'label="fail"' in exprs

    # the evidence pivot: the events table links response_id -> a TraceQL search
    (table,) = [p for p in dash["panels"] if p["type"] == "table"]
    override = next(o for o in table["fieldConfig"]["overrides"]
                    if o["matcher"]["options"] == "response_id")
    links = override["properties"][0]["value"]
    assert "span.gen_ai.response.id" in links[0]["url"]


def test_eval_alerts_are_loki_reduce_threshold_chains():
    for rule in (EVAL_GROUNDING_DRIFT, EVAL_PRIVACY_BREACH):
        refs = [(q["refId"], q["datasourceUid"]) for q in rule["data"]]
        assert refs == [("A", "grafanacloud-logs"), ("B", "__expr__"), ("C", "__expr__")]
        assert rule["condition"] == "C"
        assert rule["noDataState"] == "OK"          # must not page when no runs happened
        assert rule["annotations"]["lever"]         # every alert carries a remediation


def test_grounding_drift_fires_below_and_privacy_breach_fires_above():
    g = EVAL_GROUNDING_DRIFT["data"][2]["model"]["conditions"][0]["evaluator"]
    assert g["type"] == "lt" and g["params"] == [0.8]
    p = EVAL_PRIVACY_BREACH["data"][2]["model"]["conditions"][0]["evaluator"]
    assert p["type"] == "gt" and p["params"] == [0]
    assert EVAL_PRIVACY_BREACH["labels"]["severity"] == "critical"


def test_committed_rules_json_has_both_groups_and_is_in_sync():
    """grafana/provision.py pushes this file verbatim -- it must be current."""
    groups = json.loads(RULES_JSON.read_text())
    by_title = {g["title"]: g for g in groups}
    assert set(by_title) == {"turnaround", "turnaround-evalops"}
    assert all(g["folderUid"] == "turnaround" for g in groups)
    evalops_uids = {r["uid"] for r in by_title["turnaround-evalops"]["rules"]}
    assert evalops_uids == {"turnaround-eval-grounding-drift", "turnaround-eval-privacy-breach"}
