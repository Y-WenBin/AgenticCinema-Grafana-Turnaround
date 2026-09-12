"""The judge tier: deterministic ground-truth + privacy checks, the LLM judge
with a stubbed model, and the ``gen_ai.evaluation.result`` emission path.
"""

from __future__ import annotations

import json

import pytest

from agent.evaluation import (
    DeterministicJudge,
    LlmJudge,
    _label_for,
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


class TestTheAnswerIsReadableByAProducer:
    """The check behind ``figures_are_readable``.

    Taken from a live run against the deployed service, which reported
    ``lighting-pool-2 at 63.16190476190476h`` in a block the synthesis prompt
    asks to be written "in plain language for a producer, not an SRE".
    """

    def _judge(self, answer):
        return {r.name: r for r in DeterministicJudge().judge(
            question="who is in crunch?", answer=answer, ledger_chain=[])}

    def test_a_raw_float_fails_and_is_quoted_back(self):
        answer = ("crew: comp-pool-1 at 70.8h, comp-pool-2 at 79.328h, "
                  "lighting-pool-2 at 63.16190476190476h")
        result = self._judge(answer)["figures_are_readable"]
        assert result.label == "fail"
        assert result.score == 0.0
        # the explanation has to name the offender, or nobody can act on it
        assert "63.16190476190476" in result.explanation

    def test_the_same_answer_rounded_passes(self):
        result = self._judge(
            "crew: comp-pool-1 at 70.8h, comp-pool-2 at 79.3h, "
            "lighting-pool-2 at 63.2h")["figures_are_readable"]
        assert result.label == "pass"

    def test_two_decimals_are_allowed(self):
        """A rate quoted as 9.25% is precise, not sloppy. The line is three."""
        assert self._judge("frame failures at 9.25%")["figures_are_readable"].label == "pass"

    def test_a_version_or_a_date_is_not_a_figure(self):
        """``mcp-grafana v1.3.0`` and ``2026-09-12`` both carry digits and dots
        and neither is a measurement. A check that cried wolf on them would be
        turned off within a week."""
        answer = "seeded 2026-09-12 with mcp-grafana v1.3.0 against 10.0.0.1"
        assert self._judge(answer)["figures_are_readable"].label == "pass"


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
            "figures_are_readable",
            "relevance", "hallucination", "task_completion"} <= names
    assert all(e["gen_ai.response.id"] == "resp-77" for e in events)
    assert not scorecard.failed


def test_run_evaluation_without_llm_still_runs_the_deterministic_checks():
    h = make_harness()
    scorecard = run_evaluation(
        question="q", answer=GOOD_ANSWER, timeline=_FakeTimeline(),
        ledger=None, response_id=None, telemetry=h.telemetry, llm_generate=None)
    assert {r.name for r in scorecard.results} == {
        "grounding_numbers", "mechanism_named", "privacy_floor_respected",
        "figures_are_readable"}
    assert len(h.evaluation_events()) == 4


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


# --------------------------------------------------------------------------- #
# LLM-judge robustness: the scores come from a model, so nothing about them is
# guaranteed. A malformed reply must degrade to a low score, never to a crash
# that loses the run's answer.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("raw_score", "expected"), [
    (1.0, 1.0),
    (0.0, 0.0),
    (0.55, 0.55),
    ("0.9", 0.9),        # models routinely return the number as a string
    (7, 1.0),            # clamped: a judge that "scores out of 10"
    (-3, 0.0),
    (None, 0.0),
    ("high", 0.0),
    ([], 0.0),
])
def test_llm_judge_clamps_and_coerces_whatever_the_model_returns(raw_score, expected):
    payload = json.dumps({
        "relevance": {"score": raw_score, "reason": "r"},
        "hallucination": {"score": raw_score, "reason": "r"},
        "task_completion": {"score": raw_score, "reason": "r"},
    })
    results = LlmJudge(lambda _p: payload).judge(
        question="q", answer="a", timeline_digest="")
    assert [r.score for r in results] == [expected] * 3


@pytest.mark.parametrize(("score", "label"), [
    (1.0, "pass"), (0.7, "pass"), (0.69, "borderline"),
    (0.4, "borderline"), (0.39, "fail"), (0.0, "fail"),
])
def test_label_thresholds_are_the_ones_the_alert_rules_assume(score, label):
    """``turnaround-evalops`` alerts on the label, not the number."""
    assert _label_for(score) == label


@pytest.mark.parametrize("raw", [
    "",
    "I could not evaluate this.",
    "[1, 2, 3]",                      # valid JSON, wrong shape
    '{"relevance": "very good"}',     # right key, wrong value shape
    "```json\n{not: json}\n```",
])
def test_a_malformed_judge_reply_scores_zero_rather_than_raising(raw):
    results = LlmJudge(lambda _p: raw).judge(question="q", answer="a", timeline_digest="")
    assert [r.name for r in results] == ["relevance", "hallucination", "task_completion"]
    assert all(r.score == 0.0 and r.label == "fail" for r in results)


def test_a_judge_that_raises_becomes_one_failed_check_not_a_lost_answer():
    """The judge runs after the answer exists. Losing the answer to a judge
    failure would be the worst possible trade."""
    def _explode(_prompt):
        raise RuntimeError("Vertex 503")

    card = run_evaluation(question="q", answer="SEQ0420 waste 40 core-h, 4.5x",
                          timeline=_FakeTimeline(), response_id=None,
                          llm_generate=_explode)
    names = [r.name for r in card.results]
    assert "llm_judge_error" in names
    assert "grounding_numbers" in names  # the deterministic tier still ran


def test_the_prompt_shown_to_the_judge_carries_the_evidence_it_scores_against():
    """A judge asked to rate 'hallucination' without the tool timeline is just
    guessing; the timeline is what makes the score meaningful."""
    seen = {}

    def _capture(prompt):
        seen["prompt"] = prompt
        return '{"relevance":{"score":1,"reason":""},' \
               '"hallucination":{"score":1,"reason":""},' \
               '"task_completion":{"score":1,"reason":""}}'

    LlmJudge(_capture).judge(question="why is SEQ0420 slipping?",
                             answer="because of the cache",
                             timeline_digest="[grafana] farm_analyst.query_prometheus ok=True sum(x)")
    prompt = seen["prompt"]
    assert "why is SEQ0420 slipping?" in prompt
    assert "because of the cache" in prompt
    assert "query_prometheus" in prompt
    assert "Never reproduce a person's name or a pool name" in prompt


def test_an_explanation_from_a_model_is_truncated_before_it_ships():
    """It becomes a Loki log body on every run; an unbounded model string is a
    cost and a cardinality risk."""
    payload = json.dumps({name: {"score": 0.5, "reason": "x" * 5000}
                          for name in ("relevance", "hallucination", "task_completion")})
    results = LlmJudge(lambda _p: payload).judge(question="q", answer="a", timeline_digest="")
    assert all(len(r.explanation) <= 400 for r in results)
