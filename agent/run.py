"""Ask the Producer a question from the command line.

    uv run python -m agent.run "Why is SEQ0420 slipping and what is it costing?"
    uv run python -m agent.run --approve "What do I change to avoid both?"
    uv run python -m agent.run --interactive "What do I change to avoid both?"

Prints the Producer's answer, then the tool timeline (every Grafana MCP call
made to reach it), then any approval decisions. ``--approve`` auto-approves the
Remediator's writes; ``--interactive`` prompts on the console; the default
denies them, so a plain run never mutates the stack.

Unless ``--no-observability`` is passed, the run also self-instruments with the
OpenTelemetry GenAI conventions (``observability/``): an ``invoke_agent`` trace
with ``chat`` and ``execute_tool`` children, token and latency histograms, all
to the same Grafana Cloud stack the agent queries. Needs
``OTEL_EXPORTER_OTLP_ENDPOINT``; without it the run prints a note and proceeds
uninstrumented.

Unless ``--no-eval`` is passed, the answer is then scored by the judge tier
(``agent/evaluation.py``): a deterministic ground-truth + privacy check, and an
LLM judge (a second Gemini) for grounding / relevance / task completion. Each
check is emitted as a ``gen_ai.evaluation.result`` event correlated to the run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent.approval import AutoApprover, CliApprover
from agent.config import ANALYST_MODEL, bootstrap_vertex
from agent.mcp_grafana import HostedMcpNotAuthorized
from agent.producer import build_system

APP = "turnaround"


def _instrument(enabled: bool):
    """Best-effort GenAI instrumentation. Returns the Instrumentation or None."""
    if not enabled:
        return None
    try:
        from observability import instrument

        return instrument()
    except Exception as exc:  # noqa: BLE001 -- never let telemetry break a run
        print(f"(observability off: {exc})", file=sys.stderr)
        return None


def _llm_generate_or_none(enabled: bool):
    """A Vertex-backed ``generate`` for the LLM judge, or None if unavailable."""
    if not enabled:
        return None
    try:
        from agent.evaluation import vertex_generator

        return vertex_generator(ANALYST_MODEL)
    except Exception as exc:  # noqa: BLE001
        print(f"(LLM judge off: {exc})", file=sys.stderr)
        return None


async def ask(question: str, *, approve: bool, interactive: bool,
              observability: bool = True, evaluate: bool = True) -> int:
    cfg = bootstrap_vertex()
    if not cfg.vertex_ready:
        print("GOOGLE_CLOUD_PROJECT is unset -- fill in .env (see .env.example).", file=sys.stderr)
        return 2
    if not cfg.grafana_ready:
        print("GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN unset -- fill in .env.", file=sys.stderr)
        return 2

    approver = CliApprover() if interactive else AutoApprover(approve=approve)
    try:
        system = build_system(approver=approver, settings=cfg)
    except HostedMcpNotAuthorized as exc:
        print(str(exc), file=sys.stderr)
        return 2

    obs = _instrument(observability)
    plugins = [obs.plugin] if obs is not None else None

    sessions = InMemorySessionService()
    await sessions.create_session(app_name=APP, user_id="supervisor", session_id="cli")
    runner = Runner(app_name=APP, agent=system.producer, session_service=sessions,
                    plugins=plugins)

    async def _drive() -> str:
        answer = ""
        async for event in runner.run_async(
            user_id="supervisor",
            session_id="cli",
            new_message=types.Content(role="user", parts=[types.Part(text=question)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)
        return answer

    final = ""
    try:
        if obs is not None:
            # One invoke_agent root span around the whole run; the plugin's chat
            # and execute_tool spans nest under it through the current context.
            with obs.telemetry.invoke_agent("producer", conversation_id="cli"):
                final = await _drive()
        else:
            final = await _drive()
    finally:
        if obs is not None:
            obs.flush()

    print("\n" + "=" * 96)
    print(final.strip() or "(no answer)")
    print("=" * 96 + "\n")
    print(system.timeline.render())

    rid = obs.plugin.last_response_id if obs is not None else None
    if obs is not None:
        print(f"\ngen_ai trace emitted to {cfg.grafana_url} "
              f"(service.name=turnaround-agent"
              + (f", gen_ai.response.id={rid}" if rid else "") + ")")

    if evaluate and final.strip():
        from agent.evaluation import run_evaluation

        scorecard = run_evaluation(
            question=question, answer=final, timeline=system.timeline,
            ledger=system.ledger, response_id=rid,
            telemetry=obs.telemetry if obs is not None else None,
            llm_generate=_llm_generate_or_none(evaluate),
        )
        if obs is not None:
            obs.flush()
        print("\n" + scorecard.render())

    if system.gate.decisions:
        print("\napproval decisions:")
        for request, decision in system.gate.decisions:
            verdict = "APPROVED" if decision.approved else "DENIED"
            print(f"  {verdict}  {request.tool}  by {decision.by}"
                  + (f" -- {decision.note}" if decision.note else ""))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true", help="auto-approve the Remediator's writes")
    group.add_argument("--interactive", action="store_true", help="prompt on the console for each write")
    ap.add_argument("--no-observability", action="store_true",
                    help="skip the OpenTelemetry GenAI self-instrumentation")
    ap.add_argument("--no-eval", action="store_true",
                    help="skip the judge tier (deterministic + LLM evaluation)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(ask(
        args.question, approve=args.approve, interactive=args.interactive,
        observability=not args.no_observability, evaluate=not args.no_eval,
    )))


if __name__ == "__main__":
    main()
