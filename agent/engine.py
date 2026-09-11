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

if TYPE_CHECKING:
    from agent.evaluation import Scorecard

APP = "turnaround"


class NotConfigured(RuntimeError):
    """Startup validation failed: the message names the missing keys."""


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


def judge_generator(enabled: bool, model: str,
                   thinking_budget: int = 0) -> Callable[[str], str] | None:
    """A Vertex-backed ``generate`` for the LLM judge, or None if unavailable."""
    if not enabled:
        return None
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
    missing = []
    if not cfg.vertex_ready:
        missing.append("GOOGLE_CLOUD_PROJECT")
    if not cfg.grafana_ready:
        missing.append("GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if missing:
        raise NotConfigured(f"unset: {', '.join(missing)} -- fill in .env (see .env.example).")
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
        halt = Halt(
            kind="circuit_breaker",
            detail=f"circuit breaker tripped: {exc}",
            advice=(f"raise TURNAROUND_MAX_LLM_CALLS above {cfg.max_llm_calls} if this is a "
                    "legitimately long run; the default guards against a tool-retry loop"),
        )
    except APIError as exc:
        halt = _halt_for(exc, cfg)
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
            llm_generate=judge_generator(evaluate, cfg.analyst_model,
                                         cfg.thinking_budget),
        )
        if obs is not None:
            obs.flush()

    return outcome


def _halt_for(exc: APIError, cfg: Settings) -> Halt:
    """Turn a Vertex-side failure into something a producer can act on.

    A 429 is the single likeliest live failure on a hackathon-scale project --
    the analysts fan out, each retries a query, and a per-minute quota is small.
    It arrived as a forty-line ADK traceback, which is the worst thing that can
    happen on camera.
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
