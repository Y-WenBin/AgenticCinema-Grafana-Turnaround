"""Push the simulated show into Grafana Cloud.

Run with --dry-run to exercise the whole path against in-memory exporters and
print what would be written. That matters more than it sounds: almost every
mistake in a backfill (a counter that never accumulates, a label that explodes
cardinality, a timestamp in the wrong unit) is invisible until the data is
already in the stack and awkward to remove.

    uv run python -m seed.populate --dry-run
    uv run python -m seed.populate            # needs .env configured
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from bridge import timewarp
from bridge.emit import Emitter
from bridge.metrics import MetricBackfill, Point
from bridge.ontology import Attr, Department, Metric, TaskStatus
from seed.model import ProductionHistory, ShowSimulation

WORKWEEK_HOURS = 40.0

#: Hosted Mimir/Loki refuse samples older than a ~1h out-of-order window, so the
#: whole five-month history is compressed into this many minutes of wall-clock
#: time ending at "now". Left of an hour with margin for the seeder's own
#: runtime and clock skew. Override with --compress; --compress 0 disables it
#: (real timestamps, for a self-hosted stack with the window widened).
DEFAULT_COMPRESS_MINUTES = 45


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def build_metrics(history: ProductionHistory, backfill: MetricBackfill) -> None:
    """Derive every series from the history.

    Label sets are chosen so that nothing is keyed by a person, and so that the
    creative plane and the compute plane share `sequence` and `department` --
    that shared key is what lets one query span both.
    """
    # -- creative plane ---------------------------------------------------
    approvals: list[Point] = []
    iterations: list[Point] = []
    durations: list[Point] = []
    for stage in history.stages:
        sequence = stage.shot_id.split("_")[0]
        iterations.append(
            Point(
                stage.started_at,
                1.0,
                {"sequence": sequence, "department": stage.department.value},
            )
        )
        durations.append(
            Point(
                stage.ended_at,
                stage.duration_seconds,
                {"vendor": stage.vendor, "department": stage.department.value},
            )
        )
        if stage.department is Department.DI and stage.status is TaskStatus.DONE:
            approvals.append(Point(stage.ended_at, 1.0, {"sequence": sequence}))

    backfill.counter(
        Metric.SHOTS_APPROVED,
        approvals,
        unit="{shot}",
        description="Shots signed off through DI.",
    )
    backfill.counter(
        Metric.TASK_ITERATIONS,
        iterations,
        unit="{pass}",
        description="Department passes, including rework. The leading indicator of a slip.",
    )
    backfill.gauge(
        Metric.VENDOR_TURNAROUND,
        durations,
        unit="s",
        description="Wall-clock time for one department pass.",
    )

    # -- crew load --------------------------------------------------------
    # Weekly hours per *rostered* artist. Averaging over whoever logged time
    # that week makes a quiet week staffed by two people look like crunch.
    weekly: dict[tuple[str, Department, date], float] = defaultdict(float)
    for log in history.time_logs:
        weekly[(log.pool, log.department, _week_start(log.day))] += log.hours

    backfill.gauge(
        Metric.ARTIST_HOURS,
        [
            Point(
                _midnight(week),
                hours / max(history.pool_headcount.get(pool, 1), 1),
                {"pool": pool, "department": dept.value},
            )
            for (pool, dept, week), hours in weekly.items()
        ],
        unit="h",
        description=(
            "Hours per rostered artist per week. Reported per pool only; "
            "per-person series are refused by bridge/metrics.py."
        ),
    )

    # -- compute plane, keyed onto the creative plane ---------------------
    core_hours: list[Point] = []
    frames_total: list[Point] = []
    frames_failed: list[Point] = []
    waste: list[Point] = []
    for render in history.renders:
        labels = {
            "sequence": render.shot_id.split("_")[0],
            "department": render.department.value,
        }
        core_hours.append(Point(render.ended_at, render.core_hours, labels))
        frames_total.append(Point(render.ended_at, float(render.frames_total), labels))
        frames_failed.append(Point(render.ended_at, float(render.frames_failed), labels))
        waste.append(Point(render.ended_at, render.wasted_core_hours, labels))

    backfill.counter(Metric.RENDER_CORE_HOURS, core_hours, unit="h")
    backfill.counter(Metric.RENDER_FRAMES_TOTAL, frames_total, unit="{frame}")
    backfill.counter(Metric.RENDER_FRAMES_FAILED, frames_failed, unit="{frame}")
    backfill.counter(
        Metric.RENDER_WASTE_HOURS,
        waste,
        unit="h",
        description=(
            "Farm hours that produced no accepted frame, attributed to the shot "
            "that caused them. Requires the OpenCue job-name join; this is the "
            "number a studio cannot currently see."
        ),
    )


def emit_history(history: ProductionHistory, emitter: Emitter) -> tuple[int, int]:
    """Write spans and trace-shaped log lines. Returns (spans, logs)."""
    for stage in history.stages:
        emitter.emit_stage(stage)

    logs = 0
    for line in history.log_lines:
        emitter.emit_log(
            at=line.at,
            message=line.message,
            shot_id=line.shot_id,
            department=line.department.value,
            iteration=1,
            attributes={"stream": line.stream, **line.labels},
            severity=line.labels.get("level", "info").upper(),
        )
        logs += 1

    # Status transitions are the producer-legible history of a shot, and the
    # path the agent falls back to when it cannot reach a trace.
    for stage in history.stages:
        emitter.emit_log(
            at=stage.ended_at,
            message=(
                f"{stage.department.value} {stage.shot_id} -> {stage.status.value} "
                f"(iteration {stage.iteration}, {stage.vendor})"
            ),
            shot_id=stage.shot_id,
            department=stage.department.value,
            iteration=stage.iteration,
            attributes={
                "stream": "task_status",
                Attr.STATUS: stage.status.value,
                Attr.VENDOR: stage.vendor,
                Attr.POOL: stage.pool,
            },
            severity="WARN" if stage.status.is_rework else "INFO",
        )
        logs += 1
    return len(history.stages), logs


def summarise(history: ProductionHistory) -> str:
    delivered = {
        s.shot_id
        for s in history.stages
        if s.department is Department.DI and s.status is TaskStatus.DONE
    }
    weekly = history.weekly_hours_by_pool()
    weeks = sorted({w for _, w in weekly})[-4:]
    hottest = max(
        ((pool, weekly.get((pool, weeks[-1]), 0.0)) for pool in history.pool_headcount),
        key=lambda kv: kv[1],
    )
    waste: dict[str, float] = defaultdict(float)
    for render in history.renders:
        waste[render.shot_id.split("_")[0]] += render.wasted_core_hours
    worst = max(waste.items(), key=lambda kv: kv[1])
    return "\n".join(
        [
            f"  shots delivered     {len(delivered)}/200",
            f"  stages              {len(history.stages)}",
            f"  render jobs         {len(history.renders)}",
            f"  hottest pool        {hottest[0]} at {hottest[1]:.1f} h/artist this week",
            f"  worst render waste  {worst[0]} at {worst[1]:.0f} core-hours",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the full path against in-memory exporters and report",
    )
    parser.add_argument(
        "--compress",
        type=float,
        default=DEFAULT_COMPRESS_MINUTES,
        metavar="MINUTES",
        help=(
            "compress the whole history into this many minutes of wall-clock "
            f"time so hosted Mimir/Loki accept it (default {DEFAULT_COMPRESS_MINUTES}; "
            "0 keeps real timestamps)"
        ),
    )
    args = parser.parse_args()

    print("simulating the show...")
    sim = ShowSimulation.load()
    history = sim.run()
    print(summarise(history))

    if args.compress > 0:
        # Pad the low end: weekly buckets round back to Monday, up to 6 days
        # before the earliest logged day.
        timewarp.configure(
            anchor=sim.now,
            earliest=sim.start - timedelta(days=7),
            window=timedelta(minutes=args.compress),
        )
    print(timewarp.describe())

    if args.dry_run:
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from bridge.testing import CollectingMetricExporter

        emitter = Emitter(
            span_exporter=InMemorySpanExporter(), log_exporter=InMemoryLogRecordExporter()
        )
        backfill = MetricBackfill(exporter=CollectingMetricExporter())
    else:
        emitter = Emitter()
        backfill = MetricBackfill()

    print("\nwriting traces and logs...")
    spans, logs = emit_history(history, emitter)
    emitter.flush()
    emitter.shutdown()

    print("building metric series...")
    build_metrics(history, backfill)
    points = backfill.point_count
    written = backfill.flush()

    print(
        f"\n  spans  {spans}\n  logs   {logs}\n  metric points {written} "
        f"(buffered {points})"
    )
    print("\ndry run: nothing left this machine." if args.dry_run else "\nwritten to Grafana Cloud.")


if __name__ == "__main__":
    main()
