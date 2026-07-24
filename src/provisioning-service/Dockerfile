FROM python:3.12-slim AS base
WORKDIR /app

# msodbcsql18 (ODBC Driver 18 for SQL Server) — required for aioodbc/pyodbc
# Entra-token SQL auth (REQ-INF-040, db.py), added in Phase 3.5 when this
# service gained its sqldb-provisioning task-state store. ACCEPT_EULA is
# required by Microsoft's package for unattended install.
#
# Pinned to the bookworm (Debian 12) package repo rather than reading the
# base image's actual codename: python:3.12-slim is currently on trixie
# (Debian 13), and Microsoft's debian/13/prod repo fails APT's newer sqv
# signature verification there ("Missing key ... needed to verify
# signature") — msodbcsql18 isn't officially published for trixie yet.
# bookworm's .debs install cleanly on this base (verified below) since the
# driver only depends on stable, versioned libc-level SONAMEs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg unixodbc ca-certificates libgssapi-krb5-2 \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/* \
    && find /opt/microsoft/msodbcsql18 -name '*.so*' | grep -q . || (echo "msodbcsql18 driver .so missing after install" && exit 1) \
    && if ldd /opt/microsoft/msodbcsql18/lib64/libmsodbcsql-18.*.so.* | grep -qi "not found"; then \
        echo "msodbcsql18 has unresolved shared library dependencies:"; \
        ldd /opt/microsoft/msodbcsql18/lib64/libmsodbcsql-18.*.so.*; \
        exit 1; \
       fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini .

# Non-root user (container hardening)
RUN useradd -m appuser
USER appuser
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
