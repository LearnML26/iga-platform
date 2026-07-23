"""initial schema — roles, role_entitlements, role_membership_rules,
role_assignments, role_versions, platform_roles

Revision ID: 0001
Revises:
Create Date: 2026-07-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "role_entitlements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("roleId", sa.String(36), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("targetSystemInstanceId", sa.String(36), nullable=False),
        sa.Column("connectorType", sa.String(50), nullable=False),
        sa.Column("entitlementRef", sa.String(500), nullable=False),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_role_entitlements_role", "role_entitlements", ["roleId"])

    op.create_table(
        "role_membership_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("roleId", sa.String(36), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("criteria", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_role_membership_rules_role", "role_membership_rules", ["roleId"])

    op.create_table(
        "role_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("roleId", sa.String(36), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("identityId", sa.String(36), nullable=False),
        sa.Column("assignmentType", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revokedDate", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_role_assignments_role", "role_assignments", ["roleId"])
    op.create_index("ix_role_assignments_identity", "role_assignments", ["identityId"])

    op.create_table(
        "role_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("roleId", sa.String(36), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("changedBy", sa.String(100), nullable=False, server_default="api"),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_role_versions_role", "role_versions", ["roleId"])

    op.create_table(
        "platform_roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("permissions", sa.JSON, nullable=False),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_roles")
    op.drop_index("ix_role_versions_role", table_name="role_versions")
    op.drop_table("role_versions")
    op.drop_index("ix_role_assignments_identity", table_name="role_assignments")
    op.drop_index("ix_role_assignments_role", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_index("ix_role_membership_rules_role", table_name="role_membership_rules")
    op.drop_table("role_membership_rules")
    op.drop_index("ix_role_entitlements_role", table_name="role_entitlements")
    op.drop_table("role_entitlements")
    op.drop_table("roles")
