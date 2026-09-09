"""A record of every tool call the agents make.

The agent-tier gate is "four demo questions answered cold, with a tool timeline
showing real MCP calls". This is that timeline: a small, ordered log that the
``before``/``after`` tool callbacks on each agent write to, so the console and
the CLI can show *which agent called which Grafana MCP tool with what arguments*
and how long it took. It is evidence that the answer came from the live stack
and not from the model's imagination.

No dependency on ADK -- the callbacks in ``agent/analysts.py`` adapt ADK's
signatures to :meth:`ToolTimeline.begin` / :meth:`ToolTimeline.finish`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

_MCP_GRAFANA_TOOLS = frozenset({
    # the read side the analysts use -- kept as a set so render() can mark which
    # calls actually hit Grafana Cloud vs. local function tools
    "query_prometheus", "query_prometheus_histogram", "list_prometheus_metric_names",
    "list_prometheus_label_names", "list_prometheus_label_values",
    "list_prometheus_metric_metadata", "query_loki_logs", "query_loki_stats",
    "query_loki_patterns", "list_loki_label_names", "list_loki_label_values",
    "analyze_loki_labels", "get_annotations", "get_annotation_tags",
    "list_alert_groups", "get_alert_group", "search_dashboards",
    "get_dashboard_by_uid", "get_dashboard_panel_queries", "list_datasources",
    "tempo_traceql-search", "tempo_get-trace", "tempo_traceql-metrics-range",
    "tempo_traceql-metrics-instant", "tempo_get-attribute-names",
    "tempo_get-attribute-values",
    # the write side the remediator uses, behind the approval gate
    "create_annotation", "update_annotation", "create_incident",
    "add_activity_to_incident",
})


def _digest(value: object, limit: int = 160) -> str:
    """A single-line, length-capped repr of a tool argument or result."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(slots=True)
class ToolCall:
    seq: int
    agent: str
    tool: str
    args: dict
    started_at: datetime
    _t0: float
    duration_ms: float | None = None
    ok: bool | None = None
    result_digest: str = ""

    @property
    def is_grafana_mcp(self) -> bool:
        return self.tool in _MCP_GRAFANA_TOOLS

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "agent": self.agent,
            "tool": self.tool,
            "grafana_mcp": self.is_grafana_mcp,
            "args": {k: _digest(v) for k, v in self.args.items()},
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(self.duration_ms, 1) if self.duration_ms is not None else None,
            "ok": self.ok,
            "result": self.result_digest,
        }


def failed(response: object) -> bool:
    """Did this tool response *not* complete?

    Two shapes mean failure and they are the only two Turnaround produces: an
    MCP error envelope (``isError``) from ``mcp-grafana``, and a call the
    approval gate refused (``status: blocked``). One predicate so the analysts'
    and the Remediator's timelines agree on what "ok" means.
    """
    return isinstance(response, dict) and bool(
        response.get("isError") or response.get("status") == "blocked"
    )


@dataclass(slots=True)
class ToolTimeline:
    calls: list[ToolCall] = field(default_factory=list)

    def begin(self, *, agent: str, tool: str, args: dict) -> ToolCall:
        call = ToolCall(
            seq=len(self.calls) + 1,
            agent=agent,
            tool=tool,
            args=dict(args or {}),
            started_at=datetime.now(UTC),
            _t0=time.perf_counter(),
        )
        self.calls.append(call)
        return call

    def finish(self, call: ToolCall, *, result: object = None, ok: bool = True) -> None:
        call.duration_ms = (time.perf_counter() - call._t0) * 1000.0
        call.ok = ok
        call.result_digest = _digest(result)

    # -- reporting ------------------------------------------------------------ #

    def grafana_calls(self) -> list[ToolCall]:
        return [c for c in self.calls if c.is_grafana_mcp]

    def as_dicts(self) -> list[dict]:
        return [c.to_dict() for c in self.calls]

    def render(self) -> str:
        if not self.calls:
            return "(no tool calls)"
        rows = [f"{'#':>2}  {'agent':<16} {'tool':<26} {'ms':>7}  ok  args"]
        rows.append("-" * 96)
        for c in self.calls:
            ms = f"{c.duration_ms:.0f}" if c.duration_ms is not None else "-"
            ok = "..." if c.ok is None else ("ok " if c.ok else "ERR")
            mark = "*" if c.is_grafana_mcp else " "
            rows.append(f"{c.seq:>2}{mark} {c.agent:<16} {c.tool:<26} {ms:>7}  {ok}  "
                        f"{_digest(c.args, 90)}")
        rows.append("-" * 96)
        g = len(self.grafana_calls())
        rows.append(f"{len(self.calls)} tool calls, {g} against Grafana Cloud MCP (marked *)")
        return "\n".join(rows)


@dataclass(slots=True)
class TimelineRecorder:
    """Pairs ADK's ``before_tool`` / ``after_tool`` callbacks onto one timeline.

    ADK gives no call id linking a before to its after, but it hands the *same*
    ``ToolContext`` instance to both and runs a turn's tool calls sequentially,
    so keying the pending call on ``id(tool_context)`` is exact.

    :meth:`finish` consumes the pending entry, which makes a second finish for
    the same call a no-op. The Remediator relies on that: when the approval gate
    blocks a write it finishes the call itself in the *before* hook, and ADK's
    subsequent after-hook must not add a duplicate row. An after with no before
    is likewise dropped rather than invented -- the timeline is evidence, and a
    row with no arguments is worse than no row.
    """

    timeline: ToolTimeline
    default_agent: str = "?"
    _pending: dict[int, ToolCall] = field(default_factory=dict)

    def begin(self, tool: object, args: dict | None, tool_context: object) -> ToolCall:
        call = self.timeline.begin(
            agent=getattr(tool_context, "agent_name", self.default_agent),
            tool=getattr(tool, "name", str(tool)),
            args=args or {},
        )
        self._pending[id(tool_context)] = call
        return call

    def finish(self, tool_context: object, response: object) -> None:
        call = self._pending.pop(id(tool_context), None)
        if call is not None:
            self.timeline.finish(call, result=response, ok=not failed(response))
