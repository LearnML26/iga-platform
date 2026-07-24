"""
Access Request Service — self-service request/approval/provisioning
pipeline (REQ-COR-REQ-001..003, 006, 007, 009).

No spec document exists in this repo (IGA_Platform_Requirements_
Specification.docx isn't present — checked, same gap 3.1 hit). PHASES.md
3.2's one-line summary ("Request/LineItem/ApprovalStep models; default
chain manager -> owner (manager resolved from identity-service);
notifications via notification queue; approval -> provisioning task") is
the only source. Every shape decision below beyond that literal text is an
interpretation, flagged here and in models.py rather than presented as
spec fact — same discipline as 3.1.

- Request: what one requester is asking for (self-service only in this
  pass — no delegated/on-behalf-of requesting; there's no per-user auth
  yet to attribute a delegated request to a real actor anyway).
- LineItem: one requested entitlement per Request. Shape mirrors
  rbac-service's RoleEntitlement (targetSystemInstanceId + connectorType +
  entitlementRef) rather than referencing a RoleEntitlement by id — same
  "target-system-instance registry doubles as the requestable-item
  registry" precedent as 2.3/3.1. This means an approved request does NOT
  create a rbac-service RoleAssignment, even though
  RoleAssignment.assignmentType already reserves a 'request' value for
  this — that integration is a real, documented gap for a v-next pass, not
  silently assumed. Deciding it now would require settling whether
  requests target raw entitlements or whole Roles, which nothing in the
  3.2 summary specifies.
- ApprovalStep: built once at request creation. manager is resolved from
  the requester's own identity-service record (managerIdentityId); owner
  is resolved from the line item's target system instance
  (source-system-service's new ownerIdentityId field, added in this pass —
  no prior field existed anywhere for "who owns this target system").
  Either step is skipped immediately if unresolvable (never blocks) — see
  models.py docstring for why "skip" rather than some fallback approver.
  If BOTH steps are skipped, the line item auto-approves with no human
  gate at all (documented, not hidden).
- Dispatch on final approval: same best-effort posture as rbac-service's
  reconcile/assignment dispatch (logged + counted on failure, not
  retried) — does NOT reuse 2.3's pendingProvisioningDispatch
  persistence-and-retry mechanism. Same accepted gap, same reasoning.
- Notifications: publishes ApprovalRequested (to the current step's
  approver) and RequestDecided (to the requester) onto the
  'notification-tasks' Service Bus queue, matching notification-service's
  already-anticipated extension point (worker.py's own comment named both
  event types and this service by name before this pass existed). Neither
  identity-service nor notification-service has any per-identity email
  address concept (checked) — notification-service's sender config is a
  single static ops-distro recipient list, so these notifications do NOT
  reach the actual approver's/requester's inbox; they land in that same
  static inbox with the relevant identityId in the body. Real per-person
  delivery needs an identity -> email mapping that doesn't exist yet; a
  documented gap, not a pretended capability.
- Auth: same posture as identity-service/provisioning-service/rbac-service
  (1R.3) — every endpoint but health probes requires a validated
  iga-platform-api token with requests.read or requests.write. Deciding an
  approval step is gated on requests.write only (any caller holding that
  role can decide any step) — there is no per-user token/identity
  verification anywhere in this codebase yet (no SPA/MSAL flow has landed;
  that's 3.4, still ahead) to cryptographically confirm the caller IS the
  resolved approverIdentityId. This is the same class of gap as
  PlatformRole's unenforced binding in rbac-service: flagged explicitly,
  not silently assumed safe. requests.read/requests.write are new app
  roles — a [HUMAN] gate, printed by deploy.sh, same pattern as 3.1's.
  Calling OUT to identity-service (identities.read) and provisioning-service
  (provisioning.write) uses this service's own workload identity, same
  pattern as rbac-service. Calling source-system-service needs no token —
  that service has no auth wired at all (checked; a pre-existing gap that
  predates 1R.3, not introduced here).
"""
import json
import logging
import os
from datetime import UTC, datetime

import httpx
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_role
from .db import engine, get_session
from .models import ApprovalStep, LineItem, Request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("access-request-service")

IDENTITY_SERVICE_URL = os.environ.get("IDENTITY_SERVICE_URL", "http://identity-service")
PROVISIONING_SERVICE_URL = os.environ.get("PROVISIONING_SERVICE_URL", "http://provisioning-service")
SOURCE_SYSTEM_SERVICE_URL = os.environ.get("SOURCE_SYSTEM_SERVICE_URL", "http://source-system-service")
API_AUDIENCE = os.environ.get("API_AUDIENCE", "")
SB_NAMESPACE = os.environ.get("SERVICEBUS_NAMESPACE", "")  # sb-iga-dev.servicebus.windows.net
NOTIFY_QUEUE = "notification-tasks"

app = FastAPI(title="IGA Access Request Service", version="1.0.0")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class LineItemIn(BaseModel):
    targetSystemInstanceId: str
    connectorType: str
    entitlementRef: str
    justification: str | None = None


class ApprovalStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    lineItemId: str
    sequenceOrder: int
    stepType: str
    approverIdentityId: str | None
    status: str
    decidedByIdentityId: str | None
    comment: str | None
    createdDate: datetime
    decidedDate: datetime | None


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    requestId: str
    targetSystemInstanceId: str
    connectorType: str
    entitlementRef: str
    justification: str | None
    status: str
    createdDate: datetime
    lastModifiedDate: datetime
    approvalSteps: list[ApprovalStepOut] = Field(default_factory=list)


class RequestIn(BaseModel):
    requesterIdentityId: str
    lineItems: list[LineItemIn]


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    requesterIdentityId: str
    status: str
    createdDate: datetime
    lastModifiedDate: datetime
    lineItems: list[LineItemOut] = Field(default_factory=list)


class DecisionIn(BaseModel):
    decision: str  # approve | reject
    # decidedByIdentityId was removed (approver-binding task): the deciding
    # identity is now SERVER-resolved from the caller's token oid via
    # identity-service's claim binding — a client-supplied "who I am" was
    # trusted blindly and is never accepted for anything security-relevant
    # again. Clients still sending the field are ignored (Pydantic default
    # drops unknown keys), so the change is wire-compatible.
    comment: str | None = None


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    app.state.ready = True
    app.state.credential = DefaultAzureCredential()
    app.state.sb = ServiceBusClient(fully_qualified_namespace=SB_NAMESPACE, credential=app.state.credential)
    log.info("Access Request Service started; sb=%s", SB_NAMESPACE)


@app.on_event("shutdown")
async def shutdown() -> None:
    await engine.dispose()
    await app.state.sb.close()
    await app.state.credential.close()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not getattr(app.state, "ready", False):
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


# ---------------------------------------------------------------------------
# Outbound calls
# ---------------------------------------------------------------------------
async def _outbound_token() -> str:
    if not API_AUDIENCE:
        raise HTTPException(status_code=500, detail="API_AUDIENCE not configured")
    token = await app.state.credential.get_token(f"{API_AUDIENCE}/.default")
    return token.token


async def _identity_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=IDENTITY_SERVICE_URL, timeout=30.0,
        headers={"Authorization": f"Bearer {await _outbound_token()}"},
    )


async def _provisioning_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=PROVISIONING_SERVICE_URL, timeout=30.0,
        headers={"Authorization": f"Bearer {await _outbound_token()}"},
    )


def _source_system_client() -> httpx.AsyncClient:
    # No bearer token: source-system-service has no auth wired (pre-existing
    # gap, predates 1R.3 — checked, not introduced here).
    return httpx.AsyncClient(base_url=SOURCE_SYSTEM_SERVICE_URL, timeout=30.0)


async def _notify(event: dict) -> None:
    """Best-effort publish onto notification-tasks — a failure here must
    never block/roll back the request-state change it's reporting on, so
    it's logged rather than raised."""
    event = {**event, "occurredAt": _now().isoformat()}
    try:
        async with app.state.sb.get_queue_sender(NOTIFY_QUEUE) as sender:
            await sender.send_messages(ServiceBusMessage(json.dumps(event)))
    except Exception:
        log.exception("failed to publish %s notification", event.get("type"))


async def _resolve_manager(identity_http: httpx.AsyncClient, requester_id: str) -> str | None:
    resp = await identity_http.get(f"/identities/{requester_id}")
    if resp.status_code != 200:
        return None
    return resp.json().get("managerIdentityId")


async def _resolve_owner(source_http: httpx.AsyncClient, target_system_instance_id: str) -> str | None:
    resp = await source_http.get(f"/source-systems/{target_system_instance_id}")
    if resp.status_code != 200:
        return None
    return resp.json().get("ownerIdentityId")


# ---------------------------------------------------------------------------
# Requests (REQ-COR-REQ-001..003)
# ---------------------------------------------------------------------------
async def _dispatch_line_item(provisioning_http: httpx.AsyncClient, request: Request, line_item: LineItem) -> bool:
    """POST a single grant task (REQ-COR-REQ-009). Best-effort: see module
    docstring for why this doesn't duplicate 2.3's retry mechanism."""
    task = {
        "sourceType": "access-request",
        "sourceRef": line_item.id,
        "identityId": request.requesterIdentityId,
        "instanceId": line_item.targetSystemInstanceId,
        "connectorType": line_item.connectorType,
        "operationType": "grant",
        "entitlementRef": line_item.entitlementRef,
        "payload": {"entitlementRef": line_item.entitlementRef},
    }
    try:
        resp = await provisioning_http.post("/tasks", json=task)
        if resp.status_code == 202:
            return True
        log.warning("dispatch failed for line item %s: HTTP %s", line_item.id, resp.status_code)
        return False
    except httpx.RequestError as e:
        log.warning("dispatch failed for line item %s: %s", line_item.id, e)
        return False


async def _finalize_approved_line_item(
    session: AsyncSession, provisioning_http: httpx.AsyncClient, request: Request, line_item: LineItem
) -> None:
    line_item.status = "approved"
    await session.flush()
    ok = await _dispatch_line_item(provisioning_http, request, line_item)
    line_item.status = "dispatched" if ok else "dispatch_failed"
    await _notify({
        "type": "RequestDecided",
        "requestId": request.id,
        "lineItemId": line_item.id,
        "requesterIdentityId": request.requesterIdentityId,
        "decision": "approved",
        "targetSystemInstanceId": line_item.targetSystemInstanceId,
        "entitlementRef": line_item.entitlementRef,
    })


async def _maybe_complete_request(session: AsyncSession, request: Request) -> None:
    await session.refresh(request, attribute_names=["lineItems"])
    terminal = {"rejected", "dispatched", "dispatch_failed", "cancelled"}
    if all(li.status in terminal for li in request.lineItems):
        request.status = "completed"


@app.post("/requests", response_model=RequestOut, status_code=201, dependencies=[require_role("requests.write")])
async def create_request(body: RequestIn, session: AsyncSession = Depends(get_session)):
    if not body.lineItems:
        raise HTTPException(status_code=422, detail="at least one line item is required")

    request = Request(requesterIdentityId=body.requesterIdentityId)
    session.add(request)
    await session.flush()

    async with await _identity_client() as identity_http, _source_system_client() as source_http, \
            await _provisioning_client() as provisioning_http:
        manager_id = await _resolve_manager(identity_http, body.requesterIdentityId)

        for li_in in body.lineItems:
            line_item = LineItem(requestId=request.id, **li_in.model_dump())
            session.add(line_item)
            await session.flush()

            owner_id = await _resolve_owner(source_http, li_in.targetSystemInstanceId)
            steps_spec = [("manager", manager_id), ("owner", owner_id)]
            first_pending: ApprovalStep | None = None
            for order, (step_type, approver_id) in enumerate(steps_spec, start=1):
                step = ApprovalStep(
                    lineItemId=line_item.id, sequenceOrder=order, stepType=step_type,
                    approverIdentityId=approver_id,
                    status="pending" if approver_id else "skipped",
                )
                session.add(step)
                if approver_id and first_pending is None:
                    first_pending = step

            if first_pending is None:
                # Nothing to resolve at all — auto-approve, no human gate.
                await session.flush()
                await session.refresh(line_item, attribute_names=["approvalSteps"])
                await _finalize_approved_line_item(session, provisioning_http, request, line_item)
            else:
                await session.flush()
                await _notify({
                    "type": "ApprovalRequested",
                    "requestId": request.id,
                    "lineItemId": line_item.id,
                    "approvalStepId": first_pending.id,
                    "stepType": first_pending.stepType,
                    "approverIdentityId": first_pending.approverIdentityId,
                    "requesterIdentityId": request.requesterIdentityId,
                    "targetSystemInstanceId": line_item.targetSystemInstanceId,
                    "entitlementRef": line_item.entitlementRef,
                })

        await _maybe_complete_request(session, request)

    await session.commit()
    await session.refresh(request, attribute_names=["lineItems"])
    for li in request.lineItems:
        await session.refresh(li, attribute_names=["approvalSteps"])
    return request


@app.get("/requests", response_model=list[RequestOut], dependencies=[require_role("requests.read")])
async def list_requests(
    requesterIdentityId: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Request)
    if requesterIdentityId:
        stmt = stmt.where(Request.requesterIdentityId == requesterIdentityId)
    if status:
        stmt = stmt.where(Request.status == status)
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    requests = result.scalars().unique().all()
    for r in requests:
        await session.refresh(r, attribute_names=["lineItems"])
        for li in r.lineItems:
            await session.refresh(li, attribute_names=["approvalSteps"])
    return requests


@app.get("/requests/{request_id}", response_model=RequestOut, dependencies=[require_role("requests.read")])
async def get_request(request_id: str, session: AsyncSession = Depends(get_session)):
    request = await session.get(Request, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="request not found")
    await session.refresh(request, attribute_names=["lineItems"])
    for li in request.lineItems:
        await session.refresh(li, attribute_names=["approvalSteps"])
    return request


@app.post("/requests/{request_id}/cancel", response_model=RequestOut, dependencies=[require_role("requests.write")])
async def cancel_request(request_id: str, session: AsyncSession = Depends(get_session)):
    """Withdraw a still-open request. Only line items/steps that haven't
    reached a terminal state are cancelled; anything already
    approved/dispatched/rejected is left as-is (cancelling can't undo a
    provisioning dispatch that already happened)."""
    request = await session.get(Request, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="request not found")
    await session.refresh(request, attribute_names=["lineItems"])
    for li in request.lineItems:
        await session.refresh(li, attribute_names=["approvalSteps"])
        if li.status == "pending":
            li.status = "cancelled"
            for step in li.approvalSteps:
                if step.status == "pending":
                    step.status = "cancelled"
    request.status = "cancelled"
    await session.commit()
    await session.refresh(request, attribute_names=["lineItems"])
    return request


# ---------------------------------------------------------------------------
# Approver-side queue (Phase 3.6 — the portal's "my approvals", REQ-UI-032).
# Flat query across all requests: which steps await THIS approver's decision.
# Enriched with line-item/request context so the UI doesn't need N+1 calls.
# ---------------------------------------------------------------------------
@app.get("/approval-steps", dependencies=[require_role("requests.read")])
async def list_approval_steps(
    approverIdentityId: str,
    status: str = "pending",
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(ApprovalStep, LineItem, Request)
        .join(LineItem, ApprovalStep.lineItemId == LineItem.id)
        .join(Request, LineItem.requestId == Request.id)
        .where(ApprovalStep.approverIdentityId == approverIdentityId)
        .where(ApprovalStep.status == status)
        .order_by(ApprovalStep.createdDate.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    out = []
    for step, li, req in result.all():
        # A step is only actionable once every earlier step in its chain is
        # decided — mirror decide_approval_step's ordering rule so the UI
        # can grey out not-yet-actionable rows instead of 409ing on click.
        earlier = await session.execute(
            select(ApprovalStep).where(
                ApprovalStep.lineItemId == li.id,
                ApprovalStep.sequenceOrder < step.sequenceOrder,
                ApprovalStep.status == "pending",
            )
        )
        out.append({
            "id": step.id, "lineItemId": step.lineItemId, "sequenceOrder": step.sequenceOrder,
            "stepType": step.stepType, "approverIdentityId": step.approverIdentityId,
            "status": step.status, "createdDate": step.createdDate,
            "actionable": earlier.scalars().first() is None,
            "requestId": req.id, "requesterIdentityId": req.requesterIdentityId,
            "targetSystemInstanceId": li.targetSystemInstanceId,
            "connectorType": li.connectorType, "entitlementRef": li.entitlementRef,
            "justification": li.justification,
        })
    return out


# ---------------------------------------------------------------------------
# Approval decisions (REQ-COR-REQ-006/007)
# ---------------------------------------------------------------------------
async def _resolve_caller_identity(claims: dict) -> str:
    """Approver binding (closes the gap flagged since 3.2): map the caller's
    token oid to their claimed identity record via identity-service's
    by-entra-object-id lookup. 403 if the caller never claimed an identity.
    Note: require_role() already returns a Depends-wrapped dependency, so
    endpoints capture claims as `claims: dict = require_role(...)` — the
    task spec's `Depends(require_role(...))` form would double-wrap."""
    oid = claims.get("oid")
    if not oid:
        raise HTTPException(status_code=403, detail="token carries no oid claim")
    async with await _identity_client() as identity_http:
        resp = await identity_http.get(f"/identities/by-entra-object-id/{oid}")
    if resp.status_code == 404:
        raise HTTPException(status_code=403, detail="caller has not linked an identity")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"identity lookup failed: HTTP {resp.status_code}")
    return resp.json()["identityId"]


@app.post(
    "/requests/{request_id}/line-items/{line_item_id}/approval-steps/{step_id}/decide",
    response_model=ApprovalStepOut,
)
async def decide_approval_step(
    request_id: str, line_item_id: str, step_id: str, body: DecisionIn,
    session: AsyncSession = Depends(get_session),
    claims: dict = require_role("requests.write"),
):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'")

    # Enforce BEFORE loading state: an unlinked caller learns nothing about
    # the step, and the resolved identity is compared against the step's
    # assigned approver below.
    caller_identity_id = await _resolve_caller_identity(claims)

    request = await session.get(Request, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="request not found")
    line_item = await session.get(LineItem, line_item_id)
    if line_item is None or line_item.requestId != request_id:
        raise HTTPException(status_code=404, detail="line item not found")
    step = await session.get(ApprovalStep, step_id)
    if step is None or step.lineItemId != line_item_id:
        raise HTTPException(status_code=404, detail="approval step not found")
    if step.approverIdentityId != caller_identity_id:
        raise HTTPException(status_code=403, detail="not the assigned approver for this step")
    if step.status != "pending":
        raise HTTPException(status_code=409, detail=f"step is '{step.status}', not decidable")

    await session.refresh(line_item, attribute_names=["approvalSteps"])
    earlier_pending = [
        s for s in line_item.approvalSteps if s.sequenceOrder < step.sequenceOrder and s.status == "pending"
    ]
    if earlier_pending:
        raise HTTPException(status_code=409, detail="an earlier approval step is still pending")

    step.decidedByIdentityId = caller_identity_id  # server-resolved, never client-supplied
    step.comment = body.comment
    step.decidedDate = _now()

    if body.decision == "reject":
        step.status = "rejected"
        line_item.status = "rejected"
        for s in line_item.approvalSteps:
            if s.status == "pending":
                s.status = "cancelled"
        await _notify({
            "type": "RequestDecided",
            "requestId": request_id,
            "lineItemId": line_item_id,
            "requesterIdentityId": request.requesterIdentityId,
            "decision": "rejected",
            "targetSystemInstanceId": line_item.targetSystemInstanceId,
            "entitlementRef": line_item.entitlementRef,
        })
        await _maybe_complete_request(session, request)
        await session.commit()
        await session.refresh(step)
        return step

    step.status = "approved"
    remaining = [
        s for s in line_item.approvalSteps if s.id != step.id and s.status == "pending"
    ]
    if remaining:
        next_step = min(remaining, key=lambda s: s.sequenceOrder)
        await session.commit()
        await _notify({
            "type": "ApprovalRequested",
            "requestId": request_id,
            "lineItemId": line_item_id,
            "approvalStepId": next_step.id,
            "stepType": next_step.stepType,
            "approverIdentityId": next_step.approverIdentityId,
            "requesterIdentityId": request.requesterIdentityId,
            "targetSystemInstanceId": line_item.targetSystemInstanceId,
            "entitlementRef": line_item.entitlementRef,
        })
        await session.refresh(step)
        return step

    # Last step approved — dispatch and finalize.
    async with await _provisioning_client() as provisioning_http:
        await _finalize_approved_line_item(session, provisioning_http, request, line_item)
    await _maybe_complete_request(session, request)
    await session.commit()
    await session.refresh(step)
    return step
