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
SQL_SERVER_NAME=$(echo "$DEPLOY_OUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['sqlServerName']['value'])")
STORAGE_ACCOUNT=$(echo "$DEPLOY_OUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['storageAccountName']['value'])")
RG_COMPUTE=$(echo "$DEPLOY_OUT"      | python3 -c "import sys,json;print(json.load(sys.stdin)['computeResourceGroup']['value'])")
RG_DATA=$(echo "$DEPLOY_OUT"         | python3 -c "import sys,json;print(json.load(sys.stdin)['dataResourceGroup']['value'])")

echo "==> [2/5] Workload identities + federated credentials"
az aks get-credentials -g "$RG_COMPUTE" -n "$AKS_NAME" --overwrite-existing
kubelogin convert-kubeconfig -l azurecli
OIDC_ISSUER=$(az aks show -g "$RG_COMPUTE" -n "$AKS_NAME" --query oidcIssuerProfile.issuerUrl -o tsv)

declare -A SVC_CLIENT_IDS
for SVC in identity-service provisioning-service source-system-service flatfile-connector-service notification-service rbac-service; do
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

# Storage Blob Data Contributor for the flat-file connector (2.2) — read raw/,
# write raw/quarantine/ and curated/source-state/. No SAS/keys: the storage
# account has key-based auth off by default posture (RBAC-only data plane).
FFC_PRINCIPAL=$(az identity show -g "$RG_COMPUTE" -n "mi-${SUFFIX}-flatfile-connector-service" --query principalId -o tsv)
az role assignment create --assignee "$FFC_PRINCIPAL" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/${SUB_ID}/resourceGroups/${RG_DATA}/providers/Microsoft.Storage/storageAccounts/${STORAGE_ACCOUNT}" -o none || true

# Service Bus Data Receiver for notification-service (Phase 3.3) — it only
# consumes 'notification-tasks', never sends, so Receiver (not Owner, unlike
# provisioning-service which both sends and receives) is least-privilege.
NOTIF_PRINCIPAL=$(az identity show -g "$RG_COMPUTE" -n "mi-${SUFFIX}-notification-service" --query principalId -o tsv)
az role assignment create --assignee "$NOTIF_PRINCIPAL" \
  --role "Azure Service Bus Data Receiver" \
  --scope "/subscriptions/${SUB_ID}/resourceGroups/${RG_DATA}/providers/Microsoft.ServiceBus/namespaces/${SB_NAMESPACE}" -o none || true

echo "==> [4/5] Building and pushing images (ACR server-side build — no local Docker needed)"
for SVC in identity-service provisioning-service source-system-service flatfile-connector-service notification-service rbac-service; do
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
export SQL_SERVER_FQDN="${SQL_SERVER_NAME}.database.windows.net"
export IDENTITY_SVC_CLIENT_ID="${SVC_CLIENT_IDS[identity-service]}"
export PROVISIONING_SVC_CLIENT_ID="${SVC_CLIENT_IDS[provisioning-service]}"
export SOURCE_SYSTEM_SVC_CLIENT_ID="${SVC_CLIENT_IDS[source-system-service]}"
export FLATFILE_CONNECTOR_SVC_CLIENT_ID="${SVC_CLIENT_IDS[flatfile-connector-service]}"
export NOTIFICATION_SVC_CLIENT_ID="${SVC_CLIENT_IDS[notification-service]}"
export RBAC_SVC_CLIENT_ID="${SVC_CLIENT_IDS[rbac-service]}"
export LAKE_STORAGE_ACCOUNT="$STORAGE_ACCOUNT"
export ENTRA_TENANT_ID="$(az account show --query tenantId -o tsv)"
export API_AUDIENCE="api://$(az ad app list --display-name iga-platform-api --query '[0].appId' -o tsv)"
# Every SQL-backed service ships an Alembic migrate Job alongside its
# Deployment. Job pod templates are immutable in Kubernetes, so a plain
# `kubectl apply` fails once IMAGE_TAG changes (spec.template: field is
# immutable) — delete any prior run before re-applying so each Job can be
# recreated cleanly. (1R.7: this bit source-system-service once already;
# rbac-service needs the identical treatment, hence the loop rather than
# duplicating the block per service.)
SQL_MIGRATE_SERVICES=(source-system-service rbac-service)
for SVC in "${SQL_MIGRATE_SERVICES[@]}"; do
  kubectl delete "job/${SVC}-migrate" -n iga --ignore-not-found
done
for F in k8s/services/*.yaml; do
  envsubst < "$F" | kubectl apply -f -
done

# Wait for each migrate Job, then roll its Deployment so pods restart
# cleanly against the finished schema rather than racing it on first
# request.
for SVC in "${SQL_MIGRATE_SERVICES[@]}"; do
  if kubectl get "job/${SVC}-migrate" -n iga > /dev/null 2>&1; then
    echo "==> waiting for ${SVC}-migrate Job"
    if kubectl wait --for=condition=complete "job/${SVC}-migrate" -n iga --timeout=180s; then
      kubectl rollout restart "deployment/${SVC}" -n iga
    else
      echo "!! ${SVC}-migrate Job did not complete — check: kubectl logs -n iga job/${SVC}-migrate"
      echo "!! this is very likely the SQL permission grant below not having been run yet"
    fi
  fi
done

echo ""
echo "=== Deployment complete ==="
kubectl get pods -n iga
echo ""
echo "Next steps:"
echo "  - Store AD connector bind credentials in Key Vault kv-${SUFFIX} and sync to the 'ad-connector' k8s secret via CSI SecretProviderClass"
echo "  - [HUMAN gate, Phase 3.3] Store notification-service sender config in Key Vault kv-${SUFFIX} and sync to the"
echo "    'notification-sender' k8s secret (same manual/CSI pattern as ad-connector above). Until this is done the"
echo "    service stays healthy but logs+skips email/webhook delivery. See the notification-service task's printed"
echo "    'az keyvault secret set' commands for the exact secret names/keys."
echo "  - Grant the Entra connector managed identity Graph GroupMember.ReadWrite.All (admin consent required)"
echo "  - [HUMAN gate, Phase 2.3] Grant flatfile-connector-service's managed identity the iga-platform-api"
echo "    app roles it needs to call identity-service/provisioning-service directly (identities.read,"
echo "    identities.write, provisioning.write) — Graph app-role-assignment needs directory perms. Run:"
echo "      API_SP_ID=\$(az ad sp list --filter \"displayName eq 'iga-platform-api'\" --query '[0].id' -o tsv)"
echo "      FFC_SP_ID=\$(az ad sp list --filter \"displayName eq 'mi-${SUFFIX}-flatfile-connector-service'\" --query '[0].id' -o tsv)"
echo "      for ROLE in identities.read identities.write provisioning.write; do"
echo "        ROLE_ID=\$(az ad sp show --id \"\$API_SP_ID\" --query \"appRoles[?value=='\$ROLE'].id | [0]\" -o tsv)"
echo "        az rest --method POST --uri \"https://graph.microsoft.com/v1.0/servicePrincipals/\$FFC_SP_ID/appRoleAssignments\" \\"
echo "          --body \"{\\\"principalId\\\":\\\"\$FFC_SP_ID\\\",\\\"resourceId\\\":\\\"\$API_SP_ID\\\",\\\"appRoleId\\\":\\\"\$ROLE_ID\\\"}\""
echo "      done"
echo "    Until this is granted, every identity-service/provisioning-service call from the connector 403s —"
echo "    which correctly counts toward the apply-failure threshold (REQ-COR-SRC-009) rather than hanging."
echo "  - [ONE-TIME, HUMAN] Grant source-system-service's managed identity access to sqldb-sourcesystem."
echo "    sql-${SUFFIX} has publicNetworkAccess Disabled, so this SQL must run from inside the VNet"
echo "    (e.g. a kubectl run sqlcmd pod, or Cloud Shell with VNet integration), authenticated as a"
echo "    member of the iga-platform-admins group (the SQL AAD admin). Run against sqldb-sourcesystem:"
echo "      CREATE USER [mi-${SUFFIX}-source-system-service] FROM EXTERNAL PROVIDER;"
echo "      ALTER ROLE db_datareader ADD MEMBER [mi-${SUFFIX}-source-system-service];"
echo "      ALTER ROLE db_datawriter ADD MEMBER [mi-${SUFFIX}-source-system-service];"
echo "      ALTER ROLE db_ddladmin  ADD MEMBER [mi-${SUFFIX}-source-system-service];  -- needed for Alembic's CREATE TABLE"
echo "  - [ONE-TIME, HUMAN] Grant rbac-service's managed identity access to sqldb-rbac (same pattern as"
echo "    source-system-service above — sql-${SUFFIX} is VNet-only, run from inside it as an"
echo "    iga-platform-admins member):"
echo "      CREATE USER [mi-${SUFFIX}-rbac-service] FROM EXTERNAL PROVIDER;"
echo "      ALTER ROLE db_datareader ADD MEMBER [mi-${SUFFIX}-rbac-service];"
echo "      ALTER ROLE db_datawriter ADD MEMBER [mi-${SUFFIX}-rbac-service];"
echo "      ALTER ROLE db_ddladmin  ADD MEMBER [mi-${SUFFIX}-rbac-service];  -- needed for Alembic's CREATE TABLE"
echo "  - [HUMAN gate, Phase 3.1] rbac-service needs two NEW iga-platform-api app roles that don't exist yet"
echo "    (rbac.read, rbac.write) — defining an app role on an existing app registration is a Graph app update,"
echo "    directory perms needed. Run:"
echo "      API_APP_ID=\$(az ad app list --display-name iga-platform-api --query '[0].id' -o tsv)"
echo "      CUR_ROLES=\$(az ad app show --id \"\$API_APP_ID\" --query appRoles -o json)"
echo "      NEW_ROLES=\$(python3 -c \""
echo "import json,sys,uuid"
echo "roles = json.loads(sys.argv[1])"
echo "for v in ('rbac.read', 'rbac.write'):"
echo "    if not any(r['value'] == v for r in roles):"
echo "        roles.append({'id': str(uuid.uuid4()), 'allowedMemberTypes': ['Application'],"
echo "                      'displayName': v, 'value': v, 'description': v, 'isEnabled': True})"
echo "print(json.dumps(roles))"
echo "\" \"\$CUR_ROLES\")"
echo "      az ad app update --id \"\$API_APP_ID\" --app-roles \"\$NEW_ROLES\""
echo "    Then grant rbac-service's managed identity all four roles it needs (rbac.read/rbac.write for its"
echo "    own endpoints, identities.read to evaluate membership rules, provisioning.write to dispatch"
echo "    grant/revoke tasks) — same appRoleAssignment pattern as flatfile-connector-service above:"
echo "      API_SP_ID=\$(az ad sp list --filter \"displayName eq 'iga-platform-api'\" --query '[0].id' -o tsv)"
echo "      RBAC_SP_ID=\$(az ad sp list --filter \"displayName eq 'mi-${SUFFIX}-rbac-service'\" --query '[0].id' -o tsv)"
echo "      for ROLE in rbac.read rbac.write identities.read provisioning.write; do"
echo "        ROLE_ID=\$(az ad sp show --id \"\$API_SP_ID\" --query \"appRoles[?value=='\$ROLE'].id | [0]\" -o tsv)"
echo "        az rest --method POST --uri \"https://graph.microsoft.com/v1.0/servicePrincipals/\$RBAC_SP_ID/appRoleAssignments\" \\"
echo "          --body \"{\\\"principalId\\\":\\\"\$RBAC_SP_ID\\\",\\\"resourceId\\\":\\\"\$API_SP_ID\\\",\\\"appRoleId\\\":\\\"\$ROLE_ID\\\"}\""
echo "      done"
echo "    Until both steps are done, rbac-service's own endpoints 401 (no rbac.read/rbac.write role exists to"
echo "    grant yet) and its calls to identity-service/provisioning-service 403."
