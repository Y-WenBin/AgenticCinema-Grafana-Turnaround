"""Shared fixtures.

The time-warp in ``bridge.timewarp`` is process-global module state. No unit
test configures it, but an autouse reset keeps a stray call in one test from
bleeding into the next.

The suite is also offline by contract (tests/TESTPLAN.md): no Vertex, no Grafana
Cloud, no ``mcp-grafana`` binary. Two autouse fixtures hold that -- see below.
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

    def _refuse(_model):
        raise AssertionError(
            "a test tried to build a live Vertex client. Inject a fake "
            "`generate` into LlmJudge / run_evaluation instead."
        )

    monkeypatch.setattr(evaluation, "vertex_generator", _refuse)
