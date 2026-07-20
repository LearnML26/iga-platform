# IGA Platform — Agent Backlog

Work top to bottom. Each task lists its spec requirement IDs and acceptance
criteria. Tick the box and add a one-line note when done. Tasks marked
**[HUMAN]** cannot be completed by the agent — print instructions and wait.

---

## Phase 1R — Remediation & hardening of what's deployed

- [x] **1R.1 Key Vault DNS zone group** — Add privateDnsZoneGroups to the KV
  private endpoint in `infra/modules/security.bicep` (copy the data.bicep
  pattern). Deploy; verify `az network private-dns record-set a list -g
  rg-iga-dev-network -z privatelink.vaultcore.azure.net` shows kv-iga-dev.
  Done: added `kvPeDns` resource; deployed (subscription deployment
  `iga-dev-1r1-1784519192`, Succeeded); confirmed `kv-iga-dev` A record now
  Bicep-managed (it existed pre-fix from manual CLI drift — Bicep now matches
  live state); verify.sh green.
- [ ] **1R.2 Verify all six DNS zones registered** — every zone in
  rg-iga-dev-network shows ≥2 record sets; create any missing zone groups
  via CLI AND ensure Bicep matches.
- [ ] **1R.3 API authentication (JWT validation)** — REQ-COR-API-001/002
  (minimal slice). Add Entra ID JWT validation middleware to identity-service
  and provisioning-service: validate tokens against the tenant's JWKS,
  require audience = a new app registration `iga-platform-api`, enforce scope
  `identities.read`/`identities.write`/`provisioning.write` per endpoint.
  Health probes stay anonymous. Extend verify.sh to assert 401 without token.
  [HUMAN gate: creating the app registration + scopes needs directory perms —
  print the az ad commands and wait.]
- [ ] **1R.4 Audit container immutability** — REQ-NFR-021. Apply
  version-level WORM policy to the `audit` container via CLI; document why
  it can't be pure Bicep (follow-up call), or implement via deployment script
  resource if clean.
- [ ] **1R.5 Repo to remote + CI live** — Push to GitHub/Azure Repos [HUMAN
  provides the remote URL + auth]. Confirm ci.yaml runs green. Configure the
  OIDC federated credential for the pipeline identity [HUMAN gate].
- [ ] **1R.6 Entra connector consent** — [HUMAN] Grant provisioning-service's
  managed identity Graph app permission GroupMember.ReadWrite.All + admin
  consent. Agent then: create a test task via POST /tasks targeting a test
  group/user pair the human supplies, verify the membership change lands,
  verify idempotent re-grant no-ops, verify a bad group id retries then
  dead-letters and emits a notification message.

## Phase 2 — Source systems & identity pipeline (spec §5.3)

- [ ] **2.1 source-system-service scaffold** — REQ-COR-SRC-001. FastAPI
  service owning SourceSystemInstance + AttributeMapping + FeedRun tables in
  sqldb-sourcesystem (SQLAlchemy async, Alembic migrations, Entra token auth
  to SQL). CRUD APIs. Workload identity + manifests + verify.sh checks.
- [ ] **2.2 Flat-file connector** — REQ-COR-SRC-002. Ingest CSV from the
  ADLS `raw/` container (blob drop), mapping-driven schema, malformed-row
  quarantine, checksum validation. FeedRun produces delta summary
  (REQ-COR-ID-006): added/updated/terminated/unmatched.
- [ ] **2.3 Feed → Identity Service integration** — REQ-COR-SRC-006. Apply
  deltas through identity-service APIs (never direct DB). Emit
  IdentityCreated/Updated/Terminated events. Failure threshold halts apply
  (REQ-COR-SRC-009).
- [ ] **2.4 Lifecycle handling** — REQ-COR-SRC-007/008. pending-start for
  future-dated joiners; scheduled termination triggering deprovisioning
  tasks on effective date (needs a scheduler loop — KEDA cron or in-service).
- [ ] **2.5 End-to-end JML demo** — Synthetic 50-row HR CSV: joiners create
  identities, a transfer row changes attributes, a leaver row terminates and
  generates a disable-account provisioning task. verify.sh gains a pipeline
  smoke test using a 3-row fixture.

## Phase 3 — RBAC, requests, and the portals (spec §5.4, §5.7, §4)

- [ ] **3.1 rbac-service** — REQ-COR-RBAC-001..004, 007..009. Role,
  RoleEntitlement, RoleMembershipRule, RoleAssignment, PlatformRole models in
  sqldb-rbac; versioning on change; membership-rule evaluation endpoint;
  assignment events → provisioning tasks.
- [ ] **3.2 access-request-service** — REQ-COR-REQ-001..003, 006, 007, 009.
  Request/LineItem/ApprovalStep models; default chain manager → owner
  (manager resolved from identity-service); notifications via
  notification queue; approval → provisioning task.
- [ ] **3.3 notification-service** — consumes notification-tasks queue,
  sends email via ACS Email or SMTP relay [HUMAN gate: provide sender config
  as Key Vault secrets]. Webhook fan-out for ProvisioningFailed.
- [ ] **3.4 React frontend scaffold** — REQ-UI-001..005, 010..017. Vite +
  React + TypeScript in `web/`. MSAL.js auth-code+PKCE against Entra
  [HUMAN gate: SPA app registration]. Unified login page per REQ-UI-010/013,
  persona routing per REQ-UI-014. Serve via Static Web App (add Bicep).
- [ ] **3.5 Admin console v1** — REQ-UI-020..025. Identities list/search/
  detail (history view), target system instances, provisioning task queue
  with retry/cancel, source system feed runs.
- [ ] **3.6 End-user portal v1** — REQ-UI-030..032. My access, request cart
  against requestable entitlements, my approvals queue.

## Phase 4 — Assurance: certifications, rules, API engine (spec §5.5, §5.9, §5.6)

- [ ] **4.1 rules-engine-service** — REQ-COR-RULES-001..003, 006, 007.
  Event Hubs consumer (consumer group `rules-engine`); RuleDefinition +
  RuleExecutionLog in sqldb-rules; attribute-change triggers re-running RBAC
  membership rules; scheduled sweep loop; every evaluation logged.
- [ ] **4.2 Rules: dry-run + guarded revocation** — REQ-COR-RULES-008/009.
  Simulation endpoint reporting affected identities; configurable delay
  window before critical-tier revocations dispatch.
- [ ] **4.3 certification-service** — REQ-COR-CERT-001..005, 007. Campaign
  definitions/instances/items in sqldb-certification; reviewer resolution
  (manager/owner with fallback); revoke decisions → provisioning tasks;
  reminder/escalation via notification queue; completion report export.
- [ ] **4.4 Certification UI** — REQ-UI-033. Reviewer queue with context
  data and bulk actions, wired into the portal.
- [ ] **4.5 API engine hardening** — REQ-COR-API-003..007. APIM in front of
  the services (Bicep: apim module into snet-apim), OpenAPI import, scoped
  products, rate limiting, delta-query endpoints. SCIM 2.0 /Users /Groups
  facade (REQ-COR-API-005). Outbound webhooks w/ HMAC (REQ-COR-API-008).

## Phase 5 — NFR validation & ops (spec §6, §7)

- [ ] **5.1 Load & performance validation** — REQ-NFR-002 slice: k6 or
  locust profile proving p95 <500ms reads at dev scale; document results.
- [ ] **5.2 Alert rules completion** — REQ-INF-082: DLQ >0, provisioning
  failure rate, connector failures — as Bicep monitor alerts wired to the
  action group.
- [ ] **5.3 Reports v1** — REQ-RPT-001 subset: access-by-identity and
  orphan/dormant reports from Data Lake curated zone; CSV export endpoint.
- [ ] **5.4 DR runbook** — REQ-INF-102/103 (doc-level for dev): scripted
  redeploy-from-scratch validation in a scratch resource group, teardown.
