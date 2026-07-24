"""
RBAC Service — role/entitlement/membership-rule/assignment model and engine
(REQ-COR-RBAC-001..004, 007..009).

- Role: named bundle of entitlements (RoleEntitlement), versioned on change
  (REQ-COR-RBAC-007) via an append-only RoleVersion snapshot table —
  mirrors identity-service's identity-history pattern. Version bumps on
  changes to the role's own fields AND on entitlement add/remove (both
  change what the role actually grants).
- RoleMembershipRule: a birthright/dynamic rule (JSON criteria, ANDed
  equality match against identity-service attributes) attached to a Role.
  POST .../evaluate (REQ-COR-RBAC-008) is a dry run: reports who currently
  matches, changes nothing. POST /roles/{id}/reconcile is the real
  operation — evaluates every enabled rule (ORed across rules), creates
  RoleAssignments for newly-matching identities, revokes rule-sourced
  assignments for identities no longer matched by any enabled rule, and
  for every assignment created/revoked dispatches a grant/revoke
  provisioning task per RoleEntitlement (REQ-COR-RBAC-009). Manual
  assignment/revocation (POST/DELETE .../assignments) dispatches the same
  way; reconcile never touches manually-created assignments.
- PlatformRole: IGA's own admin/operator roles. CRUD only in this pass —
  see the model's docstring in app/models.py for what's not implemented
  and why.

Rule criteria are intentionally simple: an equality match per key against
whatever identity-service's GET /identities returns. Only `department` is
pushed down as a server-side filter (the only attribute-equality filter
search_identities currently supports beyond status/manager/q); every other
criterion key is applied client-side against the fetched records. This
avoids another identity-service schema/query change in this pass, at the
cost of being O(all-active-identities-in-department) rather than a
targeted query — acceptable at dev scale, called out here rather than
silently assumed.

Dispatch here is best-effort: a failed grant/revoke POST is logged and
counted, not retried. The pendingProvisioningDispatch persistence-and-retry
mechanism (2.3's dispatch-retry fix) lives in flatfile-connector-service
and is not duplicated here — a real gap for a v-next pass, noted on the
roadmap rather than silently accepted.

Auth: like identity-service/provisioning-service (1R.3), every endpoint
except health probes requires a validated iga-platform-api bearer token
with role rbac.read or rbac.write. Calling OUT to identity-service
(identities.read) and provisioning-service (provisioning.write) uses this
service's own workload identity, minting a fresh token per outbound call —
same pattern as flatfile-connector-service. The rbac.read/rbac.write app
roles are new on the iga-platform-api registration — a [HUMAN] gate (Graph
app-role definition + assignment need directory perms), printed by
deploy.sh.
"""
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from azure.identity.aio import DefaultAzureCredential
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_role
from .db import engine, get_session
from .models import PlatformRole, Role, RoleAssignment, RoleEntitlement, RoleMembershipRule, RoleVersion

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rbac-service")

IDENTITY_SERVICE_URL = os.environ.get("IDENTITY_SERVICE_URL", "http://identity-service")
PROVISIONING_SERVICE_URL = os.environ.get("PROVISIONING_SERVICE_URL", "http://provisioning-service")
API_AUDIENCE = os.environ.get("API_AUDIENCE", "")

app = FastAPI(title="IGA RBAC Service", version="1.0.0")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class RoleIn(BaseModel):
    name: str
    description: str | None = None
    status: str = "active"


class RoleOut(RoleIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int
    createdDate: datetime
    lastModifiedDate: datetime


class RoleEntitlementIn(BaseModel):
    targetSystemInstanceId: str
    connectorType: str
    entitlementRef: str


class RoleEntitlementOut(RoleEntitlementIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    roleId: str
    createdDate: datetime


class RoleMembershipRuleIn(BaseModel):
    criteria: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RoleMembershipRuleOut(RoleMembershipRuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    roleId: str
    createdDate: datetime
    lastModifiedDate: datetime


class RoleAssignmentIn(BaseModel):
    identityId: str


class RoleAssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    roleId: str
    identityId: str
    assignmentType: str
    status: str
    createdDate: datetime
    revokedDate: datetime | None


class PlatformRoleIn(BaseModel):
    name: str
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)


class PlatformRoleOut(PlatformRoleIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    createdDate: datetime
    lastModifiedDate: datetime


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    app.state.ready = True
    app.state.credential = DefaultAzureCredential()
    log.info("RBAC Service started")


@app.on_event("shutdown")
async def shutdown() -> None:
    await engine.dispose()
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
# Outbound auth (this service calling identity-service / provisioning-service)
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


# ---------------------------------------------------------------------------
# Roles (REQ-COR-RBAC-001) + versioning (REQ-COR-RBAC-007)
# ---------------------------------------------------------------------------
async def _snapshot_role(session: AsyncSession, role: Role, actor: str) -> None:
    """Append a version snapshot at role.version. Caller must have already
    flushed and refreshed `role.entitlements` to reflect the change being
    snapshotted."""
    ents = [
        {
            "targetSystemInstanceId": e.targetSystemInstanceId,
            "connectorType": e.connectorType,
            "entitlementRef": e.entitlementRef,
        }
        for e in role.entitlements
    ]
    snapshot = {
        "name": role.name, "description": role.description, "status": role.status,
        "entitlements": ents,
    }
    session.add(RoleVersion(roleId=role.id, version=role.version, snapshot=snapshot, changedBy=actor))


@app.post("/roles", response_model=RoleOut, status_code=201, dependencies=[require_role("rbac.write")])
async def create_role(body: RoleIn, session: AsyncSession = Depends(get_session)):
    role = Role(**body.model_dump())
    session.add(role)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"role '{body.name}' already exists")
    await session.refresh(role, attribute_names=["entitlements"])
    await _snapshot_role(session, role, actor="api")
    await session.commit()
    await session.refresh(role)
    return role


@app.get("/roles", response_model=list[RoleOut], dependencies=[require_role("rbac.read")])
async def list_roles(
    status: str | None = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Role)
    if status:
        stmt = stmt.where(Role.status == status)
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.get("/roles/{role_id}", response_model=RoleOut, dependencies=[require_role("rbac.read")])
async def get_role(role_id: str, session: AsyncSession = Depends(get_session)):
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    return role


@app.patch("/roles/{role_id}", response_model=RoleOut, dependencies=[require_role("rbac.write")])
async def update_role(role_id: str, patch: dict, session: AsyncSession = Depends(get_session)):
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    immutable = {"id", "createdDate", "version"}
    changed = False
    for k, v in patch.items():
        if k not in immutable and hasattr(role, k) and getattr(role, k) != v:
            setattr(role, k, v)
            changed = True
    if changed:
        role.version += 1
        await session.flush()
        await session.refresh(role, attribute_names=["entitlements"])
        await _snapshot_role(session, role, actor="api")
    await session.commit()
    await session.refresh(role)
    return role


@app.delete("/roles/{role_id}", status_code=204, dependencies=[require_role("rbac.write")])
async def delete_role(role_id: str, session: AsyncSession = Depends(get_session)):
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    await session.delete(role)
    await session.commit()


@app.get("/roles/{role_id}/versions", dependencies=[require_role("rbac.read")])
async def list_role_versions(role_id: str, session: AsyncSession = Depends(get_session)):
    stmt = select(RoleVersion).where(RoleVersion.roleId == role_id).order_by(RoleVersion.version.desc())
    result = await session.execute(stmt)
    return [
        {
            "id": r.id, "roleId": r.roleId, "version": r.version,
            "snapshot": r.snapshot, "changedBy": r.changedBy, "createdDate": r.createdDate,
        }
        for r in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# Role entitlements — part of the Role definition, so changes version it
# ---------------------------------------------------------------------------
@app.post(
    "/roles/{role_id}/entitlements", response_model=RoleEntitlementOut, status_code=201,
    dependencies=[require_role("rbac.write")],
)
async def add_entitlement(role_id: str, body: RoleEntitlementIn, session: AsyncSession = Depends(get_session)):
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    ent = RoleEntitlement(roleId=role_id, **body.model_dump())
    session.add(ent)
    role.version += 1
    await session.flush()
    await session.refresh(role, attribute_names=["entitlements"])
    await _snapshot_role(session, role, actor="api")
    await session.commit()
    await session.refresh(ent)
    return ent


@app.get(
    "/roles/{role_id}/entitlements", response_model=list[RoleEntitlementOut],
    dependencies=[require_role("rbac.read")],
)
async def list_entitlements(role_id: str, session: AsyncSession = Depends(get_session)):
    stmt = select(RoleEntitlement).where(RoleEntitlement.roleId == role_id)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.delete(
    "/roles/{role_id}/entitlements/{entitlement_id}", status_code=204,
    dependencies=[require_role("rbac.write")],
)
async def delete_entitlement(role_id: str, entitlement_id: str, session: AsyncSession = Depends(get_session)):
    ent = await session.get(RoleEntitlement, entitlement_id)
    if ent is None or ent.roleId != role_id:
        raise HTTPException(status_code=404, detail="entitlement not found")
    role = await session.get(Role, role_id)
    await session.delete(ent)
    role.version += 1
    await session.flush()
    await session.refresh(role, attribute_names=["entitlements"])
    await _snapshot_role(session, role, actor="api")
    await session.commit()


# ---------------------------------------------------------------------------
# Membership rules + evaluation (REQ-COR-RBAC-008)
# ---------------------------------------------------------------------------
@app.post(
    "/roles/{role_id}/membership-rules", response_model=RoleMembershipRuleOut, status_code=201,
    dependencies=[require_role("rbac.write")],
)
async def create_membership_rule(role_id: str, body: RoleMembershipRuleIn, session: AsyncSession = Depends(get_session)):
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    rule = RoleMembershipRule(roleId=role_id, **body.model_dump())
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


@app.get(
    "/roles/{role_id}/membership-rules", response_model=list[RoleMembershipRuleOut],
    dependencies=[require_role("rbac.read")],
)
async def list_membership_rules(role_id: str, session: AsyncSession = Depends(get_session)):
    stmt = select(RoleMembershipRule).where(RoleMembershipRule.roleId == role_id)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.delete(
    "/roles/{role_id}/membership-rules/{rule_id}", status_code=204,
    dependencies=[require_role("rbac.write")],
)
async def delete_membership_rule(role_id: str, rule_id: str, session: AsyncSession = Depends(get_session)):
    rule = await session.get(RoleMembershipRule, rule_id)
    if rule is None or rule.roleId != role_id:
        raise HTTPException(status_code=404, detail="membership rule not found")
    await session.delete(rule)
    await session.commit()


async def _matching_identities(identity_http: httpx.AsyncClient, criteria: dict[str, Any]) -> list[dict]:
    """Resolve a rule's criteria to currently-matching active identities.
    `department` is pushed down as identity-service's server-side filter;
    every other criterion key is applied client-side against the returned
    records (see module docstring for why)."""
    params: dict[str, Any] = {"status": "active", "limit": 200}
    if "department" in criteria:
        params["department"] = criteria["department"]
    resp = await identity_http.get("/identities", params=params)
    resp.raise_for_status()
    candidates = resp.json()
    remaining = {k: v for k, v in criteria.items() if k != "department"}
    if not remaining:
        return candidates
    return [c for c in candidates if all(c.get(k) == v for k, v in remaining.items())]


@app.post(
    "/roles/{role_id}/membership-rules/{rule_id}/evaluate",
    dependencies=[require_role("rbac.read")],
)
async def evaluate_membership_rule(role_id: str, rule_id: str, session: AsyncSession = Depends(get_session)):
    """Dry run (REQ-COR-RBAC-008): report who currently matches, change nothing."""
    rule = await session.get(RoleMembershipRule, rule_id)
    if rule is None or rule.roleId != role_id:
        raise HTTPException(status_code=404, detail="membership rule not found")
    async with await _identity_client() as identity_http:
        matched = await _matching_identities(identity_http, rule.criteria)
    return {
        "roleId": role_id, "ruleId": rule_id, "criteria": rule.criteria,
        "matchCount": len(matched),
        "identityIds": [m["identityId"] for m in matched],
    }


# ---------------------------------------------------------------------------
# Role assignments + provisioning dispatch (REQ-COR-RBAC-009)
# ---------------------------------------------------------------------------
def _entitlement_task(ent: RoleEntitlement, identity_id: str, operation: str, source_ref: str) -> dict:
    return {
        "sourceType": "role-assignment",
        "sourceRef": source_ref,
        "identityId": identity_id,
        "instanceId": ent.targetSystemInstanceId,
        "connectorType": ent.connectorType,
        "operationType": operation,
        "entitlementRef": ent.entitlementRef,
        "payload": {"entitlementRef": ent.entitlementRef},
    }


async def _dispatch_for_assignment(
    provisioning_http: httpx.AsyncClient,
    entitlements: list[RoleEntitlement],
    identity_id: str,
    operation: str,
    source_ref: str,
) -> tuple[int, int]:
    """POST a grant/revoke task per entitlement. Best-effort: a dispatch
    failure is logged and counted, not retried — see module docstring for
    why this doesn't duplicate 2.3's pendingProvisioningDispatch retry."""
    succeeded = failed = 0
    for ent in entitlements:
        try:
            resp = await provisioning_http.post(
                "/tasks", json=_entitlement_task(ent, identity_id, operation, source_ref)
            )
            if resp.status_code == 202:
                succeeded += 1
            else:
                failed += 1
                log.warning("dispatch failed for entitlement %s: HTTP %s", ent.id, resp.status_code)
        except httpx.RequestError as e:
            failed += 1
            log.warning("dispatch failed for entitlement %s: %s", ent.id, e)
    return succeeded, failed


@app.post(
    "/roles/{role_id}/assignments", response_model=RoleAssignmentOut, status_code=201,
    dependencies=[require_role("rbac.write")],
)
async def create_assignment(role_id: str, body: RoleAssignmentIn, session: AsyncSession = Depends(get_session)):
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    existing = await session.execute(
        select(RoleAssignment).where(
            RoleAssignment.roleId == role_id,
            RoleAssignment.identityId == body.identityId,
            RoleAssignment.status == "active",
        )
    )
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail="identity already has an active assignment for this role")

    assignment = RoleAssignment(roleId=role_id, identityId=body.identityId, assignmentType="manual", status="active")
    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)

    await session.refresh(role, attribute_names=["entitlements"])
    async with await _provisioning_client() as provisioning_http:
        succeeded, failed = await _dispatch_for_assignment(
            provisioning_http, role.entitlements, body.identityId, "grant", assignment.id,
        )
    log.info("assignment %s: dispatched %d/%d entitlement grants", assignment.id, succeeded, succeeded + failed)
    return assignment


@app.get(
    "/roles/{role_id}/assignments", response_model=list[RoleAssignmentOut],
    dependencies=[require_role("rbac.read")],
)
async def list_assignments(
    role_id: str, status: str | None = None, session: AsyncSession = Depends(get_session)
):
    stmt = select(RoleAssignment).where(RoleAssignment.roleId == role_id)
    if status:
        stmt = stmt.where(RoleAssignment.status == status)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.get("/assignments", dependencies=[require_role("rbac.read")])
async def list_assignments_by_identity(
    identityId: str,
    status: str | None = "active",
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Cross-role assignment view for one identity (Phase 3.6 — the portal's
    "my access", REQ-UI-030). Joined with the role so the UI gets names and
    entitlement summaries without N+1 calls."""
    stmt = (
        select(RoleAssignment, Role)
        .join(Role, RoleAssignment.roleId == Role.id)
        .where(RoleAssignment.identityId == identityId)
    )
    if status:
        stmt = stmt.where(RoleAssignment.status == status)
    stmt = stmt.order_by(RoleAssignment.createdDate.desc()).limit(limit)
    result = await session.execute(stmt)
    out = []
    for a, role in result.all():
        await session.refresh(role, attribute_names=["entitlements"])
        out.append({
            "id": a.id, "roleId": a.roleId, "identityId": a.identityId,
            "assignmentType": a.assignmentType, "status": a.status,
            "createdDate": a.createdDate, "revokedDate": a.revokedDate,
            "roleName": role.name, "roleDescription": role.description,
            "entitlements": [
                {"targetSystemInstanceId": e.targetSystemInstanceId,
                 "connectorType": e.connectorType, "entitlementRef": e.entitlementRef}
                for e in role.entitlements
            ],
        })
    return out


@app.delete(
    "/roles/{role_id}/assignments/{assignment_id}", response_model=RoleAssignmentOut,
    dependencies=[require_role("rbac.write")],
)
async def revoke_assignment(role_id: str, assignment_id: str, session: AsyncSession = Depends(get_session)):
    """Revoke (soft-delete): sets status=revoked and dispatches a revoke
    task per entitlement. Returns 200 with the updated record — not
    204 — so the caller sees revokedDate without a follow-up GET."""
    assignment = await session.get(RoleAssignment, assignment_id)
    if assignment is None or assignment.roleId != role_id:
        raise HTTPException(status_code=404, detail="assignment not found")
    if assignment.status == "revoked":
        return assignment  # idempotent

    assignment.status = "revoked"
    assignment.revokedDate = _now()
    await session.commit()
    await session.refresh(assignment)

    role = await session.get(Role, role_id)
    await session.refresh(role, attribute_names=["entitlements"])
    async with await _provisioning_client() as provisioning_http:
        succeeded, failed = await _dispatch_for_assignment(
            provisioning_http, role.entitlements, assignment.identityId, "revoke", assignment.id,
        )
    log.info("assignment %s revoked: dispatched %d/%d entitlement revokes", assignment.id, succeeded, succeeded + failed)
    return assignment


@app.post("/roles/{role_id}/reconcile", dependencies=[require_role("rbac.write")])
async def reconcile_role(role_id: str, session: AsyncSession = Depends(get_session)):
    """Evaluate every enabled membership rule for this role (ORed across
    rules, ANDed within a rule's own criteria); create RoleAssignments for
    newly-matching identities and revoke rule-sourced assignments for
    identities no longer matched by any enabled rule. Dispatches a
    grant/revoke provisioning task per RoleEntitlement for every
    assignment created/revoked (REQ-COR-RBAC-009). Manually-created
    assignments (assignmentType=manual) are never touched here — only
    rule-sourced ones are added/removed by reconcile."""
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    await session.refresh(role, attribute_names=["entitlements", "membershipRules"])

    enabled_rules = [r for r in role.membershipRules if r.enabled]
    matched_ids: set[str] = set()
    async with await _identity_client() as identity_http:
        for rule in enabled_rules:
            for ident in await _matching_identities(identity_http, rule.criteria):
                matched_ids.add(ident["identityId"])

    existing = (
        await session.execute(
            select(RoleAssignment).where(RoleAssignment.roleId == role_id, RoleAssignment.status == "active")
        )
    ).scalars().all()
    existing_ids = {a.identityId for a in existing}
    to_add = matched_ids - existing_ids
    to_revoke = [a for a in existing if a.assignmentType == "rule" and a.identityId not in matched_ids]

    added = revoked = dispatch_ok = dispatch_failed = 0
    async with await _provisioning_client() as provisioning_http:
        for identity_id in to_add:
            assignment = RoleAssignment(roleId=role_id, identityId=identity_id, assignmentType="rule", status="active")
            session.add(assignment)
            await session.flush()
            s, f = await _dispatch_for_assignment(provisioning_http, role.entitlements, identity_id, "grant", assignment.id)
            dispatch_ok += s
            dispatch_failed += f
            added += 1
        for a in to_revoke:
            a.status = "revoked"
            a.revokedDate = _now()
            s, f = await _dispatch_for_assignment(provisioning_http, role.entitlements, a.identityId, "revoke", a.id)
            dispatch_ok += s
            dispatch_failed += f
            revoked += 1
    await session.commit()

    return {
        "roleId": role_id,
        "rulesEvaluated": len(enabled_rules),
        "matched": len(matched_ids),
        "assignmentsAdded": added,
        "assignmentsRevoked": revoked,
        "dispatchSucceeded": dispatch_ok,
        "dispatchFailed": dispatch_failed,
    }


# ---------------------------------------------------------------------------
# Platform roles — CRUD only, see app/models.py's PlatformRole docstring
# ---------------------------------------------------------------------------
@app.post("/platform-roles", response_model=PlatformRoleOut, status_code=201, dependencies=[require_role("rbac.write")])
async def create_platform_role(body: PlatformRoleIn, session: AsyncSession = Depends(get_session)):
    pr = PlatformRole(**body.model_dump())
    session.add(pr)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"platform role '{body.name}' already exists")
    await session.refresh(pr)
    return pr


@app.get("/platform-roles", response_model=list[PlatformRoleOut], dependencies=[require_role("rbac.read")])
async def list_platform_roles(limit: int = Query(50, le=200), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PlatformRole).limit(limit))
    return result.scalars().all()


@app.get("/platform-roles/{pr_id}", response_model=PlatformRoleOut, dependencies=[require_role("rbac.read")])
async def get_platform_role(pr_id: str, session: AsyncSession = Depends(get_session)):
    pr = await session.get(PlatformRole, pr_id)
    if pr is None:
        raise HTTPException(status_code=404, detail="platform role not found")
    return pr


@app.patch("/platform-roles/{pr_id}", response_model=PlatformRoleOut, dependencies=[require_role("rbac.write")])
async def update_platform_role(pr_id: str, patch: dict, session: AsyncSession = Depends(get_session)):
    pr = await session.get(PlatformRole, pr_id)
    if pr is None:
        raise HTTPException(status_code=404, detail="platform role not found")
    immutable = {"id", "createdDate"}
    for k, v in patch.items():
        if k not in immutable and hasattr(pr, k):
            setattr(pr, k, v)
    await session.commit()
    await session.refresh(pr)
    return pr


@app.delete("/platform-roles/{pr_id}", status_code=204, dependencies=[require_role("rbac.write")])
async def delete_platform_role(pr_id: str, session: AsyncSession = Depends(get_session)):
    pr = await session.get(PlatformRole, pr_id)
    if pr is None:
        raise HTTPException(status_code=404, detail="platform role not found")
    await session.delete(pr)
    await session.commit()
