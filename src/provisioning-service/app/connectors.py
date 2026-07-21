"""
Connector Framework — common connector contract (REQ-COR-TGT-003) plus the
two out-of-the-box connectors: generic Active Directory (REQ-COR-TGT-004)
and Microsoft Entra ID (REQ-COR-TGT-005).

Both connectors implement verify-before-write so grant/revoke are idempotent
(REQ-COR-PROV-007). Connection secrets are resolved at runtime from
environment variables that the AKS CSI Secret Store driver mounts from
Key Vault (REQ-INF-061/062) — never hardcoded.
"""
import os
import logging
from abc import ABC, abstractmethod

import httpx
from azure.identity.aio import DefaultAzureCredential

log = logging.getLogger("connector-framework")


class ConnectorError(Exception):
    """Raised on any recoverable connector failure; triggers retry logic."""


class BaseConnector(ABC):
    """Common contract per REQ-COR-TGT-003."""

    @abstractmethod
    async def test_connection(self) -> bool: ...

    @abstractmethod
    async def grant(self, task: dict) -> None: ...

    @abstractmethod
    async def revoke(self, task: dict) -> None: ...

    async def execute(self, operation: str, task: dict) -> None:
        handler = {
            "grant": self.grant,
            "revoke": self.revoke,
            "create-account": getattr(self, "create_account", None),
            "disable-account": getattr(self, "disable_account", None),
        }.get(operation)
        if handler is None:
            raise ConnectorError(f"operation '{operation}' not supported by {type(self).__name__}")
        await handler(task)


# ---------------------------------------------------------------------------
# Microsoft Entra ID connector (Graph API) — REQ-COR-TGT-005
# ---------------------------------------------------------------------------
class EntraIdConnector(BaseConnector):
    """
    Uses the platform's workload identity (federated credential) with Graph
    application permissions: GroupMember.ReadWrite.All at minimum.
    task.payload expects: { "userObjectId": ..., "groupObjectId": ... }
    """

    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self) -> None:
        self._credential = DefaultAzureCredential()

    async def _token(self) -> str:
        token = await self._credential.get_token("https://graph.microsoft.com/.default")
        return token.token

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {await self._token()}"},
            timeout=30,
        )

    async def test_connection(self) -> bool:
        async with await self._client() as c:
            r = await c.get(f"{self.GRAPH}/organization?$select=id")
            return r.status_code == 200

    async def _is_member(self, c: httpx.AsyncClient, group: str, user: str) -> bool:
        r = await c.get(f"{self.GRAPH}/groups/{group}/members/{user}/$ref")
        return r.status_code == 200

    async def grant(self, task: dict) -> None:
        p = task["payload"]
        user, group = p["userObjectId"], p["groupObjectId"]
        async with await self._client() as c:
            if await self._is_member(c, group, user):        # idempotency
                log.info("entra grant: %s already in %s — no-op", user, group)
                return
            r = await c.post(
                f"{self.GRAPH}/groups/{group}/members/$ref",
                json={"@odata.id": f"{self.GRAPH}/directoryObjects/{user}"},
            )
            if r.status_code not in (204, 200):
                raise ConnectorError(f"entra grant failed [{r.status_code}]: {r.text[:300]}")

    async def revoke(self, task: dict) -> None:
        p = task["payload"]
        user, group = p["userObjectId"], p["groupObjectId"]
        async with await self._client() as c:
            if not await self._is_member(c, group, user):     # idempotency
                log.info("entra revoke: %s not in %s — no-op", user, group)
                return
            r = await c.delete(f"{self.GRAPH}/groups/{group}/members/{user}/$ref")
            if r.status_code != 204:
                raise ConnectorError(f"entra revoke failed [{r.status_code}]: {r.text[:300]}")

    async def discover_accounts(self, instance_config: dict) -> list[dict]:
        """Paged user discovery for aggregation runs (REQ-COR-TGT-008)."""
        accounts, url = [], f"{self.GRAPH}/users?$select=id,userPrincipalName,accountEnabled&$top=999"
        async with await self._client() as c:
            while url:
                r = await c.get(url)
                if r.status_code != 200:
                    raise ConnectorError(f"entra discovery failed [{r.status_code}]")
                body = r.json()
                accounts.extend(body.get("value", []))
                url = body.get("@odata.nextLink")
        return accounts


# ---------------------------------------------------------------------------
# Generic Active Directory connector (LDAPS) — REQ-COR-TGT-004
# ---------------------------------------------------------------------------
class ActiveDirectoryConnector(BaseConnector):
    """
    LDAPS connector using ldap3. Bind credentials are injected from Key Vault
    via CSI-mounted env vars: AD_SERVER, AD_BIND_DN, AD_BIND_PASSWORD, AD_BASE_DN.
    task.payload expects: { "userDn": ..., "groupDn": ... }

    ldap3 is synchronous; calls are pushed to a thread to avoid blocking the
    event loop.
    """

    def __init__(self) -> None:
        self.server_uri = os.environ.get("AD_SERVER", "")
        self.bind_dn = os.environ.get("AD_BIND_DN", "")
        self.bind_pw = os.environ.get("AD_BIND_PASSWORD", "")
        self.base_dn = os.environ.get("AD_BASE_DN", "")

    def _conn(self):
        import ldap3
        server = ldap3.Server(self.server_uri, use_ssl=True, get_info=ldap3.NONE)
        return ldap3.Connection(server, self.bind_dn, self.bind_pw,
                                auto_bind=True, raise_exceptions=True)

    async def _run(self, fn, *args):
        import asyncio
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:  # ldap3 exceptions → retryable ConnectorError
            raise ConnectorError(f"ad operation failed: {exc}") from exc

    async def test_connection(self) -> bool:
        def _test():
            conn = self._conn()
            ok = conn.bound
            conn.unbind()
            return ok
        return await self._run(_test)

    async def grant(self, task: dict) -> None:
        p = task["payload"]

        def _grant():
            import ldap3
            conn = self._conn()
            try:
                conn.search(p["groupDn"], "(objectClass=group)", attributes=["member"])
                members = conn.entries[0].member.values if conn.entries else []
                if p["userDn"] in members:                    # idempotency
                    log.info("ad grant: %s already in %s — no-op", p["userDn"], p["groupDn"])
                    return
                conn.modify(p["groupDn"],
                            {"member": [(ldap3.MODIFY_ADD, [p["userDn"]])]})
            finally:
                conn.unbind()
        await self._run(_grant)

    async def revoke(self, task: dict) -> None:
        p = task["payload"]

        def _revoke():
            import ldap3
            conn = self._conn()
            try:
                conn.search(p["groupDn"], "(objectClass=group)", attributes=["member"])
                members = conn.entries[0].member.values if conn.entries else []
                if p["userDn"] not in members:                # idempotency
                    log.info("ad revoke: %s not in %s — no-op", p["userDn"], p["groupDn"])
                    return
                conn.modify(p["groupDn"],
                            {"member": [(ldap3.MODIFY_DELETE, [p["userDn"]])]})
            finally:
                conn.unbind()
        await self._run(_revoke)

    async def disable_account(self, task: dict) -> None:
        p = task["payload"]

        def _disable():
            import ldap3
            conn = self._conn()
            try:
                # userAccountControl 514 = NORMAL_ACCOUNT | ACCOUNTDISABLE
                conn.modify(p["userDn"],
                            {"userAccountControl": [(ldap3.MODIFY_REPLACE, [514])]})
            finally:
                conn.unbind()
        await self._run(_disable)


CONNECTOR_REGISTRY: dict[str, BaseConnector] = {
    "entra": EntraIdConnector(),
    "ad": ActiveDirectoryConnector(),
}
