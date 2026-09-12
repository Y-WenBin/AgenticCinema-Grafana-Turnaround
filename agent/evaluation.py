"""The judge tier: score the Producer's answer and emit ``gen_ai.evaluation.result``.

The proposal's competitive claim against LangSmith / Arize Phoenix is a
continuous evaluation loop wired into the same telemetry stream as the traces.
Turnaround can make that claim honestly *and* strongly, because the seed is
deterministic with known ground truth:

* :class:`DeterministicJudge` -- cheap, exact, no model. Checks the synthesised
  answer against what the show is known to contain, and enforces the privacy
  invariant (a sub-floor pool must never be named). This doubles as a regression
  guard: if a refactor lets ``di-pool-1`` leak into an answer, a test goes red.
* :class:`LlmJudge` -- a second Gemini instance, no tools, scoring grounding /
  relevance / task completion against the tool timeline it is shown. Injected
  ``generate`` callable so tests run without Vertex.

Each check becomes one ``gen_ai.evaluation.result`` log event
(``observability.genai.GenAiTelemetry.emit_evaluation``), correlated to the run
by ``gen_ai.response.id`` so a low score in Loki pivots straight to the trace.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent.vocabulary import FLOOR

if TYPE_CHECKING:
    from agent.approval import EvidenceLedger
    from agent.timeline import ToolTimeline
    from observability.genai import GenAiTelemetry

# --------------------------------------------------------------------------- #
# Known ground truth. Measured from the generated history rather than asserted
# in the config -- `seed/model.py` applies the perturbations declared in
# `seed/show.yaml` and these are what falls out, pinned by `tests/test_show.py`
# and `tests/test_populate.py`. Bands rather than values, because dates are
# relative to seeding time and the figures drift a little run to run.
# --------------------------------------------------------------------------- #

CRUNCH_SEQUENCE = "SEQ0420"
#: pool deliberately below the floor -- must never appear in an answer
SUBFLOOR_POOL = "di-pool-1"
JOIN_RATIO_BAND = (3.0, 6.0)          # SEQ0420 core-h per comp iteration vs ~2.2 elsewhere
MECHANISM_TERMS = ("frame 118", "lighting cache", "cache regression", "cache invalidation")

# The grounded answer cites specific measured figures rather than "significantly
# more". A run legitimately varies which it leads with -- the join ratio, the raw
# render-waste core-hours, the frame-failure rate, the crew hours, the artist-day
# cost -- so grounding_numbers passes on ANY TWO of these anchors, not one
# specific number. (Live testing: an answer giving waste 40 core-h + 9.2% + 56
# artist-days but no ratio was wrongly failing a ratio-only check.)
_MULTIPLIER_RE = re.compile(r"\d+(?:\.\d+)?\s*[x×]")  # "4x", "4.1 ×"


def _grounding_anchors(answer: str) -> list[str]:
    low = answer.lower()
    nums = _numbers(answer)
    hits: list[str] = []
    lo, hi = JOIN_RATIO_BAND
    if any(lo <= n <= hi for n in nums) or _MULTIPLIER_RE.search(low):
        hits.append("join-ratio")
    if any(15 <= n <= 130 for n in nums) and ("core-h" in low or "core h" in low
                                              or "core-hour" in low or "waste" in low):
        hits.append("render-waste")
    if "%" in answer and any(3 <= n <= 20 for n in nums):
        hits.append("frame-failure-rate")
    if "artist-day" in low or "artist day" in low:
        hits.append("artist-day-cost")
    if any(55 <= n <= 110 for n in nums) and ("h/" in low or " h " in low
                                              or "hour" in low or "week" in low):
        hits.append("crew-hours")
    return hits


@dataclass(slots=True)
class EvalResult:
    name: str
    score: float           # 0..1
    label: str             # pass | fail | relevant | ...
    explanation: str
    actor_type: str = "ai"  # deterministic | ai | human


# --------------------------------------------------------------------------- #
# Deterministic judge
# --------------------------------------------------------------------------- #


_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")

#: Three decimals or more in a producer-facing answer. Two is the most any
#: figure in this domain earns; beyond that the number is a float that escaped
#: rather than a measurement. The recipes in ``agent/vocabulary.py`` round in
#: PromQL so the long form never reaches the model -- this is the check that
#: notices when a new query forgets to, which is otherwise invisible until a
#: demo puts ``63.16190476190476h`` on a screen.
_LONG_FLOAT_RE = re.compile(r"\d+\.\d{3,}")


def _numbers(text: str) -> list[float]:
    return [float(m) for m in _NUMBER_RE.findall(text)]


@dataclass(slots=True)
class DeterministicJudge:
    """Ground-truth and privacy checks over the final answer + evidence chain."""

    join_band: tuple[float, float] = JOIN_RATIO_BAND

    def judge(self, *, question: str, answer: str,
              ledger_chain: list[str] | None = None) -> list[EvalResult]:
        answer_l = answer.lower()
        chain = ledger_chain or []
        results: list[EvalResult] = []

        # 1. grounding: the crunch sequence named, and >= 2 measured anchors
        has_seq = CRUNCH_SEQUENCE.lower() in answer_l
        anchors = _grounding_anchors(answer)
        grounded = has_seq and len(anchors) >= 2
        results.append(EvalResult(
            name="grounding_numbers",
            score=1.0 if grounded else 0.0,
            label="pass" if grounded else "fail",
            explanation=(
                f"names {CRUNCH_SEQUENCE} and cites {anchors}" if grounded else
                f"expected {CRUNCH_SEQUENCE} + >=2 measured anchors; "
                f"got seq={has_seq}, anchors={anchors}"
            ),
            actor_type="deterministic",
        ))

        # 2. mechanism named: the answer points at the farm-side cause
        named = next((t for t in MECHANISM_TERMS if t in answer_l), None)
        results.append(EvalResult(
            name="mechanism_named",
            score=1.0 if named else 0.0,
            label="pass" if named else "fail",
            explanation=(f"cites {named!r}" if named
                         else f"none of {MECHANISM_TERMS} in the answer"),
            actor_type="deterministic",
        ))

        # 3. privacy floor: no sub-floor pool anywhere in the answer or evidence
        haystack = " ".join([answer_l, *(c.lower() for c in chain)])
        leaked = SUBFLOOR_POOL in haystack
        results.append(EvalResult(
            name="privacy_floor_respected",
            score=0.0 if leaked else 1.0,
            label="fail" if leaked else "pass",
            explanation=(
                f"{SUBFLOOR_POOL!r} (a {FLOOR}-minus-person pool) appears in the "
                "answer or evidence chain" if leaked else
                f"no pool below the floor of {FLOOR} is named"
            ),
            actor_type="deterministic",
        ))

        # 4. presentation: no raw float survived into the answer
        long_floats = _LONG_FLOAT_RE.findall(answer)
        results.append(EvalResult(
            name="figures_are_readable",
            score=0.0 if long_floats else 1.0,
            label="fail" if long_floats else "pass",
            explanation=(
                f"{len(long_floats)} figure(s) carry three or more decimals "
                f"({', '.join(long_floats[:3])}) -- round in the PromQL, not in the prompt"
                if long_floats else "every figure is quoted to two decimals or fewer"
            ),
            actor_type="deterministic",
        ))
        return results


# --------------------------------------------------------------------------- #
# LLM judge
# --------------------------------------------------------------------------- #

_JUDGE_INSTRUCTION = """\
You are an independent evaluator. You did NOT produce the answer below; you are
scoring it. You are given the supervisor's question, the answer a multi-agent
system gave, and the list of Grafana tool calls it made to get there.

Score each dimension from 0.0 to 1.0 and give a one-sentence reason:
- relevance: does the answer address the question asked?
- hallucination: is every quantitative claim in the answer supported by a tool
  call in the timeline? 1.0 = fully grounded, 0.0 = key numbers have no query
  behind them.
- task_completion: does the answer actually resolve the ask (a decision, not a
  description)?

Never reproduce a person's name or a pool name. Reply with ONLY a JSON object:
{"relevance": {"score": 0.0, "reason": ""},
 "hallucination": {"score": 0.0, "reason": ""},
 "task_completion": {"score": 0.0, "reason": ""}}
"""


def _label_for(score: float) -> str:
    return "pass" if score >= 0.7 else ("borderline" if score >= 0.4 else "fail")


@dataclass(slots=True)
class LlmJudge:
    """Second Gemini as evaluator. ``generate`` maps a prompt to raw model text."""

    generate: Callable[[str], str]

    def judge(self, *, question: str, answer: str, timeline_digest: str) -> list[EvalResult]:
        prompt = (
            f"{_JUDGE_INSTRUCTION}\n\n"
            f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n\n"
            f"TOOL TIMELINE:\n{timeline_digest}\n"
        )
        raw = self.generate(prompt)
        data = _parse_json_object(raw)
        out: list[EvalResult] = []
        for name in ("relevance", "hallucination", "task_completion"):
            # Everything here comes from a model, so nothing about the shape is
            # guaranteed: the key may be missing, hold a bare string instead of
            # the {"score", "reason"} object, or carry a score of "high" or 7.
            # Every one of those is a zero, never an exception -- a judge that
            # raises would cost the run the answer it had already produced.
            item = data.get(name)
            if not isinstance(item, dict):
                item = {}
            try:
                score = max(0.0, min(1.0, float(item.get("score"))))
            except (TypeError, ValueError):
                score = 0.0
            out.append(EvalResult(
                name=name, score=score, label=_label_for(score),
                explanation=str(item.get("reason", ""))[:400], actor_type="ai",
            ))
        return out


def _parse_json_object(text: str) -> dict:
    """Best-effort: models wrap JSON in prose or ```json fences."""
    text = text.strip()
    fence = re.search(r"\{.*\}", text, re.DOTALL)
    if fence:
        text = fence.group(0)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def vertex_generator(model: str, thinking_budget: int = 0) -> Callable[[str], str]:
    """A ``generate`` backed by Vertex Gemini. Lazy so imports stay cheap.

    The judge gets the same thinking budget as the rest of the pipeline
    (``agent/thinking.py``). It is the *last* model call in a run, with the
    supervisor already waiting on an answer that is already finished, so seconds
    spent here are the most visible seconds in the whole request.

    Judging *is* a reasoning task, so unlike the analysts this tier was measured
    on its own -- same answer, same timeline digest, five runs each:

        budget    0   1.5s   hallucination 1.0, 0.8, 1.0, 0.8, 0.8
        budget 1024   5.8s   hallucination 0.5, 0.5, 0.5, 0.5, 0.6

    Thinking does not make the judge *better* here, it makes it stricter and
    four seconds slower. That matters beyond latency: the alert in
    ``grafana/alerts/rules.json`` fires when the mean hallucination score over
    an hour drops below 0.80, so a thinking judge would hold this stack
    permanently in alarm. Raising the budget means re-calibrating that
    threshold, not just paying for the time.

    The regression guard is ``DeterministicJudge`` either way: it is code
    checking the answer against known ground truth, with no model in it, and no
    budget can move it.
    """
    from google import genai

    from agent.thinking import generate_config

    client = genai.Client()  # picks up GOOGLE_GENAI_USE_VERTEXAI + project from env
    config = generate_config(thinking_budget)

    def _generate(prompt: str) -> str:
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
        return resp.text or ""

    return _generate


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Scorecard:
    results: list[EvalResult] = field(default_factory=list)

    @property
    def failed(self) -> list[EvalResult]:
        return [r for r in self.results if r.label == "fail"]

    def render(self) -> str:
        if not self.results:
            return "(no evaluation)"
        rows = [f"{'evaluation':<26} {'score':>6}  {'label':<10} actor    explanation"]
        rows.append("-" * 96)
        for r in self.results:
            rows.append(f"{r.name:<26} {r.score:>6.2f}  {r.label:<10} "
                        f"{r.actor_type:<8} {r.explanation[:60]}")
        rows.append("-" * 96)
        n_fail = len(self.failed)
        rows.append(f"{len(self.results)} checks, {n_fail} failed"
                    + ("  <-- privacy floor breached" if any(
                        r.name == "privacy_floor_respected" and r.label == "fail"
                        for r in self.results) else ""))
        return "\n".join(rows)


def run_evaluation(
    *,
    question: str,
    answer: str,
    timeline: ToolTimeline,
    ledger: EvidenceLedger | None = None,
    response_id: str | None,
    telemetry: GenAiTelemetry | None = None,
    llm_generate: Callable[[str], str] | None = None,
) -> Scorecard:
    """Run the deterministic judge (always) and the LLM judge (if a generator is
    given), emit each check as a ``gen_ai.evaluation.result`` event, and return
    the scorecard for the console."""
    chain = ledger.chain() if ledger is not None else []
    results = DeterministicJudge().judge(question=question, answer=answer, ledger_chain=chain)

    if llm_generate is not None:
        digest = _timeline_digest(timeline)
        try:
            results += LlmJudge(llm_generate).judge(
                question=question, answer=answer, timeline_digest=digest)
        except Exception as exc:  # noqa: BLE001 -- a judge failure must not fail the run
            results.append(EvalResult(
                name="llm_judge_error", score=0.0, label="fail",
                explanation=f"LLM judge did not complete: {exc}", actor_type="ai"))

    if telemetry is not None:
        for r in results:
            telemetry.emit_evaluation(
                name=r.name, score_value=r.score, score_label=r.label,
                explanation=r.explanation, response_id=response_id,
                actor_type=r.actor_type,
            )
    return Scorecard(results)


def _timeline_digest(timeline: ToolTimeline) -> str:
    lines = []
    for c in timeline.calls:
        mark = "grafana" if c.is_grafana_mcp else "local"
        args = c.args.get("expr") or c.args.get("logql") or ""
        lines.append(f"[{mark}] {c.agent}.{c.tool} ok={c.ok} {args}".rstrip())
    return "\n".join(lines) or "(no tool calls)"
