"""One place that asks the Producer a question.

``agent/run.py`` (CLI) and ``agent/serve.py`` (HTTP) are two *presentations* of
the same run: build the system, instrument it, drive the runner under the
circuit breaker, score the answer. That pipeline lives here once, so the two
front ends cannot drift apart -- the CLI only formats text, the endpoint only
shapes JSON, and neither reaches into the other's internals.

    outcome = await answer_question("why is SEQ0420 slipping?",
                                    approver=AutoApprover(approve=False),
                                    conversation_id="cli")

Everything the callers need to report is on :class:`RunOutcome`; nothing else
about the run escapes.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.errors import APIError

from agent.approval import Approver
from agent.config import Settings, bootstrap_vertex
from agent.mcp_grafana import HostedMcpNotAuthorized
from agent.producer import AgentSystem, build_system
from bridge.startup import ConfigError, require

if TYPE_CHECKING:
    from agent.evaluation import Scorecard

APP = "turnaround"


class NotConfigured(ConfigError):
    """Startup validation failed: the message names the missing keys.

    A :class:`~bridge.startup.ConfigError` so that every entry point reports a
    half-filled ``.env`` the same way, whether the missing line stops the
    seeder, the provisioner or the agent.
    """


@dataclass(frozen=True, slots=True)
class Halt:
    """Why a run stopped before the pipeline finished.

    One typed value rather than a boolean per failure mode: a run can stop for
    reasons we control (the circuit breaker) and reasons Vertex controls (quota,
    a model outage), and both must reach the console and the JSON body as a
    sentence, never as a traceback. Adding a fourth reason is a new ``kind``,
    not a new field on every front end.
    """

    kind: str      # circuit_breaker | model_quota | model_error
    detail: str    # what the provider or ADK actually said
    advice: str    # the one line a human should act on

    def render(self) -> str:
        return f"{self.detail}\n({self.advice})"


@dataclass(slots=True)
class RunOutcome:
    """Everything a front end needs to report one run."""

    answer: str
    system: AgentSystem
    settings: Settings
    response_id: str | None = None
    halt: Halt | None = None
    scorecard: Scorecard | None = None

    @property
    def circuit_breaker_tripped(self) -> bool:
        """Kept as its own flag: it is part of the published ``/ask`` contract
        and means something different from a provider-side stop -- this one is
        Turnaround's own cost guard doing its job."""
        return self.halt is not None and self.halt.kind == "circuit_breaker"


# --------------------------------------------------------------------------- #
# Optional tiers -- both degrade to None rather than failing a run
# --------------------------------------------------------------------------- #


def instrumentation(enabled: bool):
    """Best-effort GenAI instrumentation. Returns the Instrumentation or None."""
    if not enabled:
        return None
    try:
        from observability import instrument

        return instrument()
    except Exception as exc:  # noqa: BLE001 -- never let telemetry break a run
        print(f"(observability off: {exc})", file=sys.stderr)
        return None


def judge_generator(model: str,
                    thinking_budget: int = 0) -> Callable[[str], str] | None:
    """A Vertex-backed ``generate`` for the LLM judge, or None if unavailable.

    It used to take an ``enabled`` flag for symmetry with
    :func:`instrumentation`. The symmetry was false: the only caller asks for
    the judge from inside ``if evaluate``, so the flag was a known-true value
    threaded through a branch that could never be taken -- and the test
    covering that branch was covering a caller that did not exist. The one
    thing this returns ``None`` for now is a judge it genuinely cannot build.
    """
    try:
        from agent.evaluation import vertex_generator

        return vertex_generator(model, thinking_budget)
    except Exception as exc:  # noqa: BLE001
        print(f"(LLM judge off: {exc})", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def resolved_settings() -> Settings:
    """Vertex-bootstrapped settings, or :class:`NotConfigured` naming what is missing."""
    cfg = bootstrap_vertex()
    # Named one key at a time rather than as a slash-joined pair: "GRAFANA_URL /
    # GRAFANA_SERVICE_ACCOUNT_TOKEN" left a reader checking a line that was
    # already correct. `Settings` has already erased any value still holding
    # `.env.example` text (`bridge.startup.real`), so a placeholder reaches this
    # check as unset -- which is the whole point.
    try:
        require(
            GOOGLE_CLOUD_PROJECT=cfg.gcp_project,
            GRAFANA_URL=cfg.grafana_url,
            GRAFANA_SERVICE_ACCOUNT_TOKEN=cfg.grafana_token,
        )
    except ConfigError as exc:
        raise NotConfigured(str(exc)) from None
    return cfg


async def answer_question(
    question: str,
    *,
    approver: Approver,
    conversation_id: str,
    settings: Settings | None = None,
    observability: bool = True,
    evaluate: bool = True,
) -> RunOutcome:
    """Run the whole pipeline once and return what happened.

    Raises :class:`NotConfigured` when the environment is incomplete and
    :class:`~agent.mcp_grafana.HostedMcpNotAuthorized` when hosted MCP mode has
    no bearer token -- both are startup problems the caller reports its own way.
    A tripped circuit breaker is *not* an exception here: it is a field on the
    outcome, because a partial answer is still worth showing.
    """
    cfg = settings or resolved_settings()
    system = build_system(approver=approver, settings=cfg)

    obs = instrumentation(observability)
    sessions = InMemorySessionService()
    await sessions.create_session(app_name=APP, user_id="supervisor",
                                  session_id=conversation_id)
    runner = Runner(app_name=APP, agent=system.producer, session_service=sessions,
                    plugins=[obs.plugin] if obs is not None else None)
    try:
        return await _run(question, runner=runner, system=system, obs=obs, cfg=cfg,
                          conversation_id=conversation_id, evaluate=evaluate)
    finally:
        # Every run owns real OS resources: in OSS mode each of the four
        # toolsets is an `mcp-grafana` stdio subprocess, and the telemetry trio
        # carries a batch processor thread per signal. Nothing releases either
        # on its own -- ADK frees toolsets only from `Runner.close()`, which
        # `run_async` never calls. A CLI run hides that (the process exits);
        # `agent/serve.py` builds a fresh system per request, so without this
        # the instance accumulates subprocesses and threads until it dies.
        await _release(runner, obs)


async def _run(
    question: str,
    *,
    runner: Runner,
    system: AgentSystem,
    obs,
    cfg: Settings,
    conversation_id: str,
    evaluate: bool,
) -> RunOutcome:
    """Drive one run to an outcome. Split from :func:`answer_question` purely so
    the teardown above is a `finally` over the whole body rather than a block
    that has to be repeated on each return path."""

    # Circuit breaker: a flash analyst stuck re-calling a tool would otherwise
    # burn tokens until ADK's default ceiling of 500 model calls.
    run_config = RunConfig(max_llm_calls=cfg.max_llm_calls)

    # Assigned as events arrive rather than returned, so a run that trips the
    # circuit breaker part-way still surfaces whatever was synthesised before it.
    answer = ""

    async def _drive() -> None:
        nonlocal answer
        async for event in runner.run_async(
            user_id="supervisor",
            session_id=conversation_id,
            new_message=types.Content(role="user", parts=[types.Part(text=question)]),
            run_config=run_config,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)

    halt: Halt | None = None
    try:
        if obs is not None:
            # One invoke_agent root span around the whole run; the plugin's chat
            # and execute_tool spans nest under it through the current context.
            with obs.telemetry.invoke_agent("producer", conversation_id=conversation_id):
                await _drive()
        else:
            await _drive()
    except LlmCallsLimitExceededError as exc:
        halt = _halt_for_limit(exc, cfg)
    except APIError as exc:
        halt = _halt_for(exc, cfg)
    except BaseExceptionGroup as group:
        # The analysts run inside ADK's ParallelAgent, which drives them in an
        # `asyncio.TaskGroup` -- and a TaskGroup wraps whatever its body raises
        # in an ExceptionGroup. A Vertex 429 therefore arrives here as
        # `ExceptionGroup(APIError(...))`, which is *not* an APIError, so the
        # two clauses above miss it and a perfectly explainable quota refusal
        # escapes as a 500 with a forty-line traceback -- exactly the failure
        # those clauses were written to prevent.
        #
        # This only became reachable when the analysts were parallelised;
        # sequentially, the same error arrived bare. Anything we cannot explain
        # is re-raised rather than flattened into a misleading halt.
        failure = _first_leaf(group, (LlmCallsLimitExceededError, APIError))
        if failure is None:
            raise
        halt = (_halt_for_limit(failure, cfg)
                if isinstance(failure, LlmCallsLimitExceededError)
                else _halt_for(failure, cfg))
    finally:
        if obs is not None:
            obs.flush()

    outcome = RunOutcome(
        answer=answer.strip(),
        system=system,
        settings=cfg,
        response_id=obs.plugin.last_response_id if obs is not None else None,
        halt=halt,
    )

    if evaluate and outcome.answer:
        from agent.evaluation import run_evaluation

        outcome.scorecard = run_evaluation(
            question=question, answer=outcome.answer, timeline=system.timeline,
            ledger=system.ledger, response_id=outcome.response_id,
            telemetry=obs.telemetry if obs is not None else None,
            llm_generate=judge_generator(cfg.analyst_model, cfg.thinking_budget),
        )
        if obs is not None:
            obs.flush()

    return outcome


#: Ceiling on teardown. ADK already bounds each toolset at 10s, but four
#: toolsets in series is 40s of a request a caller is still waiting on, and a
#: wedged subprocess must not hold the response hostage. Past this the leak is
#: the lesser harm: the process is either exiting (CLI) or will retire the
#: instance eventually (Cloud Run).
RELEASE_TIMEOUT = 15.0


async def _release(runner: Runner, obs) -> None:
    """Give back everything the run took: MCP subprocesses, then telemetry.

    Nothing here may raise. The answer has already been produced by the time
    this runs, and losing it to a cleanup error would be a strictly worse
    outcome than the leak this exists to prevent -- so every failure is reported
    to stderr and swallowed.

    Order matters: ``runner.close()`` is what terminates the ``mcp-grafana``
    subprocesses, and the telemetry providers stay up until after it so a span
    emitted during teardown still has somewhere to go.
    """
    close = getattr(runner, "close", None)
    if callable(close):
        try:
            await asyncio.wait_for(close(), timeout=RELEASE_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- see docstring (TimeoutError included)
            print(f"(toolset teardown incomplete: {exc})", file=sys.stderr)

    if obs is not None:
        try:
            obs.shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"(telemetry shutdown incomplete: {exc})", file=sys.stderr)


def _first_leaf(group: BaseExceptionGroup, kinds: tuple[type, ...]) -> BaseException | None:
    """The first exception inside a (possibly nested) group matching ``kinds``.

    First rather than all: a run stops for one reason, and when three analysts
    fail together they have almost always failed for the same reason. The halt
    names it once instead of reciting it three times.
    """
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            found = _first_leaf(exc, kinds)
            if found is not None:
                return found
        elif isinstance(exc, kinds):
            return exc
    return None


def _halt_for_limit(exc: LlmCallsLimitExceededError, cfg: Settings) -> Halt:
    """Turnaround's own cost guard, doing its job."""
    return Halt(
        kind="circuit_breaker",
        detail=f"circuit breaker tripped: {exc}",
        advice=(f"raise TURNAROUND_MAX_LLM_CALLS above {cfg.max_llm_calls} if this is a "
                "legitimately long run; the default guards against a tool-retry loop"),
    )


def _halt_for(exc: APIError, cfg: Settings) -> Halt:
    """Turn a Vertex-side failure into something a producer can act on.

    A 429 is the single likeliest live failure on a small project: the analysts
    fan out, each may retry a query, and a per-minute quota is not large. It used
    to arrive as a forty-line ADK traceback, which tells a supervisor nothing
    they can act on. One sentence and an exit code is more use than a stack.
    """
    if getattr(exc, "code", None) == 429:
        return Halt(
            kind="model_quota",
            detail=f"Vertex refused the call: quota exhausted for {cfg.analyst_model} "
                   f"in {cfg.gcp_location}.",
            advice="wait a minute and re-run, request more Gemini quota for the project, "
                   "or lower TURNAROUND_MAX_LLM_CALLS so a run asks for less at once",
        )
    return Halt(
        kind="model_error",
        detail=f"Vertex returned {getattr(exc, 'code', 'an error')}: {exc}",
        advice="check the project, region and credentials in .env, then re-run",
    )


__all__ = [
    "APP",
    "Halt",
    "HostedMcpNotAuthorized",
    "NotConfigured",
    "RunOutcome",
    "answer_question",
    "instrumentation",
    "judge_generator",
    "resolved_settings",
]
