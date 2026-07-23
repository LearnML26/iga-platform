"""
ORM models for the Source System Service (REQ-COR-SRC-001).
SourceSystemInstance -> AttributeMapping (1:N), SourceSystemInstance -> FeedRun (1:N).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SourceSystemInstance(Base):
    __tablename__ = "source_system_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    connectorType: Mapped[str] = mapped_column(String(50), nullable=False)  # 'flat-file' | 'ldap' | 'scim' | ...
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")  # active | inactive
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Connector instance IDs (target systems) that should receive a
    # disable-account provisioning task when this source marks an identity
    # terminated (2.3 termination -> provisioning-task trigger). Empty by
    # default: an instance must opt in explicitly, nothing is wired
    # automatically off connectorType.
    provisioningTargets: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Who administers/approves access to this target system (3.2's
    # "manager -> owner" default approval chain resolves the owner step from
    # here). Optional: an instance with no owner set just skips that step
    # rather than blocking a request forever.
    ownerIdentityId: Mapped[str | None] = mapped_column(String(36), nullable=True)
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    attributeMappings: Mapped[list["AttributeMapping"]] = relationship(
        back_populates="sourceSystemInstance", cascade="all, delete-orphan"
    )
    feedRuns: Mapped[list["FeedRun"]] = relationship(
        back_populates="sourceSystemInstance", cascade="all, delete-orphan"
    )


class AttributeMapping(Base):
    __tablename__ = "attribute_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sourceSystemInstanceId: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_system_instances.id"), nullable=False
    )
    sourceAttribute: Mapped[str] = mapped_column(String(200), nullable=False)
    targetAttribute: Mapped[str] = mapped_column(String(200), nullable=False)
    transform: Mapped[str | None] = mapped_column(String(500), nullable=True)
    isKey: Mapped[bool] = mapped_column(default=False)  # used to build correlationKey (REQ-COR-ID-002)
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    sourceSystemInstance: Mapped["SourceSystemInstance"] = relationship(back_populates="attributeMappings")


class FeedRun(Base):
    __tablename__ = "feed_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sourceSystemInstanceId: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_system_instances.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | running | succeeded | failed | partial
    triggeredBy: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    startedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Delta summary (REQ-COR-ID-006) — populated by the flat-file connector in 2.2
    recordsProcessed: Mapped[int] = mapped_column(Integer, default=0)
    recordsAdded: Mapped[int] = mapped_column(Integer, default=0)
    recordsUpdated: Mapped[int] = mapped_column(Integer, default=0)
    recordsTerminated: Mapped[int] = mapped_column(Integer, default=0)
    recordsUnmatched: Mapped[int] = mapped_column(Integer, default=0)
    recordsQuarantined: Mapped[int] = mapped_column(Integer, default=0)

    errorSummary: Mapped[str | None] = mapped_column(Text, nullable=True)

    sourceSystemInstance: Mapped["SourceSystemInstance"] = relationship(back_populates="feedRuns")
