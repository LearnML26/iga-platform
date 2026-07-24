"""
Notification Service — consumes the `notification-tasks` Service Bus queue
and fans failures/events out to email + webhooks.
Implements: Phase 3.3 (roadmap/PHASES.md). No REQ-COR-NOTIF-xxx IDs are
cited in that phase entry (checked — PHASES.md 3.3 lists no REQ IDs, unlike
sibling entries); the upstream event this consumes is produced under
REQ-COR-PROV-004 (provisioning-service's notify_failure()).

Architecture:
- FastAPI app exposes only health probes — this service has no domain HTTP
  API and no database of its own (not in the sqldb-* list in CLAUDE.md's
  "Architecture conventions" section); its only job is being a queue
  consumer + fan-out worker, same shape as flatfile-connector-service minus
  the inbound API.
- A background worker (app/worker.py) consumes the non-session
  'notification-tasks' queue (confirmed sessions:false in
  infra/modules/messaging.bicep) and dispatches by the message's `type`
  field. Today's only implemented handler is ProvisioningFailed, matching
  provisioning-service's notify_failure() message shape exactly (verified
  by reading that function's body, not assumed from a spec description).
- Email via SMTP relay (aiosmtplib) + webhook fan-out
  (app/notifiers.py) — both configured purely from env vars sourced from a
  k8s Secret populated from Key Vault; this service never handles a raw
  secret value itself (CLAUDE.md guardrail #1).

/healthz and /readyz probes (REQ-INF-035).
"""
import asyncio
import logging
import os

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from fastapi import FastAPI, HTTPException

from .worker import worker_loop

logging.basicConfig(level=logging.INFO)
logging.getLogger("azure").setLevel(logging.WARNING)  # SDK HTTP pipeline logging is very verbose at INFO
log = logging.getLogger("notification-service")

SB_NAMESPACE = os.environ.get("SERVICEBUS_NAMESPACE", "")  # sb-iga-dev.servicebus.windows.net

app = FastAPI(title="IGA Notification Service", version="1.0.0")


@app.on_event("startup")
async def startup() -> None:
    app.state.ready = False
    app.state.credential = DefaultAzureCredential()
    app.state.sb = ServiceBusClient(
        fully_qualified_namespace=SB_NAMESPACE, credential=app.state.credential
    )
    app.state.worker = asyncio.create_task(worker_loop(app.state.sb))
    app.state.ready = True
    log.info("Notification Service started; sb=%s", SB_NAMESPACE)


@app.on_event("shutdown")
async def shutdown() -> None:
    app.state.worker.cancel()
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
