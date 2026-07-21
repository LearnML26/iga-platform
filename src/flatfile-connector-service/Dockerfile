FROM python:3.12-slim AS base
WORKDIR /app

# No SQL driver needed here (unlike source-system-service) — this service
# only talks to Blob Storage (azure-storage-blob) and source-system-service
# over HTTP, so it skips the msodbcsql18/bookworm-pin dance entirely.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

# Non-root user (container hardening)
RUN useradd -m appuser
USER appuser
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
