"""Runtime configuration for the agent tier.

One place that: loads ``.env`` (without clobbering a real environment),
bootstraps Vertex AI, picks the Gemini models, and locates the ``mcp-grafana``
binary. Everything else in ``agent/`` imports from here so there is a single
answer to "which model / which stack / which binary".

The runtime is deliberately single-vendor: the only model strings this module
will hand out are Gemini on Vertex, ``GOOGLE_GENAI_USE_VERTEXAI`` is forced on,
and there is no path to the public Generative Language API. That is a deployment
constraint rather than a preference -- the pipeline itself holds no opinion about
which model answers, and ``analyst_model`` is the one place to change if you are
porting it. A test keeps the constraint honest rather than aspirational
(``tests/test_reproducibility.py``).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from bridge.dotenv import REPO_ROOT
from bridge.dotenv import load_env as _load_env
from bridge.startup import real

# --------------------------------------------------------------------------- #
# .env loading -- stdlib only, real environment always wins
# --------------------------------------------------------------------------- #


#: ``.env`` loading lives in ``bridge/`` so ``seed.*`` and ``grafana.*`` can use
#: it too (see that module). Re-exported here because this is where the rest of
#: the agent tier -- and the tests -- have always imported it from.
load_env = _load_env


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

#: Gemini 2.5 thinking budget, in tokens, for every agent in the pipeline.
#:
#: This is the single biggest lever on wall-clock. ``gemini-2.5-flash`` defaults
#: to *dynamic* thinking (budget -1). Measured end to end against the live
#: stack, same code, same question, only this knob changed:
#:
#:     budget -1 (dynamic)   75.4s, 60.7s     6/6 scorecard checks pass
#:     budget  0 (off)       24.5s, 18.5s     6/6 scorecard checks pass
#:
#: Roughly 3x, for no measured loss of answer quality. Deployed, the same
#: revision has since been measured between 23s and 63s end to end without any
#: code changing: nearly all of a run is model latency, and that moves around
#: through the day. The knob is still worth what it is worth -- it moved the
#: whole band down -- but a single timing is not a benchmark.
#:
#: Zero is the right default *for this pipeline specifically*, because none of
#: the five agents is doing open-ended reasoning: the analysts are handed
#: finished PromQL recipes (``agent/vocabulary.py``) and asked to run them and
#: report the numbers, and the synthesis agent is forbidden from deriving
#: anything the specialists did not already compute. The work is retrieval and
#: formatting, which is exactly what thinking does not help with. The scorecard
#: (``agent/evaluation.py``) is the regression guard: if a future prompt does
#: need reasoning, its grounding checks go red and this knob comes back up.
#:
#:   0   -- off (default)
#:   -1  -- dynamic, the Gemini default; let the model decide
#:   >0  -- a hard ceiling in thinking tokens
DEFAULT_THINKING_BUDGET = 0


#: Rejections already reported, so a per-request ``load_settings()`` does not
#: reprint one line per request. Keyed on the value as well as the name, so
#: changing a bad value to a different bad value is still reported once.
_REJECTED: set[tuple[str, str]] = set()


def _reject(name: str, raw: str, default: int, why: str) -> int:
    """Fall back to ``default``, but say so. Once.

    Silence here is what let the rate limits shipped in ``.env.example`` be
    inert for the life of the project: a trailing comment made every value
    unparseable, this function answered with the default, and the defaults
    happened to match the numbers in the file -- so nothing looked wrong
    anywhere. The parser bug is fixed (``bridge/dotenv.py``), but the reason it
    was *invisible* was here, and it would have hidden the next one too.

    A value the user did not set is not a mistake and says nothing. A value they
    set and we cannot use always says something: a typo, a stray unit, ``1_000``
    or a zero where a positive number belongs is a decision that did not take
    effect, and a budget that did not take effect is the kind that arrives as a
    bill.
    """
    if (name, raw) not in _REJECTED:
        _REJECTED.add((name, raw))
        print(f"({name}={raw!r} ignored: {why}; using {default})", file=sys.stderr)
    return default


def _int_env(name: str, default: int) -> int:
    """A positive integer from the environment, or ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return _reject(name, raw, default, "not a whole number")
    if value <= 0:
        return _reject(name, raw, default, "must be greater than zero")
    return value


def _signed_int_env(name: str, default: int) -> int:
    """Like :func:`_int_env`, but 0 and -1 are meaningful values, not rejects.

    The thinking budget needs all three of "off" (0), "dynamic" (-1) and "at
    most N", so it cannot use the positive-only parser the rate limits do.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return _reject(name, raw, default, "not a whole number")
    if value < -1:
        return _reject(name, raw, default, "must be -1 (dynamic), 0 (off) or a ceiling")
    return value


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
    #: Gemini thinking budget in tokens: 0 off, -1 dynamic, >0 a ceiling.
    #: See DEFAULT_THINKING_BUDGET -- this is the main wall-clock lever.
    thinking_budget: int = DEFAULT_THINKING_BUDGET

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
    # `real()` erases a value that is still `.env.example` text, so the
    # emptiness checks on `Settings` -- and the `NotConfigured` they raise --
    # fire on a copied-but-unedited `.env` instead of letting the run proceed
    # to an SSL handshake against the URL-encoded placeholder. See bridge/startup.
    return Settings(
        grafana_url=real(os.environ.get("GRAFANA_URL")).rstrip("/"),
        grafana_token=real(os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")),
        gcp_project=real(os.environ.get("GOOGLE_CLOUD_PROJECT")),
        gcp_location=real(os.environ.get("GOOGLE_CLOUD_LOCATION")) or "us-central1",
        mcp_grafana_bin=_find_mcp_grafana(),
        mcp_mode="hosted" if mode == "hosted" else "oss",
        grafana_cloud_mcp_token=real(os.environ.get("GRAFANA_CLOUD_MCP_TOKEN"))
        or _read_token_file(),
        ds_prom=real(os.environ.get("GRAFANA_DS_PROM_UID")) or "grafanacloud-prom",
        ds_loki=real(os.environ.get("GRAFANA_DS_LOKI_UID")) or "grafanacloud-logs",
        ds_tempo=real(os.environ.get("GRAFANA_DS_TEMPO_UID")) or "grafanacloud-traces",
        analyst_model=real(os.environ.get("TURNAROUND_GEMINI_MODEL"))
        or DEFAULT_ANALYST_MODEL,
        max_llm_calls=_int_env("TURNAROUND_MAX_LLM_CALLS", DEFAULT_MAX_LLM_CALLS),
        asks_per_hour=_int_env("TURNAROUND_ASKS_PER_HOUR", DEFAULT_ASKS_PER_HOUR),
        asks_per_day=_int_env("TURNAROUND_ASKS_PER_DAY", DEFAULT_ASKS_PER_DAY),
        concurrent_asks=_int_env("TURNAROUND_CONCURRENT_ASKS", DEFAULT_CONCURRENT_ASKS),
        thinking_budget=_signed_int_env("TURNAROUND_THINKING_BUDGET",
                                        DEFAULT_THINKING_BUDGET),
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
