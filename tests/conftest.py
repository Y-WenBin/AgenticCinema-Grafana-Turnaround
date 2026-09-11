"""Shared fixtures.

The time-warp in ``bridge.timewarp`` is process-global module state. No unit
test configures it, but an autouse reset keeps a stray call in one test from
bleeding into the next.

The suite is also offline by contract (tests/TESTPLAN.md): no Vertex, no Grafana
Cloud, no ``mcp-grafana`` binary, and no ``.env``. Three autouse fixtures hold
that -- see below.
"""

import os

import pytest

from bridge import timewarp

# The privacy layer refuses to build a pseudonym without a real salt, so any
# test that loads the simulation needs one. A fixed non-placeholder value keeps
# the suite hermetic; it never leaves the test process.
os.environ.setdefault("TURNAROUND_PSEUDONYM_SALT", "turnaround-test-salt")


@pytest.fixture(autouse=True)
def _no_timewarp():
    timewarp.reset()
    yield
    timewarp.reset()


@pytest.fixture(autouse=True)
def _no_live_vertex(monkeypatch):
    """Refuse to build a real Vertex client from a test.

    ``agent.engine.answer_question`` scores answers with a second Gemini by
    default. On a developer machine with a filled-in ``.env`` that is a live,
    billable call from what is meant to be a hermetic suite. A test that wants
    the LLM judge injects its own fake ``generate``; nothing else may reach out.
    """
    from agent import evaluation

    def _refuse(_model, *_args, **_kwargs):
        raise AssertionError(
            "a test tried to build a live Vertex client. Inject a fake "
            "`generate` into LlmJudge / run_evaluation instead."
        )

    monkeypatch.setattr(evaluation, "vertex_generator", _refuse)


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch, tmp_path):
    """Point ``load_env`` at an empty directory for every test.

    The suite must not read the developer's real ``.env`` -- a test that passes
    because of a local credential is a test that fails on someone else's
    machine, or worse, passes on theirs for a different reason. It is
    *autouse* rather than opt-in because the failure is silent: `load_env` uses
    `os.environ.setdefault`, so a leaked real value simply makes assertions
    disagree with themselves several files away.

    This went from theory to fact once ``load_env`` moved to ``bridge.dotenv``:
    the one fixture that redirected it patched ``agent.config.REPO_ROOT``, which
    was no longer the global it resolved against, and five tests in three files
    started reading real credentials. One autouse fixture in one place, so there
    is no second module that has to remember.
    """
    from bridge import dotenv

    monkeypatch.setattr(dotenv, "REPO_ROOT", tmp_path)
