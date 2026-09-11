"""The approval gate.

Analysts are read-only by construction -- they are wired to a ``mcp-grafana``
started with ``--disable-write`` (see ``agent/mcp_grafana.py``). The Remediator
is the one agent that can change anything, and *every* mutating call it makes is
intercepted here first: the supervisor sees the full evidence chain and the
exact write that is about to happen, and nothing is sent until a human says yes.

``ApprovalGate.before_tool`` has the shape of an ADK ``before_tool_callback``:
return ``None`` to let the call through, or return a dict to block it and hand
that dict back to the model as the tool result. The blocked-result text is
written for the model to read: it explains that a human declined, so the agent
reports that rather than looping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

# Mutating mcp-grafana tools + the Kitsu write-back tool. Anything in this set is
# gated; anything else the Remediator somehow calls is gated too (deny by
# default -- see ApprovalGate.before_tool).
WRITE_TOOLS = frozenset({
    "create_annotation", "update_annotation",
    "create_incident", "add_activity_to_incident",
    "kitsu_write_back",
})


@dataclass(slots=True)
class EvidenceLedger:
    """What the analysts found, in the order they found it.

    The Producer (or a test) appends a line per finding; the gate renders the
    whole chain when it asks for approval so the decision is never made blind.

    The three analysts run concurrently (``agent/producer.py``), so "the order
    they found it" is completion order and varies between runs. Which analysts
    appear does not: each writes its own ``output_key`` and the fan-out waits
    for all three, so the chain is always complete before the Remediator reads
    it.
    """

    entries: list[tuple[str, str]] = field(default_factory=list)

    def add(self, source: str, finding: str) -> None:
        self.entries.append((source, " ".join(finding.split())))

    def chain(self) -> list[str]:
        return [f"[{src}] {txt}" for src, txt in self.entries]

    def clear(self) -> None:
        self.entries.clear()


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    agent: str
    tool: str
    args: dict[str, Any]
    evidence: list[str]
    requested_at: datetime

    def render(self) -> str:
        lines = ["=" * 72, "APPROVAL REQUIRED", "=" * 72,
                 f"agent : {self.agent}", f"action: {self.tool}", "", "proposed write:"]
        for key, value in self.args.items():
            lines.append(f"  {key}: {value}")
        lines += ["", "evidence chain:"]
        if self.evidence:
            lines.extend(f"  {i}. {e}" for i, e in enumerate(self.evidence, 1))
        else:
            lines.append("  (none)")
        lines.append("=" * 72)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Decision:
    approved: bool
    by: str = "supervisor"
    note: str = ""


class Approver(Protocol):
    def decide(self, request: ApprovalRequest) -> Decision: ...


@dataclass(frozen=True, slots=True)
class AutoApprover:
    """Non-interactive: always the same answer. Tests and unattended demos."""

    approve: bool = False
    by: str = "auto"

    def decide(self, request: ApprovalRequest) -> Decision:
        del request
        return Decision(self.approve, by=self.by,
                        note="auto-approved" if self.approve else "auto-denied (no approver attached)")


@dataclass(frozen=True, slots=True)
class CliApprover:
    """Prints the request and reads y/n from a prompt function (stdin by default)."""

    prompt: Callable[[str], str] = input

    def decide(self, request: ApprovalRequest) -> Decision:
        print(request.render())
        answer = self.prompt("approve this write? [y/N] ").strip().lower()
        ok = answer in ("y", "yes")
        return Decision(ok, note=answer or "(no input)")


@dataclass(slots=True)
class ApprovalGate:
    """Holds the approver and the evidence ledger; produces the callback."""

    approver: Approver
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    #: audit trail of every decision, for the console and the write-up
    decisions: list[tuple[ApprovalRequest, Decision]] = field(default_factory=list)
    on_request: Callable[[ApprovalRequest], None] | None = None

    def review(self, agent: str, tool: str, args: dict[str, Any]) -> Decision:
        request = ApprovalRequest(
            agent=agent, tool=tool, args=dict(args or {}),
            evidence=self.ledger.chain(), requested_at=datetime.now(UTC),
        )
        if self.on_request is not None:
            self.on_request(request)
        decision = self.approver.decide(request)
        self.decisions.append((request, decision))
        return decision

    # -- ADK before_tool_callback shape ------------------------------------- #

    def before_tool(self, tool: Any, args: dict[str, Any], tool_context: Any = None) -> dict | None:
        name = getattr(tool, "name", str(tool))
        if name not in WRITE_TOOLS:
            # The Remediator's toolset is already narrowed to writes + write-back,
            # but if a read tool slips in, let it through untouched.
            return None
        agent = getattr(tool_context, "agent_name", "remediator")
        decision = self.review(agent, name, args)
        if decision.approved:
            return None
        return {
            "status": "blocked",
            "reason": (
                "A human supervisor did not approve this write. Do not retry it. "
                "Report to the user that the change is proposed but unapproved, "
                "and restate the evidence and the exact change you were about to make."
            ),
            "decision_by": decision.by,
            "decision_note": decision.note,
        }
