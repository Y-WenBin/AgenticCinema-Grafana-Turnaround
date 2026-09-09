# Turnaround — test plan for reproducible results

This is the contract the test suite enforces so a reviewer can run the project
and get the same conclusions we did. It also maps the hackathon's suggested
unit-test checklist onto Turnaround's actual architecture, and marks what each
row is: **auto** (a pytest test), **manual** (a live-stack step), or **design**
(a guarantee the shape of the code makes, verified by an auto test).

Run everything:

```bash
uv sync --group dev
uv run pytest -q          # 360 tests, no network, no credentials
uv run ruff check .       # clean
```

The suite is offline by contract: no Vertex, no Grafana Cloud, no `mcp-grafana`
binary, no `.env`. `tests/conftest.py` enforces the Vertex half with an autouse
fixture that refuses to build a live `genai.Client` — a test that wants the LLM
judge injects its own `generate`. Anything needing the live stack is in Part 6
and is marked **manual**.

---

## Part 0 — where the coverage lives

| Area | Test file(s) |
|---|---|
| Reproducibility invariants R1–R7 | `tests/test_reproducibility.py` |
| Runtime config, `.env` resolution, startup validation | `tests/test_config.py` |
| The one shared run path + both front ends (CLI, HTTP) | `tests/test_engine.py` |
| Cross-module tool-name contracts (filters ↔ gate ↔ timeline ↔ prompts) | `tests/test_contracts.py` |
| The OpenCue join / ontology / studio conventions | `tests/test_ontology.py`, `tests/test_conventions.py`, `tests/test_show.py` |
| Editorial ingest (EDL / OTIO) | `tests/test_editorial.py` |
| Privacy floor | `tests/test_privacy.py`, `tests/test_evaluation.py`, `tests/test_reproducibility.py` |
| Seed determinism + measured story | `tests/test_show.py`, `tests/test_populate.py` |
| Emitter / metrics / annotations / timewarp | `tests/test_emit.py`, `tests/test_metrics.py`, `tests/test_annotate.py`, `tests/test_timewarp.py` |
| Agent pipeline shape + privilege boundaries | `tests/test_agent_build.py` |
| Grafana MCP wiring (both modes) | `tests/test_mcp_grafana.py` |
| Approval gate | `tests/test_approval.py` |
| Tool timeline + the before/after recorder | `tests/test_timeline.py` |
| Agent-tier GenAI telemetry | `tests/test_observability.py`, `tests/test_providers.py` |
| Judge tier (deterministic + LLM, incl. malformed replies) | `tests/test_evaluation.py` |
| EvalOps dashboard + alert contract | `tests/test_evalops_grafana.py` |
| Vocabulary ↔ ontology drift | `tests/test_vocabulary.py` |
| Kitsu write-back | `tests/test_writeback.py` |

---

## Part 1 — Reproducibility invariants

A large language model is stochastic, so "reproducible" here does **not** mean
byte-identical answers. It means: every run lands inside the same measured
ground-truth bands, never breaches the privacy floor, uses only the permitted
model, and emits the same telemetry contract. Those are the invariants below.

| # | Invariant | Enforced by | Test |
|---|---|---|---|
| R1 | **Deterministic seed.** Same `seed/show.yaml` → identical metrics, logs, traces. The figures in `seed/story.md` are *measured from the generated data*, never hard-coded. | `seed/model.py` fixed RNG | `tests/test_populate.py`, `tests/test_reproducibility.py::test_seed_is_deterministic` |
| R2 | **Ground truth is a band, not a point.** Dates are relative to seed time, so the join ratio / render waste / failure rate drift a little run to run within known ranges. The deterministic judge encodes those ranges. | `agent/evaluation.py` (`JOIN_RATIO_BAND`, `_grounding_anchors`) | `tests/test_evaluation.py` |
| R3 | **Privacy floor is absolute.** A pool of < 3 people (`di-pool-1`, 2 people) never appears in an answer, an evaluation payload, captured content, or the evidence chain — every run, zero tolerance. | `bridge/privacy.py` (`MIN_POOL_SIZE=3`, `assert_no_pii`), `agent/evaluation.py` (`privacy_floor_respected`), analyst prompt | `tests/test_privacy.py`, `tests/test_evaluation.py::test_deterministic_judge_catches_a_subfloor_pool_leak_*`, `tests/test_reproducibility.py::test_privacy_floor_is_enforced_on_every_answer_surface` |
| R4 | **Model constraint.** Only Gemini on Vertex at runtime (`gemini-2.5-flash`); no non-Google AI SDK on the runtime path, and no path to the public Generative Language API. | `agent/config.py` (`bootstrap_vertex` forces `GOOGLE_GENAI_USE_VERTEXAI=TRUE`; the model is `Settings.analyst_model`, resolved after `.env` loads) | `tests/test_reproducibility.py::test_no_non_google_ai_sdk_on_the_runtime_path`, `tests/test_config.py::test_bootstrap_forces_vertex_and_never_offers_the_public_api` |
| R5 | **Telemetry contract.** The `gen_ai.*` span / metric / event names the EvalOps dashboard and alerts query are exactly what `observability/genai.py` emits. | shared constants in `observability/genai.py` | `tests/test_evalops_grafana.py`, `tests/test_observability.py` |
| R6 | **Offline assembly.** The whole agent pipeline builds with no credentials and no network, so CI is deterministic. No test may open a live Vertex client. | `agent/producer.py` lazy `root_agent`; injected exporters in `observability/providers.py`; the autouse guard in `tests/conftest.py` | `tests/test_agent_build.py`, `tests/test_providers.py` |
| R7 | **Bounded cost.** A run cannot exceed `TURNAROUND_MAX_LLM_CALLS` (default 40) Gemini calls, so token spend per question is bounded and repeatable — and the ceiling is honoured whether it is exported or set in `.env`. | `Settings.max_llm_calls` → `RunConfig(max_llm_calls=...)` in `agent/engine.py`, the one run path both front ends use | `tests/test_reproducibility.py::test_circuit_breaker_is_wired`, `tests/test_engine.py::test_the_ceiling_passed_to_adk_is_the_resolved_one`, `tests/test_config.py::test_dotenv_actually_drives_the_model_the_ceiling_and_the_mode` |

---

## Part 2 — Grafana MCP integration

Turnaround's only tool surface is `grafana/mcp-grafana`, in two modes
(`agent/mcp_grafana.py`): **oss** (default, deployed) — a stdio subprocess
started `--disable-write`; **hosted** — `https://mcp.grafana.com/mcp` over
Streamable HTTP with an OAuth 2.1 bearer.

| Case | Kind | What it asserts | Test |
|---|---|---|---|
| MCP toolset is really wired, not stubbed | auto | `analyst_toolset` / `remediator_toolset` return a real `google.adk...McpToolset` with `StdioConnectionParams` / `StreamableHTTPConnectionParams` — the partner MCP is imported and instantiated in the code path a run takes. | `test_reproducibility.py::test_grafana_mcp_is_imported_and_instantiated`, `test_mcp_grafana.py` |
| Agent invokes a tool and the payload is parsed into context | auto | Driving the `before_tool`/`after_tool` callbacks with a mock `query_prometheus` payload records the call as a Grafana MCP call, keeps the PromQL in `args`, and derives `ok` from the payload shape (`isError`). This is the seam where a real MCP response enters the agent's reasoning/evidence context. | `test_timeline.py`, `test_reproducibility.py::test_mock_mcp_payload_is_classified_and_recorded` |
| **Edge — auth failure / expired OAuth / 403 mid-session** | auto | Hosted mode with no bearer raises `HostedMcpNotAuthorized` pointing at `agent.mcp_login`; `agent/run.py` and `agent/serve.py` catch it and return a re-authorize message + non-zero exit / JSON `error` — not a traceback. | `test_mcp_grafana.py::test_hosted_without_a_token_fails_with_a_pointer_to_the_login_flow`, `test_reproducibility.py::test_auth_failure_is_handled_gracefully` |
| **Edge — subprocess env hygiene** | auto | The stdio subprocess does not inherit the parent's `OTEL_*` (would spray handshake errors); `OTEL_SDK_DISABLED=true` is pinned. | `test_mcp_grafana.py::test_oss_subprocess_env_drops_otel_and_pins_grafana` |
| **Edge — tool-name drift across modules** | auto | Every tool in any analyst's or the Remediator's filter is recognised by the timeline as a Grafana MCP call, and every Remediator write tool is in the approval gate's `WRITE_TOOLS`. These four lists live in three modules; drift between them is silent — it does not break a run, it quietly stops marking calls as Grafana calls (weakening the demo's central claim) or lets a write past the gate. | `test_contracts.py` |
| **Edge — read/write privilege split holds in hosted mode too** | auto | No analyst filter intersects `WRITE_TOOLS`. In oss mode `--disable-write` also enforces this; in hosted mode the filter is the *only* thing that does. | `test_contracts.py::test_no_analyst_can_reach_a_write_tool` |
| Config resolution: mode, model and ceiling come from `.env` | auto | `TURNAROUND_MCP_MODE` / `_GEMINI_MODEL` / `_MAX_LLM_CALLS` set only in `.env` reach `Settings`. They were module constants read at import — before `load_env()` — so `.env` set them and nothing used them. An unrecognised mode falls back to `oss` (the read-only, unattended path), never to `hosted`. | `test_config.py` |
| **Hackathon alignment — runtime use of the partner MCP** | manual + auto | Live: `uv run python -m agent.run "why is SEQ0420 slipping?"` prints a tool timeline with `*`-marked `query_prometheus` / `query_loki_logs` / `tempo_*` calls against Grafana Cloud, and an `invoke_agent` trace with `execute_tool` children in Tempo. Auto guard: the import-and-instantiate test above. | see Part 6 |

---

## Part 3 — Gemini agent multi-step orchestration

The pipeline is a fixed `SequentialAgent`: `schedule_analyst → farm_analyst →
crunch_guardian → remediator(gated) → synthesis`. It is deterministic *by shape*
— Flash is an unreliable free-form orchestrator, so the ordering is not
model-decided (`agent/producer.py`).

| Case | Kind | What it asserts | Test |
|---|---|---|---|
| Deterministic multi-step execution: plan → act (call Grafana) → format | design + auto | The root agent is a five-step `SequentialAgent` in exactly that order; each analyst writes a distinct `output_key`; `synthesis` has no tools and composes from state. A static question therefore always runs plan (fixed), act (analyst MCP calls), format (`synthesis`). | `test_agent_build.py::test_pipeline_is_a_fixed_five_step_sequence`, `::test_synthesis_has_no_tools`, `test_reproducibility.py::test_synthesis_prompt_only_consumes_specialist_state` |
| **Edge — tool hallucination** | design + auto | Two layers: (1) analysts are read-only *by construction* (`--disable-write` server), so a hallucinated write tool has nothing to call; (2) each analyst's `tool_filter` is a superset of every tool its prompt names, so the model is never steered toward a tool ADK would reject. The residual `ValueError` path (model invents a name outside the filter) is documented in `agent/mcp_grafana.py` and contained by the circuit breaker below. | `test_reproducibility.py::test_prompts_never_point_at_an_unfilterable_tool`, `test_agent_build.py::test_analyst_tool_filters_are_read_only_subsets` |
| **Edge — infinite loop / runaway token consumption** | auto | `RunConfig(max_llm_calls=TURNAROUND_MAX_LLM_CALLS)` is passed on every `runner.run_async`; `LlmCallsLimitExceededError` is caught in `run.py` (exit 3 + note) and `serve.py` (`circuit_breaker_tripped: true`). The `SequentialAgent` itself cannot loop — each sub-agent runs exactly once. | `test_reproducibility.py::test_circuit_breaker_is_wired`, `::test_circuit_breaker_trips_cleanly` |
| **One run path, two front ends** | design + auto | `agent/engine.py` builds, drives and scores a run exactly once; `agent/run.py` renders it as console text + an exit code and `agent/serve.py` as JSON. Neither reaches into the other. Both were previously ~70% copies of the same pipeline, and `serve.py` imported two private functions from the CLI module. | `test_engine.py` |
| Circuit breaker is an outcome, not an exception | auto | A tripped breaker sets `RunOutcome.circuit_breaker_tripped` and preserves whatever was synthesised before it. The CLI exits **3** only when there is nothing to show, otherwise 0 with the note on stderr; the endpoint returns 200 with `circuit_breaker_tripped: true`. | `test_engine.py::test_the_circuit_breaker_is_an_outcome_field_not_an_exception`, `::test_cli_exits_3_only_when_the_breaker_leaves_nothing_to_show`, `::test_cli_still_shows_a_partial_answer_when_the_breaker_trips` |
| **Edge — a malformed LLM-judge reply** | auto | The judge's scores come from a model, so no part of the reply is guaranteed: a missing key, a bare string where the `{score, reason}` object should be, `"high"` or `7` as a score, prose around the JSON, or an exception from Vertex. Every one is a 0.0 / `fail`, or one `llm_judge_error` check — never a lost answer. Scores are clamped to 0..1 and explanations truncated before they become Loki log bodies. | `test_evaluation.py` |
| **Edge — the tool timeline's before/after pairing** | auto | The analysts and the Remediator share one `TimelineRecorder`: two calls in flight do not cross wires, a second `finish` for a call the gate already closed adds no duplicate row, an unpaired `after` records nothing rather than a row with no arguments, and one predicate decides `ok` for both failure shapes (`isError` from MCP, `status: blocked` from the gate). | `test_timeline.py` |
| Blocked write does not cause a retry loop | auto | The approval gate returns a "human declined, do not retry" result; the remediator prompt and the gate text both forbid a retry. | `test_approval.py` |

---

## Part 4 — Media context and data-pipeline validation

Turnaround is not a captioning / sentiment tool. Its "media input" is the
OpenCue job-name string `<show>-<shot>-<user>_<name>` plus the Kitsu schedule;
its "domain-specific output" is the ontology **join** — farm telemetry relabelled
onto `shot_id` / `sequence` / `department` — and the crunch forecast that follows.

| Case | Kind | What it asserts | Test |
|---|---|---|---|
| Domain relevance: media input → domain output | auto | A real OpenCue job name round-trips through `bridge/ontology.py` to the right `shot_id` / `sequence` / `department`; a malformed name is rejected, not guessed. | `test_ontology.py`, `test_reproducibility.py::test_job_name_parses_to_domain_fields` |
| Seed → measured story | auto | The generated show reproduces the `seed/story.md` mechanism: SEQ0420's render core-hours per comp iteration sit in `JOIN_RATIO_BAND`, waste is concentrated on SEQ0420. | `test_populate.py`, `test_show.py` |
| **Edge — context window / oversized payload** | auto | An oversized tool result (e.g. a Loki query returning thousands of lines) is length-capped with a `…` marker in the evidence path rather than dumped or silently truncated mid-token; analyst prompts constrain queries to aggregates so the model context stays bounded. | `test_timeline.py::test_args_and_results_are_digested_not_dumped`, `test_reproducibility.py::test_oversized_tool_payload_is_capped_not_dumped` |
| Captured content is guarded | auto | With `TURNAROUND_CAPTURE_CONTENT=1`, a prompt containing a crew name fails `assert_no_pii` loudly instead of being attached to a span. | `test_observability.py` |

---

## Part 5 — Repository and deployment readiness

| Case | Kind | What it asserts | Test |
|---|---|---|---|
| Startup config validation | auto | With `GOOGLE_CLOUD_PROJECT` / Grafana env unset, `agent.run.ask()` returns exit 2 with a message naming the missing keys; `/healthz` reports `vertex_ready` / `grafana_ready` honestly. | `test_reproducibility.py::test_startup_refuses_incomplete_config` |
| **Edge — submission compliance** | auto | `LICENSE` exists and is Apache-2.0; `pyproject.toml` declares that license; `README.md` carries a runnable quickstart (`uv sync`, `uv run pytest`); `deploy/README.md` documents the init/deploy path. | `test_reproducibility.py::test_repo_is_submission_compliant` |
| `/ask` contract | auto | The endpoint returns exactly `answer`, `timeline`, `evaluation`, `response_id`, `circuit_breaker_tripped`, `halted_by`, `halt_detail`, `grafana_url`; an unconfigured server returns a single `error` naming the missing keys instead of a partial body; a question outside 3..2000 characters is rejected with 422 before any Gemini call is made. | `test_engine.py` |
| `/health` and `/healthz` are one pure read | auto | It reports the resolved readiness flags, MCP mode and Gemini-call ceiling without mutating process env or building an agent, so a Cloud Run probe cannot have side effects. Both paths return the same body: Google Frontend swallows the exact path `/healthz` on `*.run.app`, so the hosted URL is only reachable at `/health`. | `test_engine.py::test_healthz_is_a_pure_read_and_publishes_the_resolved_ceiling`, `::test_health_is_reachable_under_both_paths` |
| Telemetry misconfiguration is a named startup error | auto | A missing `OTEL_EXPORTER_OTLP_ENDPOINT` fails with a message pointing at `.env.example`, not a connection timeout mid-demo. Partial exporter injection wires the signals given and discards the rest rather than raising from inside the OTel SDK. | `test_providers.py` |
| Deploy image is buildable | manual | `docker build -t turnaround .` succeeds; `mcp-grafana` checksum matches the pin. | see Part 6 |
| Deploy builds the Dockerfile, not a buildpack | auto | The Dockerfile is at the repo root (`gcloud` builds a Dockerfile only from the source root; anywhere else it silently falls back to buildpacks and ships an image with no `mcp-grafana` and the wrong entrypoint), and `deploy.sh` builds once and deploys service and job from that one image. | `test_reproducibility.py::test_deploy_builds_the_dockerfile_not_a_buildpack` |
| Hosted demo data stays alive | auto | `deploy.sh` deploys the `seed.refresh` Cloud Run job and a Cloud Scheduler trigger, so the ~45-minute compressed window is re-seeded before it ages out of Mimir's ingestion horizon. | `test_reproducibility.py::test_deploy_keeps_the_demo_data_alive` |
| Deploy path is credential-safe | auto | `.gcloudignore` / `.dockerignore` exclude `.env` and `.secrets/`; `agent/serve.py` uses `AutoApprover(approve=False)` so the endpoint cannot mutate anything. | `test_reproducibility.py::test_deploy_context_excludes_secrets`, `::test_http_endpoint_cannot_approve_writes` |

---

## Part 6 — Manual live-stack checklist

Needs a filled-in `.env` (Grafana Cloud + Vertex) and `mcp-grafana` on `PATH`.
Unlike the agent tier, `seed.*` and `grafana.*` do **not** load `.env` themselves
(`docs/SETUP.md`), so export it first:

```bash
set -a && source .env && set +a
uv run python -m seed.populate
```

**Re-seed before every live session.** The whole history is compressed into a
~45-minute window ending at the moment of seeding, and every analyst query and
alert rule reads it with `last_over_time((...)[2h:])`. A stack seeded more than
about two hours ago answers every query with nothing, and the analysts then have
no numbers to ground on — which looks like a broken agent rather than stale data.

1. **Four demo questions answered cold** — for each of
   *why is SEQ0420 slipping and what is it costing* · *what is it costing in
   artist-days* · *who is heading for crunch and when* · *what do I change to
   avoid both*:
   ```bash
   uv run python -m agent.run "<question>"
   ```
   Expect: an Answer/Evidence/Remediation block; a tool timeline with `*`-marked
   Grafana MCP calls; a scorecard where `grounding_numbers`, `mechanism_named`,
   `privacy_floor_respected` all pass and the LLM judge's `hallucination` ≥ 0.8.
2. **FarmAnalyst uses Tempo** — question 1's timeline shows at least one
   `tempo_traceql-search` / `tempo_get-trace` call.
3. **Trace in Tempo** — `{ span.gen_ai.response.id = "<printed id>" }` returns one
   `invoke_agent` trace with nested `chat` + `execute_tool` spans carrying
   `gen_ai.usage.*` and `turnaround.query`.
4. **Eval events in Loki** — `{service_name="turnaround-agent"} | json | response_id="<id>"`
   returns ≥ 6 `gen_ai.evaluation.result` records; `privacy_floor_respected` = `pass`.
5. **Write-back path** — `uv run python -m agent.run --approve "what do I change to avoid both?"`
   drives `create_annotation` + `kitsu_write_back` through the gate (APPROVED),
   appends a row to `agent/_writeback.jsonl`, lands two Grafana annotations.
6. **EvalOps surface** — `uv run python -m grafana.provision` pushes all four
   dashboards, both alert groups and the ML jobs. All four rules report
   `health=ok`. Three sit `inactive`; **"A sequence is concentrating render
   waste" fires**, and should — SEQ0420 carries ~44 core-hours against ~0.7
   elsewhere. A firing waste alert on a freshly seeded stack is the product
   working, not a failure.
7. **Circuit breaker** — `TURNAROUND_MAX_LLM_CALLS=1 uv run python -m agent.run "..."`
   exits **3** with `circuit breaker tripped: Max number of llm calls limit of \`1\`
   exceeded` and no traceback.
7b. **Vertex quota** — four questions back to back can exhaust a small project's
   per-minute Gemini quota. That is a `Halt(kind="model_quota")`: exit 3, one
   sentence naming the model and the region, never an ADK traceback. Covered
   offline by `test_engine.py`; if you see it live, wait a minute and re-run.
8. **Hosted MCP** — `uv run python -m agent.mcp_login` completes the OAuth flow;
   `TURNAROUND_MCP_MODE=hosted uv run python -m agent.run "..."` answers.
9. **Deploy** — `PROJECT_ID=... REGION=... ./deploy/deploy.sh`; then
   `curl $URL/healthz` and `curl -d '{"question":"..."}' $URL/ask` (with an
   identity token) return the expected JSON.
