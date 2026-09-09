"""The agent-tier GenAI instrumentation: spans, metrics, evaluation events,
and the opt-in content-capture privacy guard.

Driven through in-memory exporters (``observability/testing.py``) and fake ADK
objects, so no Vertex, no network, no mcp-grafana.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bridge.privacy import PrivacyViolation, assert_no_pii
from observability.adk import GenAiObservabilityPlugin
from observability.genai import ChatOutcome
from observability.testing import make_harness

# --------------------------------------------------------------------------- #
# GenAiTelemetry directly
# --------------------------------------------------------------------------- #


def test_invoke_agent_span_carries_semantic_attributes():
    h = make_harness()
    with h.telemetry.invoke_agent("producer", conversation_id="cli-1"):
        pass
    (span,) = h.spans_by_name("invoke_agent producer")
    assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert span.attributes["gen_ai.agent.name"] == "producer"
    assert span.attributes["gen_ai.conversation.id"] == "cli-1"


def test_chat_span_and_token_histograms():
    h = make_harness()
    span, started = h.telemetry.start_chat(request_model="gemini-2.5-flash",
                                           agent_name="schedule_analyst")
    h.telemetry.end_chat(
        span, started,
        ChatOutcome(response_model="gemini-2.5-flash-002", response_id="resp-1",
                    finish_reasons=["STOP"], input_tokens=2450, output_tokens=412),
        request_model="gemini-2.5-flash", agent_name="schedule_analyst",
    )
    (chat,) = h.spans_by_name("chat gemini-2.5-flash")
    assert chat.attributes["gen_ai.provider.name"] == "gcp.vertex_ai"
    assert chat.attributes["gen_ai.request.model"] == "gemini-2.5-flash"
    assert chat.attributes["gen_ai.response.id"] == "resp-1"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 2450
    assert chat.attributes["gen_ai.usage.output_tokens"] == 412

    points = h.metric_points("gen_ai.client.token.usage")
    by_type = {p.attributes["gen_ai.token.type"]: p.sum for p in points}
    assert by_type == {"input": 2450, "output": 412}
    assert h.metric_points("gen_ai.client.operation.duration")


def test_execute_tool_span_records_query_for_the_pivot():
    h = make_harness()
    with h.telemetry.execute_tool(tool_name="query_prometheus", call_id="fc-1",
                                  query="sum(rate(x[5m]))", query_lang="promql"):
        pass
    (tool,) = h.spans_by_name("execute_tool query_prometheus")
    assert tool.attributes["gen_ai.tool.name"] == "query_prometheus"
    assert tool.attributes["gen_ai.tool.call_id"] == "fc-1"
    assert tool.attributes["turnaround.query"] == "sum(rate(x[5m]))"
    assert tool.attributes["turnaround.query.lang"] == "promql"


def test_evaluation_event_shape_and_correlation():
    h = make_harness()
    h.telemetry.emit_evaluation(
        name="grounding_numbers", score_value=1.0, score_label="pass",
        explanation="every figure traces to a query", response_id="resp-1",
        actor_type="deterministic",
    )
    (ev,) = h.evaluation_events()
    assert ev["gen_ai.evaluation.name"] == "grounding_numbers"
    assert ev["gen_ai.evaluation.score.value"] == 1.0
    assert ev["gen_ai.evaluation.score.label"] == "pass"
    assert ev["gen_ai.evaluation.actor.type"] == "deterministic"
    assert ev["gen_ai.response.id"] == "resp-1"


# --------------------------------------------------------------------------- #
# Content capture + privacy guard
# --------------------------------------------------------------------------- #


def test_content_capture_is_off_by_default():
    h = make_harness()  # capture_content defaults to False
    span, started = h.telemetry.start_chat(request_model="gemini-2.5-flash",
                                           agent_name="a",
                                           system_instructions="you are a producer",
                                           input_messages=[{"text": "hi"}])
    h.telemetry.end_chat(span, started, ChatOutcome(output_messages=[{"text": "yo"}]),
                         request_model="gemini-2.5-flash", agent_name="a")
    (chat,) = h.spans_by_name("chat gemini-2.5-flash")
    assert "gen_ai.input.messages" not in chat.attributes
    assert "gen_ai.output.messages" not in chat.attributes
    assert "gen_ai.system_instructions" not in chat.attributes


def test_content_capture_on_attaches_messages():
    h = make_harness(capture_content=True, content_guard=assert_no_pii)
    span, started = h.telemetry.start_chat(request_model="gemini-2.5-flash",
                                           agent_name="a",
                                           system_instructions="you are a producer",
                                           input_messages=[{"text": "why is SEQ0420 slipping"}])
    h.telemetry.end_chat(span, started, ChatOutcome(output_messages=[{"text": "farm waste"}]),
                         request_model="gemini-2.5-flash", agent_name="a")
    (chat,) = h.spans_by_name("chat gemini-2.5-flash")
    assert "SEQ0420" in chat.attributes["gen_ai.input.messages"]
    assert chat.attributes["gen_ai.output.messages"]


def test_content_capture_guard_rejects_pii_in_a_prompt():
    h = make_harness(capture_content=True, content_guard=assert_no_pii)
    with pytest.raises(PrivacyViolation):
        h.telemetry.start_chat(
            request_model="gemini-2.5-flash", agent_name="a",
            input_messages=[{"email": "artist@studio.com"}],
        )


def test_evaluation_event_guard_rejects_pii():
    h = make_harness(content_guard=assert_no_pii)
    with pytest.raises(PrivacyViolation):
        h.telemetry.emit_evaluation(
            name="x", score_value=0.0, score_label="fail",
            explanation="contact artist@studio.com", response_id="r",
            extra={"email": "artist@studio.com"},
        )


# --------------------------------------------------------------------------- #
# The ADK plugin, with fake ADK objects
# --------------------------------------------------------------------------- #


def _run(coro):
    return asyncio.run(coro)


def test_plugin_builds_a_nested_trace_from_adk_callbacks():
    """invoke_agent is opened by the caller (agent/engine.py); the plugin's chat
    and execute_tool spans nest under it. before/after get different context
    objects -- the plugin pairs them on a stack, not by identity."""
    h = make_harness()
    p = GenAiObservabilityPlugin(h.telemetry)

    tool = SimpleNamespace(name="query_prometheus")
    usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=20)
    llm_response = SimpleNamespace(model_version="gemini-2.5-flash-002",
                                  interaction_id=None, finish_reason="STOP",
                                  usage_metadata=usage, content=None, error_message=None)

    async def scenario():
        with h.telemetry.invoke_agent("producer", conversation_id="cli"):
            # different callback_context objects for before vs after -- the real
            # ADK behaviour that broke id()-keying
            await p.before_model_callback(
                callback_context=SimpleNamespace(agent_name="schedule_analyst"),
                llm_request=SimpleNamespace(model="gemini-2.5-flash", config=None, contents=None))
            await p.after_model_callback(
                callback_context=SimpleNamespace(agent_name="schedule_analyst"),
                llm_response=llm_response)
            await p.before_tool_callback(tool=tool, tool_args={"expr": "up"},
                                        tool_context=SimpleNamespace(function_call_id="fc-9"))
            await p.after_tool_callback(tool=tool, tool_args={"expr": "up"},
                                       tool_context=SimpleNamespace(function_call_id="fc-9"),
                                       result={"ok": True})

    _run(scenario())

    names = h.span_names()
    assert "invoke_agent producer" in names
    assert "chat gemini-2.5-flash" in names
    assert "execute_tool query_prometheus" in names

    invoke = h.spans_by_name("invoke_agent producer")[0]
    chat = h.spans_by_name("chat gemini-2.5-flash")[0]
    tool_span = h.spans_by_name("execute_tool query_prometheus")[0]
    assert chat.parent is not None and chat.parent.span_id == invoke.context.span_id
    assert tool_span.parent is not None and tool_span.parent.span_id == invoke.context.span_id
    assert chat.context.trace_id == invoke.context.trace_id
    assert tool_span.attributes["turnaround.query"] == "up"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 100
    # no interaction_id from the model -> the plugin's synthetic run id is used
    assert chat.attributes["gen_ai.response.id"] == p.last_response_id
    assert p.last_response_id.startswith("turnaround-")


def test_plugin_pairs_many_sequential_model_calls():
    """The bug this guards: only 2 of N chat spans survived because before/after
    were keyed on a context object that differed between them."""
    h = make_harness()
    p = GenAiObservabilityPlugin(h.telemetry)
    usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=5)

    async def scenario():
        with h.telemetry.invoke_agent("producer"):
            for _ in range(6):
                await p.before_model_callback(
                    callback_context=SimpleNamespace(agent_name="a"),
                    llm_request=SimpleNamespace(model="gemini-2.5-flash", config=None, contents=None))
                await p.after_model_callback(
                    callback_context=SimpleNamespace(agent_name="a"),
                    llm_response=SimpleNamespace(model_version="m", interaction_id=None,
                                                 finish_reason="STOP", usage_metadata=usage,
                                                 content=None, error_message=None))

    _run(scenario())
    assert len(h.spans_by_name("chat gemini-2.5-flash")) == 6
    counts = {p.attributes["gen_ai.token.type"]: p.count
              for p in h.metric_points("gen_ai.client.token.usage")}
    assert counts == {"input": 6, "output": 6}
