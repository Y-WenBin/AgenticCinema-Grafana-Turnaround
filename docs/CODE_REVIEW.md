# Code review — 2026-09-09

Standard applied: the *thermo-nuclear code quality review* bar — structural
regressions and missed simplifications first, nits last, and "it works" is not
grounds for approval.

Scope: the local `evalops-hardening` branch (`49d213a`), all of `agent/`,
`bridge/`, `observability/`, `seed/`, `grafana/` and `tests/`.

Verdict up front: **the codebase is in good shape.** The privilege boundaries
are real, the privacy floor is enforced in code rather than in prose, and the
telemetry contract is genuinely shared between the emitter and the dashboards.
Nothing here is a rewrite. What follows is one correctness bug that was live in
the shipped configuration, one structural duplication worth deleting, and four
smaller boundary problems — all fixed, with tests.

Suite: **241 → 358 tests**, all passing. `ruff check .` clean.

---

## 1. Blocker — configuration froze at import, so `.env` was silently ignored

`agent/config.py` read four knobs into module constants at import time:

```python
ANALYST_MODEL  = os.environ.get("TURNAROUND_GEMINI_MODEL", "gemini-2.5-flash")
PRODUCER_MODEL = os.environ.get("TURNAROUND_GEMINI_MODEL_PRO", ANALYST_MODEL)
MAX_LLM_CALLS  = _int_env("TURNAROUND_MAX_LLM_CALLS", 40)
MCP_MODE       = os.environ.get("TURNAROUND_MCP_MODE", "oss").strip().lower()
```

`load_env()` — the function that reads `.env` — runs inside `settings()`, which
is called *after* import. So a value set only in `.env` never reached any of
them. Demonstrated against the repo's own `.env`:

```
.env says   gemini-2.5-pro   max 7   hosted
code used   gemini-2.5-flash max 40  oss
```

Three consequences, in order of severity:

- **R7 was not configurable as documented.** The TESTPLAN and `deploy/README.md`
  both present `TURNAROUND_MAX_LLM_CALLS` as the cost ceiling. Set in `.env`, it
  did nothing. Only an exported shell variable worked — which is why the manual
  check (`TURNAROUND_MAX_LLM_CALLS=1 uv run ...`) passed and hid the bug.
- **Two answers in one process.** `config.MCP_MODE` said `oss` while
  `settings().mcp_mode` said `hosted`, because `settings()` recomputed the same
  value inline. Two sources of truth for a privilege-relevant switch.
- `PRODUCER_MODEL` was dead — defined, never read anywhere.

**Fix.** Deleted all four constants. `Settings` already *is* the resolved
configuration object, and it is built after `load_env()` — the loose constants
were a parallel, broken second channel. `analyst_model` and `max_llm_calls` are
now fields on it, and every call site already had a `Settings` in hand, so no
new plumbing was needed. The inline mode resolution in `settings()` is now the
only one.

**Tests.** New `tests/test_config.py` (36 tests) — the module previously had
none. Two of them are structural guards: one asserts those four names do not
come back, one parses the module's AST and fails if *any* module-level
assignment reads `os.environ`.

## 2. Structural — the CLI and the HTTP endpoint were two copies of the pipeline

`agent/run.py::ask` and `agent/serve.py::ask_endpoint` were ~70% identical:
build the system, instrument, create a session, construct the `Runner`, the same
`_drive()` closure, the same `invoke_agent` wrapper, the same
`LlmCallsLimitExceededError` handling, the same `run_evaluation` block. On top
of that, `serve.py` imported `_instrument` and `_llm_generate_or_none` — two
*private* functions — from the CLI module.

Two copies of a pipeline is two places to fix a bug and two places to forget.
The layering was also backwards: the HTTP tier depended on the CLI's internals.

**Fix.** Extracted `agent/engine.py`: one `answer_question()` returning a
`RunOutcome`. `run.py` is now console text plus an exit code; `serve.py` is JSON
shaping. Neither reaches into the other. Both call through the `engine` module
rather than importing its names, so the layer is visible at each call site.

**Tests.** New `tests/test_engine.py` (24 tests) pinning the shared path and
both front ends against a stubbed `Runner` — no network.

**Found while extracting:** the old `_drive()` returned the answer, so an
exception thrown mid-stream discarded it. A tripped circuit breaker therefore
*always* lost the partial answer, making `run.py`'s exit-0-with-partial-answer
branch unreachable. `_drive()` now assigns as events arrive.

## 3. Structural — the timeline recorder was copy-pasted, and the copies disagreed

`agent/analysts.py` and `agent/remediator.py` each carried their own
`pending: dict[int, object]` keyed on `id(tool_context)`, their own before/after
closures, and their own idea of what "ok" means:

- analysts: `not (isinstance(r, dict) and r.get("isError"))`
- remediator: `not (isinstance(r, dict) and r.get("status") == "blocked")`

Neither recognised the other's failure shape, so a gate-blocked write would have
been recorded `ok` by an analyst and an MCP error `ok` by the remediator. The
analysts also had a silent fallback that invented a timeline row with empty
arguments when an `after` arrived unpaired; the remediator dropped it. The
timeline is the demo's evidence, so those disagreements matter.

**Fix.** One `TimelineRecorder` in `agent/timeline.py` owns the pending map, and
one `failed()` predicate recognises both shapes. The remediator's special case —
closing a blocked call in the *before* hook — falls out for free: `finish()`
consumes the pending entry, so ADK's subsequent after-hook is a no-op rather
than a duplicate row. No `record_unpaired` flag was needed. The
invent-a-row fallback is gone: an unpaired `after` records nothing, because a
row with no arguments is worse evidence than no row.

**Tests.** 12 new cases in `tests/test_timeline.py` covering the pairing rules,
both failure shapes, the double-finish and the unpaired-after.

## 4. Boundary — the ADK plugin kept three structures where one sufficed

`observability/adk.py` paired `execute_tool` spans with a dict keyed on
`id(tool_context)`, *and* a parallel `_tool_stack`, *and* a `_forget()` that
rebuilt the stack on every completion. The `or (self._tool_stack.pop() if ...)`
fallback papered over an invariant the module's own docstring already states:
ADK hands the *same* `ToolContext` to the before and after hooks (which is
exactly why `agent/analysts.py` keys on it). The stack was a fallback for a case
that cannot occur, and it made the code harder to reason about than the thing it
was protecting.

**Fix.** Deleted `_tool_stack` and `_forget`. The dict is authoritative; a
missing entry is an early return. ~12 lines and one concept gone. The docstring
now distinguishes the model hooks (different context objects → a stack) from the
tool hooks (one shared context → identity).

## 5. Boundary — `instrument()`'s guard parameter was a factory-or-value overload

```python
guard = content_guard() if callable(content_guard) and content_guard is _default_guard \
    else content_guard
```

The parameter meant "a factory" only when it was one specific sentinel function,
and "the guard itself" otherwise. The `callable(...)` half was dead — `is
_default_guard` already implies it. Replaced with an explicit `TURNAROUND_GUARD`
sentinel and a one-line resolution. Same behaviour, one fewer trick.

## 6. Latent crash — partial exporter injection in `build_providers`

`observability/providers.py` documented that a caller may inject exporters for
tests, but assumed all-or-nothing. `build_providers(span_exporter=...)` alone
built `PeriodicExportingMetricReader(None)` and died with
`AttributeError: 'NoneType' object has no attribute '_preferred_temporality'`
from three libraries down. Each signal is now wired independently: what you
inject works, what you don't is discarded.

**Tests.** New `tests/test_providers.py` (7 tests) — the module had none. It
also pins that the semantic-convention histogram buckets the EvalOps dashboard
reads are actually registered.

## 7. Robustness — the LLM judge crashed on a right-key/wrong-shape reply

`LlmJudge.judge` did `item = data.get(name) or {}` then `item.get("score")`. A
model returning `{"relevance": "very good"}` — plausible, and models do this —
made `item` a truthy string, and `.get` raised `AttributeError`, which the
surrounding `except (TypeError, ValueError)` did not catch. The whole LLM tier
then collapsed into one `llm_judge_error` instead of three scored dimensions.

Fixed with an explicit `isinstance(item, dict)` check, and 25 new cases in
`tests/test_evaluation.py` covering score clamping (`7` → 1.0, `-3` → 0.0,
`"0.9"` → 0.9), the `_label_for` thresholds the alert rules depend on, malformed
and fenced replies, a judge that raises, and explanation truncation.

## 8. Cross-module drift had no guard at all

The same tool names live in four lists across three modules: the per-analyst
`tool_filter` (`agent/mcp_grafana.py`), the gate's `WRITE_TOOLS`
(`agent/approval.py`), and `_MCP_GRAFANA_TOOLS` (`agent/timeline.py`). Each was
correct; nothing checked that they agree. Drift here is silent — adding a tool
to an analyst without teaching the timeline does not break a run, it quietly
stops marking that call as a Grafana call, which weakens the one claim the demo
rests on. New `tests/test_contracts.py` (15 tests) joins them: every reachable
tool is timeline-recognised, every remediator write tool is gated, no analyst
filter intersects `WRITE_TOOLS`, only the FarmAnalyst holds Tempo.

## 9. The offline suite was not actually offline

`tests/TESTPLAN.md` claims "no network, no credentials". True only because
nothing exercised the run path. The moment `test_engine.py` did,
`engine.answer_question(evaluate=True)` built a real `genai.Client` and made a
**live, billable Vertex call from the unit suite** on any machine with a
filled-in `.env`. An autouse fixture in `tests/conftest.py` now refuses to
construct a live Vertex client; a test wanting the LLM judge injects its own
`generate`.

## 10. A Vertex 429 was an unhandled traceback — found live

Running the four demo questions back to back exhausted the project's per-minute
Gemini quota. The third question exited 1 with a forty-line ADK traceback ending
in `google.adk.models.google_llm._ResourceExhaustedError`. On a hackathon-scale
project this is the *likeliest* live failure there is — and `agent/config.py`'s
own comments already note that this project sees 429s.

The run path caught its own circuit breaker and hosted-MCP auth failures, but
nothing model-side.

**Fix.** Rather than adding a second boolean beside `circuit_breaker_tripped`, a
run now stops with one typed `Halt(kind, detail, advice)`:
`circuit_breaker` | `model_quota` | `model_error`. `circuit_breaker_tripped`
stays as a derived property because it is part of the published `/ask` contract
and means something different (our cost guard doing its job, not a provider
refusal). The CLI prints the halt as one sentence and exits 3 if there is
nothing to show; `/ask` gains `halted_by` and `halt_detail`, so a quota refusal
is no longer indistinguishable from "the agent had nothing to say".

## Deliberately not done

- **Pinning the ruff rule set.** `pyproject.toml` sets only `line-length`, so
  `uv run ruff check .` means "whatever this ruff version defaults to", and the
  dev dependency is `ruff>=0.8`. The gate genuinely drifts with the toolchain.
  An explicit `select` matching the current defaults surfaced 77 further
  findings (mostly `E501` and `PT018` in tests) — real churn, no real risk, days
  before judging. Worth doing after the deadline, not now.
- **Deriving `_MCP_GRAFANA_TOOLS` from the filters.** The clean version moves the
  canonical names into a dependency-free module both import. `agent/timeline.py`
  is deliberately ADK-free, so importing `agent.mcp_grafana` would break that.
  The drift test in §8 buys the same protection at a fraction of the risk.

---

## Live verification — 2026-09-09

Against Grafana Cloud stack `niftysamosa2162` (`prod-ap-southeast-1`) and Vertex
project `silent-presence-466905-f5` (`us-central1`), after `seed.populate`.

| Part 6 | Result |
|---|---|
| 1. Four demo questions cold | **pass** — 4/4, **24/24 judge checks pass**, `hallucination` 1.00 on all four, privacy floor held every time |
| 2. FarmAnalyst uses Tempo | **pass** — `tempo_traceql-search` in all four timelines |
| 3. Trace in Tempo | **pass** — 1 `invoke_agent producer` trace, 8 matching spans |
| 4. Eval events in Loki | **pass** — 6 `gen_ai.evaluation.result` records, correlated by `response_id`, `privacy_floor_respected=pass` |
| 5. Write-back path | **pass** — both writes APPROVED through the gate, JSONL row appended, 2 annotations landed |
| 6. EvalOps surface | **pass** — 4 dashboards, 2 alert groups, 3 ML jobs; all 4 rules `health=ok` |
| 7. Circuit breaker | **pass** — exit 3, one sentence, no traceback |
| 8. Hosted MCP OAuth | **not run** — needs an interactive browser flow |
| 9. Cloud Run deploy | **blocked** — no `gcloud` and no container runtime on this machine |

Two things a reviewer should know:

- **The render-waste alert fires, and should.** SEQ0420 carries ~44 core-hours
  of waste against ~0.7 elsewhere. The TESTPLAN previously said all rules
  evaluate `inactive`; corrected.
- **The demo has a ~2-hour shelf life.** History is compressed into a
  ~45-minute window ending at seed time, and every query reads it with
  `last_over_time((...)[2h:])`. The stack was 3 days stale when this review
  started and answered *every* query with nothing — which presents as a broken
  agent, not as stale data. `seed/refresh.py` exists for exactly this;
  the TESTPLAN now says so in bold.
