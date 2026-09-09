# Turnaround

**Crunch is not a surprise. It is a forecastable consequence of rework and render waste, visible weeks ahead in data that already exists.**

In production, *turnaround* is the mandated rest period between wrap and the next call. In VFX it is also how long a shot takes to come back from a vendor. This project is about both, because they are the same number.

Turnaround is a Gemini/ADK multi-agent system that joins a studio's **creative schedule** to its **render farm** inside Grafana, forecasts delivery slip *and* crew overload together, and proposes evidence-backed corrections a supervisor approves — while the schedule can still be renegotiated.

---

## The problem

The VFX labour crisis is documented and specific: roughly **70% of VFX workers report unpaid overtime**, about **two-thirds** say conditions are unsustainable, 80-hour weeks are routine, and ~**75% report working through legally mandated breaks**. The cause people name is exactly what telemetry can measure: unrealistic deadlines, plus shots corrected or remade until the schedule collapses into day-and-night work.

Nobody sees it coming, because the evidence lives in two systems that never meet:

| Who | What they see | What they miss |
|---|---|---|
| Producers | the tracker — shots, statuses, assignments, hours | why a stage keeps repeating |
| Systems / IT | the render farm — job failures, frame retries, queue depth | which shot, which deadline, which crew |

Join them and a whole class of invisible waste becomes obvious:

> *The artist isn't slow. Every comp iteration burns nine hours because frame 118 keeps failing.*

That join does not exist in any product on the market.

## The join

Every render manager puts the shot id somewhere in the job name — OpenCue's PyOutline uses `<show>-<shot>-<user>_<name>`. **The shot id is already in the farm's telemetry.** Turnaround parses it out and relabels farm metrics onto `shot_id` / `sequence` / `department`, so one PromQL query spans the creative schedule and the compute that serves it.

Everything else in the product follows from that single relabel. See [`bridge/ontology.py`](bridge/ontology.py).

## Technology-agnostic by design

The join needs two things from a studio's stack: a **shot id** and, on the schedule side, a **task status**. Nothing else. That constraint is deliberate — a studio should not have to change tools to see its own waste. Ingest is three narrow Protocols in [`bridge/sources.py`](bridge/sources.py); `SUPPORTED_TOOLS` is the current list.

| Layer | Reference implementation | Also works, out of the box |
|---|---|---|
| **Schedule / tracker** | Kitsu (`gazu`) | ShotGrid / Flow Production Tracking, ftrack — status vocabularies normalised by `TaskStatus.from_tracker`; **any** tracker via a CSV export (`CsvScheduleSource`) |
| **Render farm** | OpenCue | Deadline, Tractor, Qube!, Royal Render — `parse_job_name(convention=…)`; Slurm or a house scheme via `TURNAROUND_FARM_JOB_PATTERN` |
| **Editorial / NLE** | CMX3600 EDL — [`bridge/editorial.py`](bridge/editorial.py) | DaVinci Resolve, Premiere Pro, Avid, Shotcut, Flame (EDL); Resolve 18+, Blender VSE, Kdenlive (`.otio`, `pip install 'turnaround[editorial]'`); an online/conform EDL that carries the shot only in the reel column |

The shot-id spelling itself is a configurable `ShotIdScheme` (`seq_sh` default, plus `numeric` / `dash` / `loose`). Everything above is exercised in [`tests/test_conventions.py`](tests/test_conventions.py) and [`tests/test_editorial.py`](tests/test_editorial.py) against the dialects each tool really emits. Connecting a *live* tracker or farm is a small adapter against the Protocol — see [`docs/SETUP.md`](docs/SETUP.md).

## The ontology

| Production concept | Telemetry primitive |
|---|---|
| Shot `SEQ0420_SH0100` | **trace** |
| Task stage (previz → layout → anim → fx → lighting → comp → di) | **span** |
| Retake / rejection | span **error + retry** |
| Status transitions, notes, render + QC logs | **Loki** streams |
| Burndown, iterations, artist hours, farm counters | **Mimir** metrics |
| Director note, cut change, date change | **annotation** |
| "SEQ0420 will miss its date" · "comp pool trending to 68h" | **Grafana ML forecast + alert** |
| "Vendor B behaves unlike every other vendor" | **outlier detector** |

Modelling a retake as a span error is not a cute analogy: it means Tempo's own error-rate tooling counts rework for free.

## This is not a surveillance tool

Measuring artist hours can very easily become a way to punish artists. The premise here is that crunch is a **scheduling** failure, not a personal one, and the telemetry is built so it cannot comfortably be used the other way. Enforced in code — [`bridge/privacy.py`](bridge/privacy.py), tested in [`tests/test_privacy.py`](tests/test_privacy.py):

- Artists appear only as **salted HMAC pseudonyms**; real names never leave the tracker. The exporter refuses to start with a guessable salt.
- **No crew-load signal for a pool of fewer than three people** — below that, an "overloaded pool" alert is an alert about one person. Small pools are dropped, never merged into an "other" bucket that could be de-anonymised by elimination.
- **No per-person output metric.** The system measures load and waste, never productivity.
- **Every crunch alert carries a lever** the supervisor can pull. An alert with no remediation is just pressure.

The judge tier enforces the same line: a run that lets a below-floor pool name into an answer fails a test (`tests/test_evaluation.py`).

## Architecture

```
  any tracker  ──►  bridge/  ──OTLP──►  Grafana Cloud  ◄──MCP──  agent/  ──►  Vertex AI
  any farm          (relabel on         Tempo · Loki             ADK multi-       Gemini 2.5
  any NLE cut        shot_id;            Mimir · Alerts           agent + judge    flash
                     privacy floor)      ML forecasts
```

```
  agent/  Producer  ─ ScheduleAnalyst · FarmAnalyst · CrunchGuardian   (read-only, by construction)
                    ─ Remediator                                       (gated: every write needs approval)
                    ─ synthesis                                        (composes the answer; no tools)
          + observability/   every run self-instruments (OpenTelemetry GenAI conventions) and
                             scores its own answer (deterministic ground truth + an LLM judge)
```

Analysts are read-only *by construction*, not by prompt — they are wired to an `mcp-grafana` instance started `--disable-write`. Only the Remediator can mutate anything, and every one of its calls is intercepted by an approval gate that shows the supervisor the full evidence chain — the exact queries, the annotation that started it, the forecast — before anything is written back.

## What's built

| Document | What it is for |
|---|---|
| [PROJECT.md](PROJECT.md) | Technical reference: architecture, module map, the decisions and why |
| [AGENTS.md](AGENTS.md) | Orientation for a contributor or a coding agent: invariants, layer map, gotchas |
| [docs/SETUP.md](docs/SETUP.md) | End-to-end setup — Grafana Cloud, Google Cloud, a real NLE |
| [docs/DEMO.md](docs/DEMO.md) | The three-minute demo: script, shot list, question set |
| [docs/CODE_REVIEW.md](docs/CODE_REVIEW.md) | Structural review of the codebase and what changed |
| [docs/DESIGN_LOG.md](docs/DESIGN_LOG.md) | Dated build record, including every bug found on the way |
| [tests/TESTPLAN.md](tests/TESTPLAN.md) | The reproducibility contract the suite enforces |

| Part | State |
|---|---|
| [`bridge/`](bridge/) — ontology, privacy invariants, tool-agnostic source adapters, OTLP emitter | complete, tested |
| [`seed/`](seed/) — the simulated show *Nightfall* ([`seed/story.md`](seed/story.md)) | complete; numbers measured, not asserted |
| [`grafana/`](grafana/) — 4 dashboards, alert rules with a lever, Prophet + outlier ML jobs, an EvalOps surface | complete, provisioned by [`grafana/provision.py`](grafana/provision.py) |
| [`agent/`](agent/) — the deterministic multi-agent pipeline, `mcp-grafana` in two modes (OSS / hosted OAuth), the gated write-back | complete |
| [`observability/`](observability/) — the agent instruments itself with the OTel GenAI conventions | complete |
| [`agent/evaluation.py`](agent/evaluation.py) — a deterministic judge + an LLM judge, emitted as `gen_ai.evaluation.result` | complete |
| [`deploy/`](deploy/) — Cloud Run image + one-shot deploy script | **deployed** — [https://turnaround-agent-b465d3vxhq-uc.a.run.app](https://turnaround-agent-b465d3vxhq-uc.a.run.app), public and read-only, re-seeded every 15 min by a Cloud Run job |

## Try it

The agent is live on Cloud Run. No auth, nothing to install — open
[the URL](https://turnaround-agent-b465d3vxhq-uc.a.run.app) in a browser for
the endpoint list, or go straight at it:

```bash
curl -s https://turnaround-agent-b465d3vxhq-uc.a.run.app/health
curl -s -H 'content-type: application/json' \
  -d '{"question":"why is SEQ0420 slipping and what is it costing?"}' \
  https://turnaround-agent-b465d3vxhq-uc.a.run.app/ask
```

It is public and cannot write: `serve.py` runs `AutoApprover(approve=False)` and
the analysts are wired to `mcp-grafana --disable-write`. A Cloud Run job re-seeds
the stack every 15 minutes, so the answers are against live data whenever you ask.

## Running it

```bash
uv sync --group dev
uv run pytest            # 364 tests, offline: no network, no credentials
uv run ruff check .
```

Ask the agent a question (needs a filled-in `.env` — copy `.env.example`; [`docs/SETUP.md`](docs/SETUP.md) walks through Grafana Cloud and Vertex):

```bash
set -a && source .env && set +a
uv run python -m seed.populate
uv run python -m grafana.provision
uv run python -m agent.run "Why is SEQ0420 slipping, and what is it costing in artist-days?"
```

> **Re-seed before any live run.** The show's history is compressed into a
> ~45-minute window ending at the moment of seeding, and every query reads it
> with `last_over_time((…)[2h:])`. A stack seeded hours ago answers everything
> with nothing, which looks like a broken agent rather than stale data.

Every run self-instruments with the OpenTelemetry GenAI conventions and scores its own answer; the trace, token/latency histograms and `gen_ai.evaluation.result` events land in the same Grafana Cloud stack the agent queries. A per-run Gemini-call ceiling (`TURNAROUND_MAX_LLM_CALLS`, default 40) bounds token spend. Test plan and reproducibility contract: [`tests/TESTPLAN.md`](tests/TESTPLAN.md).

## Data provenance

The show is **invented**: *Nightfall*, 200 shots over ~21 weeks, generated deterministically by [`seed/model.py`](seed/model.py). Nothing here derives from any studio's real production data.

The generator simulates a *mechanism* rather than writing in a punchline. Two ordinary perturbations — a cache regression and a director note — are applied to an otherwise healthy show, and the iteration counts, render waste and crew hours are whatever falls out. Remove a beat from [`seed/show.yaml`](seed/show.yaml) and the numbers move on their own; forecasting a hard-coded constant would prove nothing.

Job names follow OpenCue's real `<show>-<shot>-<user>_<name>` convention and round-trip through the production parser in the test suite, so the join is exercised on generated data exactly as it would be on a real farm.

## About this project

I build things at the seam between systems that were never meant to talk to each other — that is where the interesting problems, and usually the wasted human effort, tend to hide. Turnaround started as an entry for the Agentic Cinema hackathon (Grafana Labs track) and kept going because the underlying idea holds up: an industry with a well-documented burnout problem is sitting on the data that predicts it, in tools it already runs.

The stance is deliberate on both axes. **Technology-agnostic**, because asking a studio to switch trackers or render managers to adopt a diagnostic tool is a non-starter — so the join asks for the least it possibly can, and the adapters are small. **Built for the people doing the work**, because the same measurements can be pointed at a crew to squeeze them harder, and this one is engineered so that is difficult on purpose: pseudonyms, an aggregation floor, no productivity metric, and every alert obliged to carry a fix.

Contributions, corrections and "we tried this against our Deadline farm and…" reports are all welcome.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
