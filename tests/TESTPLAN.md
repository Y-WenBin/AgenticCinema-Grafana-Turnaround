# Turnaround — test plan for reproducible results

This is the contract the test suite enforces so a reviewer can run the project
and get the same conclusions we did. It also maps the hackathon's suggested
unit-test checklist onto Turnaround's actual architecture, and marks what each
row is: **auto** (a pytest test), **manual** (a live-stack step), or **design**
(a guarantee the shape of the code makes, verified by an auto test).

Run everything:

```bash
uv sync --group dev
uv run pytest -q          # 167+ tests, no network, no credentials
uv run ruff check .
```

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
| R3 | **Privacy floor is absolute.** A pool of < 3 people (`di-pool-1`, 2 people) never appears in an answer, an evaluation payload, captured content, or the evidence chain — every run, zero tolerance. | `bridge/privacy.py` (`MIN_POOL_SIZE=3`, `assert_no_pii`), `agent/evaluation.py` (`privacy_floor_respected`), analyst prompt | `tests/test_privacy.py`, `tests/test_evaluation.py::test_deterministic_judge_catches_a_subfloor_pool_leak_*`, `tests/test_reproducibility.py::test_privacy_floor_is_enforced_on_every_surface` |
| R4 | **Model constraint.** Only Gemini on Vertex at runtime (`gemini-2.5-flash`); no non-Google AI SDK on the runtime path. | `agent/config.py` (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, no GLA path) | `tests/test_reproducibility.py::test_no_non_google_ai_sdk_on_the_runtime_path` |
| R5 | **Telemetry contract.** The `gen_ai.*` span / metric / event names the EvalOps dashboard and alerts query are exactly what `observability/genai.py` emits. | shared constants in `observability/genai.py` | `tests/test_evalops_grafana.py`, `tests/test_observability.py` |
| R6 | **Offline assembly.** The whole agent pipeline builds with no credentials and no network, so CI is deterministic. | `agent/producer.py` lazy `root_agent` | `tests/test_agent_build.py` |
| R7 | **Bounded cost.** A run cannot exceed `TURNAROUND_MAX_LLM_CALLS` (default 40) Gemini calls, so token spend per question is bounded and repeatable. | `agent/config.py` + `RunConfig(max_llm_calls=...)` in `agent/run.py` / `agent/serve.py` | `tests/test_reproducibility.py::test_circuit_breaker_is_wired` |

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
| Deploy image is buildable | manual | `docker build -f deploy/Dockerfile -t turnaround .` succeeds; `mcp-grafana` checksum matches the pin. | see Part 6 |
| Deploy path is credential-safe | auto | `.gcloudignore` / `.dockerignore` exclude `.env` and `.secrets/`; `agent/serve.py` uses `AutoApprover(approve=False)` so the endpoint cannot mutate anything. | `test_reproducibility.py::test_deploy_context_excludes_secrets`, `::test_http_endpoint_cannot_approve_writes` |

---

## Part 6 — Manual live-stack checklist

Needs a filled-in `.env` (Grafana Cloud + Vertex) and `mcp-grafana` on `PATH`.
Run after `uv run python -m seed.populate`.

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
6. **EvalOps surface** — `uv run python -m grafana.provision` pushes
   `turnaround-evalops` and both alert groups; alerts evaluate `inactive health=ok`.
7. **Circuit breaker** — `TURNAROUND_MAX_LLM_CALLS=1 uv run python -m agent.run "..."`
   exits non-zero with `circuit breaker tripped`, no traceback.
8. **Hosted MCP** — `uv run python -m agent.mcp_login` completes the OAuth flow;
   `TURNAROUND_MCP_MODE=hosted uv run python -m agent.run "..."` answers.
9. **Deploy** — `PROJECT_ID=... REGION=... ./deploy/deploy.sh`; then
   `curl $URL/healthz` and `curl -d '{"question":"..."}' $URL/ask` (with an
   identity token) return the expected JSON.
