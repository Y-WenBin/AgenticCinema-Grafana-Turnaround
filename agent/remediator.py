"""The Remediator: the one agent that can change anything, fully gated.

It proposes a concrete, evidence-backed correction and -- only after a human
supervisor approves -- writes it into Grafana (an annotation on the dashboards)
and proposes it back into Kitsu (``kitsu_write_back``). Both are mutating tools;
both pass ``ApprovalGate.before_tool`` first, which blocks the call and returns a
"a human declined" result to the model if approval is withheld.

Its toolset is a ``mcp-grafana --enabled-tools annotations,incident`` -- it
physically cannot edit a dashboard, datasource or alert rule.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from agent.approval import ApprovalGate
from agent.config import Settings
from agent.mcp_grafana import remediator_toolset
from agent.timeline import TimelineRecorder, ToolTimeline
from agent.vocabulary import shared_context
from agent.writeback import KitsuWriteBack, default_writeback, make_write_back_tool

#: session-state key the Remediator writes its outcome to
REMEDIATION_KEY = "remediation_result"

REMEDIATOR_ROLE = (
    "You are the Remediator. The specialists have already run; their findings:\n\n"
    "SCHEDULE:\n{schedule_findings}\n\n"
    "FARM:\n{farm_findings}\n\n"
    "CREW:\n{crunch_findings}\n\n"
    "---\n"
    "FIRST decide: did the supervisor's message ask you to change / fix / do "
    "something, or 'what do I change'? If NOT -- they only asked to understand "
    "the situation -- reply with the single line 'none requested' and call no "
    "tools. Stop there.\n\n"
    "If they DID ask for a change:\n"
    "PRINCIPLES\n"
    "- Fix the upstream cause, never the symptom. 'Add more artists' and 'more "
    "overtime' are NOT acceptable; the broken cache and the schedule are.\n"
    "- Be specific enough to act on today: which cache key, which sequence's "
    "dates, which department. Prefer a change still makeable while the schedule "
    "is renegotiable.\n\n"
    "PROCEDURE\n"
    "1. State the remediation in 2-3 sentences: the cause, the change, the "
    "expected effect on BOTH the delivery date and the crew hours.\n"
    "2. Call create_annotation (datasourceUid not needed; leave dashboardUID "
    "unset for org-wide) with tags ['turnaround','remediation'], the remediation "
    "text, time 'now'.\n"
    "3. Call kitsu_write_back with action='note', shot_id='SEQ0420_SH0100', "
    "department='comp', and the recommendation as text.\n"
    "4. Both calls are gated. If one comes back status='blocked', STOP -- do not "
    "retry, do not try another tool. Say the remediation is proposed but "
    "UNAPPROVED and restate it in full.\n\n"
    "End with one of: 'written: <what landed>' / 'proposed, unapproved: "
    "<remediation>' / 'none requested'. Never claim a write that was blocked."
)


def remediator(
    cfg: Settings,
    timeline: ToolTimeline,
    gate: ApprovalGate,
    *,
    writeback: KitsuWriteBack | None = None,
) -> LlmAgent:
    wb_tool = FunctionTool(make_write_back_tool(writeback or default_writeback()))
    recorder = TimelineRecorder(timeline, default_agent="remediator")

    def before_tool(tool, args, tool_context):
        """Timeline first (so a blocked call still shows), then the approval gate."""
        recorder.begin(tool, args, tool_context)
        blocked = gate.before_tool(tool, args, tool_context)
        if blocked is not None:
            # Close the row here; the recorder then treats ADK's after-hook for
            # this same blocked call as a no-op rather than a duplicate row.
            recorder.finish(tool_context, blocked)
        return blocked

    def after_tool(tool, args, tool_context, tool_response):
        del tool, args
        recorder.finish(tool_context, tool_response)

    return LlmAgent(
        name="remediator",
        model=cfg.analyst_model,
        description=("Proposes one upstream correction and, once a supervisor approves, "
                     "writes it to Grafana and back to Kitsu. All writes are gated."),
        instruction=(
            f"{shared_context(cfg.ds_prom, cfg.ds_loki, cfg.ds_tempo)}"
            f"\n\n---\n\nYOUR ROLE\n{REMEDIATOR_ROLE}"
        ),
        tools=[remediator_toolset(cfg), wb_tool],
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        output_key=REMEDIATION_KEY,
    )
