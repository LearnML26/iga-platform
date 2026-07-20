# IGA Platform — Greenfield Build on Azure

Implements the *IGA Platform Requirements Specification v1.0*. This repo contains
everything needed to stand up the platform's Phase 1–2 foundation: complete Azure
infrastructure (Bicep), the first two microservices (Identity Service and
Provisioning Service with AD + Entra ID connectors), Kubernetes manifests, CI,
and a one-command deploy script.

## What's implemented vs. the spec

| Spec section | Status |
|---|---|
| §3 Infrastructure (network, AKS, SQL/Cosmos/ADLS/Redis, Service Bus/Event Hubs, Key Vault, observability) | ✅ Bicep, compiles clean |
| §5.1 Identity Data Store (CRUD, correlation, history, events, search) | ✅ `src/identity-service` |
| §5.2 Target Systems — connector contract + AD (LDAPS) + Entra ID (Graph) connectors | ✅ `src/provisioning-service/app/connectors.py` |
| §5.8 Provisioning Engine (queue, retry/backoff, DLQ, failure alerting hook) | ✅ `src/provisioning-service` |
| §4 UI, §5.3–5.7, §5.9 (portal, source systems, RBAC, certs, API engine, access requests, rules) | 🔜 Phase 3–4 — service skeletons follow the same pattern |

## Prerequisites

- Azure subscription (dedicated subscription recommended; set a budget alert)
- Entra ID security group for platform admins (you'll pass its **objectId**)
- Tooling on your machine or Cloud Shell: `az` ≥ 2.60, `docker`, `kubectl`, `envsubst`
- Quota: ~8 vCPU (D2s_v6) in your target region for the dev AKS pools

## Deploy (dev)

```bash
az login
az account set --subscription <SUBSCRIPTION_ID>
./scripts/deploy.sh dev eastus <ADMIN_GROUP_OBJECT_ID>
```

The script deploys infra, creates workload identities with federated credentials,
grants data-plane RBAC, builds/pushes images, and applies the k8s manifests.
It ends by printing the manual follow-ups (SQL Entra admin, AD bind credentials
into Key Vault, Graph permission consent for the Entra connector).

### Running it with Claude Code / a coding agent

The recommended agent workflow: authenticate `az` **locally**, then let the agent
drive `deploy.sh`, read errors, and iterate. Never paste service-principal
secrets into a chat window — the agent doesn't need them; it inherits your
local `az` session.

## Security model (matches spec §3.7 / §3.10)

- **No secrets in code or k8s manifests.** Services use AKS Workload Identity
  (federated credentials) → Entra ID → data-plane RBAC. Cosmos, Service Bus,
  and Event Hubs all have local/key auth **disabled**.
- The only stored secret is the AD connector's LDAP bind credential, held in
  Key Vault and mounted via the CSI Secret Store driver.
- All data services are private-endpoint only in prod (`publicNetworkAccess: Disabled`).

## Repo layout

```
infra/                  Bicep — main.bicep + modules (network, data, messaging,
                        security, observability, compute), env param files
src/identity-service/   FastAPI — identity store (Cosmos), history, events
src/provisioning-service/  FastAPI + worker — task queue, retry/DLQ, connectors
k8s/                    Namespace + per-service manifests (workload identity, HPA)
.github/workflows/      CI: Bicep build, lint, Trivy scan, image build/push
scripts/deploy.sh       One-command environment deployment
```

## Cost guidance (dev)

Dev is sized to minimize spend: serverless SQL (auto-pause), Cosmos autoscale
at 1000 RU max, Service Bus Standard, single-node pools. Expect roughly
**$300–600/month** depending on how long the cluster runs. Tear down with:

```bash
az group delete -n rg-iga-dev-network -n rg-iga-dev-data ... # or delete all four rg-iga-dev-* groups
```

## Next build phases

1. **Source System Service** — HR flat-file/REST ingestion → Identity Service (spec §5.3)
2. **RBAC Service** — role objects, membership rules, SoD (spec §5.4)
3. **Rules Engine** — Event Hubs consumer re-evaluating access on identity change (spec §5.9)
4. **Access Request + Certification services** and the React portals (spec §4, §5.5, §5.7)
5. **API engine** — APIM policies + OIDC surface (spec §5.6)

Each follows the identical pattern established here: FastAPI service, workload
identity, database-per-service, events in/out, k8s manifest, CI matrix entry.
