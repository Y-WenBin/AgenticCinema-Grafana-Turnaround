# Deploying Turnaround to Cloud Run

The agent runs as one Cloud Run service: `agent/serve.py` (FastAPI) wrapping the
same deterministic ADK pipeline the CLI drives, with the OpenTelemetry GenAI
self-instrumentation and the judge tier on by default. Every request emits its
`invoke_agent` trace, token/latency histograms and `gen_ai.evaluation.result`
events to the same Grafana Cloud stack it queries.

## What's here

| File | Purpose |
|---|---|
| `../Dockerfile` | Python 3.12 + `uv`-installed deps + the pinned, checksum-verified `mcp-grafana` binary (OSS MCP mode spawns it as a stdio subprocess). **At the repo root deliberately** — see below. |
| `deploy.sh` | One-shot deploy: enables APIs, makes a least-privilege runtime service account (`roles/aiplatform.user` only), pushes Grafana + OTLP credentials to Secret Manager, builds the image once, then deploys the agent service *and* the re-seed job from it. |
| `../.gcloudignore` / `../.dockerignore` | Keep `.env`, `.secrets/`, `.venv` and caches out of the build context and the image. |

### Why the Dockerfile is at the root

`gcloud run deploy --source .` and `gcloud builds submit` build a Dockerfile
only when one is present in the **source root**. With the file at
`deploy/Dockerfile` the build silently falls back to Google Cloud buildpacks: the
deploy succeeds, and the resulting image has no `mcp-grafana` binary and the
wrong entrypoint, so the service fails at the first tool call. `deploy.sh` now
builds explicitly (`gcloud builds submit --tag`) and deploys both workloads with
`--image`, so there is no implicit build path left to guess wrong.
`tests/test_reproducibility.py::test_deploy_builds_the_dockerfile_not_a_buildpack`
pins it.

## Prerequisites

- `gcloud` CLI, authenticated (`gcloud auth login`) with a project that has billing.
- A filled-in `../.env` (copy `../.env.example`). The deploy reads it for
  `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
  `OTEL_EXPORTER_OTLP_HEADERS`, `TURNAROUND_PSEUDONYM_SALT` and the datasource UIDs.
- Vertex AI API available in the target project (the script enables it).

## Deploy

```bash
./deploy/deploy.sh
```

The project comes from `GOOGLE_CLOUD_PROJECT` in your `.env`; set
`PROJECT_ID=other-proj` (and `REGION=`) to override either.

This deploys **two** workloads from one image:

| Workload | What it is |
|---|---|
| `turnaround-agent` (service) | The FastAPI endpoint. Public, so a reviewer can open it. |
| `turnaround-seed` (job) | `python -m seed.refresh --once`, triggered every 15 minutes by Cloud Scheduler. |

The service is public (`--allow-unauthenticated`) because a hosted demo nobody
can open is not a demo. It is safe to be public for reasons that are enforced in
code rather than promised: `serve.py` runs `AutoApprover(approve=False)` so no
request can mutate Kitsu or Grafana, the analysts are wired to `mcp-grafana`
started `--disable-write`, `--max-instances 2` caps the blast radius, and
`TURNAROUND_MAX_LLM_CALLS` caps the spend of any single request.

```bash
URL=$(gcloud run services describe turnaround-agent --region us-central1 --format 'value(status.url)')

curl -s "$URL/health" | jq
curl -s -H 'content-type: application/json' \
  -d '{"question":"why is SEQ0420 slipping and what is it costing?"}' \
  "$URL/ask" | jq
```

**Curl `/health`, not `/healthz`.** Google Frontend reserves the exact path
`/healthz` on a `*.run.app` hostname: it answers with its own HTML 404 and the
request never reaches the container, so a perfectly healthy service looks dead.
`/health` is the same handler under a path GFE lets through; `/healthz` is kept
for Cloud Run's own probes and for deployments not behind GFE.

To lock it down again: `gcloud run services remove-iam-policy-binding
turnaround-agent --region us-central1 --member allUsers --role roles/run.invoker`.

### The re-seed job

The show is compressed into a ~45-minute wall-clock window ending at the moment
of seeding, and hosted Mimir will not accept samples much older than an hour. A
stack seeded three hours ago answers every question with nothing, which reads as
a broken agent rather than stale data. The job re-seeds on a schedule so the
newest points are always a few minutes old.

```bash
gcloud scheduler jobs pause  turnaround-seed-every-15m --location us-central1   # stop ingesting
gcloud scheduler jobs resume turnaround-seed-every-15m --location us-central1
gcloud run jobs execute turnaround-seed --region us-central1 --wait             # seed right now
```

Deploy with `SEED_PAUSED=1` to create the schedule paused. Each run is a full
deterministic re-seed that overwrites rather than forks; the cost that matters
is **Grafana Cloud ingestion**, not Cloud Run compute, so pause it when you are
not demoing.

`/ask` returns `{answer, timeline, evaluation, response_id, circuit_breaker_tripped, grafana_url}`.
Take `response_id` into Tempo (`{ span.gen_ai.response.id = "<id>" }`) to see the
trace, or into Loki (`{service_name="turnaround-agent"} | json | response_id="<id>"`)
for that run's evaluation events.

## Notes

- **Writes are disabled on the endpoint.** `serve.py` uses `AutoApprover(approve=False)`;
  a public URL must not be able to mutate Kitsu or Grafana. The gated write-back
  path is CLI-only (`uv run python -m agent.run --approve "..."`). Enforced by
  `test_reproducibility.py::test_http_endpoint_cannot_approve_writes`.
- **Auth model.** Vertex is the runtime service account's ADC — no key file in the
  image. Grafana and OTLP secrets are Secret Manager versions mounted as env vars.
- **Circuit breaker.** `TURNAROUND_MAX_LLM_CALLS` (default 40) caps Gemini calls
  per request; the deterministic 5-step pipeline needs ~30 in the worst case.
- **MCP mode.** `TURNAROUND_MCP_MODE=oss` (the image default). The hosted
  `mcp.grafana.com` OAuth mode has no unattended token path and is not deployable;
  it is exercised from the CLI via `agent/mcp_login.py`.
- **Cold starts.** ADK + Vertex client init is a few seconds; `--concurrency 4`
  and `--cpu 2` keep a warm instance responsive for a demo. Set `--min-instances 1`
  if you want to remove cold starts entirely (costs money while idle).
