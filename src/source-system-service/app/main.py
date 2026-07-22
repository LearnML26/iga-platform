"""
Source System Service — owns SourceSystemInstance, AttributeMapping, and
FeedRun records for the identity feed pipeline.
Implements: REQ-COR-SRC-001 (subset for v1 scaffold)

- CRUD over source system instances and their attribute mappings
  (SQLAlchemy async against Azure SQL sqldb-sourcesystem, Entra token auth —
  see db.py).
- FeedRun records are created/listed here; the flat-file connector (2.2) and
  the identity-service integration (2.3) populate their delta-summary fields.
- /healthz and /readyz probes (REQ-INF-035).

Auth to Azure SQL uses DefaultAzureCredential -> workload identity in AKS
(REQ-INF-031/062). No connection strings or SQL logins anywhere.
"""
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import engine, get_session
from .models import AttributeMapping, FeedRun, SourceSystemInstance

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("source-system-service")

app = FastAPI(title="IGA Source System Service", version="1.0.0")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SourceSystemInstanceIn(BaseModel):
    name: str
    connectorType: str
    description: Optional[str] = None
    status: str = "active"
    config: dict[str, Any] = Field(default_factory=dict)
    provisioningTargets: list[str] = Field(default_factory=list)


class SourceSystemInstanceOut(SourceSystemInstanceIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    createdDate: datetime
    lastModifiedDate: datetime


class AttributeMappingIn(BaseModel):
    sourceAttribute: str
    targetAttribute: str
    transform: Optional[str] = None
    isKey: bool = False


class AttributeMappingOut(AttributeMappingIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    sourceSystemInstanceId: str
    createdDate: datetime
    lastModifiedDate: datetime


class FeedRunIn(BaseModel):
    triggeredBy: str = "manual"


class FeedRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    sourceSystemInstanceId: str
    status: str
    triggeredBy: str
    startedAt: datetime
    completedAt: Optional[datetime]
    recordsProcessed: int
    recordsAdded: int
    recordsUpdated: int
    recordsTerminated: int
    recordsUnmatched: int
    recordsQuarantined: int
    errorSummary: Optional[str]


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    # Schema is owned by Alembic (migrations/), applied by the migrate Job
    # before this Deployment rolls out — the app never creates/alters tables.
    app.state.ready = True
    log.info("Source System Service started")


@app.on_event("shutdown")
async def shutdown() -> None:
    await engine.dispose()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not getattr(app.state, "ready", False):
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


# ---------------------------------------------------------------------------
# Source system instances
# ---------------------------------------------------------------------------
@app.post("/source-systems", response_model=SourceSystemInstanceOut, status_code=201)
async def create_source_system(body: SourceSystemInstanceIn, session: AsyncSession = Depends(get_session)):
    instance = SourceSystemInstance(**body.model_dump())
    session.add(instance)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"source system '{body.name}' already exists")
    await session.refresh(instance)
    return instance


@app.get("/source-systems", response_model=list[SourceSystemInstanceOut])
async def list_source_systems(
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(SourceSystemInstance)
    if status:
        stmt = stmt.where(SourceSystemInstance.status == status)
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.get("/source-systems/{instance_id}", response_model=SourceSystemInstanceOut)
async def get_source_system(instance_id: str, session: AsyncSession = Depends(get_session)):
    instance = await session.get(SourceSystemInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="source system not found")
    return instance


@app.patch("/source-systems/{instance_id}", response_model=SourceSystemInstanceOut)
async def update_source_system(instance_id: str, patch: dict, session: AsyncSession = Depends(get_session)):
    instance = await session.get(SourceSystemInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="source system not found")
    immutable = {"id", "createdDate"}
    for k, v in patch.items():
        if k not in immutable and hasattr(instance, k):
            setattr(instance, k, v)
    await session.commit()
    await session.refresh(instance)
    return instance


@app.delete("/source-systems/{instance_id}", status_code=204)
async def delete_source_system(instance_id: str, session: AsyncSession = Depends(get_session)):
    instance = await session.get(SourceSystemInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="source system not found")
    await session.delete(instance)
    await session.commit()


# ---------------------------------------------------------------------------
# Attribute mappings
# ---------------------------------------------------------------------------
@app.post(
    "/source-systems/{instance_id}/mappings",
    response_model=AttributeMappingOut,
    status_code=201,
)
async def create_mapping(instance_id: str, body: AttributeMappingIn, session: AsyncSession = Depends(get_session)):
    instance = await session.get(SourceSystemInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="source system not found")
    mapping = AttributeMapping(sourceSystemInstanceId=instance_id, **body.model_dump())
    session.add(mapping)
    await session.commit()
    await session.refresh(mapping)
    return mapping


@app.get("/source-systems/{instance_id}/mappings", response_model=list[AttributeMappingOut])
async def list_mappings(instance_id: str, session: AsyncSession = Depends(get_session)):
    stmt = select(AttributeMapping).where(AttributeMapping.sourceSystemInstanceId == instance_id)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.delete("/source-systems/{instance_id}/mappings/{mapping_id}", status_code=204)
async def delete_mapping(instance_id: str, mapping_id: str, session: AsyncSession = Depends(get_session)):
    mapping = await session.get(AttributeMapping, mapping_id)
    if mapping is None or mapping.sourceSystemInstanceId != instance_id:
        raise HTTPException(status_code=404, detail="mapping not found")
    await session.delete(mapping)
    await session.commit()


# ---------------------------------------------------------------------------
# Feed runs
# ---------------------------------------------------------------------------
@app.post("/source-systems/{instance_id}/feed-runs", response_model=FeedRunOut, status_code=201)
async def create_feed_run(instance_id: str, body: FeedRunIn, session: AsyncSession = Depends(get_session)):
    instance = await session.get(SourceSystemInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="source system not found")
    run = FeedRun(sourceSystemInstanceId=instance_id, status="pending", **body.model_dump())
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


@app.get("/source-systems/{instance_id}/feed-runs", response_model=list[FeedRunOut])
async def list_feed_runs(
    instance_id: str,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(FeedRun)
        .where(FeedRun.sourceSystemInstanceId == instance_id)
        .order_by(FeedRun.startedAt.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@app.get("/feed-runs/{run_id}", response_model=FeedRunOut)
async def get_feed_run(run_id: str, session: AsyncSession = Depends(get_session)):
    run = await session.get(FeedRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="feed run not found")
    return run


# Connectors (e.g. the flat-file connector, 2.2) call this to report the delta
# summary and terminal status once ingestion completes. Only the fields a
# connector legitimately owns are patchable — id/sourceSystemInstanceId/
# startedAt/triggeredBy stay immutable here.
_FEED_RUN_PATCHABLE = {
    "status",
    "completedAt",
    "recordsProcessed",
    "recordsAdded",
    "recordsUpdated",
    "recordsTerminated",
    "recordsUnmatched",
    "recordsQuarantined",
    "errorSummary",
}


@app.patch("/feed-runs/{run_id}", response_model=FeedRunOut)
async def update_feed_run(run_id: str, patch: dict, session: AsyncSession = Depends(get_session)):
    run = await session.get(FeedRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="feed run not found")
    for k, v in patch.items():
        if k in _FEED_RUN_PATCHABLE:
            setattr(run, k, v)
    await session.commit()
    await session.refresh(run)
    return run
