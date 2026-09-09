# Design log

Chronological build record: what was done, in what order, and — more usefully —
the bugs and wrong assumptions found on the way. Kept separate from
[`PROJECT.md`](../PROJECT.md) so that document can describe the system as it is
now without also being a diary.

**If you want the current state, read `PROJECT.md`, not this file.** Entries
here are true as of their date and are not retro-edited.

| Date | Entry |
|---|---|
| 2026-09-05 | [Data plane — bridge, seed, emitter](#2026-09-05--data-plane) |
| 2026-09-06 | [Live-stack verification of the join](#2026-09-06--live-stack-verification-of-the-join) |
| 2026-09-06 | [Phase 3 — dashboards, alerts, ML](#2026-09-06--phase-3--dashboards-alerts-ml) |
| 2026-09-06 | [Phase 4 — MCP wiring and the agents](#2026-09-06--phase-4--mcp-wiring-and-the-agents) |
| 2026-09-06 | [EvalOps tier — self-instrumentation, judge, surface](#2026-09-06--evalops-tier) |
| 2026-09-06 | [Grafana MCP: two modes](#2026-09-06--grafana-mcp-two-modes) |
| 2026-09-06 | [Hardening, reproducibility, deploy scaffolding](#2026-09-06--hardening-reproducibility-deploy-scaffolding) |
| 2026-09-06 | [Tool coverage — trackers, farms, NLEs](#2026-09-06--tool-coverage) |
| 2026-09-09 | [Code review and hardening pass](#2026-09-09--code-review-and-hardening-pass) |

---

## 2026-09-05 — data plane

Ontology, privacy layer, emitter, metric backfill, simulation, seeder driver —
all complete and tested. A dry run produces **1,454 spans, 1,476 correlated log
lines, 5,917 metric points** without touching the network. The same run against
the live stack completes in ~4 s.

---

## 2026-09-06 — live-stack verification of the join

- **Traces** — `{ .production.shot_id = "SEQ0420_SH0100" }` returns one trace,
  department spans, comp spans in error status. Tempo accepts historical span
  times directly (no compression needed for traces, but the seeder warps them
  anyway so the trace and metric time axes align).
- **Logs** — `{service_name="turnaround-bridge"}` carries ~1,476 lines. For one
  shot the status transitions (`comp -> retake (iteration 1)`, `-> wip
  (iteration 3)`, …) and the `frame 118` cache-miss errors all resolve, each on
  the derived trace id. This is the fallback path for when the agent cannot
  reach a trace.
- **Metrics** — all ten `turnaround_*` series present. The thesis join
  (`increase(render_core_hours[5m]) / increase(task_iterations[5m])`, by
  sequence) puts **SEQ0420 well above every other sequence**; render-waste
  totals are **~11× concentrated** on SEQ0420. Correlation holds.
- **Crew load** — `turnaround_artist_hours_logged` by pool/department present;
  comp-pool-1/2 highest.

**Bug found and fixed (`bridge/emit.py`).** The constructor tested
`span_exporter is None` to decide whether to attach a log processor — but
`span_exporter` had already been reassigned to a real `OTLPSpanExporter` a few
lines up, so on the seeder's path the condition was always false and **no log
processor was ever attached**. Every `emit_log` call was silently discarded;
`flush()` returned True because there was nothing queued. Two failed seeds
landed zero logs for this reason, not a Loki problem. The path now keys off a
`real_otlp` flag captured before the reassignment, with a regression test that
fails on the old code.

---

## 2026-09-06 — Phase 3 — dashboards, alerts, ML

Everything under `grafana/`, pushed by one idempotent `grafana/provision.py`.

- **Three dashboards** in a `Turnaround` folder: *Crew Load* (the hero — weekly
  hours per rostered artist by pool, the floor join made visible), *The Join*
  (cumulative render core-hours per comp iteration by sequence — SEQ0420 sits
  clear of the pack — plus render waste and frame-failure rate), *Delivery*
  (burndown, cumulative iteration load, vendor turnaround). A `director-note`
  annotation query is wired on all three.
- **`turnaround_pool_headcount`** added to the seed so the aggregation floor is
  a transparent PromQL join (`... and on(pool) (turnaround_pool_headcount >= 3)`)
  rather than a hard-coded pool list. `di-pool-1` is 2.
- **`bridge/annotate.py`** — the director note lands as a Grafana annotation,
  timewarped like everything else.
- **Two alert rules**, provisioned and firing correctly: *crew crunch* on
  comp-pool-1 and comp-pool-2 but **not di-pool-1** (floor join), and *render
  waste* on **SEQ0420 only**. Each carries a `lever` annotation. Queries are
  wrapped in `last_over_time(...[3h:1m])` so a single seed keeps them evaluable.
- **Grafana ML** — three jobs provisioned via the ML app API: two Prophet
  forecasts (burndown, comp-pool-2 hours) and one MAD outlier detector on vendor
  turnaround. The forecasts need ~100+ points of continuous history to train; on
  the compressed time base that only accumulates once `seed/refresh.py` has been
  re-seeding for a couple of hours, so in a single-seed session they sit in
  `error: "No series to train"`. Documented, not faked — `predict_linear` panels
  were tried and removed because repeated re-seeds reset the counters and the
  extrapolation went to nonsense.

---

## 2026-09-06 — Phase 4 — MCP wiring and the agents

Everything under `agent/`. `mcp-grafana` (OSS, 1.3.0) runs as a **stdio
subprocess** ADK spawns and owns — no ports. Two privilege tiers, both enforced
by how the server is started, not by prompt.

**The pipeline is deterministic, not model-routed.** Flash, asked to "consult the
right specialists then synthesise", reliably stopped after one and echoed it. So
`build_system()` returns a `SequentialAgent`. Every question runs the whole
board (a few extra flash calls; a demo that behaves the same each take).

**Findings that shaped it:**

- **A bare instant query at `now` returns nothing.** The compressed window ends
  when the seeder finishes; minutes later the newest sample is outside
  Prometheus's 5-minute instant lookback. Every "current state" recipe is
  wrapped `last_over_time((<expr>)[2h:])` — the same trick the alert rules use.
- **`gemini-2.5-pro` is 429 quota-locked on this project.** First call,
  `RESOURCE_EXHAUSTED`. Everything runs `gemini-2.5-flash`.
- **ADK aborts the whole run** with `ValueError: Tool 'x' not found` if a model
  calls a tool outside its `tool_filter`. Not worth risking on camera to save
  tool-schema tokens, so all three analysts share one broad read core.
- **`mcp` 2.x renamed the SDK API**; ADK 2.8 needs `mcp<2`. Pinned `1.29.1`.
- `SequentialAgent` is deprecated in ADK 2.8 for a `Workflow` type that "cannot
  yet be used as an LlmAgent sub-agent" — so `SequentialAgent` is still correct
  here. Revisit when `Workflow` composes.
- **Cloud Traces MCP was a wrong assumption.** `mcp-grafana` 1.3 ships `tempo_*`
  tools in its default set, so the FarmAnalyst queries traces directly; no
  separate endpoint is needed.

---

## 2026-09-06 — EvalOps tier

Make the *agent* legible in Grafana the way the *shot* already is, and score its
answers.

- **`observability/`** — a standalone package (no `agent/`/`bridge/` import; the
  PII guard is injected). Emits `invoke_agent` / `chat` / `execute_tool` spans
  per the OTel GenAI conventions, two histograms, and opt-in prompt capture
  behind `TURNAROUND_CAPTURE_CONTENT`.
  *Two ADK-shape bugs found in live runs: don't `context.attach` across callback
  boundaries (the root span belongs in the caller); pair `chat` spans on a
  stack, not `id(callback_context)` — ADK passes different objects to
  before/after.*
- **`agent/evaluation.py`** — a `DeterministicJudge` (ground-truth anchors + the
  `privacy_floor_respected` invariant) and an `LlmJudge` (second Gemini, no
  tools). Each check is one `gen_ai.evaluation.result` log event, correlated by
  `gen_ai.response.id`. *The LLM judge does flag real run-to-run variation —
  hallucination 0.3–1.0 depending on how much of a figure's derivation the
  answer shows — which is the point, not a bug.*
- **`grafana/evalops/build.py`** → an 11-panel EvalOps dashboard plus two
  Loki-backed alert rules: `eval-grounding-drift` and `eval-privacy-breach`.

---

## 2026-09-06 — Grafana MCP: two modes

`TURNAROUND_MCP_MODE`, default **`oss`**: local `grafana/mcp-grafana` stdio
subprocess, service-account token, read-only by construction. This is the
deployed/demo path.

**`hosted`** (opt-in) targets `https://mcp.grafana.com/mcp` over Streamable HTTP
with a bearer from the OAuth 2.1 flow (`agent/mcp_login.py` — PKCE + dynamic
client registration, browser consent, localhost callback). The hosted endpoint
has **no service-account path**, so this mode cannot run unattended and is not
the deploy target; it exists to exercise the interactive authorization the
proposal assumes.

---

## 2026-09-06 — hardening, reproducibility, deploy scaffolding

- **Circuit breaker.** `TURNAROUND_MAX_LLM_CALLS` → `RunConfig(max_llm_calls=…)`.
  ADK's own default is 500 — high enough for a stuck tool-retry loop to cost
  real money.
- **Synthesis grounding tightened.** Every figure in the final answer must
  appear in a specialist block; no newly-derived numbers. This exposed that the
  ScheduleAnalyst was failing to compose the artist-day cost PromQL inline →
  added ready-made cost recipes to `agent/vocabulary.py`. Live: total 56.26
  artist-days/week, was degenerating to 0.
- **FarmAnalyst now uses Tempo** — `tempo_traceql-search` verified live.
- **`agent/serve.py`** — FastAPI wrapper for Cloud Run; `AutoApprover(approve=False)`
  so the endpoint cannot mutate anything.
- **`deploy/`** — Dockerfile (Python 3.12 + uv + pinned, checksum-verified
  `mcp-grafana` 1.3.0), `deploy.sh`, README; `.gcloudignore` / `.dockerignore`
  keep `.env` and `.secrets/` out.

---

## 2026-09-06 — tool coverage

The join needs a shot id and a task status; that is all Turnaround asks of a
studio's stack, so it is not Kitsu+OpenCue-only.

- **`bridge/ontology.py`** — `parse_job_name(convention=…)` ships regex
  conventions for OpenCue (default), Deadline, Tractor, Qube! and Royal Render,
  plus a `template` mode. Shot-id spelling is a configurable `ShotIdScheme`.
  `TaskStatus.from_tracker` recognises ShotGrid / Flow Production Tracking /
  ftrack vocabulary, with 3-letter codes matched exactly so `ip` does not fire
  on "Shipping".
- **`bridge/sources.py`** — `ScheduleSource` / `FarmSource` / `EditorialSource`
  Protocols; `SUPPORTED_TOOLS` is the honest coverage list; `CsvScheduleSource`
  ingests an export from any tracker with no Python client.
- **`bridge/editorial.py`** — CMX3600 EDL parser (pure stdlib). Verified against
  the dialects real tools emit: Resolve / Premiere / Avid / Shotcut clip-name
  comments, drop-frame timecode, transition columns, Unix + Windows paths, and
  an online/conform EDL carrying the shot only in the reel column. `iter_otio_cut`
  reads `.otio` with `opentimelineio`.

---

## 2026-09-09 — code review and hardening pass

A full structural review against a "delete complexity rather than rearrange it"
standard. Ten findings, all fixed; the write-up with before/after is
[`docs/CODE_REVIEW.md`](CODE_REVIEW.md). The two that mattered:

- **Configuration froze at import, so `.env` was silently ignored.** Four module
  constants were read at module load — before `load_env()` ran. Against the
  operator's real `.env` this meant the documented cost ceiling
  (`TURNAROUND_MAX_LLM_CALLS`), the model and the MCP mode were all set and none
  were used. All four deleted; `Settings`, built after `load_env()`, is now the
  only channel.
- **`run.py` and `serve.py` were two copies of the pipeline**, and `serve.py`
  imported two *private* functions from the CLI. Extracted `agent/engine.py`:
  one `answer_question()`, two presentations. Extracting it exposed that a
  tripped circuit breaker always discarded the partial answer, which made a
  documented exit-code branch unreachable.

Also found live and fixed: **a Vertex 429 arrived as a 40-line ADK traceback.**
Four demo questions back to back exhaust a small project's per-minute Gemini
quota. Now a typed `Halt` — one sentence, exit 3, and `halted_by` on `/ask`.

**Operational finding, not a code defect: the demo has a ~2-hour shelf life.**
The seeded history is a ~45-minute window ending at seed time, read with
`last_over_time((…)[2h:])`. A stack seeded three days earlier answered every
query with nothing, and the analysts had no numbers to ground on — which
presents as a broken agent rather than as stale data. Re-seed before every live
session; `seed/refresh.py` exists for exactly this.

Suite **241 → 358** tests; `agent/config.py` and `observability/providers.py`
had had no direct coverage at all.
