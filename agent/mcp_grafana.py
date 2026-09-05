"""``mcp-grafana`` toolsets for the agents.

Two instances of the OSS ``grafana/mcp-grafana`` server, run as stdio
subprocesses (the transport ADK spawns and owns directly -- no ports, one less
moving part for the Cloud Run image):

* **analyst** -- ``--disable-write``. Read-only *by construction*. Every analyst
  agent gets this one, narrowed further with ``tool_filter`` so the model sees
  ten relevant tools instead of sixty-six.
* **remediator** -- ``--enabled-tools annotations,incident``. Write-capable, but
  only for annotations and incidents; it cannot touch dashboards, datasources or
  alert rules. Attached to the Remediator alone, and every call still passes the
  approval gate first.

The hosted Grafana Cloud MCP endpoint is interactive-OAuth only with no
service-account path (PROJECT.md section 2), which is why this is the OSS server
with a service-account token rather than a remote URL.
"""

from __future__ import annotations

import os

from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from agent.config import Settings

# The read tools each analyst is allowed to see. All three share a common read
# core (Prometheus + Loki + discovery + annotations + alerts); the FarmAnalyst
# additionally gets Tempo. Every analyst is told about logs and traces in the
# shared vocabulary, so each must be able to reach them -- ADK aborts the whole
# run with a ValueError if a model calls a tool outside its filter, which is not
# a failure mode worth risking on camera to save a few tokens of tool schema.
_READ_CORE = [
    "query_prometheus", "query_prometheus_histogram",
    "list_prometheus_metric_names", "list_prometheus_label_names",
    "list_prometheus_label_values", "list_prometheus_metric_metadata",
    "query_loki_logs", "query_loki_stats", "list_loki_label_names",
    "list_loki_label_values", "get_annotations", "get_annotation_tags",
    "list_alert_groups", "get_alert_group",
]
SCHEDULE_TOOLS = list(_READ_CORE)
FARM_TOOLS = [*_READ_CORE, "tempo_traceql-search", "tempo_get-trace",
              "tempo_traceql-metrics-range"]
CRUNCH_TOOLS = list(_READ_CORE)
REMEDIATOR_WRITE_TOOLS = ["create_annotation", "update_annotation", "add_activity_to_incident"]


def _env(cfg: Settings) -> dict[str, str]:
    """Environment for the subprocess: inherit, drop the parent's OTLP config,
    pin what mcp-grafana needs.

    The agent process inherits the seeder's ``OTEL_EXPORTER_OTLP_*`` variables
    from ``.env``; if mcp-grafana sees them it tries to export its own telemetry
    over gRPC to a collector that is not there and prints ten seconds of ALPN
    handshake errors. Strip every ``OTEL_*`` and disable the SDK. Phase 6 points
    mcp-grafana at our gateway on purpose (AI Observability); until then, off.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["GRAFANA_URL"] = cfg.grafana_url
    env["GRAFANA_SERVICE_ACCOUNT_TOKEN"] = cfg.grafana_token
    env["OTEL_SDK_DISABLED"] = "true"
    return env


def _toolset(cfg: Settings, *, extra_args: list[str], tool_filter: list[str]) -> McpToolset:
    # No tool_name_prefix: the canonical mcp-grafana names are what the timeline
    # marks as Grafana calls and what the approval gate matches writes on.
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=cfg.mcp_grafana_bin,
                args=["-t", "stdio", *extra_args],
                env=_env(cfg),
            ),
            timeout=30.0,
        ),
        tool_filter=tool_filter,
    )


def analyst_toolset(cfg: Settings, tool_filter: list[str]) -> McpToolset:
    """A read-only mcp-grafana, narrowed to ``tool_filter``."""
    return _toolset(cfg, extra_args=["--disable-write"], tool_filter=tool_filter)


def remediator_toolset(cfg: Settings) -> McpToolset:
    """A write-capable mcp-grafana limited to annotations and incidents."""
    return _toolset(
        cfg,
        extra_args=["--enabled-tools", "annotations,incident"],
        tool_filter=REMEDIATOR_WRITE_TOOLS,
    )
