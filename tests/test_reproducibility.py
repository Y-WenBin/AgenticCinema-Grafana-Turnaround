"""Reproducibility + hardening suite (see tests/TESTPLAN.md).

The invariants that let anyone re-run Turnaround and reach the same
conclusions: a deterministic seed, an absolute privacy floor, the permitted
model only, a bounded per-run token budget, a real MCP call path, and a
credential-safe deploy context. Grouped the way the system is: Grafana MCP
integration, agent orchestration, data pipeline, deployment readiness.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run(coro):
    return asyncio.run(coro)


def _cfg(**over):
    from agent.config import Settings

    base = {
        "grafana_url": "https://stack.grafana.net", "grafana_token": "glsa_x",
        "gcp_project": "proj", "gcp_location": "us-central1",
        "mcp_grafana_bin": "/opt/homebrew/bin/mcp-grafana",
    }
    return Settings(**{**base, **over})


# --------------------------------------------------------------------------- #
# Part 1 — reproducibility invariants
# --------------------------------------------------------------------------- #


def test_seed_is_deterministic():
    """R1: same show definition -> identical metric points, run to run."""
    from bridge.metrics import MetricBackfill
    from bridge.testing import CollectingMetricExporter
    from seed.model import ShowSimulation
    from seed.populate import build_metrics

    now = datetime(2026, 9, 6, tzinfo=UTC)

    def snapshot():
        history = ShowSimulation.load(now=now).run()
        cap = CollectingMetricExporter()
        bf = MetricBackfill(exporter=cap)
        build_metrics(history, bf)
        bf.flush()
        rows = []
        for metric in cap.metrics:
            for p in metric.data.data_points:
                rows.append((metric.name, tuple(sorted(p.attributes.items())), p.value))
        return sorted(rows)

    assert snapshot() == snapshot()


def test_no_non_google_ai_sdk_on_the_runtime_path():
    """R4: nothing under agent/ observability/ bridge/ imports a non-Google AI SDK."""
    banned = {
        "openai", "anthropic", "cohere", "litellm", "langchain", "langchain_core",
        "llama_index", "mistralai", "groq", "transformers", "vllm", "ollama",
    }
    offenders: list[str] = []
    for pkg in ("agent", "observability", "bridge"):
        for path in (REPO / pkg).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".", 1)[0]
                    if root in banned:
                        offenders.append(f"{path.relative_to(REPO)}: {name}")
    assert not offenders, offenders


def test_circuit_breaker_is_wired(monkeypatch):
    """R7 / orchestration edge: a per-run Gemini-call ceiling is configured and
    actually reaches ADK's RunConfig on the one shared run path."""
    from agent import config, engine
    from agent.approval import AutoApprover

    assert config.DEFAULT_MAX_LLM_CALLS > 0
    assert config._int_env("TURNAROUND_MAX_LLM_CALLS_UNSET_XYZ", 40) == 40
    assert config._int_env("PATH", 40) == 40  # unparseable -> default

    # The ceiling the settings resolve to is the ceiling ADK is given -- asserted
    # by capturing the RunConfig the runner is actually driven with, not by
    # grepping the source.
    seen: dict[str, object] = {}

    class _Runner:
        def __init__(self, **_kw):
            pass

        async def run_async(self, **kw):
            seen["max_llm_calls"] = kw["run_config"].max_llm_calls
            if False:  # pragma: no cover -- makes this an async generator
                yield

    monkeypatch.setattr(engine, "Runner", _Runner)
    outcome = _run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        settings=_cfg(max_llm_calls=7), observability=False, evaluate=False))

    assert seen["max_llm_calls"] == 7
    assert outcome.circuit_breaker_tripped is False
    # and the breaker is caught, not raised, on the single shared path
    assert "LlmCallsLimitExceededError" in (REPO / "agent" / "engine.py").read_text()


def test_circuit_breaker_trips_cleanly(monkeypatch, capsys):
    """The limit surfaces as a non-zero exit + a note, not a traceback."""
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    from agent import engine
    from agent import run as run_mod

    class _BoomRunner:
        def __init__(self, **_kw):
            pass

        async def run_async(self, **_kw):
            # the yield below is unreachable, but it makes this an async generator
            if False:  # pragma: no cover
                yield
            raise LlmCallsLimitExceededError("Max number of llm calls `1` exceeded")

    monkeypatch.setattr(engine, "resolved_settings", _cfg)
    monkeypatch.setattr(engine, "Runner", _BoomRunner)

    rc = _run(run_mod.ask("why is SEQ0420 slipping?", approve=False, interactive=False,
                          observability=False, evaluate=False))
    assert rc == 3
    assert "circuit breaker tripped" in capsys.readouterr().err


def test_privacy_floor_constants_agree():
    """R3: the crew-load floor and the judge's sub-floor pool are one story."""
    from agent.evaluation import SUBFLOOR_POOL
    from bridge.privacy import MIN_POOL_SIZE, may_report_crew_load

    assert MIN_POOL_SIZE == 3
    assert may_report_crew_load(["a", "b", "c"]) is True
    assert may_report_crew_load(["a", "b"]) is False
    assert SUBFLOOR_POOL == "di-pool-1"  # the 2-person pool that must never surface


def test_privacy_floor_is_enforced_on_every_answer_surface():
    """R3: the deterministic judge fails a leak in the answer or the evidence chain."""
    from agent.evaluation import DeterministicJudge

    leak_answer = "SEQ0420 is slipping; di-pool-1 is also stretched."
    r = {x.name: x for x in DeterministicJudge().judge(
        question="q", answer=leak_answer, ledger_chain=[])}
    assert r["privacy_floor_respected"].label == "fail"

    r2 = {x.name: x for x in DeterministicJudge().judge(
        question="q", answer="SEQ0420 is slipping.",
        ledger_chain=["[crunch_guardian] di-pool-1 at 71h"])}
    assert r2["privacy_floor_respected"].label == "fail"


# --------------------------------------------------------------------------- #
# Part 2 — Grafana MCP integration
# --------------------------------------------------------------------------- #


def test_grafana_mcp_is_imported_and_instantiated():
    """Grafana's MCP server is imported and instantiated in the code path a real
    run takes, rather than mocked.

    Worth asserting because a toolset that is only ever faked in tests is a
    toolset nobody notices has stopped being wired up."""
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    from agent.mcp_grafana import SCHEDULE_TOOLS, analyst_toolset, remediator_toolset

    ts = analyst_toolset(_cfg(), SCHEDULE_TOOLS)
    assert isinstance(ts, McpToolset)
    assert type(ts).__module__.startswith("google.adk")
    assert isinstance(ts.connection_params, StdioConnectionParams)
    assert ts.connection_params.server_params.command.endswith("mcp-grafana")
    assert isinstance(remediator_toolset(_cfg()), McpToolset)


def test_mock_mcp_payload_is_classified_and_recorded():
    """The seam where a real MCP response enters the agent's evidence context:
    the before/after tool callbacks record the call, keep the query, and read
    success/failure off the payload shape."""
    from agent.analysts import timeline_callbacks
    from agent.timeline import ToolTimeline

    tl = ToolTimeline()
    before, after = timeline_callbacks(tl)
    tool = SimpleNamespace(name="query_prometheus")
    ctx = SimpleNamespace(agent_name="farm_analyst")

    before(tool, {"expr": "sum by (sequence) (turnaround_render_waste_core_hours_total)"}, ctx)
    after(tool, {"expr": "sum(...)"}, ctx,
          {"data": {"result": [{"metric": {"sequence": "SEQ0420"}, "value": [0, "44.1"]}]}})

    assert len(tl.calls) == 1
    call = tl.calls[0]
    assert call.is_grafana_mcp is True
    assert "turnaround_render_waste_core_hours_total" in call.args["expr"]
    assert call.ok is True

    before(tool, {"expr": "bad"}, ctx)
    after(tool, {"expr": "bad"}, ctx, {"isError": True, "content": "parse error"})
    assert tl.calls[1].ok is False


def test_auth_failure_is_handled_gracefully(monkeypatch):
    """Edge: hosted mode with no/expired OAuth bearer -> a re-authorize pointer,
    not a crash, at both the toolset seam and the CLI entrypoint."""
    from agent import engine
    from agent import run as run_mod
    from agent.mcp_grafana import (
        SCHEDULE_TOOLS,
        HostedMcpNotAuthorized,
        analyst_toolset,
    )

    with pytest.raises(HostedMcpNotAuthorized) as exc:
        analyst_toolset(_cfg(mcp_mode="hosted", grafana_cloud_mcp_token=""), SCHEDULE_TOOLS)
    assert "agent.mcp_login" in str(exc.value)

    def _boom(**_kw):
        raise HostedMcpNotAuthorized("re-authorize: run `uv run python -m agent.mcp_login`")

    monkeypatch.setattr(engine, "resolved_settings", _cfg)
    monkeypatch.setattr(engine, "build_system", _boom)
    rc = _run(run_mod.ask("q", approve=False, interactive=False,
                          observability=False, evaluate=False))
    assert rc == 2


# --------------------------------------------------------------------------- #
# Part 3 — Gemini agent multi-step orchestration
# --------------------------------------------------------------------------- #


def test_synthesis_prompt_only_consumes_specialist_state():
    """Deterministic format step: synthesis reads the four specialist state keys,
    calls no tools, and is told to ground every figure in those blocks."""
    from agent.producer import SYNTHESIS_ROLE

    for key in ("{schedule_findings}", "{farm_findings}", "{crunch_findings}",
                "{remediation_result}"):
        assert key in SYNTHESIS_ROLE
    assert "Do NOT call any tools" in SYNTHESIS_ROLE
    # the tightened grounding rule (step 2 of the roadmap)
    assert "Every figure you state must appear" in SYNTHESIS_ROLE
    assert "not measured this run" in SYNTHESIS_ROLE


def test_prompts_never_point_at_an_unfilterable_tool():
    """Edge (tool hallucination): a model is never *instructed* toward a tool
    outside its filter -- ADK would abort the run. Every mcp-grafana tool name
    that appears in an analyst's role prompt is in that analyst's tool_filter."""
    from agent.analysts import CRUNCH_ROLE, FARM_ROLE, SCHEDULE_ROLE
    from agent.mcp_grafana import CRUNCH_TOOLS, FARM_TOOLS, SCHEDULE_TOOLS
    from agent.timeline import _MCP_GRAFANA_TOOLS

    for role, tools in ((SCHEDULE_ROLE, SCHEDULE_TOOLS),
                        (FARM_ROLE, FARM_TOOLS),
                        (CRUNCH_ROLE, CRUNCH_TOOLS)):
        mentioned = {name for name in _MCP_GRAFANA_TOOLS if name in role}
        assert mentioned <= set(tools), sorted(mentioned - set(tools))


def test_pipeline_runs_each_step_exactly_once():
    """Edge (infinite loop): neither shell agent can loop. Three ordered stages,
    the first of which fans three analysts out once each, then it terminates.

    Parallelising the analysts changed the tree's depth, not this property: a
    ParallelAgent runs each sub-agent exactly once too, and no agent appears
    twice anywhere in the tree.
    """
    from google.adk.agents import ParallelAgent, SequentialAgent

    from agent.approval import AutoApprover
    from agent.producer import build_system

    system = build_system(approver=AutoApprover(approve=False), settings=_cfg())
    assert isinstance(system.producer, SequentialAgent)
    stages = [a.name for a in system.producer.sub_agents]
    assert stages == ["analysts", "remediator", "synthesis"]

    analysts = system.producer.sub_agents[0]
    assert isinstance(analysts, ParallelAgent)
    assert [a.name for a in analysts.sub_agents] == [
        "schedule_analyst", "farm_analyst", "crunch_guardian"]

    every = [a.name for a in analysts.sub_agents] + stages[1:]
    assert len(every) == len(set(every))


# --------------------------------------------------------------------------- #
# Part 4 — media context / data pipeline
# --------------------------------------------------------------------------- #


def test_job_name_parses_to_domain_fields():
    """Domain relevance: the 'media input' (an OpenCue job name) yields the
    domain output (shot / sequence / department); a non-canonical name is
    rejected, never guessed."""
    from bridge.ontology import Department, parse_opencue_job_name

    job = parse_opencue_job_name("nightfall-SEQ0420_SH0100-a7f3c2d1_comp_v006")
    assert job is not None
    assert (job.shot_id, job.sequence, job.department) == (
        "SEQ0420_SH0100", "SEQ0420", Department.COMP)
    assert parse_opencue_job_name("farm-maintenance-cleanup") is None


def test_oversized_tool_payload_is_capped_not_dumped():
    """Edge (context window): a huge MCP payload is length-capped with a marker
    in the evidence path, not dumped and not truncated mid-structure."""
    from agent.timeline import ToolTimeline

    tl = ToolTimeline()
    big_args = {"expr": "x" * 20_000, "labels": {f"k{i}": i for i in range(2_000)}}
    call = tl.begin(agent="farm_analyst", tool="query_loki_logs", args=big_args)
    tl.finish(call, result="line\n" * 10_000, ok=True)

    d = call.to_dict()
    assert len(d["args"]["expr"]) <= 160
    assert len(d["result"]) <= 161
    assert d["result"].endswith("…") or len(d["result"]) < 161


# --------------------------------------------------------------------------- #
# Part 5 — repository + deployment readiness
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("over", [
    {"grafana_url": "", "grafana_token": "", "gcp_project": ""},   # nothing
    {"gcp_project": ""},                                           # vertex only missing
    {"grafana_url": "", "grafana_token": ""},                      # grafana only missing
])
def test_startup_refuses_incomplete_config(monkeypatch, over):
    """Startup validation: an incomplete environment is a clean exit 2 naming the
    missing keys, not a later opaque failure inside a tool call."""
    from agent import engine
    from agent import run as run_mod

    monkeypatch.setattr(engine, "bootstrap_vertex", lambda: _cfg(**over))
    with pytest.raises(engine.NotConfigured) as exc:
        engine.resolved_settings()
    assert ".env" in str(exc.value)

    rc = _run(run_mod.ask("q", approve=False, interactive=False,
                          observability=False, evaluate=False))
    assert rc == 2


def test_the_repo_is_usable_by_someone_who_just_cloned_it():
    """An OSS licence and init instructions that actually run.

    The failure this guards against is quiet: a README whose quickstart drifted
    away from the commands that work, found by the one person least equipped to
    debug it -- someone seeing the project for the first time."""
    licence = (REPO / "LICENSE").read_text()
    assert "Apache License" in licence and "Version 2.0" in licence

    pyproject = (REPO / "pyproject.toml").read_text()
    assert 'license = { file = "LICENSE" }' in pyproject

    readme = (REPO / "README.md").read_text()
    assert "uv sync" in readme and "uv run pytest" in readme

    deploy_doc = (REPO / "deploy" / "README.md").read_text()
    assert "gcloud run deploy" in deploy_doc


def test_deploy_context_excludes_secrets():
    """Both ignore files keep .env and .secrets/ out of the build context/image."""
    for fname in (".gcloudignore", ".dockerignore"):
        body = (REPO / fname).read_text()
        assert ".env" in body
        assert ".secrets" in body


def test_deploy_builds_the_dockerfile_not_a_buildpack():
    """The image must come from ./Dockerfile, and the Dockerfile must be at the root.

    `gcloud run deploy --source .` / `gcloud builds submit` build a Dockerfile
    only when one sits in the *source root*. With the file anywhere else the
    build silently falls back to Google Cloud buildpacks and ships an image with
    no `mcp-grafana` binary and the wrong entrypoint -- a deploy that succeeds
    and a service that fails at the first tool call.
    """
    dockerfile = REPO / "Dockerfile"
    assert dockerfile.is_file(), "Dockerfile must live at the repo root"
    assert not (REPO / "deploy" / "Dockerfile").exists(), "one Dockerfile, at the root"

    body = dockerfile.read_text()
    assert "mcp-grafana" in body and "sha256sum -c" in body
    assert "agent.serve" in body

    script = (REPO / "deploy" / "deploy.sh").read_text()
    assert "gcloud builds submit --tag" in script
    assert "--source ." not in script, "an implicit source build would pick buildpacks"
    assert script.count("--image \"$IMAGE\"") == 2, "service and job share one image"


class TestTheImageIsReproducibleAndRunsOnThisMachine:
    """Two build-time failures that used to be invisible until runtime.

    The tarball was hardcoded to ``Linux_x86_64``, so a ``docker build`` on
    Apple Silicon produced an arm64 image carrying an amd64 binary -- the agent
    tier came up and died on the first tool call with "exec format error". The
    one step that would have caught it ended ``|| true`` and so could not fail.
    """

    @staticmethod
    def _dockerfile() -> str:
        return (REPO / "Dockerfile").read_text()

    def test_the_binary_follows_the_target_architecture(self):
        body = self._dockerfile()
        assert "ARG TARGETARCH" in body
        assert "Linux_${slug}" in body, "the tarball must follow TARGETARCH"
        # ...and each architecture needs its own checksum, or the pin is a lie
        assert "MCP_GRAFANA_SHA256_AMD64" in body
        assert "MCP_GRAFANA_SHA256_ARM64" in body

    def test_the_verification_step_is_allowed_to_fail(self):
        """``|| true`` on the check that proves the binary runs made the whole
        step decorative. Nothing in this file may end that way."""
        for line in self._dockerfile().splitlines():
            assert not line.rstrip().endswith("|| true"), line

    def test_the_base_image_is_pinned_by_digest(self):
        """``python:3.12-slim`` is rebuilt weekly. A floating tag means the
        image is not reproducible and a base change arrives in a deploy nobody
        made -- in a file that otherwise pins the agent binary to a SHA256."""
        stages: set[str] = set()
        external = 0
        for line in self._dockerfile().splitlines():
            if not line.startswith("FROM "):
                continue
            parts = line.split()
            image = parts[1]
            if image not in stages:  # a reference to an earlier stage needs no digest
                external += 1
                assert "@sha256:" in image, f"unpinned base: {line}"
            if len(parts) >= 4 and parts[2] == "AS":
                stages.add(parts[3])
        assert external, "no external base image found -- has the file changed shape?"

    def test_curl_does_not_reach_the_runtime_image(self):
        """It is installed to fetch the binary; a separate stage keeps it out of
        what ships."""
        body = self._dockerfile()
        assert "install -y --no-install-recommends curl" in body
        stages = body.split("\nFROM ")
        runtime = stages[-1]
        assert "curl" not in runtime, "curl leaked into the final stage"


def test_the_playground_ships_in_the_image():
    """`web/` was excluded from both the image and the build upload while it was
    an empty placeholder. It is now what the service serves at `/`, so an
    exclusion here would deploy a landing page that 404s its own banner --
    and only in production, since a local run reads the file straight off disk.
    """
    for name in (".dockerignore", ".gcloudignore"):
        lines = [ln.strip() for ln in (REPO / name).read_text().splitlines()]
        assert not any(ln.rstrip("/") == "web" for ln in lines), \
            f"{name} excludes web/, so the deployed page would lose its assets"

    web = REPO / "web"
    for asset in ("index.html", "app.js", "favicon.svg"):
        assert (web / asset).is_file(), f"web/{asset} is referenced by the page"


def test_the_public_endpoint_is_capped_in_three_directions():
    """Open to everyone and unbounded are different things: every /ask spends
    real Gemini calls on a real bill."""
    limits = (REPO / "agent" / "limits.py").read_text()
    for reason in ("daily_budget", "per_visitor", "busy"):
        assert reason in limits

    serve = (REPO / "agent" / "serve.py").read_text()
    assert "gate.admit(" in serve, "/ask must pass through admission control"
    assert "finally:" in serve and "gate.release()" in serve, \
        "the concurrency slot must be returned on the error path too"

    config = (REPO / "agent" / "config.py").read_text()
    for knob in ("TURNAROUND_ASKS_PER_HOUR", "TURNAROUND_ASKS_PER_DAY",
                 "TURNAROUND_CONCURRENT_ASKS"):
        assert knob in config, f"{knob} must be tunable without a code change"


def test_deploy_keeps_the_demo_data_alive():
    """A hosted demo needs the re-seed job; the seed ages out in about an hour."""
    script = (REPO / "deploy" / "deploy.sh").read_text()
    assert "gcloud run jobs deploy" in script
    assert "seed.refresh" in script, "the job must override the entrypoint"
    assert "gcloud scheduler jobs" in script
    assert "cloudscheduler.googleapis.com" in script, "the schedule needs its API enabled"


def test_http_endpoint_cannot_approve_writes():
    """A public URL must not be able to mutate Kitsu or Grafana."""
    serve_src = (REPO / "agent" / "serve.py").read_text()
    assert "AutoApprover(approve=False)" in serve_src
    assert "approve=True" not in serve_src


def test_healthz_reports_readiness_keys():
    from fastapi.testclient import TestClient

    from agent.serve import app

    body = TestClient(app).get("/healthz").json()
    assert set(body) >= {"status", "vertex_ready", "grafana_ready", "mcp_mode", "max_llm_calls"}
    assert isinstance(body["vertex_ready"], bool)
