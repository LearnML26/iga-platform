"""
Rules Engine Service — event-driven + scheduled rule execution
(REQ-COR-RULES-001..003, 006, 007).

No spec document exists in the repo — PHASES.md 4.1's one-line summary is
the only source; interpretations are flagged here, same discipline as every
prior service:

- Consumes the `identity-changes` Event Hub on consumer group `rules-engine`
  (both existed in messaging.bicep since Phase 1, unused until now), with a
  blob checkpoint store (new `eventhub-checkpoints` container on the lake
  account). On a partition with NO checkpoint yet, consumption starts at
  "@latest" — replaying up to 7 days of history on every fresh environment
  would fire rules against long-settled state; a deliberate choice, noted.
- RuleDefinition (sqldb-rules): triggerEventTypes + optional
  changedFieldsFilter (matched against IdentityAttributeChanged's
  _changedFields — the exact field identity-service already publishes),
  runOnSweep, actionType + actionConfig. The ONLY implemented actionType is
  'rbac-reconcile' ("attribute-change triggers re-running RBAC membership
  rules" is the summary's literal ask): reconcile the configured roleIds, or
  every active role with an enabled membership rule when unset. Unknown
  actionTypes are rejected at create/update (422), not silently stored.
- RuleExecutionLog: EVERY evaluation is logged (REQ-COR-RULES-007),
  including non-matches with the reason — the trail shows a rule was
  considered, not just that it fired. Append-only, no retention policy yet
  (dev scale; follow-up noted in PHASES.md).
- Scheduled sweep loop (REQ-COR-RULES-006 interpretation): every
  RULES_SWEEP_INTERVAL_MINUTES (default 60), every enabled rule with
  runOnSweep=true executes — the safety net for missed events. Reconcile is
  idempotent, so over-firing is safe.
- POST /rules/{id}/run: manual execution with the same logged code path —
  the hook 4.2's dry-run/simulation work will extend.
- Auth: same posture as every service (1R.3) — new rules.read/rules.write
  app roles ([HUMAN] gate printed by deploy.sh). Outbound calls to
  rbac-service use this service's own workload identity (needs rbac.read +
  rbac.write — reconcile is a write).
- replicas: 1 in k8s (quota-constrained cluster; and >1 replica would run
  duplicate sweep loops — harmless but wasteful. The EH client load-balances
  partitions across instances if ever scaled).
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

import httpx
from azure.eventhub.aio import EventHubConsumerClient
from azure.eventhub.extensions.checkpointstoreblobaio import BlobCheckpointStore
from azure.identity.aio import DefaultAzureCredential
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_role
from .db import SessionLocal, engine, get_session
from .engine import KNOWN_ACTION_TYPES, execute_rule
from .models import RuleDefinition, RuleExecutionLog

logging.basicConfig(level=logging.INFO)
logging.getLogger("azure").setLevel(logging.WARNING)
log = logging.getLogger("rules-engine-service")

EVENTHUB_NAMESPACE = os.environ.get("EVENTHUB_NAMESPACE", "")  # evh-iga-dev.servicebus.windows.net
EVENTHUB_NAME = "identity-changes"
CONSUMER_GROUP = "rules-engine"
CHECKPOINT_ACCOUNT = os.environ.get("LAKE_STORAGE_ACCOUNT", "")
CHECKPOINT_CONTAINER = "eventhub-checkpoints"
RBAC_SERVICE_URL = os.environ.get("RBAC_SERVICE_URL", "http://rbac-service")
API_AUDIENCE = os.environ.get("API_AUDIENCE", "")
SWEEP_INTERVAL_MINUTES = int(os.environ.get("RULES_SWEEP_INTERVAL_MINUTES", "60"))

app = FastAPI(title="IGA Rules Engine Service", version="1.0.0")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class RuleIn(BaseModel):
    name: str
    description: str | None = None
    enabled: bool = True
    triggerEventTypes: list[str] = Field(default_factory=list)
    changedFieldsFilter: list[str] = Field(default_factory=list)
    runOnSweep: bool = False
    actionType: str
    actionConfig: dict[str, Any] = Field(default_factory=dict)


class RuleOut(RuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    createdDate: datetime
    lastModifiedDate: datetime


class ExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    ruleId: str
    ruleName: str
    triggerSource: str
    eventId: str | None
    eventType: str | None
    identityId: str | None
    matched: bool
    outcome: str
    error: str | None
    executedAt: datetime


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    app.state.ready = False
    app.state.credential = DefaultAzureCredential()
    app.state.consumer_task = asyncio.create_task(consumer_loop())
    app.state.sweep_task = asyncio.create_task(sweep_loop())
    app.state.ready = True
    log.info("Rules Engine started; eh=%s cg=%s sweep=%dm",
             EVENTHUB_NAMESPACE, CONSUMER_GROUP, SWEEP_INTERVAL_MINUTES)


@app.on_event("shutdown")
async def shutdown() -> None:
    app.state.consumer_task.cancel()
    app.state.sweep_task.cancel()
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
# Outbound rbac-service client
# ---------------------------------------------------------------------------
async def _rbac_client() -> httpx.AsyncClient:
    if not API_AUDIENCE:
        raise RuntimeError("API_AUDIENCE not configured")
    token = await app.state.credential.get_token(f"{API_AUDIENCE}/.default")
    return httpx.AsyncClient(
        base_url=RBAC_SERVICE_URL, timeout=60.0,
        headers={"Authorization": f"Bearer {token.token}"},
    )


async def _enabled_rules(*, sweep_only: bool = False) -> list[RuleDefinition]:
    async with SessionLocal() as session:
        stmt = select(RuleDefinition).where(RuleDefinition.enabled.is_(True))
        if sweep_only:
            stmt = stmt.where(RuleDefinition.runOnSweep.is_(True))
        return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Event Hub consumer (REQ-COR-RULES-001/002)
# ---------------------------------------------------------------------------
async def _handle_event(raw: dict[str, Any]) -> None:
    rules = await _enabled_rules()
    if not rules:
        return
    async with await _rbac_client() as rbac_http:
        for rule in rules:
            await execute_rule(rule, rbac_http, "event", event=raw)


async def consumer_loop() -> None:
    checkpoint_store = BlobCheckpointStore(
        blob_account_url=f"https://{CHECKPOINT_ACCOUNT}.blob.core.windows.net",
        container_name=CHECKPOINT_CONTAINER,
        credential=app.state.credential,
    )
    client = EventHubConsumerClient(
        fully_qualified_namespace=EVENTHUB_NAMESPACE,
        eventhub_name=EVENTHUB_NAME,
        consumer_group=CONSUMER_GROUP,
        credential=app.state.credential,
        checkpoint_store=checkpoint_store,
    )

    async def on_event(partition_context, event) -> None:
        if event is None:
            return
        try:
            raw = json.loads(event.body_as_str())
        except (ValueError, TypeError):
            log.error("malformed identity-changes event on partition %s; skipping",
                      partition_context.partition_id)
        else:
            try:
                await _handle_event(raw)
            except Exception:
                # Rule failures are already logged per-rule by engine.py;
                # anything reaching here is unexpected. Checkpoint anyway:
                # rules are convergent (reconcile) and the sweep loop is the
                # designed catch-up path — wedging the partition on one bad
                # event would be worse.
                log.exception("unhandled error processing event %s", raw.get("eventId"))
        await partition_context.update_checkpoint(event)

    while True:
        try:
            async with client:
                await client.receive(on_event=on_event, starting_position="@latest")
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("event consumer error; restarting in 10s")
            await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Scheduled sweep loop (REQ-COR-RULES-006)
# ---------------------------------------------------------------------------
async def sweep_loop() -> None:
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_MINUTES * 60)
            rules = await _enabled_rules(sweep_only=True)
            if rules:
                log.info("sweep: running %d rule(s)", len(rules))
                async with await _rbac_client() as rbac_http:
                    for rule in rules:
                        await execute_rule(rule, rbac_http, "sweep")
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("sweep loop error; continuing next interval")


# ---------------------------------------------------------------------------
# Rule CRUD (REQ-COR-RULES-003)
# ---------------------------------------------------------------------------
def _validate_action(action_type: str) -> None:
    if action_type not in KNOWN_ACTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown actionType '{action_type}' — implemented: {sorted(KNOWN_ACTION_TYPES)}",
        )


@app.post("/rules", response_model=RuleOut, status_code=201, dependencies=[require_role("rules.write")])
async def create_rule(body: RuleIn, session: AsyncSession = Depends(get_session)):
    _validate_action(body.actionType)
    rule = RuleDefinition(**body.model_dump())
    session.add(rule)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"rule '{body.name}' already exists")
    await session.refresh(rule)
    return rule


@app.get("/rules", response_model=list[RuleOut], dependencies=[require_role("rules.read")])
async def list_rules(
    enabled: bool | None = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(RuleDefinition)
    if enabled is not None:
        stmt = stmt.where(RuleDefinition.enabled.is_(enabled))
    result = await session.execute(stmt.limit(limit))
    return result.scalars().all()


@app.get("/rules/{rule_id}", response_model=RuleOut, dependencies=[require_role("rules.read")])
async def get_rule(rule_id: str, session: AsyncSession = Depends(get_session)):
    rule = await session.get(RuleDefinition, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return rule


@app.patch("/rules/{rule_id}", response_model=RuleOut, dependencies=[require_role("rules.write")])
async def update_rule(rule_id: str, patch: dict, session: AsyncSession = Depends(get_session)):
    rule = await session.get(RuleDefinition, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    if "actionType" in patch:
        _validate_action(patch["actionType"])
    immutable = {"id", "createdDate"}
    for k, v in patch.items():
        if k not in immutable and hasattr(rule, k):
            setattr(rule, k, v)
    await session.commit()
    await session.refresh(rule)
    return rule


@app.delete("/rules/{rule_id}", status_code=204, dependencies=[require_role("rules.write")])
async def delete_rule(rule_id: str, session: AsyncSession = Depends(get_session)):
    rule = await session.get(RuleDefinition, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    await session.delete(rule)
    await session.commit()


@app.post("/rules/{rule_id}/run", dependencies=[require_role("rules.write")])
async def run_rule(rule_id: str, session: AsyncSession = Depends(get_session)):
    """Manual execution through the same logged path as events/sweeps —
    also the hook 4.2's dry-run work extends."""
    rule = await session.get(RuleDefinition, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    async with await _rbac_client() as rbac_http:
        await execute_rule(rule, rbac_http, "manual")
    stmt = (
        select(RuleExecutionLog)
        .where(RuleExecutionLog.ruleId == rule_id)
        .order_by(RuleExecutionLog.executedAt.desc())
        .limit(1)
    )
    latest = (await session.execute(stmt)).scalars().first()
    return {"ruleId": rule_id, "executed": True,
            "outcome": latest.outcome if latest else None,
            "error": latest.error if latest else None}


# ---------------------------------------------------------------------------
# Execution log (REQ-COR-RULES-007)
# ---------------------------------------------------------------------------
@app.get("/rule-executions", response_model=list[ExecutionOut], dependencies=[require_role("rules.read")])
async def list_executions(
    ruleId: str | None = None,
    identityId: str | None = None,
    triggerSource: str | None = None,
    matched: bool | None = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(RuleExecutionLog)
    if ruleId:
        stmt = stmt.where(RuleExecutionLog.ruleId == ruleId)
    if identityId:
        stmt = stmt.where(RuleExecutionLog.identityId == identityId)
    if triggerSource:
        stmt = stmt.where(RuleExecutionLog.triggerSource == triggerSource)
    if matched is not None:
        stmt = stmt.where(RuleExecutionLog.matched.is_(matched))
    stmt = stmt.order_by(RuleExecutionLog.executedAt.desc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()
