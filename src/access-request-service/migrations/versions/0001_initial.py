"""initial schema — requests, line_items, approval_steps

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
        "requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("requesterIdentityId", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_requests_requester", "requests", ["requesterIdentityId"])

    op.create_table(
        "line_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("requestId", sa.String(36), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("targetSystemInstanceId", sa.String(36), nullable=False),
        sa.Column("connectorType", sa.String(50), nullable=False),
        sa.Column("entitlementRef", sa.String(500), nullable=False),
        sa.Column("justification", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lastModifiedDate", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_line_items_request", "line_items", ["requestId"])

    op.create_table(
        "approval_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lineItemId", sa.String(36), sa.ForeignKey("line_items.id"), nullable=False),
        sa.Column("sequenceOrder", sa.Integer, nullable=False),
        sa.Column("stepType", sa.String(20), nullable=False),
        sa.Column("approverIdentityId", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decidedByIdentityId", sa.String(36), nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("createdDate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decidedDate", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_steps_line_item", "approval_steps", ["lineItemId"])


def downgrade() -> None:
    op.drop_index("ix_approval_steps_line_item", table_name="approval_steps")
    op.drop_table("approval_steps")
    op.drop_index("ix_line_items_request", table_name="line_items")
    op.drop_table("line_items")
    op.drop_index("ix_requests_requester", table_name="requests")
    op.drop_table("requests")
