"""The tool timeline is the Phase 4 gate's evidence that answers come from live
MCP calls, so it must record them faithfully and mark which hit Grafana."""

from __future__ import annotations

import pytest

from agent.timeline import TimelineRecorder, ToolTimeline


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


# --------------------------------------------------------------------------- #
# TimelineRecorder: the before/after pairing both agent tiers now share
# --------------------------------------------------------------------------- #


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    """Stands in for ADK's ToolContext: the same instance reaches before and after."""

    def __init__(self, agent_name="farm_analyst"):
        self.agent_name = agent_name


def test_recorder_pairs_a_before_with_its_after():
    tl = ToolTimeline()
    rec = TimelineRecorder(tl)
    ctx = _Ctx()
    rec.begin(_Tool("query_prometheus"), {"expr": "up"}, ctx)
    rec.finish(ctx, {"content": "1"})

    (call,) = tl.calls
    assert call.agent == "farm_analyst"
    assert call.tool == "query_prometheus"
    assert call.args == {"expr": "up"}
    assert call.ok is True


def test_two_calls_in_flight_do_not_cross_wires():
    """ADK runs a turn's tool calls sequentially, but each carries its own
    context; keying on that identity keeps the rows independent regardless."""
    tl = ToolTimeline()
    rec = TimelineRecorder(tl)
    first, second = _Ctx(), _Ctx()
    rec.begin(_Tool("query_prometheus"), {"expr": "a"}, first)
    rec.begin(_Tool("query_loki_logs"), {"logql": "b"}, second)
    rec.finish(second, {"content": "ok"})
    rec.finish(first, {"isError": True})

    by_tool = {c.tool: c for c in tl.calls}
    assert by_tool["query_loki_logs"].ok is True
    assert by_tool["query_prometheus"].ok is False


@pytest.mark.parametrize(("response", "ok"), [
    ({"content": "42"}, True),
    ("a plain string result", True),
    (None, True),
    ({"isError": True, "content": "parse error"}, False),      # mcp-grafana error
    ({"status": "blocked", "reason": "declined"}, False),      # approval gate
    ({"isError": False}, True),
])
def test_one_predicate_decides_ok_for_both_failure_shapes(response, ok):
    """The analysts see ``isError``; the Remediator sees ``status: blocked``.
    They used to be judged by two copies of the check that disagreed."""
    tl = ToolTimeline()
    rec = TimelineRecorder(tl)
    ctx = _Ctx()
    rec.begin(_Tool("create_annotation"), {}, ctx)
    rec.finish(ctx, response)
    assert tl.calls[0].ok is ok


def test_a_second_finish_for_the_same_call_is_a_no_op():
    """The Remediator closes a blocked write in its *before* hook; ADK then
    fires the after hook for the same call. That must not add a second row."""
    tl = ToolTimeline()
    rec = TimelineRecorder(tl)
    ctx = _Ctx("remediator")
    rec.begin(_Tool("create_annotation"), {"text": "fix the cache"}, ctx)
    rec.finish(ctx, {"status": "blocked", "reason": "declined"})
    rec.finish(ctx, {"status": "blocked", "reason": "declined"})

    assert len(tl.calls) == 1
    assert tl.calls[0].ok is False


def test_an_after_with_no_before_records_nothing():
    """The timeline is evidence. A row with no arguments and no start is worse
    than no row, so an unpaired after is dropped rather than invented."""
    tl = ToolTimeline()
    TimelineRecorder(tl).finish(_Ctx(), {"content": "orphan"})
    assert tl.calls == []


def test_the_default_agent_names_the_tier_when_adk_gives_no_name():
    tl = ToolTimeline()
    rec = TimelineRecorder(tl, default_agent="remediator")
    ctx = object()  # no agent_name attribute
    rec.begin(_Tool("create_annotation"), {}, ctx)
    assert tl.calls[0].agent == "remediator"


def test_an_unserialisable_argument_still_digests():
    """Tool args come from a model; they are not guaranteed to be JSON."""
    tl = ToolTimeline()
    rec = TimelineRecorder(tl)
    ctx = _Ctx()
    rec.begin(_Tool("query_prometheus"), {"expr": object()}, ctx)
    rec.finish(ctx, {"blob": {1, 2, 3}})   # a set is not JSON-serialisable
    row = tl.calls[0].to_dict()
    assert isinstance(row["args"]["expr"], str)
    assert isinstance(row["result"], str)
