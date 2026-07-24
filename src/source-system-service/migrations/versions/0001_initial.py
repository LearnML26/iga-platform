"""initial schema — source_system_instances, attribute_mappings, feed_runs

Revision ID: 0001
Revises:
Create Date: 2026-07-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_system_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("connectorType", sa.String(50), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "attribute_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "sourceSystemInstanceId",
            sa.String(36),
            sa.ForeignKey("source_system_instances.id"),
            nullable=False,
        ),
        sa.Column("sourceAttribute", sa.String(200), nullable=False),
        sa.Column("targetAttribute", sa.String(200), nullable=False),
        sa.Column("transform", sa.String(500), nullable=True),
        sa.Column("isKey", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_attribute_mappings_instance", "attribute_mappings", ["sourceSystemInstanceId"]
    )

    op.create_table(
        "feed_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "sourceSystemInstanceId",
            sa.String(36),
            sa.ForeignKey("source_system_instances.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("triggeredBy", sa.String(50), nullable=False, server_default="manual"),
        sa.Column("startedAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completedAt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recordsProcessed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recordsAdded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recordsUpdated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recordsTerminated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recordsUnmatched", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recordsQuarantined", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errorSummary", sa.Text, nullable=True),
    )
    op.create_index("ix_feed_runs_instance", "feed_runs", ["sourceSystemInstanceId"])


def downgrade() -> None:
    op.drop_index("ix_feed_runs_instance", table_name="feed_runs")
    op.drop_table("feed_runs")
    op.drop_index("ix_attribute_mappings_instance", table_name="attribute_mappings")
    op.drop_table("attribute_mappings")
    op.drop_table("source_system_instances")
