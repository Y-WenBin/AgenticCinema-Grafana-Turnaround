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
| 2026-09-10 | [First real deploy: the buildpack trap](#2026-09-10--first-real-deploy-the-buildpack-trap) |
| 2026-09-10 | [Deployed — and three faults only a real run could find](#2026-09-10--deployed--and-three-faults-only-a-real-run-could-find) |
| 2026-09-10 | [The front door was a 404, and I had already seen it](#2026-09-10--the-front-door-was-a-404-and-i-had-already-seen-it) |
| 2026-09-10 | [Something to try, and a budget that survives it](#2026-09-10--something-to-try-and-a-budget-that-survives-it) |
| 2026-09-13 | [The demo moves out](#2026-09-13--the-demo-moves-out) |
| 2026-09-13 | [The first command a new user runs](#2026-09-13--the-first-command-a-new-user-runs) |

---

## 2026-09-05 — data plane

Ontology, privacy layer, emitter, metric backfill, simulation, seeder driver —
all complete and tested. A dry run produces roughly **1,450 spans, 1,475
correlated log lines and 5,900 metric points** without touching the network. The
same run against the live stack completes in ~4 s.

(Approximate on purpose: the show is anchored to *now*, so a run in a different
week crosses a different number of shot boundaries and the totals move by a few
either way. `uv run python -m seed.populate --dry-run` prints the exact figures
for the day you run it.)

---

## 2026-09-06 — live-stack verification of the join

- **Traces** — `{ .production.shot_id = "SEQ0420_SH0100" }` returns one trace,
  department spans, comp spans in error status. Tempo accepts historical span
  times directly (no compression needed for traces, but the seeder warps them
  anyway so the trace and metric time axes align).
- **Logs** — `{service_name="turnaround-bridge"}` carries ~1,475 lines. For one
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
  calls a tool outside its `tool_filter`. Losing a whole run is a poor trade to save
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

---

## 2026-09-10 — first real deploy: the buildpack trap

`gcloud` had never been installed on the build machine, so `deploy/deploy.sh`
had never been *run*. Installing it turned up two faults that no amount of
reading would have found, plus one clarification worth writing down.

**The Vertex service account is not a project login.** `.secrets/gcp-sa.json`
grants `roles/aiplatform.user` and nothing else. Activated in `gcloud` it can
list enabled APIs and call Gemini, and that is the end of it: Cloud Run Admin
was not even enabled on the project, and this identity cannot enable it. A
deploy needs a human account. Worth stating plainly because "we have a key
file" reads like "we are connected", and it is not the same claim.

**`--source .` would have shipped the wrong image, successfully.** `gcloud run
deploy --source .` builds a Dockerfile only if it finds one in the *source
root*; otherwise it falls back to Google Cloud buildpacks without complaint.
The Dockerfile was at `deploy/Dockerfile`, so the build would have produced a
buildpack image with no `mcp-grafana` binary and the wrong entrypoint — a green
deploy and a service that dies at the first tool call, which is the worst
possible failure to discover late. Fixed by moving the Dockerfile to the
repo root and making the build explicit: `gcloud builds submit --tag`, then
both workloads deployed with `--image`, so there is no implicit build path left
to guess wrong. Pinned by
`test_reproducibility.py::test_deploy_builds_the_dockerfile_not_a_buildpack`.

**The hosted demo needed the job `seed/refresh.py` already described.** Its
docstring said "in production this is a Cloud Run job on a schedule, not a
process babysat by a laptop (see deploy/)" — and `deploy/` had no such job. The
gap only matters once something is hosted: the compressed window ages out of
Mimir in about an hour, so a hosted URL visited the next morning answers every
question with nothing. `deploy.sh` now deploys `turnaround-seed` from the same
image with the entrypoint overridden, plus a Cloud Scheduler trigger every 15
minutes, and primes it once at the end of the deploy so the demo is live
immediately.

**The endpoint is now public.** A hosted URL a reviewer cannot open is not a
hosted URL. It is safe to be public for reasons enforced in code rather than
promised: `serve.py` runs `AutoApprover(approve=False)`, the analysts are wired
to `mcp-grafana --disable-write`, `--max-instances 2` bounds the blast radius
and `TURNAROUND_MAX_LLM_CALLS` bounds any single request.

Grafana Cloud needed no changes — four dashboards, four alert rules and three
ML jobs were still provisioned and re-provisioned clean. Only the data had aged
out, which is the whole reason the re-seed job exists. After a re-seed the full
question ran end to end: the join at 4.51 core-h/iter against ~2.2 elsewhere,
`frame 118` named as the mechanism, six evaluation checks passing, and both
write attempts denied by the gate.

---

## 2026-09-10 — deployed — and three faults only a real run could find

Live at **https://turnaround-agent-b465d3vxhq-uc.a.run.app**. Getting there took
four attempts, and every failure was in code that had been reviewed, tested and
committed. None of them was findable without running it.

**`--args` needs the `=` form.** `gcloud run jobs deploy --args "-m,seed.refresh,--once"`
fails with *"argument --args: expected one argument"* — the value begins with a
dash, so argparse reads it as another flag. `--args="..."` works.

**`--condition` is not uniform across gcloud.** `gcloud projects
add-iam-policy-binding` requires it (it prompts interactively without it); the
`gcloud run jobs` variant rejects it outright. The same idiom, copied one line
down, fails the deploy.

**Google Frontend swallows `/healthz` on `*.run.app`.** The worst of the three,
because it is a false negative on a healthy service. GFE answers that exact
lowercase path itself with a 1568-byte HTML 404; the request never reaches the
container. Everything about the service looked correct — `Ready=True`, ingress
`all`, `allUsers` bound, effective org policy `allValues: ALLOW`, TLS and DNS
clean, and the container log showing `Uvicorn running on http://0.0.0.0:8080` —
while the documented smoke test returned 404. Isolated by elimination:

    /          -> {"detail":"Not Found"}  (22b, FastAPI: the request arrives)
    /docs      -> Swagger UI              (the app is entirely fine)
    /healthz   -> 1568b of Google HTML    (intercepted before the container)
    /healthz2  -> {"detail":"Not Found"}  (so it is that exact path, not routing)

The handler is now mounted at `/health` as well. `/healthz` stays for Cloud Run's
own probes and for deployments not behind GFE.

Worth recording the shape of the lesson rather than just the three bugs: all of
this was *reviewed* code that a reasonable person would have signed off. The
deploy script was correct as prose and wrong as an executable, and the health
check was correct as an application and wrong as a deployed endpoint. Only the
run tells you.

What is live: the `turnaround-agent` service (public, `--max-instances 2`), the
`turnaround-seed` job, and Cloud Scheduler `turnaround-seed-every-15m` firing
every quarter hour — first execution `Completed`, `succeededCount 1`. The public
endpoint answered the SEQ0420 question with the join intact (44.10 core-h against
~0.8 elsewhere, `frame 118` named, 12.9 artist-days/week) and all eight documented
response keys present.


## 2026-09-10 — the front door was a 404, and I had already seen it

Someone opened the hosted URL in a browser and got `{"detail":"Not Found"}`.

The service was fine. `/health`, `/ask` and `/docs` all answered correctly; the
root simply had no route, because `agent/serve.py` declared three paths and `/`
was not one of them. Locally it 404s identically, so nothing regressed in the
deploy — the endpoint had been that way since it was written.

The uncomfortable part is in the entry directly above this one. Two hours
earlier I used that exact line —

    /          -> {"detail":"Not Found"}  (22b, FastAPI: the request arrives)

— as *evidence*, the proof that requests were reaching the container and that the
`/healthz` interception was therefore GFE's doing and not the app's. It was good
evidence. I read it purely as a signal and never once as a symptom, because I was
debugging as an operator holding a hypothesis, and to an operator a FastAPI 404 at
the root is unremarkable. To the first person who pastes a demo URL into a
browser, it is the whole product failing to start. Same twenty-two bytes.

So: `GET /` now answers. One handler, two audiences — a browser (`Accept:
text/html`) gets a small card naming the endpoints, everything else gets the same
content as JSON, both rendered from one `SERVICE` dict so they cannot drift.

One wrinkle underneath it, of the same family as the last three. The card prints
a paste-able `curl` built from `request.base_url`, and Cloud Run terminates TLS
upstream: the request reaches the container as plain `http`, and uvicorn honours
`X-Forwarded-Proto` only from `127.0.0.1`, which Google's frontend is not. The
landing page would have handed every visitor an `http://` command for an
https-only service. Read the header where it is needed rather than granting
global `X-Forwarded-*` trust for the sake of one string.

The lesson from the previous entry was "only the run tells you". This one narrows
it: only a run *by someone who is not you* tells you. I had the output in my
terminal and drew the wrong conclusion from it, not because the evidence was
lacking but because I already knew what I was looking for.

Three tests, at `tests/test_engine.py::test_root_is_not_a_404` and the two beside
it. 364 tests.


## 2026-09-10 — something to try, and a budget that survives it

Two criticisms of the deployed service, both fair, both from the position that
actually matters — someone arriving with a link and not much time. It was not
nice to look at, and there was nothing to *try*. The endpoint returned JSON to a browser and
that was the entire interface. Fixing the 404 at `/` earlier today had made the
front door answer; it had not made it a door anyone wanted to walk through.

`web/index.html` is now a playground. The four questions —
the ones verified cold against the live stack — are one click each, and each is
labelled with *what it proves* rather than just what it asks, because a stranger
should not have to reverse-engineer why one question is more interesting than
another. What comes back is the answer, the judge scorecard, and the full tool
timeline with the PromQL, LogQL and TraceQL the agents actually wrote. That last
panel is the one that matters: it is the difference between claiming a join
exists and showing the query that performs it.

Three things worth recording from building it.

**The banner was a portrait.** The hero image was 1200×630 and I gave it
`width:100%; max-width:440px` plus the intrinsic `width`/`height` attributes —
and no `height:auto`. The attribute won, so it rendered 440px wide and 614px
tall, squashed. Nothing in a test would ever have caught that; it took looking at
the page. Which is the same lesson as the two entries above it, arriving by a
third route: I keep finding the class of bug that is invisible to everything
except a person looking at the actual artefact.

**The page misreported its own judges.** It counted LLM-scored checks with
`actor_type === "llm"`. The field's values are `deterministic` and `ai`
(`agent/evaluation.py`), so the filter matched nothing and the page confidently
announced "6 deterministic checks, 0 judged by Gemini" under three chips that
were plainly the LLM judge's. A confident, specific, wrong sentence about the
project's most distinctive feature. It now counts what is *not* deterministic,
so a new actor type cannot silently become a deterministic one.

**Open and unbounded are different things.** Every `/ask` is real Gemini calls on
a real bill, so `agent/limits.py` caps three things that fail in three different
ways: a per-visitor rolling window (one person hammering it), a global daily
budget (the actual spend ceiling), and concurrency (a run holds an `mcp-grafana`
subprocess for ~40s — unbounded concurrency does not cost more, it just makes
every simultaneous run slow enough to look broken).

Two details in there are load-bearing. Admission *decides and records* under one
lock: check-then-increment as two steps lets a simultaneous burst all observe the
same pre-increment count and sail through, which a sixteen-thread test against a
budget of ten now pins. And a refused request must cost no Gemini call and spend
no budget — otherwise a bot that only ever receives 429s still drains the day for
everyone, and the rate limiter becomes the denial of service.

The honest limitation: these counters live in the process, so they are per
*instance*. With `--max-instances 2` the true ceiling is twice the configured
daily number, and a cold start forgets the window early. Exact global limits need
shared state, which is real machinery for a demo whose worst case is already
bounded at two instances. The module says so in as many words rather than
implying the numbers are global — the right number to check against a billing
alert is the product, not the setting.

One last trap, of the same family as the buildpack one: `web/` was listed in both
`.dockerignore` and `.gcloudignore`, from when it was an empty placeholder for
the supervisor console. Left alone, the deploy would have shipped a landing page
that 404s its own banner — and only in production, since a local run reads the
file straight off disk. `tests/test_reproducibility.py` now fails if either file
excludes it again.

One more, found by re-reading my own claim: the `og:image` was `/banner.png`, a
relative URL. Scrapers do not run JS and resolve relative paths unreliably, so
the pasted-link preview — the single place that image genuinely earned its keep
— would have quietly rendered nothing. Fixed then by making it absolute, pinned
by a test that checked the scheme rather than merely the presence of the tag.

*(Later: the banner was removed altogether and the page hero is now type. The
Open Graph tags remain, absolute and tested, minus the image.)*

387 tests.

## 2026-09-10 — the other side of the glass, without opening the stack

The playground shows a visitor the front of the product: a question, an answer,
a scorecard. It never showed the back — the Grafana Cloud stack all of that is
read from. The obvious fix is Grafana's own public-dashboard sharing, and it is
the wrong one for three separate reasons, only one of which I expected.

The one I expected: a natively shared board runs **every panel's query for every
anonymous visitor**, against the owner's stack, with no per-viewer limit. `/ask`
is capped three ways; a shared public-dashboard link is capped in none. The risk
is not exfiltration, it is denial of wallet — one scraper and the free-tier quota
is gone.

The one I did not expect: the crew board would have published `di-pool-1`.
`turnaround_pool_headcount` returns 2 for it, live, and "hours per rostered
artist" for a two-person pool is approximately one person's timesheet. The
board's own text panel documented this as deliberate — shown for context,
excluded from every alert. Behind auth that is a defensible internal choice.
Anonymous, it publishes exactly what `privacy_floor_respected` fails the agent
for saying. Anyone who reads that check and then opens the dashboard finds the
contradiction in about ninety seconds.

The third: the EvalOps log panel selector was bare — `{service_name="turnaround-agent"}`.
Today that stream carries only evaluation events; I sampled it. But it is
unfiltered, so it publishes whatever this service logs *next* — and since the
playground shipped, that includes judge explanations paraphrasing questions a
stranger typed into `/ask`. An open channel from a text box to a public page.

So `agent/backend.py` instead: a fixed board of nine queries the service runs
with its own credential. The strongest form of an allowlist turned out not to be
validating a request but having nowhere to put one — the endpoint takes no
parameters at all, and a test asserts the OpenAPI schema declares none. The
privacy floor moved from a convention into the PromQL:
`and on(pool) (max by (pool) (turnaround_pool_headcount) >= 3)`, with a test
tying the `3` in the query to `MIN_POOL_SIZE` in the code, so raising one and
forgetting the other fails the build. Results are cached for 30 s **while
holding the lock** — building outside it would keep readers moving but let N
simultaneous misses fan out into N upstream queries, which is the exact thing
this module exists to prevent.

Two things the seed data taught me on the way. `sum(turnaround_shots_approved_total)`
is empty for most of every quarter hour: the show re-seeds every 15 minutes and
Prometheus drops a series 5 minutes after its last sample, so a bare instant
query blinks out between runs. Every metric tile reads
`max_over_time(metric[20m])` — a window wider than the cycle. And an empty Loki
result is not a zero, except when it is: "no privacy breaches" is the best fact
on the page, and rendering it as `--` would hide a clean record behind what
looks like a broken panel. Counting tiles floor to zero; score tiles do not,
because a mean grounding score of 0.00 would claim the judges failed everything.

Found while wiring it: `/ask` had been returning `grafana_url` in every
response. Nothing rendered it. A public endpoint handing every caller the stack
hostname is a free pointer at its login page, so it is gone, and the contract
test now pins its absence rather than its presence.

The internal boards kept the small pools, in one panel titled *"Below the
aggregation floor — internal only, never share this panel"*. A test asserts
there is exactly one such panel and that every other pool aggregation on that
board carries the floor. The point is not to hide the number from the studio —
it is that anyone reaching for a share button has been told, on the board
itself, what they would be sharing.

409 tests.

---

## 2026-09-13 — the demo moves out

Everything above this entry describes one repository that was two things: a
library for joining a schedule to a render farm, and a hosted demo proving it
works. Today those separate. The web application — `agent/serve.py`,
`agent/backend.py`, `agent/limits.py`, `web/`, the Dockerfile and `deploy/` —
moves to its own repository, along with the tests that covered it. Entries
before this one still describe the whole; they are a record of what was built,
not of what is here.

The reason is what the two halves are *for*. The demo is a proof of concept for
one deployment: one billing account, one Cloud Run service, rate limits tuned
for a public link. Nobody reuses that. The library is the part someone points at
their own stack, and it was declaring FastAPI and `uvicorn[standard]` to run
four commands that never serve anything.

A caveat I nearly published without checking: dropping those two does *not*
mean they stop being installed. `google-adk` requires `fastapi`, `starlette` and
`uvicorn` itself, so they arrive transitively either way. What actually leaves
the install is the `[standard]` extra — `uvloop`, `httptools` and `watchfiles` —
and what the manifest stops doing is claiming a dependency this package does not
use. Measured on the built wheel rather than assumed, which is the only reason
the first version of this paragraph is not still wrong.

Three things fell out that are worth recording.

**The split cost nothing structurally, and that was not luck.** `agent/engine.py`
exists because the CLI and the HTTP endpoint were once ~70% copies of the same
pipeline; consolidating them into one `answer_question` was a code-review fix
months before any of this. The payoff arrived today: the service moved without
the pipeline being touched. Nothing under `bridge/`, `seed/`, `grafana/` or the
agent tier imported `serve`, `limits` or `backend` — only `serve.py` imported
*them*. A boundary drawn for one reason held for a different one.

**Removing the service exposed configuration that had quietly died with it.**
`TURNAROUND_ASKS_PER_HOUR`, `TURNAROUND_ASKS_PER_DAY`, `TURNAROUND_CONCURRENT_ASKS`
and `TURNAROUND_PUBLIC_DOCS` were still in `.env.example`, still parsed into
`Settings`, and read by nothing — the exact failure mode P2-3 was about, arriving
by a different route. The `.env.example` honesty test caught three of the four
within seconds of the move. It also flagged `OTEL_EXPORTER_OTLP_PROTOCOL`, which
is a false positive worth keeping: the OpenTelemetry SDK reads that itself, so
no grep of this source will ever find it. It is now an allowlist of one, with
the reason written next to it.

**The suite got seven times faster.** 22 s to 3 s, from 601 tests to 510. The
tests that left were the ones standing up FastAPI and a full ADK system per
case. Nothing was lost — they moved with the code they test — but it is a fair
measure of how much of the old suite was exercising the demo rather than the
product.

510 tests.

---

## 2026-09-13 — the first command a new user runs

A review of the merged repository from a clean clone, taking the README at its
word and running only what it offers.

**CI had never run. Not once.** `astral-sh/setup-uv@v10` does not exist: that
action publishes moving major tags up to `v7` and exact releases above it, so
`@v10` resolved to nothing and all three jobs died at "Set up job" in two
seconds. Every run since the workflow was added was red, and none of them had
executed a single test. The workflow that exists to prove the suite runs was the
one thing nobody had checked ran. Pinned to `v10.1.0`, and
`test_every_ci_action_is_pinned_to_something_that_resolves` now asserts the
property offline — `astral-sh/*` must carry an exact release — because the suite
cannot ask GitHub whether a tag exists.

**The README's headline promise was false.** It offers
`python -m seed.populate --dry-run` twice as the thing that "needs no accounts
and nothing configured". It exited 2 on an unset pseudonym salt. That was the
first command a new user ran, and it failed.

The gate was right in general and wrong here. A guessable salt matters because it
makes pseudonyms reversible to anyone holding a crew list — which requires the
pseudonyms to *leave*. A dry run writes to in-memory exporters and prints
"nothing left this machine". `install_ephemeral_salt` gives that run a random
16-byte salt, which is stronger than a configured one rather than weaker: it is
unguessable and it does not outlive the process. It is announced, not silent,
or a passing dry run would read as proof that a real `.env` is filled in. The
exporting path still fails closed, and a second test pins that down — a real
seed's pseudonyms have to stay stable across seeds to be joinable at all.

**CI could not have caught it, because CI tested the workaround.** The wheel
job writes a salt of its own before running the same command. It proved the
seeder works when configured, which was never in doubt.

**Neither could the test that existed to catch it.** 
`test_the_repo_is_usable_by_someone_who_just_cloned_it` said it guarded against
"a README whose quickstart drifted away from the commands that work". What it
did was assert that the strings `uv sync` and `uv run pytest` appear somewhere in
the README. It passed continuously while the documented command exited 2. A test
that greps for the documentation of a command knows nothing about the command.
It now runs it, in a subprocess, from a directory with no usable `.env`.

Getting that test honest took two tries, which is itself the finding. The first
draft deleted `TURNAROUND_*` from the environment and passed — for the wrong
reason. A subprocess walks straight past the autouse fixture that keeps the
suite away from a developer's `.env`, and `env_path` falls back to
`REPO_ROOT/.env`, so it had found the real one. It now writes an empty `.env`
in `tmp_path` (found before the fallback is reached) *and* exports the
placeholder salt, which beats any file outright.

**`turnaround-refresh` hung, and lied about why.** Run unconfigured it printed
`FAILED: simulating the show...` and then sat there. Two faults, compounding:

- `_seed` built its report from the last line of *stdout* whenever stdout was
  non-empty, falling back to stderr only if it was empty. `seed.populate` prints
  "simulating the show..." before anything can fail, so stdout was never empty
  and stderr was never shown. Every failure reported the one step that worked,
  and the sentence naming the unset variable — ending in the command that fixes
  it — was discarded.
- `main` then slept fifteen minutes and tried again, forever. Every failure this
  command actually meets is a standing one; an unset salt does not heal on a
  timer. It now exits 1 if the *first* seed fails, and keeps retrying after that,
  where a failure really is transient.

`seed/refresh.py` had no behavioural test at all — only a check that its entry
point exists. It has three now. Its docstring also still pointed at `deploy/`,
which left in the split; so did a docstring in `tests/test_config.py`.

**A test that failed about one run in ten, and the bug underneath it.**
`test_plugin_builds_a_nested_trace_from_adk_callbacks` failed on a fresh clone,
passed in isolation, and passed twenty times in a row afterwards. That shape --
intermittent, order-independent, unreproducible on demand -- is what a dangling
`id()` looks like from the outside.

`observability/adk.py` keyed open `execute_tool` spans on `id(tool_context)`
while deliberately holding no reference to the context. The object is freed the
moment `before_tool_callback` returns, and the lookup in `after_tool_callback`
worked only because CPython usually hands the next object the address it just
freed. Measured: the address is reused 1000/1000 times when nothing else is
allocated in between, and never when three objects are. So every test of this
path had been passing for a reason unrelated to the code being correct.

The consequence in production is worse than a flaky test: a missed lookup means
the span is never closed, so it never reaches the exporter and never appears in
Tempo. The evidence chain the whole product is built on would have had holes in
it, silently, under memory pressure. And a freed address can be reissued to an
unrelated object, which turns the miss into a hit that closes the wrong span.

Now keyed on ADK's `function_call_id` -- already read three lines above the
store, and an identity the plugin is actually entitled to keep. The two new
tests hold the first context alive, so the address cannot be recycled and the
old keying fails deterministically rather than one run in ten. 25 consecutive
full runs, clean.

518 tests.
