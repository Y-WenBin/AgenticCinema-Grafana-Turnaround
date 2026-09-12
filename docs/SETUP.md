# End-to-end setup

How to stand up Turnaround from nothing: Google Cloud (Vertex AI), Grafana Cloud,
the local toolchain, then the workflow — first with the built-in simulated show,
then against a real VFX / editorial tool.

---

## 0. What connects to what

```
  a source  ──►  bridge/  ──OTLP──►  Grafana Cloud  ◄──MCP──  agent/  ──►  Vertex AI
  (schedule,     (relabel on         Tempo · Loki             (ADK multi-        (Gemini
   farm, cut)     shot_id)           Mimir · Alerts            agent + judge)     2.5 flash)
```

Two ways to feed the *source* box:

| Mode | Source | Status | Use it to |
|---|---|---|---|
| **A — simulated** | `seed/` generates *Nightfall* (200 shots) and writes real telemetry to your stack | **fully wired**, deterministic | test the whole pipeline end to end today |
| **B — real tools** | a live tracker / render farm / NLE | **parsers + adapter Protocols shipped** (`bridge/ontology.py`, `bridge/sources.py`, `bridge/editorial.py`); the live ingest adapter is ~40 lines you write against the Protocol | prove the join works on your studio's data |

Do **Mode A first** — it exercises Grafana + Vertex + the agent exactly the same
way, so if A works, B is just swapping the data in.

---

## 1. Google Cloud Platform (Vertex AI)

1. **Project + billing.** Create a project at <https://console.cloud.google.com>
   (note the *project ID*, not the name) and attach a billing account.
2. **Enable the API:**
   ```bash
   gcloud config set project YOUR_PROJECT_ID
   gcloud services enable aiplatform.googleapis.com
   ```
   (or the console: APIs & Services → enable **Vertex AI API**).
3. **Pick a region** with Gemini 2.5 Flash — `us-central1` is safe (also
   `us-east4`, `europe-west1`, `asia-southeast1`).
4. **Authenticate.** Local development — use Application Default Credentials, no
   key file:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```
   `google-genai` picks ADC up automatically; leave `GOOGLE_APPLICATION_CREDENTIALS`
   unset in `.env`.

   *Alternative — a service-account key* (needed if ADC isn't available where you
   run, and what `deploy/` avoids by using the Cloud Run SA):
   ```bash
   gcloud iam service-accounts create turnaround-local --display-name "Turnaround local"
   gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
     --member "serviceAccount:turnaround-local@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
     --role roles/aiplatform.user
   mkdir -p .secrets
   gcloud iam service-accounts keys create .secrets/gcp-sa.json \
     --iam-account turnaround-local@YOUR_PROJECT_ID.iam.gserviceaccount.com
   ```
   then set `GOOGLE_APPLICATION_CREDENTIALS=.secrets/gcp-sa.json` in `.env`
   (`.secrets/` is git-ignored).

Model note: everything runs on `gemini-2.5-flash`, and `TURNAROUND_GEMINI_MODEL`
is the one variable that changes it. Point it at `gemini-2.5-pro` only if you
have pro quota — a fresh project 429s on the first analyst call.

Speed note: `TURNAROUND_THINKING_BUDGET` defaults to `0` — Gemini 2.5 thinking
off. Measured end to end against a live stack, same question, only this changed:
**dynamic thinking 75.4s / 60.7s, off 24.5s / 18.5s**, with every scorecard
check passing either way. The analysts run finished PromQL recipes and report
the numbers, so there is nothing for thinking to do. Set it to `-1` to hand the
decision back to Gemini.

---

## 2. Grafana Cloud

Create a free account at <https://grafana.com> — it provisions a stack at
`https://<name>.grafana.net`. You need **two** credentials from it.

### 2a. OTLP write credentials — for `bridge/` and `seed/`

Stack → **Connections → Add new connection → OpenTelemetry (OTLP)** (or the
"OpenTelemetry" tile). It shows you, ready to paste:

- `OTEL_EXPORTER_OTLP_ENDPOINT` — `https://otlp-gateway-prod-<region>.grafana.net/otlp`
- an **Instance ID** (a number) and a **Generate token** button — token scopes
  `metrics:write logs:write traces:write`
- the exact `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64>` line
  (base64 of `<instanceID>:<token>`)

Copy all three into `.env`. Keep the `%20` for the space if you copy the header
by hand: `Authorization=Basic%20<base64>`.

### 2b. A service-account token — for provisioning, MCP reads, write-back

Stack → **Administration → Users and access → Service accounts → Add service
account**. Role **Editor** is enough: the account creates a folder, dashboards,
alert rules and ML jobs, reads Mimir/Loki/Tempo through MCP, and writes
annotations — and an Editor can do all of that. Do **not** give it Admin. Admin
adds the ability to read users, service accounts, API keys and datasource
secrets, none of which this project touches, and this one token is mounted into
a publicly reachable Cloud Run service.

Then **Add service account token** → copy the `glsa_…` value.

> If provisioning fails with a 403 on `/api/datasources/uid/…`, your role lacks
> `datasources:read`. Grant that one permission, or set `GRAFANA_DS_PROM_ID`
> directly (part 2c) — both are better than promoting the token to Admin.

```
GRAFANA_URL=https://<name>.grafana.net
GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_…
```

### 2c. Datasource UIDs

Grafana Cloud's built-in datasources are usually `grafanacloud-prom` /
`grafanacloud-logs` / `grafanacloud-traces` (the defaults in `agent/config.py`).
Confirm and override if yours differ:

```bash
curl -s -H "Authorization: Bearer $GRAFANA_SERVICE_ACCOUNT_TOKEN" \
  "$GRAFANA_URL/api/datasources" | python3 -m json.tool | grep -E '"(name|uid|type)"'
```

Set `GRAFANA_DS_PROM_UID` / `GRAFANA_DS_LOKI_UID` / `GRAFANA_DS_TEMPO_UID` to the
`uid` of the `prometheus` / `loki` / `tempo` datasource.

Grafana's ML API wants Prometheus's *numeric* `id` rather than its uid, and that
number differs per stack. `grafana/ml/build.py` looks it up from the uid when it
provisions, so there is nothing to set — `GRAFANA_DS_PROM_ID` only exists to skip
the lookup.

---

## 3. Local toolchain

```bash
# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/Y-WenBin/AgenticCinema-Grafana-Turnaround.git turnaround
cd turnaround
uv python install 3.12
uv sync --group dev
uv run pytest -q            # all green, no credentials needed — proves the checkout
uv run ruff check .

# mcp-grafana (the agent's tool server). Any one of:
brew install grafana/grafana/mcp-grafana                       # macOS
go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@v1.3.0
# …or download the v1.3.0 release tarball for your OS and put the binary on PATH
```

Turnaround auto-finds `mcp-grafana` on `PATH` or in `/opt/homebrew/bin`,
`/usr/local/bin`, `~/go/bin`; otherwise set `TURNAROUND_MCP_GRAFANA_BIN`.

**Check it works before you have any accounts.** This runs the whole simulated
show through the real write path against in-memory exporters — no Grafana, no
Vertex, no network:

```bash
uv run python -m seed.populate --dry-run   # prints the show, ends "nothing left this machine"
```

**Installing instead of cloning?** `uv tool install turnaround` (or `pip install`)
gives you the same five steps as commands, which read `.env` from the directory
you run them in:

| Command | Same as |
|---|---|
| `turnaround-seed` | `python -m seed.populate` |
| `turnaround-refresh` | `python -m seed.refresh` |
| `turnaround-provision` | `python -m grafana.provision` |
| `turnaround-ask` | `python -m agent.run` |
| `turnaround-serve` | `python -m agent.serve` |

The rest of this guide uses the `python -m` form, since it works from a checkout
either way.

---

## 4. The `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Fill in (from parts 1–2):

```ini
# Grafana Cloud — OTLP write
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-prod-<region>.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64 instanceID:token>

# Grafana Cloud — API
GRAFANA_URL=https://<name>.grafana.net
GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_…
GRAFANA_DS_PROM_UID=grafanacloud-prom
GRAFANA_DS_LOKI_UID=grafanacloud-logs
GRAFANA_DS_TEMPO_UID=grafanacloud-traces

# Google Cloud — Vertex
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GOOGLE_CLOUD_LOCATION=us-central1
# GOOGLE_APPLICATION_CREDENTIALS=.secrets/gcp-sa.json   # only if not using ADC

# Privacy — generated below, not typed. Keep it out of git.
TURNAROUND_PSEUDONYM_SALT=

# Optional: real tool conventions (part 6)
# TURNAROUND_SHOT_ID_SCHEME=seq_sh
# TURNAROUND_FARM_CONVENTION=opencue
```

Then generate the salt, rather than typing one:

```bash
echo "TURNAROUND_PSEUDONYM_SALT=$(openssl rand -hex 16)" >> .env
```

Run that in a shell, not inside `.env`. `.env` is read literally — `load_env`
does no interpolation, by design — so `TURNAROUND_PSEUDONYM_SALT=$(openssl rand
-hex 16)` written *in the file* is a salt whose value is the twenty-three
characters `$(openssl rand -hex 16)`. Every deployment that copied it would
share one publicly documented salt, and shared salts make the artist pseudonyms
reversible by anyone holding a crew list, which is the whole thing they exist to
prevent. The entry points now refuse to start on that string, and on `change-me`
— but the fix is to generate a real one.

Every entry point loads `.env` itself — `agent.run`, `agent.serve`,
`seed.populate`, `seed.refresh` and `grafana.provision`. A variable already
exported in your shell always wins, so CI and Cloud Run (which set the
environment directly and ship no `.env`) are unaffected.

---

## 5. Mode A — run the whole pipeline with the simulated show

```bash
uv run python -m seed.populate      # writes ~1450 spans, ~1470 logs, ~5900 metric points + 1 annotation
uv run python -m grafana.provision  # folder "Turnaround" + 4 dashboards + alert groups + ML jobs

uv run python -m agent.run "Why is SEQ0420 slipping, and what is it costing in artist-days?"
```

The history is compressed into a ~45-minute wall-clock window ending at the
moment of seeding, and every query reads it with `last_over_time((…)[2h:])`.
Roughly two hours after a seed the queries return nothing at all — which reads
as a broken agent, not as stale data. **Re-seed before any live session**, and
keep it fresh while you explore:

```bash
uv run python -m seed.refresh       # re-seeds every 15 min; Ctrl-C to stop
```

Other demo questions:

```bash
uv run python -m agent.run "What is SEQ0420 costing us in artist-days?"
uv run python -m agent.run "Who is heading for crunch, and when?"
uv run python -m agent.run --approve "What do I change to avoid both the slip and the crunch?"
```

`--approve` lets the Remediator's writes through the gate (annotation + Kitsu
write-back); the default denies them.

### What a run should produce

- an **Answer / Evidence / Remediation** block
- a **tool timeline** with `*`-marked Grafana MCP calls (`query_prometheus`,
  `query_loki_logs`, `tempo_traceql-search`)
- `gen_ai trace emitted … gen_ai.response.id=turnaround-…`
- a **scorecard**: `grounding_numbers`, `mechanism_named`,
  `privacy_floor_respected` pass; the LLM judge's `hallucination` ≥ 0.8

### Verify in Grafana

Stack → Dashboards → **Turnaround** folder:

- **Turnaround · The Join** — render core-hours per comp iteration; SEQ0420 well above the rest
- **Turnaround · Crew Load** — pools approaching / over 60h (di-pool-1 never shown)
- **Turnaround · Delivery** — burndown
- **Turnaround · EvalOps** — evaluation scores, token/latency, the `response_id → Tempo` pivot

Explore:

| Datasource | Query | Shows |
|---|---|---|
| Mimir | `turnaround_render_waste_core_hours_total` | the waste series, by sequence |
| Loki | `{service_name="turnaround-bridge"} \|= "frame 118"` | the cache-miss log lines |
| Tempo | `{ span.production.shot_id = "SEQ0420_SH0100" }` | the shot as a trace, retakes as error spans |
| Tempo | `{ span.gen_ai.response.id = "<id from your run>" }` | the agent's own `invoke_agent` trace |
| Loki | `{service_name="turnaround-agent"} \| json` | `gen_ai.evaluation.result` events |

---

## 6. Mode B — a real VFX / editorial tool

The join needs only a **shot id** and, on the schedule side, a **task status**.
`bridge/` ships the parsers; connecting a live source is a small adapter against
one of the Protocols in `bridge/sources.py`. Pick whichever layer you want to
test.

### 6a. Editorial — any NLE → EDL or OTIO  *(fully wired today)*

Turnaround reads a cut through two interchange formats, so it does not care which
editor made it. Pick whichever your tool exports.

| Tool | Get a cut out | Read it with |
|---|---|---|
| **DaVinci Resolve** (free) | Timeline → Export → *EDL CMX 3600*, or *OpenTimelineIO* | `parse_edl` / `iter_otio_cut` |
| **Shotcut** (free) | File → Export EDL | `parse_edl` |
| **Kdenlive** (free) | Project → Render / OTIO export | `iter_otio_cut` |
| **Blender VSE** (free) | enable the *Import-Export: OpenTimelineIO* add-on → export `.otio` | `iter_otio_cut` |
| **Premiere Pro** | File → Export → EDL | `parse_edl` |
| **Avid Media Composer** | Output → Export → EDL | `parse_edl` |
| **Final Cut Pro** | Export XML (`.fcpxml`) → also `pip install otio-fcpx-xml-adapter` | `iter_otio_cut` |

Steps with Resolve (the rest are the same idea):

1. Install from <https://www.blackmagicdesign.com/products/davinciresolve>.
2. New project → import any media → drop several clips on a timeline.
3. **Name the clips (or their source files) with shot ids** that match your
   scheme — e.g. `SEQ0420_SH0100`, `SEQ0420_SH0110`. Non-default spelling? set
   `TURNAROUND_SHOT_ID_SCHEME` (see `SHOT_ID_SCHEMES` in `bridge/ontology.py`).
4. Export the cut as EDL (needs nothing extra) or `.otio` (`uv sync --extra editorial`).
5. Read it:
   ```bash
   uv run python - <<'PY'
   from bridge.editorial import parse_edl, shot_ids_from_edl
   text = open("cut.edl").read()
   for item in parse_edl(text):
       print(item.shot_id, item.source_name, round(item.duration_seconds, 1), "s")
   print("shots in the cut:", shot_ids_from_edl(text))
   PY
   ```
   OTIO instead: `from bridge.editorial import iter_otio_cut; list(iter_otio_cut("cut.otio"))`.

The parser also handles an online/conform EDL that carries the shot only in the
reel column (no `FROM CLIP NAME` comment), drop-frame timecode, dissolves, and
Unix or Windows file paths in the comment. Fixtures for every dialect above are
in [`tests/test_editorial.py`](../tests/test_editorial.py).

### 6b. Tracker — Kitsu (open source) → task events + time  *(adapter to write)*

1. Run Kitsu locally (Docker) or use the CG Wire hosted trial:
   ```bash
   git clone https://github.com/cgwire/kitsu && cd kitsu
   # follow README (docker compose); it brings up the Zou API + web UI
   ```
2. In Kitsu: create production **Nightfall**, add sequences/shots
   (`SEQ0420 / SH0100 …`), add task types (Comp, Lighting…), move tasks through
   statuses (WIP → Retake → …), and log time on the timesheet.
3. Write the adapter (`bridge/sources.py` defines the shape):
   ```python
   # bridge/adapters/kitsu.py  (new)
   import gazu
   from bridge.ontology import Department, TaskStatus
   from bridge.sources import ScheduleSource, TaskEvent, TimeSpent

   class KitsuSource:                       # implements ScheduleSource
       def __init__(self, url, email, password, project):
           gazu.set_host(url); gazu.log_in(email, password)
           self.project = gazu.project.get_project_by_name(project)

       def iter_task_events(self):
           for t in gazu.task.all_tasks_for_project(self.project):
               yield TaskEvent(
                   shot_id=t["entity_name"],
                   department=Department(t["task_type_name"].lower()) if ... else None,
                   status=TaskStatus.from_tracker(t["task_status_short_name"]),
                   at=..., revision=t.get("retake_count"))

       def iter_time_spent(self): ...
   ```
   `TaskStatus.from_tracker` already normalises Kitsu / ShotGrid / ftrack
   vocabularies. Then feed its events through the same emit path
   `seed/populate.py` uses (`bridge.emit.Emitter`, `bridge.metrics.MetricBackfill`)
   instead of `ShowSimulation`.

**ShotGrid / Flow Production Tracking / ftrack** are the same job: swap `gazu`
for `shotgun_api3` / `ftrack_api`; the status short-codes (`rev`, `cbb`, `apr`,
"Changes Requested", …) are already mapped.

**No API access from where Turnaround runs?** Export a report and use the
built-in fallback — no adapter needed:
```python
from bridge.sources import CsvScheduleSource
src = CsvScheduleSource.from_csv(open("tracker_export.csv").read())
# columns: shot_id,department,status,pool,hours,at
```

### 6c. Render farm — OpenCue / Deadline → core-hours, frame failures  *(adapter to write)*

1. Bring up a farm:
   - **OpenCue** — `git clone https://github.com/AcademySoftwareFoundation/OpenCue`,
     run `sandbox/` (docker compose); the cuebot Prometheus exporter is on `:8302`.
   - **Deadline** — free for ≤10 workers, from AWS Thinkbox.
2. Submit a render whose job name carries the shot id, in your farm's convention:
   - OpenCue: `nightfall-SEQ0420_SH0100-<user>_comp_v001`
   - Deadline batch name: `SEQ0420_SH0100 comp v1`
3. Resolve job names to shots:
   ```bash
   uv run python - <<'PY'
   from bridge.ontology import parse_job_name
   print(parse_job_name("SEQ0420_SH0100 comp v1", convention="deadline"))
   print(parse_job_name("nightfall-SEQ0420_SH0100-u1_comp_v001"))   # opencue = default
   PY
   ```
   Conventions: `opencue` (default), `deadline`, `tractor`, `qube`,
   `royalrender`, or `template` with `TURNAROUND_FARM_JOB_PATTERN=<regex with
   (?P<shot_id>…) (?P<dept>…) (?P<version>…)>`.
4. Adapter: scrape `OPENCUE_METRICS_URL`, run each job name through
   `parse_job_name`, emit `turnaround_render_core_hours_total` /
   `_frames_failed_total` / `_waste_core_hours_total` relabelled onto
   `sequence` / `shot_id` / `department` (mirror `seed/populate.py::build_metrics`).

Once any adapter writes to Grafana Cloud, **part 5's agent run and dashboards
work unchanged** — that's the point of the relabel.

---

## 7. (Optional) deploy the agent to Cloud Run

Needs `gcloud` installed and authenticated.

```bash
./deploy/deploy.sh          # project from GOOGLE_CLOUD_PROJECT in .env
```

It enables APIs, makes a least-privilege runtime service account
(`roles/aiplatform.user`), pushes the Grafana + OTLP secrets to Secret Manager,
builds `./Dockerfile` once, and deploys two things from that one image: the
agent service, and a **re-seed job** on a 15-minute Cloud Scheduler trigger so
the compressed window never ages out from under the hosted demo. Then:

```bash
URL=$(gcloud run services describe turnaround-agent --region us-central1 --format 'value(status.url)')
curl -s "$URL/health" | python3 -m json.tool
curl -s -H 'content-type: application/json' \
  -d '{"question":"why is SEQ0420 slipping and what is it costing?"}' "$URL/ask" | python3 -m json.tool
```

Use `/health`, not `/healthz`: Google Frontend intercepts the exact path
`/healthz` on `*.run.app` and returns its own HTML 404 without ever reaching the
container. Same handler, different path.

The service is public so a reviewer can open it. It cannot write anything:
`serve.py` runs `AutoApprover(approve=False)`, `--max-instances` caps the blast
radius and `TURNAROUND_MAX_LLM_CALLS` caps the spend of any one request.

Full detail: [`deploy/README.md`](../deploy/README.md).

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `PrivacyViolation: TURNAROUND_PSEUDONYM_SALT is unset` | No `.env` at the repo root, or the key is missing from it. Every entry point loads `.env` itself now — check `TURNAROUND_PSEUDONYM_SALT` is set and is not still `change-me` |
| OTLP `401` on seed | `OTEL_EXPORTER_OTLP_HEADERS` base64 must be of `instanceID:token`, not the token alone; keep `%20` after `Basic` |
| Mimir rejects samples ("out of order" / "too old") | Use plain `seed.populate` (it compresses to a 45-min window). Don't pass `--compress 0` against hosted Grafana Cloud |
| Dashboards empty a few minutes after seeding | Run `seed.refresh`, or widen a panel's range to `now-2h` (queries use `last_over_time(…[2h:])`) |
| Vertex `403 PERMISSION_DENIED` | `gcloud auth application-default login` not done, `aiplatform.googleapis.com` not enabled, or wrong `GOOGLE_CLOUD_PROJECT` |
| Vertex `429 RESOURCE_EXHAUSTED` | You pointed `TURNAROUND_GEMINI_MODEL` at a model you have no quota for; unset it — `gemini-2.5-flash` is the default and a fresh project has quota for it |
| `mcp-grafana: command not found` | Install it (part 3) or set `TURNAROUND_MCP_GRAFANA_BIN=/full/path/mcp-grafana` |
| Agent: `circuit breaker tripped` | A run hit `TURNAROUND_MAX_LLM_CALLS` (default 40). Raise it for a genuinely long run; otherwise it caught a tool-retry loop |
| `HostedMcpNotAuthorized` | You set `TURNAROUND_MCP_MODE=hosted` — run `uv run python -m agent.mcp_login` for the OAuth flow, or unset it to use the default OSS mode |
| EDL parses 0 shots | Clip names don't match the active `ShotIdScheme`; rename clips to `SEQ####_SH####` or set `TURNAROUND_SHOT_ID_SCHEME` |
