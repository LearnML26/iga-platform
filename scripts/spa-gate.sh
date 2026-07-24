#!/usr/bin/env bash
# ============================================================================
# [HUMAN gate, Phase 3.4] SPA app registration + user-assignable app roles.
#
# Run this yourself with an az session that has directory permissions
# (Application Administrator or owner of the iga-platform-api registration).
# It performs Graph app-registration surgery only — it never reads, writes,
# or prints a secret (SPA auth is auth-code+PKCE; no client secret exists).
#
#   ./scripts/spa-gate.sh [swa-default-hostname]
#
# What it does, and why:
#   1. Creates (idempotently) the `iga-platform-spa` public-client app
#      registration with SPA redirect URIs: http://localhost:5173 always,
#      plus https://<swa-default-hostname> if passed as $1.
#   2. On iga-platform-api: registers the api://<appId> identifier URI,
#      exposes one delegated scope `access_as_user`, and pre-authorizes the
#      SPA for it (pre-authorization = no consent prompt per user).
#   3. Makes every existing app role user-assignable (allowedMemberTypes
#      Application → Application+User). Graph requires a role to be disabled
#      before it can be modified, hence the two-pass disable→modify→enable.
#      Service-principal assignments (the workload identities) are untouched.
#   4. Assigns ALL app roles to the signed-in user (you), so your delegated
#      token carries them in its `roles` claim — the exact claim every
#      service's require_role() already validates. Zero backend changes.
#   5. Prints the three VITE_ values for web/.env.local.
# ============================================================================
set -euo pipefail
SWA_HOSTNAME="${1:-}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

TENANT_ID=$(az account show --query tenantId -o tsv)
API_APP_ID=$(az ad app list --display-name iga-platform-api --query '[0].appId' -o tsv)
API_OBJ_ID=$(az ad app list --display-name iga-platform-api --query '[0].id' -o tsv)
API_SP_ID=$(az ad sp list --filter "displayName eq 'iga-platform-api'" --query '[0].id' -o tsv)
[ -z "$API_APP_ID" ] && { echo "✘ iga-platform-api app registration not found"; exit 1; }

echo "== 1. SPA app registration =="
SPA_APP_ID=$(az ad app list --display-name iga-platform-spa --query '[0].appId' -o tsv)
if [ -z "$SPA_APP_ID" ]; then
  SPA_APP_ID=$(az ad app create --display-name iga-platform-spa \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)
  echo "  created iga-platform-spa ($SPA_APP_ID)"
else
  echo "  iga-platform-spa already exists ($SPA_APP_ID)"
fi
SPA_OBJ_ID=$(az ad app list --display-name iga-platform-spa --query '[0].id' -o tsv)
az ad sp create --id "$SPA_APP_ID" > /dev/null 2>&1 || true  # ensure enterprise app exists

if [ -n "$SWA_HOSTNAME" ]; then
  printf '{"spa":{"redirectUris":["http://localhost:5173","https://%s"]}}' "$SWA_HOSTNAME" > "$TMP/spa.json"
else
  printf '{"spa":{"redirectUris":["http://localhost:5173"]}}' > "$TMP/spa.json"
fi
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$SPA_OBJ_ID" \
  --headers 'Content-Type=application/json' --body @"$TMP/spa.json"
echo "  redirect URIs set"

echo "== 2. iga-platform-api: identifier URI + access_as_user scope + pre-auth =="
az ad app show --id "$API_OBJ_ID" -o json > "$TMP/api.json"
python3 - "$TMP/api.json" "$API_APP_ID" "$SPA_APP_ID" > "$TMP/api_patch.json" <<'PY'
import json, sys, uuid
app = json.load(open(sys.argv[1]))
api_app_id, spa_app_id = sys.argv[2], sys.argv[3]
uris = app.get("identifierUris") or []
uri = f"api://{api_app_id}"
if uri not in uris:
    uris.append(uri)
api = app.get("api") or {}
scopes = api.get("oauth2PermissionScopes") or []
scope = next((s for s in scopes if s["value"] == "access_as_user"), None)
if scope is None:
    scope = {
        "id": str(uuid.uuid4()), "value": "access_as_user", "type": "User",
        "adminConsentDisplayName": "Access the IGA platform APIs",
        "adminConsentDescription": "Allows the app to call the IGA platform APIs as the signed-in user.",
        "userConsentDisplayName": "Access the IGA platform APIs",
        "userConsentDescription": "Allows the app to call the IGA platform APIs on your behalf.",
        "isEnabled": True,
    }
    scopes.append(scope)
pre = api.get("preAuthorizedApplications") or []
entry = next((p for p in pre if p["appId"] == spa_app_id), None)
if entry is None:
    pre.append({"appId": spa_app_id, "delegatedPermissionIds": [scope["id"]]})
elif scope["id"] not in entry["delegatedPermissionIds"]:
    entry["delegatedPermissionIds"].append(scope["id"])
print(json.dumps({
    "identifierUris": uris,
    "api": {"oauth2PermissionScopes": scopes, "preAuthorizedApplications": pre},
}))
PY
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$API_OBJ_ID" \
  --headers 'Content-Type=application/json' --body @"$TMP/api_patch.json"
echo "  scope exposed + SPA pre-authorized"

echo "== 3. Make app roles user-assignable (disable → modify → enable) =="
az ad app show --id "$API_OBJ_ID" --query appRoles -o json > "$TMP/roles.json"
if python3 -c '
import json, sys
roles = json.load(open(sys.argv[1]))
sys.exit(0 if any("User" not in r["allowedMemberTypes"] for r in roles) else 1)
' "$TMP/roles.json"; then
  python3 - "$TMP/roles.json" <<'PY' > "$TMP/roles_disabled.json"
import json, sys
roles = json.load(open(sys.argv[1]))
for r in roles:
    if "User" not in r["allowedMemberTypes"]:
        r["isEnabled"] = False
print(json.dumps(roles))
PY
  az ad app update --id "$API_OBJ_ID" --app-roles "$(cat "$TMP/roles_disabled.json")"
  python3 - "$TMP/roles.json" <<'PY' > "$TMP/roles_enabled.json"
import json, sys
roles = json.load(open(sys.argv[1]))
for r in roles:
    if "User" not in r["allowedMemberTypes"]:
        r["allowedMemberTypes"] = ["Application", "User"]
    r["isEnabled"] = True
print(json.dumps(roles))
PY
  az ad app update --id "$API_OBJ_ID" --app-roles "$(cat "$TMP/roles_enabled.json")"
  echo "  all app roles now allow User principals"
else
  echo "  all app roles already user-assignable — skipped"
fi

echo "== 4. Assign every app role to the signed-in user =="
USER_ID=$(az ad signed-in-user show --query id -o tsv)
for ROLE in identities.read identities.write provisioning.write rbac.read rbac.write requests.read requests.write; do
  ROLE_ID=$(az ad sp show --id "$API_SP_ID" --query "appRoles[?value=='$ROLE'].id | [0]" -o tsv)
  if [ -z "$ROLE_ID" ]; then echo "  ! role $ROLE not found on iga-platform-api — skipped"; continue; fi
  printf '{"principalId":"%s","resourceId":"%s","appRoleId":"%s"}' "$USER_ID" "$API_SP_ID" "$ROLE_ID" > "$TMP/assign.json"
  if az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$API_SP_ID/appRoleAssignedTo" \
      --headers 'Content-Type=application/json' --body @"$TMP/assign.json" > /dev/null 2>&1; then
    echo "  $ROLE assigned"
  else
    echo "  $ROLE — already assigned (or POST failed; re-run to see the error)"
  fi
done

echo ""
echo "=== Done. Fill web/.env.local with: ==="
echo "VITE_TENANT_ID=$TENANT_ID"
echo "VITE_SPA_CLIENT_ID=$SPA_APP_ID"
echo "VITE_API_APP_ID=$API_APP_ID"
echo ""
echo "Then run the local dev loop:   ./scripts/dev-portal.sh"
echo ""
echo "To publish the static bundle to the Static Web App (content deploy is"
echo "human-run because the deployment token is a secret):"
echo "  cd web && npm install && npm run build"
echo "  TOKEN=\$(az staticwebapp secrets list --name swa-iga-dev-web --query properties.apiKey -o tsv)"
echo "  npx @azure/static-web-apps-cli deploy ./dist --deployment-token \"\$TOKEN\" --env production"
echo "NOTE: the publicly-hosted SPA can sign in but cannot reach the APIs until"
echo "Phase 4.5 (APIM) — the cluster has no public ingress by design. Local dev"
echo "via dev-portal.sh is the fully-functional path today."
