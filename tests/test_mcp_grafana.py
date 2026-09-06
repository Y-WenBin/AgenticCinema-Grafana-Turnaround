"""The two Grafana MCP modes (agent/config.py). Default is the OSS stdio
subprocess with ``--disable-write``; ``hosted`` targets mcp.grafana.com over
Streamable HTTP with the ``X-Grafana-URL`` header and an OAuth bearer.
"""

from __future__ import annotations

import pytest
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)

from agent.config import HOSTED_MCP_URL, Settings
from agent.mcp_grafana import (
    REMEDIATOR_WRITE_TOOLS,
    SCHEDULE_TOOLS,
    HostedMcpNotAuthorized,
    analyst_toolset,
    remediator_toolset,
)


def _cfg(**over) -> Settings:
    base = {
        "grafana_url": "https://stack.grafana.net", "grafana_token": "glsa_x",
        "gcp_project": "proj", "gcp_location": "us-central1",
        "mcp_grafana_bin": "/opt/homebrew/bin/mcp-grafana",
    }
    return Settings(**{**base, **over})


# --------------------------------------------------------------------------- #
# oss (default)
# --------------------------------------------------------------------------- #


def test_default_mode_is_oss():
    assert _cfg().mcp_mode == "oss"
    assert _cfg().hosted_mcp is False


def test_oss_analyst_is_a_disable_write_stdio_subprocess():
    ts = analyst_toolset(_cfg(), SCHEDULE_TOOLS)
    params = ts.connection_params
    assert isinstance(params, StdioConnectionParams)
    args = params.server_params.args
    assert "--disable-write" in args
    assert ts.tool_filter == SCHEDULE_TOOLS


def test_oss_remediator_is_narrowed_to_write_tools():
    ts = remediator_toolset(_cfg())
    args = ts.connection_params.server_params.args
    assert args[-2:] == ["--enabled-tools", "annotations,incident"]
    assert ts.tool_filter == REMEDIATOR_WRITE_TOOLS


def test_oss_subprocess_env_drops_otel_and_pins_grafana(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example")
    env = analyst_toolset(_cfg(), SCHEDULE_TOOLS).connection_params.server_params.env
    assert not any(k.startswith("OTEL_") and k != "OTEL_SDK_DISABLED" for k in env)
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["GRAFANA_URL"] == "https://stack.grafana.net"


# --------------------------------------------------------------------------- #
# hosted (opt-in)
# --------------------------------------------------------------------------- #


def test_hosted_mode_targets_mcp_grafana_com_with_instance_and_bearer():
    cfg = _cfg(mcp_mode="hosted", grafana_cloud_mcp_token="tok-abc")
    ts = analyst_toolset(cfg, SCHEDULE_TOOLS)
    params = ts.connection_params
    assert isinstance(params, StreamableHTTPConnectionParams)
    assert params.url == HOSTED_MCP_URL == "https://mcp.grafana.com/mcp"
    assert params.headers["X-Grafana-URL"] == "https://stack.grafana.net"
    assert params.headers["Authorization"] == "Bearer tok-abc"
    # read-only is enforced by the filter in this mode, not a server flag
    assert ts.tool_filter == SCHEDULE_TOOLS


def test_hosted_remediator_keeps_the_write_filter_and_the_gate_still_applies():
    cfg = _cfg(mcp_mode="hosted", grafana_cloud_mcp_token="tok")
    ts = remediator_toolset(cfg)
    assert isinstance(ts.connection_params, StreamableHTTPConnectionParams)
    assert ts.tool_filter == REMEDIATOR_WRITE_TOOLS


def test_hosted_without_a_token_fails_with_a_pointer_to_the_login_flow():
    cfg = _cfg(mcp_mode="hosted", grafana_cloud_mcp_token="")
    with pytest.raises(HostedMcpNotAuthorized) as exc:
        analyst_toolset(cfg, SCHEDULE_TOOLS)
    assert "agent.mcp_login" in str(exc.value)


def test_settings_reads_mode_and_token_from_env(monkeypatch):
    from agent import config as cfgmod

    monkeypatch.setenv("TURNAROUND_MCP_MODE", "hosted")
    monkeypatch.setenv("GRAFANA_CLOUD_MCP_TOKEN", "env-tok")
    monkeypatch.setattr(cfgmod, "load_env", lambda: None)
    s = cfgmod.settings()
    assert s.mcp_mode == "hosted" and s.grafana_cloud_mcp_token == "env-tok"

    monkeypatch.setenv("TURNAROUND_MCP_MODE", "OSS")  # case-insensitive, anything != hosted
    assert cfgmod.settings().mcp_mode == "oss"
