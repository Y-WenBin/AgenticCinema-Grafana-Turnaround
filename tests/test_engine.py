"""The one shared run path, and the two front ends that present it.

``agent/engine.py`` builds the system, drives the runner under the circuit
breaker and scores the answer. ``agent/run.py`` turns that into console text and
an exit code; ``agent/serve.py`` turns the same thing into JSON. These tests
pin the contract between them, so the CLI and the endpoint cannot drift apart
the way two copies of the pipeline would.

Nothing here touches Vertex, Grafana or the network: the ADK ``Runner`` is
replaced with a stub that yields a scripted final event.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from agent import engine
from agent import run as run_mod
from agent import serve as serve_mod
from agent.approval import AutoApprover
from agent.config import REPO_ROOT, Settings

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


def _runner_yielding(text: str):
    """A stand-in ADK Runner that emits one final response and nothing else."""

    class _Runner:
        def __init__(self, **_kw):
            pass

        async def run_async(self, **_kw):
            yield _FinalEvent(text)

    return _Runner


def _runner_raising(exc: Exception):
    class _Runner:
        def __init__(self, **_kw):
            pass

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
    assert outcome.system.producer.sub_agents[0].name == "schedule_analyst"


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
    assert names == {"grounding_numbers", "mechanism_named", "privacy_floor_respected"}
    assert not outcome.scorecard.failed


def test_an_empty_answer_is_not_scored(offline):
    """Scoring nothing would emit three misleading 'fail' events per run."""
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

    class _Runner:
        def __init__(self, **_kw):
            pass

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
    assert engine.judge_generator(False, "gemini-2.5-flash") is None

    import agent.evaluation as ev
    monkeypatch.setattr(ev, "vertex_generator",
                        lambda _m: (_ for _ in ()).throw(RuntimeError("no ADC")))
    assert engine.judge_generator(True, "gemini-2.5-flash") is None
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

    class _Runner:
        def __init__(self, **_kw):
            pass

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
# The HTTP front end
# --------------------------------------------------------------------------- #


def test_ask_returns_the_documented_json_shape(offline):
    body = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?", "observability": False}).json()

    assert set(body) == {"answer", "timeline", "evaluation", "response_id",
                         "circuit_breaker_tripped", "halted_by", "halt_detail"}
    # `grafana_url` used to be here. Nothing rendered it, and a public endpoint
    # handing every caller the stack hostname is a free pointer at its login
    # page -- `/api/backend` shows the data without naming where it lives.
    assert "grafana_url" not in body
    assert body["halted_by"] is None
    assert body["answer"].startswith("Answer: SEQ0420")
    assert body["circuit_breaker_tripped"] is False
    assert {e["name"] for e in body["evaluation"]} == {
        "grounding_numbers", "mechanism_named", "privacy_floor_respected"}
    assert all({"name", "score", "label", "actor_type", "explanation"} == set(e)
               for e in body["evaluation"])


def test_ask_reports_a_tripped_breaker_in_the_body_not_as_a_500(offline):
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    offline.setattr(engine, "Runner", _runner_raising(LlmCallsLimitExceededError("boom")))
    response = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?", "observability": False,
              "evaluate": False})

    assert response.status_code == 200
    assert response.json()["circuit_breaker_tripped"] is True


def test_ask_refuses_an_unconfigured_server_with_a_named_error(monkeypatch):
    monkeypatch.setattr(engine, "bootstrap_vertex", lambda: _cfg(gcp_project=""))
    body = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?"}).json()
    assert "GOOGLE_CLOUD_PROJECT" in body["error"]
    assert "answer" not in body


@pytest.mark.parametrize("question", ["", "hi", "x" * 2001])
def test_ask_validates_the_question_before_spending_a_token(question):
    """Length bounds are enforced by the request model, so a junk POST costs
    nothing: no build_system, no Gemini call."""
    response = TestClient(serve_mod.app).post("/ask", json={"question": question})
    assert response.status_code == 422


def test_root_is_not_a_404():
    """The front door of a public demo must not answer `{"detail":"Not Found"}`.

    FastAPI declares no route at `/` unless one is written, so the first person
    who pastes the hosted URL into a browser sees what looks like a broken
    deployment against a service that is perfectly healthy.
    """
    response = TestClient(serve_mod.app).get("/")
    assert response.status_code == 200


def test_root_serves_the_playground_to_a_browser_and_json_to_everything_else():
    """One route, two audiences: a person gets something to try, a script gets
    a description it can parse."""
    client = TestClient(serve_mod.app)

    page = client.get("/", headers={"accept": "text/html,application/xhtml+xml"})
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<textarea" in page.text, "a playground needs somewhere to type"
    assert "/ask" in page.text

    machine = client.get("/", headers={"accept": "application/json"})
    assert machine.json() == serve_mod.SERVICE
    assert machine.json()["read_only"] is True


def test_the_page_never_goes_stale_against_a_cached_budget():
    """It prints how much budget is left, so a judge reloading to see whether a
    slot freed up must not be handed the copy from ten minutes ago."""
    page = TestClient(serve_mod.app).get("/", headers={"accept": "text/html"})
    assert "no-store" in page.headers.get("cache-control", "")


def test_the_page_only_calls_endpoints_that_exist():
    """The playground is a static file, so nothing in Python stops it fetching
    a route that was since renamed -- the failure would be invisible until a
    judge clicked the button. Every same-origin path it references must be a
    real route or a real file.

    (This replaces an earlier test that the server-rendered curl example used
    the right scheme behind Cloud Run's TLS termination. The page now builds
    that string from `location.origin` in the browser, which knows its own
    scheme for certain, so the whole class of bug is gone rather than tested.)
    """
    html = (REPO_ROOT / "web" / "index.html").read_text()
    referenced = set(re.findall(r'(?:fetch\(|src=|href=)"(/[\w./-]*)"', html))
    assert {"/ask", "/api/capacity", "/api/backend",
            "/banner.png"} <= referenced, "expected calls missing"

    routes = {getattr(r, "path", None) for r in serve_mod.app.routes}
    for path in referenced:
        if (REPO_ROOT / "web" / path.lstrip("/")).is_file():
            continue
        assert path in routes, f"the page calls {path}, which is not a route"


def test_the_hackathon_card_is_the_link_preview_not_just_decoration():
    """A judge meets this project as a pasted link at least as often as a page."""
    html = (REPO_ROOT / "web" / "index.html").read_text()
    og = re.search(r'property="og:image" content="([^"]+)"', html)
    assert og, "no og:image, so a pasted link renders as a bare URL"
    assert og.group(1).startswith("https://"), \
        "og:image must be absolute -- scrapers do not run JS and resolve relative paths unreliably"
    assert og.group(1).endswith("/banner.png")
    assert (REPO_ROOT / "web" / "banner.png").is_file()


def test_ask_refuses_past_the_cap_without_running_the_pipeline(offline, monkeypatch):
    """The refusal must be cheaper than the answer.

    A limiter that admits the request, builds the agents and *then* declines is
    a slower way to spend the budget, not a guard on it.
    """
    from agent.limits import Gatekeeper

    runs = []
    monkeypatch.setattr(serve_mod.engine, "answer_question",
                        _counting(runs, serve_mod.engine.answer_question))
    monkeypatch.setattr(serve_mod, "gate", Gatekeeper(per_client=1, daily=99, concurrent=9))
    client = TestClient(serve_mod.app)

    assert client.post("/ask", json={"question": "why is SEQ0420 slipping?"}).status_code == 200
    refused = client.post("/ask", json={"question": "and again?"})

    assert refused.status_code == 429
    assert len(runs) == 1, "the refused request must not have run the pipeline"
    body = refused.json()
    assert body["reason"] == "per_visitor"
    assert body["retry_after"] > 0
    assert refused.headers["Retry-After"] == str(body["retry_after"])


def test_a_finished_run_gives_its_slot_back(offline, monkeypatch):
    """Held slots are how a demo dies: it reports 'busy' forever with nothing
    running. The release is in a `finally`, and this is what pins it there."""
    from agent.limits import Gatekeeper

    monkeypatch.setattr(serve_mod, "gate", Gatekeeper(per_client=99, daily=99, concurrent=1))
    client = TestClient(serve_mod.app)
    for _ in range(3):
        assert client.post("/ask", json={"question": "why is SEQ0420 slipping?"}).status_code == 200
    assert serve_mod.gate.snapshot()["in_flight"] == 0


def test_a_crashing_run_gives_its_slot_back_too(offline, monkeypatch):
    """The case that actually leaks: an exception on the way out."""
    from agent.limits import Gatekeeper

    async def boom(*_a, **_k):
        raise RuntimeError("vertex fell over")

    monkeypatch.setattr(serve_mod.engine, "answer_question", boom)
    monkeypatch.setattr(serve_mod, "gate", Gatekeeper(per_client=99, daily=99, concurrent=1))
    client = TestClient(serve_mod.app)

    with pytest.raises(RuntimeError):
        client.post("/ask", json={"question": "why is SEQ0420 slipping?"})
    assert serve_mod.gate.snapshot()["in_flight"] == 0, "slot leaked on the error path"


def test_capacity_is_public_so_the_page_can_warn_before_the_wall(offline, monkeypatch):
    """Better to say 'no runs left today' up front than after a judge has typed
    a question and waited a minute for the refusal."""
    from agent.limits import Gatekeeper

    monkeypatch.setattr(serve_mod, "gate", Gatekeeper(per_client=9, daily=5, concurrent=9))
    client = TestClient(serve_mod.app)
    before = client.get("/api/capacity").json()
    assert before == {"asks_today": 0, "daily_budget": 5, "in_flight": 0,
                      "concurrent_limit": 9, "per_visitor_hourly": 9}

    client.post("/ask", json={"question": "why is SEQ0420 slipping?"})
    assert client.get("/api/capacity").json()["asks_today"] == 1


def test_visitors_are_told_apart_by_the_forwarded_address(offline, monkeypatch):
    """Behind Cloud Run every request has the same peer address, so without
    X-Forwarded-For the first visitor's quota would be everyone's quota."""
    from agent.limits import Gatekeeper

    monkeypatch.setattr(serve_mod, "gate", Gatekeeper(per_client=1, daily=99, concurrent=9))
    client = TestClient(serve_mod.app)
    q = {"question": "why is SEQ0420 slipping?"}

    assert client.post("/ask", json=q, headers={"x-forwarded-for": "203.0.113.1"}).status_code == 200
    assert client.post("/ask", json=q, headers={"x-forwarded-for": "203.0.113.1"}).status_code == 429
    assert client.post("/ask", json=q, headers={"x-forwarded-for": "203.0.113.9"}).status_code == 200


def test_health_is_reachable_under_both_paths(monkeypatch):
    """`/healthz` is unreachable on *.run.app -- Google Frontend answers it itself.

    GFE intercepts the exact lowercase path `/healthz` on a run.app hostname and
    returns its own HTML 404; the request never reaches the container. A healthy
    service therefore looks dead to the documented smoke test. `/health` is the
    alias that actually gets through, and both must return the same body.
    """
    monkeypatch.setattr(serve_mod, "load_settings", lambda: _cfg(max_llm_calls=11))
    client = TestClient(serve_mod.app)
    a = client.get("/health")
    b = client.get("/healthz")
    assert a.status_code == 200 and b.status_code == 200
    assert a.json() == b.json()
    assert a.json()["status"] == "ok"


def test_healthz_is_a_pure_read_and_publishes_the_resolved_ceiling(monkeypatch):
    monkeypatch.setattr(serve_mod, "load_settings", lambda: _cfg(max_llm_calls=11))
    body = TestClient(serve_mod.app).get("/healthz").json()
    assert body == {"status": "ok", "vertex_ready": True, "grafana_ready": True,
                    "mcp_mode": "oss", "max_llm_calls": 11}


def test_the_endpoint_can_never_approve_a_write(offline):
    """A public URL must not be able to mutate Kitsu or Grafana. Asserted on the
    built system, not by grepping the source."""
    captured = {}

    def _capture(**kwargs):
        captured["approver"] = kwargs["approver"]
        from agent.producer import build_system
        return build_system(**kwargs)

    offline.setattr(engine, "build_system", _capture)
    TestClient(serve_mod.app).post(
        "/ask", json={"question": "change the SEQ0420 dates", "observability": False,
                      "evaluate": False})

    approver = captured["approver"]
    assert isinstance(approver, AutoApprover)
    assert approver.approve is False
    decision = approver.decide(None)
    assert decision.approved is False


# --------------------------------------------------------------------------- #
# Vertex-side failures. Observed live: four questions back to back exhausted the
# project's per-minute Gemini quota and the third exited 1 with a forty-line ADK
# traceback. On a small project this is the likeliest live failure there is.
# --------------------------------------------------------------------------- #


def _api_error(code: int, message: str):
    from google.genai.errors import APIError

    return APIError(code, {"error": {"code": code, "message": message,
                                     "status": "RESOURCE_EXHAUSTED"}})


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


def test_the_endpoint_names_the_halt_instead_of_returning_a_blank_answer(offline):
    """A 200 with an empty ``answer`` and no reason is indistinguishable from
    'the agent had nothing to say'."""
    offline.setattr(engine, "Runner", _runner_raising(_api_error(429, "Resource exhausted.")))
    body = TestClient(serve_mod.app).post(
        "/ask", json={"question": "why is SEQ0420 slipping?", "observability": False,
                      "evaluate": False}).json()

    assert body["answer"] == ""
    assert body["halted_by"] == "model_quota"
    assert "quota exhausted" in body["halt_detail"]
    assert body["circuit_breaker_tripped"] is False
