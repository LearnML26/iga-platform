#!/usr/bin/env bash
# ============================================================================
# IGA Platform — end-to-end deployment
#
# Prereqs (run on your machine or Cloud Shell — credentials never leave it):
#   az login
#   az account set --subscription <SUBSCRIPTION_ID>
#   Tools: az cli >= 2.60, docker, kubectl, envsubst (gettext)
#
# Usage:
#   ./scripts/deploy.sh dev canadacentral <ADMIN_GROUP_OBJECT_ID>
#
# What it does:
#   1. Deploys all Bicep infrastructure (subscription scope)
#   2. Creates user-assigned managed identities + federated credentials
#      for workload identity (REQ-INF-031)
#   3. Grants data-plane RBAC (Cosmos, Service Bus, Event Hubs) to those identities
#   4. Builds and pushes service images to ACR
#   5. Renders and applies Kubernetes manifests
# ============================================================================
set -euo pipefail

ENV="${1:?usage: deploy.sh <env> <location> <adminGroupObjectId>}"
LOCATION="${2:?}"
ADMIN_GROUP="${3:?}"
APP="iga"
SUFFIX="${APP}-${ENV}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"

echo "==> [1/5] Deploying infrastructure (${ENV} @ ${LOCATION})"
DEPLOY_OUT=$(az deployment sub create \
  --location "$LOCATION" \
  --name "iga-${ENV}-$(date +%s)" \
  --template-file infra/main.bicep \
  --parameters environment="$ENV" location="$LOCATION" adminGroupObjectId="$ADMIN_GROUP" \
  --query properties.outputs -o json)

AKS_NAME=$(echo "$DEPLOY_OUT"        | python3 -c "import sys,json;print(json.load(sys.stdin)['aksName']['value'])")
ACR_LOGIN_SERVER=$(echo "$DEPLOY_OUT"| python3 -c "import sys,json;print(json.load(sys.stdin)['acrLoginServer']['value'])")
COSMOS_ACCOUNT=$(echo "$DEPLOY_OUT"  | python3 -c "import sys,json;print(json.load(sys.stdin)['cosmosAccountName']['value'])")
SB_NAMESPACE=$(echo "$DEPLOY_OUT"    | python3 -c "import sys,json;print(json.load(sys.stdin)['serviceBusNamespace']['value'])")
EVH_NAMESPACE=$(echo "$DEPLOY_OUT"   | python3 -c "import sys,json;print(json.load(sys.stdin)['eventHubNamespace']['value'])")
RG_COMPUTE=$(echo "$DEPLOY_OUT"      | python3 -c "import sys,json;print(json.load(sys.stdin)['computeResourceGroup']['value'])")
RG_DATA=$(echo "$DEPLOY_OUT"         | python3 -c "import sys,json;print(json.load(sys.stdin)['dataResourceGroup']['value'])")

echo "==> [2/5] Workload identities + federated credentials"
az aks get-credentials -g "$RG_COMPUTE" -n "$AKS_NAME" --overwrite-existing
kubelogin convert-kubeconfig -l azurecli
OIDC_ISSUER=$(az aks show -g "$RG_COMPUTE" -n "$AKS_NAME" --query oidcIssuerProfile.issuerUrl -o tsv)

declare -A SVC_CLIENT_IDS
for SVC in identity-service provisioning-service; do
  MI_NAME="mi-${SUFFIX}-${SVC}"
  az identity create -g "$RG_COMPUTE" -n "$MI_NAME" -l "$LOCATION" -o none
  CLIENT_ID=$(az identity show -g "$RG_COMPUTE" -n "$MI_NAME" --query clientId -o tsv)
  SVC_CLIENT_IDS[$SVC]="$CLIENT_ID"
  az identity federated-credential create \
    --identity-name "$MI_NAME" -g "$RG_COMPUTE" \
    --name "fc-${SVC}" \
    --issuer "$OIDC_ISSUER" \
    --subject "system:serviceaccount:iga:${SVC}" \
    --audiences api://AzureADTokenExchange -o none
done

echo "==> [3/5] Data-plane RBAC"
SUB_ID=$(az account show --query id -o tsv)
IDN_PRINCIPAL=$(az identity show -g "$RG_COMPUTE" -n "mi-${SUFFIX}-identity-service" --query principalId -o tsv)
PRV_PRINCIPAL=$(az identity show -g "$RG_COMPUTE" -n "mi-${SUFFIX}-provisioning-service" --query principalId -o tsv)

# Cosmos DB built-in Data Contributor for identity-service
az cosmosdb sql role assignment create \
  --account-name "$COSMOS_ACCOUNT" -g "$RG_DATA" \
  --role-definition-id 00000000-0000-0000-0000-000000000002 \
  --principal-id "$IDN_PRINCIPAL" \
  --scope "/" -o none || true

# Event Hubs Data Sender for identity-service; Service Bus Data Owner for provisioning
az role assignment create --assignee "$IDN_PRINCIPAL" \
  --role "Azure Event Hubs Data Sender" \
  --scope "/subscriptions/${SUB_ID}/resourceGroups/${RG_DATA}/providers/Microsoft.EventHub/namespaces/${EVH_NAMESPACE}" -o none || true
az role assignment create --assignee "$PRV_PRINCIPAL" \
  --role "Azure Service Bus Data Owner" \
  --scope "/subscriptions/${SUB_ID}/resourceGroups/${RG_DATA}/providers/Microsoft.ServiceBus/namespaces/${SB_NAMESPACE}" -o none || true

echo "==> [4/5] Building and pushing images (ACR server-side build — no local Docker needed)"
for SVC in identity-service provisioning-service; do
  # az acr build uploads the context, builds in ACR, and pushes the image
  # to ${ACR_LOGIN_SERVER}/${SVC}:${IMAGE_TAG} automatically on success.
  az acr build \
    --registry "${ACR_LOGIN_SERVER%%.*}" \
    --image "${SVC}:${IMAGE_TAG}" \
    "src/${SVC}"
done

echo "==> [5/5] Deploying to Kubernetes"
kubectl apply -f k8s/base/namespace.yaml
export ACR_LOGIN_SERVER IMAGE_TAG COSMOS_ACCOUNT
export EVENTHUB_NAMESPACE="$EVH_NAMESPACE"
export SERVICEBUS_NAMESPACE="$SB_NAMESPACE"
export IDENTITY_SVC_CLIENT_ID="${SVC_CLIENT_IDS[identity-service]}"
export PROVISIONING_SVC_CLIENT_ID="${SVC_CLIENT_IDS[provisioning-service]}"
for F in k8s/services/*.yaml; do
  envsubst < "$F" | kubectl apply -f -
done

echo ""
echo "=== Deployment complete ==="
kubectl get pods -n iga
echo ""
echo "Next steps:"
echo "  - Store AD connector bind credentials in Key Vault kv-${SUFFIX} and sync to the 'ad-connector' k8s secret via CSI SecretProviderClass"
echo "  - Grant the Entra connector managed identity Graph GroupMember.ReadWrite.All (admin consent required)"
echo "  - Apply audit container immutability: az storage container immutability-policy create ..."
