"""Runtime configuration for the agent tier.

One place that: loads ``.env`` (without clobbering a real environment),
bootstraps Vertex AI, picks the Gemini models, and locates the ``mcp-grafana``
binary. Everything else in ``agent/`` imports from here so there is a single
answer to "which model / which stack / which binary".

The hackathon rules bar every non-Google AI runtime, so the only model strings
this module will hand out are Gemini on Vertex. ``GOOGLE_GENAI_USE_VERTEXAI`` is
forced on; there is deliberately no path to the public Generative Language API.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# .env loading -- stdlib only, real environment always wins
# --------------------------------------------------------------------------- #


def load_env(path: Path | None = None) -> None:
    """Populate ``os.environ`` from ``.env`` for keys that are not already set.

    A real exported variable (CI, Cloud Run, ``set -a && . ./.env``) is never
    overwritten. Lines are ``KEY=VALUE``; ``#`` comments and blanks are skipped;
    surrounding quotes on the value are stripped. No interpolation -- values are
    taken literally, which is what an OTLP auth header needs.
    """
    env_path = path or REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# --------------------------------------------------------------------------- #
# Defaults every knob falls back to
# --------------------------------------------------------------------------- #

#: Everything runs on flash. The coordinator does more planning and would
#: benefit from pro, but this project's Vertex quota for ``gemini-2.5-pro`` is
#: effectively zero (429 RESOURCE_EXHAUSTED on the first call), so
#: flash-everywhere is what actually runs. Override with
#: ``TURNAROUND_GEMINI_MODEL`` (Gemini on Vertex only -- see the module docstring).
DEFAULT_ANALYST_MODEL = "gemini-2.5-flash"

#: Circuit breaker for a run: the ceiling on Gemini calls across the whole
#: pipeline, passed to ADK's ``RunConfig(max_llm_calls=...)``. The five-step
#: deterministic pipeline needs ~30 in the worst honest case (each analyst may
#: retry a query once); ADK's own default is 500, high enough for a stuck
#: tool-retry loop to burn real money before anything stops it. Override with
#: ``TURNAROUND_MAX_LLM_CALLS`` for a legitimately longer run.
DEFAULT_MAX_LLM_CALLS = 40

#: Public-endpoint guardrails. The deployed demo is open -- no key, no signup --
#: so these are what stands between a curious judge and an unbounded Vertex
#: bill. They are *per instance* (see ``agent/limits.py``): with
#: ``--max-instances 2`` the real ceiling is twice DEFAULT_ASKS_PER_DAY, which
#: is the number to sanity-check against a billing alert, not this one.
DEFAULT_ASKS_PER_HOUR = 8       # per visitor: enough to explore, not to farm
DEFAULT_ASKS_PER_DAY = 200      # everyone together: the actual budget
DEFAULT_CONCURRENT_ASKS = 2     # a run holds an MCP subprocess for ~40s


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Grafana + Vertex settings
# --------------------------------------------------------------------------- #


#: How the agents reach Grafana's MCP tools.
#:  "oss"    -- a local ``grafana/mcp-grafana`` stdio subprocess with a service
#:             account token. Read-only is enforced *by construction*
#:             (``--disable-write``); runs unattended, so this is the default and
#:             the deployed/demo path.
#:  "hosted" -- the hosted ``https://mcp.grafana.com/mcp`` endpoint over
#:             Streamable HTTP with OAuth 2.1 (see ``agent/mcp_login.py``). No
#:             service-account path, so it cannot run unattended; offered as an
#:             opt-in mode that exercises the interactive-authorization flow the
#:             proposal calls for. Read-only is then enforced by ``tool_filter``
#:             plus the approval gate, not by a flag.
HOSTED_MCP_URL = "https://mcp.grafana.com/mcp"


@dataclass(frozen=True, slots=True)
class Settings:
    """Every runtime knob, resolved once, after ``.env`` has been loaded.

    This is the only place the rest of the agent tier reads configuration from.
    There are deliberately no module-level constants holding an env var: those
    freeze at import time, which is *before* :func:`load_env` runs, so a value
    set only in ``.env`` would be silently ignored.
    """

    grafana_url: str
    grafana_token: str
    gcp_project: str
    gcp_location: str
    mcp_grafana_bin: str
    mcp_mode: str = "oss"
    grafana_cloud_mcp_token: str = ""
    ds_prom: str = "grafanacloud-prom"
    ds_loki: str = "grafanacloud-logs"
    ds_tempo: str = "grafanacloud-traces"
    #: the one Gemini model every agent and the LLM judge run on
    analyst_model: str = DEFAULT_ANALYST_MODEL
    #: per-run ceiling on Gemini calls (the circuit breaker)
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS
    #: public-endpoint guardrails, per instance -- see ``agent/limits.py``
    asks_per_hour: int = DEFAULT_ASKS_PER_HOUR
    asks_per_day: int = DEFAULT_ASKS_PER_DAY
    concurrent_asks: int = DEFAULT_CONCURRENT_ASKS

    @property
    def grafana_ready(self) -> bool:
        return bool(self.grafana_url and self.grafana_token)

    @property
    def vertex_ready(self) -> bool:
        return bool(self.gcp_project)

    @property
    def hosted_mcp(self) -> bool:
        return self.mcp_mode == "hosted"


def _find_mcp_grafana() -> str:
    """Absolute path to the ``mcp-grafana`` binary.

    ``uv run`` does not inherit an interactive shell's PATH additions, so a
    Homebrew install at ``/opt/homebrew/bin`` is invisible to ``shutil.which``
    under some launchers. Check the obvious locations too before giving up.
    """
    override = os.environ.get("TURNAROUND_MCP_GRAFANA_BIN")
    if override:
        return override
    found = shutil.which("mcp-grafana")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/mcp-grafana", "/usr/local/bin/mcp-grafana",
                      str(Path.home() / "go/bin/mcp-grafana")):
        if Path(candidate).is_file():
            return candidate
    return "mcp-grafana"  # let the failure name the missing binary


def settings() -> Settings:
    """Resolve every knob from ``.env`` + the real environment. Call this, not
    ``os.environ``, from anywhere in ``agent/``."""
    load_env()
    mode = os.environ.get("TURNAROUND_MCP_MODE", "oss").strip().lower()
    return Settings(
        grafana_url=os.environ.get("GRAFANA_URL", "").rstrip("/"),
        grafana_token=os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""),
        gcp_project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        gcp_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        mcp_grafana_bin=_find_mcp_grafana(),
        mcp_mode="hosted" if mode == "hosted" else "oss",
        grafana_cloud_mcp_token=os.environ.get("GRAFANA_CLOUD_MCP_TOKEN", "")
        or _read_token_file(),
        ds_prom=os.environ.get("GRAFANA_DS_PROM_UID", "grafanacloud-prom"),
        ds_loki=os.environ.get("GRAFANA_DS_LOKI_UID", "grafanacloud-logs"),
        ds_tempo=os.environ.get("GRAFANA_DS_TEMPO_UID", "grafanacloud-traces"),
        analyst_model=os.environ.get("TURNAROUND_GEMINI_MODEL", "").strip()
        or DEFAULT_ANALYST_MODEL,
        max_llm_calls=_int_env("TURNAROUND_MAX_LLM_CALLS", DEFAULT_MAX_LLM_CALLS),
        asks_per_hour=_int_env("TURNAROUND_ASKS_PER_HOUR", DEFAULT_ASKS_PER_HOUR),
        asks_per_day=_int_env("TURNAROUND_ASKS_PER_DAY", DEFAULT_ASKS_PER_DAY),
        concurrent_asks=_int_env("TURNAROUND_CONCURRENT_ASKS", DEFAULT_CONCURRENT_ASKS),
    )


#: where ``agent/mcp_login.py`` drops the bearer token from the OAuth flow
CLOUD_MCP_TOKEN_FILE = REPO_ROOT / ".secrets" / "grafana-cloud-mcp-token"


def _read_token_file() -> str:
    try:
        return CLOUD_MCP_TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def bootstrap_vertex() -> Settings:
    """Force Vertex mode and make sure ADK/genai can see the project.

    Returns the resolved settings so callers can fail early with a clear message
    when the project or credentials are missing.
    """
    cfg = settings()
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    if cfg.gcp_project:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", cfg.gcp_project)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", cfg.gcp_location)
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds and not Path(creds).is_absolute():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(REPO_ROOT / creds)
    return cfg
