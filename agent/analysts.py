"""The three read-only analyst agents.

Each is an ``LlmAgent`` on Gemini flash with a narrow slice of the read-only
``mcp-grafana`` toolset and the shared production vocabulary. They are wired to
the tool timeline so every Grafana MCP call they make is recorded.

They are read-only *by construction* -- the toolset is a ``mcp-grafana
--disable-write`` (``agent/mcp_grafana.py``), so a prompt injection telling an
analyst to change something has nothing to call.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from agent.approval import EvidenceLedger
from agent.config import Settings
from agent.mcp_grafana import CRUNCH_TOOLS, FARM_TOOLS, SCHEDULE_TOOLS, analyst_toolset
from agent.timeline import TimelineRecorder, ToolTimeline
from agent.vocabulary import FLOOR, shared_context

# --------------------------------------------------------------------------- #
# Tool-timeline wiring
# --------------------------------------------------------------------------- #


def timeline_callbacks(timeline: ToolTimeline):
    """A (before, after) ADK callback pair that logs each tool call to ``timeline``.

    Both return ``None``: the before lets the call proceed, the after keeps the
    tool response as-is. The pairing rules live in :class:`TimelineRecorder`.
    """
    recorder = TimelineRecorder(timeline)

    def before(tool, args, tool_context):
        recorder.begin(tool, args, tool_context)

    def after(tool, args, tool_context, tool_response):
        del tool, args
        recorder.finish(tool_context, tool_response)

    return before, after


def _ledger_writer(ledger: EvidenceLedger, source: str, output_key: str):
    """after_agent_callback: copy this analyst's final text into the evidence
    ledger the approval gate shows, so the chain is built deterministically
    rather than depending on the coordinator remembering to record it."""

    def record(callback_context) -> None:
        state = getattr(callback_context, "state", {}) or {}
        finding = state.get(output_key)
        if finding:
            ledger.add(source, str(finding))

    return record


def _analyst(*, name: str, description: str, role: str, tool_filter, cfg: Settings,
             timeline: ToolTimeline, ledger: EvidenceLedger, output_key: str) -> LlmAgent:
    before, after = timeline_callbacks(timeline)
    context = shared_context(cfg.ds_prom, cfg.ds_loki, cfg.ds_tempo)
    return LlmAgent(
        name=name,
        model=cfg.analyst_model,
        description=description,
        instruction=f"{context}\n\n---\n\nYOUR ROLE\n{role}",
        tools=[analyst_toolset(cfg, tool_filter)],
        before_tool_callback=before,
        after_tool_callback=after,
        output_key=output_key,
        after_agent_callback=_ledger_writer(ledger, name, output_key),
    )


# --------------------------------------------------------------------------- #
# The analysts
# --------------------------------------------------------------------------- #

SCHEDULE_ROLE = (
    "You are the ScheduleAnalyst: creative plane only. Be terse -- 6 lines, no preamble.\n\n"
    "Run these instant queries at now and report each number:\n"
    "  A. THE JOIN recipe (render core-hours per comp iteration by sequence). "
    "Expect SEQ0420 ~4.5 vs ~2.2 for the rest.\n"
    "  B. get_annotations tags=['director-note'] -- what changed and roughly when.\n"
    "  C. last_over_time((sum by (sequence) "
    '(turnaround_task_iterations_total{department="comp"}))[2h:]) -- comp passes '
    "on SEQ0420 so far.\n"
    "  D. Cost in artist-days/week of comp overtime. Run BOTH the "
    "'Artist-days/week of comp overtime cost, by pool' recipe (per-pool split) "
    "and the 'whole comp dept (one number)' recipe (the total) exactly as "
    "written -- do not compose your own PromQL for this. Expect a total near 56 "
    "(comp-pool-1 ~25, comp-pool-2 ~31). If either comes back empty, retry once "
    "with [6h:]; report the numbers the queries return, never 0 unless the "
    "queries genuinely return 0.\n\n"
    "Answer shape:\n"
    "  JOIN: SEQ0420 <n> core-h/iter vs ~<n> others -> farm, not the artist, is the driver\n"
    "  NOTE: <what/when>; rework so far is small (<n> comp passes) so the slip is forecast\n"
    "  COST: comp-pool-1 <n>, comp-pool-2 <n> -> <total> artist-days/week of overtime\n"
    "No number without a query behind it."
)

FARM_ROLE = (
    "You are the FarmAnalyst: compute plane only. Be terse -- 7 lines, no preamble.\n\n"
    "Run these instant queries at now and report each number:\n"
    "  A. last_over_time((sum by (sequence) "
    "(turnaround_render_waste_core_hours_total))[2h:]) -- wasted farm core-hours "
    "by sequence. Expect SEQ0420 far above the rest.\n"
    "  B. Frame-failure rate recipe by sequence. Expect SEQ0420 near 9-10%.\n"
    "  C. query_loki_logs: {service_name=\"turnaround-bridge\"} |= \"SEQ0420\" |= "
    '"frame 118" -- quote one cache-miss line verbatim.\n'
    "  D. tempo_traceql-search with query "
    '\'{ span.production.shot_id="SEQ0420_SH0100" }\' (pass the Tempo '
    "datasourceUid). Each shot is one trace, department stages are spans, a "
    "retake is a span with error status. Report how many matching traces / error "
    "spans you see -- the farm waste shows up as recorded trace errors on the "
    "comp stage, not just as metrics.\n\n"
    "Answer shape:\n"
    "  WASTE: SEQ0420 <n> core-h vs <n> elsewhere (~<n>x concentration)\n"
    "  FRAMES: SEQ0420 failing <n>% -- frame 118 every comp render\n"
    "  CAUSE: <verbatim log line> -> lighting-cache regression, predates the note\n"
    "  TRACE: <n> trace(s)/error span(s) for SEQ0420_SH0100 comp -- rework recorded in Tempo\n"
    "No number without a query behind it."
)

CRUNCH_ROLE = (
    "You are the CrunchGuardian: crew load only.\n\n"
    "STEP 1 (mandatory). Instant query at now:\n"
    "  last_over_time(((max by (pool) (turnaround_artist_hours_logged)) and "
    f"on(pool) (turnaround_pool_headcount >= {FLOOR}))[2h:])\n"
    "This returns current weekly hours per rostered artist for every pool that "
    "meets the floor. Write down EVERY pool and its number from the result. If "
    "it is empty, retry once with [6h:].\n\n"
    "STEP 2. Instant query at now for the trend direction: run the same "
    "expression as a range query, now-30m..now, stepSeconds 15. Is each hot "
    "pool's line rising, flat, or falling?\n\n"
    "Then answer in exactly this shape:\n"
    "  IN CRUNCH NOW (>60h): <pool> at <N>h[, ...]\n"
    "  APPROACHING (40-60h): <pool> at <N>h[, ...]\n"
    "  TREND: <rising/flat/falling>, measured not forecast\n"
    "  ALERT: the Turnaround crew-crunch rule trips at 60h per rostered artist "
    "with the same floor join; <list the pools above 60h> breach it now.\n"
    "  LEVER: fix the SEQ0420 lighting-cache regression forcing comp rework -- "
    "not more hours, not more hires.\n\n"
    "Every number must come from a query in this turn. NEVER name or hint at a "
    "pool below the floor; di-pool-1 (two people) must not appear. Do not "
    "replace the hours numbers with the cache story -- the cache is the LEVER "
    "line only."
)


#: the session-state key each analyst writes its final answer to
SCHEDULE_KEY = "schedule_findings"
FARM_KEY = "farm_findings"
CRUNCH_KEY = "crunch_findings"


def schedule_analyst(cfg: Settings, timeline: ToolTimeline, ledger: EvidenceLedger) -> LlmAgent:
    return _analyst(
        name="schedule_analyst",
        description="Creative plane: burndown, iterations, artist-days cost, the director note, THE JOIN.",
        role=SCHEDULE_ROLE, tool_filter=SCHEDULE_TOOLS, cfg=cfg, timeline=timeline,
        ledger=ledger, output_key=SCHEDULE_KEY,
    )


def farm_analyst(cfg: Settings, timeline: ToolTimeline, ledger: EvidenceLedger) -> LlmAgent:
    return _analyst(
        name="farm_analyst",
        description="Compute plane: render core-hours, frame failures, render waste, the cache regression, traces.",
        role=FARM_ROLE, tool_filter=FARM_TOOLS, cfg=cfg, timeline=timeline,
        ledger=ledger, output_key=FARM_KEY,
    )


def crunch_guardian(cfg: Settings, timeline: ToolTimeline, ledger: EvidenceLedger) -> LlmAgent:
    return _analyst(
        name="crunch_guardian",
        description="Crew load and crunch timing, always respecting the aggregation floor. Never names small pools.",
        role=CRUNCH_ROLE, tool_filter=CRUNCH_TOOLS, cfg=cfg, timeline=timeline,
        ledger=ledger, output_key=CRUNCH_KEY,
    )
