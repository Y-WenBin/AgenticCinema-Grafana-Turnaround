# Deploying Turnaround to Cloud Run

The agent runs as one Cloud Run service: `agent/serve.py` (FastAPI) wrapping the
same deterministic ADK pipeline the CLI drives, with the OpenTelemetry GenAI
self-instrumentation and the judge tier on by default. Every request emits its
`invoke_agent` trace, token/latency histograms and `gen_ai.evaluation.result`
events to the same Grafana Cloud stack it queries.

## What's here

| File | Purpose |
|---|---|
| `Dockerfile` | Python 3.12 + `uv`-installed deps + the pinned, checksum-verified `mcp-grafana` binary (OSS MCP mode spawns it as a stdio subprocess). |
| `deploy.sh` | One-shot deploy: enables APIs, makes a least-privilege runtime service account (`roles/aiplatform.user` only), pushes Grafana + OTLP credentials to Secret Manager, `gcloud run deploy --source .`. |
| `../.gcloudignore` / `../.dockerignore` | Keep `.env`, `.secrets/`, `.venv` and caches out of the build context and the image. |

## Prerequisites

- `gcloud` CLI, authenticated (`gcloud auth login`) with a project that has billing.
- A filled-in `../.env` (copy `../.env.example`). The deploy reads it for
  `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
  `OTEL_EXPORTER_OTLP_HEADERS`, `TURNAROUND_PSEUDONYM_SALT` and the datasource UIDs.
- Vertex AI API available in the target project (the script enables it).

## Deploy

```bash
PROJECT_ID=your-gcp-project REGION=us-central1 ./deploy/deploy.sh
```

The service is deployed `--no-allow-unauthenticated`. Call it with an identity token:

```bash
URL=$(gcloud run services describe turnaround-agent --region us-central1 --format 'value(status.url)')
TOKEN=$(gcloud auth print-identity-token)

curl -s -H "Authorization: Bearer $TOKEN" "$URL/healthz" | jq
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"question":"why is SEQ0420 slipping and what is it costing?"}' \
  "$URL/ask" | jq
```

`/ask` returns `{answer, timeline, evaluation, response_id, circuit_breaker_tripped, grafana_url}`.
Take `response_id` into Tempo (`{ span.gen_ai.response.id = "<id>" }`) to see the
trace, or into Loki (`{service_name="turnaround-agent"} | json | response_id="<id>"`)
for that run's evaluation events.

## Notes

- **Writes are disabled on the endpoint.** `serve.py` uses `AutoApprover(approve=False)`;
  a public URL must not be able to mutate Kitsu or Grafana. The gated write-back
  path is CLI-only (`uv run python -m agent.run --approve "..."`).
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
