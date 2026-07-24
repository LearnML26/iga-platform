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
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from azure.cosmos import exceptions as cosmos_exceptions
from azure.cosmos.aio import CosmosClient
from azure.eventhub import EventData
from azure.eventhub.aio import EventHubProducerClient
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

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
    # extra="allow": PATCH (an untyped dict merge in update_identity) already
    # persists any field a caller sends, known or not — create must match or
    # a mapped-but-unrecognized attribute (e.g. a source connector's own key
    # column) gets silently dropped on create yet "changes" on every
    # subsequent PATCH forever, since it never existed to compare against.
    # Found via the 2.3 live smoke test: this exact asymmetry inflated
    # recordsUpdated by 1 every run for a mapping targeting `employeeId`.
    model_config = ConfigDict(extra="allow")

    correlationKey: str
    identityType: IdentityType = IdentityType.employee
    displayName: str
    givenName: str | None = None
    familyName: str | None = None
    status: IdentityStatus = IdentityStatus.active
    sourceSystemId: str | None = None
    managerIdentityId: str | None = None
    department: str | None = None
    jobTitle: str | None = None
    location: str | None = None
    costCenter: str | None = None
    startDate: str | None = None
    terminationDate: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Identity(IdentityIn):
    id: str
    tenantId: str
    identityId: str
    createdDate: str
    lastModifiedDate: str
    # Server-enforced Entra binding (approver-binding task, post-3.6): set
    # ONLY via POST /identities/{id}/claim — deliberately absent from
    # IdentityIn, stripped at create, and immutable in PATCH, because with
    # extra="allow" it would otherwise be spoofable by any identities.write
    # holder, defeating first-claim-wins. Cosmos needs no migration for a
    # new optional field; documented here instead.
    entraObjectId: str | None = None


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
        "occurredAt": datetime.now(UTC).isoformat(),
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
        "timestamp": datetime.now(UTC).isoformat(),
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

    now = datetime.now(UTC).isoformat()
    identity_id = str(uuid.uuid4())
    doc = {
        **body.model_dump(),
        "id": identity_id,
        "identityId": identity_id,
        "tenantId": TENANT_ID,
        "createdDate": now,
        "lastModifiedDate": now,
    }
    # extra="allow" would otherwise let a caller pre-set the Entra binding,
    # bypassing /claim's first-claim-wins — server-owned field, stripped.
    doc.pop("entraObjectId", None)
    await app.state.identities.create_item(doc)
    await write_history(identity_id, "IdentityCreated", None, doc, actor="api")
    await publish_event("IdentityCreated", doc)
    return doc


@app.get(
    "/identities/by-correlation-key/{correlation_key}",
    response_model=Identity,
    dependencies=[require_role("identities.read")],
)
async def get_identity_by_correlation_key(correlation_key: str):
    """Dedicated correlation-key lookup (REQ-COR-ID-002), used by source
    connectors (2.3) to resolve a feed row to an existing identity without
    a full search query. Registered ahead of /{identity_id} so the literal
    'by-correlation-key' segment isn't swallowed as a path param."""
    existing = [
        item async for item in app.state.identities.query_items(
            query="SELECT * FROM c WHERE c.correlationKey = @k AND c.tenantId = @t",
            parameters=[{"name": "@k", "value": correlation_key},
                        {"name": "@t", "value": TENANT_ID}],
        )
    ]
    if not existing:
        raise HTTPException(status_code=404, detail=f"no identity correlated to key '{correlation_key}'")
    return existing[0]


@app.get(
    "/identities/by-entra-object-id/{oid}",
    response_model=Identity,
    dependencies=[require_role("identities.read")],
)
async def get_identity_by_entra_object_id(oid: str):
    """Resolve an Entra principal (token `oid` claim) to its claimed identity
    record — the server-side link consumed by access-request-service's
    approver enforcement. Same shape as by-correlation-key above (2.3)."""
    existing = [
        item async for item in app.state.identities.query_items(
            query="SELECT * FROM c WHERE c.entraObjectId = @o AND c.tenantId = @t",
            parameters=[{"name": "@o", "value": oid},
                        {"name": "@t", "value": TENANT_ID}],
        )
    ]
    if not existing:
        raise HTTPException(status_code=404, detail=f"no identity claimed by Entra object id '{oid}'")
    if len(existing) > 1:
        # Shouldn't happen (claim enforces one identity per oid), but if it
        # ever does, fail loudly rather than silently picking one — an
        # ambiguous binding must not authorize anything.
        raise HTTPException(status_code=409, detail=f"multiple identities claimed by '{oid}' — data integrity issue")
    return existing[0]


@app.post("/identities/{identity_id}/claim", response_model=Identity)
async def claim_identity(identity_id: str, claims: dict = require_role("identities.read")):
    """Bind the CALLER's Entra object id (`oid` claim, taken from the
    validated token — never from the request body) to this identity record.
    First claim wins: succeeds only while entraObjectId is null; re-claiming
    with the same oid is an idempotent 200; a different oid gets 409. The
    caller must also not already hold a claim on another identity (the
    binding is 1:1 both ways — by-entra-object-id lookups must be
    unambiguous).

    Gated on identities.read, not identities.write: this is not an
    arbitrary write — the server writes exactly one server-derived value
    into a null field — and every portal persona holds identities.read.
    App-only (service) tokens are accepted too: their oid is the service
    principal's object id, verify.sh exercises the flow through one, and
    allowing them weakens nothing (app-only requests.write holders could
    previously decide steps as ANYONE, which this task removes).

    Known residual gap, documented not hidden: any authenticated caller can
    claim any UNCLAIMED identity — nothing verifies the human behind the
    token corresponds to the HR record being claimed (identities carry no
    UPN/email to match against). First-claim-wins bounds the damage;
    attribute-matched auto-claim is the v-next once feeds supply a UPN."""
    oid = claims.get("oid")
    if not oid:
        raise HTTPException(status_code=403, detail="token carries no oid claim")

    already = [
        item async for item in app.state.identities.query_items(
            query="SELECT c.id FROM c WHERE c.entraObjectId = @o AND c.tenantId = @t",
            parameters=[{"name": "@o", "value": oid},
                        {"name": "@t", "value": TENANT_ID}],
        )
    ]
    try:
        before = await app.state.identities.read_item(item=identity_id, partition_key=TENANT_ID)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        raise HTTPException(status_code=404, detail="identity not found")

    if before.get("entraObjectId") == oid:
        return before  # idempotent re-claim
    if before.get("entraObjectId"):
        raise HTTPException(status_code=409, detail="identity already claimed by a different principal")
    if already:
        raise HTTPException(
            status_code=409,
            detail=f"caller already claimed identity '{already[0]['id']}' — one identity per principal",
        )

    after = {**before, "entraObjectId": oid,
             "lastModifiedDate": datetime.now(UTC).isoformat()}
    await app.state.identities.replace_item(item=identity_id, body=after)
    # Security-relevant binding: audited (REQ-COR-ID-004) with the oid as actor.
    await write_history(identity_id, "IdentityClaimed", before, after, actor=oid)
    await publish_event("IdentityClaimed", after)
    return after


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

    # entraObjectId: server-owned via /claim only (see Identity model note).
    immutable = {"id", "identityId", "tenantId", "createdDate", "entraObjectId"}
    changed_fields = {k: v for k, v in patch.items() if k not in immutable and before.get(k) != v}
    if not changed_fields:
        return before

    after = {**before, **changed_fields,
             "lastModifiedDate": datetime.now(UTC).isoformat()}
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
    department: str | None = None,
    status: IdentityStatus | None = None,
    manager: str | None = Query(None, alias="managerIdentityId"),
    q: str | None = Query(None, description="displayName contains"),
    terminationDateBefore: str | None = Query(
        None,
        description="ISO date (YYYY-MM-DD); matches identities whose "
        "terminationDate is set and <= this value. Dates are compared as "
        "strings, so a timestamped terminationDate sorts after the bare "
        "date for the same day — mappings should emit bare dates. Used by "
        "the lifecycle sweep (REQ-COR-SRC-008) to find due terminations.",
    ),
    limit: int = Query(50, le=200),
):
    """Faceted search (REQ-COR-ID-009)."""
    clauses, params = ["c.tenantId = @t"], [{"name": "@t", "value": TENANT_ID}]
    if department:
        clauses.append("c.department = @d")
        params.append({"name": "@d", "value": department})
    if status:
        clauses.append("c.status = @s")
        params.append({"name": "@s", "value": status.value})
    if terminationDateBefore:
        clauses.append(
            "IS_DEFINED(c.terminationDate) AND NOT IS_NULL(c.terminationDate) "
            "AND c.terminationDate != '' AND c.terminationDate <= @tdb"
        )
        params.append({"name": "@tdb", "value": terminationDateBefore})
    if manager:
        clauses.append("c.managerIdentityId = @m")
        params.append({"name": "@m", "value": manager})
    if q:
        clauses.append("CONTAINS(LOWER(c.displayName), LOWER(@q))")
        params.append({"name": "@q", "value": q})
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
