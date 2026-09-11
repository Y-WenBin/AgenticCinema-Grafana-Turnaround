"""HTTP surface for the agent, for Cloud Run.

``agent/run.py`` is the CLI; this is the same pipeline (``agent/engine.py``)
behind one endpoint so a demo (or a CI probe) can hit a URL:

    POST /ask   {"question": "why is SEQ0420 slipping?"}
      -> {"answer": "...", "timeline": [...], "evaluation": [...],
          "response_id": "turnaround-...", "circuit_breaker_tripped": false}

    GET  /healthz   -> {"status": "ok", "vertex_ready": true, "grafana_ready": true}
    GET  /          -> what this service is and how to call it (HTML or JSON)
    GET  /api/backend -> the live Grafana signal the agents read (`agent/backend.py`)

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

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from agent import backend, engine
from agent.approval import AutoApprover
from agent.config import REPO_ROOT
from agent.config import settings as load_settings
from agent.engine import HostedMcpNotAuthorized, NotConfigured
from agent.limits import Gatekeeper, client_id
from bridge.dotenv import load_env

# --------------------------------------------------------------------------- #
# Interactive API docs
# --------------------------------------------------------------------------- #

#: `/docs`, `/redoc` and `/openapi.json` enumerate every endpoint and schema.
#: That is genuinely useful for a reviewer poking at the demo and needless
#: attack surface anywhere else, so it is one switch rather than a judgement
#: call: on by default, off by setting TURNAROUND_PUBLIC_DOCS to a falsey value.
#:
#: FastAPI only honours these three at construction, so unlike every other knob
#: this one cannot live on `Settings` -- by the time `settings()` is called the
#: app already exists. Hence the explicit `load_env()` first: without it this
#: would read the environment *before* `.env` is loaded, and a
#: `TURNAROUND_PUBLIC_DOCS=0` set only in `.env` would be silently ignored --
#: the exact failure mode `agent/config.py`'s module-constant guard exists to
#: prevent. `load_env` is idempotent and never overwrites a real variable, so
#: calling it here changes nothing for Cloud Run, which sets the environment
#: directly.
load_env()
_DOCS = os.environ.get("TURNAROUND_PUBLIC_DOCS", "1").strip().lower() \
    not in {"0", "false", "no", "off"}

app = FastAPI(
    title="Turnaround agent",
    version="0.1.0",
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)

WEB = REPO_ROOT / "web"

# --------------------------------------------------------------------------- #
# Security headers
# --------------------------------------------------------------------------- #

#: Sent on every response.
#:
#: The CSP is the point. The page's rendering is careful -- `esc()` on every
#: interpolation and `textContent` for the answer -- but several `innerHTML`
#: sinks render `/ask` and `/api/backend` data, and some of that is downstream
#: of model and tool output. `esc()` is the wall; this is the second wall, for
#: the day the first one has a gap. It only buys anything without
#: `unsafe-inline` on `script-src`, which is why `web/app.js` is a separate file
#: rather than an inline `<script>`.
#:
#: `style-src` does keep `unsafe-inline`: the page sets a couple of element
#: styles from script, and inline *style* cannot execute script.
#:
#: Everything is same-origin -- no CDN, no font host, no analytics -- so
#: `default-src 'self'` costs nothing and `connect-src 'self'` pins `/ask` and
#: `/api/*` to this origin.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    # The page can carry a question a visitor typed. Nothing downstream needs to
    # know where they came from, so send no referrer at all.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # frame-ancestors above is the modern control; this is for anything that
    # still only understands the header.
    "X-Frame-Options": "DENY",
    # No geolocation, camera, mic or FLoC from a page that asks one question.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Apply :data:`SECURITY_HEADERS` to every response, errors included.

    A middleware rather than per-route headers: a 404, a 429 from the rate
    limiter and a 500 all render in the same browser, and a header that is only
    right on the happy path is not a control.

    `setdefault` semantics: a route that has already set a header wins. Nothing
    does today, and if something ever needs to relax the CSP for one response,
    it should be able to say so at the route.
    """
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value
    return response


#: Built once per process, from the same settings everything else reads. The
#: counters live here, so they are per instance -- `agent/limits.py` explains
#: exactly what that does and does not bound.
_cfg0 = load_settings()
gate = Gatekeeper(
    per_client=_cfg0.asks_per_hour,
    daily=_cfg0.asks_per_day,
    concurrent=_cfg0.concurrent_asks,
)


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
        "GET /": "the playground, in a browser",
        "GET /health": "liveness and resolved configuration",
        "GET /api/capacity": "what the demo's daily budget has left",
        "GET /api/backend": "the live Grafana signal the agents read, as numbers",
        "POST /ask": 'ask a question: {"question": "why is SEQ0420 slipping?"}',
        # Advertised only when it is actually mounted -- a directory naming a
        # 404 is worse than one that is honestly shorter.
        **({"GET /docs": "OpenAPI / Swagger UI"} if _DOCS else {}),
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
# handler, two audiences: a browser (Accept: text/html) gets the playground in
# `web/`, and every other client -- curl, a probe, a judge's script -- gets this
# same description as JSON.
@app.get("/", response_model=None)
def index(request: Request) -> dict | FileResponse:
    page = WEB / "index.html"
    if "text/html" in request.headers.get("accept", "") and page.is_file():
        # no-store: the page embeds the live budget, and a judge reloading to
        # see whether a slot freed up must not be served yesterday's number.
        return FileResponse(page, headers={"Cache-Control": "no-store"})
    return SERVICE


@app.get("/app.js", include_in_schema=False)
def app_js() -> FileResponse:
    """The playground's script. A separate file so the CSP can refuse inline
    script -- see SECURITY_HEADERS."""
    return FileResponse(WEB / "app.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(WEB / "favicon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/capacity")
def capacity() -> dict:
    """What the demo has left, so the page can say so before someone types a
    question and waits a minute to be told no."""
    return gate.snapshot()


@app.get("/api/backend", response_model=None)
def backend_view(response: Response) -> dict | JSONResponse:
    """The control-room strip: what the agents read, without an account.

    Sync on purpose. The Grafana call is blocking `requests`, so FastAPI runs
    this in its threadpool; making it `async` would park the whole event loop
    on a socket that another service controls.

    `max-age=30` matches the server-side cache, so a browser that reloads twice
    in a second does not even ask.
    """
    try:
        board = backend.view(load_settings())
    except backend.BackendUnavailable:
        # The detail names the stack -- log-worthy, not response-worthy.
        return JSONResponse(status_code=503,
                            content={"error": "the backend view is unavailable"})
    response.headers["Cache-Control"] = "public, max-age=30"
    return board


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


@app.post("/ask", response_model=None)
async def ask_endpoint(req: AskRequest, request: Request, response: Response) -> dict | JSONResponse:
    """Run the pipeline for one question, behind the public guardrails.

    Admission happens before any work: a refusal must cost a Gemini call less
    than an answer does, or the rate limiter is just a slower way to spend the
    budget. The slot is returned in `finally` -- an exception that skipped the
    release would leak capacity one failed run at a time until the endpoint
    wedged at "busy" with nothing actually running.
    """
    verdict = gate.admit(client_id(
        request.headers.get("x-forwarded-for", ""),
        request.client.host if request.client else "",
    ))
    if not verdict.allowed:
        return JSONResponse(
            status_code=429,
            headers=verdict.headers,
            content={"error": verdict.message, "reason": verdict.reason,
                     "retry_after": verdict.retry_after},
        )
    response.headers.update(verdict.headers)

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
    finally:
        gate.release()

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
        # Deliberately absent: `grafana_url`. Nothing rendered it, and a public
        # endpoint handing every caller the stack hostname is a free pointer at
        # the login page for anyone scraping the demo. `/api/backend` shows the
        # data without naming where it lives.
    }


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
