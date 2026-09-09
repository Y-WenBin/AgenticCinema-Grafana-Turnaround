"""Ask the Producer a question from the command line.

    uv run python -m agent.run "Why is SEQ0420 slipping and what is it costing?"
    uv run python -m agent.run --approve "What do I change to avoid both?"
    uv run python -m agent.run --interactive "What do I change to avoid both?"

Prints the Producer's answer, then the tool timeline (every Grafana MCP call
made to reach it), then the judge scorecard and any approval decisions.
``--approve`` auto-approves the Remediator's writes; ``--interactive`` prompts on
the console; the default denies them, so a plain run never mutates the stack.

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

A circuit breaker (``TURNAROUND_MAX_LLM_CALLS``, default 40) caps Gemini calls
for the whole run so a stuck tool-retry loop cannot run up the token bill. That
and a Vertex-side refusal (a 429 on a small quota is the likeliest live failure)
both surface as one line naming the cause and what to do about it.

Exit codes: 0 answered, 2 startup/config problem, 3 the run was halted with
nothing to show -- by the circuit breaker, or by Vertex (quota, outage). Every
one of those is a sentence on stderr, never a traceback.

This module is the *presentation* of a run. The run itself is ``agent/engine.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agent import engine
from agent.approval import AutoApprover, CliApprover
from agent.engine import HostedMcpNotAuthorized, NotConfigured, RunOutcome


def _report(outcome: RunOutcome) -> None:
    """Print the answer, the tool timeline, the scorecard and the decisions."""
    print("\n" + "=" * 96)
    print(outcome.answer or "(no answer)")
    print("=" * 96 + "\n")
    print(outcome.system.timeline.render())

    if outcome.response_id is not None:
        print(f"\ngen_ai trace emitted to {outcome.settings.grafana_url} "
              f"(service.name=turnaround-agent, "
              f"gen_ai.response.id={outcome.response_id})")

    if outcome.scorecard is not None:
        print("\n" + outcome.scorecard.render())

    if outcome.system.gate.decisions:
        print("\napproval decisions:")
        for request, decision in outcome.system.gate.decisions:
            verdict = "APPROVED" if decision.approved else "DENIED"
            print(f"  {verdict}  {request.tool}  by {decision.by}"
                  + (f" -- {decision.note}" if decision.note else ""))


async def ask(question: str, *, approve: bool, interactive: bool,
              observability: bool = True, evaluate: bool = True) -> int:
    try:
        cfg = engine.resolved_settings()
        outcome = await engine.answer_question(
            question,
            approver=CliApprover() if interactive else AutoApprover(approve=approve),
            conversation_id="cli",
            settings=cfg,
            observability=observability,
            evaluate=evaluate,
        )
    except (NotConfigured, HostedMcpNotAuthorized) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if outcome.halt is not None:
        print("\n" + outcome.halt.render(), file=sys.stderr)
        if not outcome.answer:
            return 3

    _report(outcome)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
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
