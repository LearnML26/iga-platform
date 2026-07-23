"""
ORM models for the Access Request Service (REQ-COR-REQ-001..003, 006, 007, 009).
Request -> LineItem (1:N), LineItem -> ApprovalStep (1:N).

No spec document exists in this repo (checked, same as rbac-service before
it) — PHASES.md 3.2's one-line summary is the only source. Every shape
decision below that isn't directly stated there is flagged as an
interpretation in main.py's module docstring, not presented as spec fact.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Request(Base):
    """A requester asking for one or more entitlements for themselves.
    v1 scope: self-service only — no on-behalf-of/delegated requesting
    (PHASES.md 3.2's summary doesn't mention it, and there's no existing
    per-user auth to attribute a delegated request to a real actor yet;
    see main.py for the same gap on approval decisions).
    status is a rollup, not independently decided: 'pending' while any
    line item is non-terminal, 'completed' once every line item has
    reached a terminal state (approved+dispatched, rejected, or
    dispatch_failed)."""

    __tablename__ = "requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    requesterIdentityId: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | completed | cancelled
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    lineItems: Mapped[list["LineItem"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class LineItem(Base):
    """One requested entitlement. Shape (targetSystemInstanceId +
    connectorType + entitlementRef) deliberately mirrors rbac-service's
    RoleEntitlement rather than referencing a RoleEntitlement by id — the
    same "target-system-instance registry is the requestable-item registry"
    precedent used by 2.3/3.1, avoiding a cross-service foreign key. This
    means a request-sourced grant does NOT create a rbac-service
    RoleAssignment even though RoleAssignment.assignmentType already has a
    'request' enum value reserved for it — that wiring is a real,
    documented gap for a v-next pass (see main.py docstring), not silently
    assumed."""

    __tablename__ = "line_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    requestId: Mapped[str] = mapped_column(String(36), ForeignKey("requests.id"), nullable=False)
    targetSystemInstanceId: Mapped[str] = mapped_column(String(36), nullable=False)
    connectorType: Mapped[str] = mapped_column(String(50), nullable=False)
    entitlementRef: Mapped[str] = mapped_column(String(500), nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | approved | rejected | dispatched | dispatch_failed | cancelled
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    request: Mapped["Request"] = relationship(back_populates="lineItems")
    approvalSteps: Mapped[list["ApprovalStep"]] = relationship(
        back_populates="lineItem", cascade="all, delete-orphan", order_by="ApprovalStep.sequenceOrder"
    )


class ApprovalStep(Base):
    """One step in a line item's approval chain, built once at request
    creation (REQ-COR-REQ summary: "default chain manager -> owner").
    manager is resolved from the requester's own identity-service record
    (managerIdentityId); owner is resolved from the line item's target
    system instance (source-system-service's new ownerIdentityId field,
    added in this pass). Either step is skipped immediately (never blocks)
    if its identity can't be resolved — there's no "fallback approver"
    concept anywhere in this codebase to fall back to instead, so "skip"
    is the least-invented default; flagged here rather than assumed."""

    __tablename__ = "approval_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lineItemId: Mapped[str] = mapped_column(String(36), ForeignKey("line_items.id"), nullable=False)
    sequenceOrder: Mapped[int] = mapped_column(Integer, nullable=False)
    stepType: Mapped[str] = mapped_column(String(20), nullable=False)  # manager | owner
    approverIdentityId: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | approved | rejected | skipped | cancelled
    decidedByIdentityId: Mapped[str | None] = mapped_column(String(36), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decidedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lineItem: Mapped["LineItem"] = relationship(back_populates="approvalSteps")
