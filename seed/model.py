"""Simulate a production, so that crunch emerges rather than being asserted.

The temptation with a demo dataset is to write the punchline into the data:
decide that comp iterations are 6x and the pool is at 68 hours, then emit those
numbers. Nothing is learned from forecasting a constant.

So this simulates the mechanism instead. Two ordinary perturbations are applied
to an otherwise healthy show -- a director note, and a cache regression nobody
connected to the schedule -- and the iteration counts, the render waste and the
crew hours are whatever falls out.

The crunch mechanism is one modelling decision, and it is the honest one: when
a note lands, an artist does not get more calendar, they get more work inside
the calendar they already had. Rework therefore *overlaps* prior passes instead
of extending the schedule, so concurrent load per artist rises. That is what
studio crunch is, and it is why a burndown alone never predicts it.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from bridge.emit import StageEvent
from bridge.ontology import Department, TaskStatus, format_opencue_job_name
from bridge.privacy import pseudonymize

WORKDAY_HOURS = 8.0
SHOW_FILE = Path(__file__).parent / "show.yaml"


@dataclass(frozen=True, slots=True)
class RenderJob:
    """One farm submission, named so the shot id survives into farm telemetry."""

    job_name: str
    shot_id: str
    department: Department
    artist: str
    started_at: datetime
    ended_at: datetime
    frames_total: int
    frames_failed: int
    core_hours: float

    @property
    def wasted_core_hours(self) -> float:
        """Farm time that produced no accepted frame.

        The number no studio can currently see, because computing it requires
        knowing which shot a job belongs to *and* whether that shot's work was
        later thrown away.
        """
        if self.frames_total == 0:
            return 0.0
        return self.core_hours * (self.frames_failed / self.frames_total)


@dataclass(frozen=True, slots=True)
class TimeLog:
    """Hours an artist spent on a day. Pool-level aggregate is what gets reported."""

    day: date
    artist: str
    pool: str
    department: Department
    hours: float


@dataclass(frozen=True, slots=True)
class Annotation:
    """A production event worth marking on every chart."""

    at: datetime
    text: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogLine:
    at: datetime
    stream: str
    shot_id: str
    department: Department
    message: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class ProductionHistory:
    stages: list[StageEvent] = field(default_factory=list)
    renders: list[RenderJob] = field(default_factory=list)
    time_logs: list[TimeLog] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    log_lines: list[LogLine] = field(default_factory=list)
    pool_headcount: dict[str, int] = field(default_factory=dict)

    def weekly_hours_by_pool(self) -> dict[tuple[str, date], float]:
        """Hours per rostered artist, per pool, per ISO week.

        The denominator is the roster, not the artists who happened to log time
        that week. Dividing by active artists makes a quiet week staffed by two
        people look exactly like crunch, which would make the forecast useless.
        """
        totals: dict[tuple[str, date], float] = defaultdict(float)
        for log in self.time_logs:
            week_start = log.day - timedelta(days=log.day.weekday())
            totals[(log.pool, week_start)] += log.hours
        return {
            (pool, week): total / max(self.pool_headcount.get(pool, 1), 1)
            for (pool, week), total in totals.items()
        }

    def iterations_by_sequence_stage(self) -> dict[tuple[str, Department], float]:
        """Mean iteration count -- the leading indicator of a slip."""
        peak: dict[tuple[str, Department], int] = {}
        for stage in self.stages:
            key = (stage.shot_id, stage.department)
            peak[key] = max(peak.get(key, 0), stage.iteration)
        grouped: dict[tuple[str, Department], list[int]] = defaultdict(list)
        for (shot_id, dept), iterations in peak.items():
            grouped[(shot_id.split("_")[0], dept)].append(iterations)
        return {k: sum(v) / len(v) for k, v in grouped.items()}


class ShowSimulation:
    def __init__(self, config: dict[str, Any], *, now: datetime | None = None) -> None:
        self.cfg = config
        show = config["show"]
        self.slug: str = show["slug"]
        self.seed: int = show["seed"]
        # Everything after `now` is the future and must not exist yet: a demo
        # that shows completed work in the future is instantly unconvincing.
        self.now = now or datetime.now(UTC)
        # Anchored to `now` rather than to fixed dates, so the show is always
        # mid-flight whenever it is seeded.
        self.start = _midnight(
            (self.now - timedelta(days=show["start_days_before_now"])).date()
        )
        self.delivery: date = (
            self.now + timedelta(days=show["delivery_days_after_now"])
        ).date()
        self._pools = self._build_pools()
        self._next_artist: dict[str, int] = defaultdict(int)

    @classmethod
    def load(cls, path: Path = SHOW_FILE, *, now: datetime | None = None) -> ShowSimulation:
        return cls(yaml.safe_load(path.read_text()), now=now)

    # -- setup ------------------------------------------------------------

    def _build_pools(self) -> dict[Department, list[dict[str, Any]]]:
        """Pools by department, each with its stable pseudonymous roster."""
        by_department: dict[Department, list[dict[str, Any]]] = defaultdict(list)
        for pool_name, spec in self.cfg["pools"].items():
            dept = Department(spec["department"])
            roster = [
                pseudonymize(f"{self.slug}:{pool_name}:{i}") for i in range(spec["artists"])
            ]
            by_department[dept].append(
                {"name": pool_name, "vendor": spec["vendor"], "roster": roster}
            )
        return dict(by_department)

    def _beat_date(self, beat: dict) -> date:
        return (self.now - timedelta(days=beat["days_before_now"])).date()

    def _active_beats(self, dept: Department, sequence: str, when: datetime) -> list[dict]:
        """Story beats in force for this stage at this moment."""
        return [
            beat
            for beat in self.cfg["story"]
            if beat["sequence"] == sequence
            and Department(beat["stage"]) is dept
            and when.date() >= self._beat_date(beat)
        ]

    # -- simulation -------------------------------------------------------

    def run(self) -> ProductionHistory:
        history = ProductionHistory(
            pool_headcount={
                name: spec["artists"] for name, spec in self.cfg["pools"].items()
            }
        )
        for beat in self.cfg["story"]:
            if beat["kind"] == "creative_note":
                history.annotations.append(
                    Annotation(
                        at=_midnight(self._beat_date(beat)),
                        text=" ".join(beat["annotation"].split()),
                        tags=("director-note", beat["sequence"], self.slug),
                    )
                )

        shot_index = 0
        total_shots = sum(s["shots"] for s in self.cfg["sequences"])
        # Staggered across elapsed time, not the delivery window: shots must
        # enter early enough to have reached the department where the story
        # happens by the time anyone looks.
        entry_span = (self.now - self.start).days * self.cfg["show"]["entry_span_fraction"]

        for seq in self.cfg["sequences"]:
            for n in range(seq["shots"]):
                shot_id = f"{seq['id']}_SH{(n + 1) * 10:04d}"
                # Shots enter the pipeline staggered across the show's window,
                # so at `now` the early sequences are delivered and act three
                # is still in flight -- the shape of any real production.
                offset = int(entry_span * (shot_index / total_shots))
                self._simulate_shot(
                    history, seq, shot_id, self.start + timedelta(days=offset)
                )
                shot_index += 1

        history.stages.sort(key=lambda s: s.started_at)
        history.renders.sort(key=lambda r: r.started_at)
        return history

    def _simulate_shot(
        self,
        history: ProductionHistory,
        seq: dict[str, Any],
        shot_id: str,
        entry: datetime,
    ) -> None:
        # Seeded per shot so that adding a sequence does not reshuffle the
        # rest of the show. Reproducibility has to survive editing show.yaml.
        rng = random.Random(f"{self.seed}:{shot_id}")
        complexity = seq["complexity"]
        cursor = entry

        for stage_name in seq["stages"]:
            dept = Department(stage_name)
            spec = self.cfg["departments"][stage_name]
            pool = self._choose_pool(dept, rng)
            artist = self._assign_artist(pool)
            multiplier = self.cfg["vendors"][pool["vendor"]]["turnaround_multiplier"]

            stage_start = cursor
            iteration = 1
            while iteration <= self.cfg["rework"]["max_iterations"]:
                duration_days = max(
                    0.25,
                    rng.gauss(spec["days"], spec["spread"]) * complexity * multiplier,
                )
                started = stage_start if iteration == 1 else cursor
                ended = started + timedelta(days=duration_days)
                if started >= self.now:
                    return  # not started yet; the future does not exist

                beats = self._active_beats(dept, seq["id"], started)
                retaken = self._decide_retake(dept, complexity, beats, iteration, rng)
                truncated = ended > self.now

                history.stages.append(
                    StageEvent(
                        show=self.slug,
                        shot_id=shot_id,
                        department=dept,
                        artist=artist,
                        vendor=pool["vendor"],
                        pool=pool["name"],
                        status=(
                            TaskStatus.WIP
                            if truncated
                            else TaskStatus.RETAKE
                            if retaken
                            else TaskStatus.DONE
                        ),
                        iteration=iteration,
                        started_at=started,
                        ended_at=min(ended, self.now),
                        due_date=self.delivery.isoformat(),
                        revision=iteration,
                    )
                )
                self._log_effort(history, dept, pool, artist, started, min(ended, self.now), spec, complexity)
                if "render_hours" in spec:
                    self._submit_render(history, shot_id, dept, artist, started, min(ended, self.now), spec, iteration, beats, rng)

                if truncated:
                    return
                if not retaken:
                    cursor = ended
                    break

                # The crunch mechanism: a retake does not buy calendar. The next
                # pass starts almost immediately and overlaps the work already
                # in this artist's window, so concurrent load rises.
                cursor = ended + timedelta(days=rng.uniform(0.1, 0.6))
                iteration += 1
            else:
                cursor = cursor + timedelta(days=1)

    def _choose_pool(self, dept: Department, rng: random.Random) -> dict[str, Any]:
        pools = self._pools[dept]
        return pools[0] if len(pools) == 1 else rng.choice(pools)

    def _assign_artist(self, pool: dict[str, Any]) -> str:
        """Round-robin within the pool.

        Random assignment piles work onto unlucky artists purely by sampling,
        which would show up as crunch that no scheduling decision caused. Even
        distribution means any overload we forecast is real.
        """
        roster = pool["roster"]
        index = self._next_artist[pool["name"]] % len(roster)
        self._next_artist[pool["name"]] += 1
        return roster[index]

    def _decide_retake(
        self,
        dept: Department,
        complexity: float,
        beats: list[dict],
        iteration: int,
        rng: random.Random,
    ) -> bool:
        rate = self.cfg["rework"]["base_retake_rate"][dept.value] * complexity
        for beat in beats:
            rate *= beat.get("retake_multiplier", 1.0)
        # Notes get less likely each pass; nobody retakes forever.
        rate *= 0.82 ** (iteration - 1)
        return rng.random() < min(rate, 0.93)

    def _log_effort(
        self,
        history: ProductionHistory,
        dept: Department,
        pool: dict[str, Any],
        artist: str,
        started: datetime,
        ended: datetime,
        spec: dict[str, Any],
        complexity: float,
    ) -> None:
        """Spread this pass's effort across the days it occupied.

        Effort is a property of the work, not of the calendar, so overlapping
        passes accumulate on the same days. That accumulation is the crunch.
        """
        effort_hours = spec["days"] * WORKDAY_HOURS * complexity
        days = max((ended.date() - started.date()).days, 1)
        per_day = effort_hours / days
        for offset in range(days):
            history.time_logs.append(
                TimeLog(
                    day=started.date() + timedelta(days=offset),
                    artist=artist,
                    pool=pool["name"],
                    department=dept,
                    hours=per_day,
                )
            )

    def _submit_render(
        self,
        history: ProductionHistory,
        shot_id: str,
        dept: Department,
        artist: str,
        started: datetime,
        ended: datetime,
        spec: dict[str, Any],
        iteration: int,
        beats: list[dict],
        rng: random.Random,
    ) -> None:
        frames_total = rng.randint(90, 160)
        core_hours = spec["render_hours"]
        failure_rate = 0.02  # healthy farms still lose the odd frame

        for beat in beats:
            if beat["kind"] != "farm_regression":
                continue
            failure_rate = beat["frame_failure_rate"]
            core_hours *= beat["render_hours_multiplier"]
            history.log_lines.append(
                LogLine(
                    at=started + timedelta(hours=1),
                    stream="render",
                    shot_id=shot_id,
                    department=dept,
                    message=beat["log_signature"] % iteration,
                    labels={"level": "error", "frame": str(beat["failing_frame"])},
                )
            )

        frames_failed = int(frames_total * failure_rate)
        history.renders.append(
            RenderJob(
                job_name=format_opencue_job_name(
                    self.slug, shot_id, artist, dept, iteration
                ),
                shot_id=shot_id,
                department=dept,
                artist=artist,
                started_at=started,
                ended_at=ended,
                frames_total=frames_total,
                frames_failed=frames_failed,
                core_hours=core_hours,
            )
        )


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
