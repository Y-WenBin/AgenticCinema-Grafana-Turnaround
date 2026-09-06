# Turnaround

**Crunch is not a surprise. It is a forecastable consequence of rework and render waste, visible weeks ahead in data that already exists.**

In production, *turnaround* is the mandated rest period between wrap and the next call. In VFX it is also how long a shot takes to come back from a vendor. This project is about both, because they are the same number.

Turnaround is a Gemini/ADK multi-agent system that joins a studio's **creative schedule** to its **render farm** inside Grafana, forecasts delivery slip *and* crew overload together, and proposes evidence-backed corrections that a supervisor approves — while the schedule can still be renegotiated.

---

## The problem

The VFX labour crisis is documented and specific: roughly **70% of VFX workers report unpaid overtime**, about **two-thirds say conditions are unsustainable**, 80-hour weeks are routine, and ~**75% report working through legally mandated breaks**. The cause people name is exactly what telemetry can measure: unrealistic deadlines, plus shots corrected or remade entirely until the schedule collapses into day-and-night work.

Nobody sees it coming, because the evidence lives in two systems that never meet:

| Who | What they see | What they miss |
|---|---|---|
| Producers | Kitsu / ShotGrid — shots, statuses, assignments, hours | why a stage keeps repeating |
| Systems/IT | OpenCue — job failures, frame retries, queue depth | which shot, which deadline, which crew |

Join them and a whole class of invisible waste becomes obvious:

> *The artist isn't slow. Every comp iteration burns nine hours because frame 118 keeps failing.*

That join does not exist in any product on the market.

## The join

Every render manager puts the shot id somewhere in the job name — OpenCue's
PyOutline uses `<show>-<shot>-<user>_<name>`. **The shot id is already in the
farm's telemetry.** Turnaround parses it and relabels farm metrics onto
`shot_id` / `sequence` / `department`, so a single PromQL query spans the
creative schedule and the compute that serves it.

Everything else in the product follows from that one relabel. See
[`bridge/ontology.py`](bridge/ontology.py) — the parser ships conventions for
OpenCue, Deadline, Tractor, Qube! and Royal Render, plus a `template` mode for a
studio regex, and the shot-id spelling itself is a configurable `ShotIdScheme`.

## Supported tools

The join needs a shot id and a task status; those are the only things Turnaround
asks of a studio's stack. Adapters are three narrow Protocols in
[`bridge/sources.py`](bridge/sources.py) (`SUPPORTED_TOOLS` is the full list);
Kitsu and OpenCue are the in-tree reference implementations.

| Layer | Native | Works via a ready parser / mapping |
|---|---|---|
| **Schedule / tracker** | Kitsu (`gazu`) | ShotGrid / Flow Production Tracking, ftrack (status codes normalised by `TaskStatus.from_tracker`); any tracker via CSV export (`CsvScheduleSource`) |
| **Render farm** | OpenCue | Deadline, Tractor, Qube!, Royal Render (`parse_job_name`); Slurm / bespoke via `TURNAROUND_FARM_JOB_PATTERN` |
| **Editorial / NLE** | EDL (CMX3600) — [`bridge/editorial.py`](bridge/editorial.py) | Avid, Premiere, DaVinci Resolve, Final Cut via OpenTimelineIO (`pip install 'turnaround[editorial]'`) |

Configure with `TURNAROUND_SHOT_ID_SCHEME`, `TURNAROUND_FARM_CONVENTION`,
`TURNAROUND_FARM_JOB_PATTERN`. Tests: [`tests/test_conventions.py`](tests/test_conventions.py).

## The ontology

| Production concept | Telemetry primitive |
|---|---|
| Shot `SEQ0420_SH0100` | **trace** |
| Task stage (previz → layout → anim → fx → lighting → comp → di) | **span** |
| Retake / rejection | span **error + retry** |
| Status transitions, notes, render + QC logs | **Loki** streams |
| Burndown, iterations, artist hours, farm counters | **Mimir** metrics |
| Director note, cut change, date change | **annotation** |
| "Seq 42 will miss its date" · "comp pool trending to 68h" | **Grafana ML forecast + alert** |
| "Vendor B behaves unlike every other vendor" | **outlier detector** |

Modelling a retake as a span error is not a cute analogy: it means Tempo's own error-rate tooling counts rework for free.

## This is not a surveillance tool

Measuring artist hours can very easily become a way to punish artists. The premise here is that crunch is a **scheduling** failure, not a personal one, so the telemetry is built so it cannot comfortably be used the other way. Enforced in code, in [`bridge/privacy.py`](bridge/privacy.py) and tested in [`tests/test_privacy.py`](tests/test_privacy.py):

- Artists appear only as **salted HMAC pseudonyms**; real names never leave Kitsu. The exporter refuses to start with a guessable salt.
- **No crew-load signal for a pool of fewer than 3 people** — below that, an "overloaded pool" alert is an alert about one person. Small pools are dropped, never merged into an "other" bucket that could be de-anonymised by elimination.
- **No per-person output metric.** The system measures load and waste, never productivity.
- **Every crunch alert must carry a lever** the supervisor can pull. An alert with no remediation is just pressure.

## Architecture

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
```

Analysts are read-only **by construction**, not by prompt: they are wired to a
`mcp-grafana` instance started with `--disable-write`. Only the Remediator can
mutate anything, and every one of its calls is intercepted by an approval gate
that shows the supervisor the full evidence chain — the exact queries, the
annotation that started it, the forecast — before anything is written back to
Kitsu or Grafana.

## Status

Under active development. Full technical reference, design decisions and
progress: **[PROJECT.md](PROJECT.md)**.

- [`bridge/`](bridge/) — ontology, privacy invariants, OTLP emitter. Complete and tested.
- [`seed/`](seed/) — the simulated show. Complete; see [`seed/story.md`](seed/story.md)
  for what the generated data actually shows, measured rather than asserted.
- Next: metric backfill, dashboards and ML forecasts, then the agent layer.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
```

Ask the agent a question (needs a filled-in `.env` — see `.env.example`):

```bash
uv run python -m seed.populate
uv run python -m agent.run "Why is SEQ0420 slipping, and what is it costing in artist-days?"
```

Every run self-instruments with the OpenTelemetry GenAI conventions and scores
its own answer (deterministic + LLM judge); the trace, token/latency histograms
and `gen_ai.evaluation.result` events land in the same Grafana Cloud stack. A
per-run Gemini-call ceiling (`TURNAROUND_MAX_LLM_CALLS`, default 40) caps token
spend. Test plan and reproducibility contract: [`tests/TESTPLAN.md`](tests/TESTPLAN.md).

## Deploy

`agent/serve.py` is a FastAPI wrapper (`POST /ask`, `GET /healthz`) for Cloud
Run. `deploy/deploy.sh` provisions a least-privilege runtime service account,
pushes Grafana + OTLP credentials to Secret Manager, and deploys from
`deploy/Dockerfile`. See [`deploy/README.md`](deploy/README.md).

## Data provenance

The show is **invented**: *Nightfall*, 200 shots over ~21 weeks, generated
deterministically by [`seed/model.py`](seed/model.py). Nothing here derives from
any studio's real production data.

The generator simulates a *mechanism* rather than writing in a punchline. Two
ordinary perturbations — a cache regression and a director note — are applied to
a healthy show, and the iteration counts, render waste and crew hours are
whatever falls out. Remove a beat from [`seed/show.yaml`](seed/show.yaml) and the
numbers move on their own; forecasting a hard-coded constant would prove nothing.

Job names follow OpenCue's real `<show>-<shot>-<user>_<name>` convention and are
round-tripped through the production parser in the test suite, so the join is
exercised on generated data exactly as it would be on a real farm.

Source adapters sit behind the three Protocols in `bridge/sources.py`, so a live
Kitsu / ShotGrid / ftrack tracker and an OpenCue / Deadline / Tractor / Qube!
farm drop in without a rewrite — see **Supported tools** above.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
