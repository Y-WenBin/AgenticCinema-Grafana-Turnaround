"""The judge tier: deterministic ground-truth + privacy checks, the LLM judge
with a stubbed model, and the ``gen_ai.evaluation.result`` emission path.
"""

from __future__ import annotations

import pytest

from agent.evaluation import (
    DeterministicJudge,
    LlmJudge,
    run_evaluation,
    vertex_generator,
)
from observability.testing import make_harness

GOOD_ANSWER = (
    "Answer: SEQ0420 is slipping because every comp iteration burns ~4.3 render "
    "core-hours against ~2.2 for the rest of the show -- frame 118 keeps failing "
    "on a lighting cache regression that predates the director note. comp-pool-1 "
    "and comp-pool-2 are in crunch.\n"
    "Evidence:\n  - farm: waste ~44 core-h on SEQ0420\n"
)


class _FakeTimeline:
    """Enough of ToolTimeline for the digest helper."""

    class _Call:
        def __init__(self, agent, tool, expr, ok, grafana):
            self.agent, self.tool, self.ok = agent, tool, ok
            self.args = {"expr": expr} if expr else {}
            self.is_grafana_mcp = grafana

    def __init__(self):
        self.calls = [
            self._Call("farm_analyst", "query_prometheus",
                       "sum by (sequence) (turnaround_render_waste_core_hours_total)", True, True),
        ]


# --------------------------------------------------------------------------- #
# DeterministicJudge
# --------------------------------------------------------------------------- #


def test_deterministic_judge_passes_a_grounded_answer():
    results = {r.name: r for r in DeterministicJudge().judge(
        question="why is SEQ0420 slipping?", answer=GOOD_ANSWER, ledger_chain=[])}
    assert results["grounding_numbers"].label == "pass"
    assert results["mechanism_named"].label == "pass"
    assert results["privacy_floor_respected"].label == "pass"
    assert all(r.actor_type == "deterministic" for r in results.values())


def test_deterministic_judge_catches_a_planted_wrong_number():
    bad = "Answer: SEQ0420 comp iterations cost 1.1 core-hours, in line with the rest of the show."
    results = {r.name: r for r in DeterministicJudge().judge(
        question="q", answer=bad, ledger_chain=[])}
    assert results["grounding_numbers"].label == "fail"


def test_deterministic_grounding_passes_without_the_ratio_if_other_anchors_hold():
    """A real run (2026-09-06): grounded on waste + failure-rate + artist-days,
    but never stated the join ratio. That is still a grounded answer."""
    a = ("SEQ0420 is slipping due to a lighting-cache regression: 40.39 core-hours "
         "of farm waste, a 9.24% frame failure rate, costing the comp department "
         "56.2 artist-days per week; three pools are over 60 hours.")
    r = {x.name: x for x in DeterministicJudge().judge(question="q", answer=a, ledger_chain=[])}
    assert r["grounding_numbers"].label == "pass"


def test_deterministic_grounding_fails_a_vague_answer():
    vague = ("SEQ0420 is slipping because renders take significantly longer than "
             "usual and the team is under a lot of pressure right now.")
    r = {x.name: x for x in DeterministicJudge().judge(question="q", answer=vague, ledger_chain=[])}
    assert r["grounding_numbers"].label == "fail"


def test_deterministic_judge_catches_a_subfloor_pool_leak_in_the_answer():
    leak = GOOD_ANSWER + " di-pool-1 is also stretched."
    results = {r.name: r for r in DeterministicJudge().judge(
        question="q", answer=leak, ledger_chain=[])}
    assert results["privacy_floor_respected"].label == "fail"
    assert results["privacy_floor_respected"].score == 0.0


def test_deterministic_judge_catches_a_subfloor_pool_leak_in_the_evidence_chain():
    results = {r.name: r for r in DeterministicJudge().judge(
        question="q", answer=GOOD_ANSWER,
        ledger_chain=["[crunch_guardian] di-pool-1 at 71h"])}
    assert results["privacy_floor_respected"].label == "fail"


# --------------------------------------------------------------------------- #
# LlmJudge
# --------------------------------------------------------------------------- #


def test_llm_judge_parses_a_fenced_json_reply():
    def fake_generate(_prompt: str) -> str:
        return (
            "Here is my assessment:\n```json\n"
            '{"relevance": {"score": 0.9, "reason": "on point"},'
            ' "hallucination": {"score": 0.8, "reason": "mostly grounded"},'
            ' "task_completion": {"score": 0.7, "reason": "resolves it"}}\n```'
        )

    results = {r.name: r for r in LlmJudge(fake_generate).judge(
        question="q", answer=GOOD_ANSWER, timeline_digest="[grafana] farm.query_prometheus ok=True")}
    assert results["relevance"].score == pytest.approx(0.9)
    assert results["relevance"].label == "pass"
    assert results["hallucination"].actor_type == "ai"


def test_llm_judge_survives_unparseable_output():
    results = LlmJudge(lambda _p: "the model refused").judge(
        question="q", answer="a", timeline_digest="")
    assert {r.name for r in results} == {"relevance", "hallucination", "task_completion"}
    assert all(r.score == 0.0 and r.label == "fail" for r in results)


# --------------------------------------------------------------------------- #
# run_evaluation: emission + orchestration
# --------------------------------------------------------------------------- #


def test_run_evaluation_emits_one_event_per_check_correlated_by_response_id():
    h = make_harness()
    scorecard = run_evaluation(
        question="why is SEQ0420 slipping?", answer=GOOD_ANSWER,
        timeline=_FakeTimeline(), ledger=None, response_id="resp-77",
        telemetry=h.telemetry,
        llm_generate=lambda _p: '{"relevance":{"score":1,"reason":"x"},'
                                '"hallucination":{"score":1,"reason":"x"},'
                                '"task_completion":{"score":1,"reason":"x"}}',
    )
    events = h.evaluation_events()
    names = {e["gen_ai.evaluation.name"] for e in events}
    assert {"grounding_numbers", "mechanism_named", "privacy_floor_respected",
            "relevance", "hallucination", "task_completion"} <= names
    assert all(e["gen_ai.response.id"] == "resp-77" for e in events)
    assert not scorecard.failed


def test_run_evaluation_without_llm_still_runs_the_deterministic_checks():
    h = make_harness()
    scorecard = run_evaluation(
        question="q", answer=GOOD_ANSWER, timeline=_FakeTimeline(),
        ledger=None, response_id=None, telemetry=h.telemetry, llm_generate=None)
    assert {r.name for r in scorecard.results} == {
        "grounding_numbers", "mechanism_named", "privacy_floor_respected"}
    assert len(h.evaluation_events()) == 3


def test_run_evaluation_reports_a_privacy_breach_in_the_scorecard():
    h = make_harness()
    scorecard = run_evaluation(
        question="q", answer=GOOD_ANSWER + " di-pool-1 is buried too.",
        timeline=_FakeTimeline(), ledger=None, response_id="r",
        telemetry=h.telemetry, llm_generate=None)
    assert any(r.name == "privacy_floor_respected" and r.label == "fail"
               for r in scorecard.failed)
    assert "privacy floor breached" in scorecard.render()


def test_vertex_generator_is_lazy_and_does_not_touch_the_network_on_import():
    # Constructing the generator may build a client, but must not call the model.
    # If credentials are absent this raises; that is acceptable -- the point is
    # that importing agent.evaluation does not.
    assert callable(vertex_generator)
