"""add ownerIdentityId to source_system_instances

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_system_instances",
        sa.Column("ownerIdentityId", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source_system_instances", "ownerIdentityId")
