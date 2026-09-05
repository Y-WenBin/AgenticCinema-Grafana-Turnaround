"""Backfill historical metrics to Grafana Cloud over OTLP.

The SDK's Meter API times observations against the wall clock, so it cannot
express "this pool logged 42 hours in the week of 17 August". Backfilling five
months of production history therefore means constructing OTLP metric payloads
directly and handing them to the exporter, rather than driving a MeterProvider.

Two rules are enforced here rather than left to callers:

* **No metric may carry an artist label.** A per-person time series is exactly
  the artefact this product must not create -- it would survive every
  aggregation floor downstream, because the floor operates on queries and a
  label is already in the data. It is also unbounded cardinality.
* **Counters are cumulative.** Callers pass increments, which is what a
  simulation naturally produces; the running total per label set is computed
  here, because getting that wrong yields a series that looks plausible and
  rates to nonsense.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    Gauge,
    Metric,
    MetricExporter,
    MetricsData,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
    Sum,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from bridge import timewarp
from bridge.ontology import Attr
from bridge.privacy import PrivacyViolation, assert_no_pii

SERVICE_NAME = "turnaround-bridge"
SCOPE = InstrumentationScope("turnaround.production", "0.1.0")

#: Grafana Cloud rejects oversized OTLP payloads; large shows produce tens of
#: thousands of points, so exports are chunked.
MAX_POINTS_PER_EXPORT = 4000

#: Labels that must never appear on a metric. Everything here is either a
#: person or an unbounded identifier.
FORBIDDEN_LABELS = frozenset({Attr.ARTIST, "artist", "user", "person"})


@dataclass(frozen=True, slots=True)
class Point:
    at: datetime
    value: float
    labels: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("metric timestamps must be timezone-aware")


def _ns(when: datetime) -> int:
    return int(timewarp.apply(when).astimezone(timezone.utc).timestamp() * 1_000_000_000)


def _label_key(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


def _check_labels(labels: Mapping[str, str], *, metric: str) -> None:
    forbidden = FORBIDDEN_LABELS & set(labels)
    if forbidden:
        raise PrivacyViolation(
            f"metric {metric!r} carries {sorted(forbidden)}. A per-person series "
            "defeats every aggregation floor downstream, because the floor "
            "filters queries and the label is already in the data."
        )
    assert_no_pii(labels, where=f"metric {metric}")


class MetricBackfill:
    """Accumulates historical series and exports them in chunks."""

    def __init__(
        self,
        *,
        exporter: MetricExporter | None = None,
        resource_attributes: Mapping[str, str] | None = None,
    ) -> None:
        if exporter is None:
            if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
                raise RuntimeError(
                    "OTEL_EXPORTER_OTLP_ENDPOINT is unset. Copy .env.example and fill in "
                    "the OTLP gateway details from your Grafana Cloud stack's "
                    "OpenTelemetry tile."
                )
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )

            exporter = OTLPMetricExporter()
        self._exporter = exporter
        self._resource = Resource.create(
            {"service.name": SERVICE_NAME, **(resource_attributes or {})}
        )
        self._metrics: list[Metric] = []

    # -- series builders ---------------------------------------------------

    def gauge(
        self,
        name: str,
        points: Iterable[Point],
        *,
        unit: str = "1",
        description: str = "",
    ) -> None:
        """A value observed at a moment: weekly hours, queue depth, shots left."""
        data_points = []
        for point in sorted(points, key=lambda p: p.at):
            _check_labels(point.labels, metric=name)
            at = _ns(point.at)
            data_points.append(
                NumberDataPoint(
                    attributes=dict(point.labels),
                    start_time_unix_nano=at,
                    time_unix_nano=at,
                    value=point.value,
                )
            )
        if data_points:
            self._metrics.append(
                Metric(
                    name=name,
                    description=description,
                    unit=unit,
                    data=Gauge(data_points=data_points),
                )
            )

    def counter(
        self,
        name: str,
        points: Iterable[Point],
        *,
        unit: str = "1",
        description: str = "",
    ) -> None:
        """A monotonic total. Callers pass increments; totals are accumulated here.

        `start_time_unix_nano` is pinned to each series' first observation, so
        rate() over the backfilled range behaves the same as it would over data
        written live.
        """
        by_series: dict[tuple[tuple[str, str], ...], list[Point]] = defaultdict(list)
        for point in points:
            _check_labels(point.labels, metric=name)
            by_series[_label_key(point.labels)].append(point)

        data_points = []
        for key, series in by_series.items():
            series.sort(key=lambda p: p.at)
            start = _ns(series[0].at)
            running = 0.0
            for point in series:
                running += point.value
                data_points.append(
                    NumberDataPoint(
                        attributes=dict(key),
                        start_time_unix_nano=start,
                        time_unix_nano=_ns(point.at),
                        value=running,
                    )
                )
        if data_points:
            self._metrics.append(
                Metric(
                    name=name,
                    description=description,
                    unit=unit,
                    data=Sum(
                        data_points=data_points,
                        aggregation_temporality=AggregationTemporality.CUMULATIVE,
                        is_monotonic=True,
                    ),
                )
            )

    # -- export ------------------------------------------------------------

    @property
    def point_count(self) -> int:
        return sum(len(m.data.data_points) for m in self._metrics)

    def flush(self) -> int:
        """Export everything accumulated, chunked. Returns points written."""
        written = 0
        for chunk in self._chunks():
            self._exporter.export(
                MetricsData(
                    resource_metrics=[
                        ResourceMetrics(
                            resource=self._resource,
                            scope_metrics=[
                                ScopeMetrics(scope=SCOPE, metrics=chunk, schema_url="")
                            ],
                            schema_url="",
                        )
                    ]
                )
            )
            written += sum(len(m.data.data_points) for m in chunk)
        self._metrics.clear()
        return written

    def _chunks(self) -> Iterable[Sequence[Metric]]:
        """Split by point count, splitting a single large metric if need be."""
        batch: list[Metric] = []
        budget = MAX_POINTS_PER_EXPORT
        for metric in self._metrics:
            for piece in _split_metric(metric, MAX_POINTS_PER_EXPORT):
                size = len(piece.data.data_points)
                if batch and size > budget:
                    yield batch
                    batch, budget = [], MAX_POINTS_PER_EXPORT
                batch.append(piece)
                budget -= size
        if batch:
            yield batch


def _split_metric(metric: Metric, limit: int) -> Iterable[Metric]:
    points = metric.data.data_points
    if len(points) <= limit:
        yield metric
        return
    for start in range(0, len(points), limit):
        window = points[start : start + limit]
        if isinstance(metric.data, Sum):
            data = Sum(
                data_points=window,
                aggregation_temporality=metric.data.aggregation_temporality,
                is_monotonic=metric.data.is_monotonic,
            )
        else:
            data = Gauge(data_points=window)
        yield Metric(
            name=metric.name,
            description=metric.description,
            unit=metric.unit,
            data=data,
        )
