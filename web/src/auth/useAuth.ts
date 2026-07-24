/**
 * Signed-in-user context: account, API roles, persona (REQ-UI-014).
 *
 * Persona derivation is an interpretation, flagged as such in
 * roadmap/PHASES.md: no persona store exists anywhere in the platform, so
 * persona = the app roles the user was assigned on iga-platform-api (the
 * same [HUMAN gate] that lets their tokens pass require_role()). A user
 * holding identities.write is routed to the admin console; everyone else
 * gets the end-user portal. This is display/routing only — every API call
 * is enforced server-side regardless of what the client renders.
 */
import { useEffect, useState } from "react";
import { useMsal } from "@azure/msal-react";
import { decodeJwtPayload } from "../authConfig";
import { getApiToken } from "../api/client";

export interface AuthInfo {
  name: string;
  username: string;
  roles: string[];
  isAdmin: boolean;
  loaded: boolean;
  error?: string;
}

export function useAuth(): AuthInfo {
  const { accounts } = useMsal();
  const [info, setInfo] = useState<AuthInfo>({
    name: "", username: "", roles: [], isAdmin: false, loaded: false,
  });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (accounts.length === 0) return;
      try {
        const token = await getApiToken();
        const payload = decodeJwtPayload(token);
        const roles = Array.isArray(payload.roles) ? (payload.roles as string[]) : [];
        if (!cancelled) {
          setInfo({
            name: accounts[0].name ?? accounts[0].username,
            username: accounts[0].username,
            roles,
            isAdmin: roles.includes("identities.write"),
            loaded: true,
          });
        }
      } catch (e) {
        if (!cancelled) {
          setInfo((prev) => ({ ...prev, loaded: true, error: e instanceof Error ? e.message : String(e) }));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [accounts]);

  return info;
}
