/**
 * MSAL configuration — auth-code + PKCE against Entra (REQ-UI-010..013).
 * No client secret exists anywhere in this flow: the SPA app registration is
 * a public client and PKCE protects the code exchange.
 *
 * The three IDs come from web/.env.local (see .env.example), filled from the
 * deploy.sh [HUMAN gate, Phase 3.4] output after the SPA app registration is
 * created. They are identifiers, not secrets.
 */
import { Configuration, LogLevel } from "@azure/msal-browser";

export const TENANT_ID = import.meta.env.VITE_TENANT_ID as string;
export const SPA_CLIENT_ID = import.meta.env.VITE_SPA_CLIENT_ID as string;
export const API_APP_ID = import.meta.env.VITE_API_APP_ID as string;

/**
 * Delegated scope exposed on iga-platform-api (part of the same human gate).
 * The access token minted for this scope carries the signing user's assigned
 * app roles in its `roles` claim — the exact claim require_role() already
 * validates server-side — so the backend needed zero changes for user auth.
 */
export const API_SCOPES = [`api://${API_APP_ID}/access_as_user`];

export const msalConfig: Configuration = {
  auth: {
    clientId: SPA_CLIENT_ID,
    authority: `https://login.microsoftonline.com/${TENANT_ID}`,
    redirectUri: window.location.origin,
    postLogoutRedirectUri: window.location.origin,
  },
  cache: {
    // sessionStorage over localStorage: tokens don't survive the tab, which
    // is the conservative default for an admin tool.
    cacheLocation: "sessionStorage",
  },
  system: {
    loggerOptions: {
      logLevel: LogLevel.Warning,
      loggerCallback: (_level, message, containsPii) => {
        if (!containsPii) console.warn(message);
      },
    },
  },
};

/** Decode the payload of a JWT without verifying it (display/routing only —
 * enforcement is entirely server-side in each service's require_role()). */
export function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const base64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(base64));
  } catch {
    return {};
  }
}
