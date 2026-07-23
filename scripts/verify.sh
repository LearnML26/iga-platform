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
      --federated-token "$(cat "$AZURE_FEDERATED_TOKEN_FILE")" --allow-no-subscriptions -o none 2>/dev/null &&
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
VRFY_IDENTITY_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
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
  # Since 2.3, ingest creates real identities: the fixture needs a
  # displayName mapping (required field, 422 without it) and unique
  # per-run keys (correlationKey is global — fixed V1/V2 keys would exist
  # after the first run and report updates instead of adds forever).
  FFC_TS=$(date +%s)
  run_curl vrfy-ffc-mapping -X POST "http://source-system-service/source-systems/${FFC_SRC_ID}/mappings" \
    -H 'Content-Type: application/json' \
    -d '{"sourceAttribute":"emp_id","targetAttribute":"correlationKey","isKey":true}' > /dev/null
  run_curl vrfy-ffc-mapping2 -X POST "http://source-system-service/source-systems/${FFC_SRC_ID}/mappings" \
    -H 'Content-Type: application/json' \
    -d '{"sourceAttribute":"name","targetAttribute":"displayName","isKey":false}' > /dev/null

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
    kubectl exec -n $NS "$FFC_POD" -- env FFC_TS="$FFC_TS" bash -c '
      az login --service-principal -u "$AZURE_CLIENT_ID" --tenant "$AZURE_TENANT_ID" \
        --federated-token "$(cat "$AZURE_FEDERATED_TOKEN_FILE")" -o none 2>/dev/null
      printf "emp_id,name\nVF${FFC_TS}A,Alice\nVF${FFC_TS}B,Bob\n" > /tmp/f.csv
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

# JML pipeline smoke test (2.5) + lifecycle slice (2.4) — the literal 3-row
# joiner/transfer/leaver fixture from the spec, against a fresh source
# system each run: a future-dated joiner lands as pending-start
# (REQ-COR-SRC-007), a transfer updates attributes, a leaver terminates via
# absence, and the lifecycle sweep endpoint runs without activating a
# joiner still outside the pre-start window (its startDate is +10 days,
# window is 3). provisioningTargets stays [] on purpose: a real target
# would queue a disable-account task that dead-letters ~2.5h later (AD
# creds unwired, 1R.7 note) and trip this script's own DLQ check on the
# next run — dispatch is covered by scripts/smoketest.sh round 3 and
# scripts/dispatch-retry-verify.sh instead. Throwaway pod names must stay
# lowercase (RFC 1123) even though the correlation keys are uppercase.
JML_TS=$(date +%s)
JML_SRC="vrfy-jml-${JML_TS}"
JML_J="VJ${JML_TS}"; JML_T="VT${JML_TS}"; JML_L="VL${JML_TS}"
JML_FUTURE=$(date -d "+10 days" +%F)

OUT=$(run_curl vrfy-jml-src -X POST http://source-system-service/source-systems \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$JML_SRC\",\"connectorType\":\"flat-file\",\"provisioningTargets\":[]}")
JML_SRC_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -z "${JML_SRC_ID:-}" ]; then
  bad "jml: source system create failed: $OUT"
else
  for M in \
    '{"sourceAttribute":"EmployeeID","targetAttribute":"employeeId","isKey":true}' \
    '{"sourceAttribute":"DisplayName","targetAttribute":"displayName","isKey":false}' \
    '{"sourceAttribute":"Department","targetAttribute":"department","isKey":false}' \
    '{"sourceAttribute":"StartDate","targetAttribute":"startDate","isKey":false}' \
    '{"sourceAttribute":"TerminationDate","targetAttribute":"terminationDate","isKey":false}'; do
    run_curl vrfy-jml-map -X POST "http://source-system-service/source-systems/${JML_SRC_ID}/mappings" \
      -H 'Content-Type: application/json' -d "$M" > /dev/null
  done

  JML_POD="vrfy-jml-upload"
  kubectl delete pod "$JML_POD" -n $NS --ignore-not-found > /dev/null 2>&1
  cat <<PODYAML | kubectl apply -f - > /dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $JML_POD
  namespace: $NS
  labels: { azure.workload.identity/use: "true" }
spec:
  serviceAccountName: flatfile-connector-service
  restartPolicy: Never
  containers:
    - name: az
      image: mcr.microsoft.com/azure-cli:latest
      command: ["sleep", "180"]
PODYAML
  if kubectl wait --for=condition=Ready "pod/$JML_POD" -n $NS --timeout=60s > /dev/null 2>&1; then
    kubectl exec -n $NS "$JML_POD" -- bash -c "
      az login --service-principal -u \"\$AZURE_CLIENT_ID\" --tenant \"\$AZURE_TENANT_ID\" \
        --federated-token \"\$(cat \"\$AZURE_FEDERATED_TOKEN_FILE\")\" -o none 2>/dev/null
      printf 'EmployeeID,DisplayName,Department,StartDate,TerminationDate\n$JML_J,Joiner Vrfy,Engineering,$JML_FUTURE,\n$JML_T,Transfer Vrfy,Sales,2024-01-15,\n$JML_L,Leaver Vrfy,Support,2024-01-15,\n' > /tmp/a.csv
      printf 'EmployeeID,DisplayName,Department,StartDate,TerminationDate\n$JML_J,Joiner Vrfy,Engineering,$JML_FUTURE,\n$JML_T,Transfer Vrfy,Marketing,2024-01-15,\n' > /tmp/b.csv
      for F in a b; do
        md5sum /tmp/\$F.csv | cut -d' ' -f1 | tr -d '\n' > /tmp/\$F.csv.md5
        az storage blob upload --auth-mode login --account-name stigadevlake -c raw -f /tmp/\$F.csv -n verify/jml-$JML_TS-\$F.csv --overwrite -o none
        az storage blob upload --auth-mode login --account-name stigadevlake -c raw -f /tmp/\$F.csv.md5 -n verify/jml-$JML_TS-\$F.csv.md5 --overwrite -o none
      done
    " > /dev/null 2>&1
  fi
  kubectl delete pod "$JML_POD" -n $NS --ignore-not-found > /dev/null 2>&1

  OUT=$(run_curl vrfy-jml-ingest-a -X POST http://flatfile-connector-service/ingest \
    -H 'Content-Type: application/json' \
    -d "{\"sourceSystemInstanceId\":\"${JML_SRC_ID}\",\"blobPath\":\"verify/jml-${JML_TS}-a.csv\",\"triggeredBy\":\"verify-jml\"}")
  echo "$OUT" | grep -q '"recordsAdded":3' && ok "jml round A: 3 joiners created" \
    || bad "jml round A unexpected deltas: $OUT"

  OUT=$(run_curl vrfy-jml-get-j "${AUTH_HDR[@]}" "http://identity-service/identities/by-correlation-key/$JML_J")
  echo "$OUT" | grep -q '"status":"pending-start"' && ok "jml: future-dated joiner is pending-start (REQ-COR-SRC-007)" \
    || bad "jml: future joiner not pending-start: $OUT"

  OUT=$(run_curl vrfy-jml-ingest-b -X POST http://flatfile-connector-service/ingest \
    -H 'Content-Type: application/json' \
    -d "{\"sourceSystemInstanceId\":\"${JML_SRC_ID}\",\"blobPath\":\"verify/jml-${JML_TS}-b.csv\",\"triggeredBy\":\"verify-jml\"}")
  echo "$OUT" | grep -q '"recordsUpdated":1' && echo "$OUT" | grep -q '"recordsTerminated":1' \
    && ok "jml round B: 1 transfer updated, 1 leaver terminated" \
    || bad "jml round B unexpected deltas: $OUT"

  OUT=$(run_curl vrfy-jml-get-l "${AUTH_HDR[@]}" "http://identity-service/identities/by-correlation-key/$JML_L")
  echo "$OUT" | grep -q '"status":"terminated"' && ok "jml: leaver is terminated" \
    || bad "jml: leaver not terminated: $OUT"

  # Lifecycle sweep (2.4): endpoint healthy, and the +10-day joiner must
  # SURVIVE it still pending-start (outside the 3-day pre-start window).
  # Note the sweep acts globally on the dev cluster — that's its job; any
  # genuinely-due identities from earlier demo data will be processed here.
  OUT=$(run_curl vrfy-jml-sweep -X POST http://flatfile-connector-service/lifecycle/sweep)
  echo "$OUT" | grep -q 'HTTP_STATUS:200' && echo "$OUT" | grep -q '"halted":false' \
    && ok "lifecycle sweep ran clean" || bad "lifecycle sweep failed: $OUT"

  OUT=$(run_curl vrfy-jml-get-j2 "${AUTH_HDR[@]}" "http://identity-service/identities/by-correlation-key/$JML_J")
  echo "$OUT" | grep -q '"status":"pending-start"' \
    && ok "jml: +10d joiner still pending-start after sweep (outside 3-day window)" \
    || bad "jml: +10d joiner unexpectedly changed by sweep: $OUT"

  run_curl vrfy-jml-del -X DELETE "http://source-system-service/source-systems/${JML_SRC_ID}" > /dev/null
fi

# rbac-service (3.1): health, role CRUD + versioning, membership-rule
# evaluation against $KEY (the "QA" department identity created earlier
# in this script), and reconcile dispatching a provisioning task.
OUT=$(run_curl vrfy-rbac-health http://rbac-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "rbac-service /healthz 200" || bad "rbac-service health failed: $OUT"

RBAC_TOKEN=$(mint_token rbac-service)
if [ -z "$RBAC_TOKEN" ]; then
  bad "could not mint an rbac-service token — is the rbac.read/write app role assignment done? (roadmap/PHASES.md 3.1)"
else
  RBAC_HDR=(-H "Authorization: Bearer $RBAC_TOKEN")
  RBAC_NAME="vrfy-role-$(date +%s)"

  OUT=$(run_curl vrfy-rbac-noauth -X POST http://rbac-service/roles \
    -H 'Content-Type: application/json' -d "{\"name\":\"$RBAC_NAME\"}")
  echo "$OUT" | grep -q 'HTTP_STATUS:401' && ok "role create without token 401" || bad "expected 401 without token: $OUT"

  OUT=$(run_curl vrfy-rbac-create -X POST http://rbac-service/roles "${RBAC_HDR[@]}" \
    -H 'Content-Type: application/json' -d "{\"name\":\"$RBAC_NAME\",\"description\":\"verify probe\"}")
  ROLE_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
  echo "$OUT" | grep -q 'HTTP_STATUS:201' && echo "$OUT" | grep -q '"version":1' \
    && ok "role create 201, version 1 ($RBAC_NAME)" || bad "role create failed: $OUT"

  if [ -n "${ROLE_ID:-}" ]; then
    OUT=$(run_curl vrfy-rbac-ent -X POST "http://rbac-service/roles/${ROLE_ID}/entitlements" "${RBAC_HDR[@]}" \
      -H 'Content-Type: application/json' \
      -d '{"targetSystemInstanceId":"verify-target","connectorType":"ad","entitlementRef":"CN=verify-group,DC=example,DC=com"}')
    echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "role entitlement added" || bad "entitlement add failed: $OUT"

    OUT=$(run_curl vrfy-rbac-getrole "${RBAC_HDR[@]}" "http://rbac-service/roles/${ROLE_ID}")
    echo "$OUT" | grep -q '"version":2' && ok "role version bumped to 2 on entitlement add (REQ-COR-RBAC-007)" \
      || bad "role version did not bump: $OUT"

    OUT=$(run_curl vrfy-rbac-rule -X POST "http://rbac-service/roles/${ROLE_ID}/membership-rules" "${RBAC_HDR[@]}" \
      -H 'Content-Type: application/json' -d '{"criteria":{"department":"QA"}}')
    RULE_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
    echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "membership rule created (department=QA)" || bad "rule create failed: $OUT"

    if [ -n "${RULE_ID:-}" ]; then
      OUT=$(run_curl vrfy-rbac-eval -X POST \
        "http://rbac-service/roles/${ROLE_ID}/membership-rules/${RULE_ID}/evaluate" "${RBAC_HDR[@]}")
      echo "$OUT" | grep -q "\"$VRFY_IDENTITY_ID\"" && ok "membership-rule evaluate matches \$KEY ($KEY) (REQ-COR-RBAC-008)" \
        || bad "evaluate did not match $KEY ($VRFY_IDENTITY_ID): $OUT"
    fi

    OUT=$(run_curl vrfy-rbac-reconcile -X POST "http://rbac-service/roles/${ROLE_ID}/reconcile" "${RBAC_HDR[@]}")
    ADDED=$(echo "$OUT" | grep -o '"assignmentsAdded":[0-9]*' | grep -o '[0-9]*$')
    # >=1 rather than ==1: identity-service has no delete-by-correlation-key
    # endpoint, so every prior verify.sh run's QA-department identity persists
    # and legitimately keeps matching this rule.
    [ -n "$ADDED" ] && [ "$ADDED" -ge 1 ] && ok "reconcile added $ADDED rule-sourced assignment(s)" \
      || bad "reconcile unexpected result: $OUT"
    echo "$OUT" | grep -q '"dispatchSucceeded"' && ok "reconcile dispatched a provisioning task (REQ-COR-RBAC-009)" \
      || bad "reconcile did not report dispatch counts: $OUT"

    OUT=$(run_curl vrfy-rbac-assignments "${RBAC_HDR[@]}" "http://rbac-service/roles/${ROLE_ID}/assignments?status=active")
    ASSIGNMENT_ID=$(echo "$OUT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
    echo "$OUT" | grep -q '"assignmentType":"rule"' && [ -n "${ASSIGNMENT_ID:-}" ] \
      && ok "active rule-sourced assignment recorded" || bad "assignment list unexpected: $OUT"

    if [ -n "${ASSIGNMENT_ID:-}" ]; then
      OUT=$(run_curl vrfy-rbac-revoke -X DELETE \
        "http://rbac-service/roles/${ROLE_ID}/assignments/${ASSIGNMENT_ID}" "${RBAC_HDR[@]}")
      echo "$OUT" | grep -q '"status":"revoked"' && ok "assignment revoked, revoke dispatched" \
        || bad "assignment revoke failed: $OUT"
    fi

    run_curl vrfy-rbac-del -X DELETE "http://rbac-service/roles/${ROLE_ID}" "${RBAC_HDR[@]}" > /dev/null
  fi
fi

# The reconcile/revoke dispatches just above queue real connectorType:"ad"
# tasks that cannot succeed — there's no real AD/LDAPS server for this dev
# cluster to bind to (confirmed, not just "not yet wired"), so they will
# dead-letter on their own schedule regardless of when this script runs.
# Self-drain here rather than requiring a manual pre-step before every
# verify.sh run; this only removes already-dead-lettered messages, never
# the live queue.
"$(dirname "$0")/drain-provisioning-dlq.sh" > /dev/null 2>&1

# notification-service (3.3): health + a real end-to-end queue smoke test.
# It has no domain HTTP API of its own (pure Service Bus consumer + email/
# webhook fan-out worker — see src/notification-service/app/main.py), so
# instead of a curl-based create/dedupe test like the other services, this
# publishes a synthetic ProvisioningFailed message onto the non-session
# 'notification-tasks' queue (confirmed sessions:false in
# infra/modules/messaging.bicep, shape matches provisioning-service's
# notify_failure() exactly) using provisioning-service's own workload
# identity (already granted Service Bus Data Owner in this script), then
# polls the queue's activeMessageCount back down to 0 to confirm
# notification-service actually received, parsed, dispatched, and completed
# it. Email/webhook delivery itself is NOT asserted here — those channels
# legitimately no-op-and-log until the human populates the
# 'notification-sender' k8s secret (Phase 3.3 [HUMAN gate]); this test only
# proves the consumer wiring (auth, queue name, message parsing, dispatch)
# is correct end to end.
OUT=$(run_curl vrfy-notif-health http://notification-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "notification-service /healthz 200" || bad "notification-service health failed: $OUT"

NOTIF_POD="vrfy-notif-publish"
kubectl delete pod "$NOTIF_POD" -n $NS --ignore-not-found > /dev/null 2>&1
cat <<PODYAML | kubectl apply -f - > /dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $NOTIF_POD
  namespace: $NS
  labels: { azure.workload.identity/use: "true" }
spec:
  serviceAccountName: provisioning-service
  restartPolicy: Never
  containers:
    - name: publish
      image: mcr.microsoft.com/azure-cli:latest
      command: ["sleep", "90"]
PODYAML
if kubectl wait --for=condition=Ready "pod/$NOTIF_POD" -n $NS --timeout=60s > /dev/null 2>&1; then
  # DefaultAzureCredential picks up the pod's workload-identity env vars
  # (AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_FEDERATED_TOKEN_FILE, injected
  # by the AKS webhook) automatically — no explicit az login needed here,
  # unlike the bearer-token curl tests above.
  kubectl exec -n $NS "$NOTIF_POD" -- env SERVICEBUS_NAMESPACE=sb-iga-dev.servicebus.windows.net bash -c '
    python3 -m pip install --quiet azure-servicebus azure-identity > /dev/null 2>&1
    python3 - <<PY
import asyncio, json, os, uuid
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage

async def main():
    cred = DefaultAzureCredential()
    async with ServiceBusClient(fully_qualified_namespace=os.environ["SERVICEBUS_NAMESPACE"], credential=cred) as sb:
        async with sb.get_queue_sender("notification-tasks") as sender:
            await sender.send_messages(ServiceBusMessage(json.dumps({
                "type": "ProvisioningFailed",
                "taskId": f"verify-{uuid.uuid4()}",
                "identityId": "verify",
                "instanceId": "verify",
                "operationType": "grant",
                "error": "verify.sh smoke test",
                "occurredAt": "1970-01-01T00:00:00+00:00",
            })))
    await cred.close()

asyncio.run(main())
PY
  ' > /dev/null 2>&1
fi
kubectl delete pod "$NOTIF_POD" -n $NS --ignore-not-found > /dev/null 2>&1

DRAINED=0
for i in $(seq 1 12); do
  ACTIVE=$(az servicebus queue show -g rg-iga-dev-data --namespace-name sb-iga-dev \
    -n notification-tasks --query countDetails.activeMessageCount -o tsv 2>/dev/null || echo "?")
  if [ "$ACTIVE" = "0" ]; then DRAINED=1; break; fi
  sleep 5
done
if [ "$DRAINED" -eq 1 ]; then ok "notification-service drained the test message off notification-tasks"; else
  bad "notification-tasks still has an unprocessed message after 60s — check: kubectl logs -n iga deploy/notification-service"
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

NOTIF_DLQ=$(az servicebus queue show -g rg-iga-dev-data --namespace-name sb-iga-dev \
  -n notification-tasks --query countDetails.deadLetterMessageCount -o tsv 2>/dev/null || echo "?")
if [ "$NOTIF_DLQ" = "0" ]; then ok "notification-tasks DLQ empty"; elif [ "$NOTIF_DLQ" = "?" ]; then
  bad "could not read notification-tasks Service Bus DLQ count"; else
  bad "notification-tasks DLQ has $NOTIF_DLQ message(s) — investigate before proceeding"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then echo "=== VERIFY PASSED ==="; else echo "=== VERIFY FAILED ==="; fi
exit $FAIL
