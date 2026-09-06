#!/usr/bin/env bash
# Deploy the Turnaround agent to Cloud Run from source (deploy/Dockerfile).
#
#   PROJECT_ID=my-proj REGION=us-central1 ./deploy/deploy.sh
#
# Reads runtime configuration from ./.env (same file the CLI uses). Vertex auth
# is the Cloud Run service account's ADC — this script grants it
# roles/aiplatform.user. Grafana + OTLP credentials are pushed to Secret Manager
# and mounted as env vars, so they are never in the image or the service YAML.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-turnaround-agent}"
RUNTIME_SA="${RUNTIME_SA:-${SERVICE}-run}"
ENV_FILE="${ENV_FILE:-.env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

[[ -f "$ENV_FILE" ]] || { echo "no $ENV_FILE — copy .env.example and fill it in" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

need() { [[ -n "${!1:-}" ]] || { echo "missing $1 in $ENV_FILE" >&2; exit 1; }; }
need GRAFANA_URL
need GRAFANA_SERVICE_ACCOUNT_TOKEN
need OTEL_EXPORTER_OTLP_ENDPOINT
need OTEL_EXPORTER_OTLP_HEADERS
need TURNAROUND_PSEUDONYM_SALT

echo "==> project=$PROJECT_ID region=$REGION service=$SERVICE"
gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  aiplatform.googleapis.com >/dev/null

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

# --- deploy ------------------------------------------------------------
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

echo "==> deploying from source (deploy/Dockerfile)"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "GRAFANA_SERVICE_ACCOUNT_TOKEN=turnaround-grafana-token:latest,OTEL_EXPORTER_OTLP_HEADERS=turnaround-otlp-headers:latest,TURNAROUND_PSEUDONYM_SALT=turnaround-pseudonym-salt:latest" \
  --cpu 2 --memory 2Gi --timeout 300 --concurrency 4 \
  --no-allow-unauthenticated

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
echo
echo "deployed: $URL"
echo "smoke test:"
echo "  TOKEN=\$(gcloud auth print-identity-token)"
echo "  curl -s -H \"Authorization: Bearer \$TOKEN\" $URL/healthz | jq"
echo "  curl -s -H \"Authorization: Bearer \$TOKEN\" -H 'content-type: application/json' \\"
echo "    -d '{\"question\":\"why is SEQ0420 slipping and what is it costing?\"}' $URL/ask | jq"
