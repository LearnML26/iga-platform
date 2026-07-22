"""
Flat-file connector core (REQ-COR-SRC-002, REQ-COR-ID-006, REQ-COR-SRC-006/009).

Stateless connector: it owns no database of its own. SourceSystemInstance,
AttributeMapping, and FeedRun all live in source-system-service (2.1) — this
connector reads mappings and reports results there over HTTP.

Delta semantics (added/updated/terminated/unmatched), as of 2.3: each unique
correlation key in the file is resolved against identity-service itself
(GET /identities/by-correlation-key/{key}) — a 404 means "create", a 200
means "diff and PATCH only the changed attributes". Identity data and the
add/update decision both come from identity-service now, not from a local
snapshot. "unmatched" is unchanged from 2.2: *ambiguous correlation within a
single file* (two-or-more rows resolving to the same key) — such rows are
never applied and the previous known-keys entry for that key, if any, is
left untouched.

Termination detection is the one thing identity-service alone can't answer:
iterating this run's rows can only ever see keys present *now* — it cannot
discover a key that's now *absent*. There is no bulk "list identities for
this source system" query, so this connector still keeps a minimal local
state at `curated/source-state/<instanceId>/latest.json` — but as of 2.3 it
is just the *set* of correlation keys seen as of the last completed run, no
longer a content-hash snapshot (see `_load_state`/`_save_state`). A key that
was known and is absent from the current file gets its identity PATCHed to
status=terminated, and — for each entry in that source system instance's
`provisioningTargets` — a disable-account task is POSTed to
provisioning-service. An empty `provisioningTargets` is a valid, safe
default (nothing dispatched, just logged), not an error condition.

Provisioning-dispatch retry (post-2.3 fix): a POST /tasks failure for one
target in a multi-target provisioningTargets list used to just count toward
the apply-failure threshold and get silently lost forever — by the time the
dispatch loop runs, the identity's correlation key has already been removed
from the known-keys set (it's terminated), so no future run's termination
pass would ever revisit it. The same state file now also carries a sibling
`pendingProvisioningDispatch: {correlationKey: [connectorType, ...]}` map of
dispatches still owed. At the start of every run, before touching the
current file's rows, any entries left over from a prior run are retried
(re-resolving the identity via GET by-correlation-key — its status=
terminated PATCH already happened, this only re-sends the disable-account
task) via `_retry_pending_dispatches`; a repeat failure counts toward this
run's apply-failure threshold exactly like any other apply failure and
stays in the map for the next run. This does not add any saga/rollback
mechanism — the status=terminated PATCH in `_apply_terminations` is
unaffected and still applied immediately; only the dispatch loop's failure
handling changed.

Known, deliberately out-of-scope gap: there is no target-system-instance
registry or account-identifier mapping (userDn/userObjectId) anywhere in the
current data model, so a disable-account task's `payload` cannot carry a
real target-system identifier — it's best-effort (`correlationKey` only) for
traceability. `instanceId` on the task reuses the *source* instance id, since
no separate target-instance registry exists. Real execution of these tasks
already has known gaps tracked elsewhere (AD bind creds never wired per
1R.7; EntraIdConnector has no disable_account handler at all) — 2.3 is
scoped to emitting the correct task at the correct trigger point, not to
closing those connector-side gaps.

Failure-threshold halt (REQ-COR-SRC-009, paraphrased): only identity-service/
provisioning-service apply failures (5xx, timeouts/connection errors on the
GET/POST/PATCH calls below) count toward `APPLY_FAILURE_THRESHOLD` — 2.2's
malformed-row quarantine is a separate, already-handled concern and never
counts here. Crossing the threshold stops processing remaining rows in this
run (no more identity-service/provisioning-service calls) but does NOT roll
back rows already applied — there's no compensation/saga mechanism here.
The known-keys/pending-dispatch state is saved reflecting exactly what was
durably applied before the halt, so the next run resumes correctly. The
FeedRun is marked `failed` with attempted/succeeded/apply-failure counts
folded into `errorSummary` (no new FeedRun columns were added for this — it
reuses the existing free-text field, same as the other operational notes
below).

Auth: identity-service and provisioning-service both validate a bearer token
against the `iga-platform-api` app registration (REQ-COR-API-001/002, Phase
1R.3) and require a specific app role per endpoint. This connector acquires
one token per run via its own workload identity (`API_AUDIENCE` env var,
same audience as 1R.3) and attaches it to every identity-service/
provisioning-service call. Its managed identity must be granted the
`identities.read`, `identities.write`, and `provisioning.write` app roles —
a [HUMAN] gate (Graph app-role-assignment needs directory perms), printed by
deploy.sh, same pattern as 1R.3/1R.6. Without that grant, every call below
will 403 and (correctly) count toward the apply-failure threshold above.

Malformed rows (missing/empty key attribute, or a value that can't survive
its transform) are quarantined rather than failing the whole run. A missing
*mapped source column* in the file header, or an unreadable/checksum-failed
file, fails the whole run — those are config/integrity problems, not
bad-data problems.
"""
import csv
import hashlib
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient

log = logging.getLogger("flatfile-connector-service")

STORAGE_ACCOUNT = os.environ.get("STORAGE_ACCOUNT_NAME", "")
RAW_CONTAINER = "raw"
CURATED_CONTAINER = "curated"
SOURCE_SYSTEM_SERVICE_URL = os.environ.get("SOURCE_SYSTEM_SERVICE_URL", "http://source-system-service")
IDENTITY_SERVICE_URL = os.environ.get("IDENTITY_SERVICE_URL", "http://identity-service")
PROVISIONING_SERVICE_URL = os.environ.get("PROVISIONING_SERVICE_URL", "http://provisioning-service")
API_AUDIENCE = os.environ.get("API_AUDIENCE", "")  # e.g. api://<iga-platform-api appId> — same as 1R.3
APPLY_FAILURE_THRESHOLD = int(os.environ.get("APPLY_FAILURE_THRESHOLD", "5"))  # REQ-COR-SRC-009

# Small, fixed set of named transforms (REQ-COR-SRC-002 "mapping-driven
# schema" — kept intentionally minimal for 2.2; an unrecognized transform
# name is treated as a no-op rather than failing the row, and is noted in
# the feed run's errorSummary).
_TRANSFORMS = {
    "upper": str.upper,
    "lower": str.lower,
    "strip": str.strip,
    "title": str.title,
}


class IngestError(Exception):
    """Whole-file failure — the feed run is marked failed, no rows applied."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _verify_checksum(container_client, blob_path: str, content: bytes) -> Optional[str]:
    """Verify file integrity before processing. Returns a note on how it was
    verified, or None if no checksum reference was available to check
    against (noted as a limitation, not fatal). Raises IngestError on a
    genuine mismatch.

    Prefers a sidecar '<blob_path>.md5' object (hex digest text) — works
    regardless of how the CSV was uploaded — over the blob's own
    Content-MD5 property, which is only present if the uploading client set
    it explicitly.
    """
    digest = hashlib.md5(content).digest()
    digest_hex = digest.hex()

    sidecar_path = f"{blob_path}.md5"
    try:
        sidecar_client = container_client.get_blob_client(sidecar_path)
        sidecar = await sidecar_client.download_blob()
        raw = await sidecar.readall()
        expected = raw.decode("utf-8").strip().lower()
        if expected != digest_hex:
            raise IngestError(
                f"checksum mismatch: sidecar {sidecar_path} expects {expected}, computed {digest_hex}"
            )
        return f"checksum verified against sidecar {sidecar_path}"
    except ResourceNotFoundError:
        pass

    blob_client = container_client.get_blob_client(blob_path)
    props = await blob_client.get_blob_properties()
    content_md5 = props.content_settings.content_md5
    if content_md5:
        if bytes(content_md5) != digest:
            raise IngestError(
                f"checksum mismatch: blob Content-MD5 property does not match downloaded content for {blob_path}"
            )
        return "checksum verified against blob Content-MD5 property"

    return None


async def run_ingestion(instance_id: str, blob_path: str, triggered_by: str = "manual") -> dict[str, Any]:
    credential = DefaultAzureCredential()
    feed_run_id: Optional[str] = None
    try:
        if not API_AUDIENCE:
            raise IngestError("API_AUDIENCE not configured; cannot authenticate to identity-service/provisioning-service")
        token = (await credential.get_token(f"{API_AUDIENCE}/.default")).token
        auth_headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=SOURCE_SYSTEM_SERVICE_URL, timeout=30.0) as http, \
                httpx.AsyncClient(base_url=IDENTITY_SERVICE_URL, timeout=30.0, headers=auth_headers) as identity_http, \
                httpx.AsyncClient(base_url=PROVISIONING_SERVICE_URL, timeout=30.0, headers=auth_headers) as provisioning_http:

            instance_resp = await http.get(f"/source-systems/{instance_id}")
            if instance_resp.status_code == 404:
                raise IngestError(f"source system instance {instance_id} not found")
            instance_resp.raise_for_status()
            provisioning_targets = instance_resp.json().get("provisioningTargets", [])

            mappings_resp = await http.get(f"/source-systems/{instance_id}/mappings")
            if mappings_resp.status_code == 404:
                raise IngestError(f"source system instance {instance_id} not found")
            mappings_resp.raise_for_status()
            mappings = mappings_resp.json()
            if not mappings:
                raise IngestError(f"source system instance {instance_id} has no attribute mappings defined")

            run_resp = await http.post(
                f"/source-systems/{instance_id}/feed-runs", json={"triggeredBy": triggered_by}
            )
            run_resp.raise_for_status()
            feed_run_id = run_resp.json()["id"]

            summary = await _process(
                credential, http, identity_http, provisioning_http,
                instance_id, feed_run_id, blob_path, mappings, provisioning_targets,
            )
            return summary
    except IngestError as e:
        if feed_run_id:
            async with httpx.AsyncClient(base_url=SOURCE_SYSTEM_SERVICE_URL, timeout=30.0) as http:
                await http.patch(
                    f"/feed-runs/{feed_run_id}",
                    json={"status": "failed", "completedAt": _now().isoformat(), "errorSummary": str(e)},
                )
        log.error("feed run %s failed: %s", feed_run_id, e)
        return {"feedRunId": feed_run_id, "status": "failed", "error": str(e)}
    finally:
        await credential.close()


async def _call(coro) -> tuple[Optional[httpx.Response], Optional[str]]:
    """Run a single identity-service/provisioning-service apply call.
    Returns (response, None) on a completed round-trip — caller checks
    status_code — or (None, reason) on a network-level failure (timeout,
    connection error). Both branches count toward the apply-failure
    threshold (REQ-COR-SRC-009) the same way."""
    try:
        resp = await coro
        return resp, None
    except httpx.RequestError as e:
        return None, f"{type(e).__name__}: {e}"


def _disable_account_task(feed_run_id: str, instance_id: str, identity_id: str, key: str, connector_type: str) -> dict:
    return {
        "sourceType": "source-feed",
        "sourceRef": feed_run_id,
        "identityId": identity_id,
        "instanceId": instance_id,
        "connectorType": connector_type,
        "operationType": "disable-account",
        "payload": {"correlationKey": key},
    }


async def _retry_pending_dispatches(
    identity_http: httpx.AsyncClient,
    provisioning_http: httpx.AsyncClient,
    instance_id: str,
    feed_run_id: str,
    pending: dict[str, list[str]],
    threshold: int,
) -> dict[str, Any]:
    """Re-attempt provisioning-task dispatches that failed on a prior run,
    before this run's own file is applied. The identity's status=terminated
    PATCH already happened when it was first terminated — that's not
    repeated here — this only re-sends the disable-account task(s) that
    never got a 202. Failures (or targets never reached because the
    threshold was hit) stay in the returned `remaining` map for next time;
    nothing is ever dropped silently."""
    attempted = succeeded = failed = 0
    halted = False
    halt_reason: Optional[str] = None
    remaining: dict[str, list[str]] = {}

    keys = list(pending.keys())
    i = 0
    while i < len(keys) and not halted:
        key = keys[i]
        targets = pending[key]
        i += 1

        resp, err = await _call(identity_http.get(f"/identities/by-correlation-key/{quote(key, safe='')}"))
        if err or resp.status_code not in (200, 404):
            failed += 1
            remaining[key] = list(targets)
            if failed >= threshold:
                halted, halt_reason = True, err or f"GET by-correlation-key returned {resp.status_code} for '{key}'"
            continue
        if resp.status_code == 404:
            log.warning("pending dispatch retry: '%s' has no identity-service record; dropping %s", key, targets)
            continue
        existing = resp.json()

        still_failing: list[str] = []
        for ti in range(len(targets)):
            connector_type = targets[ti]
            attempted += 1
            resp2, err2 = await _call(provisioning_http.post(
                "/tasks", json=_disable_account_task(feed_run_id, instance_id, existing["identityId"], key, connector_type)
            ))
            if err2 or resp2.status_code != 202:
                failed += 1
                still_failing.append(connector_type)
                if failed >= threshold:
                    halted = True
                    halt_reason = err2 or f"POST /tasks (disable-account retry, {connector_type}) returned {resp2.status_code}"
                    still_failing.extend(targets[ti + 1:])
                    break
            else:
                succeeded += 1
        if still_failing:
            remaining[key] = still_failing

    for key in keys[i:]:
        remaining[key] = list(pending[key])

    return {
        "attempted": attempted, "succeeded": succeeded, "failed": failed,
        "halted": halted, "halt_reason": halt_reason, "remaining": remaining,
    }


async def _apply_added_updated(
    identity_http: httpx.AsyncClient,
    instance_id: str,
    unique_rows: dict[str, dict[str, Any]],
    threshold: int,
    failed_so_far: int = 0,
) -> dict[str, Any]:
    """Add/update pass (REQ-COR-SRC-006): resolve each row's correlation key
    against identity-service and create-or-patch accordingly. Halts once
    `threshold` cumulative apply failures accumulate (REQ-COR-SRC-009); does
    not roll back rows already applied."""
    added = updated = succeeded = attempted = 0
    failed = failed_so_far
    applied_keys: set[str] = set()
    halted = False
    halt_reason: Optional[str] = None

    for key, target in unique_rows.items():
        attempted += 1
        resp, err = await _call(identity_http.get(f"/identities/by-correlation-key/{quote(key, safe='')}"))
        if err or resp.status_code not in (200, 404):
            failed += 1
            if failed >= threshold:
                halted, halt_reason = True, err or f"GET by-correlation-key returned {resp.status_code} for '{key}'"
                break
            continue

        if resp.status_code == 404:
            body = {**target, "correlationKey": key, "sourceSystemId": instance_id}
            resp2, err2 = await _call(identity_http.post("/identities", json=body))
            if err2 or resp2.status_code != 201:
                failed += 1
                if failed >= threshold:
                    halted, halt_reason = True, err2 or f"POST /identities returned {resp2.status_code} for '{key}'"
                    break
                continue
            added += 1
            succeeded += 1
            applied_keys.add(key)
            continue

        existing = resp.json()
        applied_keys.add(key)  # confirmed to exist regardless of whether an update follows
        changed = {k: v for k, v in target.items() if existing.get(k) != v}
        if not changed:
            succeeded += 1
            continue
        resp3, err3 = await _call(identity_http.patch(f"/identities/{existing['identityId']}", json=changed))
        if err3 or resp3.status_code != 200:
            failed += 1
            if failed >= threshold:
                halted, halt_reason = True, err3 or f"PATCH /identities/{existing['identityId']} returned {resp3.status_code}"
                break
            continue
        updated += 1
        succeeded += 1

    return {
        "added": added, "updated": updated, "succeeded": succeeded, "attempted": attempted, "failed": failed,
        "halted": halted, "halt_reason": halt_reason, "applied_keys": applied_keys,
    }


async def _apply_terminations(
    identity_http: httpx.AsyncClient,
    provisioning_http: httpx.AsyncClient,
    instance_id: str,
    feed_run_id: str,
    provisioning_targets: list[str],
    terminated_keys: set[str],
    threshold: int,
    failed_so_far: int,
) -> dict[str, Any]:
    """Termination pass: a key known as of the last run but absent from this
    one gets its identity PATCHed to status=terminated (always applied
    immediately, regardless of what happens next), then a disable-account
    task is dispatched to each of the source instance's provisioningTargets
    (none configured -> logged, not an error). A target whose dispatch
    fails — or is never reached because the threshold was hit — is recorded
    in `failed_target_dispatches` for the caller to persist and retry on a
    future run, rather than being silently lost."""
    terminated = succeeded = attempted = 0
    failed = failed_so_far
    halted = False
    halt_reason: Optional[str] = None
    removed_keys: set[str] = set()
    failed_target_dispatches: dict[str, list[str]] = {}

    for key in terminated_keys:
        attempted += 1
        resp, err = await _call(identity_http.get(f"/identities/by-correlation-key/{quote(key, safe='')}"))
        if err or resp.status_code not in (200, 404):
            failed += 1
            if failed >= threshold:
                halted, halt_reason = True, err or f"GET by-correlation-key returned {resp.status_code} for '{key}'"
                break
            continue

        if resp.status_code == 404:
            # Locally known as previously seen, but identity-service has no
            # record of it (e.g. a prior run's create never actually
            # landed) — nothing to terminate; stop tracking it as active.
            log.warning("terminated-key '%s' has no identity-service record; dropping from known-keys", key)
            removed_keys.add(key)
            succeeded += 1
            continue

        existing = resp.json()
        if existing.get("status") == "terminated":
            removed_keys.add(key)  # already terminated — stop tracking it as active
            succeeded += 1
            continue

        resp2, err2 = await _call(
            identity_http.patch(f"/identities/{existing['identityId']}", json={"status": "terminated"})
        )
        if err2 or resp2.status_code != 200:
            failed += 1
            if failed >= threshold:
                halted, halt_reason = True, err2 or f"PATCH /identities/{existing['identityId']} (terminate) returned {resp2.status_code}"
                break
            continue

        terminated += 1
        removed_keys.add(key)

        if not provisioning_targets:
            log.info(
                "identity %s terminated; source instance %s has no provisioningTargets configured — "
                "no disable-account tasks dispatched", existing["identityId"], instance_id,
            )
            succeeded += 1
            continue

        failed_targets: list[str] = []
        for ti in range(len(provisioning_targets)):
            connector_type = provisioning_targets[ti]
            resp3, err3 = await _call(provisioning_http.post(
                "/tasks", json=_disable_account_task(feed_run_id, instance_id, existing["identityId"], key, connector_type)
            ))
            if err3 or resp3.status_code != 202:
                failed += 1
                failed_targets.append(connector_type)
                if failed >= threshold:
                    halted = True
                    halt_reason = err3 or f"POST /tasks (disable-account, {connector_type}) returned {resp3.status_code}"
                    failed_targets.extend(provisioning_targets[ti + 1:])
                    break
        if failed_targets:
            failed_target_dispatches[key] = failed_targets
        else:
            succeeded += 1
        if halted:
            break

    return {
        "terminated": terminated, "succeeded": succeeded, "attempted": attempted, "failed": failed,
        "halted": halted, "halt_reason": halt_reason, "removed_keys": removed_keys,
        "failed_target_dispatches": failed_target_dispatches,
    }


async def _process(
    credential,
    http: httpx.AsyncClient,
    identity_http: httpx.AsyncClient,
    provisioning_http: httpx.AsyncClient,
    instance_id: str,
    feed_run_id: str,
    blob_path: str,
    mappings: list[dict],
    provisioning_targets: list[str],
) -> dict[str, Any]:
    async with BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net", credential=credential
    ) as blob_service:
        raw_container = blob_service.get_container_client(RAW_CONTAINER)
        curated_container = blob_service.get_container_client(CURATED_CONTAINER)

        try:
            source_blob = raw_container.get_blob_client(blob_path)
            downloader = await source_blob.download_blob()
            content = await downloader.readall()
        except ResourceNotFoundError:
            raise IngestError(f"blob '{blob_path}' not found in container '{RAW_CONTAINER}'")

        checksum_note = await _verify_checksum(raw_container, blob_path, content)

        key_mappings = [m for m in mappings if m["isKey"]]
        if not key_mappings:
            raise IngestError("no attribute mapping is marked isKey — cannot compute a correlation key")

        required_columns = {m["sourceAttribute"] for m in mappings}
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        header = set(reader.fieldnames or [])
        missing = required_columns - header
        if missing:
            raise IngestError(f"CSV is missing mapped column(s): {sorted(missing)}")

        unknown_transforms: set[str] = set()
        quarantined: list[tuple[int, dict, str]] = []
        rows_by_key: dict[str, list[tuple[int, dict]]] = {}

        for row_num, row in enumerate(reader, start=2):
            target: dict[str, Any] = {}
            row_bad = False
            for m in mappings:
                raw_val = row.get(m["sourceAttribute"], "") or ""
                transform_name = m.get("transform")
                if transform_name:
                    fn = _TRANSFORMS.get(transform_name)
                    if fn is None:
                        unknown_transforms.add(transform_name)
                    else:
                        try:
                            raw_val = fn(raw_val)
                        except Exception as e:  # noqa: BLE001 - any transform failure quarantines the row
                            quarantined.append((row_num, row, f"transform '{transform_name}' failed: {e}"))
                            row_bad = True
                            break
                target[m["targetAttribute"]] = raw_val
            if row_bad:
                continue

            key_parts = [str(target[m["targetAttribute"]]).strip() for m in key_mappings]
            if any(not part for part in key_parts):
                quarantined.append((row_num, row, "missing required key attribute value"))
                continue

            correlation_key = "|".join(key_parts)
            rows_by_key.setdefault(correlation_key, []).append((row_num, target))

        duplicate_keys = {k: v for k, v in rows_by_key.items() if len(v) > 1}
        unmatched_count = sum(len(v) for v in duplicate_keys.values())
        unique_rows = {k: v[0][1] for k, v in rows_by_key.items() if len(v) == 1}

        previous_keys, pending_dispatches = await _load_state(curated_container, instance_id)
        pending_at_start = dict(pending_dispatches)

        retry = await _retry_pending_dispatches(
            identity_http, provisioning_http, instance_id, feed_run_id,
            pending_dispatches, APPLY_FAILURE_THRESHOLD,
        )
        pending_dispatches = retry["remaining"]
        apply_failed = retry["failed"]
        halted, halt_reason = retry["halted"], retry["halt_reason"]

        apply: dict[str, Any] = {"added": 0, "updated": 0, "succeeded": 0, "attempted": 0, "applied_keys": set()}
        if not halted:
            apply = await _apply_added_updated(
                identity_http, instance_id, unique_rows, APPLY_FAILURE_THRESHOLD, apply_failed
            )
            apply_failed = apply["failed"]
            halted, halt_reason = apply["halted"], apply["halt_reason"]

        known_keys = set(previous_keys) | apply["applied_keys"]

        terminated = term_succeeded = term_attempted = 0
        failed_target_dispatches: dict[str, list[str]] = {}
        if not halted:
            terminated_keys = set(previous_keys) - set(unique_rows) - set(duplicate_keys)
            term = await _apply_terminations(
                identity_http, provisioning_http, instance_id, feed_run_id,
                provisioning_targets, terminated_keys, APPLY_FAILURE_THRESHOLD, apply_failed,
            )
            terminated = term["terminated"]
            term_succeeded = term["succeeded"]
            term_attempted = term["attempted"]
            apply_failed = term["failed"]
            halted, halt_reason = term["halted"], term["halt_reason"]
            known_keys -= term["removed_keys"]
            failed_target_dispatches = term["failed_target_dispatches"]

        pending_dispatches.update(failed_target_dispatches)

        # Persist exactly what was durably applied, halted or not — no
        # rollback, so the next run resumes from real state (REQ-COR-SRC-009).
        # pendingProvisioningDispatch ensures a partially-dispatched
        # termination's remaining task(s) are never silently lost.
        await _save_state(curated_container, instance_id, known_keys, pending_dispatches)

        quarantine_path = None
        if quarantined:
            quarantine_path = f"quarantine/{instance_id}/{feed_run_id}.csv"
            await _write_quarantine(raw_container, quarantine_path, quarantined)

        records_processed = sum(len(v) for v in rows_by_key.values()) + len(quarantined)

        notes = []
        if checksum_note:
            notes.append(checksum_note)
        else:
            notes.append("no checksum reference found (.md5 sidecar or blob Content-MD5) — integrity unverified")
        if unknown_transforms:
            notes.append(f"unrecognized transform(s) ignored: {sorted(unknown_transforms)}")
        if quarantine_path:
            notes.append(f"{len(quarantined)} row(s) quarantined at {RAW_CONTAINER}/{quarantine_path}")
        if pending_at_start:
            notes.append(
                f"provisioning-dispatch retries from a prior run: {retry['attempted']} attempted, "
                f"{retry['succeeded']} succeeded, {len(retry['remaining'])} correlation key(s) still pending"
            )

        total_attempted = retry["attempted"] + apply["attempted"] + term_attempted
        total_succeeded = retry["succeeded"] + apply["succeeded"] + term_succeeded
        apply_note = (
            f"identity-service/provisioning-service apply: {total_attempted} row(s) attempted, "
            f"{total_succeeded} succeeded, {apply_failed} apply failure(s)"
        )
        if halted:
            apply_note += f" — HALTED (REQ-COR-SRC-009): {halt_reason}; remaining rows in this run were not processed"
        notes.append(apply_note)

        status = "failed" if halted else ("partial" if quarantined else "succeeded")
        summary = {
            "status": status,
            "completedAt": _now().isoformat(),
            "recordsProcessed": records_processed,
            "recordsAdded": apply["added"],
            "recordsUpdated": apply["updated"],
            "recordsTerminated": terminated,
            "recordsUnmatched": unmatched_count,
            "recordsQuarantined": len(quarantined),
            "errorSummary": "; ".join(notes) if notes else None,
        }
        await http.patch(f"/feed-runs/{feed_run_id}", json=summary)
        summary["feedRunId"] = feed_run_id
        return summary


async def _load_state(curated_container, instance_id: str) -> tuple[set[str], dict[str, list[str]]]:
    """Connector-owned state for this source instance: the set of
    correlation keys known active as of the last completed run (used
    solely to detect terminations — add/update decisions and identity data
    come from identity-service itself), and any provisioning-task
    dispatches that failed and still need a retry (correlationKey -> list
    of connectorType targets not yet successfully dispatched)."""
    try:
        client = curated_container.get_blob_client(f"source-state/{instance_id}/latest.json")
        downloader = await client.download_blob()
        raw = await downloader.readall()
        doc = json.loads(raw)
        return set(doc.get("correlationKeys", [])), dict(doc.get("pendingProvisioningDispatch", {}))
    except ResourceNotFoundError:
        return set(), {}


async def _save_state(
    curated_container, instance_id: str, known_keys: set[str], pending_dispatches: dict[str, list[str]]
) -> None:
    client = curated_container.get_blob_client(f"source-state/{instance_id}/latest.json")
    await client.upload_blob(
        json.dumps({
            "correlationKeys": sorted(known_keys),
            "pendingProvisioningDispatch": {k: sorted(v) for k, v in pending_dispatches.items() if v},
        }).encode("utf-8"),
        overwrite=True,
    )


async def _write_quarantine(raw_container, path: str, rows: list[tuple[int, dict, str]]) -> None:
    buf = io.StringIO()
    fieldnames = sorted({k for _, row, _ in rows for k in row.keys()}) + ["_sourceRowNumber", "_quarantineReason"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row_num, row, reason in rows:
        writer.writerow({**row, "_sourceRowNumber": row_num, "_quarantineReason": reason})
    client = raw_container.get_blob_client(path)
    await client.upload_blob(buf.getvalue().encode("utf-8"), overwrite=True)
