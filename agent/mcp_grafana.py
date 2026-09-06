"""``mcp-grafana`` toolsets for the agents, in one of two modes (``agent/config.py``).

**oss** (default, deployed path). Two instances of OSS ``grafana/mcp-grafana``
run as stdio subprocesses ADK spawns and owns -- no ports:

* **analyst** -- ``--disable-write``. Read-only *by construction*: a
  prompt-injected "change X" has nothing to call.
* **remediator** -- ``--enabled-tools annotations,incident``. Write-capable for
  annotations and incidents only; every call still passes the approval gate.

**hosted** (opt-in). The hosted ``https://mcp.grafana.com/mcp`` endpoint over
Streamable HTTP, the Grafana instance passed in the ``X-Grafana-URL`` header and
a bearer token from the OAuth 2.1 flow (``agent/mcp_login.py``). The hosted
endpoint has **no service-account path**, so this mode cannot run unattended --
it is here to exercise the interactive-authorization flow, not to deploy.
There is no ``--disable-write`` server to lean on, so in hosted mode read-only
is enforced by ``tool_filter`` and the approval gate alone; the privilege split
is a filter, not a separate process.
"""

from __future__ import annotations

import os

from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from agent.config import HOSTED_MCP_URL, Settings

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


class HostedMcpNotAuthorized(RuntimeError):
    """Hosted mode was selected but no Cloud MCP bearer token is available."""


def _env(cfg: Settings) -> dict[str, str]:
    """Environment for the stdio subprocess: inherit, drop the parent's OTLP
    config, pin what mcp-grafana needs.

    The agent process inherits the seeder's ``OTEL_EXPORTER_OTLP_*`` variables;
    if mcp-grafana sees them it tries to export its own telemetry over gRPC to a
    collector that is not there and prints ten seconds of ALPN handshake errors.
    Strip every ``OTEL_*`` and disable the SDK. (``observability/`` instruments
    the ADK process directly, not this subprocess.)
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["GRAFANA_URL"] = cfg.grafana_url
    env["GRAFANA_SERVICE_ACCOUNT_TOKEN"] = cfg.grafana_token
    env["OTEL_SDK_DISABLED"] = "true"
    return env


# --------------------------------------------------------------------------- #
# Connection-params builders -- pure, so the mode wiring is unit-testable
# --------------------------------------------------------------------------- #


def oss_params(cfg: Settings, *, extra_args: list[str]) -> StdioConnectionParams:
    return StdioConnectionParams(
        server_params=StdioServerParameters(
            command=cfg.mcp_grafana_bin,
            args=["-t", "stdio", *extra_args],
            env=_env(cfg),
        ),
        timeout=30.0,
    )


def hosted_params(cfg: Settings) -> StreamableHTTPConnectionParams:
    if not cfg.grafana_cloud_mcp_token:
        raise HostedMcpNotAuthorized(
            "TURNAROUND_MCP_MODE=hosted but no Cloud MCP token. Run "
            "`uv run python -m agent.mcp_login` to authorize in a browser, or "
            "set GRAFANA_CLOUD_MCP_TOKEN. Hosted mode has no service-account "
            "path; use the default oss mode for unattended runs."
        )
    return StreamableHTTPConnectionParams(
        url=HOSTED_MCP_URL,
        headers={
            "X-Grafana-URL": cfg.grafana_url,
            "Authorization": f"Bearer {cfg.grafana_cloud_mcp_token}",
        },
        timeout=30.0,
    )


def _toolset(cfg: Settings, *, tool_filter: list[str], oss_extra_args: list[str]) -> McpToolset:
    # No tool_name_prefix: the canonical mcp-grafana names are what the timeline
    # marks as Grafana calls and what the approval gate matches writes on.
    params = hosted_params(cfg) if cfg.hosted_mcp else oss_params(cfg, extra_args=oss_extra_args)
    return McpToolset(connection_params=params, tool_filter=tool_filter)


def analyst_toolset(cfg: Settings, tool_filter: list[str]) -> McpToolset:
    """A read-only Grafana MCP toolset, narrowed to ``tool_filter``.

    oss: an ``--disable-write`` subprocess (read-only by construction).
    hosted: the same ``tool_filter`` against mcp.grafana.com, read-only by
    filter -- there is no write-disabling server in this mode.
    """
    return _toolset(cfg, tool_filter=tool_filter, oss_extra_args=["--disable-write"])


def remediator_toolset(cfg: Settings) -> McpToolset:
    """A write-capable Grafana MCP toolset limited to annotations and incidents.
    Every call still passes ``ApprovalGate.before_tool`` first, in both modes."""
    return _toolset(cfg, tool_filter=REMEDIATOR_WRITE_TOOLS,
                    oss_extra_args=["--enabled-tools", "annotations,incident"])
