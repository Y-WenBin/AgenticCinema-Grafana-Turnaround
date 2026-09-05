"""The seeder's metric derivation -- specifically the invariants dashboards
and alerts depend on."""

from datetime import UTC, datetime

from bridge.metrics import FORBIDDEN_LABELS, MetricBackfill
from bridge.ontology import Metric
from bridge.testing import CollectingMetricExporter
from seed.model import ShowSimulation
from seed.populate import build_metrics

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def _metrics():
    history = ShowSimulation.load(now=NOW).run()
    capture = CollectingMetricExporter()
    backfill = MetricBackfill(exporter=capture)
    build_metrics(history, backfill)
    backfill.flush()
    return history, capture


def test_pool_headcount_series_is_emitted_for_every_pool():
    history, capture = _metrics()
    points = capture.points(Metric.POOL_HEADCOUNT)
    by_pool = {p.attributes["pool"]: p.value for p in points}
    assert by_pool == {pool: float(n) for pool, n in history.pool_headcount.items()}


def test_di_pool_1_is_below_the_floor_in_the_headcount_series():
    _, capture = _metrics()
    by_pool = {p.attributes["pool"]: p.value for p in capture.points(Metric.POOL_HEADCOUNT)}
    assert by_pool["di-pool-1"] < 3
    assert by_pool["comp-pool-2"] >= 3


def test_no_emitted_series_carries_a_person_label():
    _, capture = _metrics()
    for metric in capture.metrics:
        for point in metric.data.data_points:
            assert not FORBIDDEN_LABELS & set(point.attributes), metric.name
