#!/usr/bin/env bash
# ============================================================================
# Drain (receive-and-complete) every message in the provisioning-tasks
# dead-letter queue.
#
# Why this exists: manual dispatch tests (smoketest.sh round 3,
# dispatch-retry-verify.sh, the JML demo with real provisioningTargets)
# queue disable-account tasks whose AD execution can't succeed yet (bind
# creds never wired into Key Vault — known gap). Each retries ~2.5h on the
# backoff schedule, dead-letters, and then trips verify.sh's DLQ-empty
# check on every subsequent run. Run this after manual dispatch testing to
# reset the gate. It only completes (removes) dead-lettered messages — the
# live queue is untouched.
#
# Azure Service Bus has no CLI purge; this uses a throwaway in-cluster pod
# on provisioning-service's own workload identity (Service Bus Data Owner).
# The DLQ sub-queue of a sessions-enabled queue is itself non-sessioned, so
# a plain receiver is correct here.
#
# Runs the drain script on provisioning-service's OWN image
# (python:3.12-slim, already has azure-identity/azure-servicebus baked in
# via requirements.txt) instead of azure-cli + a runtime `pip install` —
# an earlier version did that and the install failed with no visible
# error (swallowed by `2>/dev/null`). Using DefaultAzureCredential's own
# workload-identity support also means no `az login` step is needed at
# all: the SDK reads AZURE_CLIENT_ID/AZURE_TENANT_ID/
# AZURE_FEDERATED_TOKEN_FILE directly, the same as ingest.py/lifecycle.py
# already do — no Azure CLI involved anywhere in this script.
# ============================================================================
set -uo pipefail
NS=iga
POD=drain-dlq
SB_NS="${SERVICEBUS_NAMESPACE:-sb-iga-dev.servicebus.windows.net}"

PROV_IMAGE=$(kubectl get deployment provisioning-service -n $NS -o jsonpath='{.spec.template.spec.containers[0].image}')
if [ -z "$PROV_IMAGE" ]; then
  echo "✘ could not read provisioning-service's current image (is it deployed?)"; exit 1
fi
echo "Using provisioning-service's own image: $PROV_IMAGE"

kubectl delete pod "$POD" -n $NS --ignore-not-found > /dev/null 2>&1
cat <<PODYAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
  labels: { azure.workload.identity/use: "true" }
spec:
  serviceAccountName: provisioning-service
  restartPolicy: Never
  containers:
    - name: drain
      image: $PROV_IMAGE
      command: ["sleep", "300"]
PODYAML
if ! kubectl wait --for=condition=Ready "pod/$POD" -n $NS --timeout=60s > /dev/null 2>&1; then
  echo "✘ drain pod never became ready"
  kubectl describe pod "$POD" -n $NS 2>&1 | tail -20
  kubectl delete pod "$POD" -n $NS --ignore-not-found > /dev/null 2>&1
  exit 1
fi

kubectl exec -i -n $NS "$POD" -- env SERVICEBUS_NAMESPACE="$SB_NS" python3 <<'PY'
import os
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusSubQueue

cred = DefaultAzureCredential()
drained = 0
with ServiceBusClient(fully_qualified_namespace=os.environ["SERVICEBUS_NAMESPACE"], credential=cred) as sb:
    with sb.get_queue_receiver(
        "provisioning-tasks", sub_queue=ServiceBusSubQueue.DEAD_LETTER, max_wait_time=10
    ) as receiver:
        for msg in receiver:
            receiver.complete_message(msg)
            drained += 1
print(f"drained {drained} dead-lettered message(s) from provisioning-tasks")
PY
RC=$?
kubectl delete pod "$POD" -n $NS --ignore-not-found > /dev/null 2>&1
exit $RC
