/** Admin console — target system instances (REQ-UI-023). The registry is
 * source-system-service's, dual-purposed as the target registry (2.3/3.1
 * precedent). Note: that service has no auth wired (pre-existing gap,
 * flagged in PHASES.md) — the browser still sends the bearer token. */
import { useState } from "react";
import { api } from "../api/client";
import { SourceSystem } from "../api/types";
import { Empty, ErrorBox, StatusPill, useLoad } from "../components/bits";

export default function TargetSystemsPage() {
  const { data, error, loading, refresh } = useLoad<SourceSystem[]>(
    () => api.get("/api/source/source-systems?limit=100"),
  );
  const [name, setName] = useState("");
  const [connectorType, setConnectorType] = useState("flat-file");
  const [createError, setCreateError] = useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setCreateError(null);
    try {
      await api.post("/api/source/source-systems", { name, connectorType });
      setName("");
      refresh();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <section>
      <h2>Target system instances</h2>
      <form className="filter-bar" onSubmit={create}>
        <input placeholder="New instance name" value={name} onChange={(e) => setName(e.target.value)} required />
        <select value={connectorType} onChange={(e) => setConnectorType(e.target.value)}>
          <option value="flat-file">flat-file</option>
          <option value="ad">ad</option>
          <option value="entra">entra</option>
        </select>
        <button className="btn btn-primary" type="submit">Create</button>
      </form>
      <ErrorBox error={createError} />
      <ErrorBox error={error} />
      {loading && <div className="page-loading">Loading…</div>}
      {data && data.length === 0 && <Empty>No instances registered.</Empty>}
      {data && data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr><th>Name</th><th>Connector</th><th>Status</th><th>Provisioning targets</th><th>Owner identity</th><th>Id</th></tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td>{s.connectorType}</td>
                <td><StatusPill value={s.status} /></td>
                <td className="mono">{s.provisioningTargets.join(", ") || "—"}</td>
                <td className="mono">{s.ownerIdentityId ?? "—"}</td>
                <td className="mono">{s.id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
