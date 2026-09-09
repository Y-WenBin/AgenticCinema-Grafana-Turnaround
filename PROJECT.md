# Turnaround — technical reference

What the system is, how it is put together, and the decisions that are expensive
to re-derive. This describes the project **as it stands**; the dated build
record — including every bug found on the way — is
[`docs/DESIGN_LOG.md`](docs/DESIGN_LOG.md).

| Document | What it is for |
|---|---|
| [`README.md`](README.md) | Public front door: the problem, the idea, how to run it |
| **`PROJECT.md`** (this file) | Technical reference: architecture, module map, design decisions |
| [`AGENTS.md`](AGENTS.md) | Orientation for a coding agent or a new contributor: invariants and where things live |
| [`docs/SETUP.md`](docs/SETUP.md) | End-to-end setup: Grafana Cloud, Google Cloud, a real NLE |
| [`docs/DEMO.md`](docs/DEMO.md) | The three-minute demo: script, shot list, question set |
| [`docs/CODE_REVIEW.md`](docs/CODE_REVIEW.md) | The 2026-09-09 structural review and what changed |
| [`docs/DESIGN_LOG.md`](docs/DESIGN_LOG.md) | Chronological build record; findings, dated |
| [`tests/TESTPLAN.md`](tests/TESTPLAN.md) | The reproducibility contract the suite enforces |
| [`seed/story.md`](seed/story.md) | The measured demo narrative |
| [`deploy/README.md`](deploy/README.md) | Cloud Run image and deploy script |

**Status.** Data plane, dashboards/alerts/ML, the agent tier, the EvalOps tier
and the deploy scaffolding are complete and tested. All four demo questions are
verified cold against the live stack. **364 tests**, offline; `ruff` clean. The
Cloud Run *deploy run* and a live Kitsu/OpenCue instance are the two open items
(see [Roadmap](#roadmap)). Grafana Cloud stack `your-stack`
(`prod-ap-southeast-1`); Vertex AI `gemini-2.5-flash`.
Repo: `github.com/Y-WenBin/AgenticCinema-Grafana-Turnaround`.

---

## The problem

The VFX labour crisis is documented and specific: roughly **70% of VFX workers
report unpaid overtime**, about **two-thirds say conditions are unsustainable**,
80-hour weeks are routine, and ~**75% report working through legally mandated
breaks**. The cause people name is exactly what telemetry can measure —
unrealistic deadlines, plus shots corrected or remade until the schedule
collapses into day-and-night work.

Crunch is not a surprise. It is a **forecastable consequence of rework and
render waste**, visible weeks ahead in data that already exists. Nobody looks,
because the evidence lives in two systems that never meet:

| Who | What they see | What they miss |
|---|---|---|
| Producers | the tracker — shots, statuses, assignments, hours | why a stage keeps repeating |
| Systems / IT | the render farm — job failures, frame retries, queue depth | which shot, which deadline, which crew |

Join them and a class of invisible waste becomes obvious:

> *The artist isn't slow. Every comp iteration burns nine hours because frame
> 118 keeps failing.*

**What Turnaround is.** A Gemini/ADK multi-agent system that joins the creative
schedule to the render farm inside Grafana, forecasts delivery slip *and* crew
overload together, and proposes evidence-backed corrections a supervisor
approves — while the schedule can still be renegotiated.

**Audience:** VFX producers, post supervisors, and the crews they staff.

**The generalisable thesis.** Grafana already ships an OpenCue integration.
Studios still don't use Grafana. *The gap isn't data or connectors — it's
vocabulary and audience.* Turnaround is one instance of a repeatable pattern: an
**MCP-driven domain semantic layer** that ingests domain events over OTLP, owns
the ontology mapping domain vocabulary to query language, and returns *decisions
with evidence* instead of charts. VFX is the first vertical; the same shape
serves OR scheduling, construction, logistics, legal discovery.

---

## Architecture

```
 any tracker (Kitsu / ShotGrid / ftrack / CSV) ─┐
 any farm    (OpenCue / Deadline / Tractor …)  ─┼─► bridge/ ─OTLP─► Grafana Cloud
 any NLE cut (EDL / OTIO)                      ─┘   relabel on      Tempo · Loki
                                                    shot_id;        Mimir · Alerts
                                                    privacy floor   ML forecasts
                                                                         │ MCP
                                                                         ▼
                        Cloud Run / CLI: ADK multi-agent on Gemini (Vertex AI)
                          producer ─ schedule_analyst · farm_analyst
                                   · crunch_guardian          [read-only]
                                   ─ remediator               [gated]
                                   ─ synthesis                [no tools]
                                                                         │
                        observability/ — the agent instruments and scores itself,
                        into the same stack it queries
```

Two privilege tiers, **enforced by how the MCP server is started, not by prompt**:

- **Analysts** → `mcp-grafana --disable-write`, narrowed further with
  `tool_filter`. A prompt-injected "change X" has nothing to call.
- **Remediator** → `mcp-grafana --enabled-tools annotations,incident`. Every
  call still passes `ApprovalGate.before_tool`, which shows the supervisor the
  proposed write *and* the full evidence chain before anything is written back.

The pipeline is a fixed `SequentialAgent`, not model-routed — see
[Design decisions](#design-decisions-worth-knowing).

---

## The ontology and the join

Defined once in [`bridge/ontology.py`](bridge/ontology.py); every other component
derives its vocabulary from there, so a producer's question and a PromQL query
refer to the same thing.

| Production concept | Telemetry primitive |
|---|---|
| Shot `SEQ0420_SH0100` | **trace**, `trace_id` derived from the shot id |
| Task stage (previz → layout → anim → fx → lighting → comp → di) | **span** |
| Retake / rejection | span **error + retry** |
| Status transitions, notes, render + QC logs | **Loki** streams |
| Burndown, iterations, artist hours, farm counters | **Mimir** metrics |
| Director note, cut change, date change | **annotation** |
| "SEQ0420 will miss its date" · "comp pool trending to 68h" | **ML forecast + alert** |
| "Vendor B behaves unlike every other vendor" | **outlier detector** |

Modelling a retake as a span error is not decoration: Tempo's own error-rate
tooling then counts rework with no bespoke query.

### The join

OpenCue's PyOutline names every job `<show>-<shot>-<user>_<name>`. **The shot id
is already in the farm's telemetry.** `parse_job_name` recovers it and relabels
farm metrics onto `shot_id` / `sequence` / `department`, so one PromQL query
spans the creative schedule and the compute that serves it.

Two details that make it robust:

- It **anchors on the shot id** (`SEQ\d{4}_SH\d{4}` by default) rather than
  splitting on hyphens, so studio show slugs like `night-fall-s2` survive.
- It returns **`None`, not an exception**, for unattributable jobs — farm
  maintenance and ad-hoc tool jobs are real and must not crash a tap.

Everything else in the product follows from that one relabel. The join asks for
a **shot id** and a **task status** and nothing else, which is what makes it
tool-agnostic: `parse_job_name(convention=…)` ships OpenCue / Deadline / Tractor
/ Qube! / Royal Render plus a `template` mode, `ShotIdScheme` configures the
spelling, and `TaskStatus.from_tracker` normalises ShotGrid / Flow Production
Tracking / ftrack vocabulary. `SUPPORTED_TOOLS` in
[`bridge/sources.py`](bridge/sources.py) is the honest coverage list.

---

## Module reference

Line counts are indicative, not maintained to the digit.

### `bridge/` — studio tools → Grafana Cloud

| File | Lines | Purpose |
|---|---:|---|
| [`ontology.py`](bridge/ontology.py) | 553 | The single source of vocabulary: pipeline model, status normalisation, shot identity, the farm job-name join, metric and attribute names |
| [`sources.py`](bridge/sources.py) | 238 | `ScheduleSource` / `FarmSource` / `EditorialSource` Protocols, normalised events, `SUPPORTED_TOOLS`, `CsvScheduleSource` |
| [`editorial.py`](bridge/editorial.py) | 202 | CMX3600 EDL parser (stdlib only) + `iter_otio_cut` for `.otio` |
| [`emit.py`](bridge/emit.py) | 318 | Backfilled spans with derivable ids; trace-correlated Loki lines |
| [`metrics.py`](bridge/metrics.py) | 259 | Historical metric backfill over OTLP; counter accumulation; label rules; chunking |
| [`privacy.py`](bridge/privacy.py) | 115 | Pseudonymisation, PII guard, aggregation floor |
| [`timewarp.py`](bridge/timewarp.py) | 78 | Monotonic affine map compressing history into the ingestion window |
| [`annotate.py`](bridge/annotate.py) | 69 | Production events → Grafana annotations |
| [`testing.py`](bridge/testing.py) | 36 | In-memory exporters, shipped with the package |

### `seed/` — the simulated show

| File | Lines | Purpose |
|---|---:|---|
| [`model.py`](seed/model.py) | 414 | The simulation: crunch emerges from a mechanism rather than being asserted |
| [`populate.py`](seed/populate.py) | 307 | Driver: history → spans, logs, metrics, annotations; `--dry-run` exercises the full write path |
| [`show.yaml`](seed/show.yaml) | 123 | The show declaration and its two story beats |
| [`refresh.py`](seed/refresh.py) | 62 | Re-seed on a loop so the compressed window always overlaps "now" |
| [`story.md`](seed/story.md) | 59 | The measured demo narrative |

### `grafana/` — dashboards, alerts, ML

| File | Lines | Purpose |
|---|---:|---|
| [`build.py`](grafana/build.py) | 257 | Builds *Crew Load*, *The Join*, *Delivery* as plain dicts → `dashboards/*.json` |
| [`evalops/build.py`](grafana/evalops/build.py) | 268 | Builds the 11-panel EvalOps dashboard |
| [`alerts/build.py`](grafana/alerts/build.py) | 207 | Four alert rules, each carrying a `lever` annotation |
| [`ml/build.py`](grafana/ml/build.py) | 100 | Two Prophet forecasts + one MAD outlier detector |
| [`provision.py`](grafana/provision.py) | 115 | One idempotent push: folder, dashboards, alert groups, ML jobs |

### `agent/` — the multi-agent tier

| File | Lines | Purpose |
|---|---:|---|
| [`engine.py`](agent/engine.py) | 257 | **The one run path.** Build, instrument, drive under the circuit breaker, score. Returns a `RunOutcome`; both front ends only present it |
| [`producer.py`](agent/producer.py) | 129 | `build_system()` → the five-step `SequentialAgent` + shared timeline, gate and ledger |
| [`analysts.py`](agent/analysts.py) | 182 | `schedule_analyst`, `farm_analyst`, `crunch_guardian` — read-only, each with a distinct `output_key` |
| [`remediator.py`](agent/remediator.py) | 98 | The one write-capable agent; every call goes through the gate and the timeline |
| [`approval.py`](agent/approval.py) | 154 | `ApprovalGate` + `EvidenceLedger`; `Approver` protocol (`AutoApprover`, `CliApprover`) |
| [`writeback.py`](agent/writeback.py) | 145 | Kitsu write-back behind a Protocol: `RecordingKitsu` now, `GazuKitsu` for a real instance |
| [`evaluation.py`](agent/evaluation.py) | 314 | The judge tier: deterministic ground truth + privacy invariant, plus an LLM judge |
| [`vocabulary.py`](agent/vocabulary.py) | 198 | Ontology → prompt: metric catalogue, PromQL recipes, tool-calling / privacy / time-base rules |
| [`mcp_grafana.py`](agent/mcp_grafana.py) | 131 | `McpToolset` factories for both modes and both privilege tiers |
| [`mcp_login.py`](agent/mcp_login.py) | 153 | One-time OAuth 2.1 browser flow for the hosted MCP endpoint |
| [`config.py`](agent/config.py) | 201 | `.env` loading, Vertex bootstrap, `Settings` — the only channel for runtime configuration |
| [`timeline.py`](agent/timeline.py) | 174 | `ToolTimeline` + `TimelineRecorder`: every tool call, Grafana ones marked. The demo's evidence |
| [`run.py`](agent/run.py) | 114 | CLI presentation: console text + exit code |
| [`serve.py`](agent/serve.py) | 98 | HTTP presentation: `POST /ask`, `GET /healthz` |

### `observability/` — the agent watching itself

Standalone: imports neither `agent/` nor `bridge/` (the PII guard is injected).

| File | Lines | Purpose |
|---|---:|---|
| [`genai.py`](observability/genai.py) | 315 | The `gen_ai.*` span / metric / event vocabulary and the emitters |
| [`adk.py`](observability/adk.py) | 152 | ADK `BasePlugin`: lifecycle callbacks → telemetry |
| [`providers.py`](observability/providers.py) | 125 | The tracer / logger / meter provider trio |
| [`testing.py`](observability/testing.py) | 96 | In-memory wiring for tests and dry runs |

### Dependencies

`google-adk==2.8.0`, `google-genai==2.22.0`, `mcp==1.29.1` (pinned `<2`; ADK
predates the mcp 2.x API rename), `opentelemetry-sdk==1.42.1`, `gazu==1.2.2`,
`fastapi==0.141.1`, `pyyaml==6.0.3`. Python 3.12 via `uv`. The `mcp-grafana`
binary is a separate install (`brew install mcp-grafana`, 1.3.0);
`agent/config.py` finds it on `PATH` or at `/opt/homebrew/bin`.

---

## Design decisions worth knowing

Each of these was a bug or a wrong assumption found while building, not a
preference. They are recorded because re-deriving them is expensive.

**Trace ids are derived, not discovered.** `DeterministicIdGenerator` derives the
trace id from the shot id and the span id from `(shot, department, iteration)`.
The agent computes a shot's trace id from a producer's question with no lookup
table; re-running the seeder overwrites rather than duplicates; a scripted demo
stays reproducible. It falls back to random ids when unseeded, so the agent's own
instrumentation — which shares the process — is unaffected.

**Spans are backfilled with explicit timestamps.** A shot's trace spans weeks, so
spans cannot be timed in-process. Timezone validation lives in
`StageEvent.__post_init__` rather than at export: production data crosses
facilities, so a naive timestamp is a silent hours-out error and must fail where
it is introduced.

**Every stage event is written twice** — as a span *and* as a trace-shaped Loki
line carrying the same trace and span ids. The redundancy guarantees the agent
can reconstruct a shot's history even if trace tools are unavailable. Tests pin
that the ids genuinely match: a redundant path that disagrees with the primary
one is worse than none.

**Counters accumulate inside `metrics.py`.** Callers pass increments, which is
what a simulation produces naturally. `start_time_unix_nano` is pinned per series
so `rate()` behaves the same over backfilled data as over live data. A
mis-accumulated counter yields a series that looks entirely plausible and rates
to nonsense.

**No metric may carry an artist label — a hard failure, not a filter.** This is
not merely cardinality. A per-person series survives every aggregation floor
downstream, because the floor filters *queries* while the label is already in the
*data*.

**`--dry-run` exercises the full write path.** Most backfill mistakes — a counter
that never accumulates, a timestamp in the wrong unit, a label that explodes
cardinality — are invisible until the data is already in the stack and awkward to
remove.

**History is compressed into the ingestion window, not into the data.** Hosted
Mimir rejects any sample older than a ~1 h out-of-order window (measured: 65 min
accepted, 90 min refused), so live OTLP push cannot carry a five-month backfill
as-is. [`bridge/timewarp.py`](bridge/timewarp.py) applies a **monotonic affine
map** at the single `datetime → unix_nano` seam: `[earliest .. now]` onto
`[now − window .. now]`, default 45 min. The *simulation* stays in real calendar
time, so only the time axis scales and every series keeps its exact shape.
Queries use proportional ranges: `increase(...[14d])` becomes `increase(...[5m])`.
`--compress 0` restores real timestamps for a self-hosted stack.

**A bare instant query at `now` returns nothing.** The compressed window ends when
the seeder finishes; minutes later the newest sample is outside Prometheus's
5-minute instant lookback. Every "current state" recipe is wrapped
`last_over_time((<expr>)[2h:])` — the same trick the alert rules use. **This is
also why the demo has a shelf life:** see [Running it](#running-it).

**The pipeline is deterministic, not model-routed.** Flash, asked to "consult the
right specialists then synthesise", reliably stopped after one and echoed it. So
the shape is a `SequentialAgent` and every question runs the whole board. It costs
a few extra Flash calls and buys a demo that behaves the same way every take.

**One run path, two front ends.** `agent/engine.py` builds, drives and scores a
run exactly once. `run.py` renders text, `serve.py` renders JSON, and neither
reaches into the other. They were previously ~70% copies of the same pipeline.

**Configuration resolves after `.env` loads, never at import.** Module-level
`os.environ` reads froze the model, the cost ceiling and the MCP mode before
`load_env()` ran, so a filled-in `.env` was silently ignored. `Settings` is now
the only channel, and an AST guard in `tests/test_config.py` fails the build if a
module-level assignment reads the environment again.

**A provider-side stop is a typed `Halt`, not a second boolean.** A run can stop
because of our own circuit breaker or because Vertex refused (quota, outage).
Both must reach the console and the JSON body as a sentence, never a traceback.
Adding a fourth reason is a new `kind`, not a new field on every front end.

### Simulation decisions

**A retake does not buy calendar.** When a note lands, an artist does not get more
time, they get more work inside the time they had. Rework therefore *overlaps*
prior passes rather than extending the schedule, so concurrent load rises. That is
what studio crunch is, and why a burndown alone never predicts it.

**Weekly hours divide by the roster, not by who logged time.** Averaging over
active artists makes a quiet week staffed by two people look identical to crunch.

**Artist assignment is round-robin, not random.** Random assignment piles work onto
unlucky people by sampling alone, which reads as crunch that no scheduling decision
caused.

**Shot entry staggers across elapsed time, not the delivery window.** The original
version left the whole show moving in lockstep waves and act three never reached
comp, so the story beats never fired.

**The retake multiplier does nothing above ~2.2** because the rate is already at
its 0.93 ceiling. Noted in `show.yaml` so nobody tunes it again.

---

## Privacy model

Measuring artist hours can easily become a way to punish artists. The premise is
that crunch is a **scheduling** failure, not a personal one, so the telemetry is
built so it cannot comfortably be used the other way. Enforced in
[`bridge/privacy.py`](bridge/privacy.py), tested in
[`tests/test_privacy.py`](tests/test_privacy.py):

- **Salted HMAC pseudonyms.** HMAC rather than a bare digest — the crew-id space
  is small enough to brute-force from a crew list in seconds. The emitter
  **refuses to start** on a missing or placeholder salt.
- **Aggregation floor of 3.** Below that, an "overloaded pool" alert is an alert
  about one person. Small pools are **dropped, never merged** into an "other"
  bucket, which could be de-anonymised by elimination.
- **No per-person output metric.** Load and waste only, never productivity.
- **Every crunch alert must carry a lever.** An alert with no remediation is just
  pressure.

The floor is a transparent PromQL join —
`… and on(pool) (turnaround_pool_headcount >= 3)` — not a hard-coded pool list.
`di-pool-1` in the show is deliberately two people, so the demo can *show* the
floor suppressing a signal rather than claiming it does. The judge tier enforces
the same line: a run that lets a below-floor pool name into an answer fails
`privacy_floor_respected`, and a Loki-backed alert (`eval-privacy-breach`,
severity critical) fires on it.

---

## The generated show

*Nightfall*, a limited series: 200 shots across 12 sequences, ~21 weeks in,
**81% delivered**, delivery in 19 days, 67 artists in 10 pools. Dates are relative
to seeding time, so the dataset never goes stale. The show is **invented** —
nothing here derives from any studio's real production data.

Two ordinary perturbations are applied to a healthy show and the numbers fall out.
Nothing is hard-coded — remove a beat from `show.yaml` and the figures move on
their own.

- **T-25 days:** a lighting cache regression starts failing frame 118 of every
  SEQ0420 comp render. Nobody connects it to the schedule.
- **T-18 days:** a director note recuts act three. Ordinary — and it lands on a
  pipeline already quietly wasting farm time on exactly those shots.

### Measured outcome

| Signal | SEQ0420 | Rest of show | |
|---|---|---|---|
| Render hours per comp iteration | **9.0 h** | 2.2 h | **4.1×** |
| Wasted render core-hours | **67–81 h** | 5–6 h | ~11× concentration |
| Comp iterations since the note | 1.73 | 1.46 | 1.2× — *only just started* |
| comp-pool-2 weekly hours | 38.6 → 31.9 → 46.1 → **79–90** | — | this week |

Figures shift slightly with seeding time because dates are relative; ranges above
span observed runs.

**The ordering is the argument for the product.** The rework signal is still too
weak for a producer to notice — eighteen days after a note, with a six-day comp
pass, barely two iterations have had time to happen. The crew-load signal is loud
but arrives *after* the overtime has been worked. The render-waste signal was loud
from day one, and lives in a system no producer opens, keyed by a job name no
scheduling tool parses.

An earlier draft of the pitch claimed "6× iterations since the note". That is not
honestly achievable in the elapsed time and would have meant hard-coding the
punchline. The real story is stronger.

---

## Running it

```bash
uv sync --group dev
brew install mcp-grafana                 # 1.3.0; the agent tier needs it
uv run pytest -q                         # 364 tests, offline, no credentials
uv run ruff check .
```

Anything beyond the test suite needs a filled-in `.env` — copy `.env.example`;
[`docs/SETUP.md`](docs/SETUP.md) walks through both clouds. Four values matter:

| Variable | Where it comes from |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Your stack's **OpenTelemetry tile** — not the docs. Host varies by region and stack vintage; a wrong one fails silently |
| `OTEL_EXPORTER_OTLP_HEADERS` | Same tile. Basic auth, `instanceID:token`, URL-encoded |
| `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN` | Service account, Editor or above. Used by `mcp-grafana` and the annotations API |
| `TURNAROUND_PSEUDONYM_SALT` | Anything real. The emitter refuses the placeholder |

> **Re-seed before every live session.** The whole history is compressed into a
> ~45-minute window ending at the moment of seeding, and every analyst query and
> alert rule reads it with `last_over_time((…)[2h:])`. A stack seeded more than
> about two hours ago answers every query with nothing, and the analysts then
> have no numbers to ground on — which presents as a broken agent rather than as
> stale data. `seed/refresh.py` re-seeds on a loop for a long session.

`seed.*` and `grafana.*` do **not** load `.env` themselves; the agent tier does.

```bash
set -a && source .env && set +a
uv run python -m seed.populate           # writes several thousand series; --dry-run first
uv run python -m grafana.provision       # dashboards, alert groups, ML jobs — idempotent

uv run python -m agent.run "Why is SEQ0420 slipping, and what is it costing in artist-days?"
uv run python -m agent.run "Who is heading for crunch, and when?"
uv run python -m agent.run --interactive "What do I change to avoid both?"   # prompts per write
uv run python -m agent.run --approve     "What do I change to avoid both?"   # auto-approves
```

A plain run never mutates the stack: the default approver denies every write.
Output is the synthesised answer, then the tool timeline (every Grafana MCP call
`*`-marked), then the judge scorecard and any approval decisions.

Flags: `--no-observability` skips self-instrumentation, `--no-eval` skips the
judge tier. Exit codes: **0** answered, **2** unconfigured (the message names the
missing keys), **3** halted with nothing to show.

---

## Verification gate

The go/no-go. One `shot_id` must resolve across all three signals.

**Traces** (Explore → Tempo): `{ .production.shot_id = "SEQ0420_SH0100" }` —
expect one trace per shot, spans per department, comp spans in **error** status
where rework occurred.

**Logs** (Explore → Loki):
`{service_name="turnaround-bridge"} | production_shot_id = "SEQ0420_SH0100"` —
expect both status transitions and the `frame 118` cache-miss errors, each
carrying the trace id above.

**The join — this single query is the product's thesis.** Render hours per comp
iteration, by sequence, spanning the compute plane (farm core-hours) and the
creative plane (tracker iterations) on a shared key:

```promql
sum by (sequence) (increase(turnaround_render_core_hours_total{department="comp"}[14d]))
/
sum by (sequence) (increase(turnaround_task_iterations_total{department="comp"}[14d]))
```

Expect SEQ0420 at roughly **4×** every other sequence. On the compressed time
base use `[5m]` in place of `[14d]`.

**Crew load, respecting the floor:**
`turnaround_artist_hours_logged{department="comp"}` — `di-pool-1` must be absent
from any crew-load alert; it is under the floor.

---

## Hackathon context

**Agentic Cinema: The Blockbuster Hackathon** — Grafana Labs track.

| Criterion | How Turnaround answers it |
|---|---|
| Technological Implementation | Real industry conventions (OpenCue job names, Kitsu/ShotGrid/ftrack status vocabularies), Grafana MCP as the only tool surface, Tempo/Loki/Mimir/alerting/annotations, Grafana ML doing real prediction, ADK multi-agent on Vertex AI, and the agent self-instrumented with the OTel GenAI conventions |
| Design | Crew Load hero view, a visible evidence chain, an approval queue |
| Potential Impact | A documented labour crisis with hard numbers; the audience is named in the rules; the mechanism addresses the stated cause |
| Quality of the Idea | Two non-obvious moves — *a shot is a distributed trace*, and *joining the creative plane to the compute plane*. Neither reimplements Grafana Assistant |

Constraints that still shape the code: Apache-2.0 and detectable; a hosted
project URL; a ≤3-minute public demo showing the project functioning as built;
the partner MCP imported and *called*, not named in a README; and **no non-Google
AI in the runtime path** (Claude Code as a *development* tool is fine; the
deployed agent is Gemini/Vertex only — pinned by
`test_reproducibility.py::test_no_non_google_ai_sdk_on_the_runtime_path`).

The hosted Grafana Cloud MCP endpoint is **interactive-OAuth only with no
service-account path**, which is why the deployed, unattended path runs OSS
`grafana/mcp-grafana` with a service-account token, and hosted mode exists as an
opt-in.

---

## Roadmap

| Phase | Work | Gate | State |
|---|---|---|---|
| 1 | Data plane | Correlation confirmed in a real stack | ✅ |
| 2 | The show | Story present and measured | ✅ |
| 3 | Dashboards; ML forecasts; outlier detectors; alert rules | Forecast differs from plan; crew alert fires on comp-pool-2 | ✅ |
| 4 | MCP read-only + write instances; ADK pipeline; approval gate; write-back | Four demo questions answered cold, tool timeline showing real MCP calls | ✅ 4/4 |
| 5 | EvalOps: self-instrumentation, judge tier, EvalOps surface | Trace + eval events land in the same stack; drift and privacy alerts evaluate | ✅ |
| 6 | Cloud Run deploy *run*; supervisor console | Full demo against the public URL in a clean browser profile | ✅ deployed — [https://turnaround-agent-b465d3vxhq-uc.a.run.app](https://turnaround-agent-b465d3vxhq-uc.a.run.app); supervisor console still open |
| 7 | Video, README, Devpost | Submitted | in progress — [`docs/DEMO.md`](docs/DEMO.md) |

**Open by decision.** Live Kitsu and OpenCue instances: no container runtime on
the build machine. Source adapters sit behind Protocols so real instances drop in
without a rewrite, and job names already follow OpenCue's real convention and
round-trip through the production parser in the test suite, so the join is
exercised exactly as it would be on a real farm. Thin live API clients for
ShotGrid / ftrack / Deadline are likewise unwritten; the parsers and Protocols
are in.

---

## Risks

| Risk | Mitigation |
|---|---|
| Stale seed on demo day reads as a broken agent | Re-seed immediately before recording; `seed/refresh.py` for long sessions. This is the single most likely live failure |
| Vertex per-minute quota exhausted by consecutive questions | Typed `Halt(kind="model_quota")`: one sentence, exit 3, no traceback. Space the questions, or raise project quota |
| Synthetic data reads as fake | Provenance stated plainly; real OpenCue job-name convention round-tripped in tests; a simulated *mechanism* rather than a hard-coded outcome |
| Agent non-determinism on camera | Fixed seed, fixed pipeline shape, scripted `story.md`, rehearsed question set |
| Forecast quality on limited history | Prophet jobs need ~100+ continuous points; on the compressed base that means `refresh.py` running for a couple of hours before the forecasts train |
| No live Kitsu/OpenCue costs implementation points | Adapters behind Protocols; restore if a container runtime becomes available |
| Backfilled data awkward to remove | `--dry-run` first; consider a throwaway stack |
