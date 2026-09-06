"""In-memory wiring for tests and dry runs, shipped with the package.

Mirrors ``bridge/testing.py``: exercising the full span/metric/log path without
a network is the only way to catch a missing attribute or a wrong metric unit
before the data is in the stack.
"""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from observability.genai import GenAiTelemetry
from observability.providers import build_providers


@dataclass(slots=True)
class Harness:
    telemetry: GenAiTelemetry
    spans: InMemorySpanExporter
    logs: InMemoryLogRecordExporter
    metrics: InMemoryMetricReader

    def finished_spans(self) -> list:
        return list(self.spans.get_finished_spans())

    def span_names(self) -> list[str]:
        return [s.name for s in self.finished_spans()]

    def spans_by_name(self, name: str) -> list:
        return [s for s in self.finished_spans() if s.name == name]

    def log_records(self) -> list:
        return [r.log_record for r in self.logs.get_finished_logs()]

    def evaluation_events(self) -> list[dict]:
        """One merged dict per ``gen_ai.evaluation.result`` record: the log
        attributes plus the JSON body, keyed under the semantic-convention
        names so a caller sees the whole event regardless of which side a field
        rides on."""
        import json

        _BODY_TO_SEMCONV = {
            "name": "gen_ai.evaluation.name",
            "score": "gen_ai.evaluation.score.value",
            "label": "gen_ai.evaluation.score.label",
            "actor_type": "gen_ai.evaluation.actor.type",
            "response_id": "gen_ai.response.id",
            "explanation": "gen_ai.evaluation.explanation",
        }
        out = []
        for rec in self.log_records():
            attrs = dict(rec.attributes or {})
            if attrs.get("event.name") != "gen_ai.evaluation.result":
                continue
            merged = dict(attrs)
            try:
                body = json.loads(rec.body) if isinstance(rec.body, str) else {}
            except (TypeError, ValueError):
                body = {}
            for k, v in body.items():
                merged.setdefault(_BODY_TO_SEMCONV.get(k, k), v)
                merged.setdefault(k, v)
            out.append(merged)
        return out

    def metric_points(self, name: str) -> list:
        data = self.metrics.get_metrics_data()
        pts = []
        for rm in getattr(data, "resource_metrics", []):
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == name:
                        pts.extend(metric.data.data_points)
        return pts


def make_harness(*, capture_content: bool = False, content_guard=None) -> Harness:
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    reader = InMemoryMetricReader()
    providers = build_providers(
        service_name="turnaround-agent-test",
        span_exporter=spans, log_exporter=logs, metric_reader=reader,
    )
    telemetry = GenAiTelemetry(
        tracer_provider=providers.tracer_provider,
        logger_provider=providers.logger_provider,
        meter_provider=providers.meter_provider,
        capture_content=capture_content,
        content_guard=content_guard,
    )
    return Harness(telemetry=telemetry, spans=spans, logs=logs, metrics=reader)
