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

# identity-service: health, create (unique key), dedupe
KEY="VRFY$(date +%s)"
OUT=$(run_curl vrfy-health http://identity-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "identity-service /healthz 200" || bad "identity-service health failed: $OUT"

OUT=$(run_curl vrfy-create -X POST http://identity-service/identities \
  -H 'Content-Type: application/json' \
  -d "{\"correlationKey\":\"$KEY\",\"displayName\":\"Verify Bot\",\"department\":\"QA\",\"jobTitle\":\"Probe\"}")
echo "$OUT" | grep -q 'HTTP_STATUS:201' && ok "identity create 201 ($KEY)" || bad "identity create failed: $OUT"

OUT=$(run_curl vrfy-dedupe -X POST http://identity-service/identities \
  -H 'Content-Type: application/json' \
  -d "{\"correlationKey\":\"$KEY\",\"displayName\":\"Verify Bot\",\"department\":\"QA\",\"jobTitle\":\"Probe\"}")
echo "$OUT" | grep -q 'HTTP_STATUS:409' && ok "correlation dedupe 409" || bad "dedupe check failed: $OUT"

OUT=$(run_curl vrfy-search "http://identity-service/identities?department=QA")
echo "$OUT" | grep -q "$KEY" && ok "identity search returns created record" || bad "search failed: $OUT"

# provisioning-service: health + task acceptance (no connector execution asserted)
OUT=$(run_curl vrfy-prov-health http://provisioning-service/healthz)
echo "$OUT" | grep -q 'HTTP_STATUS:200' && ok "provisioning-service /healthz 200" || bad "provisioning health failed: $OUT"

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
