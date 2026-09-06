"""GenAI telemetry: OTel spans, metrics and evaluation events under ``gen_ai.*``.

Turnaround's domain telemetry (``bridge/``) makes a *shot* legible in Grafana.
This module makes the *agent* legible in the same stack, following the
OpenTelemetry GenAI semantic conventions so the data reads the same as any other
Gemini/ADK workload a studio's AI engineers already watch:

  * an ``invoke_agent`` span per question, with ``chat`` (LLM) and
    ``execute_tool`` (MCP) children;
  * ``gen_ai.client.token.usage`` and ``gen_ai.client.operation.duration``
    histograms for the token-budget and latency panels;
  * ``gen_ai.evaluation.result`` log events from the judge tier (see
    ``agent/evaluation.py``), correlated to the run by ``gen_ai.response.id``.

It is deliberately free of any ``agent/`` or ``bridge/`` import so it can be
lifted into its own package later (Horizon B). The one Turnaround-specific hook
is injected: ``content_guard`` is handed in by the caller
(``bridge.privacy.assert_no_pii`` in this repo) so opt-in prompt capture cannot
leak crew identity, without this module knowing what a crew is.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics as _metrics
from opentelemetry import trace as _trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs._internal import LogRecord
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

SCOPE = "turnaround.observability"

# -- semantic-convention attribute keys ------------------------------------- #
# Spelled out rather than imported from opentelemetry.semconv so the string a
# panel query needs is visible in this file and stable if the incubating module
# reshuffles.
OP_NAME = "gen_ai.operation.name"
AGENT_NAME = "gen_ai.agent.name"
PROVIDER_NAME = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
RESPONSE_ID = "gen_ai.response.id"
FINISH_REASONS = "gen_ai.response.finish_reasons"
USAGE_INPUT = "gen_ai.usage.input_tokens"
USAGE_OUTPUT = "gen_ai.usage.output_tokens"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call_id"
CONVERSATION_ID = "gen_ai.conversation.id"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
EVAL_NAME = "gen_ai.evaluation.name"
EVAL_SCORE_VALUE = "gen_ai.evaluation.score.value"
EVAL_SCORE_LABEL = "gen_ai.evaluation.score.label"
EVAL_EXPLANATION = "gen_ai.evaluation.explanation"
EVAL_ACTOR_TYPE = "gen_ai.evaluation.actor.type"  # "ai" | "human" | "deterministic"

# Turnaround-specific: the query text behind an execute_tool span, so a weak
# answer can be pivoted straight to the PromQL/LogQL that fed it.
TURNAROUND_QUERY = "turnaround.query"
TURNAROUND_QUERY_LANG = "turnaround.query.lang"

METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_OP_DURATION = "gen_ai.client.operation.duration"
EVENT_EVALUATION = "gen_ai.evaluation.result"

_PROVIDER_VERTEX = "gcp.vertex_ai"

# Advisory histogram buckets from the semantic conventions. Registered via a
# View in providers.py; kept here so the two stay together.
TOKEN_BUCKETS = (1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304)
DURATION_BUCKETS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12,
                    10.24, 20.48, 40.96, 81.92)


def _clean(attrs: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values -- OTel rejects them and half our attrs are optional."""
    return {k: v for k, v in attrs.items() if v is not None}


@dataclass(slots=True)
class ChatOutcome:
    """What ``after_model`` learned about one LLM call, passed back to ``end_chat``."""

    response_model: str | None = None
    response_id: str | None = None
    finish_reasons: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    output_messages: Any | None = None
    error: str | None = None


@dataclass(slots=True)
class GenAiTelemetry:
    """Owns the tracer/meter/logger and turns agent lifecycle events into signals.

    One instance per process, created by :func:`observability.instrument`. The
    ADK plugin (``observability/adk.py``) is the only caller in Turnaround; the
    methods are also usable directly from a plain script.
    """

    tracer_provider: TracerProvider
    logger_provider: LoggerProvider
    meter_provider: _metrics.MeterProvider
    capture_content: bool = False
    content_guard: Callable[[Mapping[str, Any]], None] | None = None
    provider_name: str = _PROVIDER_VERTEX

    _tracer: _trace.Tracer = field(init=False)
    _token_usage: _metrics.Histogram = field(init=False)
    _op_duration: _metrics.Histogram = field(init=False)
    _logger: Any = field(init=False)

    def __post_init__(self) -> None:
        self._tracer = self.tracer_provider.get_tracer(SCOPE)
        meter = self.meter_provider.get_meter(SCOPE)
        self._token_usage = meter.create_histogram(
            METRIC_TOKEN_USAGE, unit="{token}",
            description="Number of input and output tokens used per request.",
        )
        self._op_duration = meter.create_histogram(
            METRIC_OP_DURATION, unit="s",
            description="GenAI operation duration.",
        )
        self._logger = self.logger_provider.get_logger(SCOPE)

    # -- invoke_agent ----------------------------------------------------- #

    @contextmanager
    def invoke_agent(self, name: str, *, conversation_id: str | None = None):
        """Wrap one agent's run. Nesting is automatic via the OTel context."""
        attrs = _clean({
            OP_NAME: "invoke_agent",
            AGENT_NAME: name,
            CONVERSATION_ID: conversation_id,
        })
        started = time.perf_counter()
        with self._tracer.start_as_current_span(
            f"invoke_agent {name}", kind=SpanKind.INTERNAL, attributes=attrs
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                self._op_duration.record(
                    time.perf_counter() - started,
                    {OP_NAME: "invoke_agent", AGENT_NAME: name},
                )

    # -- chat ----------------------------------------------------------- #

    def start_chat(self, *, request_model: str | None, agent_name: str | None,
                   system_instructions: Any | None = None,
                   input_messages: Any | None = None) -> tuple[Span, float]:
        attrs = _clean({
            OP_NAME: "chat",
            PROVIDER_NAME: self.provider_name,
            REQUEST_MODEL: request_model,
            AGENT_NAME: agent_name,
        })
        span = self._tracer.start_span(
            f"chat {request_model or 'model'}", kind=SpanKind.CLIENT, attributes=attrs
        )
        if self.capture_content:
            self._attach_content(span, {
                SYSTEM_INSTRUCTIONS: system_instructions,
                INPUT_MESSAGES: input_messages,
            })
        return span, time.perf_counter()

    def end_chat(self, span: Span, started: float, outcome: ChatOutcome,
                 *, request_model: str | None, agent_name: str | None) -> None:
        span.set_attributes(_clean({
            RESPONSE_MODEL: outcome.response_model,
            RESPONSE_ID: outcome.response_id,
            FINISH_REASONS: outcome.finish_reasons or None,
            USAGE_INPUT: outcome.input_tokens,
            USAGE_OUTPUT: outcome.output_tokens,
        }))
        if self.capture_content and outcome.output_messages is not None:
            self._attach_content(span, {OUTPUT_MESSAGES: outcome.output_messages})
        if outcome.error:
            span.set_status(Status(StatusCode.ERROR, outcome.error))

        base = _clean({OP_NAME: "chat", REQUEST_MODEL: request_model,
                       PROVIDER_NAME: self.provider_name})
        self._op_duration.record(time.perf_counter() - started, base)
        if outcome.input_tokens is not None:
            self._token_usage.record(outcome.input_tokens, {**base, "gen_ai.token.type": "input"})
        if outcome.output_tokens is not None:
            self._token_usage.record(outcome.output_tokens, {**base, "gen_ai.token.type": "output"})
        span.end()

    # -- execute_tool -------------------------------------------------- #

    @contextmanager
    def execute_tool(self, *, tool_name: str, call_id: str | None = None,
                     query: str | None = None, query_lang: str | None = None):
        attrs = _clean({
            OP_NAME: "execute_tool",
            TOOL_NAME: tool_name,
            TOOL_CALL_ID: call_id,
            TURNAROUND_QUERY: query,
            TURNAROUND_QUERY_LANG: query_lang,
        })
        started = time.perf_counter()
        with self._tracer.start_as_current_span(
            f"execute_tool {tool_name}", kind=SpanKind.INTERNAL, attributes=attrs
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                self._op_duration.record(
                    time.perf_counter() - started,
                    {OP_NAME: "execute_tool", TOOL_NAME: tool_name},
                )

    # -- evaluation events ------------------------------------------- #

    def emit_evaluation(self, *, name: str, score_value: float, score_label: str,
                        explanation: str, response_id: str | None,
                        actor_type: str = "ai",
                        extra: Mapping[str, Any] | None = None) -> None:
        """Emit one ``gen_ai.evaluation.result`` log event.

        The GenAI events spec models an evaluation as a log record, not a span,
        so it lands in Loki beside the bridge's shot logs and is reachable
        through mcp-grafana's Loki tools.

        The body is a JSON object (queried with ``| json`` in Loki); only the
        low-cardinality dimensions go in log attributes, so a free-text
        ``explanation`` never becomes a Loki label.
        """
        payload: dict[str, Any] = {
            "name": name,
            "score": float(score_value),
            "label": score_label,
            "actor_type": actor_type,
            "response_id": response_id,
            "explanation": explanation,
            **(extra or {}),
        }
        if self.content_guard is not None:
            # Catch a stray email / raw id in the explanation before it ships.
            self.content_guard(payload)
        attrs = _clean({
            "event.name": EVENT_EVALUATION,
            EVAL_NAME: name,
            EVAL_SCORE_LABEL: score_label,
            EVAL_ACTOR_TYPE: actor_type,
            RESPONSE_ID: response_id,
        })
        self._logger.emit(LogRecord(
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            severity_text="INFO",
            severity_number=SeverityNumber.INFO,
            body=_stringify(payload),
            attributes=attrs,
        ))

    # -- content capture ------------------------------------------- #

    def _attach_content(self, span: Span, payload: Mapping[str, Any]) -> None:
        cleaned = _clean({k: _stringify(v) for k, v in payload.items()})
        if not cleaned:
            return
        if self.content_guard is not None:
            # A prompt that carries a crew name is a privacy regression -- fail
            # here rather than let it reach the exporter.
            self.content_guard(cleaned)
        span.set_attributes(cleaned)

    def flush(self, timeout_millis: int = 30_000) -> bool:
        ok = self.tracer_provider.force_flush(timeout_millis)
        ok = self.logger_provider.force_flush(timeout_millis) and ok
        ok = self.meter_provider.force_flush(timeout_millis) and ok
        return bool(ok)

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.logger_provider.shutdown()
        self.meter_provider.shutdown()


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def new_response_id() -> str:
    """A correlation id for a run when the model gives us none of its own."""
    return f"turnaround-{uuid.uuid4().hex[:16]}"
