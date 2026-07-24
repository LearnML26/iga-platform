/** Portal — my access (REQ-UI-030): role assignments for the linked
 * identity, from rbac-service's cross-role assignments view. */
import { useState } from "react";
import { api } from "../api/client";
import { RoleAssignmentView } from "../api/types";
import { Empty, ErrorBox, StatusPill, fmtDate, useLoad } from "../components/bits";
import { IdentityLinkBanner, clearMyIdentity, getMyIdentityId, getMyIdentityName } from "./myIdentity";

export default function MyAccessPage() {
  const [identityId, setIdentityId] = useState(getMyIdentityId());
  const { data, error, loading } = useLoad<RoleAssignmentView[]>(
    () => (identityId ? api.get(`/api/rbac/assignments?identityId=${identityId}`) : Promise.resolve([])),
    [identityId],
  );

  if (!identityId) return <IdentityLinkBanner onLinked={() => setIdentityId(getMyIdentityId())} />;

  return (
    <section>
      <h2>My access</h2>
      <p className="linked-as">
        Linked as <strong>{getMyIdentityName()}</strong>{" "}
        <button className="btn btn-ghost" onClick={() => { clearMyIdentity(); setIdentityId(null); }}>
          Unlink
        </button>
      </p>
      <ErrorBox error={error} />
      {loading && <div className="page-loading">Loading…</div>}
      {data && data.length === 0 && <Empty>No active role assignments.</Empty>}
      {data && data.length > 0 && (
        <div className="card-list">
          {data.map((a) => (
            <div className="card" key={a.id}>
              <div className="card-head">
                <strong>{a.roleName}</strong>
                <StatusPill value={a.status} />
                <span className="pill pill-neutral">{a.assignmentType}</span>
              </div>
              {a.roleDescription && <p>{a.roleDescription}</p>}
              <ul className="ent-list">
                {a.entitlements.map((e, i) => (
                  <li key={i} className="mono">
                    {e.connectorType} · {e.entitlementRef}
                  </li>
                ))}
                {a.entitlements.length === 0 && <li>(role has no entitlements)</li>}
              </ul>
              <span className="muted">granted {fmtDate(a.createdDate)}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
