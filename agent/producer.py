"""The pipeline the supervisor talks to.

Flash is an unreliable orchestrator -- asked to "consult the right specialists
then synthesise", it routinely stops after one and echoes it. So the shape is
deterministic instead of model-decided:

    SequentialAgent(producer):
      1. ParallelAgent(analysts)                             (concurrent)
           schedule_analyst -> state["schedule_findings"]
           farm_analyst     -> state["farm_findings"]
           crunch_guardian  -> state["crunch_findings"]
      2. remediator         -> state["remediation_result"]   (writes are gated)
      3. synthesis          -> the final Answer / Evidence / Remediation block

Every question runs the whole board. It costs a few extra flash calls per run
and buys a pipeline that answers the same way every run. A coordinator that
decides which specialists to consult is a coordinator that answers the same
question two different ways on two different days.

The three analysts are *concurrent* because they are genuinely independent: each
reads a different plane of the stack, writes its own ``output_key``, and reads
none of the others. Run one after another they were the bulk of the wall clock
-- measured against the live stack, the analyst phase alone was ~42s of a ~90s
run, almost all of it waiting on Gemini rather than on Grafana. Fanned out, the
phase costs whatever the slowest analyst costs. The remediator still runs after
them, because it reasons about what they found, and synthesis still runs last.

Order is the only thing the fan-out gives up. The tool timeline now interleaves
the three analysts' calls instead of grouping them, which is what actually
happened; ``ToolTimeline`` already tolerated it, because ADK issues one agent's
tool calls concurrently too (a single analyst turn routinely fires five queries
at once). The evidence ledger the approval gate shows
is filled by each analyst's ``after_agent_callback``, not by a coordinator
remembering to call a tool.

``build_system`` returns the root agent plus the shared timeline, gate and
ledger for the CLI, the Phase 5 console and the tests.

``SYNTHESIS_ROLE`` interpolates the four ``output_key`` names the sub-agents
write to; ``tests/test_agent_build.py`` asserts they still match.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

from agent.analysts import crunch_guardian, farm_analyst, schedule_analyst
from agent.approval import ApprovalGate, Approver, AutoApprover, EvidenceLedger
from agent.config import Settings, bootstrap_vertex
from agent.remediator import remediator
from agent.thinking import planner
from agent.timeline import ToolTimeline
from agent.writeback import KitsuWriteBack

SYNTHESIS_ROLE = (
    "You are the Producer, writing the final review for a VFX post supervisor. "
    "Three specialists and a remediator have already run; their outputs are "
    "below. Do NOT call any tools. Compose the answer from what they found -- "
    "never invent a number, never name an individual artist or a pool of fewer "
    "than three people.\n\n"
    "SCHEDULE ANALYST:\n{schedule_findings}\n\n"
    "FARM ANALYST:\n{farm_findings}\n\n"
    "CRUNCH GUARDIAN:\n{crunch_findings}\n\n"
    "REMEDIATOR:\n{remediation_result}\n\n"
    "---\n"
    "GROUNDING RULES (the answer is scored against these):\n"
    "- Every figure you state must appear, in words or digits, in one of the "
    "  four specialist blocks above. If it is not there, do not write it -- not "
    "  a date, not a percentage, not a core-hour count, not a headcount.\n"
    "- Do not derive a new number the specialists did not already compute. The "
    "  artist-day cost is the ScheduleAnalyst's COST line; quote it, and if you "
    "  show the arithmetic show only the terms it gave you. If a figure the "
    "  question asks for was not measured this run, say 'not measured this run' "
    "  rather than estimate it.\n"
    "- Attribute the delivery-slip framing to the ScheduleAnalyst's NOTE line "
    "  (it is a forecast, not a measured date) and say so.\n\n"
    "Write exactly:\n"
    "Answer: <3-4 sentences in plain language for a producer, not an SRE. Say "
    "whether the sequence is slipping, why (name the farm mechanism), what it "
    "costs, and who is in crunch. If the supervisor asked what to change, lead "
    "with the remediation.>\n"
    "Evidence:\n"
    "  - schedule: <one line with the key number, copied from the block above>\n"
    "  - farm: <one line with the key number, copied from the block above>\n"
    "  - crew: <one line with the key number, copied from the block above>\n"
    "Remediation: <what the remediator proposed and whether the write was "
    "approved, blocked, or not attempted; 'none requested' if the supervisor "
    "did not ask for a change>"
)


@dataclass(slots=True)
class AgentSystem:
    producer: SequentialAgent
    timeline: ToolTimeline
    gate: ApprovalGate
    ledger: EvidenceLedger
    settings: Settings

    @property
    def root_agent(self) -> SequentialAgent:
        return self.producer


def build_system(
    *,
    approver: Approver | None = None,
    settings: Settings | None = None,
    writeback: KitsuWriteBack | None = None,
) -> AgentSystem:
    cfg = settings or bootstrap_vertex()
    timeline = ToolTimeline()
    gate = ApprovalGate(approver=approver or AutoApprover(approve=False))
    ledger = gate.ledger

    synthesis = LlmAgent(
        name="synthesis",
        model=cfg.analyst_model,
        description="Writes the final Answer / Evidence / Remediation block from the specialists' outputs.",
        instruction=SYNTHESIS_ROLE,
        planner=planner(cfg.thinking_budget),
    )

    analysts = ParallelAgent(
        name="analysts",
        description="Reads the creative, compute and crew planes at the same time.",
        sub_agents=[
            schedule_analyst(cfg, timeline, ledger),
            farm_analyst(cfg, timeline, ledger),
            crunch_guardian(cfg, timeline, ledger),
        ],
    )

    producer = SequentialAgent(
        name="producer",
        description="Runs the schedule, farm and crunch analysts together, then the gated remediator, then synthesises.",
        sub_agents=[
            analysts,
            remediator(cfg, timeline, gate, writeback=writeback),
            synthesis,
        ],
    )
    return AgentSystem(producer=producer, timeline=timeline, gate=gate,
                       ledger=ledger, settings=cfg)


# ADK's `adk web` / `adk run` looks for a module-level `root_agent`. Build lazily
# so importing this module (e.g. in tests that never run it) costs nothing and
# needs no credentials.
def __getattr__(name: str):
    if name == "root_agent":
        return build_system().producer
    raise AttributeError(name)
