/** Admin console — source system feed runs with delta summaries
 * (REQ-UI-024/025; delta fields from REQ-COR-ID-006). */
import { useState } from "react";
import { api } from "../api/client";
import { FeedRun, SourceSystem } from "../api/types";
import { Empty, ErrorBox, StatusPill, fmtDate, useLoad } from "../components/bits";

export default function FeedRunsPage() {
  const systems = useLoad<SourceSystem[]>(() => api.get("/api/source/source-systems?limit=100"));
  const [selected, setSelected] = useState("");
  const runs = useLoad<FeedRun[]>(
    () => (selected ? api.get(`/api/source/source-systems/${selected}/feed-runs`) : Promise.resolve([])),
    [selected],
  );

  return (
    <section>
      <h2>Feed runs</h2>
      <div className="filter-bar">
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">Select a source system…</option>
          {systems.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <button className="btn" onClick={runs.refresh} disabled={!selected}>Refresh</button>
      </div>
      <ErrorBox error={systems.error} />
      <ErrorBox error={runs.error} />
      {selected && runs.data && runs.data.length === 0 && <Empty>No runs for this source system.</Empty>}
      {runs.data && runs.data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Started</th><th>Status</th><th>Trigger</th><th>Processed</th>
              <th>Added</th><th>Updated</th><th>Terminated</th><th>Quarantined</th><th>Error</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.map((r) => (
              <tr key={r.id}>
                <td>{fmtDate(r.startedAt)}</td>
                <td><StatusPill value={r.status} /></td>
                <td>{r.triggeredBy}</td>
                <td>{r.recordsProcessed}</td>
                <td>{r.recordsAdded}</td>
                <td>{r.recordsUpdated}</td>
                <td>{r.recordsTerminated}</td>
                <td>{r.recordsQuarantined}</td>
                <td className="error-cell" title={r.errorSummary ?? ""}>{r.errorSummary ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
