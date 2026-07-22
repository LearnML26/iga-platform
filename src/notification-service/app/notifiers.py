"""
Outbound notification channels for notification-service (Phase 3.3,
roadmap/PHASES.md).

Two channels, both best-effort / independently failing:
  - EmailNotifier: SMTP relay (aiosmtplib). Chosen over ACS Email because
    `infra/` has no Communication Services resource today (grep confirms:
    no ACS/CommunicationServices/EmailServices module in infra/modules) and
    CLAUDE.md guardrail #4 says don't add new Azure resources beyond Bicep
    without asking. An SMTP relay (existing corporate relay, SendGrid SMTP,
    etc.) needs no new Azure infra — only Key Vault secrets — so it fits the
    "least new footprint" bar better for this task. Swapping in ACS Email
    later is a contained change (this module's send_email + a new Bicep
    Communication Services module) if the human decides to move to it.
  - WebhookNotifier: plain HTTPS POST fan-out, one request per configured
    URL, independently retried/logged (one bad subscriber must not block
    email delivery or other subscribers).

Sender configuration (SMTP host/port/username/password, from-address,
recipient list, webhook URLs) is loaded ONLY from environment variables —
populated in k8s from the `notification-sender` Secret (see
k8s/services/notification-service.yaml), which the human populates from Key
Vault. This module never fabricates or hardcodes a credential (CLAUDE.md
guardrail #1); if SMTP/webhook config is absent, the relevant channel is
skipped with a logged warning rather than failing the message.
"""
import asyncio
import logging
import os
from email.message import EmailMessage
from typing import Any

import aiosmtplib
import httpx

log = logging.getLogger("notification-service.notifiers")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")  # never logged/echoed
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

NOTIFY_EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "")
NOTIFY_EMAIL_TO = [
    a.strip() for a in os.environ.get("NOTIFY_EMAIL_TO", "").split(",") if a.strip()
]
NOTIFY_WEBHOOK_URLS = [
    u.strip() for u in os.environ.get("NOTIFY_WEBHOOK_URLS", "").split(",") if u.strip()
]

WEBHOOK_TIMEOUT_SECONDS = 10.0


def email_configured() -> bool:
    return bool(SMTP_HOST and NOTIFY_EMAIL_FROM and NOTIFY_EMAIL_TO)


def webhooks_configured() -> bool:
    return bool(NOTIFY_WEBHOOK_URLS)


async def send_email(subject: str, body: str) -> None:
    """Send a plain-text email to NOTIFY_EMAIL_TO via the configured SMTP
    relay. No-op (with a warning) if sender config is absent — this lets the
    service run in dev before the human has populated the Key Vault secrets,
    per the Phase 3.3 [HUMAN gate] note in roadmap/PHASES.md.
    """
    if not email_configured():
        log.warning("email channel not configured (SMTP_HOST/NOTIFY_EMAIL_FROM/NOTIFY_EMAIL_TO); skipping: %s", subject)
        return

    msg = EmailMessage()
    msg["From"] = NOTIFY_EMAIL_FROM
    msg["To"] = ", ".join(NOTIFY_EMAIL_TO)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USERNAME or None,
            password=SMTP_PASSWORD or None,
            start_tls=SMTP_USE_TLS,
            timeout=15,
        )
        log.info("email sent: %s -> %s", subject, NOTIFY_EMAIL_TO)
    except Exception:
        log.exception("email send failed: %s", subject)
        raise


async def fan_out_webhooks(payload: dict[str, Any]) -> None:
    """POST payload to every configured webhook URL, independently. A
    failing subscriber is logged, not raised — one bad webhook endpoint must
    not fail the whole notification (and must not block email delivery).
    """
    if not webhooks_configured():
        log.warning("no webhook URLs configured (NOTIFY_WEBHOOK_URLS); skipping fan-out for %s", payload.get("type"))
        return

    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
        results = await asyncio.gather(
            *(_post_one(client, url, payload) for url in NOTIFY_WEBHOOK_URLS),
            return_exceptions=True,
        )
    for url, result in zip(NOTIFY_WEBHOOK_URLS, results):
        if isinstance(result, Exception):
            log.warning("webhook delivery failed for %s: %s", url, result)


async def _post_one(client: httpx.AsyncClient, url: str, payload: dict[str, Any]) -> None:
    resp = await client.post(url, json=payload)
    resp.raise_for_status()
    log.info("webhook delivered to %s (status %d)", url, resp.status_code)
