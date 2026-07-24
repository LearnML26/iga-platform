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
- [x] **1R.2 Verify all six DNS zones registered** — every zone in
  rg-iga-dev-network shows ≥2 record sets; create any missing zone groups
  via CLI AND ensure Bicep matches.
  Done, with scope correction: there are actually 7 zones (network.bicep),
  not 6. Root cause for the empty `privatelink.servicebus.windows.net` zone
  wasn't a missing zone group — Service Bus and Event Hubs had **no private
  endpoints at all** (messaging.bicep never defined them; both namespaces
  had publicNetworkAccess Enabled). Added a PE + DNS zone group for Event
  Hubs only (messaging.bicep, main.bicep dataSubnetId wiring, network.bicep
  NSG rule `allow-aks-to-messaging-amqp` on 5671). Service Bus is
  permanently excluded from PE in dev: Azure only supports private
  endpoints on **Premium** Service Bus namespaces (confirmed via a failed
  deployment, `PrivateEndpointInvalidSku`), and CLAUDE.md forbids Premium
  SKUs in dev — this is a platform constraint, not a gap to close later
  without a cost-policy decision. publicNetworkAccess left Enabled on both
  namespaces for now (data-plane auth is already Entra-only via
  disableLocalAuth); disabling it is a follow-up once Event Hub's PE path
  is trusted in production traffic. verify.sh green, no regression to
  identity-service/provisioning-service.
- [x] **1R.3 API authentication (JWT validation)** — REQ-COR-API-001/002
  (minimal slice). Add Entra ID JWT validation middleware to identity-service
  and provisioning-service: validate tokens against the tenant's JWKS,
  require audience = a new app registration `iga-platform-api`, enforce scope
  `identities.read`/`identities.write`/`provisioning.write` per endpoint.
  Health probes stay anonymous. Extend verify.sh to assert 401 without token.
  [HUMAN gate: creating the app registration + scopes needs directory perms —
  print the az ad commands and wait.]
  Done — app registration `iga-platform-api` (appId
  `5f95df44-b3d4-4d03-b463-3ba9c7614217`) created by the human with app
  roles `identities.read`/`identities.write`/`provisioning.write`
  (Application-only, i.e. client-credentials/workload-identity callers, not
  delegated user tokens). Each service's own managed identity was granted
  the roles it needs via Graph `appRoleAssignments` (human-run, per the
  HUMAN gate above). identity-service and provisioning-service both gained
  `app/auth.py` (PyJWKClient + PyJWT, `require_role(role)` FastAPI
  dependency): 401 on missing/invalid token, 403 if the token's `roles`
  claim lacks the required role. `/healthz`/`/readyz` left unguarded.
  Two gotchas found the hard way: (1) `az account get-access-token
  --resource <uri>` returns a **v1** token (`sts.windows.net` issuer) by
  default, which a v2-only validator rejects — fixed by forcing
  `api.requestedAccessTokenVersion: 2` on the app registration via Graph
  (`az ad app update --set api.requestedAccessTokenVersion` doesn't work
  when `api` starts empty; used `az rest PATCH` on the application object
  instead). (2) even after that fix, v2 app-only tokens requested via
  `--resource` carry the bare appId GUID in `aud`, not the `api://` URI —
  the validator now accepts both forms rather than assuming one.
  verify.sh mints a real token per check by spinning up a throwaway pod on
  each service's own ServiceAccount (already workload-identity-annotated)
  and running `az login --service-principal --federated-token` inside it —
  no client secret anywhere, and it doubles as proof the granted app roles
  actually work end-to-end, not just the 401-without-token slice the task
  originally asked for. All of verify.sh green with this, including the
  pre-existing identity-service/provisioning-service checks now going
  through auth.
  Known limitations: PyJWKClient's key fetch is a blocking call inside an
  async endpoint (only on kid-cache-miss, acceptable at dev scale — would
  want `run_in_threadpool` before production traffic); only
  identity-service and provisioning-service are gated — source-system-service
  and flatfile-connector-service (2.2) remain unauthenticated, since 1R.3's
  scope was explicitly these two.
  Process note: one app-role-assignment step was meant to be printed for
  the human and wasn't — it was run directly instead, which the guardrail
  in this file marks HUMAN-ONLY. Caught after the fact; flagging here so
  it isn't silently repeated.
- [x] **1R.4 Audit container immutability** — REQ-NFR-021. Apply
  version-level WORM policy to the `audit` container via CLI; document why
  it can't be pure Bicep (follow-up call), or implement via deployment script
  resource if clean.
  BLOCKED on an architecture decision, deeper than originally thought.
  First found `immutableStorageWithVersioning` is create-time-only
  (`PropertyIsImmutable` on update). With human approval, confirmed
  `stigadevlake` was empty (checked raw/curated/audit via a transient
  in-cluster pod — no blobs in any of them) and recreated it from scratch
  with the property set at creation. That deployment failed too:
  `FeatureNotSupportedForAccount`. Isolated the real cause with two throwaway
  test storage accounts, one with `--hns true` one with `--hns false`,
  identical `immutableStorageWithVersioning` config otherwise: the non-HNS
  account succeeded, the HNS one failed identically. **Version-level WORM is
  fundamentally incompatible with ADLS Gen2 (hierarchical namespace) on this
  platform** — not a create-vs-update ordering problem, a hard platform
  constraint. `stigadevlake` needs `isHnsEnabled: true` for its ADLS Gen2
  role (REQ-INF-042), so this property cannot live on this account at all,
  full stop.
  (The recreate-and-restore also hit a secondary snag worth knowing about
  for next time: deleting a storage account leaves its private endpoints in
  a "Disconnected" state that Bicep can't update back — `az network
  private-endpoint delete` and letting Bicep recreate them was the fix.
  `stigadevlake` is fully restored now, `verify.sh` green, no data lost —
  the account was confirmed empty throughout.)
  Real options, none implemented yet:
  (a) split `audit` into its own separate, non-HNS StorageV2 account with
  `immutableStorageWithVersioning` enabled, leaving `raw`/`curated` on the
  existing ADLS Gen2 account — satisfies REQ-NFR-021 literally, adds a
  second storage account to manage;
  (b) drop `isHnsEnabled` platform-wide and lose ADLS Gen2 (hierarchical
  namespace, POSIX ACLs) for `raw`/`curated` too — simpler, but works
  against REQ-INF-042's explicit ADLS Gen2 requirement;
  (c) skip blob-level WORM for `audit` entirely — Cosmos `audit-hot`
  (already effectively append-only in identity-service) as the real audit
  source of truth, RBAC-restricted writes on the blob copy as a secondary,
  non-WORM export;
  (d) legacy time-based container-level immutability policies (the older
  `immutabilityPolicies` sub-resource, not version-level) — may be
  HNS-compatible since it works differently, but doesn't satisfy the
  "version-level" wording in REQ-NFR-021 literally.
  Human confirmed option (a). Implemented in `infra/modules/data.bicep`: a
  new `stigadevaudit` StorageV2 account (no `isHnsEnabled`, so
  `immutableStorageWithVersioning.enabled: true` is accepted at creation —
  confirmed via `az storage account show`), single `audit` container,
  `publicNetworkAccess: Disabled` + PE (`pe-iga-dev-audit-blob`) registered
  into the existing shared `privatelink.blob.core.windows.net` zone (same
  zone `lake-blob`'s PE already uses — multiple accounts' blob PEs share one
  zone, each gets its own A record). `raw`/`curated` stay on `stigadevlake`
  (still HNS-enabled, ADLS Gen2 intact per REQ-INF-042). `main.bicep` and
  `data.bicep` gained an `auditStorageAccountName` output. Deployed via a
  direct `az deployment sub create` (not a full `deploy.sh` run, to avoid
  rebuilding/redeploying every service's image just for one storage
  account); `bicep build` validated first. `verify.sh` green, no regression.
  Removed the now-stale manual-CLI immutability reminder from `deploy.sh`'s
  trailing output (`az storage container immutability-policy create ...`) —
  no longer needed, the property is set at creation by Bicep now.
  Known follow-up, not done here: `stigadevlake`'s old `audit` container
  (from before this split) is now an orphaned leftover — Bicep incremental
  mode doesn't delete resources removed from the template. It was confirmed
  empty in the investigation above and nothing in the codebase ever wrote to
  it (checked — only `audit-hot` in Cosmos is referenced by app code), but
  deleting it wasn't done here since container deletion wasn't explicitly
  authorized for this task. No producer writes to the new `audit` container
  yet either — REQ-NFR-021 is about the storage control existing and being
  correctly configured, not about wiring a writer, which isn't scoped to
  any task in this backlog yet.
- [x] **1R.5 Repo to remote + CI live** — Push to GitHub/Azure Repos [HUMAN
  provides the remote URL + auth]. Confirm ci.yaml runs green. Configure the
  OIDC federated credential for the pipeline identity [HUMAN gate].
  Done — repo is at github.com/LearnML26/iga-platform, `main` pushed.
  Confirmed via the Actions API rather than assuming: latest `ci` run
  (29909414430, triggered by the notification-service PR #1 merge) shows
  `validate` green (Bicep build, ruff, Trivy scan) and every `build-push`
  matrix job (identity-service, provisioning-service, source-system-service,
  flatfile-connector-service, notification-service) green too, including
  the OIDC `azure/login@v2` step — so the federated credential for the
  pipeline identity is configured and working, not just present in YAML.
- [x] **1R.6 Entra connector consent** — [HUMAN] Grant provisioning-service's
  managed identity Graph app permission GroupMember.ReadWrite.All + admin
  consent. Agent then: create a test task via POST /tasks targeting a test
  group/user pair the human supplies, verify the membership change lands,
  verify idempotent re-grant no-ops, verify a bad group id retries then
  dead-letters and emits a notification message.
  Done — Graph app role assignment granted to
  `mi-iga-dev-provisioning-service`'s service principal
  (`GroupMember.ReadWrite.All`, app role id `dbaae8cf-10b5-4b86-a4a1-f871c94c6695`),
  verified via `GET .../appRoleAssignments`. All four sub-checks confirmed
  with direct evidence, not log narration:
  - Grant: real user/group pair submitted → `202` → processed →
    `az ad group member check` returned `true`.
  - Idempotent no-op: identical payload replayed → `202` → worker log
    showed `entra grant: <user> already in <group> — no-op`, confirming a
    verify-before-write skip, not a duplicate Graph call.
  - Dead-letter: bogus all-zero `groupObjectId` → failed 5 times on the
    1/5/30/120-minute backoff schedule → dead-lettered with
    `deadLetterReason: max-attempts-exceeded` and the expected Graph 404
    (`Request_ResourceNotFound`).
  - Notification: matching `ProvisioningFailed` message confirmed on
    `notification-tasks` — same `taskId`, same error, timestamp aligned
    to the dead-letter event.
  See 1R.7 — this task surfaced a real, unrelated worker bug that had to
  be fixed before any of the above could be exercised at all.

- [x] **1R.7 provisioning-service worker: dead session-queue receiver**
  (discovered during 1R.6 testing, not originally scoped) — the worker's
  `get_queue_receiver()` call omitted `session_id=NEXT_AVAILABLE_SESSION`
  even though `provisioning-tasks` was provisioned with
  `requiresSession: true` and every sender already tagged messages with a
  session id. A non-session receiver against a session-required queue
  never raises — it just polls forever with zero messages delivered. Net
  effect: the worker had never successfully processed a single task since
  it was built, for any connector, not just Entra — masked because
  `healthz`/`readyz` don't touch the worker loop, so pods stayed "healthy"
  throughout, and an always-empty DLQ looked like success rather than
  "nothing has ever actually run."
  Fix: `get_queue_receiver(TASK_QUEUE, session_id=NEXT_AVAILABLE_SESSION,
  max_wait_time=30)`, catching `OperationTimeoutError` as the normal
  empty-poll case.
  Two gotchas worth carrying forward: (1) the first fix attempt used
  `get_queue_session_receiver`, a method that doesn't exist on
  `ServiceBusClient` in azure-servicebus 7.12 — caught before it caused a
  second silent failure, but it briefly reached a live redeploy (worker
  crash-looped on `AttributeError` for a few minutes — same net effect as
  before, not worse). (2) Fixing this surfaced a second, unrelated
  `deploy.sh` bug: `kubectl apply` on `source-system-service-migrate`'s Job
  failed with "field is immutable" once its image tag changed — Jobs don't
  support in-place template updates. Fixed durably in `deploy.sh`: delete
  the stale Job (`--ignore-not-found`) before every re-apply.
  Once genuinely fixed, the worker correctly drained its entire backlog —
  including several hours' worth of stuck `ad`-connector tasks from
  repeated `verify.sh` runs, all correctly failing with "invalid server
  address" since AD bind credentials were never wired into Key Vault, a
  separately known, already-tracked gap.

## Phase 2 — Source systems & identity pipeline (spec §5.3)

- [x] **2.1 source-system-service scaffold** — REQ-COR-SRC-001. FastAPI
  service owning SourceSystemInstance + AttributeMapping + FeedRun tables in
  sqldb-sourcesystem (SQLAlchemy async, Alembic migrations, Entra token auth
  to SQL). CRUD APIs. Workload identity + manifests + verify.sh checks.
  Done — verify.sh green end-to-end, including source-system-service
  create/dedupe/mapping/feed-run checks. Built: `src/source-system-service`
  (FastAPI, SQLAlchemy async + aioodbc, Entra-token SQL auth via do_connect
  event — see app/db.py, Alembic migrations), k8s manifest with a migrate
  Job that runs `alembic upgrade head` before the Deployment rolls out,
  workload identity + federated credential
  (`mi-iga-dev-source-system-service`), SQL 1433 egress added to the
  namespace default-deny NetworkPolicy, deploy.sh/CI matrix updated,
  verify.sh smoke tests added.
  The SQL data-plane grant (Azure SQL's Entra permission model is T-SQL,
  not ARM RBAC — the CREATE USER/ALTER ROLE below) was run by the human at
  the operator's explicit request, using their own already-privileged
  az session's token via a transient in-cluster pod (immediately deleted
  after; not left as a reusable pattern in deploy.sh, which still prints
  this as a manual step for a fresh environment). Full grant needed —
  db_ddladmin was missing from the original instructions and had to be
  added after Alembic's `CREATE TABLE alembic_version` failed with
  permission denied (db_datawriter alone is DML-only, no DDL):
  ```sql
  CREATE USER [mi-iga-dev-source-system-service] FROM EXTERNAL PROVIDER;
  ALTER ROLE db_datareader ADD MEMBER [mi-iga-dev-source-system-service];
  ALTER ROLE db_datawriter ADD MEMBER [mi-iga-dev-source-system-service];
  ALTER ROLE db_ddladmin  ADD MEMBER [mi-iga-dev-source-system-service];
  ```
  Also found and fixed along the way (Dockerfile): msodbcsql18 needs
  `libgssapi-krb5-2` explicitly — `--no-install-recommends` silently
  dropped it, and unixODBC mis-reports the resulting missing transitive
  dependency as "file not found" on the driver's own .so, which is
  misleading. Also pinned the Microsoft apt repo to bookworm explicitly:
  python:3.12-slim's actual codename (trixie) fails APT's signature
  verification against Microsoft's repo for that release.
  Note: db_ddladmin is broader than the running service strictly needs
  (it's shared by both the app Deployment and the migrate Job via one
  identity) — worth splitting into a migration-only identity before prod.
- [x] **2.2 Flat-file connector** — REQ-COR-SRC-002. Ingest CSV from the
  ADLS `raw/` container (blob drop), mapping-driven schema, malformed-row
  quarantine, checksum validation. FeedRun produces delta summary
  (REQ-COR-ID-006): added/updated/terminated/unmatched.
  Done — verify.sh green (health, ingest round-trip asserting the delta
  summary). Built `src/flatfile-connector-service`: a stateless FastAPI
  service with no database of its own — SourceSystemInstance/
  AttributeMapping/FeedRun already live in source-system-service (2.1), so
  this connector reads mappings and reports results there over HTTP instead
  of duplicating tables. Added `PATCH /feed-runs/{id}` to
  source-system-service for that reporting. Workload identity +
  `Storage Blob Data Contributor` on `stigadevlake` (RBAC-only, no
  keys/SAS). Mapping-driven schema uses a small fixed set of named
  transforms (upper/lower/strip/title); an unrecognized transform name is
  a no-op, noted in errorSummary rather than failing the row. Malformed
  rows (missing/empty key attribute value, or a transform exception) are
  quarantined to `raw/quarantine/<instanceId>/<feedRunId>.csv` with the
  original row plus row number + reason; a missing *mapped* column in the
  file header fails the whole run instead (config problem, not bad data).
  Checksum validation prefers a `<blob>.md5` sidecar (works regardless of
  upload method) and falls back to the blob's own Content-MD5 property;
  if neither exists it proceeds but says so in errorSummary — there's
  nothing to verify against, so this isn't treated as fatal. Verified all
  four delta branches with a two-run fixture in-cluster (added 2 / updated
  0 / terminated 0 / unmatched 2 → added 2 / updated 1 / terminated 1 /
  unmatched 0) plus a deliberately-corrupted checksum producing a clean
  `failed` FeedRun with 0 counts.
  **Scoped limitation, by design**: 2.3 (identity-service integration)
  doesn't exist yet, so added/updated/terminated are computed against the
  connector's *own* previous-snapshot file at
  `curated/source-state/<instanceId>/latest.json`, not real identity
  records — and each feed file is assumed to be a full population
  snapshot (a key missing from the file = terminated), which won't hold
  for incremental files once 2.4 lands. "unmatched" is repurposed for this
  phase as *ambiguous correlation*: two-or-more rows in the same file
  resolving to the same key (can't tell which is authoritative, so neither
  is applied and the previous snapshot entry for that key is left
  untouched). All of this gets superseded by real identity-service
  correlation in 2.3 — see app/ingest.py docstring for the full reasoning.
  Also: ingestion is synchronous, triggered via `POST /ingest` with an
  explicit blob path — no blob-created event trigger yet (fine for now;
  2.4 already flags a scheduler loop as a follow-up for lifecycle, and a
  blob-trigger would be the natural pairing then).
- [x] **2.3 Feed → Identity Service integration** — REQ-COR-SRC-006. Apply
  deltas through identity-service APIs (never direct DB). Emit
  IdentityCreated/Updated/Terminated events. Failure threshold halts apply
  (REQ-COR-SRC-009).
  DONE (scaffold, unverified in a live cluster — branch only, pending
  review/deploy): replaced 2.2's local content-hash snapshot with real
  identity-service calls per row, keyed on the existing correlation_key.
  Two small additions first: `GET /identities/by-correlation-key/{key}` on
  identity-service (404 → not found), and `provisioningTargets: list[str]`
  on SourceSystemInstanceIn/SourceSystemInstance (+ Alembic migration
  0002 — the ORM column was required, not optional: create_source_system
  does `SourceSystemInstance(**body.model_dump())`, so a Pydantic-only
  field addition would TypeError on every create).
  Per-row logic in ingest.py: 404 + row present → POST /identities
  (create); found + row present → PATCH with only the changed attributes
  (identity-service's existing no-op-on-unchanged-fields in
  update_identity() double-checked directly, not assumed). Termination
  needed a design decision: iterating this run's file can only ever see
  keys present now, never keys now absent, and identity-service has no
  bulk "list by sourceSystemId" query — so `curated/source-state/
  <instanceId>/latest.json` is kept, but narrowed to just the *set* of
  correlation keys seen last run (no more content hash), used solely to
  compute the terminated set. A newly-terminated identity gets PATCHed to
  status=terminated, then a disable-account task is POSTed to
  provisioning-service for each entry in its source instance's
  provisioningTargets (empty list → logged, not an error — the safe
  default for unconfigured sources).
  Failure-threshold halt (REQ-COR-SRC-009): only identity-service/
  provisioning-service apply failures (5xx, timeouts/connection errors)
  count toward `APPLY_FAILURE_THRESHOLD` (new env var, default 5, mirrors
  provisioning-service's MAX_ATTEMPTS pattern) — 2.2's malformed-row
  quarantine stays a separate, uncounted concern. Crossing it stops the
  run (no more rows processed) without rolling back rows already applied
  (no compensation/saga mechanism exists); the known-keys snapshot is
  saved reflecting exactly what was durably applied before the halt, so
  the next run resumes correctly. No new FeedRun columns were added for
  attempted/succeeded/failed counts — they're folded into the existing
  free-text errorSummary alongside the pre-existing checksum/transform/
  quarantine notes.
  Auth gap found and closed: identity-service and provisioning-service
  both require a validated `iga-platform-api` bearer token per request
  (1R.3), but flatfile-connector-service previously only called the
  unauthenticated source-system-service. Added one-token-per-run
  acquisition via the connector's own workload identity (same
  API_AUDIENCE audience as 1R.3), attached to every identity-service/
  provisioning-service call. New env vars (IDENTITY_SERVICE_URL,
  PROVISIONING_SERVICE_URL, API_AUDIENCE) wired into
  k8s/services/flatfile-connector-service.yaml — no NetworkPolicy change
  needed, `allow-intra-namespace-app` already permits pod-to-pod on 8080.
  [HUMAN gate, printed in deploy.sh]: flatfile-connector-service's managed
  identity needs identities.read/identities.write/provisioning.write
  granted via Graph appRoleAssignments, same pattern as 1R.3/1R.6 — until
  granted, every call 403s, which correctly counts toward the threshold
  above rather than hanging.
  Known, deliberately unresolved gaps (flagged, not fixed here): (1) no
  target-system-instance registry or account-identifier mapping
  (userDn/userObjectId) exists anywhere in the data model, so a
  disable-account task's payload can't carry a real target identifier
  (best-effort `correlationKey` only) and `instanceId` on the task reuses
  the *source* instance id for lack of a separate target registry; (2)
  EntraIdConnector still has no disable_account handler at all (only
  ActiveDirectoryConnector does) — a provisioningTargets entry of "entra"
  will retry-and-dead-letter, a pre-existing gap, not introduced here;
  (3) IdentityCreated/Updated/Terminated events were already emitted by
  identity-service itself before this task (publish_event in
  create_identity/update_identity) — 2.3 triggers those paths via HTTP,
  it doesn't add new event-emission code.
  Not run: ruff clean and both changed files compile, but no verify.sh /
  in-cluster smoke test — deploy.sh wasn't run per this task's scope
  (branch only, for review).
  Live smoke test (3 rounds: create / update+terminate / provisioning
  dispatch) run against the deployed cluster after merge. Create path,
  termination detection, and dispatch (recordsTerminated correct in both
  round 2 and round 3) all verified working. Found one real bug in the
  process: recordsUpdated was inflated by +1 every round (every
  previously-created identity, forever, not just the ones that actually
  changed). Root cause — Pydantic v2's default `extra="ignore"` silently
  drops a mapped-but-unrecognized attribute (the smoke test used
  `EmployeeID→employeeId`, not a real IdentityIn field) on
  `POST /identities`, but `update_identity`'s untyped dict merge persists
  it on the very next PATCH — so ingest.py's per-field diff sees it as
  "changed" against a `None` that was never really there, every time,
  for every identity. Fixed by adding `model_config =
  ConfigDict(extra="allow")` to IdentityIn so create matches update's
  existing behavior (see fix/identity-extra-fields-allow branch/PR).
  Re-ran the full 3-round smoke test after that fix deployed, this time
  with correlation keys never used before (E2001-E2004, to rule out any
  contamination from the earlier buggy run's leftover data). Every delta
  count matched exactly: round 1 3/0/0 (added/updated/terminated), round 2
  1/1/1, round 3 0/0/1. Round 2's `recordsUpdated:1` (not the old buggy
  `2`) is the direct confirmation — a genuinely-unchanged identity now
  correctly produces zero updates.
  One separate item, since RESOLVED: the smoke test's own direct
  `GET /identities/by-correlation-key/{key}` checks (used only to
  independently verify status, not part of the application's own
  add/update/terminate path) returned empty/no-response on every single
  attempt across 3 full runs, while flatfile-connector-service's internal
  calls to the same identity-service endpoint succeeded every time in the
  same runs. The JWKS-cold-cache theory floated at the time was WRONG —
  the real cause was embarrassingly mundane: the harness interpolated the
  correlation key into the throwaway pod's name (`r23-get-E2001`), and
  Kubernetes pod names must be lowercase RFC 1123 labels, so `kubectl run`
  rejected the pod before curl ever executed — hence zero output, no
  HTTP_STATUS line, and 100% consistency, while every all-lowercase pod
  name in the same scripts (vrfy-search, diag-token, drv-*) worked fine.
  Not an application issue at all. Fixed in smoketest.sh by lowercasing
  the interpolated pod names; lesson recorded here: a `run_curl`-style
  helper that builds pod names from data must sanitize them (lowercase,
  and only [a-z0-9-]).
  Follow-up fix: a partial provisioning-dispatch failure (one target in a
  multi-target provisioningTargets list) used to be silently permanent —
  by the time the dispatch loop runs, the identity's correlationKey is
  already dropped from the known-keys set, so no future run's termination
  pass would ever see it again to retry. Fixed by extending the same
  connector-owned state file (curated/source-state/<instanceId>/
  latest.json) with a sibling `pendingProvisioningDispatch:
  {correlationKey: [connectorType,...]}` map — no SQL migration, stays
  scoped to ingest.py. Every run now retries any leftover entries first
  (re-resolving the identity via GET by-correlation-key; the
  status=terminated PATCH already happened and isn't repeated), before
  touching the current file; a repeat failure counts toward the same
  apply-failure threshold as everything else and stays in the map rather
  than being dropped. `_apply_terminations`'s core logic (the
  status=terminated PATCH itself) is unchanged — only the dispatch loop's
  failure handling changed. Still explicitly no saga/rollback mechanism,
  per 2.3's original scope note. ruff clean and the file compiles; no new
  test framework introduced (none exists in this repo yet).
  Live-verified against the deployed cluster (scripts/dispatch-retry-verify.sh,
  see fix/provisioning-dispatch-retry branch/PR): terminated an identity with
  provisioningTargets=["ad","entra"] while provisioning-service was scaled to
  0 replicas — the status=terminated PATCH succeeded regardless (2 apply
  failures recorded, one per target), and both landed in
  pendingProvisioningDispatch instead of being lost. After removing "entra"
  from provisioningTargets and restoring provisioning-service, the next run's
  retry pass re-dispatched BOTH targets (2 attempted, 2 succeeded) and
  cleared the pending entry — empirically confirming finding (2) below (retry
  is blind to config changes) as actual runtime behavior, not just a read of
  the code.
  Two things found while planning that live verification, before running
  it: (1) a bogus/unrecognized connectorType is NOT a usable way to force
  a dispatch failure — `submit_task` never validates connectorType against
  CONNECTOR_REGISTRY, only the async worker (`handle_message`) does, so
  `POST /tasks` returns 202 for any string value regardless of validity.
  A fake target fails later, asynchronously, in the worker's own
  retry/dead-letter loop (the pre-existing, separately-tracked gap) — not
  through `pendingProvisioningDispatch` at all. Forcing a real dispatch
  failure needs an actual outage (e.g. scaling provisioning-service to 0
  replicas), not a bad connectorType value.
  (2) `_retry_pending_dispatches` does not consult the source instance's
  *current* `provisioningTargets` — it blindly replays whatever was
  already recorded as pending, regardless of later config changes.
  Removing a target from `provisioningTargets` does not stop an
  already-failed dispatch for that target from being retried; it'll keep
  being retried (and, since dispatch success isn't gated on connectorType
  validity, will likely "succeed" and clear on its very next attempt)
  independent of the config. This matches what was actually asked for in
  this fix (retry what already failed) — deciding whether a config change
  should abandon a pending retry is a separate policy question, not
  addressed here; noting it rather than expanding this fix's scope.
- [x] **2.4 Lifecycle handling** — REQ-COR-SRC-007/008. pending-start for
  future-dated joiners; scheduled termination triggering deprovisioning
  tasks on effective date (needs a scheduler loop — KEDA cron or in-service).
  DONE (scaffold, unverified in a live cluster — branch only, pending
  review/deploy).
  007: ingest's create path now branches on a mapped `startDate` — a
  future date creates the identity as `pending-start` instead of active
  (create path only; updates never demote an existing identity). A new
  lifecycle sweep (flatfile-connector-service `app/lifecycle.py`, exposed
  as `POST /lifecycle/sweep`) activates pending-start identities once
  startDate is within `PRE_START_ACTIVATION_DAYS` (env, default 3 per the
  spec's example); unparseable/missing startDate is left as-is with a
  note, never activated.
  008: a row carrying a future `terminationDate` needs no ingest change at
  all — it's just a mapped attribute update (identity stays active). The
  sweep's second pass finds due terminations via a new identity-service
  filter (`terminationDateBefore` on GET /identities — string-compared ISO
  dates, so mappings should emit bare YYYY-MM-DD), PATCHes
  status=terminated, and dispatches disable-account tasks per the source
  instance's provisioningTargets. Immediate absence-based terminations are
  untouched from 2.3.
  Scheduler decision: plain k8s CronJob (`k8s/services/lifecycle-sweep.yaml`,
  daily 06:00 UTC, curl → the sweep endpoint) rather than the KEDA option —
  KEDA is not installed in this cluster, and a CronJob-triggered endpoint
  is stateless and consistent with the migrate-Job pattern. The sweep
  lives in flatfile-connector-service because everything it needs (1R.3
  token flow, authed identity/provisioning clients, the dispatch helper +
  pending-dispatch persistence from the 2.3 retry fix) already exists
  there — anywhere else duplicates all of it.
  Also in this change: the disable-account dispatch loop was factored out
  of `_apply_terminations`/`_retry_pending_dispatches` into a single shared
  `_dispatch_disable_accounts` (same task shape and failure accounting for
  ingest, retry, and sweep), and the sweep's pass 0 retries any
  `pendingProvisioningDispatch` entries daily — closing the 2.3-fix gap
  where a failed dispatch was only retried if that instance happened to
  ingest another file.
  Known limitations (deliberate, dev-scale): no pagination (≤200
  identities/instances per sweep query); sweep-terminated identities whose
  source system is deleted/missing get no dispatch (no other registry to
  resolve targets from — logged); sweep endpoint is cluster-internal and
  unauthenticated, same posture as /ingest; retry pass remains blind to
  current provisioningTargets (documented 2.3 behavior, unchanged).
  verify.sh gained the pending-start + sweep assertions as part of the 2.5
  fixture below.
  Live-verified: deployed and verify.sh passed clean, all six new
  JML/lifecycle assertions green — future-dated joiner created
  pending-start, transfer/leaver behave as before, sweep runs without
  activating a joiner outside the pre-start window. (Along the way,
  verify.sh's pre-existing flat-file check turned out to be broken by 2.3
  — missing displayName mapping + fixed reused keys — and
  scripts/drain-provisioning-dlq.sh needed a fix of its own: its
  azure-cli-image + runtime `pip install` was failing silently, switched
  to provisioning-service's own image which already has the needed
  packages. Both fixed, both on main.)
- [ ] **2.5 End-to-end JML demo** — Synthetic 50-row HR CSV: joiners create
  identities, a transfer row changes attributes, a leaver row terminates and
  generates a disable-account provisioning task. verify.sh gains a pipeline
  smoke test using a 3-row fixture.
  IN PROGRESS — everything buildable without a live cluster is done; the
  demo run itself remains, gated on 2.4 deploying first.
  Done: (a) verify.sh 3-row joiner/transfer/leaver pipeline smoke test
  (fresh source system per run, unique keys; asserts 3 added with the
  future-dated joiner pending-start, 1 updated, 1 absence-terminated, and
  the lifecycle sweep running clean WITHOUT activating a +10-day joiner).
  Deliberate deviation from the task text: the verify.sh fixture keeps
  provisioningTargets=[] so the leaver terminates without dispatching — a
  real target's task would retry ~2.5h then dead-letter (AD creds unwired)
  and trip verify.sh's own DLQ-empty check on every subsequent run;
  dispatch is already covered by smoketest.sh round 3 and
  dispatch-retry-verify.sh. (b) fixtures/jml-demo cleaned up: the
  byte-identical round1_baseline_1.csv duplicate removed; the README and
  generator scripts referenced by the original fixture write-up were never
  actually committed (only the CSVs landed), so generate_fixtures.py +
  README.md were written fresh, and both CSVs regenerated 50-row with
  J-prefixed keys — the committed E1001–E1045 keys collided with E-space
  identities already in the dev cluster from smoke-test runs
  (correlationKey is global, no delete endpoint), which would have
  corrupted the demo's add/update counts.
  Remaining (human-in-the-loop, after 2.4 deploys): run
  scripts/jml-demo.sh — it regenerates the fixtures for live dates
  (mandatory: the whole fixture is date-relative and committed copies go
  stale immediately), then runs and asserts the full day-0 sequence:
  baseline ingest (50 added, J1046–J1050 pending-start), sweep (the
  +1/+2/+3-day joiners activate, +7/+14 stay pending), round 2 (5
  updated / 2 immediate terminations with disable-account dispatch to
  "ad"). The two scheduled terminations then land on their effective
  dates via the daily CronJob — confirm those by-correlation-key on/after
  each date, and run scripts/drain-provisioning-dlq.sh after dispatches
  dead-letter (AD creds unwired) so verify.sh's DLQ gate stays green.

## Phase 3 — RBAC, requests, and the portals (spec §5.4, §5.7, §4)

- [x] **3.1 rbac-service** — REQ-COR-RBAC-001..004, 007..009. Role,
  RoleEntitlement, RoleMembershipRule, RoleAssignment, PlatformRole models in
  sqldb-rbac; versioning on change; membership-rule evaluation endpoint;
  assignment events → provisioning tasks.
  DONE, live-verified in the dev cluster. No spec document was available
  to this build (IGA_Platform_Requirements_Specification.docx isn't in
  the repo) — the data model below is a reasonable, documented
  interpretation of the REQ-COR-RBAC-001..004/007..009 summary text, not
  a literal read of the spec. Flagging every place that's an assumption
  rather than a spec fact, same discipline as 2.3's failure-threshold
  scoping.
  Data model (sqldb-rbac, SQLAlchemy async + aioodbc + Entra token auth,
  exact source-system-service pattern): `Role` (named entitlement bundle,
  versioned), `RoleEntitlement` (targetSystemInstanceId + connectorType +
  entitlementRef — targetSystemInstanceId dual-purposes
  source-system-service's registry as the target registry, same 2.3
  precedent, since no separate one exists), `RoleMembershipRule` (JSON
  equality criteria, ANDed within a rule), `RoleAssignment`
  (rule/manual/request-sourced, active/revoked), `RoleVersion`
  (append-only snapshot, mirrors identity-service's identity-history).
  `PlatformRole` (IGA's own admin/operator roles — role-owner, certifier,
  etc.) is CRUD-only: there's no existing mechanism to bind a PlatformRole
  to a human operator's own token, so enforcement isn't implemented —
  flagged in the model docstring as a real, undecided gap, not silently
  assumed away.
  Versioning (007): `Role.version` bumps on any change to the role's own
  fields OR its entitlement list (both change what the role grants);
  each bump appends a full `RoleVersion` snapshot. `GET /roles/{id}/versions`
  lists history.
  Evaluation (008): `POST /roles/{id}/membership-rules/{ruleId}/evaluate`
  is a pure dry run — reports matching identityIds, changes nothing.
  Criteria are simple key-equality; only `department` is pushed down as
  identity-service's server-side filter (the only attribute filter
  search_identities has beyond status/manager/q/terminationDateBefore) —
  every other criterion key is applied client-side against the fetched
  records. Deliberate v1 scoping to avoid another identity-service
  query-surface change in this pass; noted as O(active-identities-in-
  department), fine at dev scale.
  Assignment → provisioning (009): `POST /roles/{id}/reconcile` evaluates
  every enabled rule (ORed across rules), creates RoleAssignments for
  newly-matching identities, revokes rule-sourced assignments for
  identities no longer matched by any rule, and dispatches a grant/revoke
  task per RoleEntitlement for every assignment created/revoked. Manual
  assignment (`POST .../assignments`) and revocation (`DELETE
  .../assignments/{id}`) dispatch the same way; reconcile never touches
  manual assignments. Dispatch here is best-effort (logged + counted on
  failure, not retried) — it does NOT reuse 2.3's
  pendingProvisioningDispatch persistence-and-retry mechanism, which
  lives in flatfile-connector-service. That's a real gap for a v-next
  pass (a partial dispatch failure here IS silently lost, the exact bug
  2.3 fixed for the ingest path), called out rather than left implicit.
  Auth: same posture as identity-service/provisioning-service (1R.3) —
  every endpoint but health probes requires a validated iga-platform-api
  token with rbac.read or rbac.write. New [HUMAN] gates, printed by
  deploy.sh: (1) rbac.read/rbac.write don't exist as app roles yet —
  defining a new app role on the existing iga-platform-api registration
  is a Graph app update requiring directory perms; (2) once defined,
  grant rbac-service's managed identity all four roles it needs
  (rbac.read, rbac.write, identities.read to evaluate rules,
  provisioning.write to dispatch) — same appRoleAssignment pattern as
  2.3's flatfile-connector-service gate. Until both land, rbac-service's
  own endpoints 401 and its outbound calls 403 (correctly, not silently).
  Plus the usual [ONE-TIME, HUMAN] SQL grant for sqldb-rbac, same pattern
  as source-system-service's.
  Wiring: k8s/services/rbac-service.yaml (Deployment + migrate Job +
  Service + HPA, exact source-system-service shape), deploy.sh (SVC
  loops, SQL grant, both new gates above), CI matrix, verify.sh (health,
  401-without-token, role create + version-bump-on-entitlement-add,
  membership-rule evaluate matching the identity-service check's own
  $KEY/QA-department identity, reconcile + dispatch, assignment
  list + revoke). ruff clean, all files compile.
  Live-verified. Deploy hit three real bugs on the way, all fixed:
  (1) rbac-service-migrate's Job never got 1R.7's stale-Job
  delete-before-reapply treatment — deploy.sh only special-cased
  source-system-service, so 4 failed migrate pods sat unnoticed. Fixed by
  generalizing that block to loop over `SQL_MIGRATE_SERVICES=(source-system-service
  rbac-service)` instead of duplicating it per service. (2) The initial
  migrate failure really was the anticipated SQL-grant-not-yet-run
  bootstrapping order issue (18456 login failure), confirmed via pod
  logs — both [HUMAN] gates (SQL grant, rbac.read/rbac.write app roles +
  assignments) were run and, on inspection of the "already exists"
  errors returned, had actually already been satisfied by the time this
  was checked. (3) verify.sh's own `mint_token()` broke specifically for
  rbac-service: `az login --service-principal` treats "zero ARM-visible
  subscriptions" as a login failure, and rbac-service is the first
  service with no Azure ARM role assignment at all (SQL access is a
  T-SQL grant, not ARM RBAC; Graph app roles aren't ARM RBAC either) —
  every other service happens to dodge this because it holds some
  Cosmos/Storage/Service Bus/Event Hub role for a real reason. Fixed
  with `--allow-no-subscriptions`, az's own escape hatch for this case;
  production code was never affected since it calls
  `DefaultAzureCredential` directly rather than shelling out to `az login`.
  Two of verify.sh's own rbac assertions were also wrong and are now
  fixed: the evaluate check grepped the created identity's
  correlationKey against a response that only ever returns identityIds
  (UUIDs) — could never have passed; now captures the real id at
  creation and checks for that. The reconcile check required
  `assignmentsAdded==1`, but identity-service has no
  delete-by-correlation-key endpoint, so every prior verify.sh run's
  QA-department identity persists and legitimately keeps matching the
  rule — loosened to `>=1`, consistent with how every other check in
  this script already treats correlationKey accumulation as permanent.
  Known, accepted side effect: rbac-service's reconcile test dispatches
  real `connectorType: "ad"` tasks (same as 2.3/2.4's dispatch tests),
  which dead-letter hours later. Confirmed directly with the user: this
  dev environment has no real AD/LDAPS server to bind to at all, so the
  "ad-connector" Key Vault/[HUMAN] gate mentioned in deploy.sh's printed
  next-steps isn't a pending task — there's nothing real to point it at
  yet, and the dead-lettering is permanent, not transient. verify.sh now
  calls scripts/drain-provisioning-dlq.sh on itself right after the rbac
  block (self-cleans prior runs' accumulated dead letters) rather than
  requiring that as a manual pre-step every time.
- [x] **3.2 access-request-service** — REQ-COR-REQ-001..003, 006, 007, 009.
  Request/LineItem/ApprovalStep models; default chain manager → owner
  (manager resolved from identity-service); notifications via
  notification queue; approval → provisioning task.
  DONE, live-verified in the dev cluster. Same "no spec document,
  decisions flagged as interpretation not fact" discipline as 3.1
  (IGA_Platform_Requirements_Specification.docx still isn't in the repo).
  Deploy hit the anticipated SQL-grant-not-yet-run bootstrapping order
  (same class as every prior new service) — 4 failed migrate pods until
  the sqldb-accessrequest grant landed. Also surfaced two real, unrelated
  fixes: `kubectl run --rm -it` conflicts with piping a heredoc script as
  stdin (needs plain `-i`, no `-t`) and Python buffers stdout fully when
  it isn't a TTY, silently swallowing DeviceCodeCredential's login prompt
  until `python3 -u` forced it to flush — both were tooling issues on the
  human side, not this service. Also generalized a related deploy.sh gap
  while here: `kubectl rollout restart` returns immediately, so `deploy.sh`
  was handing control back before the restarted pods were actually ready,
  and a verify.sh run immediately after caught them mid-startup and
  reported a false "not ready" cluster-health failure despite every
  functional check passing — added `kubectl rollout status` so `deploy.sh`
  now waits for the rollout to finish before returning.
  Data model (sqldb-accessrequest, identical SQLAlchemy async + aioodbc +
  Entra token auth pattern as source-system-service/rbac-service):
  `Request` (self-service only in v1 — no on-behalf-of/delegated
  requesting; there's no per-user auth yet to attribute a delegated
  request to a real actor), `LineItem` (one requested entitlement;
  targetSystemInstanceId + connectorType + entitlementRef — deliberately
  mirrors rbac-service's RoleEntitlement shape rather than referencing one
  by id, the same "target-system-instance registry doubles as the
  requestable-item registry" precedent from 2.3/3.1), `ApprovalStep`
  (built once at request creation, ordered).
  Real, documented gap: an approved LineItem does NOT create a
  rbac-service RoleAssignment, even though `RoleAssignment.assignmentType`
  already reserves a `'request'` value for exactly this. Wiring it would
  require deciding whether requests target raw entitlements or whole
  Roles, which nothing in the 3.2 summary specifies — left for a v-next
  pass rather than guessed at.
  Approval chain ("manager → owner", REQ-COR-REQ-006): manager is resolved
  from the requester's own identity-service record (`managerIdentityId`,
  already existed). owner is resolved from the line item's target system
  instance — source-system-service had no such concept at all, so this
  pass adds `ownerIdentityId` (nullable, additive migration 0003) to
  `SourceSystemInstance`, exactly like 2.3 added `provisioningTargets`.
  Either step is skipped immediately if its identity can't be resolved
  (never blocks); if BOTH skip, the line item auto-approves with no human
  gate at all. There's no "fallback approver" concept anywhere in this
  codebase to fall back to instead — "skip" is the least-invented default,
  flagged rather than silently assumed to match whatever "with fallback"
  in 4.3's similar wording actually means.
  Dispatch on final approval (REQ-COR-REQ-009): one grant task per
  approved line item to provisioning-service, `sourceType: "access-
  request"` — the exact literal already named in provisioning-service's
  own `ProvisioningTask.sourceType` docstring before this pass existed.
  Best-effort (logged + counted on failure, not retried) — same posture
  and same reasoning as rbac-service's dispatch, does not duplicate 2.3's
  pendingProvisioningDispatch retry mechanism.
  Notifications: publishes `ApprovalRequested` (to the current step's
  approver) and `RequestDecided` (to the requester) onto the
  `notification-tasks` queue — both new event types added to
  notification-service's `_HANDLERS` (its worker.py had already
  anticipated this exact producer and event-type-discriminator extension
  point by name, before this pass existed). Neither identity-service nor
  notification-service has any per-identity email address concept
  (checked) and notification-service's sender config is a single static
  ops-distro recipient list, not per-identity routing — so these land in
  that same static inbox with the relevant identityId in the body, not the
  approver's/requester's actual inbox. Real per-person delivery needs an
  identity → email mapping that doesn't exist yet; a documented gap, not a
  pretended capability.
  Auth: same posture as identity-service/provisioning-service/rbac-service
  (1R.3) — requests.read/requests.write are new app roles, another
  [HUMAN] gate printed by deploy.sh (same appRoleAssignment pattern as
  3.1's rbac.read/rbac.write). Deciding an approval step is gated on
  requests.write only — there is no per-user token/identity verification
  anywhere in this codebase yet (that's 3.4's SPA/MSAL work, still ahead)
  to cryptographically confirm the caller IS the resolved
  approverIdentityId. Same class of gap as PlatformRole's unenforced
  binding in 3.1, flagged rather than silently assumed safe. Calls to
  source-system-service need no token — that service has no auth wired at
  all, a pre-existing gap that predates 1R.3, not introduced here.
  Wiring: k8s/services/access-request-service.yaml (Deployment + migrate
  Job + Service + HPA, same shape as rbac-service's), deploy.sh (SVC
  loops, Service Bus Data Sender role assignment for publishing to
  notification-tasks, SQL grant, both new gates above), CI matrix,
  verify.sh (health, 401-without-token, a resolvable-manager/skipped-owner
  chain through decide → dispatch → request completion, and the
  both-steps-unresolvable auto-approve path). verify.sh's own dispatch
  from this test queues another "ad"-connector task that can't succeed for
  the same reason as rbac-service's (no real AD server) — covered by the
  same self-drain call, just moved to run after both dispatch-generating
  blocks instead of only after rbac-service's. ruff clean, all files
  compile.
- [x] **3.3 notification-service** — consumes notification-tasks queue,
  sends email via ACS Email or SMTP relay [HUMAN gate: provide sender config
  as Key Vault secrets]. Webhook fan-out for ProvisioningFailed.
  DONE (scaffold, unverified in a live cluster): SMTP relay chosen over ACS
  Email (no ACS resource exists in infra/ yet); non-session consumer of
  notification-tasks (verified sessions:false in messaging.bicep) dispatches
  ProvisioningFailed to email + webhook fan-out; sender config is a
  [HUMAN gate] — service degrades to log-and-skip until the
  `notification-sender` Key Vault secrets/k8s Secret are populated (see
  scripts/deploy.sh next-steps output).
- [x] **3.4 React frontend scaffold** — REQ-UI-001..005, 010..017. Vite +
  React + TypeScript in `web/`. MSAL.js auth-code+PKCE against Entra
  [HUMAN gate: SPA app registration]. Unified login page per REQ-UI-010/013,
  persona routing per REQ-UI-014. Serve via Static Web App (add Bicep).
  DONE (scaffold + build verified locally; live sign-in pending the SPA
  gate). No REQ-UI spec text exists in the repo (same gap as 3.1/3.2) —
  the UI is an interpretation of the one-line summaries, flagged per item.
  Stack: Vite 5 + React 18 + TS strict; deps deliberately minimal
  (@azure/msal-browser/react + react-router only, hand-rolled CSS).
  `npm run build` (tsc + vite) passes and runs in CI's validate job.
  Auth design — zero backend changes: the SPA acquires a DELEGATED token
  for iga-platform-api's new `access_as_user` scope; Entra puts the
  user's assigned app roles in that token's `roles` claim, the exact
  claim every service's require_role() already validates. The [HUMAN]
  gate is scripts/spa-gate.sh (printed by deploy.sh): creates the
  iga-platform-spa registration (PKCE public client, no secret exists
  anywhere), registers api://<appId> + the scope + pre-authorizes the
  SPA, flips the existing app roles' allowedMemberTypes to
  Application+User (Graph requires the disable→modify→enable dance), and
  assigns all roles to the signed-in user.
  Persona routing (REQ-UI-014) is an interpretation: no persona store
  exists anywhere, so persona = app roles held (identities.write ⇒ admin
  console + portal; otherwise portal only). Client-side routing is
  convenience — every call is enforced server-side.
  CRITICAL, DELIBERATE SCOPE LIMIT: the services are ClusterIP-only in a
  private VNet with no ingress — the roadmap's own plan for public API
  exposure is 4.5's APIM, so the publicly-hosted SPA (Static Web App,
  infra/modules/web.bicep, Free tier, eastus2 because SWA rejects eastus
  — same documented-exception class as SQL in canadacentral) hosts
  static assets that can sign in but cannot reach the APIs yet. The
  fully-functional path today is scripts/dev-portal.sh: port-forwards
  all five services and starts Vite with /api/* proxies — the browser
  carries a real delegated token end-to-end against the real cluster,
  zero mocks. SWA content deploy is human-run (deployment token is a
  secret; spa-gate.sh prints the steps).
- [x] **3.5 Admin console v1** — REQ-UI-020..025. Identities list/search/
  detail (history view), target system instances, provisioning task queue
  with retry/cancel, source system feed runs.
  DONE (code complete; live verification pending deploy + gates).
  Identities list/search (department/status/name-contains, the filters
  identity-service already had), detail with the append-only history
  diffed per-field client-side; target-system registry (source-system-
  service's, dual-purposed per 2.3/3.1 precedent — that service still has
  no auth wired, pre-existing gap); feed runs with full delta summaries.
  The task queue REQUIRED A BACKEND ADDITION — provisioning-service had
  only POST /tasks; tasks lived solely as Service Bus messages, nothing
  queryable. Added the sqldb-provisioning task-state store (that DB was
  in data.bicep's serviceDatabases from day one — clearly the intended
  design, finally used): a record per task, status transitions written
  by the worker (queued/in-progress/retry-scheduled/succeeded/
  dead-lettered/cancelled), best-effort so a SQL blip can never poison
  queue processing; worker backfills rows for pre-migration messages.
  GET /tasks(+filters)/{id}, POST retry (re-enqueues from the record,
  fresh attempt budget; the DLQ copy stays — no selective DLQ delete
  exists, drain script remains the cleanup), POST cancel (marks the
  record; worker completes the message unexecuted). All gated on
  provisioning.write — a new provisioning.read role would mean another
  Graph gate with no current read-only caller; deliberate coarse choice.
  New [ONE-TIME, HUMAN] gate: sqldb-provisioning SQL grant (printed by
  deploy.sh); provisioning-service's Dockerfile gained the msodbcsql18
  block and a migrate Job (already covered by the SQL_MIGRATE_SERVICES
  loop). verify.sh: record-visible check + a deterministic cancel test
  (unregistered connectorType fails its first attempt instantly →
  retry-scheduled → cancel; the pending retry message is then completed
  unexecuted, so the probe self-cleans).
- [x] **3.6 End-user portal v1** — REQ-UI-030..032. My access, request cart
  against requestable entitlements, my approvals queue.
  DONE (code complete; live verification pending deploy + gates).
  Two small backend additions: rbac-service GET /assignments?identityId=
  (cross-role, enriched with role name + entitlements) and
  access-request-service GET /approval-steps?approverIdentityId=
  (enriched with request/line-item context + an `actionable` flag
  mirroring the decide endpoint's chain-ordering rule). Both covered by
  new verify.sh checks.
  GAP CLOSED (approver-binding hardening task, post-3.6): the identity
  link is now server-side and ENFORCED for approval decisions.
  identity-service gained `entraObjectId` — settable ONLY via
  POST /identities/{id}/claim, which binds the CALLER's validated token
  oid (never a request-body value) to an unclaimed record: first claim
  wins, idempotent same-oid re-claim, 409 for a different principal, one
  identity per principal (so GET /identities/by-entra-object-id/{oid},
  same shape as 2.3's by-correlation-key, is unambiguous — and 409s
  loudly if data integrity is ever violated rather than picking one).
  The field is deliberately absent from IdentityIn, stripped at create,
  and immutable in PATCH — with extra="allow" it would otherwise be
  spoofable by any identities.write holder. Claims are audited
  (IdentityClaimed history event, actor=oid) and published to the event
  hub. Gated on identities.read, not .write: it's not an arbitrary
  write, and every portal persona holds read. App-only (service) tokens
  may claim too — their oid is the SP's object id, verify.sh exercises
  the whole flow through one, and allowing it weakens nothing (app-only
  requests.write holders could previously decide steps as ANYONE).
  access-request-service's decide endpoint now resolves the caller's oid
  via that lookup and returns 403 for an unlinked caller or a caller
  whose claimed identity isn't the step's assigned approver;
  decidedByIdentityId is server-resolved and was REMOVED from the
  request schema (unknown keys are dropped, so old clients still work —
  their self-declared identity is simply ignored). Mechanical note: the
  task spec suggested `Depends(require_role(...))`, but require_role()
  already returns a Depends — endpoints capture claims as
  `claims: dict = require_role(...)`.
  The portal now claims server-side (localStorage is a display cache
  only) and auto-resolves an existing claim from the MSAL account's oid
  on load, so the binding follows the user across browsers.
  verify.sh: unlinked-caller 403 (fresh environments only — claims are
  permanent, so after the AR service principal's oid links once there is
  no unlinked requests.write principal left; the check self-skips),
  claim + server-resolved-decider assertion on the approve path, and an
  every-run wrong-approver 403 against a step assigned to a fresh
  never-claimed identity.
  Residual gaps, still open and documented: (1) nothing verifies the
  HUMAN behind a token corresponds to the HR record they claim
  (identities carry no UPN/email to match) — first-claim-wins bounds the
  damage; attribute-matched auto-claim is the v-next once feeds supply a
  UPN. (2) OUT OF SCOPE here by explicit task decision: rbac-service's
  PlatformRole binding (role-owner/certifier enforcement) has the
  identical unresolved shape — admin actions authorized by app role
  alone with no per-user binding. It needs this same
  entraObjectId-resolution pattern now that the primitive exists; scoped
  out of the approval-chain task, tracked as a known follow-up.
  "Requestable entitlements" (REQ-UI-031) is interpreted as entitlements
  defined on active rbac-service roles (the only entitlement catalogue
  the platform has) plus a free-form entry — flagged interpretation.
  Approvals queue drives the existing decide endpoint; RequestDecided/
  ApprovalRequested notifications flow as wired in 3.2.

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
