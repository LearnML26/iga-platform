# CLAUDE.md — Agent Operating Guide for the IGA Platform Build

You are the build agent for a greenfield Identity Governance and Administration
(IGA) platform on Azure. The requirements specification is
`IGA_Platform_Requirements_Specification.docx` (v1.0); requirement IDs
(REQ-INF-xxx, REQ-COR-xxx, REQ-UI-xxx) referenced throughout this repo map to
that document. Your backlog is `roadmap/PHASES.md`. Work through it top to
bottom unless the human directs otherwise.

## Current state (as of last human session)

- Dev environment is DEPLOYED and VERIFIED in Azure: subscription
  43c7ab61-eedc-4bf7-9373-8c8ff1163dbb, resource groups `rg-iga-dev-*`,
  AKS cluster `aks-iga-dev` (eastus), SQL server `sql-iga-dev` (canadacentral —
  eastus/eastus2 reject SQL creation on this subscription; do NOT move it back).
- identity-service and provisioning-service run in namespace `iga`,
  2 replicas each, smoke-tested green (create 201, dedupe 409).
- Regional vCPU quota in eastus is 10. AKS node pools are capped at
  maxCount 2/2 on Standard_D2s_v6. Do not raise pool sizes without asking.
- The human runs you from WSL2 Ubuntu with az CLI authenticated. `kubectl`
  is configured with kubelogin azurecli mode.

## Non-negotiable guardrails

1. NEVER handle secret values. Do not ask for, echo, or write passwords,
   bind credentials, connection strings, or keys. When a task needs a secret
   (e.g., AD bind password into Key Vault), STOP and print the exact
   `az keyvault secret set` command for the human to run themselves.
2. Steps requiring elevated Entra roles (Graph admin consent, directory role
   grants) are HUMAN-ONLY. Print the exact portal path or CLI command,
   explain what it grants, and wait.
3. Never delete resource groups, databases, Cosmos containers, or Key Vaults.
   Destructive operations require explicit human confirmation in the session.
4. Dev cost ceiling: do not create resources beyond the Bicep templates
   without asking. No Premium SKUs in dev. No new regions.
5. All infra changes go through Bicep in `infra/` — never `az resource create`
   ad hoc for anything that should persist. After editing Bicep, always run
   `bicep build infra/main.bicep --stdout > /dev/null` before deploying.
6. Never commit directly to main once a remote exists; use feature branches.

## Verification loop (run after EVERY change you deploy)

```bash
./scripts/verify.sh          # cluster health + API smoke tests
```

A task is not complete until verify.sh passes. If you changed a service,
also tail its logs for 60 seconds looking for errors:
`kubectl logs -n iga deploy/<service> --tail=50`

## Hard-won environment knowledge (do not re-learn these)

- **Bicep on this machine needs libicu** (already installed). If Bicep
  crashes with a .NET stack trace, check ICU first.
- **Async Azure SDKs require `aiohttp`** in requirements.txt. Every new
  Python service that uses `azure.*.aio` must include it.
- **Images run as named user `appuser`** but k8s `runAsNonRoot` needs numeric
  IDs: every Deployment must set `runAsUser: 1000` / `runAsGroup: 1000`.
- **The namespace has default-deny NetworkPolicy.** New services need their
  port covered by `allow-intra-namespace-app` (port 8080) or a new policy.
  New egress destinations (e.g., SQL port 1433) need explicit egress rules
  added to `k8s/base/namespace.yaml`.
- **Private endpoints need privateDnsZoneGroups** or pods resolve public IPs
  and get blocked. data.bicep has the pattern; replicate it for any new PE
  (Key Vault PE in security.bicep still needs this — see Phase 1R).
- **Cosmos/Service Bus/Event Hubs have local auth DISABLED.** Data-plane
  access is Entra RBAC via workload identity only. New services need:
  a user-assigned managed identity, a federated credential bound to their
  k8s ServiceAccount, and data-plane role assignments (see deploy.sh stages
  2–3 for the pattern).
- **RBAC propagation lags 2–5 minutes.** If a new pod 403s right after its
  role assignment, wait and `kubectl rollout restart` before debugging.
- **Build images with `az acr build`** (server-side) — no local Docker here.
- **deploy.sh is idempotent and re-run safe.** Full redeploy:
  `./scripts/deploy.sh dev eastus $ADMIN_GROUP` (ADMIN_GROUP =
  objectId of `iga-platform-admins`).

## Architecture conventions for new services

Follow the identity-service pattern exactly:
- Python 3.12 / FastAPI, async Azure SDKs, DefaultAzureCredential.
- `/healthz` + `/readyz` endpoints; readiness only after startup completes.
- Database-per-service: relational services get their own Azure SQL database
  (already provisioned: sqldb-targetsystem, sqldb-sourcesystem, sqldb-rbac,
  sqldb-accessrequest, sqldb-certification, sqldb-provisioning, sqldb-rules).
  Use SQLAlchemy async + aioodbc or asyncpg-style driver appropriate for
  Azure SQL, with Entra token auth (no SQL logins — server is Entra-only).
- Domain events out via Event Hubs `identity-changes` (or a new hub if the
  volume/domain warrants; add hubs via Bicep messaging module).
- Async work in via Service Bus queues (add queues via Bicep).
- One k8s manifest per service in `k8s/services/`, one Dockerfile per
  service in `src/<service>/`, one entry in the deploy.sh service loops and
  the CI matrix.
- Every REQ ID a task implements gets a comment reference in the code.

## Definition of done, per task

1. Code + Bicep + manifests merged and deployed to dev.
2. `verify.sh` green, including any new checks the task adds (each task that
   adds an API must extend verify.sh with a smoke test for it).
3. No secrets in code, config, or logs.
4. `roadmap/PHASES.md` checkbox ticked with a one-line completion note.
