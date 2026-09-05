"""Backfilled metrics look plausible even when they are wrong, so the
accumulation and the label rules are tested directly."""

from datetime import datetime, timedelta, timezone

import pytest
from opentelemetry.sdk.metrics.export import AggregationTemporality, Gauge, Sum

from bridge.metrics import MAX_POINTS_PER_EXPORT, MetricBackfill, Point
from bridge.ontology import Attr
from bridge.privacy import PrivacyViolation
from bridge.testing import CollectingMetricExporter

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest.fixture
def capture():
    return CollectingMetricExporter()


@pytest.fixture
def backfill(capture):
    return MetricBackfill(exporter=capture)


def days(n: int) -> datetime:
    return T0 + timedelta(days=n)


class TestCounterAccumulation:
    def test_increments_become_a_running_total(self, backfill, capture):
        backfill.counter(
            "turnaround_shots_approved_total",
            [Point(days(i), 2.0, {"sequence": "SEQ0420"}) for i in range(4)],
        )
        backfill.flush()
        values = [p.value for p in capture.points("turnaround_shots_approved_total")]
        assert values == [2.0, 4.0, 6.0, 8.0]

    def test_series_accumulate_independently(self, backfill, capture):
        backfill.counter(
            "m",
            [
                Point(days(0), 1.0, {"sequence": "A"}),
                Point(days(0), 10.0, {"sequence": "B"}),
                Point(days(1), 1.0, {"sequence": "A"}),
                Point(days(1), 10.0, {"sequence": "B"}),
            ],
        )
        backfill.flush()
        by_seq = {}
        for p in capture.points("m"):
            by_seq.setdefault(p.attributes["sequence"], []).append(p.value)
        assert by_seq["A"] == [1.0, 2.0]
        assert by_seq["B"] == [10.0, 20.0]

    def test_out_of_order_input_still_accumulates_chronologically(self, backfill, capture):
        backfill.counter(
            "m",
            [Point(days(2), 3.0, {}), Point(days(0), 1.0, {}), Point(days(1), 2.0, {})],
        )
        backfill.flush()
        points = sorted(capture.points("m"), key=lambda p: p.time_unix_nano)
        assert [p.value for p in points] == [1.0, 3.0, 6.0]

    def test_start_time_is_pinned_so_rate_behaves(self, backfill, capture):
        backfill.counter("m", [Point(days(i), 1.0, {}) for i in range(3)])
        backfill.flush()
        starts = {p.start_time_unix_nano for p in capture.points("m")}
        assert len(starts) == 1, "a moving start time makes rate() reset every scrape"

    def test_counter_is_cumulative_and_monotonic(self, backfill, capture):
        backfill.counter("m", [Point(days(0), 1.0, {})])
        backfill.flush()
        data = capture.metrics[0].data
        assert isinstance(data, Sum)
        assert data.is_monotonic
        assert data.aggregation_temporality is AggregationTemporality.CUMULATIVE


class TestGauge:
    def test_values_are_observations_not_totals(self, backfill, capture):
        backfill.gauge(
            "turnaround_artist_hours_logged",
            [Point(days(0), 38.0, {}), Point(days(1), 89.8, {})],
        )
        backfill.flush()
        assert [p.value for p in capture.points("turnaround_artist_hours_logged")] == [38.0, 89.8]
        assert isinstance(capture.metrics[0].data, Gauge)

    def test_historical_timestamps_survive(self, backfill, capture):
        backfill.gauge("m", [Point(days(5), 1.0, {})])
        backfill.flush()
        assert capture.points("m")[0].time_unix_nano == int(days(5).timestamp() * 1e9)

    def test_naive_timestamps_are_refused(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            Point(datetime(2026, 8, 1), 1.0, {})


class TestNoPerPersonSeries:
    """A per-person series survives every aggregation floor downstream, because
    the floor filters queries and the label is already in the data."""

    def test_artist_label_is_refused_on_a_gauge(self, backfill):
        with pytest.raises(PrivacyViolation, match="aggregation floor"):
            backfill.gauge("m", [Point(days(0), 1.0, {Attr.ARTIST: "a7f3c2d1"})])

    def test_artist_label_is_refused_on_a_counter(self, backfill):
        with pytest.raises(PrivacyViolation, match="aggregation floor"):
            backfill.counter("m", [Point(days(0), 1.0, {"user": "a7f3c2d1"})])

    def test_pool_labels_are_allowed(self, backfill, capture):
        backfill.gauge("m", [Point(days(0), 1.0, {"pool": "comp-pool-2"})])
        backfill.flush()
        assert capture.points("m")[0].attributes["pool"] == "comp-pool-2"

    def test_email_in_a_label_is_refused(self, backfill):
        with pytest.raises(PrivacyViolation):
            backfill.gauge("m", [Point(days(0), 1.0, {"vendor": "ops@studio.com"})])


class TestChunking:
    def test_a_large_series_is_split_across_exports(self, backfill, capture):
        n = MAX_POINTS_PER_EXPORT * 2 + 17
        backfill.gauge("m", [Point(days(0) + timedelta(minutes=i), 1.0, {}) for i in range(n)])
        written = backfill.flush()
        assert written == n
        assert len(capture.batches) >= 3
        assert all(
            sum(len(m.data.data_points) for m in batch) <= MAX_POINTS_PER_EXPORT
            for batch in capture.batches
        )

    def test_flush_empties_the_buffer(self, backfill):
        backfill.gauge("m", [Point(days(0), 1.0, {})])
        assert backfill.point_count == 1
        backfill.flush()
        assert backfill.point_count == 0
        assert backfill.flush() == 0
