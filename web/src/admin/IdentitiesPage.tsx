/** Admin console — identities list/search (REQ-UI-020/021). */
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Identity } from "../api/types";
import { Empty, ErrorBox, StatusPill, useLoad } from "../components/bits";

export default function IdentitiesPage() {
  const [q, setQ] = useState("");
  const [department, setDepartment] = useState("");
  const [status, setStatus] = useState("");
  const [applied, setApplied] = useState({ q: "", department: "", status: "" });

  const { data, error, loading } = useLoad<Identity[]>(() => {
    const params = new URLSearchParams();
    if (applied.q) params.set("q", applied.q);
    if (applied.department) params.set("department", applied.department);
    if (applied.status) params.set("status", applied.status);
    params.set("limit", "100");
    return api.get(`/api/identity/identities?${params}`);
  }, [applied]);

  return (
    <section>
      <h2>Identities</h2>
      <form
        className="filter-bar"
        onSubmit={(e) => { e.preventDefault(); setApplied({ q, department, status }); }}
      >
        <input placeholder="Name contains…" value={q} onChange={(e) => setQ(e.target.value)} />
        <input placeholder="Department" value={department} onChange={(e) => setDepartment(e.target.value)} />
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option>
          <option value="active">active</option>
          <option value="pending-start">pending-start</option>
          <option value="terminated">terminated</option>
        </select>
        <button className="btn btn-primary" type="submit">Search</button>
      </form>
      <ErrorBox error={error} />
      {loading && <div className="page-loading">Loading…</div>}
      {data && data.length === 0 && <Empty>No identities match.</Empty>}
      {data && data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr><th>Name</th><th>Correlation key</th><th>Department</th><th>Job title</th><th>Status</th></tr>
          </thead>
          <tbody>
            {data.map((i) => (
              <tr key={i.identityId}>
                <td><Link to={`/admin/identities/${i.identityId}`}>{i.displayName}</Link></td>
                <td className="mono">{i.correlationKey}</td>
                <td>{i.department ?? "—"}</td>
                <td>{i.jobTitle ?? "—"}</td>
                <td><StatusPill value={i.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
