"""
ORM models for the RBAC Service (REQ-COR-RBAC-001..004, 007..009).
Role -> RoleEntitlement (1:N), Role -> RoleMembershipRule (1:N),
Role -> RoleAssignment (1:N), Role -> RoleVersion (1:N, append-only history).
PlatformRole is a separate, standalone entity — see its own docstring for
what's (deliberately) not implemented for it in this pass.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")  # active | inactive
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # REQ-COR-RBAC-007
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    entitlements: Mapped[list["RoleEntitlement"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    membershipRules: Mapped[list["RoleMembershipRule"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    assignments: Mapped[list["RoleAssignment"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    versions: Mapped[list["RoleVersion"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class RoleEntitlement(Base):
    __tablename__ = "role_entitlements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    roleId: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id"), nullable=False)
    # source-system-service instance id, dual-purposed as the target-system
    # registry — same precedent as 2.3's provisioning task instanceId (no
    # separate target-instance registry exists in this platform yet).
    targetSystemInstanceId: Mapped[str] = mapped_column(String(36), nullable=False)
    connectorType: Mapped[str] = mapped_column(String(50), nullable=False)  # 'ad' | 'entra' | ... (provisioning-service CONNECTOR_REGISTRY key)
    entitlementRef: Mapped[str] = mapped_column(String(500), nullable=False)  # target-specific identifier (AD group DN, Entra group objectId, ...)
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    role: Mapped["Role"] = relationship(back_populates="entitlements")


class RoleMembershipRule(Base):
    __tablename__ = "role_membership_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    roleId: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id"), nullable=False)
    # Equality match, ANDed within one rule; a role's rules are ORed against
    # each other (see app/main.py's reconcile for the exact semantics).
    criteria: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    role: Mapped["Role"] = relationship(back_populates="membershipRules")


class RoleAssignment(Base):
    __tablename__ = "role_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    roleId: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id"), nullable=False)
    identityId: Mapped[str] = mapped_column(String(36), nullable=False)  # identity-service identity; no FK, different database
    assignmentType: Mapped[str] = mapped_column(String(20), nullable=False)  # rule | manual | request
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")  # active | revoked
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revokedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped["Role"] = relationship(back_populates="assignments")


class RoleVersion(Base):
    """Append-only version history (REQ-COR-RBAC-007) — a full snapshot of
    the role's own fields plus its entitlement list at the moment version
    incremented. Membership rules and assignments are NOT versioned here —
    they're operational state, not the role's definition."""
    __tablename__ = "role_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    roleId: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    changedBy: Mapped[str] = mapped_column(String(100), nullable=False, default="api")
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    role: Mapped["Role"] = relationship(back_populates="versions")


class PlatformRole(Base):
    """IGA platform's own admin/operator roles (who can administer the IGA
    tool itself — e.g. role-owner, certifier, platform-admin), distinct
    from the business Role model above. Scoped to CRUD only in this pass:
    actual enforcement (checking a caller holds a given PlatformRole before
    allowing an action) is NOT implemented — there is no existing mechanism
    here for binding a PlatformRole to a human operator's own identity or
    token, and the full spec text for that isn't available to this build.
    Tracked as a known gap, not silently assumed away."""
    __tablename__ = "platform_roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    permissions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # e.g. ["roles.manage", "certifications.review"]
    createdDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lastModifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
