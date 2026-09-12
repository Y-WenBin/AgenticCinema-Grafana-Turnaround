"""ADK plugin: agent lifecycle callbacks -> ``gen_ai.*`` telemetry.

Attached once on the ``Runner`` (``agent/engine.py``); needs no per-agent wiring and
survives new sub-agents being added to the pipeline.

Two facts about ADK 2.8 shape this:

* The plugin's ``before_model``/``after_model`` hooks receive **different**
  ``callback_context`` objects, so a span cannot be paired across them by object
  identity. But Turnaround's pipeline is a ``SequentialAgent`` and ADK does not
  run a turn's tool calls concurrently, so model and tool calls form one
  strictly sequential stream -- a stack pairs the model calls with no keys. Tool
  calls carry ADK's own ``function_call_id``, so those pair on that. They used
  to pair on the ToolContext's *address*, which is not an identity the plugin
  gets to keep -- see ``_tools``.
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
        #: open execute_tool spans, keyed on ADK's own ``function_call_id``.
        #:
        #: This used to key on the *address* of the ToolContext, for an object
        #: the plugin deliberately does not hold a reference to. The context is
        #: freed the moment ``before_tool_callback`` returns, and the lookup
        #: worked only because CPython usually hands the next ToolContext the
        #: address it just freed. Allocate anything in between and the key
        #: differs, the lookup misses, and the ``execute_tool`` span is never
        #: closed -- so it never reaches the exporter and never appears in
        #: Tempo. From the outside that looked like a test failing about one run
        #: in ten. Worse, a freed address can be reused by an unrelated object,
        #: and then the miss becomes a hit that closes the wrong span.
        #:
        #: ``function_call_id`` is ADK's own identifier for one tool call, it is
        #: already read a few lines below, and it stays valid however the
        #: context objects happen to be allocated.
        self._tools: dict[str, tuple[Any, Any]] = {}
        #: Calls arriving with no ``function_call_id`` at all. Paired LIFO, for
        #: the same reason the model hooks are (see ``_chat_stack``): within one
        #: agent the callback stream is strictly nested.
        self._anonymous_tools: list[tuple[Any, Any]] = []

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
        entry = (cm, cm.__enter__())
        if call_id is None:
            self._anonymous_tools.append(entry)
        else:
            self._tools[call_id] = entry

    def _close_tool(self, tool_context: Any) -> tuple[Any, Any] | None:
        """The open span for this call, taken from whichever side holds it."""
        call_id = getattr(tool_context, "function_call_id", None)
        if call_id is not None:
            return self._tools.pop(call_id, None)
        return self._anonymous_tools.pop() if self._anonymous_tools else None

    async def after_tool_callback(self, *, tool: Any, tool_args: dict[str, Any],
                                  tool_context: Any, result: Any) -> None:
        entry = self._close_tool(tool_context)
        if entry is None:
            return
        cm, span = entry
        if isinstance(result, dict) and (result.get("isError") or result.get("status") == "blocked"):
            span.set_status(Status(StatusCode.ERROR, "tool did not complete"))
        cm.__exit__(None, None, None)

    async def on_tool_error_callback(self, *, tool: Any, tool_args: dict[str, Any],
                                     tool_context: Any, error: Exception) -> None:
        entry = self._close_tool(tool_context)
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
