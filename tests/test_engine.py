"""The one shared run path, and the front end that presents it.

``agent/engine.py`` builds the system, drives the runner under the circuit
breaker and scores the answer; ``agent/run.py`` turns that into console text and
an exit code. These tests pin the contract between them.

The split was made when there were two front ends and the HTTP one was ~70% a
copy of the CLI. That service now lives in its own repository, and these tests
are what let it move: they constrain what ``answer_question`` returns, not who
is asking.

Nothing here touches Vertex, Grafana or the network: the ADK ``Runner`` is
replaced with a stub that yields a scripted final event.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from google.adk.agents.invocation_context import LlmCallsLimitExceededError

from agent import engine
from agent import run as run_mod
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


class _StubRunner:
    """Base for the ADK Runner stand-ins.

    Counts `close()` because that is the call that terminates the `mcp-grafana`
    subprocesses -- a stub without it would let the leak back in silently.
    """

    closes = 0

    def __init__(self, **_kw):
        pass

    async def close(self):
        type(self).closes += 1


def _runner_yielding(text: str):
    """A stand-in ADK Runner that emits one final response and nothing else."""

    class _Runner(_StubRunner):
        closes = 0

        async def run_async(self, **_kw):
            yield _FinalEvent(text)

    return _Runner


def _runner_raising(exc: Exception):
    class _Runner(_StubRunner):
        closes = 0

        async def run_async(self, **_kw):
            if False:  # pragma: no cover -- makes this an async generator
                yield
            raise exc

    return _Runner


def _counting(log: list, inner):
    """Wrap answer_question so a test can prove it was *not* reached."""

    async def wrapper(*args, **kwargs):
        log.append(kwargs.get("question") or args[0])
        return await inner(*args, **kwargs)

    return wrapper


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
    assert [a.name for a in outcome.system.producer.sub_agents] == [
        "analysts", "remediator", "synthesis"]


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
    assert names == {"grounding_numbers", "mechanism_named",
                     "privacy_floor_respected", "figures_are_readable"}
    assert not outcome.scorecard.failed


def test_an_empty_answer_is_not_scored(offline):
    """Scoring nothing would emit a full card of misleading 'fail' events per run."""
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

    class _Runner(_StubRunner):
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

    import agent.evaluation as ev
    monkeypatch.setattr(ev, "vertex_generator",
                        lambda _m: (_ for _ in ()).throw(RuntimeError("no ADC")))
    assert engine.judge_generator("gemini-2.5-flash") is None
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

    class _Runner(_StubRunner):
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
# Vertex-side failures. Observed live: four questions back to back exhausted the
# project's per-minute Gemini quota and the third exited 1 with a forty-line ADK
# traceback. On a small project this is the likeliest live failure there is.
# --------------------------------------------------------------------------- #


def _api_error(code: int, message: str):
    from google.genai.errors import APIError

    return APIError(code, {"error": {"code": code, "message": message,
                                     "status": "RESOURCE_EXHAUSTED"}})


def test_a_quota_refusal_still_halts_cleanly_when_a_task_group_wraps_it(offline):
    """The analysts run inside ADK's ParallelAgent, which uses an
    `asyncio.TaskGroup` -- and a TaskGroup wraps whatever its body raises in an
    ExceptionGroup. `except APIError` does not match `ExceptionGroup(APIError)`,
    so parallelising the analysts silently turned every Vertex 429 from a
    one-sentence halt back into a 500 and a forty-line traceback.

    Found live, in Cloud Run logs, after the change shipped.
    """
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup",
                             [_api_error(429, "Resource exhausted.")])
    offline.setattr(engine, "Runner", _runner_raising(wrapped))

    outcome = asyncio.run(engine.answer_question(
        "why is SEQ0420 slipping?", approver=AutoApprover(approve=False),
        conversation_id="t", settings=_cfg(), observability=False, evaluate=False))

    assert outcome.halt is not None, "the group escaped, so this is a 500 again"
    assert outcome.halt.kind == "model_quota"
    assert "quota exhausted" in outcome.halt.detail


def test_a_wrapped_circuit_breaker_is_still_the_circuit_breaker(offline):
    """The other exception the run path explains, through the same wrapper.
    A tripped breaker reported as a generic model error would send someone
    looking at Vertex for a limit this project set on itself."""
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    wrapped = ExceptionGroup("unhandled errors in a TaskGroup",
                             [LlmCallsLimitExceededError("Max number of llm calls `40` exceeded")])
    offline.setattr(engine, "Runner", _runner_raising(wrapped))

    outcome = asyncio.run(engine.answer_question(
        "why is SEQ0420 slipping?", approver=AutoApprover(approve=False),
        conversation_id="t", settings=_cfg(), observability=False, evaluate=False))

    assert outcome.circuit_breaker_tripped is True
    assert "TURNAROUND_MAX_LLM_CALLS" in outcome.halt.advice


def test_a_nested_group_is_still_unwrapped(offline):
    """TaskGroups nest: the analysts sit inside a ParallelAgent inside the
    runner, so the 429 can arrive one layer deeper than expected."""
    wrapped = ExceptionGroup("outer", [ExceptionGroup("inner", [_api_error(429, "Resource exhausted.")])])
    offline.setattr(engine, "Runner", _runner_raising(wrapped))

    outcome = asyncio.run(engine.answer_question(
        "why is SEQ0420 slipping?", approver=AutoApprover(approve=False),
        conversation_id="t", settings=_cfg(), observability=False, evaluate=False))

    assert outcome.halt is not None and outcome.halt.kind == "model_quota"


def test_an_unexplainable_group_is_re_raised_not_flattened(offline):
    """Unwrapping must not become a catch-all. A bug in an analyst is not a
    quota problem, and reporting it as one would send someone to the Vertex
    console for a KeyError."""
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("a real bug")])
    offline.setattr(engine, "Runner", _runner_raising(wrapped))

    with pytest.raises(ExceptionGroup):
        asyncio.run(engine.answer_question(
            "why is SEQ0420 slipping?", approver=AutoApprover(approve=False),
            conversation_id="t", settings=_cfg(), observability=False, evaluate=False))


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


# --------------------------------------------------------------------------- #
# Teardown: a run must give back the OS resources it took
# --------------------------------------------------------------------------- #


def test_a_completed_run_closes_the_runner(offline):
    """`Runner.close()` is the only thing in ADK that terminates the four
    `mcp-grafana` subprocesses a run spawns -- `run_async` never calls it. The
    HTTP front end builds a fresh system per request, so a missed close is a
    subprocess leak per `/ask`."""
    runner_cls = engine.Runner
    asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=False))
    assert runner_cls.closes == 1


def test_a_halted_run_closes_the_runner_too(offline):
    """The failure path is the one that matters: a Vertex quota refusal that
    leaked its subprocess would bleed capacity fastest, because it is the
    failure most likely to repeat."""
    offline.setattr(engine, "Runner", _runner_raising(LlmCallsLimitExceededError("boom")))
    asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=False))
    assert engine.Runner.closes == 1


def test_an_unexpected_exception_still_closes_the_runner(offline):
    """Teardown is a `finally`, not a happy-path step: an error the engine does
    not model still has to give the subprocesses back before it propagates."""
    offline.setattr(engine, "Runner", _runner_raising(RuntimeError("unmodelled")))
    with pytest.raises(RuntimeError):
        asyncio.run(engine.answer_question(
            "q", approver=AutoApprover(approve=False), conversation_id="t",
            observability=False, evaluate=False))
    assert engine.Runner.closes == 1


def test_a_run_shuts_the_telemetry_down_not_just_flushes_it(offline):
    """Each run builds its own tracer/logger/meter trio, and each carries a
    batch-processor thread. `flush()` empties them; only `shutdown()` stops the
    threads, so flushing alone leaks one thread per signal per request."""
    calls = []

    class _Telemetry:
        @contextlib.contextmanager
        def invoke_agent(self, *_a, **_k):
            yield

    class _Obs:
        plugin = type("P", (), {"last_response_id": None})()
        telemetry = _Telemetry()

        def flush(self, *_a, **_k):
            calls.append("flush")

        def shutdown(self):
            calls.append("shutdown")

    offline.setattr(engine, "instrumentation", lambda _enabled: _Obs())
    asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=True, evaluate=False))
    assert calls[-1] == "shutdown"


def test_a_failing_teardown_does_not_cost_the_answer(offline, capsys):
    """The answer is already produced by the time teardown runs. Losing it to a
    wedged subprocess would be strictly worse than the leak, so `_release`
    reports and swallows."""

    class _Runner(_StubRunner):
        async def run_async(self, **_kw):
            yield _FinalEvent(ANSWER)

        async def close(self):
            raise OSError("the subprocess is already gone")

    offline.setattr(engine, "Runner", _Runner)
    outcome = asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=False))
    assert outcome.answer.startswith("Answer: SEQ0420")
    assert "teardown incomplete" in capsys.readouterr().err


def test_teardown_is_bounded_so_a_wedged_subprocess_cannot_hold_the_response(offline, capsys):
    """ADK bounds each toolset at 10s; four in series is 40s a caller waits
    through after their answer is ready. `RELEASE_TIMEOUT` caps the whole thing."""

    class _Runner(_StubRunner):
        async def run_async(self, **_kw):
            yield _FinalEvent(ANSWER)

        async def close(self):
            await asyncio.sleep(60)

    offline.setattr(engine, "Runner", _Runner)
    offline.setattr(engine, "RELEASE_TIMEOUT", 0.05)
    outcome = asyncio.run(engine.answer_question(
        "q", approver=AutoApprover(approve=False), conversation_id="t",
        observability=False, evaluate=False))
    assert outcome.answer.startswith("Answer: SEQ0420")
    assert "teardown incomplete" in capsys.readouterr().err
