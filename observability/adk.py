"""ADK plugin: agent lifecycle callbacks -> ``gen_ai.*`` telemetry.

Attached once on the ``Runner`` (``agent/engine.py``); needs no per-agent wiring and
survives new sub-agents being added to the pipeline.

Two facts about ADK 2.8 shape this:

* The plugin's ``before_model``/``after_model`` hooks receive **different**
  ``callback_context`` objects, so a span cannot be paired across them by object
  identity. But Turnaround's pipeline is a ``SequentialAgent`` and ADK does not
  run a turn's tool calls concurrently, so model and tool calls form one
  strictly sequential stream -- a stack pairs the model calls with no keys. Tool
  calls do get one shared ``ToolContext``, so those pair on its identity.
* Doing ``opentelemetry.context.attach``/``detach`` across two callbacks fails
  (they run in different async contexts). So the ``invoke_agent`` root span is
  opened by ``agent/engine.py`` around the whole run instead, and the ``chat`` and
  ``execute_tool`` spans nest under it through the normal current-span context.

``agent/timeline.py`` still keeps its own in-process log in parallel -- that is
the CLI's evidence dump. This plugin is the *exported* view of the same events.
"""

from __future__ import annotations

from typing import Any

from opentelemetry.trace import Status, StatusCode

from observability.genai import ChatOutcome, GenAiTelemetry, new_response_id

try:  # ADK is a runtime dep; guard only so bare unit tests can import
    from google.adk.plugins.base_plugin import BasePlugin
except Exception:  # noqa: BLE001 - pragma: no cover
    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str = "") -> None:
            self.name = name


# Tool-arg keys that carry the query worth pivoting from, in priority order.
_QUERY_KEYS = (("expr", "promql"), ("query", "logql"), ("logql", "logql"),
               ("promql", "promql"), ("traceql", "traceql"))


def _extract_query(tool_args: dict[str, Any]) -> tuple[str | None, str | None]:
    for key, lang in _QUERY_KEYS:
        val = tool_args.get(key)
        if isinstance(val, str) and val.strip():
            return val, lang
    return None, None


def _first(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value)


class GenAiObservabilityPlugin(BasePlugin):
    """Translate ADK's model/tool callbacks into ``gen_ai.*`` spans and metrics."""

    def __init__(self, telemetry: GenAiTelemetry, *, name: str = "genai_observability") -> None:
        super().__init__(name=name)
        self._t = telemetry
        #: one synthetic id for the whole run -- put on every chat span and used
        #: by the judge tier so a low eval score in Loki pivots to this trace
        self.run_id: str = new_response_id()
        self._chat_stack: list[tuple[Any, float, str | None, str | None]] = []
        #: open execute_tool spans, keyed on the ToolContext ADK hands to both
        #: the before and after hooks for one call (unlike the model hooks,
        #: which get different context objects -- hence the stack above)
        self._tools: dict[int, tuple[Any, Any]] = {}

    @property
    def last_response_id(self) -> str:
        return self.run_id

    # -- chat --------------------------------------------------------- #

    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> None:
        request_model = getattr(llm_request, "model", None)
        agent_name = getattr(callback_context, "agent_name", None)
        sys_instr = inp = None
        if self._t.capture_content:
            cfg = getattr(llm_request, "config", None)
            sys_instr = getattr(cfg, "system_instruction", None)
            inp = getattr(llm_request, "contents", None)
        span, started = self._t.start_chat(
            request_model=request_model, agent_name=agent_name,
            system_instructions=sys_instr, input_messages=inp,
        )
        self._chat_stack.append((span, started, request_model, agent_name))

    async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> None:
        if not self._chat_stack:
            return
        span, started, request_model, agent_name = self._chat_stack.pop()
        outcome = _outcome_from_response(llm_response, capture=self._t.capture_content)
        outcome.response_id = self.run_id  # correlate the whole run on one id
        self._t.end_chat(span, started, outcome,
                         request_model=request_model, agent_name=agent_name)

    async def on_model_error_callback(self, *, callback_context: Any, llm_request: Any,
                                      error: Exception) -> None:
        if not self._chat_stack:
            return
        span, started, request_model, agent_name = self._chat_stack.pop()
        self._t.end_chat(span, started,
                         ChatOutcome(response_id=self.run_id, error=str(error)),
                         request_model=request_model, agent_name=agent_name)

    # -- execute_tool ----------------------------------------------- #

    async def before_tool_callback(self, *, tool: Any, tool_args: dict[str, Any],
                                   tool_context: Any) -> None:
        name = getattr(tool, "name", str(tool))
        call_id = getattr(tool_context, "function_call_id", None)
        query, lang = _extract_query(tool_args or {})
        cm = self._t.execute_tool(tool_name=name, call_id=call_id, query=query, query_lang=lang)
        self._tools[id(tool_context)] = (cm, cm.__enter__())

    async def after_tool_callback(self, *, tool: Any, tool_args: dict[str, Any],
                                  tool_context: Any, result: Any) -> None:
        entry = self._tools.pop(id(tool_context), None)
        if entry is None:
            return
        cm, span = entry
        if isinstance(result, dict) and (result.get("isError") or result.get("status") == "blocked"):
            span.set_status(Status(StatusCode.ERROR, "tool did not complete"))
        cm.__exit__(None, None, None)

    async def on_tool_error_callback(self, *, tool: Any, tool_args: dict[str, Any],
                                     tool_context: Any, error: Exception) -> None:
        entry = self._tools.pop(id(tool_context), None)
        if entry is None:
            return
        cm, span = entry
        span.set_status(Status(StatusCode.ERROR, str(error)))
        cm.__exit__(type(error), error, error.__traceback__)


def _outcome_from_response(llm_response: Any, *, capture: bool) -> ChatOutcome:
    usage = getattr(llm_response, "usage_metadata", None)
    return ChatOutcome(
        response_model=getattr(llm_response, "model_version", None),
        finish_reasons=[r for r in [_first(getattr(llm_response, "finish_reason", None))] if r],
        input_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
        output_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
        output_messages=getattr(llm_response, "content", None) if capture else None,
        error=getattr(llm_response, "error_message", None),
    )
