#!/usr/bin/env bash
# ============================================================================
# JML end-to-end demo, day 0 (Phase 2.5) — 50-row synthetic HR extract
# through the full pipeline: flatfile-connector → identity-service →
# provisioning-service, including the 2.4 lifecycle behaviors.
#
# What it does (see fixtures/jml-demo/README.md for the narrative):
#   0. Regenerates the fixtures anchored to TODAY (mandatory — all
#      lifecycle behavior is date-relative).
#   1. Creates a fresh jml-demo source system with provisioningTargets
#      ["ad"] and all 8 column mappings.
#   2. Ingests round 1 (50 rows): 45 active + 5 pending-start joiners.
#   3. Runs the lifecycle sweep: the +1/+2/+3-day joiners activate, the
#      +7/+14-day ones must stay pending-start.
#   4. Ingests round 2: 3 transfers + 2 rows gaining a future
#      terminationDate (5 updates), 2 dropped rows (immediate
#      terminations, each dispatching a disable-account task to "ad").
#   5. Prints what remains for the scheduled dates (the daily CronJob
#      handles those) and how to check.
#
# Caveats, by design:
#   - The 2 immediate-leaver disable-account tasks will retry ~2.5h and
#     dead-letter (AD bind creds unwired — known gap). Run
#     scripts/drain-provisioning-dlq.sh afterwards or verify.sh's DLQ
#     check stays red. Same happens again on each scheduled-termination
#     date.
#   - Rerunning the demo needs fresh keys: correlationKey is global with
#     no delete endpoint, so this script aborts if J1001 already exists —
#     bump FIRST_ID/KEY_PREFIX in generate_fixtures.py to rerun.
# ============================================================================
set -uo pipefail
FAIL=0
ok()   { echo "  ✔ $1"; }
bad()  { echo "  ✘ $1"; FAIL=1; }

NS=iga
STORAGE_ACCOUNT=stigadevlake
API_AUDIENCE="api://$(az ad app list --display-name iga-platform-api --query '[0].appId' -o tsv 2>/dev/null)"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXDIR="$REPO_ROOT/fixtures/jml-demo"

run_curl() {
  local name="$1"; shift
  kubectl delete pod "$name" -n $NS --ignore-not-found > /dev/null 2>&1
  kubectl run -n $NS "$name" --image=curlimages/curl --restart=Never --quiet -- \
    curl -sS --connect-timeout 10 -w '\nHTTP_STATUS:%{http_code}\n' "$@" > /dev/null 2>&1
  kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" -n $NS --timeout=90s > /dev/null 2>&1
  kubectl logs -n $NS "$name" 2>/dev/null
  kubectl delete pod "$name" -n $NS --ignore-not-found > /dev/null 2>&1
}

mint_token() {
  local SA="$1" POD="jmldemo-token-${1}"
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

# get_status <lowercase-pod-suffix> <correlationKey> -> echoes full response
get_ident() {
  run_curl "jmldemo-get-$1" "${AUTH_HDR[@]}" "http://identity-service/identities/by-correlation-key/$2"
}

echo "== 0. Regenerate fixtures (anchored to today) =="
python3 "$FIXDIR/generate_fixtures.py" || { bad "fixture regeneration failed"; exit 1; }
TERM_J1020=$(awk -F, '$1=="J1020"{print $8}' "$FIXDIR/round2_transfers_terminations.csv")
TERM_J1033=$(awk -F, '$1=="J1033"{print $8}' "$FIXDIR/round2_transfers_terminations.csv")
ok "fixtures regenerated (scheduled terminations: J1020 -> $TERM_J1020, J1033 -> $TERM_J1033)"

echo "== Setup: token + rerun guard =="
IDENTITY_TOKEN=$(mint_token identity-service)
[ -z "$IDENTITY_TOKEN" ] && { bad "could not mint identity-service token"; exit 1; }
AUTH_HDR=(-H "Authorization: Bearer $IDENTITY_TOKEN")
ok "minted identity token"

OUT=$(get_ident guard J1001)
if echo "$OUT" | grep -q 'HTTP_STATUS:200'; then
  bad "J1001 already exists — this demo was run before. correlationKey is global with no delete"
  echo "    endpoint, so rerunning would report updates instead of adds. Bump FIRST_ID or"
  echo "    KEY_PREFIX in fixtures/jml-demo/generate_fixtures.py and rerun."
  exit 1
fi
ok "key space clear (J1001 not present)"

echo "== 1. Source system + mappings =="
TS=$(date +%s)
SRC_NAME="jml-demo-${TS}"
OUT=$(run_curl jmldemo-src -X POST http://source-system-service/source-systems \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$SRC_NAME\",\"connectorType\":\"flat-file\",\"provisioningTargets\":[\"ad\"]}")
SRC_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -z "${SRC_ID:-}" ] && { bad "source system create failed: $OUT"; exit 1; }
ok "source system $SRC_NAME created ($SRC_ID), provisioningTargets=[\"ad\"]"

i=0
for M in \
  '{"sourceAttribute":"EmployeeID","targetAttribute":"employeeId","isKey":true}' \
  '{"sourceAttribute":"FirstName","targetAttribute":"givenName","isKey":false}' \
  '{"sourceAttribute":"LastName","targetAttribute":"familyName","isKey":false}' \
  '{"sourceAttribute":"DisplayName","targetAttribute":"displayName","isKey":false}' \
  '{"sourceAttribute":"Department","targetAttribute":"department","isKey":false}' \
  '{"sourceAttribute":"JobTitle","targetAttribute":"jobTitle","isKey":false}' \
  '{"sourceAttribute":"StartDate","targetAttribute":"startDate","isKey":false}' \
  '{"sourceAttribute":"TerminationDate","targetAttribute":"terminationDate","isKey":false}'; do
  i=$((i+1))
  OUT=$(run_curl "jmldemo-map-$i" -X POST "http://source-system-service/source-systems/${SRC_ID}/mappings" \
    -H 'Content-Type: application/json' -d "$M")
  echo "$OUT" | grep -q 'HTTP_STATUS:201' || bad "mapping $i failed: $OUT"
done
ok "8 mappings created"

echo "== 2. Upload fixtures =="
UP_POD=jmldemo-upload
kubectl delete pod "$UP_POD" -n $NS --ignore-not-found > /dev/null 2>&1
cat <<PODYAML | kubectl apply -f - > /dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $UP_POD
  namespace: $NS
  labels: { azure.workload.identity/use: "true" }
spec:
  serviceAccountName: flatfile-connector-service
  restartPolicy: Never
  containers:
    - name: az
      image: mcr.microsoft.com/azure-cli:latest
      command: ["sleep", "300"]
PODYAML
kubectl wait --for=condition=Ready "pod/$UP_POD" -n $NS --timeout=60s > /dev/null 2>&1 \
  || { bad "upload pod never became ready"; exit 1; }
kubectl exec -i -n $NS "$UP_POD" -- sh -c 'cat > /tmp/r1.csv' < "$FIXDIR/round1_baseline.csv"
kubectl exec -i -n $NS "$UP_POD" -- sh -c 'cat > /tmp/r2.csv' < "$FIXDIR/round2_transfers_terminations.csv"
kubectl exec -n $NS "$UP_POD" -- env TS="$TS" bash -c '
  az login --service-principal -u "$AZURE_CLIENT_ID" --tenant "$AZURE_TENANT_ID" \
    --federated-token "$(cat "$AZURE_FEDERATED_TOKEN_FILE")" -o none 2>/dev/null
  for F in r1 r2; do
    md5sum /tmp/$F.csv | cut -d" " -f1 | tr -d "\n" > /tmp/$F.csv.md5
    az storage blob upload --auth-mode login --account-name '"$STORAGE_ACCOUNT"' -c raw -f /tmp/$F.csv -n "jml-demo/$TS-$F.csv" --overwrite -o none
    az storage blob upload --auth-mode login --account-name '"$STORAGE_ACCOUNT"' -c raw -f /tmp/$F.csv.md5 -n "jml-demo/$TS-$F.csv.md5" --overwrite -o none
  done
' > /dev/null 2>&1
kubectl delete pod "$UP_POD" -n $NS --ignore-not-found > /dev/null 2>&1
ok "both rounds uploaded to raw/jml-demo/"

echo "== 3. Round 1: 50-row baseline =="
OUT=$(run_curl jmldemo-ingest-r1 -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"jml-demo/${TS}-r1.csv\",\"triggeredBy\":\"jml-demo-r1\"}")
echo "    $OUT"
echo "$OUT" | grep -q '"recordsAdded":50' && ok "round 1: 50 identities created" \
  || bad "round 1 unexpected deltas: $OUT"

OUT=$(get_ident j1001 J1001)
echo "$OUT" | grep -q '"status":"active"' && ok "J1001 (established) is active" || bad "J1001 not active: $OUT"
for K in J1046 J1050; do
  OUT=$(get_ident "$(echo $K | tr '[:upper:]' '[:lower:]')" "$K")
  echo "$OUT" | grep -q '"status":"pending-start"' && ok "$K (future joiner) is pending-start" \
    || bad "$K not pending-start: $OUT"
done

echo "== 4. Lifecycle sweep: pre-start window =="
OUT=$(run_curl jmldemo-sweep -X POST http://flatfile-connector-service/lifecycle/sweep)
echo "    $OUT"
echo "$OUT" | grep -q '"halted":false' && ok "sweep ran clean" || bad "sweep failed: $OUT"
for K in J1046 J1047 J1048; do
  OUT=$(get_ident "$(echo $K | tr '[:upper:]' '[:lower:]')-b" "$K")
  echo "$OUT" | grep -q '"status":"active"' && ok "$K activated (inside 3-day window)" \
    || bad "$K not activated: $OUT"
done
for K in J1049 J1050; do
  OUT=$(get_ident "$(echo $K | tr '[:upper:]' '[:lower:]')-b" "$K")
  echo "$OUT" | grep -q '"status":"pending-start"' && ok "$K still pending-start (+7/+14d, outside window)" \
    || bad "$K unexpectedly changed: $OUT"
done

echo "== 5. Round 2: transfers, leavers, scheduled terminations =="
OUT=$(run_curl jmldemo-ingest-r2 -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"jml-demo/${TS}-r2.csv\",\"triggeredBy\":\"jml-demo-r2\"}")
echo "    $OUT"
if echo "$OUT" | grep -q '"recordsUpdated":5' && echo "$OUT" | grep -q '"recordsTerminated":2' \
    && echo "$OUT" | grep -q '0 apply failure'; then
  ok "round 2: 5 updated (3 transfers + 2 gained terminationDate), 2 immediate terminations, all dispatches accepted"
else
  bad "round 2 unexpected deltas: $OUT"
fi

for K in J1005 J1012; do
  OUT=$(get_ident "$(echo $K | tr '[:upper:]' '[:lower:]')" "$K")
  echo "$OUT" | grep -q '"status":"terminated"' && ok "$K (dropped row) terminated immediately" \
    || bad "$K not terminated: $OUT"
done
for K in J1020 J1033; do
  OUT=$(get_ident "$(echo $K | tr '[:upper:]' '[:lower:]')" "$K")
  if echo "$OUT" | grep -q '"status":"active"' && echo "$OUT" | grep -q '"terminationDate":"20'; then
    ok "$K still active with future terminationDate set (scheduled, not immediate)"
  else
    bad "$K wrong state for a scheduled termination: $OUT"
  fi
done
OUT=$(get_ident j1003 J1003)
echo "$OUT" | grep -q '"status":"active"' && ok "J1003 (transfer) still active with updated attributes" \
  || bad "J1003 wrong state: $OUT"

echo ""
echo "== Day-0 demo complete — what happens next, on its own =="
echo "  - The lifecycle-sweep CronJob (daily 06:00 UTC) will terminate:"
echo "      J1020 on $TERM_J1020, J1033 on $TERM_J1033"
echo "    each dispatching a disable-account task to 'ad'. Check afterwards with:"
echo "      kubectl run check --rm -i --image=curlimages/curl --restart=Never -n iga -- \\"
echo "        curl -sS -H \"Authorization: Bearer \$TOKEN\" http://identity-service/identities/by-correlation-key/J1020"
echo "    (or re-run the sweep manually on/after those dates: POST /lifecycle/sweep)"
echo "  - The 2 disable-account tasks just dispatched for J1005/J1012 will retry ~2.5h"
echo "    then dead-letter (AD creds unwired — known gap). Run scripts/drain-provisioning-dlq.sh"
echo "    afterwards so verify.sh's DLQ check stays green; same after each scheduled date."
echo ""
if [ "$FAIL" -eq 0 ]; then echo "=== JML DEMO DAY-0 PASSED ==="; else echo "=== JML DEMO DAY-0 FAILED ==="; fi
exit $FAIL
