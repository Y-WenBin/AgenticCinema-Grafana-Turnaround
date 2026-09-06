"""HTTP surface for the agent, for Cloud Run.

``agent/run.py`` is the CLI; this is the same pipeline behind one endpoint so a
demo (or a CI probe) can hit a URL:

    POST /ask   {"question": "why is SEQ0420 slipping?"}
      -> {"answer": "...", "timeline": [...], "evaluation": [...],
          "response_id": "turnaround-...", "circuit_breaker_tripped": false}

    GET  /healthz   -> {"status": "ok", "vertex_ready": true, "grafana_ready": true}

Writes are always auto-denied here (``AutoApprover(approve=False)``) -- a public
endpoint must never be able to mutate Kitsu or Grafana. Run the CLI with
``--approve`` for the gated write-back path.

Self-instrumentation and the judge tier are on by default, same as the CLI, so a
request produces its ``invoke_agent`` trace, token/latency histograms and
``gen_ai.evaluation.result`` events in the same Grafana Cloud stack.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from agent.approval import AutoApprover
from agent.config import MAX_LLM_CALLS, bootstrap_vertex
from agent.mcp_grafana import HostedMcpNotAuthorized
from agent.producer import build_system
from agent.run import _instrument, _llm_generate_or_none

APP = "turnaround"

app = FastAPI(title="Turnaround agent", version="0.1.0")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    observability: bool = True
    evaluate: bool = True


@app.get("/healthz")
def healthz() -> dict:
    cfg = bootstrap_vertex()
    return {
        "status": "ok",
        "vertex_ready": cfg.vertex_ready,
        "grafana_ready": cfg.grafana_ready,
        "mcp_mode": cfg.mcp_mode,
        "max_llm_calls": MAX_LLM_CALLS,
    }


@app.post("/ask")
async def ask_endpoint(req: AskRequest) -> dict:
    cfg = bootstrap_vertex()
    if not cfg.vertex_ready or not cfg.grafana_ready:
        return {"error": "server not configured: GOOGLE_CLOUD_PROJECT and "
                         "GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN must be set"}

    try:
        system = build_system(approver=AutoApprover(approve=False), settings=cfg)
    except HostedMcpNotAuthorized as exc:
        return {"error": str(exc)}

    obs = _instrument(req.observability)
    plugins = [obs.plugin] if obs is not None else None

    sessions = InMemorySessionService()
    await sessions.create_session(app_name=APP, user_id="supervisor", session_id="http")
    runner = Runner(app_name=APP, agent=system.producer, session_service=sessions,
                    plugins=plugins)
    run_config = RunConfig(max_llm_calls=MAX_LLM_CALLS)

    async def _drive() -> str:
        answer = ""
        async for event in runner.run_async(
            user_id="supervisor", session_id="http",
            new_message=types.Content(role="user", parts=[types.Part(text=req.question)]),
            run_config=run_config,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)
        return answer

    final, tripped = "", False
    try:
        if obs is not None:
            with obs.telemetry.invoke_agent("producer", conversation_id="http"):
                final = await _drive()
        else:
            final = await _drive()
    except LlmCallsLimitExceededError:
        tripped = True
    finally:
        if obs is not None:
            obs.flush()

    rid = obs.plugin.last_response_id if obs is not None else None
    evaluation: list[dict] = []
    if req.evaluate and final.strip():
        from agent.evaluation import run_evaluation

        scorecard = run_evaluation(
            question=req.question, answer=final, timeline=system.timeline,
            ledger=system.ledger, response_id=rid,
            telemetry=obs.telemetry if obs is not None else None,
            llm_generate=_llm_generate_or_none(req.evaluate),
        )
        if obs is not None:
            obs.flush()
        evaluation = [
            {"name": r.name, "score": r.score, "label": r.label,
             "actor_type": r.actor_type, "explanation": r.explanation}
            for r in scorecard.results
        ]

    return {
        "answer": final.strip(),
        "timeline": system.timeline.as_dicts(),
        "evaluation": evaluation,
        "response_id": rid,
        "circuit_breaker_tripped": tripped,
        "grafana_url": cfg.grafana_url,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
