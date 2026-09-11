"""The thinking budget resolves, reaches every agent, and stays overridable.

This knob is worth a test file of its own because it is the difference between
a ~20-second answer and a ~70-second one (measured; see
``agent/config.py::DEFAULT_THINKING_BUDGET``), and because "off" is a *zero* --
exactly the value a positive-integer environment parser silently discards.
"""

from __future__ import annotations

import pytest

from agent.approval import AutoApprover
from agent.config import DEFAULT_THINKING_BUDGET, Settings, settings
from agent.producer import build_system
from agent.thinking import DYNAMIC, generate_config, planner, thinking_config


def _cfg(**kw) -> Settings:
    base = {"grafana_url": "https://stack.grafana.net", "grafana_token": "glsa_x",
            "gcp_project": "proj", "gcp_location": "us-central1",
            "mcp_grafana_bin": "/opt/homebrew/bin/mcp-grafana"}
    return Settings(**{**base, **kw})


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def test_the_default_is_thinking_off():
    assert DEFAULT_THINKING_BUDGET == 0
    assert _cfg().thinking_budget == 0


@pytest.mark.parametrize(("raw", "expected"), [
    ("0", 0),            # off -- the default, and the value a >0 parser would eat
    ("-1", DYNAMIC),     # dynamic: hand the decision back to Gemini
    ("512", 512),        # an explicit ceiling
    ("  256  ", 256),    # whitespace, as a .env line often has
])
def test_the_environment_can_set_any_meaningful_budget(monkeypatch, raw, expected):
    monkeypatch.setenv("TURNAROUND_THINKING_BUDGET", raw)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    assert settings().thinking_budget == expected


@pytest.mark.parametrize("raw", ["", "lots", "-2", "-99"])
def test_nonsense_falls_back_to_the_default_rather_than_failing_a_run(monkeypatch, raw):
    """-2 is not a smaller dynamic; it is a typo. Gemini rejects it, so it must
    never reach the API -- an unparseable knob degrades to the default."""
    monkeypatch.setenv("TURNAROUND_THINKING_BUDGET", raw)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    assert settings().thinking_budget == DEFAULT_THINKING_BUDGET


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_dynamic_produces_no_config_at_all():
    """Passing nothing and passing "you decide" mean the same thing to Gemini,
    so the dynamic path stays byte-identical to the untuned one."""
    assert thinking_config(DYNAMIC) is None
    assert planner(DYNAMIC) is None
    assert generate_config(DYNAMIC) is None


@pytest.mark.parametrize("budget", [0, 128, 2048])
def test_a_real_budget_reaches_both_the_adk_and_the_raw_genai_shapes(budget):
    assert thinking_config(budget).thinking_budget == budget
    assert planner(budget).thinking_config.thinking_budget == budget
    assert generate_config(budget).thinking_config.thinking_budget == budget


def test_thoughts_are_never_included_in_the_response():
    """The analysts' output_key text is copied verbatim into the evidence ledger
    and then into the synthesis prompt. Thought summaries leaking into that
    stream would put unmeasured prose where the grounding rules expect
    specialist findings."""
    assert thinking_config(1024).include_thoughts is False


# --------------------------------------------------------------------------- #
# Wiring -- every model call in the pipeline, not just some of them
# --------------------------------------------------------------------------- #


def _leaves(agent):
    out = {}
    for sub in agent.sub_agents:
        if getattr(sub, "sub_agents", None):
            out.update(_leaves(sub))
        else:
            out[sub.name] = sub
    return out


def test_every_agent_in_the_pipeline_gets_the_budget():
    leaves = _leaves(build_system(approver=AutoApprover(approve=False),
                                  settings=_cfg(thinking_budget=256)).producer)
    assert set(leaves) == {"schedule_analyst", "farm_analyst", "crunch_guardian",
                           "remediator", "synthesis"}
    for name, agent in leaves.items():
        assert agent.planner is not None, f"{name} has no thinking budget"
        assert agent.planner.thinking_config.thinking_budget == 256, name


def test_dynamic_leaves_every_agent_on_adks_own_default():
    leaves = _leaves(build_system(approver=AutoApprover(approve=False),
                                  settings=_cfg(thinking_budget=DYNAMIC)).producer)
    assert [a.planner for a in leaves.values()] == [None] * 5


def test_the_llm_judge_is_given_the_same_budget(monkeypatch):
    """The judge is the last model call in a run, with the supervisor already
    waiting on a finished answer, so it must not be the one tier left thinking."""
    seen = {}

    def fake_vertex_generator(model, thinking_budget=0):
        seen["model"], seen["budget"] = model, thinking_budget
        return lambda prompt: "{}"

    from agent import engine, evaluation

    monkeypatch.setattr(evaluation, "vertex_generator", fake_vertex_generator)
    assert engine.judge_generator(True, "gemini-2.5-flash", 256) is not None
    assert seen == {"model": "gemini-2.5-flash", "budget": 256}
