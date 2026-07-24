/**
 * Persona routing (REQ-UI-014): one login for everyone; admins (users whose
 * token carries identities.write) land on the admin console and can also use
 * the portal; everyone else lands on the end-user portal. Routing is
 * client-side convenience only — every service re-validates the token and
 * role on every call.
 */
import { AuthenticatedTemplate, UnauthenticatedTemplate } from "@azure/msal-react";
import { Navigate, Route, Routes } from "react-router-dom";
import LoginPage from "./auth/LoginPage";
import { useAuth } from "./auth/useAuth";
import Layout from "./components/Layout";
import IdentitiesPage from "./admin/IdentitiesPage";
import IdentityDetailPage from "./admin/IdentityDetailPage";
import TargetSystemsPage from "./admin/TargetSystemsPage";
import TasksPage from "./admin/TasksPage";
import FeedRunsPage from "./admin/FeedRunsPage";
import MyAccessPage from "./portal/MyAccessPage";
import RequestCartPage from "./portal/RequestCartPage";
import ApprovalsPage from "./portal/ApprovalsPage";

function AuthedApp() {
  const auth = useAuth();
  if (!auth.loaded) return <div className="page-loading">Signing you in…</div>;
  if (auth.error) {
    return (
      <div className="page-loading">
        <p>Could not acquire an API token: {auth.error}</p>
        <p>
          If this is a fresh setup, check that the [HUMAN gate, Phase 3.4] SPA
          registration steps ran and that your user is assigned app roles on
          iga-platform-api.
        </p>
      </div>
    );
  }
  return (
    <Routes>
      <Route element={<Layout auth={auth} />}>
        {auth.isAdmin && (
          <>
            <Route path="/admin/identities" element={<IdentitiesPage />} />
            <Route path="/admin/identities/:id" element={<IdentityDetailPage />} />
            <Route path="/admin/targets" element={<TargetSystemsPage />} />
            <Route path="/admin/tasks" element={<TasksPage />} />
            <Route path="/admin/feed-runs" element={<FeedRunsPage />} />
          </>
        )}
        <Route path="/portal/access" element={<MyAccessPage />} />
        <Route path="/portal/request" element={<RequestCartPage />} />
        <Route path="/portal/approvals" element={<ApprovalsPage />} />
        <Route
          path="*"
          element={<Navigate to={auth.isAdmin ? "/admin/identities" : "/portal/access"} replace />}
        />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <>
      <AuthenticatedTemplate>
        <AuthedApp />
      </AuthenticatedTemplate>
      <UnauthenticatedTemplate>
        <LoginPage />
      </UnauthenticatedTemplate>
    </>
  );
}
