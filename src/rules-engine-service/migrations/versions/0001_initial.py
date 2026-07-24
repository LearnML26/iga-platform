"""initial schema — rule_definitions, rule_execution_logs

Revision ID: 0001
Revises:
Create Date: 2026-07-24
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
        "rule_definitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("triggerEventTypes", sa.JSON, nullable=False),
        sa.Column("changedFieldsFilter", sa.JSON, nullable=False),
        sa.Column("runOnSweep", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("actionType", sa.String(50), nullable=False),
        sa.Column("actionConfig", sa.JSON, nullable=False),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "rule_execution_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ruleId", sa.String(36), nullable=False),
        sa.Column("ruleName", sa.String(200), nullable=False),
        sa.Column("triggerSource", sa.String(20), nullable=False),
        sa.Column("eventId", sa.String(36), nullable=True),
        sa.Column("eventType", sa.String(50), nullable=True),
        sa.Column("identityId", sa.String(64), nullable=True),
        sa.Column("matched", sa.Boolean, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("executedAt", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rel_rule", "rule_execution_logs", ["ruleId"])
    op.create_index("ix_rel_identity", "rule_execution_logs", ["identityId"])
    op.create_index("ix_rel_executed", "rule_execution_logs", ["executedAt"])


def downgrade() -> None:
    op.drop_index("ix_rel_executed", table_name="rule_execution_logs")
    op.drop_index("ix_rel_identity", table_name="rule_execution_logs")
    op.drop_index("ix_rel_rule", table_name="rule_execution_logs")
    op.drop_table("rule_execution_logs")
    op.drop_table("rule_definitions")
