"""
Provisioning Service — execution engine for target-system access changes.
Implements: REQ-COR-PROV-001..005, 007 (v1 scaffold)

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
"""
import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage, NEXT_AVAILABLE_SESSION
from azure.servicebus.exceptions import OperationTimeoutError

from .connectors import CONNECTOR_REGISTRY, ConnectorError
from .auth import require_role

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
    entitlementRef: Optional[str] = None
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
@app.post("/tasks", status_code=202, dependencies=[require_role("provisioning.write")])
async def submit_task(task: ProvisioningTask):
    task.taskId = task.taskId or str(uuid.uuid4())
    session_id = f"{task.identityId}:{task.instanceId}"  # ordering scope
    async with app.state.sb.get_queue_sender(TASK_QUEUE) as sender:
        await sender.send_messages(
            ServiceBusMessage(
                task.model_dump_json(),
                session_id=session_id,
                message_id=task.taskId,
            )
        )
    log.info("task %s queued (session %s, op %s)", task.taskId, session_id, task.operationType)
    return {"taskId": task.taskId, "status": "queued"}


# ---------------------------------------------------------------------------
# Worker — session-aware consumer with retry/backoff/DLQ
# ---------------------------------------------------------------------------
async def worker_loop() -> None:
    while True:
        try:
            async with app.state.sb.get_queue_session_receiver(
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


async def handle_message(receiver, msg) -> None:
    task = ProvisioningTask(**json.loads(str(msg)))
    task.attemptCount += 1
    log.info("executing task %s attempt %d", task.taskId, task.attemptCount)
    try:
        connector = CONNECTOR_REGISTRY.get(task.connectorType)
        if connector is None:
            raise ConnectorError(f"no connector registered for type '{task.connectorType}'")
        await connector.execute(task.operationType.value, task.model_dump())
        await receiver.complete_message(msg)
        log.info("task %s succeeded", task.taskId)
    except ConnectorError as exc:
        if task.attemptCount >= MAX_ATTEMPTS:
            # Dead-letter + alert (REQ-COR-PROV-003/004)
            await receiver.dead_letter_message(
                msg, reason="max-attempts-exceeded", error_description=str(exc)
            )
            await notify_failure(task, str(exc))
            log.error("task %s dead-lettered after %d attempts: %s",
                      task.taskId, task.attemptCount, exc)
        else:
            # Re-schedule with backoff
            delay = BACKOFF_MINUTES[min(task.attemptCount - 1, len(BACKOFF_MINUTES) - 1)]
            scheduled = datetime.now(timezone.utc) + timedelta(minutes=delay)
            async with app.state.sb.get_queue_sender(TASK_QUEUE) as sender:
                retry_msg = ServiceBusMessage(
                    task.model_dump_json(),
                    session_id=f"{task.identityId}:{task.instanceId}",
                    scheduled_enqueue_time_utc=scheduled,
                )
                await sender.send_messages(retry_msg)
            await receiver.complete_message(msg)
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
            "occurredAt": datetime.now(timezone.utc).isoformat(),
        })))
