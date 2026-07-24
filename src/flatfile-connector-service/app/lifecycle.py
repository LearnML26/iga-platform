"""
Lifecycle sweep — Phase 2.4 (REQ-COR-SRC-007/008).

Three passes, run daily by the `lifecycle-sweep` CronJob
(k8s/services/lifecycle-sweep.yaml) POSTing /lifecycle/sweep:

0. **Pending-dispatch retry**: for every source system instance whose
   connector-owned state file carries `pendingProvisioningDispatch`
   entries, re-attempt them via the same `_retry_pending_dispatches` the
   ingest path uses. Without this, a scheduled termination whose dispatch
   failed would only ever be retried if that instance happened to ingest
   another file — the sweep gives those entries a daily retry vehicle of
   their own.
1. **Pending-start activation** (REQ-COR-SRC-007): every `pending-start`
   identity whose startDate is within PRE_START_ACTIVATION_DAYS of today
   (default 3, per the spec's pre-start-window example) is PATCHed to
   `active`. That transition is what makes the identity eligible for
   provisioning — nothing provisions while pending-start. An unparseable
   or missing startDate leaves the identity pending-start and gets a note,
   never an activation.
2. **Due scheduled terminations** (REQ-COR-SRC-008): every `active`
   identity with terminationDate <= today (via identity-service's
   `terminationDateBefore` filter) is PATCHed to `terminated`, then
   disable-account tasks are dispatched per its source system's
   provisioningTargets — the same shared dispatch helper and
   pending-persistence the ingest termination pass uses, so a sweep-time
   dispatch failure lands in the same `pendingProvisioningDispatch` map
   and is retried by pass 0 tomorrow or by the instance's next ingest,
   whichever comes first.

Why this lives in flatfile-connector-service rather than a new service or
source-system-service: everything the sweep needs already exists here —
the iga-platform-api token flow (1R.3), authenticated identity/provisioning
HTTP clients, the dispatch helper, and the state-blob persistence from the
2.3 dispatch-retry fix. Any other home would duplicate all of it. The
scheduler is a plain k8s CronJob curling the endpoint (stateless,
consistent with how source-system-service-migrate runs as a one-shot Job)
rather than KEDA, which is not installed in this cluster.

Known limitations, deliberate at dev scale:
- No pagination: each query reads at most 200 identities/instances per
  sweep. Fine for dev; needs continuation-token support in
  identity-service before production volumes.
- Date semantics are string-compared ISO dates. Mappings should emit bare
  YYYY-MM-DD; a timestamped terminationDate on its due day sorts after the
  bare date and gets picked up one sweep late.
- Identities whose sourceSystemId is missing, or whose source system was
  deleted, terminate WITHOUT dispatch (logged in notes) — there is no
  other registry to resolve provisioningTargets from. Same posture as
  2.3's documented target-registry gap.
- The endpoint is cluster-internal and unauthenticated, same as /ingest
  (flatfile-connector-service was explicitly out of 1R.3's auth scope).
"""
import logging
import os
from datetime import date
from typing import Any

import httpx
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient

from .ingest import (
    API_AUDIENCE,
    APPLY_FAILURE_THRESHOLD,
    CURATED_CONTAINER,
    IDENTITY_SERVICE_URL,
    PROVISIONING_SERVICE_URL,
    SOURCE_SYSTEM_SERVICE_URL,
    STORAGE_ACCOUNT,
    IngestError,
    _call,
    _dispatch_disable_accounts,
    _load_state,
    _parse_iso_date,
    _retry_pending_dispatches,
    _save_state,
)

log = logging.getLogger("flatfile-connector-service")

# REQ-COR-SRC-007 pre-start window: activate a pending-start identity this
# many days (or fewer) before its startDate. Spec's worked example is 3.
PRE_START_ACTIVATION_DAYS = int(os.environ.get("PRE_START_ACTIVATION_DAYS", "3"))


async def run_sweep() -> dict[str, Any]:
    if not API_AUDIENCE:
        raise IngestError("API_AUDIENCE not configured; cannot authenticate to identity-service/provisioning-service")

    credential = DefaultAzureCredential()
    try:
        token = (await credential.get_token(f"{API_AUDIENCE}/.default")).token
        auth_headers = {"Authorization": f"Bearer {token}"}
        today = date.today()
        sweep_ref = f"lifecycle-sweep-{today.isoformat()}"

        notes: list[str] = []
        failures = 0
        halted = False
        halt_reason: str | None = None
        retry_attempted = retry_succeeded = 0
        ps_checked = activated = 0
        due = terminated = dispatch_ok = dispatch_failed = 0

        async with httpx.AsyncClient(base_url=SOURCE_SYSTEM_SERVICE_URL, timeout=30.0) as source_http, \
                httpx.AsyncClient(base_url=IDENTITY_SERVICE_URL, timeout=30.0, headers=auth_headers) as identity_http, \
                httpx.AsyncClient(base_url=PROVISIONING_SERVICE_URL, timeout=30.0, headers=auth_headers) as provisioning_http, \
                BlobServiceClient(
                    account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net", credential=credential
                ) as blob_service:
            curated = blob_service.get_container_client(CURATED_CONTAINER)

            # ---- Pass 0: retry pending dispatches left by earlier runs ----
            resp, err = await _call(source_http.get("/source-systems", params={"limit": 200}))
            if err or resp.status_code != 200:
                failures += 1
                notes.append(f"could not list source systems for pending-dispatch retry: {err or resp.status_code}")
            else:
                for inst in resp.json():
                    known_keys, pending = await _load_state(curated, inst["id"])
                    if not pending:
                        continue
                    r = await _retry_pending_dispatches(
                        identity_http, provisioning_http, inst["id"], sweep_ref,
                        pending, APPLY_FAILURE_THRESHOLD,
                    )
                    retry_attempted += r["attempted"]
                    retry_succeeded += r["succeeded"]
                    failures += r["failed"]
                    await _save_state(curated, inst["id"], known_keys, r["remaining"])
                    if r["halted"]:
                        halted, halt_reason = True, r["halt_reason"]
                        break

            # ---- Pass 1: pending-start -> active (REQ-COR-SRC-007) ----
            if not halted:
                resp, err = await _call(identity_http.get(
                    "/identities", params={"status": "pending-start", "limit": 200}
                ))
                if err or resp.status_code != 200:
                    failures += 1
                    notes.append(f"pending-start query failed: {err or resp.status_code}")
                else:
                    for ident in resp.json():
                        ps_checked += 1
                        sd = _parse_iso_date(ident.get("startDate"))
                        if sd is None:
                            notes.append(
                                f"pending-start identity {ident['identityId']} has no parseable startDate — left as-is"
                            )
                            continue
                        if (sd - today).days > PRE_START_ACTIVATION_DAYS:
                            continue  # still outside the pre-start window
                        r2, e2 = await _call(identity_http.patch(
                            f"/identities/{ident['identityId']}", json={"status": "active"}
                        ))
                        if e2 or r2.status_code != 200:
                            failures += 1
                            if failures >= APPLY_FAILURE_THRESHOLD:
                                halted = True
                                halt_reason = e2 or f"activation PATCH returned {r2.status_code}"
                                break
                        else:
                            activated += 1
                            log.info("lifecycle: activated %s (startDate %s)", ident["identityId"], sd)

            # ---- Pass 2: due scheduled terminations (REQ-COR-SRC-008) ----
            if not halted:
                resp, err = await _call(identity_http.get(
                    "/identities",
                    params={"status": "active", "terminationDateBefore": today.isoformat(), "limit": 200},
                ))
                if err or resp.status_code != 200:
                    failures += 1
                    notes.append(f"due-termination query failed: {err or resp.status_code}")
                else:
                    for ident in resp.json():
                        due += 1
                        r2, e2 = await _call(identity_http.patch(
                            f"/identities/{ident['identityId']}", json={"status": "terminated"}
                        ))
                        if e2 or r2.status_code != 200:
                            failures += 1
                            if failures >= APPLY_FAILURE_THRESHOLD:
                                halted = True
                                halt_reason = e2 or f"termination PATCH returned {r2.status_code}"
                                break
                            continue
                        terminated += 1
                        log.info("lifecycle: terminated %s (terminationDate due)", ident["identityId"])

                        src_id = ident.get("sourceSystemId")
                        targets: list[str] = []
                        if not src_id:
                            notes.append(
                                f"identity {ident['identityId']} has no sourceSystemId — terminated without dispatch"
                            )
                        else:
                            r3, e3 = await _call(source_http.get(f"/source-systems/{src_id}"))
                            if e3 or r3.status_code not in (200, 404):
                                failures += 1
                                notes.append(
                                    f"identity {ident['identityId']}: source system lookup failed "
                                    f"({e3 or r3.status_code}) — terminated without dispatch"
                                )
                            elif r3.status_code == 404:
                                notes.append(
                                    f"identity {ident['identityId']}: source system {src_id} no longer "
                                    "exists — terminated without dispatch"
                                )
                            else:
                                targets = r3.json().get("provisioningTargets", [])
                                if not targets:
                                    log.info(
                                        "lifecycle: %s terminated; source instance %s has no "
                                        "provisioningTargets — nothing dispatched", ident["identityId"], src_id,
                                    )
                        if targets:
                            key = ident.get("correlationKey", "")
                            d = await _dispatch_disable_accounts(
                                provisioning_http, sweep_ref, src_id, ident["identityId"], key,
                                targets, failure_budget=APPLY_FAILURE_THRESHOLD - failures,
                            )
                            dispatch_ok += len(d["succeeded"])
                            dispatch_failed += d["attemptedFailures"]
                            failures += d["attemptedFailures"]
                            if d["failedTargets"]:
                                known_keys, pending = await _load_state(curated, src_id)
                                pending[key] = sorted(set(pending.get(key, [])) | set(d["failedTargets"]))
                                await _save_state(curated, src_id, known_keys, pending)
                            if d["haltReason"]:
                                halted, halt_reason = True, d["haltReason"]
                        if halted:
                            break

        summary = {
            "date": today.isoformat(),
            "preStartActivationDays": PRE_START_ACTIVATION_DAYS,
            "pendingDispatchRetried": retry_attempted,
            "pendingDispatchSucceeded": retry_succeeded,
            "pendingStartChecked": ps_checked,
            "activated": activated,
            "terminationsDue": due,
            "terminated": terminated,
            "dispatchSucceeded": dispatch_ok,
            "dispatchFailed": dispatch_failed,
            "applyFailures": failures,
            "halted": halted,
            "haltReason": halt_reason,
            "notes": notes,
        }
        log.info("lifecycle sweep complete: %s", summary)
        return summary
    finally:
        await credential.close()
