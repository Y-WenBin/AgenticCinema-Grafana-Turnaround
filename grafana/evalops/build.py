"""Build the EvalOps dashboard and write ``grafana/dashboards/turnaround-evalops.json``.

This is the surface the attached proposal is really about: Grafana showing the
*agent* the way an AI engineer needs to see it -- token economics, operation
latency, tool fan-out, and a continuous evaluation score -- next to the
infrastructure it reasons over, in one stack.

Data sources:

* **Mimir / Prometheus** -- the ``gen_ai_client_*`` histograms emitted by
  ``observability/`` (``gen_ai.client.token.usage``,
  ``gen_ai.client.operation.duration``).
* **Loki** -- the ``gen_ai.evaluation.result`` events from ``agent/evaluation.py``.
  The body is a JSON object; every panel does ``| json`` and unwraps ``score``.

The evidence pivot: each eval row carries ``response_id``; the same id is on
every ``chat`` / ``execute_tool`` span as ``gen_ai.response.id``. The events
table links each row to ``{ span.gen_ai.response.id = "<id>" }`` in Tempo.

    uv run python -m grafana.evalops.build     # rewrite the JSON
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

PROM = "grafanacloud-prom"
LOKI = "grafanacloud-logs"
TEMPO = "grafanacloud-traces"
SERVICE = "turnaround-agent"
OUT = Path(__file__).parents[1] / "dashboards"

_EVENTS = f'{{service_name="{SERVICE}"}} | json'


def _grid(x, y, w, h):
    return {"h": h, "w": w, "x": x, "y": y}


def _prom_t(expr, legend="__auto", instant=False):
    return {"datasource": {"type": "prometheus", "uid": PROM}, "editorMode": "code",
            "expr": expr, "legendFormat": legend, "range": not instant,
            "instant": instant, "refId": "A"}


def _loki_t(expr, legend="__auto", instant=False):
    return {"datasource": {"type": "loki", "uid": LOKI}, "editorMode": "code",
            "expr": expr, "legendFormat": legend, "queryType": "range",
            "range": not instant, "instant": instant, "refId": "A"}


def _stat(title, target, *, unit="none", thresholds, x, y, w=6, h=6, desc=""):
    steps = [{"color": "green", "value": None}] + [{"color": c, "value": v} for v, c in thresholds]
    return {
        "type": "stat", "title": title, "description": desc,
        "datasource": target["datasource"], "gridPos": _grid(x, y, w, h),
        "fieldConfig": {"defaults": {"unit": unit,
                        "thresholds": {"mode": "absolute", "steps": steps},
                        "color": {"mode": "thresholds"}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "textMode": "auto"},
        "targets": [target],
    }


def _timeseries(title, targets, *, unit="none", thresholds=None, x=0, y=0, w=24, h=8, desc=""):
    steps = [{"color": "green", "value": None}]
    if thresholds:
        steps += [{"color": c, "value": v} for v, c in thresholds]
    return {
        "type": "timeseries", "title": title, "description": desc,
        "datasource": targets[0]["datasource"], "gridPos": _grid(x, y, w, h),
        "fieldConfig": {"defaults": {
            "unit": unit,
            "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 8, "showPoints": "auto"},
            "thresholds": {"mode": "absolute", "steps": steps},
            **({"color": {"mode": "thresholds"}} if thresholds else {})}, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "right",
                               "calcs": ["lastNotNull", "min", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": targets,
    }


def _bargauge(title, target, *, unit="short", x=0, y=0, w=12, h=8, desc=""):
    return {
        "type": "bargauge", "title": title, "description": desc,
        "datasource": target["datasource"], "gridPos": _grid(x, y, w, h),
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": [
            {"color": "green", "value": None}]}, "color": {"mode": "continuous-BlPu"}}, "overrides": []},
        "options": {"displayMode": "gradient", "orientation": "horizontal",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "targets": [target],
    }


def _text(content, *, x=0, y=0, w=24, h=4):
    return {"type": "text", "title": "", "gridPos": _grid(x, y, w, h),
            "options": {"mode": "markdown", "content": content}}


def _tempo_pivot_url() -> str:
    inner = {
        "datasource": TEMPO,
        "queries": [{"refId": "A", "queryType": "traceql",
                     "query": '{ span.gen_ai.response.id = "${__value.raw}" }'}],
        "range": {"from": "now-6h", "to": "now"},
    }
    return "/explore?left=" + urllib.parse.quote(json.dumps(inner))


def _events_table():
    """Recent evaluation events, newest first, each linking to its trace."""
    return {
        "type": "table", "title": "Evaluation events (newest first)",
        "description": "Every gen_ai.evaluation.result. The response_id links to "
                       "the run's trace in Tempo -- pivot a low score to the exact "
                       "chat and PromQL behind it.",
        "datasource": {"type": "loki", "uid": LOKI},
        "gridPos": _grid(0, 22, 24, 10),
        "targets": [{"datasource": {"type": "loki", "uid": LOKI}, "refId": "A",
                     "expr": f'{{service_name="{SERVICE}"}}', "queryType": "range",
                     "maxLines": 200}],
        "transformations": [
            {"id": "extractFields", "options": {"source": "Line", "format": "json",
                                                "replace": False}},
            {"id": "organize", "options": {
                "excludeByName": {"Line": True, "labels": True, "tsNs": True, "id": True,
                                  "Time": False},
                "indexByName": {"Time": 0, "name": 1, "label": 2, "score": 3,
                                "actor_type": 4, "response_id": 5, "explanation": 6}}},
        ],
        "fieldConfig": {"defaults": {"custom": {"filterable": True}}, "overrides": [
            {"matcher": {"id": "byName", "options": "response_id"},
             "properties": [{"id": "links", "value": [
                 {"title": "Open the run's trace in Tempo", "url": _tempo_pivot_url(),
                  "targetBlank": True}]}]},
            {"matcher": {"id": "byName", "options": "label"},
             "properties": [{"id": "mappings", "value": [
                 {"type": "value", "options": {
                     "fail": {"color": "red", "index": 0},
                     "pass": {"color": "green", "index": 1},
                     "borderline": {"color": "yellow", "index": 2}}}]},
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
        ]},
        "options": {"showHeader": True, "sortBy": [{"displayName": "Time", "desc": True}]},
    }


def evalops() -> dict:
    dur_bucket = "gen_ai_client_operation_duration_seconds_bucket"
    tok_sum = "gen_ai_client_token_usage_sum"
    panels = [
        _text(
            "**The agent, watched the way it reasons.** Token economics and "
            "operation latency from the OpenTelemetry GenAI conventions "
            "(`observability/`), and a continuous evaluation score from the "
            "judge tier (`agent/evaluation.py`) -- a deterministic ground-truth "
            "+ privacy check and an independent Gemini judge. Every "
            "`gen_ai.evaluation.result` carries the run's `response_id`; the "
            "same id is on every `chat` and `execute_tool` span, so a low score "
            "pivots straight to the trace, the prompt and the PromQL behind it.",
            h=4),

        _stat("Evaluations (last 1h)", _loki_t(
            f'sum(count_over_time({_EVENTS} [1h]))', "events"),
            unit="short", thresholds=[(1, "green")], x=0, y=4, w=6,
            desc="Judge activity. Zero means no runs, or the judge tier is off."),
        _stat("Mean grounding score (1h)", _loki_t(
            f'avg_over_time({_EVENTS} | name="hallucination" | unwrap score [1h])', "grounding"),
            unit="percentunit", thresholds=[(0.8, "yellow"), (0.9, "green")], x=6, y=4, w=6,
            desc="LLM judge: is every number in the answer backed by a tool call? "
                 "The EVAL_DRIFT alert trips below 0.8."),
        _stat("Task completion (1h)", _loki_t(
            f'avg_over_time({_EVENTS} | name="task_completion" | unwrap score [1h])', "task"),
            unit="percentunit", thresholds=[(0.8, "yellow"), (0.9, "green")], x=12, y=4, w=6,
            desc="LLM judge: does the answer resolve the ask, not just describe it?"),
        _stat("Privacy-floor failures (1h)", _loki_t(
            f'sum(count_over_time({_EVENTS} | name="privacy_floor_respected" | label="fail" [1h]))',
            "breaches"),
            unit="short", thresholds=[(1, "red")], x=18, y=4, w=6,
            desc="A sub-floor pool (di-pool-1) named in an answer or its evidence. "
                 "Must be zero. Any non-zero value pages."),

        _timeseries("Evaluation score by dimension", [
            _loki_t(f'avg_over_time({_EVENTS} | name="grounding_numbers" | unwrap score [$__interval])',
                    "grounding_numbers"),
            {**_loki_t(f'avg_over_time({_EVENTS} | name="mechanism_named" | unwrap score [$__interval])',
                       "mechanism_named"), "refId": "B"},
            {**_loki_t(f'avg_over_time({_EVENTS} | name="privacy_floor_respected" | unwrap score [$__interval])',
                       "privacy_floor_respected"), "refId": "C"},
            {**_loki_t(f'avg_over_time({_EVENTS} | name="relevance" | unwrap score [$__interval])',
                       "relevance"), "refId": "D"},
            {**_loki_t(f'avg_over_time({_EVENTS} | name="hallucination" | unwrap score [$__interval])',
                       "hallucination"), "refId": "E"},
            {**_loki_t(f'avg_over_time({_EVENTS} | name="task_completion" | unwrap score [$__interval])',
                       "task_completion"), "refId": "F"},
        ], unit="percentunit", thresholds=[(0.8, "red")], x=0, y=10, w=24, h=8,
            desc="Quality drift over time. A dimension trending down is the signal "
                 "the proposal calls for -- pivot to the failing run below."),

        _timeseries("Token usage by model and type (cumulative)", [
            _prom_t(f'sum by (gen_ai_request_model, gen_ai_token_type) ({tok_sum})',
                    "{{gen_ai_request_model}} {{gen_ai_token_type}}")],
            unit="short", x=0, y=18, w=12, h=8,
            desc="gen_ai.client.token.usage. Group by team/feature/model to build a "
                 "token-budget panel; here it is model x {input,output}."),
        _timeseries("Operation duration p95 (chat vs tool, over the window)", [
            _prom_t('histogram_quantile(0.95, sum by (le, gen_ai_operation_name) '
                    f'(increase({dur_bucket}{{gen_ai_operation_name=~"chat|execute_tool"}}'
                    '[$__range])))', "{{gen_ai_operation_name}}")],
            unit="s", x=12, y=18, w=12, h=8,
            desc="gen_ai.client.operation.duration. increase() over the dashboard "
                 "range, not rate() -- runs are bursty, so a short rate window is "
                 "usually empty between them."),

        _events_table(),

        _bargauge("Tool calls by tool (cumulative)", _prom_t(
            'sum by (gen_ai_tool_name) '
            '(gen_ai_client_operation_duration_seconds_count{gen_ai_operation_name="execute_tool"})',
            "{{gen_ai_tool_name}}"),
            unit="short", x=0, y=32, w=12, h=8,
            desc="Which Grafana MCP tools the agent leans on. A spike in one tool "
                 "is often an agent looping."),
        _bargauge("Chat calls by agent (cumulative)", _prom_t(
            'sum by (gen_ai_agent_name) '
            '(gen_ai_client_token_usage_count{gen_ai_token_type="input"})',
            "{{gen_ai_agent_name}}"),
            unit="short", x=12, y=32, w=12, h=8,
            desc="LLM calls per agent in the pipeline. The synthesis step and a "
                 "looping analyst both show up here."),
    ]
    return {
        "uid": "turnaround-evalops",
        "title": "Turnaround · EvalOps",
        "description": "The agent's own token economics, latency and continuous "
                       "evaluation score -- gen_ai.* telemetry from observability/ "
                       "and agent/evaluation.py.",
        "tags": ["turnaround", "evalops", "genai"],
        "timezone": "utc",
        "schemaVersion": 39,
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "",
        "annotations": {"list": [
            {"name": "Director notes", "enable": True, "iconColor": "yellow",
             "datasource": {"type": "prometheus", "uid": "-- Grafana --"},
             "target": {"type": "tags", "tags": ["director-note"], "limit": 100, "matchAny": True}},
        ]},
        "templating": {"list": []},
        "panels": panels,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    dash = evalops()
    path = OUT / f"{dash['uid']}.json"
    path.write_text(json.dumps(dash, indent=2) + "\n")
    print(f"wrote {path.relative_to(Path(__file__).parents[2])}  ({len(dash['panels'])} panels)")


if __name__ == "__main__":
    main()
