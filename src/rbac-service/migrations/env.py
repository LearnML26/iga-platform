"""Alembic env — runs against Azure SQL with the same Entra token auth as the
app (app/db.py). Invoked by the migrate Job before the Deployment rolls out."""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import Base, engine
from app.models import (  # noqa: F401  (register models)
    PlatformRole,
    Role,
    RoleAssignment,
    RoleEntitlement,
    RoleMembershipRule,
    RoleVersion,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url="mssql+aioodbc://",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    assert isinstance(engine, AsyncEngine)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
