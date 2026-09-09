#!/usr/bin/env bash
# Deploy Turnaround to Cloud Run: the agent service + the re-seed job that keeps
# the demo data alive.
#
#   ./deploy/deploy.sh                       # project from GOOGLE_CLOUD_PROJECT in .env
#   PROJECT_ID=other-proj ./deploy/deploy.sh # or override it
#
# Reads runtime configuration from ./.env (same file the CLI uses). Vertex auth
# is the Cloud Run service account's ADC — this script grants it
# roles/aiplatform.user. Grafana + OTLP credentials are pushed to Secret Manager
# and mounted as env vars, so they are never in the image or the service YAML.
#
# The image is built ONCE from ./Dockerfile (repo root — see the note in that
# file) and reused by both the service and the job, so they can never drift.
set -euo pipefail

REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-turnaround-agent}"
SEED_JOB="${SEED_JOB:-turnaround-seed}"
SEED_EVERY_MIN="${SEED_EVERY_MIN:-15}"
RUNTIME_SA="${RUNTIME_SA:-${SERVICE}-run}"
AR_REPO="${AR_REPO:-turnaround}"
ENV_FILE="${ENV_FILE:-.env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

[[ -f Dockerfile ]] || { echo "no ./Dockerfile at the repo root — the build would silently fall back to buildpacks" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "no $ENV_FILE — copy .env.example and fill it in" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

need() { [[ -n "${!1:-}" ]] || { echo "missing $1 in $ENV_FILE" >&2; exit 1; }; }
need GRAFANA_URL
need GRAFANA_SERVICE_ACCOUNT_TOKEN
need OTEL_EXPORTER_OTLP_ENDPOINT
need OTEL_EXPORTER_OTLP_HEADERS
need TURNAROUND_PSEUDONYM_SALT

# Resolved after `.env` is sourced, not before: the project id is already in
# there as GOOGLE_CLOUD_PROJECT, and demanding a second name for the same value
# is a trip hazard for anyone whose .env is complete. An explicit PROJECT_ID
# still wins, for deploying to a project other than the one the CLI talks to.
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
[[ -n "$PROJECT_ID" ]] || {
  echo "set PROJECT_ID, or GOOGLE_CLOUD_PROJECT in $ENV_FILE" >&2; exit 1; }

echo "==> project=$PROJECT_ID region=$REGION service=$SERVICE job=$SEED_JOB"
gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  aiplatform.googleapis.com cloudscheduler.googleapis.com >/dev/null

SA_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  echo "==> creating runtime service account $SA_EMAIL"
  gcloud iam service-accounts create "$RUNTIME_SA" \
    --display-name "Turnaround Cloud Run runtime"
fi
echo "==> granting Vertex AI user to $SA_EMAIL"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/aiplatform.user --condition=None >/dev/null

# --- secrets -------------------------------------------------------------
put_secret() {
  local name="$1" value="$2"
  if ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=automatic >/dev/null
  fi
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  gcloud secrets add-iam-policy-binding "$name" \
    --member "serviceAccount:${SA_EMAIL}" \
    --role roles/secretmanager.secretAccessor --condition=None >/dev/null
}
echo "==> syncing secrets to Secret Manager"
put_secret turnaround-grafana-token     "$GRAFANA_SERVICE_ACCOUNT_TOKEN"
put_secret turnaround-otlp-headers      "$OTEL_EXPORTER_OTLP_HEADERS"
put_secret turnaround-pseudonym-salt    "$TURNAROUND_PSEUDONYM_SALT"

SECRETS="GRAFANA_SERVICE_ACCOUNT_TOKEN=turnaround-grafana-token:latest"
SECRETS+=",OTEL_EXPORTER_OTLP_HEADERS=turnaround-otlp-headers:latest"
SECRETS+=",TURNAROUND_PSEUDONYM_SALT=turnaround-pseudonym-salt:latest"

ENV_VARS="GRAFANA_URL=${GRAFANA_URL}"
ENV_VARS+=",OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT}"
ENV_VARS+=",OTEL_EXPORTER_OTLP_PROTOCOL=${OTEL_EXPORTER_OTLP_PROTOCOL:-http/protobuf}"
ENV_VARS+=",GRAFANA_DS_PROM_UID=${GRAFANA_DS_PROM_UID:-grafanacloud-prom}"
ENV_VARS+=",GRAFANA_DS_LOKI_UID=${GRAFANA_DS_LOKI_UID:-grafanacloud-logs}"
ENV_VARS+=",GRAFANA_DS_TEMPO_UID=${GRAFANA_DS_TEMPO_UID:-grafanacloud-traces}"
ENV_VARS+=",TURNAROUND_GEMINI_MODEL=${TURNAROUND_GEMINI_MODEL:-gemini-2.5-flash}"
ENV_VARS+=",TURNAROUND_MAX_LLM_CALLS=${TURNAROUND_MAX_LLM_CALLS:-40}"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
ENV_VARS+=",GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION:-${REGION}}"
ENV_VARS+=",GOOGLE_GENAI_USE_VERTEXAI=TRUE"

# --- build once ----------------------------------------------------------
if ! gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1; then
  echo "==> creating Artifact Registry repo $AR_REPO"
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location "$REGION" \
    --description "Turnaround images" >/dev/null
fi
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/turnaround:${TAG}"
if [[ -n "${SKIP_BUILD:-}" ]] && gcloud artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "==> SKIP_BUILD set and $IMAGE already exists — reusing it"
else
  echo "==> building $IMAGE from ./Dockerfile"
  gcloud builds submit --tag "$IMAGE" .
fi

# --- the agent service ---------------------------------------------------
# Public on purpose: the hosted URL has to be openable by a judge. Writes are
# impossible here regardless -- serve.py runs AutoApprover(approve=False), and
# a test enforces it. --max-instances caps the blast radius of an open endpoint;
# TURNAROUND_MAX_LLM_CALLS caps the spend of any single request.
echo "==> deploying service $SERVICE"
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRETS" \
  --cpu 2 --memory 2Gi --timeout 300 --concurrency 4 \
  --min-instances 0 --max-instances "${MAX_INSTANCES:-2}" \
  --allow-unauthenticated

# --- the re-seed job -----------------------------------------------------
# The show is compressed into a ~45-minute window ending at the moment of
# seeding, and hosted Mimir rejects samples much older than an hour. Without
# this the hosted demo answers "nothing" a couple of hours after the last seed,
# which reads as a broken agent rather than stale data. Same image, entrypoint
# overridden (see seed/refresh.py).
echo "==> deploying re-seed job $SEED_JOB"
gcloud run jobs deploy "$SEED_JOB" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRETS" \
  --command=python --args="-m,seed.refresh,--once" \
  --cpu 1 --memory 1Gi --max-retries 1 --task-timeout 15m

echo "==> granting run.invoker on $SEED_JOB to $SA_EMAIL"
# NB: the `run jobs` variant of add-iam-policy-binding does not accept
# --condition, unlike the `projects` one used above. Passing it fails the deploy.
gcloud run jobs add-iam-policy-binding "$SEED_JOB" --region "$REGION" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/run.invoker >/dev/null

SCHED="${SEED_JOB}-every-${SEED_EVERY_MIN}m"
RUN_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${SEED_JOB}:run"
if gcloud scheduler jobs describe "$SCHED" --location "$REGION" >/dev/null 2>&1; then
  echo "==> updating schedule $SCHED"
  VERB=update
else
  echo "==> creating schedule $SCHED (every ${SEED_EVERY_MIN}m)"
  VERB=create
fi
gcloud scheduler jobs "$VERB" http "$SCHED" \
  --location "$REGION" \
  --schedule "*/${SEED_EVERY_MIN} * * * *" \
  --uri "$RUN_URI" \
  --http-method POST \
  --oauth-service-account-email "$SA_EMAIL" >/dev/null
if [[ -n "${SEED_PAUSED:-}" ]]; then
  gcloud scheduler jobs pause "$SCHED" --location "$REGION" >/dev/null
  echo "   (schedule created paused — resume with: gcloud scheduler jobs resume $SCHED --location $REGION)"
fi

echo "==> priming the data once so the demo is live immediately"
gcloud run jobs execute "$SEED_JOB" --region "$REGION" --wait || \
  echo "   (first seed failed — check: gcloud run jobs executions list --job $SEED_JOB --region $REGION)"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
echo
echo "deployed: $URL"
echo "re-seeding every ${SEED_EVERY_MIN} minutes via Cloud Scheduler job '$SCHED'"
echo "smoke test:"
# /health, not /healthz: GFE intercepts the exact path /healthz on *.run.app.
echo "  curl -s $URL/health | jq"
echo "  curl -s -H 'content-type: application/json' \\"
echo "    -d '{\"question\":\"why is SEQ0420 slipping and what is it costing?\"}' $URL/ask | jq"
