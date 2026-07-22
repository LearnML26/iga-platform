FROM python:3.12-slim AS base
WORKDIR /app

# No SQL driver needed (this service owns no database) and no LDAP/AD deps
# (unlike provisioning-service) — it only talks to Service Bus
# (azure-servicebus), an SMTP relay (aiosmtplib), and outbound webhook URLs
# (httpx), so it skips the msodbcsql18/bookworm-pin dance entirely.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

# Non-root user (container hardening)
RUN useradd -m appuser
USER appuser
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
