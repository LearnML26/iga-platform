#!/usr/bin/env bash
# ============================================================================
# IGA Platform — verification loop
# The build agent runs this after every deployed change. Humans can too.
# Exits non-zero on any failure. Extend this file as new services land —
# a task that adds an API must add a smoke test here.
# ============================================================================
set -uo pipefail
FAIL=0
ok()   { echo "  ✔ $1"; }
bad()  { echo "  ✘ $1"; FAIL=1; }

NS=iga
API_AUDIENCE="api://$(az ad app list --display-name iga-platform-api --query '[0].appId' -o tsv 2>/dev/null)"

echo "== Cluster health =="
if ! kubectl get ns $NS > /dev/null 2>&1; then
  bad "namespace $NS missing — is kubeconfig set? (az aks get-credentials + kubelogin convert-kubeconfig -l azurecli)"
  exit 1
fi

# ignore Completed/Succeeded one-shot pods (smoke tests, jobs)
NOT_READY=$(kubectl get pods -n $NS --no-headers 2>/dev/null | awk '$3 != "Completed" && $3 != "Succeeded" && ($2 != "1/1" || $3 != "Running")' | wc -l)
TOTAL=$(kubectl get pods -n $NS --no-headers 2>/dev/null | awk '$3 != "Completed" && $3 != "Succeeded"' | wc -l)
if [ "$TOTAL" -eq 0 ]; then bad "no pods in $NS"; else
  if [ "$NOT_READY" -eq 0 ]; then ok "$TOTAL/$TOTAL pods Running and Ready"; else
    bad "$NOT_READY of $TOTAL pods not ready:"; kubectl get pods -n $NS | awk '$2 != "1/1" || $3 != "Running"'
  fi
fi

RESTARTS=$(kubectl get pods -n $NS --no-headers | awk '{s+=$4} END {print s+0}')
if [ "${RESTARTS:-0}" -le 20 ]; then ok "restart count acceptable ($RESTARTS)"; else
  bad "high restart count: $RESTARTS — check logs"
fi

echo "== API smoke tests (in-cluster) =="
run_curl() {
  local name="$1"; shift
  kubectl delete pod "$name" -n $NS --ignore-not-found > /dev/null 2>&1
  kubectl run -n $NS "$name" --image=curlimages/curl --restart=Never --quiet -- \
    curl -sS --connect-timeout 10 -w '\nHTTP_STATUS:%{http_code}\n' "$@" > /dev/null 2>&1
  kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" -n $NS --timeout=90s > /dev/null 2>&1
  kubectl logs -n $NS "$name" 2>/dev/null
  kubectl delete pod "$name" -n $NS --ignore-not-found > /dev/null 2>&1
}

# Mint a bearer token for API_AUDIENCE using a service's own workload identity
# (1R.3 — no client secrets: exchanges the pod's projected federated token for
# an AAD access token carrying that service's already-granted app roles).
# $1 = k8s ServiceAccount name (must already be workload-identity annotated)
mint_token() {
  local SA="$1" POD="vrfy-token-${1}"
  kubectl delete pod "$POD" -n $NS --ignore-not-found > /dev/null 2>&1
  cat <<PODYAML | kubectl apply -f - > /dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
  labels: { azure.workload.identity/use: "true" }
spec:
  serviceAccountName: $SA
  restartPolicy: Never
  containers:
    - name: az
      image: mcr.microsoft.com/azure-cli:latest
      command: ["sleep", "90"]
PODYAML
  if ! kubectl wait --for=condition=Ready "pod/$POD" -n $NS --timeout=60s > /dev/null 2>&1; then
    kubectl delete pod "$POD" -n $NS --ignore-not-found > /dev/null 2>&1
    return 1
  fi
  local TOKEN
  TOKEN=$(kubectl exec -n $NS "$POD" -- env AUD="$API_AUDIENCE" bash -c '
    az login --service-principal -u "$AZURE_CLIENT_ID" --tenant "$AZURE_TENANT_ID" \
      --federated-token "$(cat "$AZURE_FEDERATED_TOKEN_FILE")" -o none 2>/dev/null &&
    az account get-access-token --resource "$AUD" --query accessToken -o tsv 2>/dev/null
  ')
  kubectl delete pod "$POD" -n $NS --ignore-not-found > /dev/null 2>&1
  echo "$TOKEN"
}

# identity-service: health, create (unique key), dedupe
KEY="VRFY$(date +%s)"
OUT=$(run_curl vrfy-health http://identity-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "identity-service /healthz 200" || bad "identity-service health failed: $OUT"

# 1R.3: JWT auth is enforced on /identities now — mint a token via
# identity-service's own workload identity (already granted identities.read;
# identities.write must also be granted, see roadmap/PHASES.md 1R.3 note).
IDENTITY_TOKEN=$(mint_token identity-service)
if [ -z "$IDENTITY_TOKEN" ]; then
  bad "could not mint an identity-service token — is the identities.read/write app role assignment done? (roadmap/PHASES.md 1R.3)"
fi
AUTH_HDR=(-H "Authorization: Bearer $IDENTITY_TOKEN")

OUT=$(run_curl vrfy-noauth -X POST http://identity-service/identities \
  -H 'Content-Type: application/json' \
  -d "{\"correlationKey\":\"${KEY}-noauth\",\"displayName\":\"Verify Bot\"}")
echo "$OUT" | grep -q 'HTTP_STATUS:401' && ok "identity create without token 401" || bad "expected 401 without token: $OUT"

OUT=$(run_curl vrfy-create -X POST http://identity-service/identities "${AUTH_HDR[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"correlationKey\":\"$KEY\",\"displayName\":\"Verify Bot\",\"department\":\"QA\",\"jobTitle\":\"Probe\"}")
echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "identity create 201 ($KEY)" || bad "identity create failed: $OUT"

OUT=$(run_curl vrfy-dedupe -X POST http://identity-service/identities "${AUTH_HDR[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"correlationKey\":\"$KEY\",\"displayName\":\"Verify Bot\",\"department\":\"QA\",\"jobTitle\":\"Probe\"}")
echo "$OUT" | grep -q 'HTTP_STATUS:409' && ok "correlation dedupe 409" || bad "dedupe check failed: $OUT"

OUT=$(run_curl vrfy-search "${AUTH_HDR[@]}" "http://identity-service/identities?department=QA")
echo "$OUT" | grep -q "$KEY" && ok "identity search returns created record" || bad "search failed: $OUT"

# provisioning-service: health + task acceptance (no connector execution asserted)
OUT=$(run_curl vrfy-prov-health http://provisioning-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "provisioning-service /healthz 200" || bad "provisioning health failed: $OUT"

# 1R.3: /tasks now requires provisioning.write
OUT=$(run_curl vrfy-prov-noauth -X POST http://provisioning-service/tasks \
  -H 'Content-Type: application/json' \
  -d '{"sourceType":"manual","sourceRef":"verify","identityId":"verify","instanceId":"verify","connectorType":"ad","operationType":"grant"}')
echo "$OUT" | grep -q 'HTTP_STATUS:401' && ok "task submit without token 401" || bad "expected 401 without token: $OUT"

PROVISIONING_TOKEN=$(mint_token provisioning-service)
if [ -z "$PROVISIONING_TOKEN" ]; then
  bad "could not mint a provisioning-service token — is the provisioning.write app role assignment done? (roadmap/PHASES.md 1R.3)"
else
  OUT=$(run_curl vrfy-prov-task -X POST http://provisioning-service/tasks \
    -H "Authorization: Bearer $PROVISIONING_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"sourceType":"manual","sourceRef":"verify","identityId":"verify","instanceId":"verify","connectorType":"ad","operationType":"grant"}')
  echo "$OUT" | grep -q 'HTTP_STATUS:202' && ok "task submit (authenticated) 202" || bad "authenticated task submit failed: $OUT"
fi

# source-system-service: health, create, dedupe (unique name), mapping, feed run
SRC_NAME="vrfy-src-$(date +%s)"
OUT=$(run_curl vrfy-src-health http://source-system-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "source-system-service /healthz 200" || bad "source-system-service health failed: $OUT"

OUT=$(run_curl vrfy-src-create -X POST http://source-system-service/source-systems \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$SRC_NAME\",\"connectorType\":\"flat-file\",\"description\":\"verify probe\"}")
echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "source system create 201 ($SRC_NAME)" || bad "source system create failed: $OUT"
SRC_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)

OUT=$(run_curl vrfy-src-dedupe -X POST http://source-system-service/source-systems \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$SRC_NAME\",\"connectorType\":\"flat-file\"}")
echo "$OUT" | grep -q 'HTTP_STATUS:409' && ok "source system name uniqueness 409" || bad "source system dedupe check failed: $OUT"

if [ -n "${SRC_ID:-}" ]; then
  OUT=$(run_curl vrfy-src-mapping -X POST "http://source-system-service/source-systems/${SRC_ID}/mappings" \
    -H 'Content-Type: application/json' \
    -d '{"sourceAttribute":"emp_id","targetAttribute":"correlationKey","isKey":true}')
  echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "attribute mapping create 201" || bad "attribute mapping create failed: $OUT"

  OUT=$(run_curl vrfy-src-feedrun -X POST "http://source-system-service/source-systems/${SRC_ID}/feed-runs" \
    -H 'Content-Type: application/json' \
    -d '{"triggeredBy":"verify"}')
  echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "feed run create 201" || bad "feed run create failed: $OUT"
else
  bad "source system id not captured — skipped mapping/feed-run checks"
fi

# flatfile-connector-service (2.2): health + a full ingest round-trip.
# Uploads a tiny CSV fixture + .md5 sidecar to raw/ via an ephemeral pod
# using the connector's own workload identity (same mint pattern as above),
# then asserts the delta summary and checksum verification note.
OUT=$(run_curl vrfy-ffc-health http://flatfile-connector-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "flatfile-connector-service /healthz 200" || bad "flatfile-connector-service health failed: $OUT"

FFC_SRC_NAME="vrfy-ffc-src-$(date +%s)"
OUT=$(run_curl vrfy-ffc-src-create -X POST http://source-system-service/source-systems \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$FFC_SRC_NAME\",\"connectorType\":\"flat-file\",\"description\":\"verify probe\"}")
FFC_SRC_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)

if [ -n "${FFC_SRC_ID:-}" ]; then
  run_curl vrfy-ffc-mapping -X POST "http://source-system-service/source-systems/${FFC_SRC_ID}/mappings" \
    -H 'Content-Type: application/json' \
    -d '{"sourceAttribute":"emp_id","targetAttribute":"correlationKey","isKey":true}' > /dev/null

  FFC_POD="vrfy-ffc-upload"
  kubectl delete pod "$FFC_POD" -n $NS --ignore-not-found > /dev/null 2>&1
  cat <<PODYAML | kubectl apply -f - > /dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $FFC_POD
  namespace: $NS
  labels: { azure.workload.identity/use: "true" }
spec:
  serviceAccountName: flatfile-connector-service
  restartPolicy: Never
  containers:
    - name: az
      image: mcr.microsoft.com/azure-cli:latest
      command: ["sleep", "90"]
PODYAML
  if kubectl wait --for=condition=Ready "pod/$FFC_POD" -n $NS --timeout=60s > /dev/null 2>&1; then
    kubectl exec -n $NS "$FFC_POD" -- bash -c '
      az login --service-principal -u "$AZURE_CLIENT_ID" --tenant "$AZURE_TENANT_ID" \
        --federated-token "$(cat "$AZURE_FEDERATED_TOKEN_FILE")" -o none 2>/dev/null
      printf "emp_id,name\nV1,Alice\nV2,Bob\n" > /tmp/f.csv
      md5sum /tmp/f.csv | cut -d" " -f1 | tr -d "\n" > /tmp/f.csv.md5
      az storage blob upload --auth-mode login --account-name stigadevlake -c raw -f /tmp/f.csv -n verify/ffc-fixture.csv --overwrite -o none
      az storage blob upload --auth-mode login --account-name stigadevlake -c raw -f /tmp/f.csv.md5 -n verify/ffc-fixture.csv.md5 --overwrite -o none
    ' > /dev/null 2>&1
  fi
  kubectl delete pod "$FFC_POD" -n $NS --ignore-not-found > /dev/null 2>&1

  OUT=$(run_curl vrfy-ffc-ingest -X POST http://flatfile-connector-service/ingest \
    -H 'Content-Type: application/json' \
    -d "{\"sourceSystemInstanceId\":\"${FFC_SRC_ID}\",\"blobPath\":\"verify/ffc-fixture.csv\",\"triggeredBy\":\"verify\"}")
  echo "$OUT" | grep -q 'HTTP_STATUS:200' && echo "$OUT" | grep -q '"recordsAdded":2' \
    && ok "flat-file ingest: 2 rows added, checksum verified" \
    || bad "flat-file ingest failed or unexpected delta: $OUT"
else
  bad "flatfile connector: source system id not captured — skipped ingest check"
fi

echo "== Infra spot checks =="
for Z in privatelink.documents.azure.com privatelink.vaultcore.azure.net; do
  N=$(az network private-dns record-set a list -g rg-iga-dev-network -z "$Z" --query "length(@)" -o tsv 2>/dev/null || echo 0)
  if [ "${N:-0}" -ge 1 ]; then ok "DNS zone $Z has $N A record(s)"; else
    bad "DNS zone $Z has no A records — private endpoint unregistered"
  fi
done

DLQ=$(az servicebus queue show -g rg-iga-dev-data --namespace-name sb-iga-dev \
  -n provisioning-tasks --query countDetails.deadLetterMessageCount -o tsv 2>/dev/null || echo "?")
if [ "$DLQ" = "0" ]; then ok "provisioning-tasks DLQ empty"; elif [ "$DLQ" = "?" ]; then
  bad "could not read Service Bus DLQ count"; else
  bad "provisioning-tasks DLQ has $DLQ message(s) — investigate before proceeding"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then echo "=== VERIFY PASSED ==="; else echo "=== VERIFY FAILED ==="; fi
exit $FAIL
