#!/usr/bin/env bash
# ============================================================================
# Local dev loop for the web frontend (Phase 3.4).
#
# The platform's services are ClusterIP-only inside the AKS VNet — there is
# deliberately no public ingress until Phase 4.5's APIM. This script
# port-forwards each service to the localhost ports web/vite.config.ts
# proxies to, then starts the Vite dev server. Sign-in (MSAL) goes straight
# to Entra's public endpoints; API calls flow browser → Vite proxy →
# port-forward → service, carrying the user's real delegated token, which
# each service validates exactly as it does for service-to-service calls.
# Full production auth path, zero mocks.
#
# Prereqs: kubectl context on the dev cluster; web/.env.local filled from
# the deploy.sh [HUMAN gate, Phase 3.4] output; npm install ran in web/.
# ============================================================================
set -uo pipefail
NS=iga
declare -A FORWARDS=(
  [identity-service]=8081
  [provisioning-service]=8082
  [source-system-service]=8083
  [rbac-service]=8084
  [access-request-service]=8085
)

PIDS=()
cleanup() {
  echo ""
  echo "Stopping port-forwards…"
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
}
trap cleanup EXIT

for SVC in "${!FORWARDS[@]}"; do
  PORT="${FORWARDS[$SVC]}"
  kubectl port-forward -n "$NS" "svc/${SVC}" "${PORT}:80" > /dev/null 2>&1 &
  PIDS+=($!)
  echo "  ${SVC} → localhost:${PORT}"
done

sleep 2
for pid in "${PIDS[@]}"; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "✘ a port-forward died immediately — is kubectl pointed at the dev cluster?"
    exit 1
  fi
done

echo "Port-forwards up. Starting Vite dev server (http://localhost:5173)…"
cd "$(dirname "$0")/../web" && npm run dev
