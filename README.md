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

OpenCue's PyOutline names every job `<show>-<shot>-<user>_<name>`. **The shot id is already in the farm's telemetry.** Turnaround parses it and relabels farm metrics onto `shot_id` / `sequence` / `department`, so a single PromQL query spans the creative schedule and the compute that serves it.

Everything else in the product follows from that one relabel. See [`bridge/ontology.py`](bridge/ontology.py).

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

Under active development.

- [`bridge/`](bridge/) — ontology, privacy invariants, OTLP emitter. Complete and tested.
- [`seed/`](seed/) — the simulated show. Complete; see [`seed/story.md`](seed/story.md)
  for what the generated data actually shows, measured rather than asserted.
- Next: metric backfill, dashboards and ML forecasts, then the agent layer.

## Development

```bash
uv sync --group dev
uv run pytest
```

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

Source adapters sit behind an interface so live Kitsu and OpenCue instances drop
in without a rewrite.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
