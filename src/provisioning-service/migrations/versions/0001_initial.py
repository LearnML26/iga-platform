"""initial schema — provisioning_task_records (Phase 3.5 task-state store)

Revision ID: 0001
Revises:
Create Date: 2026-07-24
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
        "provisioning_task_records",
        sa.Column("taskId", sa.String(36), primary_key=True),
        sa.Column("sourceType", sa.String(50), nullable=False),
        sa.Column("sourceRef", sa.String(200), nullable=False),
        sa.Column("identityId", sa.String(64), nullable=False),
        sa.Column("instanceId", sa.String(64), nullable=False),
        sa.Column("connectorType", sa.String(50), nullable=False),
        sa.Column("operationType", sa.String(30), nullable=False),
        sa.Column("entitlementRef", sa.String(500), nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attemptCount", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lastError", sa.Text, nullable=True),
        sa.Column("nextAttemptAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ptr_status", "provisioning_task_records", ["status"])
    op.create_index("ix_ptr_identity", "provisioning_task_records", ["identityId"])
    op.create_index("ix_ptr_created", "provisioning_task_records", ["createdDate"])


def downgrade() -> None:
    op.drop_index("ix_ptr_created", table_name="provisioning_task_records")
    op.drop_index("ix_ptr_identity", table_name="provisioning_task_records")
    op.drop_index("ix_ptr_status", table_name="provisioning_task_records")
    op.drop_table("provisioning_task_records")
