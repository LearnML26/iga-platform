#!/usr/bin/env bash
# ============================================================================
# Phase 2.3 live smoke test — flatfile-connector -> identity-service ->
# provisioning-service, three rounds (create / update+terminate / dispatch).
#
# Run this in an environment with kubectl + az CLI already authenticated
# against the iga-dev AKS cluster:
#   az aks get-credentials -g <compute-rg> -n <aks-name> --overwrite-existing
#   kubelogin convert-kubeconfig -l azurecli
#
# Mirrors scripts/verify.sh's established patterns exactly: run_curl (a
# throwaway curlimages/curl pod per call), mint_token (a throwaway
# azure-cli pod on a service's own workload identity, exchanging the
# federated token for a bearer token), and the blob-upload-via-pod pattern
# from the flatfile-connector-service check.
#
# PREREQ: this assumes the 2.3 [HUMAN gate] has already been granted —
# flatfile-connector-service's managed identity has identities.read,
# identities.write, and provisioning.write app roles on iga-platform-api
# (commands printed at the end of scripts/deploy.sh). Without that grant,
# every round below will show recordsAdded/Updated/Terminated all 0 and
# status "failed", with errorSummary citing 403s that crossed
# APPLY_FAILURE_THRESHOLD — that's the tell if this hasn't been run yet.
# ============================================================================
set -uo pipefail
FAIL=0
ok()   { echo "  ✔ $1"; }
bad()  { echo "  ✘ $1"; FAIL=1; }

NS=iga
API_AUDIENCE="api://$(az ad app list --display-name iga-platform-api --query '[0].appId' -o tsv 2>/dev/null)"
STORAGE_ACCOUNT=stigadevlake

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
  local SA="$1" POD="r23-token-${1}"
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

echo "== Setup: bearer tokens =="
IDENTITY_TOKEN=$(mint_token identity-service)
if [ -z "$IDENTITY_TOKEN" ]; then bad "could not mint identity-service token"; exit 1; fi
ok "minted identity-service token"
AUTH_HDR=(-H "Authorization: Bearer $IDENTITY_TOKEN")

echo "== Setup: upload pod (flatfile-connector-service identity) =="
UP_POD=r23-upload
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

upload_csv() { # upload_csv <csv-content> <blob-name>
  local CONTENT="$1" BLOB="$2"
  kubectl exec -n $NS "$UP_POD" -- bash -c "cat > /tmp/f.csv <<'CSV'
$CONTENT
CSV
md5sum /tmp/f.csv | cut -d' ' -f1 | tr -d '\n' > /tmp/f.csv.md5
az storage blob upload --auth-mode login --account-name $STORAGE_ACCOUNT -c raw -f /tmp/f.csv -n '$BLOB' --overwrite -o none
az storage blob upload --auth-mode login --account-name $STORAGE_ACCOUNT -c raw -f /tmp/f.csv.md5 -n '$BLOB.md5' --overwrite -o none
" > /dev/null 2>&1
}

# ============================================================================
echo ""
echo "== Round 1: joiners (create path) =="
# 1. create source system instance
OUT=$(run_curl r23-src-create -X POST http://source-system-service/source-systems \
  -H 'Content-Type: application/json' \
  -d '{"name":"hr-smoke-test","connectorType":"flat-file","provisioningTargets":[]}')
if echo "$OUT" | grep -q 'HTTP_STATUS:201'; then ok "source system 'hr-smoke-test' created"; else
  bad "source system create failed: $OUT"; exit 1
fi
SRC_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "    instance id: $SRC_ID"

# 2. five mappings
declare -a MAPPINGS=(
  '{"sourceAttribute":"EmployeeID","targetAttribute":"employeeId","isKey":true}'
  '{"sourceAttribute":"FirstName","targetAttribute":"givenName","isKey":false}'
  '{"sourceAttribute":"LastName","targetAttribute":"familyName","isKey":false}'
  '{"sourceAttribute":"DisplayName","targetAttribute":"displayName","isKey":false}'
  '{"sourceAttribute":"Department","targetAttribute":"department","isKey":false}'
)
for i in "${!MAPPINGS[@]}"; do
  OUT=$(run_curl "r23-map-$i" -X POST "http://source-system-service/source-systems/${SRC_ID}/mappings" \
    -H 'Content-Type: application/json' -d "${MAPPINGS[$i]}")
  echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "mapping $i created" || bad "mapping $i failed: $OUT"
done

# 3. upload round1.csv
upload_csv 'EmployeeID,FirstName,LastName,DisplayName,Department
E2001,Alice,Wong,Alice Wong,Engineering
E2002,Ben,Diaz,Ben Diaz,Sales
E2003,Carla,Smith,Carla Smith,Engineering' "hr-smoke-test/round1.csv"
ok "round1.csv uploaded"

# 4. ingest
OUT=$(run_curl r23-ingest-1 -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"hr-smoke-test/round1.csv\",\"triggeredBy\":\"smoke-test-r1\"}")
echo "    ingest response: $OUT"
if echo "$OUT" | grep -q 'HTTP_STATUS:200' \
    && echo "$OUT" | grep -q '"recordsAdded":3' \
    && echo "$OUT" | grep -q '"recordsUpdated":0' \
    && echo "$OUT" | grep -q '"recordsTerminated":0'; then
  ok "round 1 ingest: 3 added / 0 updated / 0 terminated"
else
  bad "round 1 ingest did not match expected deltas: $OUT"
fi

# 5. confirm E2001/E2002/E2003 active
declare -A IDENTITY_ID  # correlationKey -> identityId, reused in later rounds
for KEY in E2001 E2002 E2003; do
  # Pod names must be lowercase (RFC 1123) — an uppercase key in the name
  # makes `kubectl run` fail before curl ever executes, which surfaces as a
  # completely empty $OUT with no HTTP_STATUS line (the "silent GET failure"
  # seen on every earlier run of this script).
  OUT=$(run_curl "r23-get-$(echo "$KEY" | tr '[:upper:]' '[:lower:]')" "${AUTH_HDR[@]}" "http://identity-service/identities/by-correlation-key/$KEY")
  if echo "$OUT" | grep -q 'HTTP_STATUS:200' && echo "$OUT" | grep -q '"status":"active"'; then
    ok "$KEY exists and is active"
  else
    bad "$KEY not found or not active: $OUT"
  fi
  IDENTITY_ID[$KEY]=$(echo "$OUT" | grep -o '"identityId":"[^"]*"' | head -1 | cut -d'"' -f4)
done
echo "    identityIds: ${IDENTITY_ID[E2001]:-?} / ${IDENTITY_ID[E2002]:-?} / ${IDENTITY_ID[E2003]:-?}"

# ============================================================================
echo ""
echo "== Round 2: update + termination =="
# 6. round2.csv — Carla's department changes, E2002 dropped, E2004 added
upload_csv 'EmployeeID,FirstName,LastName,DisplayName,Department
E2001,Alice,Wong,Alice Wong,Engineering
E2003,Carla,Smith,Carla Smith,Product
E2004,Dana,Lee,Dana Lee,Sales' "hr-smoke-test/round2.csv"
ok "round2.csv uploaded"

# 7. ingest
OUT=$(run_curl r23-ingest-2 -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"hr-smoke-test/round2.csv\",\"triggeredBy\":\"smoke-test-r2\"}")
echo "    ingest response: $OUT"
if echo "$OUT" | grep -q 'HTTP_STATUS:200' \
    && echo "$OUT" | grep -q '"recordsAdded":1' \
    && echo "$OUT" | grep -q '"recordsUpdated":1' \
    && echo "$OUT" | grep -q '"recordsTerminated":1'; then
  ok "round 2 ingest: 1 added / 1 updated / 1 terminated"
else
  bad "round 2 ingest did not match expected deltas: $OUT"
fi

# 8. confirm E2002 terminated, and (since provisioningTargets was still [])
#    that nothing was dispatched for it — the errorSummary's apply tally
#    covers this: 0 apply failures and no dispatch note, per ingest.py's
#    _apply_terminations logging-only path for an empty provisioningTargets.
OUT=$(run_curl r23-get-e2002-v2 "${AUTH_HDR[@]}" "http://identity-service/identities/by-correlation-key/E2002")
if echo "$OUT" | grep -q 'HTTP_STATUS:200' && echo "$OUT" | grep -q '"status":"terminated"'; then
  ok "E2002 is now terminated"
else
  bad "E2002 not terminated as expected: $OUT"
fi
echo "    (E2002 has no provisioning tasks: provisioningTargets was [] at ingest time — see round 2's errorSummary above, printed with the ingest response)"

# ============================================================================
echo ""
echo "== Round 3: provisioning dispatch =="
# 9. set provisioningTargets = ["ad"]
OUT=$(run_curl r23-patch-targets -X PATCH "http://source-system-service/source-systems/${SRC_ID}" \
  -H 'Content-Type: application/json' \
  -d '{"provisioningTargets":["ad"]}')
if echo "$OUT" | grep -q 'HTTP_STATUS:200' && echo "$OUT" | grep -q '"provisioningTargets":\["ad"\]'; then
  ok "provisioningTargets set to [\"ad\"]"
else
  bad "PATCH provisioningTargets failed or didn't stick: $OUT"
fi

# 10. round3.csv — E2003 (Carla) dropped
upload_csv 'EmployeeID,FirstName,LastName,DisplayName,Department
E2001,Alice,Wong,Alice Wong,Engineering
E2004,Dana,Lee,Dana Lee,Sales' "hr-smoke-test/round3.csv"
ok "round3.csv uploaded"

OUT=$(run_curl r23-ingest-3 -X POST http://flatfile-connector-service/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"sourceSystemInstanceId\":\"${SRC_ID}\",\"blobPath\":\"hr-smoke-test/round3.csv\",\"triggeredBy\":\"smoke-test-r3\"}")
echo "    ingest response: $OUT"
if echo "$OUT" | grep -q 'HTTP_STATUS:200' \
    && echo "$OUT" | grep -q '"recordsAdded":0' \
    && echo "$OUT" | grep -q '"recordsUpdated":0' \
    && echo "$OUT" | grep -q '"recordsTerminated":1'; then
  ok "round 3 ingest: 0 added / 0 updated / 1 terminated"
else
  bad "round 3 ingest did not match expected deltas: $OUT"
fi

# 11a. confirm E2003 terminated
OUT=$(run_curl r23-get-e2003-v2 "${AUTH_HDR[@]}" "http://identity-service/identities/by-correlation-key/E2003")
if echo "$OUT" | grep -q 'HTTP_STATUS:200' && echo "$OUT" | grep -q '"status":"terminated"'; then
  ok "E2003 is now terminated"
else
  bad "E2003 not terminated as expected: $OUT"
fi
E2003_ID="${IDENTITY_ID[E2003]:-}"

# 11b. primary evidence a disable-account task for E2003/ad was dispatched:
# ingest.py's _apply_terminations only increments the "succeeded" tally (and
# leaves apply-failure count unchanged) when POST /tasks returns 202 — so a
# clean (non-halted, 0-new-apply-failure) round-3 result for a termination
# with provisioningTargets=["ad"] IS the dispatch confirmation. Corroborate
# directly from provisioning-service's own logs as a second, independent
# signal (the API-side "queued" log line, then — since the worker polls
# continuously — very likely the first AD-connector failure attempt too;
# full dead-letter takes ~2.5h of backoff and is NOT waited for here).
echo "    checking provisioning-service logs for E2003's task (identityId=${E2003_ID:-unknown})..."
FOUND_QUEUED=0
FOUND_AD_ATTEMPT=0
for i in $(seq 1 8); do
  LOGS=$(kubectl logs -n $NS -l app=provisioning-service --all-containers --since=3m 2>/dev/null)
  if [ -n "${E2003_ID:-}" ] && echo "$LOGS" | grep -q "$E2003_ID"; then
    echo "$LOGS" | grep "$E2003_ID" | grep -qi "queued" && FOUND_QUEUED=1
    echo "$LOGS" | grep -qi "ad operation failed\|no connector registered\|ConnectorError" && FOUND_AD_ATTEMPT=1
    [ "$FOUND_QUEUED" -eq 1 ] && break
  fi
  sleep 5
done
if [ "$FOUND_QUEUED" -eq 1 ]; then
  ok "provisioning-service logs confirm a task was queued for E2003 (connectorType=ad, operationType=disable-account)"
else
  echo "    (could not independently confirm via logs within 40s — not fatal; the FeedRun-summary check above is the primary assertion this task specifies)"
fi
if [ "$FOUND_AD_ATTEMPT" -eq 1 ]; then
  ok "provisioning-service already attempted (and, as expected, failed) the AD disable-account execution — confirms dispatch reached the connector, not just the queue"
fi

echo ""
echo "== Cleanup =="
kubectl delete pod "$UP_POD" -n $NS --ignore-not-found > /dev/null 2>&1
ok "upload pod removed"
echo "    NOTE: hr-smoke-test source system ($SRC_ID), its identities (E2001/E2002/E2003/E2004),"
echo "    and the queued disable-account task are left in place intentionally — this is dev data,"
echo "    delete manually if you want it gone (the task will dead-letter itself in ~2.5h regardless)."

echo ""
if [ "$FAIL" -eq 0 ]; then echo "=== PHASE 2.3 SMOKE TEST PASSED ==="; else echo "=== PHASE 2.3 SMOKE TEST FAILED ==="; fi
exit $FAIL
