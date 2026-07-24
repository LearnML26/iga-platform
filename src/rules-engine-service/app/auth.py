"""
Entra ID JWT validation (REQ-COR-API-001/002, minimal slice — Phase 1R.3).

Validates bearer tokens against the tenant's JWKS, requires audience =
the `iga-platform-api` app registration, and enforces a per-endpoint app
role via the token's `roles` claim (app-only/client-credentials tokens from
each service's own workload identity — no client secrets involved).

/healthz and /readyz stay anonymous (not wired to this module).
"""
import logging
import os

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient

log = logging.getLogger("auth")

ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "")
API_AUDIENCE = os.environ.get("API_AUDIENCE", "")  # e.g. api://<iga-platform-api appId>
# v2 tokens for app-only (client-credentials/workload-identity) callers put the
# bare appId GUID in `aud` when the caller requested a token via --resource;
# callers using --scope <uri>/.default can get the api://<appId> URI form
# instead. Accept both rather than dictating how future callers must ask.
_APP_ID = API_AUDIENCE.removeprefix("api://")
VALID_AUDIENCES = [_APP_ID, f"api://{_APP_ID}"]
ISSUER = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0"
JWKS_URI = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/discovery/v2.0/keys"

_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    # Lazy singleton; PyJWKClient caches keys internally and only re-fetches
    # the JWKS on a signing-key-not-found (kid rotation), not on every call.
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(JWKS_URI)
    return _jwk_client


def require_role(required_role: str):
    """FastAPI dependency factory: 401 with no/invalid token, 403 without the role."""

    async def _check(authorization: str | None = Header(None)) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1]
        try:
            signing_key = _get_jwk_client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=VALID_AUDIENCES,
                issuer=ISSUER,
            )
        except Exception as exc:  # noqa: BLE001 — any validation failure is a 401
            log.warning("token validation failed: %s", exc)
            raise HTTPException(status_code=401, detail="invalid token")

        roles = claims.get("roles", [])
        if required_role not in roles:
            raise HTTPException(status_code=403, detail=f"missing required role '{required_role}'")
        return claims

    return Depends(_check)
