"""The tool timeline is the Phase 4 gate's evidence that answers come from live
MCP calls, so it must record them faithfully and mark which hit Grafana."""

from __future__ import annotations

from agent.timeline import ToolTimeline


def test_records_calls_in_order_with_timing():
    tl = ToolTimeline()
    c1 = tl.begin(agent="farm_analyst", tool="query_prometheus", args={"expr": "sum(x)"})
    tl.finish(c1, result={"data": [1, 2, 3]}, ok=True)
    c2 = tl.begin(agent="farm_analyst", tool="query_loki_logs", args={"query": '{app="x"} |= "frame 118"'})
    tl.finish(c2, result="matched 5 lines", ok=True)

    assert [c.seq for c in tl.calls] == [1, 2]
    assert all(c.duration_ms is not None and c.duration_ms >= 0 for c in tl.calls)
    assert all(c.ok for c in tl.calls)


def test_marks_grafana_mcp_calls():
    tl = ToolTimeline()
    tl.finish(tl.begin(agent="a", tool="query_prometheus", args={}), result="ok")
    tl.finish(tl.begin(agent="producer", tool="record_evidence", args={}), result="ok")
    tl.finish(tl.begin(agent="remediator", tool="create_annotation", args={}), result="ok")

    grafana = {c.tool for c in tl.grafana_calls()}
    assert grafana == {"query_prometheus", "create_annotation"}
    assert len(tl.grafana_calls()) == 2


def test_args_and_results_are_digested_not_dumped():
    tl = ToolTimeline()
    big = {"expr": "x" * 5000, "nested": {"y": list(range(200))}}
    c = tl.begin(agent="a", tool="query_prometheus", args=big)
    tl.finish(c, result="z" * 9000, ok=True)
    d = c.to_dict()
    assert len(d["args"]["expr"]) <= 160
    assert len(d["result"]) <= 161
    assert d["grafana_mcp"] is True


def test_render_summarises_and_counts_grafana_calls():
    tl = ToolTimeline()
    tl.finish(tl.begin(agent="farm_analyst", tool="query_prometheus", args={"expr": "up"}), result="ok")
    tl.finish(tl.begin(agent="producer", tool="record_evidence", args={"source": "x"}), result="ok")
    out = tl.render()
    assert "query_prometheus" in out
    assert "1 against Grafana Cloud MCP" in out


def test_empty_timeline_renders_safely():
    assert ToolTimeline().render() == "(no tool calls)"
