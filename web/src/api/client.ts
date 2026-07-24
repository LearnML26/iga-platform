/**
 * API client — acquires a delegated access token for iga-platform-api via
 * MSAL and calls the platform services under /api/<service>/... paths.
 *
 * Path shape: /api/identity, /api/provisioning, /api/source, /api/rbac,
 * /api/requests. In local dev, vite.config.ts proxies these to the
 * port-forwards opened by scripts/dev-portal.sh. The same paths are what an
 * APIM front door (Phase 4.5) can expose publicly later, so nothing here
 * changes when that lands. VITE_API_BASE can prefix an absolute origin once
 * it exists.
 */
import { IPublicClientApplication, InteractionRequiredAuthError } from "@azure/msal-browser";
import { API_SCOPES } from "../authConfig";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

let msalInstance: IPublicClientApplication | null = null;

export function registerMsal(instance: IPublicClientApplication) {
  msalInstance = instance;
}

export async function getApiToken(): Promise<string> {
  if (!msalInstance) throw new Error("MSAL not initialised");
  const account = msalInstance.getAllAccounts()[0];
  if (!account) throw new Error("not signed in");
  try {
    const result = await msalInstance.acquireTokenSilent({ scopes: API_SCOPES, account });
    return result.accessToken;
  } catch (e) {
    if (e instanceof InteractionRequiredAuthError) {
      await msalInstance.acquireTokenRedirect({ scopes: API_SCOPES, account });
    }
    throw e;
  }
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`HTTP ${status}: ${detail}`);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = await getApiToken();
  const resp = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const parsed = await resp.json();
      detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail ?? parsed);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export const api = {
  get: <T>(path: string) => call<T>("GET", path),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => call<T>("PATCH", path, body),
  delete: <T>(path: string) => call<T>("DELETE", path),
};
