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

import os
import re
from dataclasses import dataclass, field
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

    def upstream(self) -> tuple[Department, ...]:
        """Stages this one depends on. Used to attribute a slip to its origin."""
        return tuple(d for d in Department if d.order < self.order)


_DEPARTMENT_ORDER = {d: i for i, d in enumerate(Department)}

#: Departments whose work is rendered on the farm. Only these can accrue render
#: waste, so only these can show the "artist isn't slow, the farm is" signature.
RENDERING_DEPARTMENTS = frozenset(
    {Department.FX, Department.LIGHTING, Department.COMP}
)


class TaskStatus(str, Enum):
    """A production task status, normalised across trackers.

    Every tracker (Kitsu, ShotGrid / Flow Production Tracking, ftrack, or a
    studio's own) lets a coordinator rename statuses freely, so `from_tracker`
    matches loosely on well-known stems and short codes rather than requiring an
    exact vocabulary. The one status that matters for this product is RETAKE --
    work that must be redone -- so its synonyms are the most thorough.
    """

    TODO = "todo"
    WIP = "wip"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RETAKE = "retake"
    DONE = "done"

    @classmethod
    def from_tracker(cls, short_name: str) -> TaskStatus:
        """Normalise a tracker status name or short code (case/space/underscore
        insensitive). Unknown values fall back to TODO -- the safe default,
        since an unrecognised status has not been shown to be rework or done."""
        key = re.sub(r"[^a-z]", "", (short_name or "").lower())
        for pattern, status in _STATUS_PATTERNS:
            # 3-letter tracker codes (ip, rev, apr, cbb, ...) match the whole
            # key only; longer stems match as a substring of it.
            exact = len(pattern) <= 3
            if (key == pattern) if exact else (pattern in key):
                return status
        return cls.TODO

    #: Back-compat alias; Kitsu is still the reference tracker.
    from_kitsu = from_tracker

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


# Ordered: first match wins, so more specific patterns come first. Covers Kitsu,
# ShotGrid / Flow Production Tracking (three-letter codes: rev, cbb, apr, fin,
# ip, hld, omt, rdy), ftrack ("changes requested", "awaiting approval") and the
# common free-text variants.
_STATUS_PATTERNS: list[tuple[str, TaskStatus]] = [
    ("changesrequested", TaskStatus.RETAKE),
    ("retake", TaskStatus.RETAKE),
    ("reject", TaskStatus.RETAKE),
    ("redo", TaskStatus.RETAKE),
    ("kickback", TaskStatus.RETAKE),
    ("cbb", TaskStatus.RETAKE),               # ShotGrid "could be better"
    ("notestoaddress", TaskStatus.RETAKE),
    ("awaitingapproval", TaskStatus.WAITING_FOR_APPROVAL),
    ("waitingforapproval", TaskStatus.WAITING_FOR_APPROVAL),
    ("pendingreview", TaskStatus.WAITING_FOR_APPROVAL),
    ("wfa", TaskStatus.WAITING_FOR_APPROVAL),
    ("review", TaskStatus.WAITING_FOR_APPROVAL),
    ("rev", TaskStatus.WAITING_FOR_APPROVAL),
    ("submitted", TaskStatus.WAITING_FOR_APPROVAL),
    ("approved", TaskStatus.DONE),
    ("apr", TaskStatus.DONE),
    ("done", TaskStatus.DONE),
    ("final", TaskStatus.DONE),
    ("fin", TaskStatus.DONE),
    ("complete", TaskStatus.DONE),
    ("delivered", TaskStatus.DONE),
    ("omit", TaskStatus.DONE),                # ShotGrid "omt" -- cut, not our concern
    ("omt", TaskStatus.DONE),
    ("notstarted", TaskStatus.TODO),
    ("inprogress", TaskStatus.WIP),
    ("workinprogress", TaskStatus.WIP),
    ("wip", TaskStatus.WIP),
    ("ip", TaskStatus.WIP),
    ("hold", TaskStatus.TODO),
    ("hld", TaskStatus.TODO),
    ("blocked", TaskStatus.TODO),
    ("onhold", TaskStatus.TODO),
    ("ready", TaskStatus.TODO),
    ("rdy", TaskStatus.TODO),
    ("todo", TaskStatus.TODO),
]

#: Deprecated name kept so external imports do not break.
_KITSU_STATUS_PATTERNS = _STATUS_PATTERNS

#: OpenTelemetry span status per task status. A retake is genuinely an error in
#: the trace sense: work that had to be repeated. Modelling it this way means
#: Tempo's own error-rate tooling counts rework for free.
SPAN_STATUS_ERROR = frozenset({TaskStatus.RETAKE})


# --------------------------------------------------------------------------
# Shot identity
# --------------------------------------------------------------------------
#
# Every studio spells a shot id differently -- "SEQ0420_SH0100" here, but also
# "0420_0100", "sq420-sh100", "A012_015". A ShotIdScheme captures one spelling:
# a regex with `sequence` and `shot` groups, plus a template to rebuild the id.
# The active scheme is picked once from TURNAROUND_SHOT_ID_SCHEME (default
# "seq_sh"), so a studio configures its convention without touching code.


@dataclass(frozen=True, slots=True)
class ShotIdScheme:
    name: str
    pattern: re.Pattern[str]
    template: str  # uses {sequence} and {shot}

    def match(self, shot_id: str) -> tuple[str, str] | None:
        m = self.pattern.match(shot_id or "")
        return (m["sequence"], m["shot"]) if m else None

    def sequence(self, shot_id: str) -> str | None:
        got = self.match(shot_id)
        return got[0] if got else None

    def format(self, sequence: str, shot: str) -> str:
        return self.template.format(sequence=sequence, shot=shot)


SHOT_ID_SCHEMES: dict[str, ShotIdScheme] = {
    # the project default: SEQ0420_SH0100
    "seq_sh": ShotIdScheme(
        "seq_sh", re.compile(r"^(?P<sequence>SEQ\d{4})_(?P<shot>SH\d{4})$"),
        "{sequence}_{shot}"),
    # ShotGrid / Flow Production Tracking style: 0420_0100
    "numeric": ShotIdScheme(
        "numeric", re.compile(r"^(?P<sequence>\d{2,4})_(?P<shot>\d{2,4})$"),
        "{sequence}_{shot}"),
    # hyphen-joined, alphanumeric sequence and shot codes: SQ042-SH0100, A012-015
    "dash": ShotIdScheme(
        "dash", re.compile(r"^(?P<sequence>[A-Za-z]{0,4}\d{1,4})-(?P<shot>[A-Za-z]{0,4}\d{1,4})$"),
        "{sequence}-{shot}"),
    # underscore-joined, alphanumeric: sq0420_sh0100, ep02sq10_sh020
    "loose": ShotIdScheme(
        "loose", re.compile(r"^(?P<sequence>[A-Za-z0-9]+?)_(?P<shot>[A-Za-z]{0,4}\d{1,4})$"),
        "{sequence}_{shot}"),
}


def _scheme_from_env() -> ShotIdScheme:
    want = os.environ.get("TURNAROUND_SHOT_ID_SCHEME", "seq_sh").strip().lower()
    return SHOT_ID_SCHEMES.get(want, SHOT_ID_SCHEMES["seq_sh"])


#: Resolved once at import. A studio sets TURNAROUND_SHOT_ID_SCHEME to one of
#: SHOT_ID_SCHEMES, or registers its own ShotIdScheme before import.
DEFAULT_SHOT_ID_SCHEME = _scheme_from_env()

#: Back-compat: the default scheme's compiled pattern. Existing callers that
#: imported SHOT_ID_RE keep working; new code should use a ShotIdScheme.
SHOT_ID_RE = DEFAULT_SHOT_ID_SCHEME.pattern


@dataclass(frozen=True, slots=True)
class ShotRef:
    show: str
    sequence: str
    shot: str
    scheme: ShotIdScheme = field(default=DEFAULT_SHOT_ID_SCHEME, compare=False, repr=False)

    @property
    def shot_id(self) -> str:
        return self.scheme.format(self.sequence, self.shot)

    @classmethod
    def parse(cls, show: str, shot_id: str,
              scheme: ShotIdScheme | None = None) -> ShotRef:
        scheme = scheme or DEFAULT_SHOT_ID_SCHEME
        got = scheme.match(shot_id)
        if not got:
            raise ValueError(f"not a {scheme.name} shot id: {shot_id!r}")
        return cls(show=show, sequence=got[0], shot=got[1], scheme=scheme)


def sequence_of(shot_id: str, scheme: ShotIdScheme | None = None) -> str:
    """Sequence for a shot id, without constructing a ShotRef."""
    scheme = scheme or DEFAULT_SHOT_ID_SCHEME
    seq = scheme.sequence(shot_id)
    if seq is None:
        raise ValueError(f"not a {scheme.name} shot id: {shot_id!r}")
    return seq


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
    scheme: ShotIdScheme = field(default=None, compare=False, repr=False)  # type: ignore[assignment]

    @property
    def sequence(self) -> str:
        return sequence_of(self.shot_id, self.scheme or DEFAULT_SHOT_ID_SCHEME)


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
# The join, generalised: any render manager's job name -> shot
# --------------------------------------------------------------------------
#
# OpenCue is the reference farm, but the same relabel-on-shot-id trick works for
# every render manager -- they all put the shot somewhere in the job or batch
# name. A FarmConvention is one manager's naming pattern; the shot id it pulls
# out is validated against the active ShotIdScheme, so a job that does not name a
# real shot (farm maintenance, a bake, a tool test) resolves to None and the tap
# drops it, exactly as with OpenCue.

#: Short department codes seen in job/scene names across studios, mapped to the
#: canonical pipeline stage. Unknown codes (matchmove, roto, paint, ...) stay
#: None -- still attributable to a shot, just not to a stage we model.
_DEPT_ALIASES: dict[str, Department] = {
    "pvz": Department.PREVIZ, "pv": Department.PREVIZ, "prev": Department.PREVIZ,
    "lay": Department.LAYOUT, "lyt": Department.LAYOUT,
    "anm": Department.ANIM, "ani": Department.ANIM, "anim": Department.ANIM,
    "efx": Department.FX, "fx": Department.FX, "sim": Department.FX,
    "lgt": Department.LIGHTING, "light": Department.LIGHTING, "lit": Department.LIGHTING,
    "cmp": Department.COMP, "comp": Department.COMP, "cmpst": Department.COMP,
    "di": Department.DI, "difinish": Department.DI, "grade": Department.DI,
}


def department_from_token(token: str) -> Department | None:
    """Resolve a department name or short code, or None if it is not a stage
    this pipeline models."""
    key = re.sub(r"[^a-z]", "", (token or "").lower())
    if not key:
        return None
    try:
        return Department(key)
    except ValueError:
        return _DEPT_ALIASES.get(key)


# A permissive shot-id token for the built-in patterns: letters then digits,
# a separator, letters then digits -- "SEQ0420_SH0100", "0420-0100", "a12_015".
# What it captures is always re-validated by the active ShotIdScheme.
_SHOT_TOKEN = r"[A-Za-z]{0,4}\d{1,4}[_-][A-Za-z]{0,4}\d{1,4}"
_VER = r"(?:[ ._/v-]+v?(?P<version>\d{1,4}))?"
_DEPT = r"(?P<dept>[A-Za-z]{2,12})"


class FarmConvention(str, Enum):
    OPENCUE = "opencue"          # <show>-<shot>-<user>_<dept>_v###   (PyOutline)
    DEADLINE = "deadline"        # <shot> <dept> v###  /  <shot>_<dept>_v###
    TRACTOR = "tractor"          # [show] <shot> <dept> [v###]        (job title)
    QUBE = "qube"                # <shot>_<dept>[_v###]
    ROYALRENDER = "royalrender"  # <shot>.<dept>[.v###]  (scene stem)
    TEMPLATE = "template"        # TURNAROUND_FARM_JOB_PATTERN regex


_CONVENTION_PATTERNS: dict[FarmConvention, re.Pattern[str]] = {
    FarmConvention.DEADLINE: re.compile(
        rf"(?P<shot_id>{_SHOT_TOKEN})[ _/-]+{_DEPT}{_VER}", re.IGNORECASE),
    FarmConvention.TRACTOR: re.compile(
        rf"(?:(?P<show>\S+)\s+)?(?P<shot_id>{_SHOT_TOKEN})\s+{_DEPT}{_VER}", re.IGNORECASE),
    FarmConvention.QUBE: re.compile(
        rf"(?P<shot_id>{_SHOT_TOKEN})_{_DEPT}{_VER}", re.IGNORECASE),
    FarmConvention.ROYALRENDER: re.compile(
        rf"(?P<shot_id>{_SHOT_TOKEN})[._]{_DEPT}{_VER}", re.IGNORECASE),
}


def _template_pattern() -> re.Pattern[str] | None:
    raw = os.environ.get("TURNAROUND_FARM_JOB_PATTERN", "").strip()
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error:
        return None


def _default_farm_convention() -> FarmConvention:
    want = os.environ.get("TURNAROUND_FARM_CONVENTION", "opencue").strip().lower()
    try:
        return FarmConvention(want)
    except ValueError:
        return FarmConvention.OPENCUE


DEFAULT_FARM_CONVENTION = _default_farm_convention()


def parse_job_name(
    job_name: str,
    *,
    convention: FarmConvention | str | None = None,
    scheme: ShotIdScheme | None = None,
) -> JobRef | None:
    """Resolve a render-manager job (or batch) name to the shot it serves.

    ``convention`` defaults to ``TURNAROUND_FARM_CONVENTION`` (``opencue``).
    ``scheme`` defaults to the active ``ShotIdScheme``. Returns None when the
    name does not carry a real shot id -- a bake, a tool test, farm maintenance.
    """
    conv = FarmConvention(convention) if convention else DEFAULT_FARM_CONVENTION
    if conv is FarmConvention.OPENCUE:
        return parse_opencue_job_name(job_name)

    pattern = _template_pattern() if conv is FarmConvention.TEMPLATE \
        else _CONVENTION_PATTERNS[conv]
    if pattern is None:
        return None
    m = pattern.search((job_name or "").strip())
    if not m:
        return None

    gd = m.groupdict()
    scheme = scheme or DEFAULT_SHOT_ID_SCHEME
    shot_id = gd.get("shot_id")
    if not shot_id and gd.get("sequence") and gd.get("shot"):
        shot_id = scheme.format(gd["sequence"], gd["shot"])
    if not shot_id or scheme.sequence(shot_id) is None:
        return None

    version = int(gd["version"]) if gd.get("version") else None
    return JobRef(
        show=gd.get("show") or "",
        shot_id=shot_id,
        user=gd.get("user") or "",
        name=gd.get("name") or job_name,
        department=department_from_token(gd.get("dept") or ""),
        version=version,
        scheme=scheme,
    )


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

    #: Rostered headcount per pool. Not a load signal -- it exists so the
    #: aggregation floor is a transparent join in a query or alert
    #: (`... and on(pool) turnaround_pool_headcount >= 3`) rather than a magic
    #: list of pool names. di-pool-1 is deliberately 2, so the demo can show a
    #: crunch alert being suppressed rather than assert that it would be.
    POOL_HEADCOUNT = "turnaround_pool_headcount"

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
