"""
ORM model for the Provisioning Service's task-state store (Phase 3.5,
REQ-UI-022's "provisioning task queue with retry/cancel").

Before this pass, tasks existed ONLY as Service Bus messages — nothing
queryable, so an admin queue view was impossible. The queue remains the
execution mechanism (ordering, backoff, DLQ all unchanged); this table is
the queryable projection of each task's lifecycle, written by the submit
endpoint and updated by the worker at each state transition.

Status vocabulary:
  queued          — record written, message sent (or re-sent via retry)
  in-progress     — worker picked the message up and is executing
  retry-scheduled — attempt failed; a delayed retry message is on the queue
  succeeded       — connector executed cleanly
  dead-lettered   — MAX_ATTEMPTS exhausted; message moved to the DLQ
  cancelled       — admin cancelled before execution; the worker completes
                    the message without executing when it sees this status
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProvisioningTaskRecord(Base):
    __tablename__ = "provisioning_task_records"

    taskId: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sourceType: Mapped[str] = mapped_column(String(50), nullable=False)
    sourceRef: Mapped[str] = mapped_column(String(200), nullable=False)
    # 64 not 36: callers may pass non-UUID probe values (verify.sh uses
    # literal 'verify'), and nothing upstream enforces UUID shape here.
    identityId: Mapped[str] = mapped_column(String(64), nullable=False)
    instanceId: Mapped[str] = mapped_column(String(64), nullable=False)
    connectorType: Mapped[str] = mapped_column(String(50), nullable=False)
    operationType: Mapped[str] = mapped_column(String(30), nullable=False)
    entitlementRef: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attemptCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastError: Mapped[str | None] = mapped_column(Text, nullable=True)
    nextAttemptAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
