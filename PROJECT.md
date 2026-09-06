# Turnaround — project reference

Master reference for objective, architecture, design decisions and progress.
Working notes live here; [`README.md`](README.md) is the public front door and
[`seed/story.md`](seed/story.md) is the measured demo narrative.

**Status:** Phases 1–3 **passed** in a live stack. **Phase 4 in progress** —
the `agent/` tier is built and wired: a deterministic five-step ADK pipeline
(three read-only analysts on `mcp-grafana --disable-write`, a gated Remediator,
a synthesis step) answers demo questions cold against Grafana Cloud MCP with a
recorded tool timeline. 133 tests. Two of the four demo questions
(`why is SEQ0420 slipping / what is it costing`, `who is heading for crunch`)
verified end to end; the remediation/write-back path is wired and gated but was
mid-verification when the session paused. Grafana Cloud stack `your-stack`
(region `prod-ap-southeast-1`). GCP / Vertex AI reachable (`gemini-2.5-flash`;
`gemini-2.5-pro` is 429 quota-locked on this project, so everything runs flash).
Private repo: `github.com/Y-WenBin/AgenticCinema-Grafana-Turnaround`.
**Last updated:** 2026-09-06.

---

## 1. Objective

### The problem

The VFX labour crisis is documented and specific: roughly **70% of VFX workers
report unpaid overtime**, about **two-thirds say conditions are unsustainable**,
80-hour weeks are routine, and ~**75% report working through legally mandated
breaks**. The cause people name is exactly what telemetry can measure —
unrealistic deadlines, plus shots corrected or remade entirely until the
schedule collapses into day-and-night work.

Crunch is not a surprise. It is a **forecastable consequence of rework and
render waste**, visible weeks ahead in data that already exists. Nobody looks,
because the evidence lives in two systems that never meet:

| Who | What they see | What they miss |
|---|---|---|
| Producers | Kitsu / ShotGrid — shots, statuses, assignments, hours | why a stage keeps repeating |
| Systems / IT | OpenCue — job failures, frame retries, queue depth | which shot, which deadline, which crew |

Join them and a class of invisible waste becomes obvious:

> *The artist isn't slow. Every comp iteration burns nine hours because frame
> 118 keeps failing.*

That join does not exist in any product on the market.

### What Turnaround is

A Gemini/ADK multi-agent system that joins the creative schedule to the render
farm inside Grafana, forecasts delivery slip *and* crew overload together, and
proposes evidence-backed corrections a supervisor approves — while the schedule
can still be renegotiated.

**Audience:** VFX producers, post supervisors, and the crews they staff.

### The generalisable thesis

Grafana already ships an OpenCue integration. Studios still don't use Grafana.
**The gap isn't data or connectors — it's vocabulary and audience.** Grafana's
data model is general-purpose; its interface assumes PromQL.

Turnaround is one instance of a repeatable pattern: an **MCP-driven domain
semantic layer** that ingests domain events over OTLP, owns the ontology mapping
domain vocabulary to query language, and returns *decisions with evidence*
instead of charts. VFX is the first vertical; the same shape serves OR
scheduling, construction, logistics, legal discovery.

---

## 2. Hackathon context

**Agentic Cinema: The Blockbuster Hackathon** — Grafana Labs track.
Deadline **2026-09-09, 14:00 PT**. Judging 2026-09-23 → 10-07.
Prizes per track: $7,500 / $4,500 / $3,000.

### Judging criteria (equally weighted)

| Criterion | How Turnaround answers it |
|---|---|
| Technological Implementation | Real industry software (OpenCue, Kitsu), four MCP toolsets, Tempo/Loki/Mimir/alerting/incidents/annotations, Grafana ML doing real prediction, ADK multi-agent on Vertex AI, agent self-instrumented with Grafana AI Observability |
| Design | Supervisor console with a Crew Load hero view, visible evidence chain, approval queue |
| Potential Impact | Documented labour crisis with hard numbers; audience named in the rules; mechanism addresses the stated cause |
| Quality of the Idea | Two non-obvious moves — *a shot is a distributed trace*, and *joining the creative plane to the compute plane*. Neither reimplements Grafana Assistant. |

### Hard requirements (from the Official Rules)

- Solve bottlenecks **"targeting the workflows of filmmakers, screenwriters, studio crews, or fans"**
- Public repo, **open-source licence detectable in the About box** — Apache-2.0 ✅
- **Hosted project URL**, running on web / Android / iOS
- ≤3-min public YouTube/Vimeo demo, English, showing the project *functioning as built*
- Runtime proof in code: `google-adk` / `google-genai` / `google-cloud-aiplatform`
  **and** a live Grafana MCP connection — imported and called, not named in a README
- Project newly created within the contest period (from 2026-07-27)
- **No non-Google AI at runtime.** The rules bar all non-Google AI models, agent
  frameworks and AI APIs, naming Anthropic and OpenAI explicitly. Claude Code as
  a *development* tool is fine; the deployed agent must be Gemini/Vertex only,
  and no other AI SDK may appear in the runtime path.

### Why the original concept was replaced

The earlier "autonomous SRE for render farms" idea was dropped for four reasons,
which still constrain design decisions today:

1. **Audience mismatch — Stage One pass/fail risk.** A GKE node-cordoning agent
   serves studio IT/DevOps, who are not on the rules' list.
2. **It rebuilds the sponsor's own product, worse.** Grafana Assistant
   Investigations (GA Oct 2025, agents expanded Jul 2026) already does
   hypothesis-driven RCA across metrics/logs/traces/profiles; Sift already runs
   detectors unprompted. A Grafana Partner Architect is on the judging panel.
3. **The showpiece was invisible.** Model Armor and a fail-closed gateway render
   as nothing on screen in a three-minute video.
4. **A missed constraint.** The hosted Grafana Cloud MCP endpoint is
   interactive-OAuth only with no service-account path, so a hosted, judged,
   unattended app must run OSS `grafana/mcp-grafana` with a service account token.

---

## 3. Architecture

```
Kitsu (gazu: task events + time-spents) ─┐
                                          ├─► bridge/ ─► OTLP ─► Grafana Cloud
OpenCue (Prometheus exporter :8302) ──────┘                      Tempo · Loki · Mimir
                                                                 ML forecasts · Alerts
                                                                        │ MCP
                                                                        ▼
                              Cloud Run: ADK multi-agent on Gemini (Vertex AI)
                                Producer ─ ScheduleAnalyst · FarmAnalyst
                                         ─ CrunchGuardian · Remediator [gated]
                                mcp-grafana --disable-write  → analysts
                                mcp-grafana  write-capable    → Remediator only
                                                                        │
                              Grafana AI Observability watches the agent itself
```

Analysts are read-only **by construction**, not by prompt — wired to a
`mcp-grafana` instance started with `--disable-write`. Only the Remediator can
mutate anything, and every call is intercepted by an approval gate showing the
supervisor the full evidence chain before anything is written back.

---

## 4. The ontology

Defined once in [`bridge/ontology.py`](bridge/ontology.py); every other
component derives its vocabulary from there, so a producer's question and a
PromQL query refer to the same thing.

| Production concept | Telemetry primitive |
|---|---|
| Shot `SEQ0420_SH0100` | **trace**, `trace_id` derived from the shot id |
| Task stage (previz → layout → anim → fx → lighting → comp → di) | **span** |
| Retake / rejection | span **error + retry** |
| Status transitions, notes, render + QC logs | **Loki** streams |
| Burndown, iterations, artist hours, farm counters | **Mimir** metrics |
| Director note, cut change, date change | **annotation** |
| "Seq 42 will miss its date" · "comp pool trending to 68h" | **ML forecast + alert** |
| "Vendor B behaves unlike every other vendor" | **outlier detector** |

Modelling a retake as a span error is not decoration: Tempo's own error-rate
tooling then counts rework with no bespoke query.

### The join

OpenCue's PyOutline names every job `<show>-<shot>-<user>_<name>` (format string
`"%s-%s-%s_%s"`). **The shot id is already in the farm's telemetry.**
`parse_opencue_job_name` recovers it and relabels farm metrics onto
`shot_id` / `sequence` / `department`.

Two details that make it robust:

- It **anchors on the shot id** (`SEQ\d{4}_SH\d{4}`) rather than splitting on
  hyphens, so studio show slugs like `night-fall-s2` survive.
- It returns **`None`, not an exception**, for unattributable jobs — farm
  maintenance and ad-hoc tool jobs are real and must not crash a tap.

Everything else in the product follows from that one relabel.

---

## 5. Module reference

| File | Lines | Purpose |
|---|---:|---|
| [`bridge/ontology.py`](bridge/ontology.py) | 313 | Production ↔ telemetry mapping; the OpenCue join; metric and attribute names |
| [`bridge/privacy.py`](bridge/privacy.py) | 118 | Pseudonymisation, PII guard, aggregation floor |
| [`bridge/emit.py`](bridge/emit.py) | 313 | Backfilled spans with derivable ids; trace-correlated Loki lines |
| [`bridge/metrics.py`](bridge/metrics.py) | 258 | Historical metric backfill over OTLP; label rules; chunking |
| [`bridge/testing.py`](bridge/testing.py) | 36 | In-memory exporters, shipped with the package |
| [`seed/show.yaml`](seed/show.yaml) | 123 | The show declaration and its two story beats |
| [`seed/model.py`](seed/model.py) | 414 | The simulation |
| [`seed/populate.py`](seed/populate.py) | 254 | Driver: history → spans, logs, metrics; `--dry-run` |
| [`seed/story.md`](seed/story.md) | 59 | The measured demo narrative |
| [`grafana/build.py`](grafana/build.py) · [`grafana/provision.py`](grafana/provision.py) | — | Dashboards + idempotent pusher (Phase 3) |
| [`grafana/alerts/build.py`](grafana/alerts/build.py) · [`grafana/ml/build.py`](grafana/ml/build.py) | — | Alert rules and Grafana ML jobs (Phase 3) |
| [`agent/config.py`](agent/config.py) | 130 | `.env` loader, Vertex bootstrap, model + datasource-UID settings, locates `mcp-grafana` |
| [`agent/vocabulary.py`](agent/vocabulary.py) | 175 | Ontology → prompt: metric catalogue, PromQL recipes (compressed base + `last_over_time` wrapper), tool-calling rules, privacy + time-base rules |
| [`agent/mcp_grafana.py`](agent/mcp_grafana.py) | 100 | `McpToolset` factories: read-only (`--disable-write`) narrowed per analyst; write (`--enabled-tools annotations,incident`) for the Remediator |
| [`agent/timeline.py`](agent/timeline.py) | 130 | `ToolTimeline` — records every tool call (agent, tool, args, ms, ok), marks the Grafana MCP ones. The Phase 4 gate's evidence |
| [`agent/approval.py`](agent/approval.py) | 150 | `ApprovalGate` + `EvidenceLedger`; `Approver` protocol (`AutoApprover`, `CliApprover`); blocks every mutating call until a human approves |
| [`agent/writeback.py`](agent/writeback.py) | 135 | Kitsu write-back behind a `Protocol`: `RecordingKitsu` (JSONL + Grafana annotation) now, `GazuKitsu` when a studio instance exists |
| [`agent/analysts.py`](agent/analysts.py) | 175 | `schedule_analyst`, `farm_analyst`, `crunch_guardian` — read-only LlmAgents, each `output_key` feeds the ledger |
| [`agent/remediator.py`](agent/remediator.py) | 110 | The one write-capable agent; every tool call goes through the gate + timeline |
| [`agent/producer.py`](agent/producer.py) | 120 | `build_system()` → `SequentialAgent`[schedule, farm, crunch, remediator, synthesis] + shared timeline/gate/ledger |
| [`agent/run.py`](agent/run.py) | 90 | CLI: `uv run python -m agent.run [--approve\|--interactive] "<question>"` |

Tests: 133 across twelve files.

Dependencies: `google-adk==2.8.0`, `google-genai==2.22.0`, `mcp==1.29.1`
(pinned `<2`; ADK predates the mcp 2.x API rename),
`opentelemetry-sdk==1.42.1`, `gazu==1.2.2`, `fastapi==0.141.1`, `pyyaml==6.0.3`.
Python 3.12 via `uv`. The `mcp-grafana` binary is a separate install:
`brew install mcp-grafana` (1.3.0). `agent/config.py` finds it on PATH or at
`/opt/homebrew/bin`.

---

## 6. Design decisions worth knowing

Each of these was a bug or a wrong assumption found while building, not a
preference. They are recorded because re-deriving them is expensive.

**Trace ids are derived, not discovered.** `DeterministicIdGenerator` derives the
trace id from the shot id and the span id from
`(shot, department, iteration)`. This buys three things: the agent computes a
shot's trace id from a producer's question with no lookup table; re-running the
seeder overwrites rather than duplicates; and a scripted demo stays reproducible.
It falls back to random ids when unseeded, so the agent's own instrumentation —
which shares the process — is unaffected.

**Spans are backfilled with explicit timestamps.** A shot's trace spans weeks, so
spans cannot be timed in-process. Timezone validation lives in
`StageEvent.__post_init__` rather than at export: production data crosses
facilities, so a naive timestamp is a silent hours-out error and must fail where
it is introduced. *(Found by a test that failed with `TypeError` from the
comparison before the check ran.)*

**Every stage event is written twice** — as a span *and* as a trace-shaped Loki
line carrying the same trace and span ids. TraceQL tools are **not** in
`mcp-grafana`'s default tool set (they live on the separate Cloud Traces MCP
endpoint); Loki's tools are. The redundancy guarantees the agent can reconstruct
a shot's history through tools we know exist. Tests pin that the ids genuinely
match — a redundant path that disagrees with the primary one is worse than none.

**Counters accumulate inside `metrics.py`.** Callers pass increments, which is
what a simulation produces naturally. `start_time_unix_nano` is pinned per
series so `rate()` behaves the same over backfilled data as over live data. A
mis-accumulated counter yields a series that looks entirely plausible and rates
to nonsense.

**No metric may carry an artist label — a hard failure, not a filter.** This is
not merely cardinality. A per-person series survives every aggregation floor
downstream, because the floor filters *queries* while the label is already in
the *data*.

**`--dry-run` exercises the full write path.** Most backfill mistakes — a
counter that never accumulates, a timestamp in the wrong unit, a label that
explodes cardinality — are invisible until the data is already in the stack and
awkward to remove.

**History is compressed into the ingestion window, not into the data.** Hosted
Mimir rejects any sample older than a ~1 h out-of-order window
(`err-mimir-sample-timestamp-too-old`; measured on `your-stack`: 65 min
accepted, 90 min refused). Live OTLP push therefore cannot carry a five-month
backfill as-is. [`bridge/timewarp.py`](bridge/timewarp.py) applies a **monotonic
affine map** at the single `datetime → unix_nano` seam in `emit.py` and
`metrics.py`: `[earliest .. now]` onto `[now − window .. now]`, default window
45 min. The *simulation* stays in real calendar time — every day/week bucket,
every overlap argument untouched — so only the time axis scales; every series
keeps its exact shape. Queries use proportional ranges: `increase(...[14d])`
becomes `increase(...[5m])` (scale ≈ 0.0002, 1 real day ≈ 17.6 s). Off unless
`seed.populate` configures it; `--compress 0` restores real timestamps for a
self-hosted stack with the window widened. 12 tests in
[`tests/test_timewarp.py`](tests/test_timewarp.py) pin endpoints, monotonicity
and identity-when-off.

### Simulation decisions

**A retake does not buy calendar.** When a note lands, an artist does not get
more time, they get more work inside the time they had. Rework therefore
*overlaps* prior passes rather than extending the schedule, so concurrent load
rises. That is what studio crunch is, and why a burndown alone never predicts it.

**Weekly hours divide by the roster, not by who logged time.** Averaging over
active artists makes a quiet week staffed by two people look identical to crunch.

**Artist assignment is round-robin, not random.** Random assignment piles work
onto unlucky people by sampling alone, which reads as crunch that no scheduling
decision caused.

**Shot entry staggers across elapsed time, not the delivery window.** The
original version left the whole show moving in lockstep waves — every department
either drowning or idle — and act three never reached comp, so the story beats
never fired.

**The retake multiplier does nothing above ~2.2** because the rate is already at
its 0.93 ceiling. Noted in `show.yaml` so nobody tunes it again.

---

## 7. Privacy model

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
- **Every crunch alert must carry a lever.** An alert with no remediation is
  just pressure.

`di-pool-1` in the show is deliberately two people, so the demo can *show* the
floor suppressing a signal rather than claiming it does.

---

## 8. The generated show

*Nightfall*, a limited series: 200 shots across 12 sequences, ~21 weeks in,
**81% delivered**, delivery in 19 days, 67 artists in 10 pools. Dates are
relative to seeding time, so the dataset never goes stale.

Two ordinary perturbations are applied to a healthy show and the numbers fall
out. Nothing is hard-coded — remove a beat from `show.yaml` and the figures move
on their own.

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

Figures shift slightly with seeding time because dates are relative; ranges
above span observed runs.

**The ordering is the argument for the product.** The rework signal is still too
weak for a producer to notice — eighteen days after a note, with a six-day comp
pass, barely two iterations have had time to happen. The crew-load signal is
loud but arrives after the overtime has been worked. The render-waste signal was
loud from day one, and lives in a system no producer opens, keyed by a job name
no scheduling tool parses.

An earlier draft of the pitch claimed "6× iterations since the note". That is
not honestly achievable in the elapsed time and would have meant hard-coding the
punchline. The real story is stronger.

---

## 9. Progress

### Complete and tested

- **Ontology** — pipeline model, status normalisation, shot identity, the
  OpenCue join, metric/attribute names.
- **Privacy layer** — pseudonymisation, PII guard, aggregation floor.
- **Emitter** — backfilled spans with derivable ids, trace-correlated Loki lines.
- **Metric backfill** — OTLP historical series, counter accumulation, label
  rules, chunking.
- **Simulation** — the show, the two story beats, the crunch mechanism.
- **Seeder driver** — full write path with `--dry-run`.

A dry run produces **1,454 spans, 1,476 correlated log lines, 5,917 metric
points** without touching the network. The same run against `your-stack`
completes in ~4 s.

### Verified in the live stack (2026-09-06)

- **Traces** — `{ .production.shot_id = "SEQ0420_SH0100" }` returns one trace,
  department spans, comp spans in error status. Tempo accepts the historical
  span times directly (no compression needed for traces, but the seeder warps
  them anyway so the trace and metric time axes align).
- **Logs** — `{service_name="turnaround-bridge"}` carries ~1,476 lines. For one
  shot the status transitions (`comp -> retake (iteration 1)`, `-> wip
  (iteration 3)`, …) and the `frame 118` cache-miss errors all resolve, each
  on the derived trace id. This is the fallback path for when the agent cannot
  reach a trace.
- **Metrics** — all ten `turnaround_*` series present. The thesis join
  (`increase(render_core_hours[5m]) / increase(task_iterations[5m])`, by
  sequence) puts **SEQ0420 well above every other sequence**; render-waste
  totals are **~11× concentrated** on SEQ0420. Correlation holds.
- **Crew load** — `turnaround_artist_hours_logged` by pool/department present;
  comp-pool-1/2 highest. Floor behaviour (`di-pool-1` suppression) is an
  alert-layer concern, still to wire in Phase 3.

**Bug found and fixed on the way (`bridge/emit.py`).** The constructor tested
`span_exporter is None` to decide whether to attach a log processor — but
`span_exporter` had already been reassigned to a real `OTLPSpanExporter` a few
lines up, so on the seeder's path the condition was always false and **no log
processor was ever attached**. Every `emit_log` call was silently discarded;
`flush()` returned True because there was nothing queued. Two failed seeds
(pre- and post-compression) landed zero logs for this reason, not a Loki
problem. The path now keys off a `real_otlp` flag captured before the
reassignment, with a regression test that asserts a log processor is attached
when both exporters default (it fails on the old code).

### Phase 3 — dashboards, alerts, ML (2026-09-06)

Everything under `grafana/`, pushed by one idempotent `grafana/provision.py`
(needs `GRAFANA_URL` + the service-account token). `uv run python -m
grafana.build` / `grafana.alerts.build` / `grafana.ml.build` regenerate the
committed JSON.

- **Three dashboards** in a `Turnaround` folder: *Crew Load* (the hero —
  weekly hours per rostered artist by pool, the floor join made visible),
  *The Join* (cumulative render core-hours per comp iteration by sequence —
  SEQ0420 sits clear of the pack — plus render waste and frame-failure rate),
  *Delivery* (burndown, cumulative iteration load, vendor turnaround). A
  `director-note` annotation query is wired on all three.
- **`turnaround_pool_headcount`** added to the seed so the aggregation floor is
  a transparent PromQL join (`... and on(pool) (turnaround_pool_headcount >=
  3)`) rather than a hard-coded pool list. `di-pool-1` is 2.
- **`bridge/annotate.py`** — the director note now lands as a Grafana
  annotation, timewarped like everything else.
- **Two alert rules**, provisioned, **firing correctly**: *crew crunch* on
  comp-pool-1 and comp-pool-2 but **not di-pool-1** (floor join), and *render
  waste* on **SEQ0420 only**. Each carries a `lever` annotation — a remediation,
  not just pressure. Queries are wrapped in `last_over_time(...[3h:1m])` so a
  single seed keeps them evaluable.
- **Grafana ML** — three jobs provisioned via the ML app API
  (`grafana-ml-app/resources/manage/api/v1`): two Prophet forecasts
  (burndown, comp-pool-2 hours) and one MAD outlier detector on vendor
  turnaround. The forecasts need ~100+ points of continuous history to train;
  on the compressed time base that only accumulates once `seed/refresh.py` (or
  the Cloud Run job) has been re-seeding for a couple of hours, so in a
  single-seed session they sit in `error: "No series to train"`. Documented,
  not faked — `predict_linear` panels were tried and removed because repeated
  re-seeds reset the counters and the extrapolation went to nonsense.

**Phase 3 gate.** Crew alert fires on comp-pool-2 ✅. "Forecast differs from
plan" is carried by the alert itself (a forward-looking *"heading past 60 h"*
that trips before a burndown would show it) and by The Join showing SEQ0420's
per-iteration cost elevated since before the note — the story's real argument.
The Prophet charts are infrastructure-complete but need the deployment's
continuous history to render.

### Phase 4 — MCP wiring + the agents (2026-09-06, in progress)

Everything under `agent/`. `mcp-grafana` (OSS, `brew install`, 1.3.0) runs as a
**stdio subprocess** ADK spawns and owns — no ports. Two privilege tiers, both
enforced by how the server is started, not by prompt:

- **Analysts** → `mcp-grafana --disable-write`, further narrowed with
  `tool_filter` (Prometheus + Loki + annotations + alerts; FarmAnalyst also
  Tempo). A prompt-injected "change X" has nothing to call.
- **Remediator** → `mcp-grafana --enabled-tools annotations,incident`. Can
  create/update an annotation or add incident activity — nothing else. Every
  call still passes `ApprovalGate.before_tool` first.

**The pipeline is deterministic, not model-routed.** Flash, asked to "consult
the right specialists then synthesise", reliably stopped after one and echoed
it. So `build_system()` returns a `SequentialAgent`: `schedule_analyst →
farm_analyst → crunch_guardian → remediator → synthesis`. Every question runs
the whole board (a few extra flash calls; a demo that behaves the same each
take — Risks table, "agent non-determinism on camera"). Each analyst writes its
answer to a distinct `output_key`; an `after_agent_callback` copies that into
the `EvidenceLedger` the approval gate shows, so the evidence chain is built
without a coordinator remembering to.

**The approval gate.** `WRITE_TOOLS` (annotations, incidents, `kitsu_write_back`)
are intercepted; the gate renders the proposed write + the full evidence chain,
asks the `Approver` (`AutoApprover(approve=False)` default, `CliApprover` for
`--interactive`, `--approve` to auto-yes), and on a "no" returns a
`status: "blocked"` dict the model is told to report rather than retry. Every
decision is kept in `gate.decisions` for the console and the write-up.

**Kitsu write-back** is behind a `Protocol`. `RecordingKitsu` (the default)
appends each write to `agent/_writeback.jsonl` *and* drops a Grafana annotation
so the loop closes on the same dashboards the evidence came from. `GazuKitsu` is
the real path, selected only when `TURNAROUND_KITSU_LIVE` is set and `KITSU_URL`
is non-loopback; it never raises into the agent.

**Verified end to end (live stack + Vertex):**

- *"Why is SEQ0420 slipping, and what is it costing in artist-days?"* — both
  analysts ran, **9 real Grafana MCP calls**, every number in the evidence
  chain carries the exact PromQL behind it: JOIN 4.3 core-h/comp-iteration vs
  ~2.2, waste ~44 core-h, frame-failure 9.4%, `frame 118` cache-miss quoted
  from Loki, director note found. Cost ≈ 65 artist-days/week of comp overtime,
  arithmetic shown.
- *"Who is heading for crunch, and when?"* — `IN CRUNCH NOW (>60h): comp-pool-1
  88.8h, comp-pool-2 85.1h, lighting-pool-2 63.2h`. `di-pool-1` (2 people)
  correctly absent — the floor join holds through the agent.

**Findings that shaped it (bugs/wrong assumptions, not preferences):**

- **A bare instant query at `now` returns nothing.** The compressed window ends
  when the seeder finishes; minutes later the newest sample is outside
  Prometheus's 5-minute instant lookback. Every "current state" recipe is
  wrapped `last_over_time((<expr>)[2h:])` — the same trick the alert rules use.
- **`gemini-2.5-pro` is 429 quota-locked on this project.** First call,
  `RESOURCE_EXHAUSTED`. Everything runs `gemini-2.5-flash`;
  `TURNAROUND_GEMINI_MODEL_PRO` opts the synthesis/remediator up if quota lands.
- **ADK aborts the whole run** with `ValueError: Tool 'x' not found` if a model
  calls a tool outside its `tool_filter`. Not worth risking on camera to save
  tool-schema tokens, so all three analysts share one broad read core.
- **`mcp` 2.x renamed the SDK API**; ADK 2.8 needs `mcp<2`. Pinned `1.29.1`.
- `SequentialAgent` is deprecated in ADK 2.8 for a `Workflow` type that "cannot
  yet be used as an LlmAgent sub-agent" — so `SequentialAgent` is still correct
  here. Revisit when `Workflow` composes.

**Still to close before the Phase 4 gate is met:**

- The remediation/write-back path (`agent.run --approve "what do I change…"`)
  was mid-verification when the session paused: the pipeline reaches the
  Remediator, the gate fires, `kitsu_write_back` and `create_annotation` are
  called — last fix was making `default_writeback` ignore the `.env.example`
  loopback `KITSU_URL` so `RecordingKitsu` is used. Needs one clean run to
  confirm the annotation lands and the JSONL row is written.
- Demo questions 2 (*"what is it costing in artist-days"* on its own) and 4
  (*"what do I change to avoid both"*) want one more pass each for answer
  polish.
- Cloud Traces MCP: `mcp-grafana` 1.3 ships `tempo_*` tools in the default set
  (PROJECT.md §6 said they were on a separate endpoint — no longer true), so the
  FarmAnalyst can query traces directly. Not yet exercised in a live run.

### EvalOps tier — GenAI self-instrumentation + judge + surface (2026-09-06)

Answers the "Agentic EvalOps & Telemetry Gateway" proposal: make the *agent*
legible in Grafana the way the *shot* already is, and score its answers.

- **`observability/`** — a standalone package (no `agent/`/`bridge/` import; the
  PII guard is injected). `instrument()` returns an ADK `BasePlugin` +
  `GenAiTelemetry`, wired once on the `Runner` in `agent/run.py`
  (`--no-observability` to skip). Emits, to the same OTLP gateway as the seeder
  under `service.name=turnaround-agent`: an `invoke_agent` root span (opened in
  `run.py`) with `chat` and `execute_tool` children per the OTel GenAI
  conventions (`gen_ai.provider.name`, `request.model`, `usage.*_tokens`,
  `response.id`, `finish_reasons`, `tool.name`/`tool.call_id`, plus a
  `turnaround.query` pivot attribute); `gen_ai.client.token.usage` and
  `gen_ai.client.operation.duration` histograms; opt-in prompt capture behind
  `TURNAROUND_CAPTURE_CONTENT` (guarded by `assert_no_pii`).
  *Two ADK-shape bugs found and fixed in live runs: don't `context.attach` across
  callback boundaries (root span belongs in `run.py`); pair `chat` spans on a
  stack, not `id(callback_context)` — ADK passes different objects to
  before/after. `tests/test_observability.py`.*
- **`agent/evaluation.py`** — after `synthesis`, a `DeterministicJudge`
  (ground-truth anchors + the `privacy_floor_respected` invariant — a sub-floor
  pool named anywhere = fail) and an `LlmJudge` (second Gemini, no tools:
  relevance / hallucination / task_completion). Each check is one
  `gen_ai.evaluation.result` log event (JSON body, `| json`-queryable in Loki),
  correlated by `gen_ai.response.id`. `agent/run.py --no-eval` skips.
  `tests/test_evaluation.py`. *The LLM judge does flag real run-to-run variation
  — hallucination 0.3–1.0 depending on how much of a figure's derivation the
  answer shows — which is the point, not a bug.*
- **`grafana/evalops/build.py`** → `dashboards/turnaround-evalops.json` (11
  panels: Loki eval stats + score-by-dimension drift + an events table linking
  `response_id` → the run's Tempo trace; Prom token/latency/tool-fan-out).
  Two Loki-backed alert rules in a new `turnaround-evalops` group:
  `eval-grounding-drift` (mean grounding < 0.8) and `eval-privacy-breach`
  (`privacy_floor_respected=fail` > 0, severity critical), each with a `lever`.
  `provision.py` pushes both unchanged. `tests/test_evalops_grafana.py`.
  Verified live on `your-stack`: full trace, metrics, 6 eval events/run,
  alerts evaluate `inactive health=ok`.

### Grafana MCP: two modes (2026-09-06)

`TURNAROUND_MCP_MODE` (`agent/config.py`), default **`oss`** — unchanged: local
`grafana/mcp-grafana` stdio subprocess, SA token, read-only *by construction*
(`--disable-write`). This is the deployed/demo path.

**`hosted`** (opt-in) targets `https://mcp.grafana.com/mcp` over Streamable HTTP
with the instance in `X-Grafana-URL` and a bearer from the OAuth 2.1 flow
(`agent/mcp_login.py` — PKCE + dynamic client registration, browser consent,
localhost callback, token to `.secrets/grafana-cloud-mcp-token`). The hosted
endpoint has no service-account path, so this mode cannot run unattended and is
not the deploy target; it exists to exercise the interactive authorization the
proposal assumes. In hosted mode the read/write split is enforced by
`tool_filter` + the approval gate, not a server flag. `agent/mcp_grafana.py`
raises `HostedMcpNotAuthorized` with a pointer to `mcp_login` if no token is
present. `tests/test_mcp_grafana.py`.

### Not started

Supervisor console (folds into the Scenes App Plugin, Horizon B) · Cloud Run
deploy · demo video.

### Deferred by decision

**Live Kitsu and OpenCue instances.** No container runtime is installed
(no Docker, colima, podman or lima; Homebrew is present). Source adapters sit
behind an interface so real instances drop in without a rewrite. Job names
already follow OpenCue's real convention and are round-tripped through the
production parser in the test suite, so the join is exercised exactly as it
would be on a real farm.

*Trade-off to revisit:* "real industry software, not a mock" is a meaningful part
of the Technological Implementation score. Worth restoring before submission if
time allows.

---

## 10. Running it

```bash
cd /Users/wenbin/turnaround
uv sync --group dev
brew install mcp-grafana                       # 1.3.0; the agent tier needs it
uv run pytest -q                               # 133 tests (hermetic; no network)
TURNAROUND_PSEUDONYM_SALT=dev uv run python -m seed.populate --dry-run
```

### Restarting a session (what to do first)

1. `.env` is git-ignored and holds every live credential — it must already be on
   the machine. `.secrets/gcp-sa.json` (the Vertex service-account key) likewise.
   If they are missing, see `.env.example` and §10 Configuration.
2. `brew install mcp-grafana` if the binary is gone (`which mcp-grafana`).
3. **Re-seed before any agent run** — the compressed history sits in a
   ~45-minute window ending at seed time and ages out of the queries after
   roughly an hour:
   ```bash
   set -a && . ./.env && set +a && uv run python -m seed.populate
   ```
4. Ask a question (loads `.env` itself; a plain run never mutates the stack):
   ```bash
   uv run python -m agent.run "Why is SEQ0420 slipping, and what is it costing in artist-days?"
   uv run python -m agent.run "Who is heading for crunch, and when?"
   uv run python -m agent.run --approve     "What do I change to avoid both?"   # auto-approves writes
   uv run python -m agent.run --interactive "What do I change to avoid both?"   # prompts per write
   ```
   Output is the synthesised answer, then the tool timeline (every Grafana MCP
   call, `*`-marked), then the approval decisions.
5. Pick Phase 4 back up from §9 "Still to close before the Phase 4 gate is met".

### Configuration

Copy `.env.example` to `.env`. Four values matter:

| Variable | Where it comes from |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Your stack's **OpenTelemetry tile** — not the docs. Host varies by region and stack vintage; a wrong one fails silently. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Same tile. Basic auth, `instanceID:token`, URL-encoded. |
| `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN` | Service account, Editor or above. Used by `mcp-grafana` and the annotations API. |
| `TURNAROUND_PSEUDONYM_SALT` | Anything real. The emitter refuses the placeholder. |

Then seed for real:

```bash
uv run python -m seed.populate
```

> Writes several thousand series into a live stack. Backfilled data is awkward
> to remove — consider a throwaway stack first. The dry run exercises the
> identical code path.

### Also needed later

- **Container runtime** (`brew install colima docker docker-compose`) — for live
  Kitsu and OpenCue.
- **`gcloud`** — for Cloud Run deploy.
- **Grafana Assistant terms accepted** by a stack admin, once, before MCP works.

---

## 11. Phase 1 verification gate

The go/no-go. One `shot_id` must resolve across all three signals.

**Traces** (Explore → Tempo):
```
{ .production.shot_id = "SEQ0420_SH0100" }
```
Expect one trace per shot, spans per department, comp spans in **error** status
where rework occurred.

**Logs** (Explore → Loki):
```
{service_name="turnaround-bridge"} | production_shot_id = "SEQ0420_SH0100"
```
Expect both status transitions and the `frame 118` cache-miss errors, each
carrying the trace id above.

**Metrics** (Explore → Prometheus):
```promql
turnaround_render_waste_core_hours_total{sequence="SEQ0420"}
```

**The join — this single query is the product's thesis.** Render hours per comp
iteration, by sequence, spanning the compute plane (OpenCue core hours) and the
creative plane (Kitsu iterations) on a shared key:

```promql
sum by (sequence) (increase(turnaround_render_core_hours_total{department="comp"}[14d]))
/
sum by (sequence) (increase(turnaround_task_iterations_total{department="comp"}[14d]))
```

Expect SEQ0420 at roughly **4×** every other sequence. If that holds, the gate
is passed.

**Crew load, respecting the floor:**
```promql
turnaround_artist_hours_logged{department="comp"}
```
`di-pool-1` must be absent from any crew-load alert — it is under the floor.

---

## 12. Roadmap

| Phase | Work | Gate |
|---|---|---|
| 1 ✅→⏸ | Data plane | **Correlation confirmed in a real stack** |
| 2 ✅ | The show | Story present and measured |
| 3 ✅ | Dashboards; ML forecasts (delivery date, weekly hours); outlier detectors; alert rules | Forecast differs from plan; crew alert fires on comp-pool-2 |
| 4 🔨 | `mcp-grafana` read-only + write instances; Cloud Traces MCP; ADK pipeline (3 analysts + gated remediator + synthesis); approval gate; Kitsu write-back | Four demo questions answered cold, tool timeline showing real MCP calls — **2 of 4 verified**, write-back path mid-verification |
| 5 | Supervisor console — Crew Load hero, evidence chain, approval queue | Judges can drive it |
| 6 | AI Observability instrumentation; Cloud Run deploy | Full demo against the public URL in a clean browser profile |
| 7 | Video, README, Devpost | Submitted with hours to spare |

The four questions phase 4 must answer:
*why is Seq 42 slipping · what is it costing in artist-days · who is heading for
crunch and when · what do I change to avoid both.*

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Cloud Traces MCP flaky, or tools differ from expectation | Loki and Prometheus carry the product; traces are the showpiece, not a dependency. Every stage event already exists as both. |
| Synthetic data reads as fake | Provenance stated plainly; real OpenCue job-name convention, round-tripped in tests; simulation of a mechanism rather than a hard-coded outcome. |
| No live Kitsu/OpenCue costs Technological Implementation points | Adapters behind an interface; restore before submission if possible. |
| Agent non-determinism on camera | Fixed seed, scripted `story.md`, rehearsed question set. |
| Forecast quality on limited history | ~21 weeks of history; validate against held-out weeks before trusting on camera. |
| Backfilled data awkward to remove | `--dry-run` first; consider a throwaway stack. |
