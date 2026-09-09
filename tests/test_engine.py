"""The one shared run path, and the two front ends that present it.

``agent/engine.py`` builds the system, drives the runner under the circuit
breaker and scores the answer. ``agent/run.py`` turns that into console text and
an exit code; ``agent/serve.py`` turns the same thing into JSON. These tests
pin the contract between them, so the CLI and the endpoint cannot drift apart
the way two copies of the pipeline would.

Nothing here touches Vertex, Grafana or the network: the ADK ``Runner`` is
replaced with a stub that yields a scripted final event.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent import engine
from agent import run as run_mod
from agent import serve as serve_mod
from agent.approval import AutoApprover
from agent.config import Settings

ANSWER = ("Answer: SEQ0420 is slipping. The join shows 4.5 core-hours per comp "
          "iteration against 2.2 elsewhere, and 40 core-h of render waste, from "
          "the lighting cache regression failing frame 118.")


def _cfg(**over) -> Settings:
    base = {
        "grafana_url": "https://stack.grafana.net", "grafana_token": "glsa_x",
        "gcp_project": "proj", "gcp_location": "us-central1",
        "mcp_grafana_bin": "/opt/homebrew/bin/mcp-grafana",
    }
    return Settings(**{**base, **over})


class _Part:
    def __init__(self, text): self.text = text


class _Content:
    def __init__(self, text): self.parts = [_Part(text)]


class _FinalEvent:
    def __init__(self, text):
        self.content = _Content(text)

    def is_final_response(self): return True


def _runner_yielding(text: str):
    """A stand-in ADK Runner that emits one final response and nothing else."""

    class _Runner:
        def __init__(self, **_kw):
            pass

        async def run_async(self, **_kw):
            yield _FinalEvent(text)

    return _Runner


def _runner_raising(exc: Exception):
    class _Runner:
        def __init__(self, **_kw):
            pass

        async def run_async(self, **_kw):
            if False:  # pragma: no cover -- makes this an async generator
                yield
            raise exc

    return _Runner


@pytest.fixture
def offline(monkeypatch):
    """No credentials, no network: settings are fixed and the runner is a stub."""
    monkeypatch.setattr(engine, "resolved_settings", _cfg)
    monkeypatch.setattr(engine, "Runner", _runner_yielding(ANSWER))
    return monkeypatch


# --------------------------------------------------------------------------- #
# engine.answer_question
# --------------------------------------------------------------------------- #


def test_a_run_returns_the_answer_the_system_and_the_settings(offline):
    outcome = asyncio.run(engine.answer_question(
        "why is SEQ0420 slipping?", approver=AutoApprover(approve=False),
        conversation_id="t", observability=False, evaluate=False))

    assert outcome.answer.startswith("Answer: SEQ0420")
    assert outcome.circuit_breaker_tripped is False
    assert outcome.scorecard is None            # evaluate=False
    assert outcome.response_id is None          # observability=False
    assert outcome.settings.grafana_url == "https://stack.grafana.net"
    assert outcome.system.producer.sub_agents[0].name == "schedule_analyst"


def test_the_answer_is_stripped_once_here_not_in_each_front_end(offline):
    offline.setattr(engine, "Runner", _runner_yielding("\n\n  padded  \n"))
    outcome = asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=False))
    assert outcome.answer == "padded"


def test_the_deterministic_judge_runs_without_an_llm_or_telemetry(offline):
    """evaluate=True with no Vertex still scores: the deterministic tier needs
    no model, so a reviewer running offline still sees a scorecard."""
    offline.setattr(engine, "judge_generator", lambda *_a, **_k: None)
    outcome = asyncio.run(engine.answer_question(
        "why is SEQ0420 slipping?", approver=AutoApprover(approve=False),
        conversation_id="t", observability=False, evaluate=True))

    assert outcome.scorecard is not None
    names = {r.name for r in outcome.scorecard.results}
    assert names == {"grounding_numbers", "mechanism_named", "privacy_floor_respected"}
    assert not outcome.scorecard.failed


def test_an_empty_answer_is_not_scored(offline):
    """Scoring nothing would emit three misleading 'fail' events per run."""
    offline.setattr(engine, "Runner", _runner_yielding("   "))
    outcome = asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=True))
    assert outcome.answer == ""
    assert outcome.scorecard is None


def test_the_circuit_breaker_is_an_outcome_field_not_an_exception(offline):
    """A tripped breaker must not become a traceback: both front ends need to
    report it alongside whatever partial state exists."""
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    offline.setattr(engine, "Runner",
                    _runner_raising(LlmCallsLimitExceededError("Max number of llm calls `1` exceeded")))
    outcome = asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=False))

    assert outcome.circuit_breaker_tripped is True
    assert outcome.halt.kind == "circuit_breaker"
    assert "exceeded" in outcome.halt.detail
    assert "TURNAROUND_MAX_LLM_CALLS" in outcome.halt.advice
    assert outcome.answer == ""


def test_the_ceiling_passed_to_adk_is_the_resolved_one(offline):
    seen = {}

    class _Runner:
        def __init__(self, **_kw):
            pass

        async def run_async(self, **kw):
            seen["ceiling"] = kw["run_config"].max_llm_calls
            yield _FinalEvent(ANSWER)

    offline.setattr(engine, "Runner", _Runner)
    asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        settings=_cfg(max_llm_calls=3), observability=False, evaluate=False))
    assert seen["ceiling"] == 3


def test_resolved_settings_names_every_missing_key(monkeypatch):
    monkeypatch.setattr(engine, "bootstrap_vertex",
                        lambda: _cfg(gcp_project="", grafana_url=""))
    with pytest.raises(engine.NotConfigured) as exc:
        engine.resolved_settings()
    message = str(exc.value)
    assert "GOOGLE_CLOUD_PROJECT" in message
    assert "GRAFANA_URL" in message


def test_optional_tiers_degrade_to_none_rather_than_failing_a_run(monkeypatch, capsys):
    """Telemetry and the LLM judge are both best-effort: an unreachable OTLP
    gateway or a missing Vertex credential must not lose the answer."""
    assert engine.instrumentation(enabled=False) is None
    assert engine.judge_generator(False, "gemini-2.5-flash") is None

    import agent.evaluation as ev
    monkeypatch.setattr(ev, "vertex_generator",
                        lambda _m: (_ for _ in ()).throw(RuntimeError("no ADC")))
    assert engine.judge_generator(True, "gemini-2.5-flash") is None
    assert "LLM judge off" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The CLI front end
# --------------------------------------------------------------------------- #


def test_cli_exits_zero_and_prints_the_answer_timeline_and_scorecard(offline, capsys):
    rc = asyncio.run(run_mod.ask("why is SEQ0420 slipping?", approve=False,
                                 interactive=False, observability=False, evaluate=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Answer: SEQ0420" in out
    assert "(no tool calls)" in out          # the timeline block rendered
    assert "privacy_floor_respected" in out  # the scorecard block rendered


def test_cli_exits_3_only_when_the_breaker_leaves_nothing_to_show(offline, capsys):
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    offline.setattr(engine, "Runner", _runner_raising(LlmCallsLimitExceededError("boom")))
    rc = asyncio.run(run_mod.ask("q", approve=False, interactive=False,
                                 observability=False, evaluate=False))
    assert rc == 3
    assert "circuit breaker tripped" in capsys.readouterr().err


def test_cli_still_shows_a_partial_answer_when_the_breaker_trips(offline, capsys):
    """A run that hit the ceiling after synthesising something is worth showing;
    the note goes to stderr and the exit stays 0."""
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    class _Runner:
        def __init__(self, **_kw):
            pass

        async def run_async(self, **_kw):
            yield _FinalEvent(ANSWER)
            raise LlmCallsLimitExceededError("boom")

    offline.setattr(engine, "Runner", _Runner)
    rc = asyncio.run(run_mod.ask("q", approve=False, interactive=False,
                                 observability=False, evaluate=False))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Answer: SEQ0420" in captured.out
    assert "circuit breaker tripped" in captured.err


def test_cli_reports_a_hosted_auth_failure_as_exit_2_not_a_traceback(offline, capsys):
    from agent.mcp_grafana import HostedMcpNotAuthorized

    def _boom(**_kw):
        raise HostedMcpNotAuthorized("re-authorize: run `uv run python -m agent.mcp_login`")

    offline.setattr(engine, "build_system", _boom)
    rc = asyncio.run(run_mod.ask("q", approve=False, interactive=False,
                                 observability=False, evaluate=False))
    assert rc == 2
    assert "mcp_login" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The HTTP front end
# --------------------------------------------------------------------------- #


def test_ask_returns_the_documented_json_shape(offline):
    body = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?", "observability": False}).json()

    assert set(body) == {"answer", "timeline", "evaluation", "response_id",
                         "circuit_breaker_tripped", "halted_by", "halt_detail",
                         "grafana_url"}
    assert body["halted_by"] is None
    assert body["answer"].startswith("Answer: SEQ0420")
    assert body["circuit_breaker_tripped"] is False
    assert {e["name"] for e in body["evaluation"]} == {
        "grounding_numbers", "mechanism_named", "privacy_floor_respected"}
    assert all({"name", "score", "label", "actor_type", "explanation"} == set(e)
               for e in body["evaluation"])


def test_ask_reports_a_tripped_breaker_in_the_body_not_as_a_500(offline):
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    offline.setattr(engine, "Runner", _runner_raising(LlmCallsLimitExceededError("boom")))
    response = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?", "observability": False,
              "evaluate": False})

    assert response.status_code == 200
    assert response.json()["circuit_breaker_tripped"] is True


def test_ask_refuses_an_unconfigured_server_with_a_named_error(monkeypatch):
    monkeypatch.setattr(engine, "bootstrap_vertex", lambda: _cfg(gcp_project=""))
    body = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?"}).json()
    assert "GOOGLE_CLOUD_PROJECT" in body["error"]
    assert "answer" not in body


@pytest.mark.parametrize("question", ["", "hi", "x" * 2001])
def test_ask_validates_the_question_before_spending_a_token(question):
    """Length bounds are enforced by the request model, so a junk POST costs
    nothing: no build_system, no Gemini call."""
    response = TestClient(serve_mod.app).post("/ask", json={"question": question})
    assert response.status_code == 422


def test_healthz_is_a_pure_read_and_publishes_the_resolved_ceiling(monkeypatch):
    monkeypatch.setattr(serve_mod, "load_settings", lambda: _cfg(max_llm_calls=11))
    body = TestClient(serve_mod.app).get("/healthz").json()
    assert body == {"status": "ok", "vertex_ready": True, "grafana_ready": True,
                    "mcp_mode": "oss", "max_llm_calls": 11}


def test_the_endpoint_can_never_approve_a_write(offline):
    """A public URL must not be able to mutate Kitsu or Grafana. Asserted on the
    built system, not by grepping the source."""
    captured = {}

    def _capture(**kwargs):
        captured["approver"] = kwargs["approver"]
        from agent.producer import build_system
        return build_system(**kwargs)

    offline.setattr(engine, "build_system", _capture)
    TestClient(serve_mod.app).post(
        "/ask", json={"question": "change the SEQ0420 dates", "observability": False,
                      "evaluate": False})

    approver = captured["approver"]
    assert isinstance(approver, AutoApprover)
    assert approver.approve is False
    decision = approver.decide(None)
    assert decision.approved is False


# --------------------------------------------------------------------------- #
# Vertex-side failures. Observed live: four questions back to back exhausted the
# project's per-minute Gemini quota and the third exited 1 with a forty-line ADK
# traceback. On a small project this is the likeliest live failure there is.
# --------------------------------------------------------------------------- #


def _api_error(code: int, message: str):
    from google.genai.errors import APIError

    return APIError(code, {"error": {"code": code, "message": message,
                                     "status": "RESOURCE_EXHAUSTED"}})


def test_a_vertex_quota_refusal_halts_cleanly_and_says_what_to_do(offline):
    offline.setattr(engine, "Runner", _runner_raising(_api_error(429, "Resource exhausted.")))
    outcome = asyncio.run(engine.answer_question(
        "why is SEQ0420 slipping?", approver=AutoApprover(approve=False),
        conversation_id="t", observability=False, evaluate=False))

    assert outcome.halt is not None
    assert outcome.halt.kind == "model_quota"
    assert "quota exhausted" in outcome.halt.detail
    assert "gemini-2.5-flash" in outcome.halt.detail   # names the model that ran out
    assert "TURNAROUND_MAX_LLM_CALLS" in outcome.halt.advice
    # a provider stop is not our cost guard doing its job
    assert outcome.circuit_breaker_tripped is False


def test_any_other_vertex_error_still_halts_rather_than_raising(offline):
    offline.setattr(engine, "Runner", _runner_raising(_api_error(503, "backend unavailable")))
    outcome = asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=False))

    assert outcome.halt.kind == "model_error"
    assert "503" in outcome.halt.detail
    assert ".env" in outcome.halt.advice


def test_the_cli_prints_a_quota_refusal_as_one_sentence_not_a_traceback(offline, capsys):
    offline.setattr(engine, "Runner", _runner_raising(_api_error(429, "Resource exhausted.")))
    rc = asyncio.run(run_mod.ask("q", approve=False, interactive=False,
                                 observability=False, evaluate=False))
    err = capsys.readouterr().err
    assert rc == 3
    assert "quota exhausted" in err
    assert "Traceback" not in err
    assert "google.adk" not in err


def test_the_endpoint_names_the_halt_instead_of_returning_a_blank_answer(offline):
    """A 200 with an empty ``answer`` and no reason is indistinguishable from
    'the agent had nothing to say'."""
    offline.setattr(engine, "Runner", _runner_raising(_api_error(429, "Resource exhausted.")))
    body = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?", "observability": False,
                      "evaluate": False}).json()

    assert body["answer"] == ""
    assert body["halted_by"] == "model_quota"
    assert "quota exhausted" in body["halt_detail"]
    assert body["circuit_breaker_tripped"] is False
