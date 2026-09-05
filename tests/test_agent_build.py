"""The agent pipeline assembles offline (no credentials, no subprocess) with the
privilege boundaries intact: a fixed Sequential shape, analysts read-only and
narrowed, the Remediator gated and write-only, one shared timeline and ledger."""

from __future__ import annotations

import pytest
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from agent.approval import AutoApprover
from agent.config import Settings
from agent.mcp_grafana import (
    CRUNCH_TOOLS,
    FARM_TOOLS,
    REMEDIATOR_WRITE_TOOLS,
    SCHEDULE_TOOLS,
)
from agent.producer import build_system

RO_TOOL_UNIVERSE = {
    "query_prometheus", "query_prometheus_histogram", "list_prometheus_metric_names",
    "list_prometheus_label_names", "list_prometheus_label_values",
    "list_prometheus_metric_metadata", "query_loki_logs", "query_loki_stats",
    "query_loki_patterns", "list_loki_label_names", "list_loki_label_values",
    "analyze_loki_labels", "get_annotations", "get_annotation_tags",
    "list_alert_groups", "get_alert_group", "search_dashboards",
    "get_dashboard_by_uid", "tempo_traceql-search", "tempo_get-trace",
    "tempo_traceql-metrics-range",
}
WRITE_TOOL_NAMES = {"create_annotation", "update_annotation", "add_activity_to_incident"}


@pytest.fixture
def system():
    cfg = Settings(grafana_url="https://stack.grafana.net", grafana_token="glsa_x",
                   gcp_project="proj", gcp_location="us-central1",
                   mcp_grafana_bin="/opt/homebrew/bin/mcp-grafana")
    return build_system(approver=AutoApprover(approve=False), settings=cfg)


def _mcp_toolset(agent):
    return next(t for t in agent.tools if isinstance(t, McpToolset))


def test_pipeline_is_a_fixed_five_step_sequence(system):
    assert isinstance(system.producer, SequentialAgent)
    names = [a.name for a in system.producer.sub_agents]
    assert names == ["schedule_analyst", "farm_analyst", "crunch_guardian",
                     "remediator", "synthesis"]


def test_each_analyst_writes_a_distinct_output_key_and_feeds_the_ledger(system):
    keys = {a.name: a.output_key for a in system.producer.sub_agents}
    assert keys["schedule_analyst"] == "schedule_findings"
    assert keys["farm_analyst"] == "farm_findings"
    assert keys["crunch_guardian"] == "crunch_findings"
    assert keys["remediator"] == "remediation_result"
    for name in ("schedule_analyst", "farm_analyst", "crunch_guardian"):
        agent = next(a for a in system.producer.sub_agents if a.name == name)
        assert agent.after_agent_callback is not None


def test_analyst_tool_filters_are_read_only_subsets(system):
    by_name = {a.name: a for a in system.producer.sub_agents}
    for name, expected in [("schedule_analyst", SCHEDULE_TOOLS),
                           ("farm_analyst", FARM_TOOLS),
                           ("crunch_guardian", CRUNCH_TOOLS)]:
        tf = set(_mcp_toolset(by_name[name]).tool_filter)
        assert tf == set(expected)
        assert tf <= RO_TOOL_UNIVERSE, f"{name} can see a non-read tool"
        assert not tf & WRITE_TOOL_NAMES


def test_remediator_is_write_only_and_gated(system):
    rem = next(a for a in system.producer.sub_agents if a.name == "remediator")
    assert rem.before_tool_callback is not None
    tf = set(_mcp_toolset(rem).tool_filter)
    assert tf == set(REMEDIATOR_WRITE_TOOLS) == WRITE_TOOL_NAMES
    fn_names = {getattr(t, "name", "") for t in rem.tools}
    assert "kitsu_write_back" in fn_names


def test_synthesis_has_no_tools(system):
    syn = next(a for a in system.producer.sub_agents if a.name == "synthesis")
    assert isinstance(syn, LlmAgent)
    assert not syn.tools


def test_default_approver_denies_and_shares_one_ledger(system):
    assert system.gate.approver.approve is False
    assert system.ledger is system.gate.ledger
