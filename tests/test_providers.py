"""The OTel provider trio the agent tier exports through.

``observability/providers.py`` had no direct coverage. It is the seam where a
misconfiguration turns into either a clear startup message or an opaque failure
inside an exporter three libraries down, so the boundaries are worth pinning:
what a missing endpoint does, what an injected exporter does, and that the
histogram buckets the EvalOps dashboard expects are actually registered.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from observability.genai import (
    DURATION_BUCKETS,
    METRIC_OP_DURATION,
    METRIC_TOKEN_USAGE,
    TOKEN_BUCKETS,
)
from observability.providers import DEFAULT_SERVICE_NAME, build_providers


def test_a_missing_endpoint_is_a_named_startup_error(monkeypatch):
    """Not a connection timeout twenty seconds into a demo."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError) as exc:
        build_providers()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in str(exc.value)
    assert ".env.example" in str(exc.value)


def test_injected_exporters_need_no_environment_at_all(monkeypatch):
    """R6 / offline assembly: the suite must build these with no OTEL_* set."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    providers = build_providers(
        span_exporter=InMemorySpanExporter(),
        log_exporter=InMemoryLogRecordExporter(),
        metric_reader=InMemoryMetricReader(),
    )
    assert providers.tracer_provider is not None
    assert providers.logger_provider is not None
    assert providers.meter_provider is not None


def test_injecting_only_one_signal_discards_the_rest_rather_than_crashing(monkeypatch):
    """A partially wired trio used to build a metric reader around ``None`` and
    die with an AttributeError from inside the OTel SDK."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    spans = InMemorySpanExporter()
    providers = build_providers(span_exporter=spans)

    with providers.tracer_provider.get_tracer("t").start_as_current_span("s"):
        pass
    providers.tracer_provider.force_flush()
    assert [s.name for s in spans.get_finished_spans()] == ["s"]

    # the unwired signals still work, they just go nowhere
    providers.meter_provider.get_meter("m").create_histogram("h").record(1)
    providers.meter_provider.force_flush()


def test_injected_processors_are_synchronous_so_a_test_can_assert(monkeypatch):
    """The real path batches; the injected path must not, or every assertion
    would need a sleep."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    spans = InMemorySpanExporter()
    providers = build_providers(span_exporter=spans, log_exporter=InMemoryLogRecordExporter())
    with providers.tracer_provider.get_tracer("t").start_as_current_span("immediate"):
        pass
    assert [s.name for s in spans.get_finished_spans()] == ["immediate"]  # no flush needed


def test_the_service_name_separates_the_agent_tier_from_the_bridge(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert DEFAULT_SERVICE_NAME == "turnaround-agent"
    providers = build_providers(span_exporter=InMemorySpanExporter())
    with providers.tracer_provider.get_tracer("t").start_as_current_span("s"):
        pass
    providers.tracer_provider.force_flush()
    resource = providers.tracer_provider.resource.attributes
    assert resource["service.name"] == "turnaround-agent"


def test_extra_resource_attributes_are_merged_not_replaced(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    providers = build_providers(
        service_name="turnaround-agent",
        resource_attributes={"deployment.environment": "cloudrun"},
        span_exporter=InMemorySpanExporter(),
    )
    attrs = providers.tracer_provider.resource.attributes
    assert attrs["service.name"] == "turnaround-agent"
    assert attrs["deployment.environment"] == "cloudrun"


def test_the_semantic_convention_histogram_buckets_are_registered(monkeypatch):
    """The EvalOps dashboard's token and latency panels read these buckets; the
    SDK's defaults are wrong for both (tokens run to millions, latency to
    minutes)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    reader = InMemoryMetricReader()
    providers = build_providers(metric_reader=reader)
    meter = providers.meter_provider.get_meter("scope")
    meter.create_histogram(METRIC_TOKEN_USAGE, unit="{token}").record(70_000)
    meter.create_histogram(METRIC_OP_DURATION, unit="s").record(3.5)

    buckets = {
        metric.name: tuple(metric.data.data_points[0].explicit_bounds)
        for rm in [reader.get_metrics_data()]
        for sm in rm.resource_metrics[0].scope_metrics
        for metric in sm.metrics
    }
    assert buckets[METRIC_TOKEN_USAGE] == TOKEN_BUCKETS
    assert buckets[METRIC_OP_DURATION] == DURATION_BUCKETS
