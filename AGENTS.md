# Orientation

Read this first if you are a coding agent or a new contributor. It is the
shortest path to being useful in this repo without breaking something load-bearing.

**Turnaround** joins a studio's creative schedule (tracker) to its render farm
inside Grafana Cloud, and puts a Gemini/ADK multi-agent system in front of it
that answers a producer's question with evidence and a proposed fix. One idea
carries the whole product: **the render farm's job name already contains the shot
id**, so farm telemetry can be relabelled onto `shot_id` / `sequence` /
`department` and queried alongside the schedule.

- **Why it exists and what the numbers mean** → [`README.md`](README.md)
- **How it is built and why** → [`PROJECT.md`](PROJECT.md)
- **How to stand it up** → [`docs/SETUP.md`](docs/SETUP.md)
- **What the tests guarantee** → [`tests/TESTPLAN.md`](tests/TESTPLAN.md)

---

## Invariants — do not break these

These are enforced by tests. If a change makes one fail, the change is wrong, not
the test.

1. **The privacy floor is absolute.** A pool of fewer than three people
   (`di-pool-1`, 2 people) must never appear in a metric label, an answer, an
   evaluation payload, captured content, or the evidence chain. No per-person
   output metric may exist at all. `bridge/privacy.py`, `tests/test_privacy.py`.
2. **Analysts are read-only by construction.** They are wired to a
   `mcp-grafana` started `--disable-write`, not merely told to behave. No analyst
   `tool_filter` may intersect the approval gate's `WRITE_TOOLS`.
   `tests/test_contracts.py`.
3. **Every mutating call passes the approval gate.** The Remediator is the only
   write-capable agent, and `ApprovalGate.before_tool` intercepts each call.
   `agent/approval.py`, `tests/test_approval.py`.
4. **Only Gemini on Vertex at runtime.** No non-Google AI SDK may appear on the
   runtime import path, and no path to the public Generative Language API.
   `tests/test_reproducibility.py::test_no_non_google_ai_sdk_on_the_runtime_path`.
5. **Runtime configuration resolves after `.env` loads.** Never read
   `os.environ` in a module-level assignment — an AST guard in
   `tests/test_config.py` fails the build if you do. Add settings to
   `Settings` in `agent/config.py`; that is the only channel.
6. **The suite is offline.** No network, no credentials, no `mcp-grafana`
   binary, no `.env`. Autouse fixtures in `tests/conftest.py` refuse to build a
   live Vertex client and point `load_env` at an empty directory, so a
   developer's real `.env` can never make a test pass. A test that wants the LLM
   judge injects its own `generate`.
7. **Bounded cost.** Every run passes `RunConfig(max_llm_calls=…)`. ADK's own
   default is 500, which is enough for a stuck retry loop to cost real money.
8. **The ontology is defined once.** `bridge/ontology.py` owns metric names,
   attribute names and status vocabulary; `agent/vocabulary.py` renders them into
   prompts. Drift between them is caught by `tests/test_vocabulary.py` and
   `tests/test_contracts.py` — do not hard-code a metric name anywhere else.

---

## Layer map

Data flows left to right. Each layer depends only on the ones to its left.

```
bridge/          seed/            grafana/          agent/           observability/
studio tools  →  the simulated →  dashboards,    →  the ADK      +   the agent
→ OTLP           show Nightfall   alerts, ML        pipeline         watching itself
```

| Directory | Owns | Depends on |
|---|---|---|
| `bridge/` | The ontology, the farm-name join, the privacy floor, OTLP emission, `.env` loading | nothing in this repo |
| `seed/` | The generated show; the driver that pushes it into Grafana Cloud | `bridge/` |
| `grafana/` | Dashboards, alert rules, ML jobs, and one idempotent provisioner | `bridge/` (metric names) |
| `agent/` | The multi-agent pipeline, MCP toolsets, approval gate, judge tier | `bridge/`, `observability/` |
| `observability/` | `gen_ai.*` telemetry for the agent tier | **nothing** — standalone by design; the PII guard is injected |

**`observability/` importing `agent/` or `bridge/` is a design break**, not a
convenience. It is a reusable package that happens to live here.

### Inside `agent/`

`engine.py` is the one run path: build → instrument → drive under the circuit
breaker → score, returning a `RunOutcome`. `run.py` (CLI) and `serve.py` (HTTP)
are **presentations only** — text and JSON respectively. Neither reaches into the
other, and new run behaviour belongs in `engine.py`, never in a front end.

They call `engine.resolved_settings()` / `engine.answer_question()` *through the
module* rather than importing the names. That is deliberate: it gives tests one
patch point and names the layer at each call site. Keep it that way.

---

## Commands

```bash
uv sync --group dev
uv run pytest -q          # the full suite, offline
uv run ruff check .
```

Anything live needs a filled-in `.env` (see `.env.example` and `docs/SETUP.md`).
Every entry point loads it itself, via `bridge/dotenv.py`; a variable already
exported in the shell always wins.

```bash
uv run python -m seed.populate --dry-run     # exercises the full write path, no network
uv run python -m seed.populate               # writes several thousand series
uv run python -m grafana.provision           # idempotent
uv run python -m agent.run "Why is SEQ0420 slipping, and what is it costing in artist-days?"
```

Exit codes: **0** answered · **2** unconfigured (message names the missing keys)
· **3** halted with nothing to show.

---

## Gotchas that will waste an hour

- **The seeded data has a ~2-hour shelf life.** History is compressed into a
  ~45-minute window ending at seed time, and queries read it with
  `last_over_time((…)[2h:])`. A stale stack answers *every* query with nothing
  and the agent looks broken. **Re-seed before any live run.**
- **A bare instant PromQL query at `now` returns nothing** for the same reason.
  Wrap current-state expressions in `last_over_time((<expr>)[2h:])`.
- **Range selectors are proportional on the compressed base.** `[14d]` in real
  calendar time is about `[5m]` after the time warp.
- **A live Vertex 429 is expected under load.** Four questions back to back can
  exhaust a small project's per-minute Gemini quota. It surfaces as
  `Halt(kind="model_quota")` — one sentence, exit 3. Wait a minute and re-run.
- **`mcp-grafana` is a separate binary**, not a Python dependency:
  `brew install mcp-grafana` (1.3.0). `agent/config.py` finds it on `PATH` or at
  `/opt/homebrew/bin`.
- **`mcp` must stay `<2`.** ADK 2.8 predates the mcp 2.x API rename.
- **`SequentialAgent` shows a deprecation warning.** Its replacement, `Workflow`,
  cannot yet be used as an `LlmAgent` sub-agent, so the warning is expected and
  the current shape is correct.

---

## Conventions

- Docstrings explain **why**, not what — the *what* is readable from the code. A
  docstring that records a bug found the hard way is worth more than one that
  restates the signature.
- Prefer deleting a structure to adding a flag to it. Two structures that must be
  kept in step are a defect waiting to happen; the cross-module contracts in
  `tests/test_contracts.py` exist because of exactly that.
- Test names are sentences that state the guarantee
  (`test_a_second_finish_for_the_same_call_is_a_no_op`), so a failure report
  reads as a specification.
- Reference documentation sections by **name**, not number, so renumbering does
  not silently break the pointer.
