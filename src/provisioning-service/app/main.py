"""
Provisioning Service — execution engine for target-system access changes.
Implements: REQ-COR-PROV-001..005, 007 (v1 scaffold) + the task-state store
behind the admin console's provisioning queue (Phase 3.5, REQ-UI-022).

Architecture:
- FastAPI app exposes task submission + status APIs and health probes.
- A background worker consumes session-enabled 'provisioning-tasks' from
  Service Bus (sessions keyed on identityId:instanceId → ordered writes,
  REQ-COR-PROV-002 / REQ-INF-053).
- Failures are retried with exponential backoff by re-scheduling onto the
  queue (REQ-COR-PROV-003); after MAX_ATTEMPTS the task is dead-lettered and
  a notification message is emitted (REQ-COR-PROV-004).
- Connector dispatch is a pluggable registry — the AD and Entra connectors
  register their handlers here (grant/revoke are idempotent per
  REQ-COR-PROV-007: verify-before-write inside each connector).
- Phase 3.5: every task also gets a row in sqldb-provisioning
  (ProvisioningTaskRecord) — the queue remains the execution mechanism,
  unchanged; the table is the queryable projection the admin console lists,
  retries, and cancels against. The database was in data.bicep's
  serviceDatabases list from day one; this pass finally uses it.
  Auth note: list/get/retry/cancel are gated on provisioning.write (the
  only app role this service has) rather than a new provisioning.read —
  adding a read role would mean another Graph [HUMAN] gate for every
  caller, and nothing currently needs read-but-not-write. Flagged as a
  deliberate coarse-grained choice, not an oversight.
  Retry semantics: re-enqueues a FRESH message from the stored record with
  attemptCount reset. If the original message was dead-lettered, its DLQ
  copy is NOT removed by retry (Service Bus has no selective DLQ delete) —
  scripts/drain-provisioning-dlq.sh remains the cleanup for that.
  Cancel semantics: queued/retry-scheduled only. The queue message can't be
  selectively deleted either, so cancel marks the record and the worker
  completes the message WITHOUT executing when it sees status=cancelled.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import NEXT_AVAILABLE_SESSION, ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import OperationTimeoutError
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_role
from .connectors import CONNECTOR_REGISTRY, ConnectorError
from .db import SessionLocal, engine, get_session
from .models import ProvisioningTaskRecord

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("provisioning-service")

SB_NAMESPACE = os.environ.get("SERVICEBUS_NAMESPACE", "")  # sb-iga-dev.servicebus.windows.net
TASK_QUEUE = "provisioning-tasks"
NOTIFY_QUEUE = "notification-tasks"
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
# Exponential backoff schedule (REQ-COR-PROV-003)
BACKOFF_MINUTES = [1, 5, 30, 120, 720]

app = FastAPI(title="IGA Provisioning Service", version="1.0.0")


class OperationType(str, Enum):
    grant = "grant"
    revoke = "revoke"
    create_account = "create-account"
    disable_account = "disable-account"


class ProvisioningTask(BaseModel):
    taskId: str = ""
    sourceType: str  # access-request | role-assignment | certification-revoke | rule | manual
    sourceRef: str
    identityId: str
    instanceId: str
    connectorType: str  # 'ad' | 'entra' | ...
    operationType: OperationType
    entitlementRef: str | None = None
    payload: dict = {}
    attemptCount: int = 0


@app.on_event("startup")
async def startup() -> None:
    app.state.ready = False
    app.state.credential = DefaultAzureCredential()
    app.state.sb = ServiceBusClient(
        fully_qualified_namespace=SB_NAMESPACE, credential=app.state.credential
    )
    app.state.worker = asyncio.create_task(worker_loop())
    app.state.ready = True
    log.info("Provisioning Service started; sb=%s", SB_NAMESPACE)


@app.on_event("shutdown")
async def shutdown() -> None:
    app.state.worker.cancel()
    await app.state.sb.close()
    await engine.dispose()
    await app.state.credential.close()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not getattr(app.state, "ready", False):
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


# ---------------------------------------------------------------------------
# Task submission API (called by Access Request / RBAC / Certification / Rules
# services — REQ-COR-PROV-001)
# ---------------------------------------------------------------------------
async def _send_task_message(task: ProvisioningTask, scheduled: datetime | None = None) -> None:
    session_id = f"{task.identityId}:{task.instanceId}"  # ordering scope
    msg = ServiceBusMessage(
        task.model_dump_json(),
        session_id=session_id,
        message_id=str(uuid.uuid4()),  # unique per send: retries must not be de-duplicated
    )
    if scheduled is not None:
        msg.scheduled_enqueue_time_utc = scheduled
    async with app.state.sb.get_queue_sender(TASK_QUEUE) as sender:
        await sender.send_messages(msg)


@app.post("/tasks", status_code=202, dependencies=[require_role("provisioning.write")])
async def submit_task(task: ProvisioningTask, session: AsyncSession = Depends(get_session)):
    task.taskId = task.taskId or str(uuid.uuid4())
    # Record first, then message — the worker can receive the message
    # milliseconds later and must find the row (it backfills a missing row
    # for pre-migration messages, but new tasks should never need that).
    session.add(ProvisioningTaskRecord(
        taskId=task.taskId,
        sourceType=task.sourceType,
        sourceRef=task.sourceRef,
        identityId=task.identityId,
        instanceId=task.instanceId,
        connectorType=task.connectorType,
        operationType=task.operationType.value,
        entitlementRef=task.entitlementRef,
        payload=task.payload,
        status="queued",
    ))
    await session.commit()
    await _send_task_message(task)
    log.info("task %s queued (session %s:%s, op %s)",
             task.taskId, task.identityId, task.instanceId, task.operationType)
    return {"taskId": task.taskId, "status": "queued"}


# ---------------------------------------------------------------------------
# Task queue APIs (Phase 3.5 — admin console list/retry/cancel, REQ-UI-022)
# ---------------------------------------------------------------------------
def _record_out(r: ProvisioningTaskRecord) -> dict:
    return {
        "taskId": r.taskId, "sourceType": r.sourceType, "sourceRef": r.sourceRef,
        "identityId": r.identityId, "instanceId": r.instanceId,
        "connectorType": r.connectorType, "operationType": r.operationType,
        "entitlementRef": r.entitlementRef, "payload": r.payload,
        "status": r.status, "attemptCount": r.attemptCount, "lastError": r.lastError,
        "nextAttemptAt": r.nextAttemptAt.isoformat() if r.nextAttemptAt else None,
        "createdDate": r.createdDate.isoformat() if r.createdDate else None,
        "lastModifiedDate": r.lastModifiedDate.isoformat() if r.lastModifiedDate else None,
    }


@app.get("/tasks", dependencies=[require_role("provisioning.write")])
async def list_tasks(
    status: str | None = None,
    identityId: str | None = None,
    instanceId: str | None = None,
    sourceType: str | None = None,
    connectorType: str | None = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(ProvisioningTaskRecord)
    if status:
        stmt = stmt.where(ProvisioningTaskRecord.status == status)
    if identityId:
        stmt = stmt.where(ProvisioningTaskRecord.identityId == identityId)
    if instanceId:
        stmt = stmt.where(ProvisioningTaskRecord.instanceId == instanceId)
    if sourceType:
        stmt = stmt.where(ProvisioningTaskRecord.sourceType == sourceType)
    if connectorType:
        stmt = stmt.where(ProvisioningTaskRecord.connectorType == connectorType)
    stmt = stmt.order_by(ProvisioningTaskRecord.createdDate.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_record_out(r) for r in result.scalars().all()]


@app.get("/tasks/{task_id}", dependencies=[require_role("provisioning.write")])
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
    record = await session.get(ProvisioningTaskRecord, task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _record_out(record)


@app.post("/tasks/{task_id}/retry", dependencies=[require_role("provisioning.write")])
async def retry_task(task_id: str, session: AsyncSession = Depends(get_session)):
    """Re-enqueue a dead-lettered/cancelled task from its stored record with
    a fresh attempt budget. The dead-lettered message's DLQ copy is NOT
    removed (Service Bus has no selective DLQ delete) —
    scripts/drain-provisioning-dlq.sh remains the DLQ cleanup."""
    record = await session.get(ProvisioningTaskRecord, task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    if record.status not in ("dead-lettered", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"task is '{record.status}' — only dead-lettered/cancelled tasks can be retried",
        )
    record.status = "queued"
    record.attemptCount = 0
    record.nextAttemptAt = None
    await session.commit()
    await _send_task_message(ProvisioningTask(
        taskId=record.taskId, sourceType=record.sourceType, sourceRef=record.sourceRef,
        identityId=record.identityId, instanceId=record.instanceId,
        connectorType=record.connectorType, operationType=OperationType(record.operationType),
        entitlementRef=record.entitlementRef, payload=record.payload,
    ))
    log.info("task %s re-queued via admin retry", task_id)
    return _record_out(record)


@app.post("/tasks/{task_id}/cancel", dependencies=[require_role("provisioning.write")])
async def cancel_task(task_id: str, session: AsyncSession = Depends(get_session)):
    """Cancel a not-yet-executed task. The queue message can't be selectively
    deleted, so this marks the record; the worker completes the message
    without executing when it sees status=cancelled."""
    record = await session.get(ProvisioningTaskRecord, task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    if record.status not in ("queued", "retry-scheduled"):
        raise HTTPException(
            status_code=409,
            detail=f"task is '{record.status}' — only queued/retry-scheduled tasks can be cancelled",
        )
    record.status = "cancelled"
    await session.commit()
    log.info("task %s cancelled", task_id)
    return _record_out(record)


# ---------------------------------------------------------------------------
# Worker — session-aware consumer with retry/backoff/DLQ
# ---------------------------------------------------------------------------
async def worker_loop() -> None:
    while True:
        try:
            async with app.state.sb.get_queue_receiver(
                TASK_QUEUE, session_id=NEXT_AVAILABLE_SESSION, max_wait_time=30
            ) as receiver:
                async for msg in receiver:
                    await handle_message(receiver, msg)
        except OperationTimeoutError:
            continue  # normal — no session had a waiting message this cycle
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("worker loop error; restarting in 10s")
            await asyncio.sleep(10)


async def _load_or_backfill_record(session: AsyncSession, task: ProvisioningTask) -> ProvisioningTaskRecord:
    """Messages enqueued before the task-store migration have no row —
    backfill one so their lifecycle is tracked from here on."""
    record = await session.get(ProvisioningTaskRecord, task.taskId)
    if record is None:
        record = ProvisioningTaskRecord(
            taskId=task.taskId, sourceType=task.sourceType, sourceRef=task.sourceRef,
            identityId=task.identityId, instanceId=task.instanceId,
            connectorType=task.connectorType, operationType=task.operationType.value,
            entitlementRef=task.entitlementRef, payload=task.payload,
            status="queued", attemptCount=task.attemptCount,
        )
        session.add(record)
        await session.flush()
    return record


async def _set_status(task: ProvisioningTask, status: str, *,
                      attempt: int | None = None, error: str | None = None,
                      next_attempt: datetime | None = None) -> None:
    """Best-effort task-record update. A SQL blip must never break queue
    processing (the queue is the source of truth for execution) — log and
    continue rather than letting a DB error poison the message."""
    try:
        async with SessionLocal() as session:
            record = await _load_or_backfill_record(session, task)
            record.status = status
            if attempt is not None:
                record.attemptCount = attempt
            record.lastError = error
            record.nextAttemptAt = next_attempt
            await session.commit()
    except Exception:
        log.exception("task-record update failed for %s (status=%s); queue processing continues",
                      task.taskId, status)


async def _is_cancelled(task: ProvisioningTask) -> bool:
    try:
        async with SessionLocal() as session:
            record = await session.get(ProvisioningTaskRecord, task.taskId)
            return record is not None and record.status == "cancelled"
    except Exception:
        log.exception("cancel check failed for %s; proceeding with execution", task.taskId)
        return False


async def handle_message(receiver, msg) -> None:
    task = ProvisioningTask(**json.loads(str(msg)))
    if await _is_cancelled(task):
        await receiver.complete_message(msg)
        log.info("task %s cancelled — message completed without execution", task.taskId)
        return
    task.attemptCount += 1
    log.info("executing task %s attempt %d", task.taskId, task.attemptCount)
    await _set_status(task, "in-progress", attempt=task.attemptCount)
    try:
        connector = CONNECTOR_REGISTRY.get(task.connectorType)
        if connector is None:
            raise ConnectorError(f"no connector registered for type '{task.connectorType}'")
        await connector.execute(task.operationType.value, task.model_dump())
        await receiver.complete_message(msg)
        await _set_status(task, "succeeded", attempt=task.attemptCount)
        log.info("task %s succeeded", task.taskId)
    except ConnectorError as exc:
        if task.attemptCount >= MAX_ATTEMPTS:
            # Dead-letter + alert (REQ-COR-PROV-003/004)
            await receiver.dead_letter_message(
                msg, reason="max-attempts-exceeded", error_description=str(exc)
            )
            await _set_status(task, "dead-lettered", attempt=task.attemptCount, error=str(exc))
            await notify_failure(task, str(exc))
            log.error("task %s dead-lettered after %d attempts: %s",
                      task.taskId, task.attemptCount, exc)
        else:
            # Re-schedule with backoff
            delay = BACKOFF_MINUTES[min(task.attemptCount - 1, len(BACKOFF_MINUTES) - 1)]
            scheduled = datetime.now(UTC) + timedelta(minutes=delay)
            await _send_task_message(task, scheduled=scheduled)
            await receiver.complete_message(msg)
            await _set_status(task, "retry-scheduled", attempt=task.attemptCount,
                              error=str(exc), next_attempt=scheduled)
            log.warning("task %s failed (attempt %d); retry in %dm: %s",
                        task.taskId, task.attemptCount, delay, exc)


async def notify_failure(task: ProvisioningTask, error: str) -> None:
    """Emit failure notification for the Notification Service (REQ-COR-PROV-004)."""
    async with app.state.sb.get_queue_sender(NOTIFY_QUEUE) as sender:
        await sender.send_messages(ServiceBusMessage(json.dumps({
            "type": "ProvisioningFailed",
            "taskId": task.taskId,
            "identityId": task.identityId,
            "instanceId": task.instanceId,
            "operationType": task.operationType.value,
            "error": error,
            "occurredAt": datetime.now(UTC).isoformat(),
        })))
