"""Build the three OTel providers, pointed at the same gateway as ``bridge/``.

``bridge/emit.py`` already stands up a ``TracerProvider`` + ``LoggerProvider``
from the standard ``OTEL_EXPORTER_OTLP_*`` variables for shot telemetry. This is
the same construction for the agent tier, plus a ``MeterProvider`` for the token
and latency histograms, under a distinct ``service.name`` so the two tiers are
separable in Grafana while sharing one stack.

Test/dry-run callers inject in-memory exporters (see ``observability/testing.py``)
and no environment is required; only the real OTLP path reads ``OTEL_*``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)

from observability.genai import (
    DURATION_BUCKETS,
    METRIC_OP_DURATION,
    METRIC_TOKEN_USAGE,
    TOKEN_BUCKETS,
)

DEFAULT_SERVICE_NAME = "turnaround-agent"


@dataclass(slots=True)
class Providers:
    tracer_provider: TracerProvider
    logger_provider: LoggerProvider
    meter_provider: MeterProvider


def _histogram_views() -> list[View]:
    return [
        View(instrument_name=METRIC_TOKEN_USAGE,
             aggregation=ExplicitBucketHistogramAggregation(TOKEN_BUCKETS)),
        View(instrument_name=METRIC_OP_DURATION,
             aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS)),
    ]


def build_providers(
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    resource_attributes: dict[str, str] | None = None,
    span_exporter: SpanExporter | None = None,
    log_exporter: LogExporter | None = None,
    metric_exporter: MetricExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> Providers:
    """One tracer/logger/meter provider trio.

    Pass no exporters for the real OTLP path (``OTEL_EXPORTER_OTLP_ENDPOINT``
    must be set, same as the bridge). Pass exporters or a ``metric_reader`` for
    tests -- processors then run synchronously so a caller can assert on output.
    Injecting some but not all is fine: the signals you did not wire are simply
    discarded.
    """
    real_otlp = span_exporter is None and log_exporter is None and metric_exporter is None \
        and metric_reader is None
    if real_otlp and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        raise RuntimeError(
            "OTEL_EXPORTER_OTLP_ENDPOINT is unset. The agent tier exports to the "
            "same Grafana Cloud OTLP gateway as the seeder -- copy .env.example "
            "and fill in the OpenTelemetry tile details."
        )

    resource = Resource.create({"service.name": service_name, **(resource_attributes or {})})

    if real_otlp:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter()
        log_exporter = OTLPLogExporter()
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())

    # Each signal is wired independently: a caller that injects only a span
    # exporter gets a working tracer and two providers that quietly discard,
    # rather than an AttributeError from deep inside an exporter built on None.
    tp = TracerProvider(resource=resource)
    if span_exporter is not None:
        tp.add_span_processor(
            BatchSpanProcessor(span_exporter) if real_otlp
            else SimpleSpanProcessor(span_exporter)
        )

    lp = LoggerProvider(resource=resource)
    if log_exporter is not None:
        lp.add_log_record_processor(
            BatchLogRecordProcessor(log_exporter) if real_otlp
            else SimpleLogRecordProcessor(log_exporter)
        )

    if metric_reader is None and metric_exporter is not None:
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
    mp = MeterProvider(resource=resource,
                       metric_readers=[metric_reader] if metric_reader is not None else [],
                       views=_histogram_views())

    return Providers(tracer_provider=tp, logger_provider=lp, meter_provider=mp)
