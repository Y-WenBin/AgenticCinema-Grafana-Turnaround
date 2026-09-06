"""Source adapters: the seam between a studio's tools and the ontology.

Turnaround's value is the *join* -- creative schedule against render farm on a
shared shot id -- not any one vendor's API. So the ingest side is defined as
three narrow Protocols, and a studio implements the one or two that match its
stack. The reference implementations are Kitsu (``gazu``) and OpenCue; the rest
are a documented mapping plus, where the naming is regular, a ready parser in
``bridge.ontology`` (``parse_job_name``, ``ShotIdScheme``, ``TaskStatus.from_tracker``).

Nothing here imports a vendor SDK. An adapter does, in its own module, so a
studio installs only what it uses.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from bridge.ontology import Department, JobRef, TaskStatus

# --------------------------------------------------------------------------
# Normalised events every adapter yields
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """A status transition on one shot/department task."""

    shot_id: str
    department: Department | None
    status: TaskStatus
    at: datetime
    revision: int | None = None
    artist_pseudonym: str | None = None  # never a real name -- see bridge.privacy


@dataclass(frozen=True, slots=True)
class TimeSpent:
    """Hours logged against a shot/department, already aggregated to a pool."""

    shot_id: str
    department: Department | None
    pool: str
    hours: float
    week_starting: datetime


@dataclass(frozen=True, slots=True)
class RenderRecord:
    """One farm job, resolved back to the shot it served."""

    job: JobRef
    core_hours: float
    frames_total: int
    frames_failed: int
    at: datetime


@dataclass(frozen=True, slots=True)
class CutItem:
    """One clip in an editorial cut, mapped to the shot it represents."""

    shot_id: str
    source_name: str            # clip / reel name as it appears in the NLE
    record_in_seconds: float
    duration_seconds: float
    revision: int | None = None


# --------------------------------------------------------------------------
# The three adapter Protocols
# --------------------------------------------------------------------------


@runtime_checkable
class ScheduleSource(Protocol):
    """A production tracker: shots, task statuses, logged time.

    Reference adapter: Kitsu via ``gazu``. Others map cleanly -- see
    ``SUPPORTED_TOOLS['schedule']`` -- because status vocabularies are
    normalised by ``TaskStatus.from_tracker`` and hours are pooled before they
    leave the adapter (``bridge.privacy.aggregation_floor``).
    """

    def iter_task_events(self) -> Iterable[TaskEvent]: ...

    def iter_time_spent(self) -> Iterable[TimeSpent]: ...


@runtime_checkable
class FarmSource(Protocol):
    """A render manager: per-job core-hours, frame counts, failures.

    Reference adapter: OpenCue's Prometheus exporter. Any manager works if its
    job or batch names carry the shot id -- ``bridge.ontology.parse_job_name``
    ships patterns for Deadline, Tractor, Qube! and Royal Render, and a
    ``template`` mode for a studio regex.
    """

    def iter_renders(self) -> Iterable[RenderRecord]: ...


@runtime_checkable
class EditorialSource(Protocol):
    """An NLE / conform: which shots are in the current cut and how long.

    Reference adapter: an EDL (CMX3600) or OpenTimelineIO file
    (``bridge.editorial``). Avid, Premiere, Resolve and Final Cut all export one
    or both. Clip names are mapped to shot ids with the active ``ShotIdScheme``.
    """

    def iter_cut(self) -> Iterable[CutItem]: ...


# --------------------------------------------------------------------------
# Support matrix -- what "accommodates most VFX / editorial tools" means here
# --------------------------------------------------------------------------


class Support(str, Enum):
    NATIVE = "native"            # reference adapter in-tree
    PARSER = "parser"            # regular naming -> a ready parser, adapter is thin
    MAPPING = "mapping"          # status/field mapping documented; adapter is a stub
    ALIAS = "alias"              # same product under another name


@dataclass(frozen=True, slots=True)
class ToolSupport:
    tool: str
    kind: Support
    via: str
    note: str = ""


#: Grouped by adapter Protocol. This is the honest answer to "which tools does
#: this work with": the join needs a shot id and a status, and these are the
#: routes to them for the tools a real VFX / post pipeline uses.
SUPPORTED_TOOLS: dict[str, tuple[ToolSupport, ...]] = {
    "schedule": (
        ToolSupport("kitsu", Support.NATIVE, "gazu",
                    "reference ScheduleSource + write-back"),
        ToolSupport("shotgrid", Support.MAPPING, "shotgun_api3 / shotgun-api3",
                    "status codes rev/cbb/apr/fin/ip/hld normalised by from_tracker"),
        ToolSupport("flow_production_tracking", Support.ALIAS, "shotgun_api3",
                    "Autodesk's 2024 rename of ShotGrid"),
        ToolSupport("ftrack", Support.MAPPING, "ftrack_api",
                    "'Changes requested' / 'Awaiting approval' normalised"),
        ToolSupport("autodesk_flow", Support.ALIAS, "shotgun_api3", ""),
        ToolSupport("csv_export", Support.PARSER, "bridge.sources.CsvScheduleSource",
                    "any tracker that exports shot,status,hours rows"),
    ),
    "farm": (
        ToolSupport("opencue", Support.NATIVE, "prometheus exporter :8302",
                    "reference FarmSource; PyOutline job names"),
        ToolSupport("deadline", Support.PARSER, "parse_job_name(convention='deadline')",
                    "<shot> <dept> v### batch names"),
        ToolSupport("tractor", Support.PARSER, "parse_job_name(convention='tractor')",
                    "Pixar Tractor job titles"),
        ToolSupport("qube", Support.PARSER, "parse_job_name(convention='qube')",
                    "<shot>_<dept>_v### job names"),
        ToolSupport("royalrender", Support.PARSER, "parse_job_name(convention='royalrender')",
                    "scene-stem job names"),
        ToolSupport("slurm", Support.PARSER, "parse_job_name(convention='template')",
                    "TURNAROUND_FARM_JOB_PATTERN regex"),
        ToolSupport("aws_thinkbox", Support.ALIAS, "deadline", "Deadline"),
    ),
    "editorial": (
        ToolSupport("edl_cmx3600", Support.NATIVE, "bridge.editorial.parse_edl",
                    "reference EditorialSource; every NLE exports EDL"),
        ToolSupport("opentimelineio", Support.PARSER, "bridge.editorial.iter_otio_cut",
                    "optional dep; Avid/Premiere/Resolve/FCP all round-trip OTIO"),
        ToolSupport("avid_media_composer", Support.MAPPING, "edl / aaf via OTIO", ""),
        ToolSupport("adobe_premiere", Support.MAPPING, "edl / fcpxml via OTIO", ""),
        ToolSupport("davinci_resolve", Support.MAPPING, "edl / otio export", ""),
        ToolSupport("final_cut_pro", Support.MAPPING, "fcpxml via OTIO", ""),
        ToolSupport("autodesk_flame", Support.MAPPING, "edl export", ""),
    ),
}


def supported(category: str) -> tuple[str, ...]:
    return tuple(t.tool for t in SUPPORTED_TOOLS.get(category, ()))


# --------------------------------------------------------------------------
# A tool-agnostic adapter for the common "just give me a CSV" case
# --------------------------------------------------------------------------


@dataclass(slots=True)
class CsvScheduleSource:
    """A ScheduleSource over rows any tracker can export:
    ``shot_id,department,status,pool,hours,at`` (header row required).

    Keeps a studio whose tracker has no Python client -- or no API access from
    where Turnaround runs -- from being blocked: export a report, point this at
    it. Status text is normalised by ``TaskStatus.from_tracker``.
    """

    rows: list[dict[str, str]]

    @classmethod
    def from_csv(cls, text: str) -> CsvScheduleSource:
        import csv
        import io

        return cls(list(csv.DictReader(io.StringIO(text))))

    def iter_task_events(self) -> Iterator[TaskEvent]:
        for r in self.rows:
            if not r.get("status"):
                continue
            yield TaskEvent(
                shot_id=r["shot_id"].strip(),
                department=Department(r["department"].strip().lower())
                if r.get("department") else None,
                status=TaskStatus.from_tracker(r["status"]),
                at=datetime.fromisoformat(r["at"]) if r.get("at") else datetime.now(UTC),
                revision=int(r["revision"]) if r.get("revision") else None,
            )

    def iter_time_spent(self) -> Iterator[TimeSpent]:
        for r in self.rows:
            if not r.get("hours"):
                continue
            yield TimeSpent(
                shot_id=r["shot_id"].strip(),
                department=Department(r["department"].strip().lower())
                if r.get("department") else None,
                pool=r.get("pool", "").strip(),
                hours=float(r["hours"]),
                week_starting=datetime.fromisoformat(r["at"]) if r.get("at")
                else datetime.now(UTC),
            )
