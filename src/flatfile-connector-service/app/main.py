"""
Flat-File Connector Service (REQ-COR-SRC-002).

Ingests a CSV dropped in the ADLS `raw/` container against a
source-system-service instance's attribute mappings, quarantining malformed
rows and producing a FeedRun delta summary (REQ-COR-ID-006). Owns no
database of its own — SourceSystemInstance/AttributeMapping/FeedRun live in
source-system-service (2.1); see app/ingest.py for the full design notes and
the scoped delta semantics (no identity-service integration until 2.3).

/healthz and /readyz probes (REQ-INF-035).
"""
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .ingest import IngestError, run_ingestion
from .lifecycle import run_sweep

logging.basicConfig(level=logging.INFO)
logging.getLogger("azure").setLevel(logging.WARNING)  # SDK HTTP pipeline logging is very verbose at INFO
log = logging.getLogger("flatfile-connector-service")

app = FastAPI(title="IGA Flat-File Connector Service", version="1.0.0")


class IngestRequest(BaseModel):
    sourceSystemInstanceId: str
    blobPath: str
    triggeredBy: str = "manual"


@app.on_event("startup")
async def startup() -> None:
    app.state.ready = True
    log.info("Flat-File Connector Service started")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not getattr(app.state, "ready", False):
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


@app.post("/ingest")
async def ingest(body: IngestRequest):
    result = await run_ingestion(body.sourceSystemInstanceId, body.blobPath, body.triggeredBy)
    if result.get("status") == "failed":
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/lifecycle/sweep")
async def lifecycle_sweep():
    """Phase 2.4 (REQ-COR-SRC-007/008): activate due pending-start joiners,
    apply due scheduled terminations (dispatching disable-account tasks per
    the source instance's provisioningTargets), and retry any pending
    provisioning dispatches from earlier runs. Triggered daily by the
    lifecycle-sweep CronJob; safe to invoke ad hoc — a second run the same
    day finds nothing left to do. Cluster-internal and unauthenticated,
    same posture as /ingest. See app/lifecycle.py for design notes."""
    try:
        return await run_sweep()
    except IngestError as e:
        raise HTTPException(status_code=500, detail=str(e))
