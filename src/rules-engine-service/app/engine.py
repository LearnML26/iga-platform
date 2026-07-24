"""
Rule evaluation + action execution for the Rules Engine (Phase 4.1).

Split from main.py so the event consumer, the sweep loop, and (in verify /
future dry-run work, 4.2) API-triggered evaluation all share one code path —
"every evaluation logged" (REQ-COR-RULES-007) is enforced here, in exactly
one place, rather than trusted to each caller.

Action semantics (v1, interpretation flagged in main.py):
  rbac-reconcile — POST /roles/{id}/reconcile on rbac-service for the roles
  in actionConfig.roleIds, or for EVERY active role that has at least one
  enabled membership rule when roleIds is empty/absent. O(roles) per firing;
  acceptable at dev scale and noted rather than silently assumed. Reconcile
  itself is idempotent (it converges assignments to the rule-matched set),
  so over-firing is safe, just wasteful.
"""
import logging
from typing import Any

import httpx

from .db import SessionLocal
from .models import RuleDefinition, RuleExecutionLog

log = logging.getLogger("rules-engine.engine")

KNOWN_ACTION_TYPES = {"rbac-reconcile"}


async def _log_execution(
    rule: RuleDefinition, trigger_source: str, matched: bool, outcome: str,
    error: str | None = None, event: dict[str, Any] | None = None,
) -> None:
    try:
        async with SessionLocal() as session:
            session.add(RuleExecutionLog(
                ruleId=rule.id, ruleName=rule.name, triggerSource=trigger_source,
                eventId=(event or {}).get("eventId"),
                eventType=(event or {}).get("eventType"),
                identityId=(event or {}).get("identityId"),
                matched=matched, outcome=outcome, error=error,
            ))
            await session.commit()
    except Exception:
        # The log row must never break event processing, but a silent audit
        # gap is also unacceptable — surface loudly in service logs.
        log.exception("FAILED to write rule execution log for rule %s (%s)", rule.name, trigger_source)


def _event_matches(rule: RuleDefinition, event: dict[str, Any]) -> tuple[bool, str]:
    """Pure match decision + human-readable reason (goes into the log)."""
    etype = event.get("eventType", "")
    if etype not in (rule.triggerEventTypes or []):
        return False, f"eventType '{etype}' not in triggerEventTypes"
    if rule.changedFieldsFilter and etype == "IdentityAttributeChanged":
        changed = set((event.get("snapshot") or {}).get("_changedFields") or [])
        wanted = set(rule.changedFieldsFilter)
        if not (changed & wanted):
            return False, f"changed fields {sorted(changed)} don't intersect filter {sorted(wanted)}"
    return True, "matched"


async def _run_rbac_reconcile(rbac_http: httpx.AsyncClient, rule: RuleDefinition) -> str:
    role_ids: list[str] = list((rule.actionConfig or {}).get("roleIds") or [])
    if not role_ids:
        resp = await rbac_http.get("/roles", params={"status": "active", "limit": 200})
        resp.raise_for_status()
        roles = resp.json()
        # Only roles with at least one enabled membership rule can produce
        # different assignments from reconcile — skip the rest.
        for role in roles:
            rules_resp = await rbac_http.get(f"/roles/{role['id']}/membership-rules")
            rules_resp.raise_for_status()
            if any(r.get("enabled") for r in rules_resp.json()):
                role_ids.append(role["id"])
    reconciled, failed = [], []
    for role_id in role_ids:
        try:
            resp = await rbac_http.post(f"/roles/{role_id}/reconcile")
            resp.raise_for_status()
            body = resp.json()
            reconciled.append(f"{role_id}(+{body.get('assignmentsAdded', 0)}/-{body.get('assignmentsRevoked', 0)})")
        except httpx.HTTPError as e:
            failed.append(f"{role_id}: {e}")
    outcome = f"rbac-reconcile: {len(reconciled)} role(s) reconciled"
    if reconciled:
        outcome += f" [{', '.join(reconciled)}]"
    if failed:
        outcome += f"; {len(failed)} failed [{'; '.join(failed)}]"
    return outcome


async def execute_rule(
    rule: RuleDefinition, rbac_http: httpx.AsyncClient, trigger_source: str,
    event: dict[str, Any] | None = None,
) -> None:
    """Evaluate one rule against one trigger, run its action if matched, and
    ALWAYS log — match or not, success or not."""
    if trigger_source == "event":
        matched, reason = _event_matches(rule, event or {})
        if not matched:
            await _log_execution(rule, trigger_source, False, reason, event=event)
            return
    try:
        if rule.actionType == "rbac-reconcile":
            outcome = await _run_rbac_reconcile(rbac_http, rule)
        else:
            # Unknown types are rejected at create; a row predating a
            # removed action type still gets an honest log line.
            outcome = f"actionType '{rule.actionType}' not implemented — no action taken"
        await _log_execution(rule, trigger_source, True, outcome, event=event)
        log.info("rule '%s' fired (%s): %s", rule.name, trigger_source, outcome)
    except Exception as e:
        await _log_execution(rule, trigger_source, True, "action raised", error=str(e), event=event)
        log.exception("rule '%s' action failed (%s)", rule.name, trigger_source)
