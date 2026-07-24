/**
 * Unified login page (REQ-UI-010/013): one entry point for every persona;
 * where you land after sign-in is decided by persona routing (App.tsx).
 */
import { useMsal } from "@azure/msal-react";
import { API_SCOPES } from "../authConfig";

export default function LoginPage() {
  const { instance } = useMsal();
  return (
    <div className="login-shell">
      <div className="login-card">
        <h1>IGA Platform</h1>
        <p>Identity governance &amp; administration</p>
        <button
          className="btn btn-primary"
          onClick={() => instance.loginRedirect({ scopes: API_SCOPES })}
        >
          Sign in with Microsoft Entra
        </button>
        <p className="login-note">
          Single sign-on via your organisation account. Access is governed by
          the app roles assigned to you — the portal and console only render
          what your token allows, and every API call is re-checked server-side.
        </p>
      </div>
    </div>
  );
}
