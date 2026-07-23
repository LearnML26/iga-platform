"""
Database access for the RBAC Service — Azure SQL (sqldb-rbac),
Entra-only auth (no SQL logins). Implements REQ-COR-RBAC-001..004.

The server has azureADOnlyAuthentication enabled, so every connection presents
an Entra access token via the ODBC SQL_COPT_SS_ACCESS_TOKEN connection
attribute instead of a username/password. DefaultAzureCredential resolves to
the pod's federated workload identity in AKS (REQ-INF-031/062).
"""
import os
import struct

from azure.identity import DefaultAzureCredential
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

SQL_SERVER = os.environ.get("SQL_SERVER", "")          # e.g. sql-iga-dev.database.windows.net
SQL_DATABASE = os.environ.get("SQL_DATABASE", "sqldb-rbac")
SQL_COPT_SS_ACCESS_TOKEN = 1256
TOKEN_SCOPE = "https://database.windows.net/.default"

_credential = DefaultAzureCredential()


class Base(DeclarativeBase):
    pass


def _connection_string() -> str:
    return (
        f"mssql+aioodbc:///?odbc_connect="
        f"Driver={{ODBC Driver 18 for SQL Server}};"
        f"Server=tcp:{SQL_SERVER},1433;"
        f"Database={SQL_DATABASE};"
        f"Encrypt=yes;TrustServerCertificate=no;"
    )


engine = create_async_engine(_connection_string(), pool_pre_ping=True)


@event.listens_for(engine.sync_engine, "do_connect")
def _provide_entra_token(dialect, conn_rec, cargs, cparams) -> None:
    """Attach a fresh Entra access token to each new physical connection.

    Runs inside aioodbc's executor thread (do_connect fires on the sync
    engine), so a synchronous credential call here is correct — it must not
    be awaited.
    """
    token = _credential.get_token(TOKEN_SCOPE).token
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    cparams["attrs_before"] = {SQL_COPT_SS_ACCESS_TOKEN: token_struct}


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session():
    async with SessionLocal() as session:
        yield session
