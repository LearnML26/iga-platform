import { NavLink, Outlet } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { AuthInfo } from "../auth/useAuth";

export default function Layout({ auth }: { auth: AuthInfo }) {
  const { instance } = useMsal();
  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">IGA Platform</span>
        <nav>
          {auth.isAdmin && (
            <>
              <span className="nav-group">Admin</span>
              <NavLink to="/admin/identities">Identities</NavLink>
              <NavLink to="/admin/targets">Target systems</NavLink>
              <NavLink to="/admin/tasks">Provisioning</NavLink>
              <NavLink to="/admin/feed-runs">Feed runs</NavLink>
            </>
          )}
          <span className="nav-group">Portal</span>
          <NavLink to="/portal/access">My access</NavLink>
          <NavLink to="/portal/request">Request access</NavLink>
          <NavLink to="/portal/approvals">My approvals</NavLink>
        </nav>
        <div className="user-box">
          <span title={auth.roles.join(", ")}>{auth.name}</span>
          <button className="btn btn-ghost" onClick={() => instance.logoutRedirect()}>
            Sign out
          </button>
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
