# The three-minute demo

Everything needed to shoot the submission video: the storyline, a timed script,
the shot list, the question set, and a pre-flight checklist.

Three minutes is about **430 spoken words**. That is not enough time to explain
the architecture, so this script does not try. It shows one contradiction, one
idea, and the agent doing the thing — and lets the repo carry the detail.

---

## What has to land

A judge who watches once and never opens the repo should come away with four
things, in this order:

1. **The business problem.** Crunch is a scheduling failure with a measurable
   mechanism — rework and render waste — and the data that predicts it already
   exists, split across two systems that never meet.
2. **The idea.** The render farm's job name already contains the shot id. One
   relabel joins the creative plane to the compute plane. Everything else follows.
3. **The agent earns its answer.** Every figure carries the query that produced
   it, every Grafana MCP call is on screen, and the write-back is gated by a human.
4. **It is built not to be turned against the crew.** The privacy floor is
   visible on camera: a two-person pool is *absent* from an answer that names
   every other pool.

Anything that does not serve one of those four is a cut.

---

## Storyline

> A show that looks healthy on the tracker is quietly destroying its comp crew.
> The reason is not in the tracker — it is in the render farm, under a job name
> nobody parses. Turnaround joins the two, and a producer can now ask, in
> English, *why is this slipping and what is it costing me* — and get an answer
> with receipts, a fix, and an approval prompt before anything is changed.

The arc is **contradiction → blind spot → the join → the agent → the loop closes
→ the agent is watched too**. The emotional beat is the second one: the crew-load
number is loud, and it arrives *after* the overtime has already been worked.

---

## Script

Narration in **bold**; timings assume ~150 wpm. Word counts are per segment so
you can trim against a stopwatch rather than by feel.

### 0:00 – 0:20 · Cold open — the contradiction (53 words)

**"Two weeks from delivery. On paper this show is fine — eighty-one percent
through, crew load thirty-five hours a week. Here's the same show's comp pool.
Eighty-nine hours. Seventy percent of VFX artists report unpaid overtime — and
nobody sees it coming, because the evidence is split across two systems that
never meet."**

*Visual:* **Turnaround · Delivery**, burndown panel — a healthy line. Hard cut
to **Turnaround · Crew Load**, the `comp-pool-2 — hours per rostered artist`
stat. No transition, no easing. The cut is the argument.

*On screen:* `81% delivered · 19 days out` → `comp-pool-2: 89.8 h/week`

### 0:20 – 0:38 · The blind spot (40 words)

**"The producer's tracker knows which shots are late. The render farm knows
which renders keep failing. Neither knows about the other. So 'comp is behind
and everyone's exhausted' never becomes 'this sequence is burning four times the
compute it should.'"**

*Visual:* split screen — the Delivery dashboard left, an Explore panel of raw
farm metrics right. Deliberately unglamorous on the right: rows of
`turnaround_render_core_hours_total` with no shot in sight.

### 0:38 – 1:00 · The join (59 words)

**"Except the link is already there. Every render manager writes the shot id
into the job name — OpenCue's is show-shot-user-name. Turnaround parses it out
and relabels farm metrics onto shot, sequence and department. One query now
spans both planes. This is SEQ0420: four times the render hours per comp
iteration as every other sequence. Since twenty-five days ago."**

*Visual:* a two-second beat on a single job name with the shot id highlighted
(`nightfall-SEQ0420_SH0100-a7f3c2d1_comp_v006`), then **Turnaround · The Join**,
`Render core-hours per comp iteration, by sequence`. Let the SEQ0420 line sit
clear of the pack for a full second before moving.

*On screen:* `<show>-<shot>-<user>_<name>` with `SEQ0420_SH0100` boxed.

### 1:00 – 1:35 · The agent answers (77 words)

**"Now ask it in a producer's words. Five Gemini agents on Vertex AI, wired to
Grafana's MCP server: three read-only analysts, a gated remediator, a synthesis
step. The answer comes back with a receipt — every figure carries the PromQL
that produced it, and the timeline shows every Grafana call it made to get
there. Nine of them, live. Frame 118 fails on every SEQ0420 comp render.
Sixty-seven wasted core-hours. Fifty-six artist-days a week of comp overtime."**

*Visual:* terminal. Type the question, then speed-ramp the run to ~8 seconds.
Land on the **Evidence** block, then scroll to the **tool timeline** and let the
`*`-marked Grafana calls scroll past at readable speed.

*Highlight:* box the `*` column and the `9 against Grafana Cloud MCP` line.

### 1:35 – 2:00 · The privacy floor (58 words)

**"Second question — who's heading for crunch. comp-pool-1, comp-pool-2,
lighting-pool-2. And one pool is deliberately missing. di-pool-1 has two people
in it, and an alert about a two-person pool is an alert about a person. That
floor is enforced in the metrics, in the queries, and in the judge that scores
every answer. This measures scheduling, not people."**

*Visual:* the second run's answer. Then cut to **Crew Load → Rostered headcount**
and let the viewer read `di-pool-1 · 2` themselves.

*Highlight:* circle `di-pool-1 · 2` in the headcount table, then circle its
absence in the answer above it. This is the single most distinctive frame in the
video — give it two full seconds.

### 2:00 – 2:30 · The loop closes (63 words)

**"Third question: what do I change. The remediator proposes a fix — and stops.
Every write is intercepted and shown with the full evidence chain before a human
approves it. The analysts couldn't write even if they were asked to; they're
connected to a server started with writes disabled. Approve — and the annotation
lands on the same dashboard the evidence came from."**

*Visual:* the `--interactive` run pausing on the approval prompt. Hold on the
prompt for a beat — the pause is the point. Press `y`. Cut to **The Join** with
the new annotation marker on the timeline, and hover it so the text shows.

*Highlight:* `mcp-grafana --disable-write` in the prompt's evidence block.

### 2:30 – 2:52 · The agent is watched too (54 words)

**"And the agent is watched the way the show is. Every run emits an
OpenTelemetry GenAI trace, token and latency histograms, and its own grades —
into the same Grafana stack it queries. If answers drift off their evidence, an
alert fires. If a protected pool ever reaches an answer, a critical alert
fires."**

*Visual:* **Turnaround · EvalOps** — the four stat tiles, then the events table.
Click the `response_id` link through to the `invoke_agent` trace in Tempo with
its `chat` and `execute_tool` children. Finish on the alert-rule list showing
`A sub-floor pool was named in an answer · critical`.

### 2:52 – 3:00 · Close (31 words)

**"Crunch is not a surprise. It's a forecastable consequence of rework and
render waste, in data studios already have. Turnaround — Apache-2.0, and it runs
on the tools they already own."**

*Visual:* title card. Repo URL, licence, the hosted URL.

**Total: 435 words — 2 min 54 s of narration at 150 wpm.** Budget for the two silent beats (the approval pause, the
di-pool-1 hold) by trimming segment 4 first — it is the wordiest.

---

## Shot list

Capture in this order; it is roughly the order of setup effort, and every shot
except the terminal ones can be re-taken from a single seeded session.

### Grafana Cloud

| # | Where | What to frame | What to highlight |
|---|---|---|---|
| G1 | Dashboards → **Turnaround · Delivery** | `Shots signed off through DI` | The healthy-looking burndown. Nothing wrong here — that is the point |
| G2 | **Turnaround · Crew Load** | `comp-pool-2 — hours per rostered artist` stat, then `Weekly hours by pool` | The stat number. Then the climb: 38.6 → 31.9 → 46.1 → ~90 |
| G3 | **Turnaround · Crew Load** | `Rostered headcount` table | `di-pool-1 · 2` — the reason it never appears in an alert |
| G4 | **Turnaround · The Join** | `Render core-hours per comp iteration, by sequence` | SEQ0420's line clear of every other sequence. **The hero frame** |
| G5 | **Turnaround · The Join** | `Wasted render core-hours by sequence` bargauge | SEQ0420 ~67 h against ~5–6 h everywhere else |
| G6 | Explore → **Loki** | `{service_name="turnaround-bridge"} \|= "frame 118"` | The cache-miss lines. The mechanism in plain text |
| G7 | Explore → **Tempo** | `{ span.production.shot_id = "SEQ0420_SH0100" }` | A shot rendered as a distributed trace; comp spans in **error** — retakes are span errors |
| G8 | Alerting → **Alert rules** | The `turnaround` group | Expand `A sequence is concentrating render waste` and show the **`lever`** annotation. Every alert carries a fix |
| G9 | **Turnaround · The Join**, post-approval | The new annotation marker | Hover so the write-back text shows on the dashboard the evidence came from |
| G10 | **Turnaround · EvalOps** | The four stat tiles + `Evaluation events` table | `Mean grounding score`, `Privacy-floor failures: 0` |
| G11 | Explore → **Tempo**, from the events table link | `{ span.gen_ai.response.id = "<id>" }` | The `invoke_agent` trace with `chat` + `execute_tool` children, `gen_ai.usage.*` on the span |
| G12 | Alerting → **Alert rules** | `turnaround-evalops` group | `A sub-floor pool was named in an answer · critical` |

### Google Cloud

| # | Where | What to frame | What to highlight |
|---|---|---|---|
| C1 | Vertex AI → **Dashboard / Quotas**, your region | Request count for `gemini-2.5-flash` rising during the recorded runs | Real Vertex traffic, timestamped against the demo. This is the honest proof the model ran |
| C2 | IAM → the runtime **service account** | Its role list | Least privilege. Two seconds, no narration — a background frame under segment 4 |
| C3 | Cloud Run → the **service page** *(only if deployed)* | Service URL + a green revision | The hosted URL the rules require. **See the risk note below** |

### Terminal

| # | Command | What to highlight |
|---|---|---|
| T1 | `uv run pytest -q` | `360 passed`. Two seconds under the close, or cut entirely if tight |
| T2 | Question 1 (below) | Evidence block, then the `*`-marked tool timeline |
| T3 | Question 2 | The pools named — and the one that isn't |
| T4 | Question 3 with `--interactive` | The approval prompt, the evidence chain, the `y` |
| T5 | The scorecard tail of any run | `privacy_floor_respected  pass`, `hallucination 1.00` |

Terminal settings: 16–18 pt, high-contrast light-on-dark, window ~100 columns so
the timeline does not wrap. Clear scrollback before each take.

---

## The question set

Use these four verbatim. They are the ones verified cold against the live stack,
and each exists to prove a different claim.

| # | Prompt | Proves | Expected shape |
|---|---|---|---|
| **Q1** | `Why is SEQ0420 slipping, and what is it costing in artist-days?` | The join, real MCP calls, cost arithmetic | Both analysts run; ~9 Grafana MCP calls incl. `tempo_traceql-search`; frame 118 quoted from Loki; ~4× join ratio; ~56 artist-days/week |
| **Q2** | `Who is heading for crunch, and when?` | The privacy floor, live | `comp-pool-1`, `comp-pool-2`, `lighting-pool-2` named with hours; **`di-pool-1` absent** |
| **Q3** | `What do I change to avoid both?` — run with `--interactive` | The gate, the write-back, the closed loop | Remediator proposes; gate shows evidence; on `y`, `create_annotation` + `kitsu_write_back` land |
| **Q4** | `What is SEQ0420 costing us in artist-days?` | *Backup.* A narrower Q1 if the full one runs long or the cost query wobbles | The cost figure with its PromQL |

**Do not improvise a fifth question on camera.** Anything outside the seeded
show's vocabulary will be answered honestly — "not measured this run" — which is
correct behaviour and a bad frame.

Verified expectations for every run: an **Answer / Evidence / Remediation**
block; a timeline whose Grafana calls are `*`-marked; and a scorecard with
`grounding_numbers`, `mechanism_named` and `privacy_floor_respected` all `pass`
and the LLM judge's `hallucination` at or near 1.00.

---

## Pre-flight

1. **Re-seed immediately before recording.** Non-negotiable. The history is a
   ~45-minute window ending at seed time, read with `last_over_time((…)[2h:])`.
   A stack seeded this morning answers every question with nothing, and the
   agent will look broken on camera.
   ```bash
   set -a && source .env && set +a
   uv run python -m seed.populate
   uv run python -m grafana.provision
   ```
   For a long shoot, leave `uv run python -m seed.refresh` running in another
   window — it re-seeds every 15 minutes.
2. **Space the questions by a minute.** Four runs back to back can exhaust a
   small project's per-minute Gemini quota. That surfaces as one clean sentence
   (`Halt(kind="model_quota")`, exit 3) rather than a traceback — but it is still
   a wasted take. Record each question as its own take and cut them together.
3. **Do a full dry take first**, then record. The pipeline is deterministic in
   *shape*, so the second run of a question looks like the first; the wording
   varies slightly, which is normal and worth knowing before you are rolling.
4. **Check the alert state.** On a freshly seeded stack, *A sequence is
   concentrating render waste* **fires** — that is the product working, not a
   fault. If you show the alert list, say so or the red badge reads as a bug.
5. **Have G4 and G3 recorded before the terminal takes.** They are the two
   frames the video cannot ship without.

---

## Risk: the hosted URL

The rules require a **hosted project URL**. `deploy/` has the Dockerfile, the
one-shot `deploy.sh` and a documented path, but the deploy has never been *run* —
`gcloud` is not installed on the build machine. Until it is:

- Install the SDK (`brew install --cask google-cloud-sdk`), then
  `PROJECT_ID=… REGION=… ./deploy/deploy.sh`, and shoot C3.
- If that cannot happen before the deadline, do **not** stage a fake Cloud Run
  page. Show the Vertex AI traffic (C1) and the CLI, and be explicit in the
  Devpost text about what is deployed and what runs locally. A judge who catches
  a staged frame discounts everything else in the video.

---

## Differentiators — crib sheet

For the narration, the Devpost description and any Q&A. Each is demonstrable on
screen, which is why it is here.

| Claim | Why it is different | Where it shows |
|---|---|---|
| **The join** — creative plane ↔ compute plane on `shot_id` | Grafana already ships an OpenCue integration and studios still don't use it. The gap was never data or connectors; it was vocabulary and audience. No product on the market makes this join | G4, Q1 |
| **Read-only by construction, not by prompt** | The analysts are wired to `mcp-grafana --disable-write`. A prompt-injected "change X" has nothing to call. Most agent demos rely on the model choosing to behave | Q3's evidence block, segment 5 |
| **Human approval with the full evidence chain** | The gate shows the proposed write *and* the queries, the annotation and the forecast behind it, before anything mutates | T4 |
| **A privacy floor enforced in code** | Below three people, a pool alert is an alert about a person. Small pools are dropped, never merged into an "other" bucket that elimination could unmask. There is no per-person output metric at all | G3, Q2 |
| **Every alert carries a lever** | An alert with no remediation is just pressure on the people already being crushed | G8 |
| **Answers with receipts** | Every figure in the answer carries the PromQL that produced it; the timeline lists every Grafana call. A figure the run did not measure is reported as *not measured*, never estimated | T2 |
| **The agent is observable and graded in the stack it queries** | OTel GenAI trace, token/latency histograms, and `gen_ai.evaluation.result` events — with alerts on grounding drift and on privacy breach. "Trust me" becomes a metric with a threshold | G10–G12 |
| **Tool-agnostic by design** | A studio should not switch trackers to see its own waste. The join needs a shot id and a task status; OpenCue / Deadline / Tractor / Qube! / Royal Render, Kitsu / ShotGrid / ftrack / CSV, EDL / OTIO | README coverage table |

### The business problem, in three sentences

VFX crunch is routinely treated as an unavoidable cost of creative work. It is
not: it is a scheduling failure with a measurable mechanism — rework and render
waste compounding inside a fixed delivery window — and the telemetry that
predicts it weeks ahead already exists, sitting in two systems that were never
connected. Turnaround connects them, quantifies the waste in artist-days, and
puts a fix in front of a supervisor while the schedule can still be
renegotiated.

---

## If it runs long

Cut in this order. The first three are safe; past that you start losing a claim.

1. **T1** (the test count) — proof of rigour, but the repo carries it.
2. **C2** (the service account) — background texture.
3. **G6** (the Loki frame 118 lines) — the mechanism is already narrated in Q1's
   answer.
4. **G7** (the shot as a Tempo trace) — beautiful, and the one genuinely
   *conceptual* frame in the video. Cut it only if you must.
5. **Segment 6 down to five seconds** — hold on the EvalOps stat tiles, drop the
   Tempo click-through. Do not cut the segment entirely: self-evaluation is a
   scored differentiator.

Never cut: **G4** (the join), **G3 + Q2** (the privacy floor), **T4** (the
approval prompt).
