"""
Flat-file connector core (REQ-COR-SRC-002, REQ-COR-ID-006).

Stateless connector: it owns no database of its own. SourceSystemInstance,
AttributeMapping, and FeedRun all live in source-system-service (2.1) — this
connector reads mappings and reports results there over HTTP.

Delta semantics (added/updated/terminated/unmatched), scoped to 2.2:
there is no identity-service integration yet (that's 2.3), so "added" /
"updated" / "terminated" are computed against this connector's OWN previous
snapshot of the file, stored at
`curated/source-state/<instanceId>/latest.json` (keyed by correlation key,
valued by a content hash of the mapped attributes) — not against real
identity records. Each feed file is assumed to be a full snapshot of the
source population (common for HR flat-file extracts): a correlation key
present in the previous snapshot but absent from the current file is
"terminated". "unmatched" is repurposed for 2.2 as *ambiguous correlation
within a single file*: two or more rows resolving to the same correlation
key, which cannot be safely applied as added/updated without knowing which
is authoritative. True "does this correlate to a known identity" unmatched
semantics arrive with 2.3. See roadmap/PHASES.md 2.2 note.

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

import httpx
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient

log = logging.getLogger("flatfile-connector-service")

STORAGE_ACCOUNT = os.environ.get("STORAGE_ACCOUNT_NAME", "")
RAW_CONTAINER = "raw"
CURATED_CONTAINER = "curated"
SOURCE_SYSTEM_SERVICE_URL = os.environ.get("SOURCE_SYSTEM_SERVICE_URL", "http://source-system-service")

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


def _content_hash(target: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(target, sort_keys=True).encode("utf-8")).hexdigest()


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
        async with httpx.AsyncClient(base_url=SOURCE_SYSTEM_SERVICE_URL, timeout=30.0) as http:
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

            summary = await _process(credential, http, instance_id, feed_run_id, blob_path, mappings)
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


async def _process(
    credential, http: httpx.AsyncClient, instance_id: str, feed_run_id: str, blob_path: str, mappings: list[dict]
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

        previous_snapshot = await _load_snapshot(curated_container, instance_id)

        added = updated = 0
        new_snapshot = dict(previous_snapshot)
        for key, target in unique_rows.items():
            content_hash = _content_hash(target)
            if key not in previous_snapshot:
                added += 1
            elif previous_snapshot[key] != content_hash:
                updated += 1
            new_snapshot[key] = content_hash

        terminated_keys = set(previous_snapshot) - set(unique_rows) - set(duplicate_keys)
        for key in terminated_keys:
            del new_snapshot[key]
        terminated = len(terminated_keys)

        await _save_snapshot(curated_container, instance_id, new_snapshot)

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

        status = "partial" if quarantined else "succeeded"
        summary = {
            "status": status,
            "completedAt": _now().isoformat(),
            "recordsProcessed": records_processed,
            "recordsAdded": added,
            "recordsUpdated": updated,
            "recordsTerminated": terminated,
            "recordsUnmatched": unmatched_count,
            "recordsQuarantined": len(quarantined),
            "errorSummary": "; ".join(notes) if notes else None,
        }
        await http.patch(f"/feed-runs/{feed_run_id}", json=summary)
        summary["feedRunId"] = feed_run_id
        return summary


async def _load_snapshot(curated_container, instance_id: str) -> dict[str, str]:
    try:
        client = curated_container.get_blob_client(f"source-state/{instance_id}/latest.json")
        downloader = await client.download_blob()
        raw = await downloader.readall()
        return json.loads(raw)
    except ResourceNotFoundError:
        return {}


async def _save_snapshot(curated_container, instance_id: str, snapshot: dict[str, str]) -> None:
    client = curated_container.get_blob_client(f"source-state/{instance_id}/latest.json")
    await client.upload_blob(json.dumps(snapshot, sort_keys=True).encode("utf-8"), overwrite=True)


async def _write_quarantine(raw_container, path: str, rows: list[tuple[int, dict, str]]) -> None:
    buf = io.StringIO()
    fieldnames = sorted({k for _, row, _ in rows for k in row.keys()}) + ["_sourceRowNumber", "_quarantineReason"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row_num, row, reason in rows:
        writer.writerow({**row, "_sourceRowNumber": row_num, "_quarantineReason": reason})
    client = raw_container.get_blob_client(path)
    await client.upload_blob(buf.getvalue().encode("utf-8"), overwrite=True)
