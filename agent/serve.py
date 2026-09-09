"""HTTP surface for the agent, for Cloud Run.

``agent/run.py`` is the CLI; this is the same pipeline (``agent/engine.py``)
behind one endpoint so a demo (or a CI probe) can hit a URL:

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

This module is the *JSON presentation* of a run. The run itself is
``agent/engine.py``.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel, Field

from agent import engine
from agent.approval import AutoApprover
from agent.config import settings as load_settings
from agent.engine import HostedMcpNotAuthorized, NotConfigured

app = FastAPI(title="Turnaround agent", version="0.1.0")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    observability: bool = True
    evaluate: bool = True


@app.get("/healthz")
def healthz() -> dict:
    cfg = load_settings()
    return {
        "status": "ok",
        "vertex_ready": cfg.vertex_ready,
        "grafana_ready": cfg.grafana_ready,
        "mcp_mode": cfg.mcp_mode,
        "max_llm_calls": cfg.max_llm_calls,
    }


@app.post("/ask")
async def ask_endpoint(req: AskRequest) -> dict:
    try:
        cfg = engine.resolved_settings()
        outcome = await engine.answer_question(
            req.question,
            approver=AutoApprover(approve=False),
            conversation_id="http",
            settings=cfg,
            observability=req.observability,
            evaluate=req.evaluate,
        )
    except (NotConfigured, HostedMcpNotAuthorized) as exc:
        return {"error": str(exc)}

    scorecard = outcome.scorecard
    return {
        "answer": outcome.answer,
        "timeline": outcome.system.timeline.as_dicts(),
        "evaluation": [
            {"name": r.name, "score": r.score, "label": r.label,
             "actor_type": r.actor_type, "explanation": r.explanation}
            for r in (scorecard.results if scorecard is not None else [])
        ],
        "response_id": outcome.response_id,
        "circuit_breaker_tripped": outcome.circuit_breaker_tripped,
        # None when the run completed; otherwise why it stopped early. A Vertex
        # quota refusal is a halt too, and it must not read as an empty answer.
        "halted_by": outcome.halt.kind if outcome.halt is not None else None,
        "halt_detail": outcome.halt.render() if outcome.halt is not None else "",
        "grafana_url": outcome.settings.grafana_url,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
