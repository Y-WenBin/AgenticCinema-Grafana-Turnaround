"""The ontology: production vocabulary <-> telemetry primitives.

This module is the single source of truth for how a film production is
represented as observability data. Every other component -- the taps, the
dashboards, the agent prompts -- derives its vocabulary from here, so that a
producer's question and a PromQL query are talking about the same thing.

The central mapping:

    shot                -> trace          (trace_id derived from shot id)
    task stage          -> span           (previz -> layout -> ... -> DI)
    retake / rejection  -> span error + retry
    status transition   -> Loki line
    schedule + farm     -> Mimir series

The join that makes this product possible lives in `parse_opencue_job_name`.
OpenCue's PyOutline names every job "<show>-<shot>-<user>_<name>", so the shot
id is already present in farm telemetry. Relabelling on it lets a single query
span the creative schedule and the compute that serves it -- two planes that in
a real studio are owned by different departments and never joined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


class Department(str, Enum):
    """Pipeline stages in dependency order.

    Order matters: it defines span nesting, the burndown's expected progression,
    and which stage is "upstream" when the agent attributes a slip.
    """

    PREVIZ = "previz"
    LAYOUT = "layout"
    ANIM = "anim"
    FX = "fx"
    LIGHTING = "lighting"
    COMP = "comp"
    DI = "di"

    @property
    def order(self) -> int:
        return _DEPARTMENT_ORDER[self]

    def upstream(self) -> tuple["Department", ...]:
        """Stages this one depends on. Used to attribute a slip to its origin."""
        return tuple(d for d in Department if d.order < self.order)


_DEPARTMENT_ORDER = {d: i for i, d in enumerate(Department)}

#: Departments whose work is rendered on the farm. Only these can accrue render
#: waste, so only these can show the "artist isn't slow, the farm is" signature.
RENDERING_DEPARTMENTS = frozenset(
    {Department.FX, Department.LIGHTING, Department.COMP}
)


class TaskStatus(str, Enum):
    """Kitsu task statuses, normalised.

    A studio can rename these freely in Kitsu, so `from_kitsu` matches loosely
    rather than requiring an exact vocabulary.
    """

    TODO = "todo"
    WIP = "wip"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RETAKE = "retake"
    DONE = "done"

    @classmethod
    def from_kitsu(cls, short_name: str) -> "TaskStatus":
        key = re.sub(r"[^a-z]", "", (short_name or "").lower())
        for pattern, status in _KITSU_STATUS_PATTERNS:
            if pattern in key:
                return status
        return cls.TODO

    @property
    def is_terminal(self) -> bool:
        return self is TaskStatus.DONE

    @property
    def is_rework(self) -> bool:
        """A retake means the work was done once and must be done again.

        This is the single most important signal in the product: the documented
        cause of VFX crunch is shots being corrected or remade until the
        schedule collapses. Rework is that cause, made countable.
        """
        return self is TaskStatus.RETAKE


# Ordered: first match wins, so more specific patterns come first.
_KITSU_STATUS_PATTERNS: list[tuple[str, TaskStatus]] = [
    ("retake", TaskStatus.RETAKE),
    ("reject", TaskStatus.RETAKE),
    ("waitingforapproval", TaskStatus.WAITING_FOR_APPROVAL),
    ("wfa", TaskStatus.WAITING_FOR_APPROVAL),
    ("review", TaskStatus.WAITING_FOR_APPROVAL),
    ("approved", TaskStatus.DONE),
    ("done", TaskStatus.DONE),
    ("final", TaskStatus.DONE),
    ("wip", TaskStatus.WIP),
    ("inprogress", TaskStatus.WIP),
    ("todo", TaskStatus.TODO),
]

#: OpenTelemetry span status per task status. A retake is genuinely an error in
#: the trace sense: work that had to be repeated. Modelling it this way means
#: Tempo's own error-rate tooling counts rework for free.
SPAN_STATUS_ERROR = frozenset({TaskStatus.RETAKE})


# --------------------------------------------------------------------------
# Shot identity
# --------------------------------------------------------------------------

#: Canonical shot id, e.g. "SEQ0420_SH0100". The sequence prefix is part of the
#: id so that sequence-level rollups need no lookup table.
SHOT_ID_RE = re.compile(r"^(?P<sequence>SEQ\d{4})_(?P<shot>SH\d{4})$")


@dataclass(frozen=True, slots=True)
class ShotRef:
    show: str
    sequence: str
    shot: str

    @property
    def shot_id(self) -> str:
        return f"{self.sequence}_{self.shot}"

    @classmethod
    def parse(cls, show: str, shot_id: str) -> "ShotRef":
        m = SHOT_ID_RE.match(shot_id)
        if not m:
            raise ValueError(f"not a canonical shot id: {shot_id!r}")
        return cls(show=show, sequence=m["sequence"], shot=m["shot"])


def sequence_of(shot_id: str) -> str:
    """Sequence for a shot id, without constructing a ShotRef."""
    m = SHOT_ID_RE.match(shot_id)
    if not m:
        raise ValueError(f"not a canonical shot id: {shot_id!r}")
    return m["sequence"]


# --------------------------------------------------------------------------
# The join: OpenCue job name -> shot
# --------------------------------------------------------------------------

#: PyOutline builds job names as "%s-%s-%s_%s" % (show, shot, user, name).
#: Show and user are themselves allowed to contain no spaces but *may* contain
#: hyphens, so we anchor on the shot id, which has a fixed shape. That makes the
#: parse robust against studio-specific show slugs like "night-fall-s2".
OPENCUE_JOB_RE = re.compile(
    r"^(?P<show>.+?)-(?P<shot_id>SEQ\d{4}_SH\d{4})-(?P<user>[^-_]+)_(?P<name>.+)$"
)

#: Trailing version suffix on the outline name, e.g. "comp_v006" -> 6.
_VERSION_RE = re.compile(r"_v(?P<version>\d+)$")


@dataclass(frozen=True, slots=True)
class JobRef:
    """A farm job, resolved back to the creative work it serves."""

    show: str
    shot_id: str
    user: str
    name: str
    department: Department | None
    version: int | None

    @property
    def sequence(self) -> str:
        return sequence_of(self.shot_id)


def parse_opencue_job_name(job_name: str) -> JobRef | None:
    """Resolve an OpenCue job name to the shot and department it renders.

    Returns None for job names that do not follow the convention -- ad-hoc
    tools, test jobs, farm maintenance. Those are real and must not crash the
    tap; they are simply not attributable to a shot, and the caller drops them.
    """
    m = OPENCUE_JOB_RE.match(job_name)
    if not m:
        return None

    name = m["name"]
    version_match = _VERSION_RE.search(name)
    version = int(version_match["version"]) if version_match else None
    stem = _VERSION_RE.sub("", name)

    try:
        department = Department(stem.lower())
    except ValueError:
        department = None

    return JobRef(
        show=m["show"],
        shot_id=m["shot_id"],
        user=m["user"],
        name=name,
        department=department,
        version=version,
    )


def format_opencue_job_name(
    show: str,
    shot_id: str,
    user: str,
    department: Department,
    version: int,
) -> str:
    """Inverse of `parse_opencue_job_name`, used by the seeder.

    Sharing one formatter between the seeder and the parser means the join is
    tested end to end rather than assumed.
    """
    return f"{show}-{shot_id}-{user}_{department.value}_v{version:03d}"


# --------------------------------------------------------------------------
# Telemetry names
# --------------------------------------------------------------------------


class Attr:
    """Span and log attribute keys.

    Namespaced under `production.*` and `farm.*` so they never collide with the
    OTel semantic conventions the AI Observability SDK emits for the agent
    itself -- both land in the same Tempo instance.
    """

    SHOW = "production.show"
    SEQUENCE = "production.sequence"
    SHOT_ID = "production.shot_id"
    DEPARTMENT = "production.department"
    ARTIST = "production.artist"  # pseudonym only; see bridge/privacy.py
    POOL = "production.pool"
    VENDOR = "production.vendor"
    STATUS = "production.status"
    REVISION = "production.revision"
    DUE_DATE = "production.due_date"
    ITERATION = "production.iteration"

    FARM_JOB = "farm.job_name"
    FARM_FRAMES_TOTAL = "farm.frames.total"
    FARM_FRAMES_FAILED = "farm.frames.failed"
    FARM_CORE_HOURS = "farm.core_hours"


class Metric:
    """Mimir series names.

    `turnaround_` prefixed so a studio can drop these into an existing Grafana
    Cloud stack without colliding with their infrastructure metrics.
    """

    # Creative plane, from Kitsu
    SHOTS_APPROVED = "turnaround_shots_approved_total"
    SHOTS_REMAINING = "turnaround_shots_remaining"
    TASK_ITERATIONS = "turnaround_task_iterations_total"
    ARTIST_HOURS = "turnaround_artist_hours_logged"
    VENDOR_TURNAROUND = "turnaround_vendor_turnaround_seconds"

    # Compute plane, from OpenCue, relabelled onto shots
    RENDER_CORE_HOURS = "turnaround_render_core_hours_total"
    RENDER_FRAMES_FAILED = "turnaround_render_frames_failed_total"
    RENDER_FRAMES_TOTAL = "turnaround_render_frames_total"
    RENDER_QUEUE_DEPTH = "turnaround_render_queue_depth"

    # The joined series -- render effort that produced no accepted work.
    # This is the number no studio can currently see.
    RENDER_WASTE_HOURS = "turnaround_render_waste_core_hours_total"


class LokiStream:
    """Loki stream label sets, by event kind."""

    STATUS_CHANGE = "task_status"
    NOTE = "review_note"
    RENDER = "render"
    QC = "delivery_qc"


#: Every stage event is emitted as both a span and a trace-shaped Loki line.
#: Tempo's query tools are not in mcp-grafana's default tool set (TraceQL lives
#: on the separate Cloud Traces MCP endpoint), so the Loki copy guarantees the
#: agent can always reconstruct a shot's history through tools we know exist.
TRACE_SHAPED_LOG_FIELDS = (
    "trace_id",
    "span_id",
    Attr.SHOT_ID,
    Attr.DEPARTMENT,
    Attr.STATUS,
    Attr.ITERATION,
)
