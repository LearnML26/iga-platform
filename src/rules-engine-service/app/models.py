"""
ORM models for the Rules Engine Service (REQ-COR-RULES-001..003, 006, 007).

No spec document exists in this repo (same gap as every Phase 3 service) —
PHASES.md 4.1's one-line summary is the only source: "Event Hubs consumer
(consumer group `rules-engine`); RuleDefinition + RuleExecutionLog in
sqldb-rules; attribute-change triggers re-running RBAC membership rules;
scheduled sweep loop; every evaluation logged." Every shape decision beyond
that literal text is an interpretation, flagged in main.py's module
docstring.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class RuleDefinition(Base):
    """An event-driven automation rule: when an identity-changes event of a
    matching type arrives (and its optional attribute filter matches), run
    the configured action. The only implemented actionType in this pass is
    'rbac-reconcile' — the one the 4.1 summary literally demands
    ("attribute-change triggers re-running RBAC membership rules"). The
    model is deliberately shaped to hold future actions (actionType +
    actionConfig JSON) without pretending they exist: unknown actionTypes
    are rejected at create time, not silently accepted."""

    __tablename__ = "rule_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Which identity-changes eventTypes fire this rule, e.g.
    # ["IdentityAttributeChanged", "IdentityTerminated"]. Empty = none
    # (a sweep-only rule).
    triggerEventTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Optional filter: only fire when one of these attribute names is in the
    # event's _changedFields (IdentityAttributeChanged events only; other
    # event types carry no _changedFields and skip this filter). Empty =
    # any change matches.
    changedFieldsFilter: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Also run on the scheduled sweep loop (safety net for missed events).
    runOnSweep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actionType: Mapped[str] = mapped_column(String(50), nullable=False)  # 'rbac-reconcile'
    # rbac-reconcile config: {"roleIds": [...]} — empty/missing = all active
    # roles that have enabled membership rules.
    actionConfig: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class RuleExecutionLog(Base):
    """One row per rule evaluation ("every evaluation logged",
    REQ-COR-RULES-007) — including non-matches, so the trail shows a rule
    was CONSIDERED and why it did or didn't fire. Append-only; no retention
    policy in this pass (dev-scale; flagged as a follow-up in PHASES.md)."""

    __tablename__ = "rule_execution_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ruleId: Mapped[str] = mapped_column(String(36), nullable=False)
    ruleName: Mapped[str] = mapped_column(String(200), nullable=False)
    # 'event' or 'sweep'
    triggerSource: Mapped[str] = mapped_column(String(20), nullable=False)
    # For event triggers: the event's id/type/identity. Null for sweep.
    eventId: Mapped[str | None] = mapped_column(String(36), nullable=True)
    eventType: Mapped[str | None] = mapped_column(String(50), nullable=True)
    identityId: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Why it didn't match, or what the action did. Kept as free text.
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    executedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
