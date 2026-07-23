"""
Service Bus consumer for the `notification-tasks` queue (Phase 3.3,
roadmap/PHASES.md; upstream producer: provisioning-service's
notify_failure(), src/provisioning-service/app/main.py).

Queue shape verified directly from infra/modules/messaging.bicep:
  { name: 'notification-tasks', sessions: false }
i.e. NOT session-enabled, unlike provisioning-tasks. The consumer therefore
uses a plain `get_queue_receiver(QUEUE_NAME, ...)` with no `session_id` /
`NEXT_AVAILABLE_SESSION` — provisioning-service's worker_loop had a real bug
of calling session APIs against the wrong assumption; this queue does not
need that mode at all, so we deliberately avoid it entirely.

Message shape verified directly from notify_failure()'s body (not trusted
from any spec description):
    {
        "type": "ProvisioningFailed",
        "taskId": ...,
        "identityId": ...,
        "instanceId": ...,
        "operationType": ...,
        "error": ...,
        "occurredAt": ...,
    }
sent as a plain (non-session) ServiceBusMessage with no message_id/session_id
set. Other Phase 3 producers (access-request-service 3.2, certification-
service 4.3) are documented as also publishing onto this same queue
("notifications via notification queue" / "reminder/escalation via
notification queue") but have not landed yet, so their message shapes don't
exist to verify against. Dispatch is therefore by a `type` discriminator
with ProvisioningFailed as the only implemented handler; unknown types are
logged and completed (not dead-lettered/retried) so a not-yet-implemented
event type from a future service doesn't wedge the queue — extend
`_HANDLERS` as those land.

Delivery/retry: no app-level backoff here (unlike provisioning-service).
The queue's own maxDeliveryCount=5 + deadLetteringOnMessageExpiration
(infra/modules/messaging.bicep) handles retry-then-DLQ for transient
failures (e.g. SMTP relay briefly down) — on a handler exception the
message is abandoned (left un-completed) so Service Bus redelivers it.
"""
import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from .notifiers import fan_out_webhooks, send_email

log = logging.getLogger("notification-service.worker")

QUEUE_NAME = "notification-tasks"


class ProvisioningFailedEvent(BaseModel):
    type: str = "ProvisioningFailed"
    taskId: str
    identityId: str
    instanceId: str
    operationType: str
    error: str
    occurredAt: str


async def _handle_provisioning_failed(raw: dict[str, Any]) -> None:
    event = ProvisioningFailedEvent(**raw)
    subject = f"[IGA] Provisioning task failed: {event.taskId}"
    body = (
        f"Provisioning task {event.taskId} failed and was dead-lettered.\n\n"
        f"identityId:    {event.identityId}\n"
        f"instanceId:    {event.instanceId}\n"
        f"operationType: {event.operationType}\n"
        f"occurredAt:    {event.occurredAt}\n"
        f"error:         {event.error}\n"
    )
    # Independent channels: a failing webhook subscriber must not suppress
    # email, and vice versa — gather with return_exceptions and re-raise
    # only if BOTH failed (so a partial success doesn't trigger a redundant
    # Service Bus redelivery/duplicate email on top of a working webhook).
    results = await asyncio.gather(
        send_email(subject, body),
        fan_out_webhooks(event.model_dump()),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, Exception)]
    if len(failures) == len(results):
        raise failures[0]


# Phase 3.2 (access-request-service) event shapes. Neither identity-service
# nor this service has any notion of a per-identity email address (checked —
# no such field exists), and NOTIFY_EMAIL_TO/NOTIFY_WEBHOOK_URLS are a single
# static ops-distro recipient list, not per-identity routing — same posture
# as ProvisioningFailed above. So these do NOT email the actual approver;
# they land in the same static inbox with the approver's/requester's
# identityId in the body. True per-person delivery needs a real identity ->
# email mapping and is a documented gap, not silently pretended away.
class ApprovalRequestedEvent(BaseModel):
    type: str = "ApprovalRequested"
    requestId: str
    lineItemId: str
    approvalStepId: str
    stepType: str
    approverIdentityId: str
    requesterIdentityId: str
    targetSystemInstanceId: str
    entitlementRef: str
    occurredAt: str


async def _handle_approval_requested(raw: dict[str, Any]) -> None:
    event = ApprovalRequestedEvent(**raw)
    subject = f"[IGA] Approval needed: request {event.requestId}"
    body = (
        f"An access request needs a {event.stepType} decision.\n\n"
        f"requestId:      {event.requestId}\n"
        f"lineItemId:     {event.lineItemId}\n"
        f"approverIdentityId:  {event.approverIdentityId}\n"
        f"requesterIdentityId: {event.requesterIdentityId}\n"
        f"targetSystemInstanceId: {event.targetSystemInstanceId}\n"
        f"entitlementRef: {event.entitlementRef}\n"
        f"occurredAt:     {event.occurredAt}\n"
    )
    results = await asyncio.gather(
        send_email(subject, body),
        fan_out_webhooks(event.model_dump()),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, Exception)]
    if len(failures) == len(results):
        raise failures[0]


class RequestDecidedEvent(BaseModel):
    type: str = "RequestDecided"
    requestId: str
    lineItemId: str
    requesterIdentityId: str
    decision: str  # approved | rejected
    targetSystemInstanceId: str
    entitlementRef: str
    occurredAt: str


async def _handle_request_decided(raw: dict[str, Any]) -> None:
    event = RequestDecidedEvent(**raw)
    subject = f"[IGA] Request {event.decision}: {event.requestId}"
    body = (
        f"Access request line item {event.decision}.\n\n"
        f"requestId:      {event.requestId}\n"
        f"lineItemId:     {event.lineItemId}\n"
        f"requesterIdentityId: {event.requesterIdentityId}\n"
        f"targetSystemInstanceId: {event.targetSystemInstanceId}\n"
        f"entitlementRef: {event.entitlementRef}\n"
        f"occurredAt:     {event.occurredAt}\n"
    )
    results = await asyncio.gather(
        send_email(subject, body),
        fan_out_webhooks(event.model_dump()),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, Exception)]
    if len(failures) == len(results):
        raise failures[0]


# type discriminator -> handler
_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "ProvisioningFailed": _handle_provisioning_failed,
    "ApprovalRequested": _handle_approval_requested,
    "RequestDecided": _handle_request_decided,
}


async def dispatch(raw: dict[str, Any]) -> None:
    event_type = raw.get("type")
    handler = _HANDLERS.get(event_type)
    if handler is None:
        log.warning("no handler for notification type '%s'; dropping message: %s", event_type, raw)
        return
    await handler(raw)


async def handle_message(receiver, msg) -> None:
    try:
        raw = json.loads(str(msg))
    except (TypeError, ValueError):
        log.error("malformed notification-tasks message (not JSON); dead-lettering: %s", msg)
        await receiver.dead_letter_message(msg, reason="malformed-json")
        return

    try:
        await dispatch(raw)
        await receiver.complete_message(msg)
        log.info("notification %s (type=%s) processed", raw.get("taskId", "?"), raw.get("type"))
    except Exception:
        # Leave the message un-completed; Service Bus redelivers up to
        # maxDeliveryCount (5) then dead-letters it automatically.
        log.exception("notification handling failed for %s; will be redelivered", raw)


async def worker_loop(sb_client) -> None:
    while True:
        try:
            async with sb_client.get_queue_receiver(QUEUE_NAME, max_wait_time=30) as receiver:
                async for msg in receiver:
                    await handle_message(receiver, msg)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("worker loop error; restarting in 10s")
            await asyncio.sleep(10)
