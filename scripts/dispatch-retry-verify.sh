#!/usr/bin/env bash
# ============================================================================
# Live verification: pendingProvisioningDispatch fix (bca2b2a, merged to main)
#
# Uses a real infra failure (scaling provisioning-service to 0 replicas) to
# force a genuine POST /tasks dispatch failure for both targets — bogus
# connectorTypes don't work for this: submit_task never validates
# connectorType against CONNECTOR_REGISTRY, only the async worker does, so
# a fake connector type always gets a 202 at dispatch time regardless.
#
# Also empirically confirms a documented (not fixed) limitation: retrying
# pendingProvisioningDispatch entries does NOT consult the source
# instance's *current* provisioningTargets — a target removed from config
# is still retried until it succeeds, exercised below via the mid-test
# PATCH in step 6.
#
# Uses a fresh, run-unique source system name AND correlationKey every
# time (correlationKey is global per tenant and identity-service has no
# DELETE endpoint — reusing a fixed employee ID across runs silently
# picks up leftover state from a prior attempt and produces confusing,
# contaminated results).
# ============================================================================
set -uo pipefail
FAIL=0
ok()   { echo "  ✔ $1"; }
bad()  { echo "  ✘ $1"; FAIL=1; }

NS=iga
STORAGE_ACCOUNT=stigadevlake
RUN_ID=$(date +%s)
SRC_NAME="dispatch-retry-verify-${RUN_ID}"
EMP_ID="E${RUN_ID}"
echo "Run ID: $RUN_ID (source system: $SRC_NAME, employee: $EMP_ID)"

run_curl() {
  local name="$1"; shift
  kubectl delete pod "$name" -n $NS --ignore-not-found > /dev/null 2>&1
  kubectl run -n $NS "$name" --image=curlimages/curl --restart=Never --quiet -- \
    curl -sS --connect-timeout 10 -w '\nHTTP_STATUS:%{http_code}\n' "$@" > /dev/null 2>&1
  kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" -n $NS --timeout=90s > /dev/null 2>&1
  kubectl logs -n $NS "$name" 2>/dev/null
  kubectl delete pod "$name" -n $NS --ignore-not-found > /dev/null 2>&1
}

echo "== Setup: upload pod (flatfile-connector-service identity) =="
UP_POD=drv-upload
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
      command: ["sleep", "600"]
PODYAML
if ! kubectl wait --for=condition=Ready "pod/$UP_POD" -n $NS --timeout=60s > /dev/null 2>&1; then
  bad "upload pod never became ready"; exit 1
fi
kubectl exec -n $NS "$UP_POD" -- bash -c '
  az login --service-principal -u "$AZURE_CLIENT_ID" --tenant "$AZURE_TENANT_ID" \
    --federated-token "$(cat "$AZURE_FEDERATED_TOKEN_FILE")" -o none 2>/dev/null
' > /dev/null 2>&1
ok "upload pod ready and logged in"

upload_csv() {
  local CONTENT="$1" BLOB="$2"
  kubectl exec -n $NS "$UP_POD" -- bash -c "cat > /tmp/f.csv <<'CSV'
$CONTENT
CSV
md5sum /tmp/f.csv | cut -d' ' -f1 | tr -d '\n' > /tmp/f.csv.md5
az storage blob upload --auth-mode login --account-name $STORAGE_ACCOUNT -c raw -f /tmp/f.csv -n '$BLOB' --overwrite -o none
az storage blob upload --auth-mode login --account-name $STORAGE_ACCOUNT -c raw -f /tmp/f.csv.md5 -n '$BLOB.md5' --overwrite -o none
" > /dev/null 2>&1
}

dump_state() {
  kubectl exec -n $NS "$UP_POD" -- bash -c "
az storage blob download --auth-mode login --account-name $STORAGE_ACCOUNT -c curated \
  -n 'source-state/${SRC_ID}/latest.json' -f /tmp/state.json --overwrite -o none 2>/dev/null && cat /tmp/state.json
" 2>/dev/null
}

# ============================================================================
echo ""
echo "== 1. Fresh source system, two real provisioning targets =="
OUT=$(run_curl drv-src-create -X POST http://source-system-service/source-systems \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"${SRC_NAME}\",\"connectorType\":\"flat-file\",\"provisioningTargets\":[\"ad\",\"entra\"]}")
if echo "$OUT" | grep -q 'HTTP_STATUS:201'; then ok "source system '$SRC_NAME' created"; else
  bad "source system create failed: $OUT"; exit 1
fi
SRC_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "    instance id: $SRC_ID"

run_curl drv-mapping -X POST "http://source-system-service/source-systems/${SRC_ID}/mappings" \
  -H 'Content-Type: application/json' \
  -d '{"sourceAttribute":"EmployeeID","targetAttribute":"employeeId","isKey":true}' > /dev/null
run_curl drv-mapping-2 -X POST "http://source-system-service/source-systems/${SRC_ID}/mappings" \
  -H 'Content-Type: application/json' \
  -d '{"sourceAttribute":"DisplayName","targetAttribute":"displayName","isKey":false}' > /dev/null

echo ""
echo "== 2. Create $EMP_ID =="
upload_csv "EmployeeID,DisplayName
${EMP_ID},Retry Test User" "${SRC_NAME}/a.csv"
OUT=$(run_curl drv-ingest-a -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"${SRC_NAME}/a.csv\",\"triggeredBy\":\"drv-a\"}")
echo "    ingest response: $OUT"
echo "$OUT" | grep -q '"recordsAdded":1' && ok "$EMP_ID created" || bad "$EMP_ID create failed: $OUT"

echo ""
echo "== 3. Break provisioning-service, terminate $EMP_ID (both dispatches should fail) =="
kubectl scale deployment/provisioning-service -n $NS --replicas=0 > /dev/null
kubectl wait --for=delete pod -l app=provisioning-service -n $NS --timeout=60s > /dev/null 2>&1
ok "provisioning-service scaled to 0"

upload_csv "EmployeeID,DisplayName
" "${SRC_NAME}/b.csv"   # $EMP_ID absent -> terminated
OUT=$(run_curl drv-ingest-b -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"${SRC_NAME}/b.csv\",\"triggeredBy\":\"drv-b\"}")
echo "    ingest response: $OUT"
echo "$OUT" | grep -q '"recordsTerminated":1' && ok "$EMP_ID terminated (identity PATCH unaffected by provisioning-service outage)" \
  || bad "expected recordsTerminated:1: $OUT"
echo "$OUT" | grep -q '2 apply failure' && ok "both dispatch attempts counted as apply failures" \
  || echo "    (check errorSummary above manually)"

echo ""
echo "== 4. Confirm both targets landed in pendingProvisioningDispatch =="
STATE=$(dump_state)
echo "    state: $STATE"
if echo "$STATE" | grep -q "\"$EMP_ID\"" && echo "$STATE" | grep -q '"ad"' && echo "$STATE" | grep -q '"entra"'; then
  ok "pendingProvisioningDispatch has $EMP_ID: [ad, entra]"
else
  bad "pendingProvisioningDispatch missing expected entry: $STATE"
fi

echo ""
echo "== 5. Remove 'entra' from provisioningTargets (config change, while still pending) =="
OUT=$(run_curl drv-patch -X PATCH "http://source-system-service/source-systems/${SRC_ID}" \
  -H 'Content-Type: application/json' \
  -d '{"provisioningTargets":["ad"]}')
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "provisioningTargets now [\"ad\"] only" || bad "PATCH failed: $OUT"

echo ""
echo "== 6. Restore provisioning-service, run again — does retry respect the config change? =="
kubectl scale deployment/provisioning-service -n $NS --replicas=2 > /dev/null
kubectl wait --for=condition=available deployment/provisioning-service -n $NS --timeout=120s > /dev/null
ok "provisioning-service scaled back to 2"

OUT=$(run_curl drv-ingest-c -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"${SRC_NAME}/b.csv\",\"triggeredBy\":\"drv-c\"}")
echo "    ingest response: $OUT"
if echo "$OUT" | grep -q '2 attempted, 2 succeeded, 0 correlation key(s) still pending'; then
  ok "retry attempted BOTH ad and entra (2/2) despite entra no longer being in provisioningTargets"
  echo "    -> CONFIRMS the documented limitation: retry is blind to current config, it only replays"
  echo "       what was previously recorded as pending, regardless of later PATCH changes."
else
  echo "    (retry counts didn't match 2/2/0 exactly — check errorSummary above: $OUT)"
fi

echo ""
echo "== 7. Confirm pendingProvisioningDispatch is now cleared =="
STATE=$(dump_state)
echo "    state after retry: $STATE"
if echo "$STATE" | grep -q '"pendingProvisioningDispatch":{}' || ! echo "$STATE" | grep -q "\"$EMP_ID\""; then
  ok "pendingProvisioningDispatch cleared for $EMP_ID — nothing silently lost, both eventually dispatched"
else
  bad "$EMP_ID still present in pendingProvisioningDispatch: $STATE"
fi

echo ""
echo "== Cleanup =="
kubectl delete pod "$UP_POD" -n $NS --ignore-not-found > /dev/null 2>&1
ok "upload pod removed"
echo "    NOTE: $SRC_NAME source system ($SRC_ID) and $EMP_ID left in place intentionally."

echo ""
if [ "$FAIL" -eq 0 ]; then echo "=== DISPATCH RETRY VERIFICATION PASSED ==="; else echo "=== DISPATCH RETRY VERIFICATION FAILED ==="; fi
exit $FAIL
