"""The seeder's metric derivation -- specifically the invariants dashboards
and alerts depend on."""

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from bridge.metrics import FORBIDDEN_LABELS, MetricBackfill
from bridge.ontology import Metric
from bridge.testing import CollectingMetricExporter
from seed import refresh
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


# --------------------------------------------------------------------------- #
# `seed.refresh` -- the loop around the seeder
#
# It had no behavioural test at all, only a check that its entry point exists,
# and it shipped two faults that a single run would have shown: a failure
# reported the wrong stream, and a standing failure looped on it forever.
# --------------------------------------------------------------------------- #


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_a_failed_seed_reports_stderr_not_the_last_thing_that_worked(monkeypatch, capsys):
    """The seeder's own explanation has to survive the loop that wraps it.

    `seed.populate` prints "simulating the show..." before anything can fail, so
    a report built from the last line of stdout could only ever name the one
    step that succeeded. Every misconfiguration announced itself as
    ``FAILED: simulating the show...`` and the sentence naming the unset
    variable -- and the command that fixes it -- was discarded.
    """
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _completed(
            2,
            stdout="simulating the show...\n",
            stderr="TURNAROUND_PSEUDONYM_SALT is unset or still the placeholder.\n"
                   '  echo "TURNAROUND_PSEUDONYM_SALT=$(openssl rand -hex 16)" >> .env',
        ),
    )
    assert refresh._seed() is False
    err = capsys.readouterr().err
    assert "TURNAROUND_PSEUDONYM_SALT" in err
    assert "openssl rand" in err, "the fix was clipped out of the message"
    assert "simulating the show" not in err


def test_a_seed_that_fails_on_the_first_try_stops_instead_of_looping(monkeypatch):
    """Every failure this command actually hits is a standing one.

    An unset salt or an unfilled token does not heal in fifteen minutes, so
    retrying on a timer produces the same error forever. It used to do exactly
    that -- and with the reason swallowed by the bug above, the command looked
    like it had hung.
    """
    monkeypatch.setattr(sys, "argv", ["turnaround-refresh"])
    monkeypatch.setattr(refresh, "_seed", lambda: False)
    monkeypatch.setattr(
        refresh.time, "sleep",
        lambda _: pytest.fail("looped on a failure that will not fix itself"),
    )
    with pytest.raises(SystemExit) as exit_:
        refresh.main()
    assert exit_.value.code == 1


def test_a_first_seed_that_works_goes_on_to_loop(monkeypatch):
    """The other half: past one success a failure is transient and worth a retry."""
    monkeypatch.setattr(sys, "argv", ["turnaround-refresh", "--interval", "0"])
    seeds = iter([True, False, True])
    monkeypatch.setattr(refresh, "_seed", lambda: next(seeds))

    def stop_after(_):
        if next(ticks) >= 2:
            raise KeyboardInterrupt

    ticks = iter(range(10))
    monkeypatch.setattr(refresh.time, "sleep", stop_after)
    with pytest.raises(KeyboardInterrupt):
        refresh.main()
    assert next(seeds, None) is None, "the loop stopped at the transient failure"
