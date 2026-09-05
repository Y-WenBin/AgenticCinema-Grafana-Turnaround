"""The approval gate: mutating tool calls are blocked until a human approves,
and the block is shaped so the model reports it instead of retrying."""

from __future__ import annotations

from dataclasses import dataclass

from agent.approval import (
    ApprovalGate,
    ApprovalRequest,
    AutoApprover,
    CliApprover,
    Decision,
    EvidenceLedger,
)


@dataclass
class _Tool:
    name: str


class _Ctx:
    agent_name = "remediator"


def test_read_tool_passes_through_untouched():
    gate = ApprovalGate(approver=AutoApprover(approve=False))
    assert gate.before_tool(_Tool("query_prometheus"), {"expr": "up"}, _Ctx()) is None
    assert gate.decisions == []


def test_write_tool_is_blocked_when_denied():
    gate = ApprovalGate(approver=AutoApprover(approve=False))
    out = gate.before_tool(_Tool("create_annotation"), {"text": "fix"}, _Ctx())
    assert out is not None
    assert out["status"] == "blocked"
    assert "did not approve" in out["reason"]
    assert "retry" in out["reason"].lower()
    assert len(gate.decisions) == 1 and gate.decisions[0][1].approved is False


def test_write_tool_passes_when_approved():
    gate = ApprovalGate(approver=AutoApprover(approve=True))
    assert gate.before_tool(_Tool("create_annotation"), {"text": "fix"}, _Ctx()) is None
    assert gate.decisions[0][1].approved is True


def test_kitsu_write_back_is_gated():
    gate = ApprovalGate(approver=AutoApprover(approve=False))
    out = gate.before_tool(_Tool("kitsu_write_back"), {"action": "note"}, _Ctx())
    assert out and out["status"] == "blocked"


def test_evidence_chain_reaches_the_approver():
    seen = {}

    class Spy:
        def decide(self, request: ApprovalRequest) -> Decision:
            seen["evidence"] = request.evidence
            seen["rendered"] = request.render()
            return Decision(False)

    gate = ApprovalGate(approver=Spy())
    gate.ledger.add("farm_analyst", "SEQ0420 wasted 44 core-hours vs 2-3 elsewhere")
    gate.ledger.add("schedule_analyst", "cost ~6 artist-days of rework")
    gate.before_tool(_Tool("create_annotation"), {"text": "invalidate cache key nightfall/lighting/SEQ0420"}, _Ctx())

    assert seen["evidence"] == [
        "[farm_analyst] SEQ0420 wasted 44 core-hours vs 2-3 elsewhere",
        "[schedule_analyst] cost ~6 artist-days of rework",
    ]
    assert "invalidate cache key" in seen["rendered"]
    assert "evidence chain:" in seen["rendered"]


def test_cli_approver_reads_yes_no():
    yes = CliApprover(prompt=lambda _p: "y")
    no = CliApprover(prompt=lambda _p: "")
    req = ApprovalRequest("remediator", "create_annotation", {"text": "x"}, [], __import__("datetime").datetime.now())
    assert yes.decide(req).approved is True
    assert no.decide(req).approved is False


def test_ledger_clear():
    led = EvidenceLedger()
    led.add("a", "b")
    led.clear()
    assert led.chain() == []
