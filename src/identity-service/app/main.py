"""
Identity Service — owns the identity profile object model.
Implements: REQ-COR-ID-001..009 (subset for v1 scaffold)

- CRUD + search over identity profiles (Cosmos DB 'identities' container)
- Append-only change history (REQ-COR-ID-004) into 'identity-history'
- Correlation by correlationKey (REQ-COR-ID-002, deterministic match)
- Domain events published to Event Hubs 'identity-changes' (REQ-COR-SRC-006)
- /healthz and /readyz probes (REQ-INF-035)

Auth to Azure uses DefaultAzureCredential → workload identity in AKS
(REQ-INF-031/062). No connection strings or keys anywhere.
"""
import os
import uuid
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from azure.identity.aio import DefaultAzureCredential
from azure.cosmos.aio import CosmosClient
from azure.cosmos import exceptions as cosmos_exceptions
from azure.eventhub.aio import EventHubProducerClient
from azure.eventhub import EventData

from .auth import require_role

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("identity-service")

COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT", "")
EVENTHUB_NAMESPACE = os.environ.get("EVENTHUB_NAMESPACE", "")  # e.g. evh-iga-dev.servicebus.windows.net
EVENTHUB_NAME = os.environ.get("EVENTHUB_NAME", "identity-changes")
DATABASE = "iga"
TENANT_ID = os.environ.get("PLATFORM_TENANT_ID", "default")

app = FastAPI(title="IGA Identity Service", version="1.0.0")

# ---------------------------------------------------------------------------
# Models (REQ 5.1.1 data model)
# ---------------------------------------------------------------------------
class IdentityStatus(str, Enum):
    active = "active"
    inactive = "inactive"
    pending_start = "pending-start"
    terminated = "terminated"
    leave_of_absence = "leave-of-absence"


class IdentityType(str, Enum):
    employee = "employee"
    contractor = "contractor"
    service_account = "service-account"
    other = "non-employee-other"


class IdentityIn(BaseModel):
    correlationKey: str
    identityType: IdentityType = IdentityType.employee
    displayName: str
    givenName: Optional[str] = None
    familyName: Optional[str] = None
    status: IdentityStatus = IdentityStatus.active
    sourceSystemId: Optional[str] = None
    managerIdentityId: Optional[str] = None
    department: Optional[str] = None
    jobTitle: Optional[str] = None
    location: Optional[str] = None
    costCenter: Optional[str] = None
    startDate: Optional[str] = None
    terminationDate: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Identity(IdentityIn):
    id: str
    tenantId: str
    identityId: str
    createdDate: str
    lastModifiedDate: str


# ---------------------------------------------------------------------------
# Azure clients (lazy singletons via app state)
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    app.state.ready = False
    app.state.credential = DefaultAzureCredential()
    app.state.cosmos = CosmosClient(COSMOS_ENDPOINT, credential=app.state.credential)
    db = app.state.cosmos.get_database_client(DATABASE)
    app.state.identities = db.get_container_client("identities")
    app.state.history = db.get_container_client("identity-history")
    if EVENTHUB_NAMESPACE:
        app.state.producer = EventHubProducerClient(
            fully_qualified_namespace=EVENTHUB_NAMESPACE,
            eventhub_name=EVENTHUB_NAME,
            credential=app.state.credential,
        )
    else:
        app.state.producer = None
    app.state.ready = True
    log.info("Identity Service started; cosmos=%s eventhub=%s", COSMOS_ENDPOINT, EVENTHUB_NAMESPACE)


@app.on_event("shutdown")
async def shutdown() -> None:
    await app.state.cosmos.close()
    if app.state.producer:
        await app.state.producer.close()
    await app.state.credential.close()


async def publish_event(event_type: str, identity: dict) -> None:
    """Emit domain event for Rules Engine + Audit (REQ-COR-SRC-006)."""
    if not app.state.producer:
        log.warning("EventHub not configured; skipping event %s", event_type)
        return
    payload = {
        "eventId": str(uuid.uuid4()),
        "eventType": event_type,
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "identityId": identity["identityId"],
        "tenantId": identity["tenantId"],
        "snapshot": identity,
    }
    async with app.state.producer:
        batch = await app.state.producer.create_batch()
        batch.add(EventData(json.dumps(payload)))
        await app.state.producer.send_batch(batch)


async def write_history(identity_id: str, event_type: str, before: dict | None, after: dict | None, actor: str) -> None:
    """Append-only change log (REQ-COR-ID-004)."""
    await app.state.history.create_item({
        "id": str(uuid.uuid4()),
        "identityId": identity_id,
        "eventType": event_type,
        "actor": actor,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "before": before,
        "after": after,
    })


# ---------------------------------------------------------------------------
# Probes (REQ-INF-035)
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not getattr(app.state, "ready", False):
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.post("/identities", response_model=Identity, status_code=201, dependencies=[require_role("identities.write")])
async def create_identity(body: IdentityIn):
    # Deterministic correlation: reject duplicate correlationKey (REQ-COR-ID-002)
    existing = [
        item async for item in app.state.identities.query_items(
            query="SELECT c.id FROM c WHERE c.correlationKey = @k AND c.tenantId = @t",
            parameters=[{"name": "@k", "value": body.correlationKey},
                        {"name": "@t", "value": TENANT_ID}],
        )
    ]
    if existing:
        raise HTTPException(status_code=409, detail=f"correlationKey '{body.correlationKey}' already correlated")

    now = datetime.now(timezone.utc).isoformat()
    identity_id = str(uuid.uuid4())
    doc = {
        **body.model_dump(),
        "id": identity_id,
        "identityId": identity_id,
        "tenantId": TENANT_ID,
        "createdDate": now,
        "lastModifiedDate": now,
    }
    await app.state.identities.create_item(doc)
    await write_history(identity_id, "IdentityCreated", None, doc, actor="api")
    await publish_event("IdentityCreated", doc)
    return doc


@app.get("/identities/{identity_id}", response_model=Identity, dependencies=[require_role("identities.read")])
async def get_identity(identity_id: str):
    try:
        return await app.state.identities.read_item(item=identity_id, partition_key=TENANT_ID)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        raise HTTPException(status_code=404, detail="identity not found")


@app.patch("/identities/{identity_id}", response_model=Identity, dependencies=[require_role("identities.write")])
async def update_identity(identity_id: str, patch: dict):
    try:
        before = await app.state.identities.read_item(item=identity_id, partition_key=TENANT_ID)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        raise HTTPException(status_code=404, detail="identity not found")

    immutable = {"id", "identityId", "tenantId", "createdDate"}
    changed_fields = {k: v for k, v in patch.items() if k not in immutable and before.get(k) != v}
    if not changed_fields:
        return before

    after = {**before, **changed_fields,
             "lastModifiedDate": datetime.now(timezone.utc).isoformat()}
    await app.state.identities.replace_item(item=identity_id, body=after)
    await write_history(identity_id, "IdentityUpdated", before, after, actor="api")

    # Lifecycle-aware event typing (REQ-COR-ID-005)
    if changed_fields.get("status") == IdentityStatus.terminated:
        await publish_event("IdentityTerminated", after)
    else:
        await publish_event("IdentityAttributeChanged",
                            {**after, "_changedFields": sorted(changed_fields.keys())})
    return after


@app.get("/identities", dependencies=[require_role("identities.read")])
async def search_identities(
    department: Optional[str] = None,
    status: Optional[IdentityStatus] = None,
    manager: Optional[str] = Query(None, alias="managerIdentityId"),
    q: Optional[str] = Query(None, description="displayName contains"),
    limit: int = Query(50, le=200),
):
    """Faceted search (REQ-COR-ID-009)."""
    clauses, params = ["c.tenantId = @t"], [{"name": "@t", "value": TENANT_ID}]
    if department:
        clauses.append("c.department = @d"); params.append({"name": "@d", "value": department})
    if status:
        clauses.append("c.status = @s"); params.append({"name": "@s", "value": status.value})
    if manager:
        clauses.append("c.managerIdentityId = @m"); params.append({"name": "@m", "value": manager})
    if q:
        clauses.append("CONTAINS(LOWER(c.displayName), LOWER(@q))"); params.append({"name": "@q", "value": q})
    query = f"SELECT * FROM c WHERE {' AND '.join(clauses)} OFFSET 0 LIMIT {limit}"
    return [item async for item in app.state.identities.query_items(query=query, parameters=params)]


@app.get("/identities/{identity_id}/history", dependencies=[require_role("identities.read")])
async def get_history(identity_id: str, limit: int = Query(50, le=200)):
    return [
        item async for item in app.state.history.query_items(
            query=f"SELECT * FROM c WHERE c.identityId = @i ORDER BY c.timestamp DESC OFFSET 0 LIMIT {limit}",
            parameters=[{"name": "@i", "value": identity_id}],
            partition_key=identity_id,
        )
    ]
