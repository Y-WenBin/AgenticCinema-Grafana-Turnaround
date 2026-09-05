"""Ask the Producer a question from the command line.

    uv run python -m agent.run "Why is SEQ0420 slipping and what is it costing?"
    uv run python -m agent.run --approve "What do I change to avoid both?"
    uv run python -m agent.run --interactive "What do I change to avoid both?"

Prints the Producer's answer, then the tool timeline (every Grafana MCP call
made to reach it), then any approval decisions. ``--approve`` auto-approves the
Remediator's writes; ``--interactive`` prompts on the console; the default
denies them, so a plain run never mutates the stack.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent.approval import AutoApprover, CliApprover
from agent.config import bootstrap_vertex
from agent.producer import build_system

APP = "turnaround"


async def ask(question: str, *, approve: bool, interactive: bool) -> int:
    cfg = bootstrap_vertex()
    if not cfg.vertex_ready:
        print("GOOGLE_CLOUD_PROJECT is unset -- fill in .env (see .env.example).", file=sys.stderr)
        return 2
    if not cfg.grafana_ready:
        print("GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN unset -- fill in .env.", file=sys.stderr)
        return 2

    approver = CliApprover() if interactive else AutoApprover(approve=approve)
    system = build_system(approver=approver, settings=cfg)

    sessions = InMemorySessionService()
    await sessions.create_session(app_name=APP, user_id="supervisor", session_id="cli")
    runner = Runner(app_name=APP, agent=system.producer, session_service=sessions)

    final = ""
    async for event in runner.run_async(
        user_id="supervisor",
        session_id="cli",
        new_message=types.Content(role="user", parts=[types.Part(text=question)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final = "".join(p.text or "" for p in event.content.parts)

    print("\n" + "=" * 96)
    print(final.strip() or "(no answer)")
    print("=" * 96 + "\n")
    print(system.timeline.render())

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
    args = ap.parse_args()
    raise SystemExit(asyncio.run(ask(args.question, approve=args.approve, interactive=args.interactive)))


if __name__ == "__main__":
    main()
