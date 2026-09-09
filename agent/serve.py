"""HTTP surface for the agent, for Cloud Run.

``agent/run.py`` is the CLI; this is the same pipeline (``agent/engine.py``)
behind one endpoint so a demo (or a CI probe) can hit a URL:

    POST /ask   {"question": "why is SEQ0420 slipping?"}
      -> {"answer": "...", "timeline": [...], "evaluation": [...],
          "response_id": "turnaround-...", "circuit_breaker_tripped": false}

    GET  /healthz   -> {"status": "ok", "vertex_ready": true, "grafana_ready": true}
    GET  /          -> what this service is and how to call it (HTML or JSON)

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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
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


SERVICE = {
    "service": "turnaround",
    "what": (
        "Joins a VFX show's creative schedule to its render farm in Grafana, "
        "forecasts delivery slip and crew overload together, and proposes "
        "evidence-backed corrections a supervisor approves."
    ),
    "read_only": True,
    "endpoints": {
        "GET /health": "liveness and resolved configuration",
        "POST /ask": 'ask a question: {"question": "why is SEQ0420 slipping?"}',
        "GET /docs": "OpenAPI / Swagger UI",
    },
    "example": (
        "curl -s -H 'content-type: application/json' "
        "-d '{\"question\": \"why is SEQ0420 slipping and what is it costing?\"}' "
        "$URL/ask"
    ),
    "source": "https://github.com/Y-WenBin/AgenticCinema-Grafana-Turnaround",
}


# A public demo URL's first visitor is a person with a browser, and FastAPI
# declares no route at `/` -- so the front door answered `{"detail":"Not Found"}`,
# which reads as a broken deployment when the service is perfectly healthy. One
# handler, two audiences: a browser (Accept: text/html) gets a readable card, and
# every other client -- curl, a probe, a judge's script -- gets the same content
# as JSON. Keep them fed from one `SERVICE` dict so they can never drift.
@app.get("/", response_model=None)
def index(request: Request) -> dict | HTMLResponse:
    if "text/html" not in request.headers.get("accept", ""):
        return SERVICE
    rows = "\n".join(
        f"<tr><td><code>{path}</code></td><td>{what}</td></tr>"
        for path, what in SERVICE["endpoints"].items()
    )
    # uvicorn trusts `X-Forwarded-Proto` only from 127.0.0.1, and Cloud Run's
    # frontend is not that -- so `request.base_url` says `http` on an https
    # service and the paste-able example would be wrong. Read the header here
    # rather than trusting every `X-Forwarded-*` globally for one string.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    base = str(request.base_url).rstrip("/")
    base = base.replace("http://", f"{scheme}://", 1) if base.startswith("http://") else base
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>Turnaround</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;"
        "margin:4rem auto;padding:0 1.5rem}td{padding:.2rem .8rem .2rem 0;"
        "vertical-align:top}pre{background:#f4f4f5;padding:1rem;overflow-x:auto}"
        "</style>"
        "<h1>Turnaround</h1>"
        f"<p>{SERVICE['what']}</p>"
        f"<table>{rows}</table>"
        f"<pre>{SERVICE['example'].replace('$URL', base)}</pre>"
        f"<p>Read-only by construction. <a href=\"{SERVICE['source']}\">Source</a>.</p>"
    )


# Two paths, one handler. Google Frontend swallows the exact path `/healthz` on
# *.run.app and answers it itself with an HTML 404 -- the request never reaches
# the container, so the documented smoke test silently "fails" against a service
# that is perfectly healthy. `/health` is the one to curl on a hosted URL;
# `/healthz` stays for Cloud Run's own probes and for any deployment not behind
# GFE (a custom domain, GKE, a local run).
@app.get("/health")
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
