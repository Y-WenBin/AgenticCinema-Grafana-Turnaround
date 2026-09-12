"""The affine time compression that lets a five-month backfill clear the
hosted-Mimir out-of-order window."""

from datetime import UTC, datetime, timedelta

import pytest

from bridge import timewarp
from bridge.emit import _ns as emit_ns
from bridge.metrics import _ns as metrics_ns

ANCHOR = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
EARLIEST = ANCHOR - timedelta(days=150)
WINDOW = timedelta(minutes=45)


class TestInactiveByDefault:
    def test_apply_is_identity_when_unconfigured(self):
        assert timewarp.apply(EARLIEST) == EARLIEST
        assert not timewarp.is_active()

    def test_emit_ns_untouched_when_off(self):
        assert emit_ns(EARLIEST) == int(EARLIEST.timestamp() * 1e9)

    def test_metrics_ns_untouched_when_off(self):
        assert metrics_ns(EARLIEST) == int(EARLIEST.timestamp() * 1e9)


class TestConfigured:
    @pytest.fixture(autouse=True)
    def _warp(self):
        timewarp.configure(anchor=ANCHOR, earliest=EARLIEST, window=WINDOW)

    def test_anchor_maps_to_itself(self):
        assert timewarp.apply(ANCHOR) == ANCHOR

    def test_earliest_maps_to_the_window_edge(self):
        assert timewarp.apply(EARLIEST) == ANCHOR - WINDOW

    def test_midpoint_maps_to_half_the_window(self):
        mid = EARLIEST + (ANCHOR - EARLIEST) / 2
        assert abs((timewarp.apply(mid) - (ANCHOR - WINDOW / 2)).total_seconds()) < 1e-3

    def test_monotonic_and_order_preserving(self):
        from itertools import pairwise

        pts = [EARLIEST + timedelta(days=d) for d in range(0, 150, 3)]
        warped = [timewarp.apply(p) for p in pts]
        assert warped == sorted(warped)
        assert all(a < b for a, b in pairwise(warped))

    def test_everything_lands_inside_the_window(self):
        for d in range(151):
            w = timewarp.apply(EARLIEST + timedelta(days=d))
            assert ANCHOR - WINDOW <= w <= ANCHOR

    def test_durations_scale_by_the_same_factor(self):
        scale = WINDOW.total_seconds() / (ANCHOR - EARLIEST).total_seconds()
        a = EARLIEST + timedelta(days=40)
        b = a + timedelta(days=6)  # a six-day comp pass
        warped = (timewarp.apply(b) - timewarp.apply(a)).total_seconds()
        assert abs(warped - timedelta(days=6).total_seconds() * scale) < 1e-3

    def test_future_timestamps_extrapolate_forward(self):
        # A log line an hour past `now` stays just past the warped `now`.
        assert timewarp.apply(ANCHOR + timedelta(hours=1)) > ANCHOR

    def test_reset_restores_identity(self):
        timewarp.reset()
        assert timewarp.apply(EARLIEST) == EARLIEST


def test_configure_rejects_earliest_after_anchor():
    with pytest.raises(ValueError):
        timewarp.configure(anchor=ANCHOR, earliest=ANCHOR + timedelta(days=1), window=WINDOW)


class TestTheGlobalIsScopedRatherThanAmbient:
    """It is process-wide by design -- it is read three layers below anyone who
    knows a warp exists -- but "the last call wins" is a bad way to discover
    that two are live."""

    def test_configuring_the_same_warp_twice_is_fine(self):
        for _ in range(2):
            timewarp.configure(anchor=ANCHOR, earliest=EARLIEST,
                               window=timedelta(minutes=45))
        assert timewarp.is_active()

    def test_replacing_a_live_warp_with_a_different_one_is_refused(self):
        """The symptom would be one series on two time bases, which reads as
        corrupt data rather than as a misuse of this module."""
        timewarp.configure(anchor=ANCHOR, earliest=EARLIEST, window=timedelta(minutes=45))
        with pytest.raises(RuntimeError, match="already active"):
            timewarp.configure(anchor=ANCHOR, earliest=EARLIEST,
                               window=timedelta(minutes=10))

    def test_warped_restores_whatever_was_there_before(self):
        timewarp.configure(anchor=ANCHOR, earliest=EARLIEST, window=timedelta(minutes=45))
        outer = timewarp.describe()
        with timewarp.warped(anchor=ANCHOR, earliest=EARLIEST,
                             window=timedelta(minutes=5)):
            assert timewarp.describe() != outer
        assert timewarp.describe() == outer

    def test_warped_restores_after_a_failure_too(self):
        assert not timewarp.is_active()
        with pytest.raises(RuntimeError, match="seeding blew up"), timewarp.warped(
                anchor=ANCHOR, earliest=EARLIEST, window=timedelta(minutes=5)):
            raise RuntimeError("seeding blew up")
        assert not timewarp.is_active()
