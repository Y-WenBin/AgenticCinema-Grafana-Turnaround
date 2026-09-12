"""Production vocabulary, rendered for a Gemini prompt.

The thesis (PROJECT.md, "The problem") is that the gap between Grafana and a studio is
*vocabulary*, not data. This module is where that gap is closed on the agent
side: it turns :mod:`bridge.ontology` into prose and PromQL an analyst agent can
act on, so the instruction text and the seeded series can never drift apart --
every metric name here is the same constant the seeder emits.

Nothing here talks to a model or a datasource. It is pure string assembly, unit
tested against the ontology so a renamed metric breaks a test rather than a demo.
"""

from __future__ import annotations

from bridge.ontology import Department, Metric

_M = Metric

# --------------------------------------------------------------------------- #
# The metric catalogue -- metric -> (unit, one-line meaning), reading order
# --------------------------------------------------------------------------- #

METRIC_MEANINGS: dict[str, tuple[str, str]] = {
    _M.SHOTS_APPROVED: ("shots", "Cumulative shots signed off through DI. Counter. The burndown numerator."),
    _M.SHOTS_REMAINING: ("shots", "Shots not yet delivered. Gauge. 200 at the start of the show."),
    _M.TASK_ITERATIONS: ("passes", "Cumulative department passes, rework included. Counter. labels: sequence, department. The leading indicator of a slip."),
    _M.ARTIST_HOURS: ("hours", "Hours logged per rostered artist per week, by pool. Gauge. labels: pool, department. Divided by the roster, not by who logged time, so a small crunching team does not hide behind a large idle one."),
    _M.POOL_HEADCOUNT: ("artists", "Rostered artists per pool. Gauge. label: pool. NOT a load signal -- it is the join key for the aggregation floor (below)."),
    _M.VENDOR_TURNAROUND: ("seconds", "Wall-clock a vendor takes per department pass. Gauge. labels: vendor, department."),
    _M.RENDER_CORE_HOURS: ("core-hours", "Farm CPU time, relabelled from OpenCue job names onto the shot they serve. Counter. labels: sequence, department. The compute plane."),
    _M.RENDER_FRAMES_TOTAL: ("frames", "Frames submitted to the farm. Counter. labels: sequence, department."),
    _M.RENDER_FRAMES_FAILED: ("frames", "Frames that came back failed and had to be re-rendered. Counter. labels: sequence, department."),
    _M.RENDER_QUEUE_DEPTH: ("jobs", "Farm jobs waiting. Gauge. labels: sequence, department."),
    _M.RENDER_WASTE_HOURS: ("core-hours", "Farm core-hours that produced no accepted frame, attributed to the shot that caused them. Counter. labels: sequence, department. The number no studio can currently see."),
}

DEPARTMENT_ORDER = " -> ".join(d.value for d in Department)

# --------------------------------------------------------------------------- #
# Query recipes -- written in the compressed time base (see bridge/timewarp.py)
# --------------------------------------------------------------------------- #

# Point-in-time reads must survive a single seed ageing a few minutes past
# "now": the compressed history sits in a ~45-minute window that ended when the
# seeder finished, so a bare instant query at `now` falls outside Prometheus's
# 5-minute lookback and returns nothing. Wrapping the expression in
# `last_over_time((...)[2h:])` and querying instant at `now` returns the last
# real value -- the same trick the alert rules use.
STALE = "2h"


def _latest(expr: str, *, to: float | None = None) -> str:
    """Wrap a PromQL expression so an instant query at 'now' returns its last value.

    ``to`` rounds the result to that step, in PromQL, before anything reads it.
    A producer-facing answer once carried ``lighting-pool-2 at
    63.16190476190476h``, and no prompt could have prevented it: the analysts
    pass tool results through as text, and the synthesis agent is deliberately
    forbidden from deriving new numbers -- which forbids it from rounding them
    too. The only place the long form can be stopped is the place it is
    produced, so it is stopped here and the model never sees it.

    Rounding inside the subquery rather than around it keeps the
    ``last_over_time((`` prefix every recipe is checked for, and is the same
    value either way: rounding is pointwise, so rounding each sample and taking
    the last equals taking the last and rounding it.
    """
    body = expr if to is None else f"round({expr}, {to})"
    return f"last_over_time(({body})[{STALE}:])"


#: Rounding steps, by what the figure *is* rather than by recipe. Hours and
#: core-hours to a tenth (a producer does not act on six minutes); things that
#: are counted to whole units; ratios to a tenth of a percentage point, which is
#: finer than the rest because a frame-failure rate lives near 0.05 and a tenth
#: would erase it.
HOURS, COUNT, RATIO = 0.1, 1, 0.001


#: The product's thesis as one PromQL expression: farm effort per creative
#: iteration, keyed on a label the farm and the schedule now share. Cumulative
#: counters (not increase()) so it is stable on the compressed, single-seed base.
_JOIN_CORE = (
    f'sum by (sequence) ({_M.RENDER_CORE_HOURS}{{department="comp"}})'
    f' / sum by (sequence) ({_M.TASK_ITERATIONS}{{department="comp"}})'
)
THE_JOIN = _latest(_JOIN_CORE, to=HOURS)

_JOIN_TITLE = "Render hours per comp iteration, by sequence (THE JOIN -- start here for 'why is a sequence slipping / what is it costing'). SEQ0420 ~4.5, every other sequence ~2.2."
_FLOOR_TITLE = "Crew load respecting the aggregation floor (pools of >= 3 only) -- use this for any statement about who is in crunch"
_FRAME_FAIL = _latest(
    f"sum by (sequence) ({_M.RENDER_FRAMES_FAILED}) "
    f"/ clamp_min(sum by (sequence) ({_M.RENDER_FRAMES_TOTAL}), 1)",
    to=RATIO,
)
_FLOOR_QUERY = _latest(
    f"(max by (pool) ({_M.ARTIST_HOURS})) and on(pool) ({_M.POOL_HEADCOUNT} >= 3)",
    to=HOURS,
)

# Artist-days/week of comp overtime: per-pool hours above a 40h week, times the
# roster, over an 8h day. `... >= 3` both filters to pools that meet the floor
# and supplies the headcount to multiply by, so di-pool-1 (2 people) never
# contributes. One PromQL the model was previously failing to compose inline.
_OT_HOURS = f'clamp_min((max by (pool) ({_M.ARTIST_HOURS}{{department="comp"}})) - 40, 0)'
_ROSTER_GE_FLOOR = f'on(pool) ({_M.POOL_HEADCOUNT}{{department="comp"}} >= 3)'
_ARTIST_DAYS_PER_POOL = _latest(f"{_OT_HOURS} * {_ROSTER_GE_FLOOR} / 8", to=HOURS)
_ARTIST_DAYS_TOTAL = _latest(f"sum({_OT_HOURS} * {_ROSTER_GE_FLOOR}) / 8", to=HOURS)

#: title -> PromQL. Every recipe is an instant query at endTime='now'.
QUERY_RECIPES: dict[str, str] = {
    _JOIN_TITLE: THE_JOIN,
    "Wasted farm core-hours by sequence, cumulative": _latest(f"sum by (sequence) ({_M.RENDER_WASTE_HOURS})", to=HOURS),
    "Frame-failure rate by sequence": _FRAME_FAIL,
    "Weekly hours per rostered artist, by pool -- crew load": _latest(f"max by (pool) ({_M.ARTIST_HOURS})", to=HOURS),
    _FLOOR_TITLE: _FLOOR_QUERY,
    "Artist-days/week of comp overtime cost, by pool (pools >= floor only) -- use for 'what is it costing in artist-days'": _ARTIST_DAYS_PER_POOL,
    "Artist-days/week of comp overtime cost, whole comp dept (one number)": _ARTIST_DAYS_TOTAL,
    "Delivery burndown (shots signed off; 200 is the whole show)": _latest(f"sum({_M.SHOTS_APPROVED})", to=COUNT),
    "Department passes per sequence, cumulative (rework included)": _latest(f"sum by (sequence) ({_M.TASK_ITERATIONS})", to=COUNT),
    "Comp passes per sequence (how far SEQ0420's rework has actually got)": _latest(f'sum by (sequence) ({_M.TASK_ITERATIONS}{{department="comp"}})', to=COUNT),
}

# --------------------------------------------------------------------------- #
# Shared context every agent gets
# --------------------------------------------------------------------------- #

FLOOR = 3

PRIVACY_RULE = (
    "PRIVACY (non-negotiable): never name or single out an individual artist. No series carries an"
    f" artist identity. A pool with fewer than {FLOOR} rostered artists (check {_M.POOL_HEADCOUNT})"
    " is BELOW THE FLOOR: never report it as being in crunch, never name it in a recommendation."
    " In this show 'di-pool-1' has two people and must stay invisible to any crew alert. Crunch is"
    " a scheduling failure, not a personal one."
)

TIME_BASE_RULE = (
    "TIME BASE: history is compressed into a ~45-minute wall-clock window that ended when the"
    " seeder last ran (bridge/timewarp.py); the whole ~21-week show lives in that window. Always"
    " read current state with an INSTANT query at endTime='now' whose expression is wrapped"
    " last_over_time((<expr>)[2h:]) -- a bare instant query misses because the newest sample is"
    " already minutes old. The recipes above are already in this form; keep it when you adapt"
    " them. For a genuine trend, use a range query now-30m..now with stepSeconds 15 and look at"
    " the slope. Counters are cumulative; prefer ratios of cumulative counters over increase()."
)

NUMBER_RULE = (
    "REPORTING FIGURES: a producer reads these, not an SRE. Hours and core-hours get one decimal,"
    " counts are whole numbers, rates are a percentage to one decimal. The recipes above already"
    " return rounded values; if you write your own PromQL, wrap the expression round(<expr>, 0.1)"
    " so the rounding happens in the query rather than in your head. Never copy a raw float such"
    " as 63.16190476190476 into a finding -- fourteen decimal places is not more precise, it is"
    " less readable, and it makes a measured number look like a machine artefact."
)

THE_SHOW = (
    "THE SHOW: 'Nightfall', 200 shots across 12 sequences (SEQ0100..SEQ1200), ~21 weeks in. Two"
    " perturbations were applied to an otherwise healthy show: (1) ~T-25 days a lighting-cache"
    " regression starts failing frame 118 of every SEQ0420 comp render -- pure farm waste, in a"
    " system no producer opens; (2) ~T-18 days a director note recuts act three. Your job is to"
    " surface the first when someone asks about the second."
)

_LOGS_NOTE = (
    'LOGS (Loki, service_name="turnaround-bridge"): every task status change and every render error'
    " is a line, each carrying trace_id, span_id, production_shot_id, production_department,"
    " production_status. The cache regression shows up as lines mentioning 'frame 118'."
)

_TRACES_NOTE = (
    "TRACES (Tempo): each shot is one trace; department stages are spans; a retake is a span with"
    ' error status. Query with TraceQL { span.production.shot_id = "SEQ0420_SH0100" }, or'
    ' { span.production.department = "comp" && status = error } for rework across the show.'
)


def tool_calling_rules(prom_uid: str, loki_uid: str, tempo_uid: str) -> str:
    """Exact argument shapes for the mcp-grafana tools. The tools require a
    datasourceUid the model cannot guess, so it is spelled out here."""
    return (
        "TOOL CALLING -- the Grafana MCP tools need a datasourceUid you must pass explicitly:\n"
        f"  Prometheus/Mimir datasourceUid: {prom_uid}\n"
        f"  Loki datasourceUid:             {loki_uid}\n"
        f"  Tempo datasourceUid:            {tempo_uid}\n"
        "\n"
        "query_prometheus: required args are datasourceUid, expr, endTime. For a current value use\n"
        f'  {{"datasourceUid":"{prom_uid}","expr":"<PromQL>","queryType":"instant","endTime":"now"}}\n'
        "For a trend use\n"
        f'  {{"datasourceUid":"{prom_uid}","expr":"<PromQL>","queryType":"range",'
        '"startTime":"now-10m","endTime":"now","stepSeconds":15}\n'
        "query_loki_logs: args are datasourceUid, logql (NOT 'query'), queryType ('range'),\n"
        f'  startRfc3339/endRfc3339. Example: {{"datasourceUid":"{loki_uid}",'
        '"logql":"{service_name=\\"turnaround-bridge\\"} |= \\"frame 118\\"",'
        '"queryType":"range","startRfc3339":"now-15m","endRfc3339":"now","limit":20}\n'
        "list_prometheus_metric_names / list_prometheus_label_values: pass datasourceUid too.\n"
        f'get_annotations: {{"tags":["director-note"],"limit":20}} (no datasourceUid).\n'
        "If a call returns an error, read the error, fix the arguments, and retry once -- do not "
        "repeat the same call unchanged."
    )


def metric_catalogue_text() -> str:
    lines = [
        f"  {name:<38} [{unit:>10}]  {meaning}"
        for name, (unit, meaning) in METRIC_MEANINGS.items()
    ]
    return "The turnaround_* series (Mimir / Prometheus datasource):\n" + "\n".join(lines)


def query_recipes_text() -> str:
    blocks = [f"# {title}\n{expr}" for title, expr in QUERY_RECIPES.items()]
    return "PromQL recipes (compressed time base):\n\n" + "\n\n".join(blocks)


def shared_context(
    prom_uid: str = "grafanacloud-prom",
    loki_uid: str = "grafanacloud-logs",
    tempo_uid: str = "grafanacloud-traces",
) -> str:
    """The block prepended to every analyst's instruction."""
    return "\n\n".join([
        THE_SHOW,
        f"PIPELINE ORDER: {DEPARTMENT_ORDER}. When a stage slips, look upstream.",
        metric_catalogue_text(),
        query_recipes_text(),
        _LOGS_NOTE,
        _TRACES_NOTE,
        tool_calling_rules(prom_uid, loki_uid, tempo_uid),
        TIME_BASE_RULE,
        NUMBER_RULE,
        PRIVACY_RULE,
    ])
