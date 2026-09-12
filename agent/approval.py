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

**The gate decides on an allowlist of reads, not a blocklist of writes.** A tool
it does not recognise needs a human, which is the only direction that stays
correct as ``mcp-grafana`` gains tools nobody here has read about yet. See
:data:`READ_TOOLS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# The privilege boundary: a name-based allowlist of things that cannot mutate
# --------------------------------------------------------------------------- #

#: Every ``mcp-grafana`` tool that only reads. **This, not ``WRITE_TOOLS``, is
#: what ``before_tool`` decides on**, and the direction is the whole point:
#: anything absent is gated, including a tool nobody here has heard of.
#:
#: It used to be the other way round -- gate the names on a write list, let
#: everything else through -- which meant the control was only as complete as
#: somebody's memory. A new mutating tool appearing in ``mcp-grafana`` (or a
#: rename of one already listed: ``create_annotation`` becoming
#: ``annotations_create``, say) would pass the gate untouched, and the failure
#: would be silent, on the one path in the system that changes a studio's data.
#: Inverted, the same event costs a *read* tool an unnecessary approval prompt,
#: which is a nuisance rather than an incident.
#:
#: This list is the security policy, so it lives with the gate rather than with
#: the transport wiring in ``agent/mcp_grafana.py``. ``tests/test_contracts.py``
#: asserts the two agree -- every tool actually handed to an analyst has to
#: appear here, or the gate would start prompting on ordinary reads.
READ_TOOLS = frozenset({
    # Prometheus / Mimir
    "query_prometheus", "query_prometheus_histogram",
    "list_prometheus_metric_names", "list_prometheus_label_names",
    "list_prometheus_label_values", "list_prometheus_metric_metadata",
    # Loki
    "query_loki_logs", "query_loki_stats",
    "list_loki_label_names", "list_loki_label_values",
    # Tempo
    "tempo_traceql-search", "tempo_get-trace", "tempo_traceql-metrics-range",
    # Annotations and alerting -- reads only; `create_annotation` is a write
    "get_annotations", "get_annotation_tags",
    "list_alert_groups", "get_alert_group",
})

#: The mutating tools the Remediator is wired to reach, plus the Kitsu
#: write-back. Documentation and a test anchor rather than the decision: the
#: gate does not consult it, because a set of known writes cannot protect
#: against an unknown one. Kept so ``tests/test_contracts.py`` can assert that
#: everything the Remediator *can* call is in fact gated.
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
        """Let a known read through; make a human approve everything else.

        Deny by default, which is what the docstring above the tool lists has
        always claimed and what the code did not do. The cost of the safe
        direction is one needless prompt if a read tool is ever added to a
        filter without being added to :data:`READ_TOOLS` -- and
        ``tests/test_contracts.py`` fails before that can reach anyone.
        """
        name = getattr(tool, "name", str(tool))
        if name in READ_TOOLS:
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
