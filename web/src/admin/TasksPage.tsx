/** Admin console — provisioning task queue with retry/cancel (REQ-UI-022),
 * backed by the Phase 3.5 task-state store in provisioning-service. */
import { useState } from "react";
import { api } from "../api/client";
import { ProvisioningTaskRecord } from "../api/types";
import { Empty, ErrorBox, StatusPill, fmtDate, useLoad } from "../components/bits";

const STATUSES = ["", "queued", "in-progress", "retry-scheduled", "succeeded", "dead-lettered", "cancelled"];

export default function TasksPage() {
  const [status, setStatus] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const { data, error, loading, refresh } = useLoad<ProvisioningTaskRecord[]>(() => {
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    return api.get(`/api/provisioning/tasks?${params}`);
  }, [status]);

  async function act(taskId: string, action: "retry" | "cancel") {
    setActionError(null);
    try {
      await api.post(`/api/provisioning/tasks/${taskId}/${action}`);
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section>
      <h2>Provisioning tasks</h2>
      <div className="filter-bar">
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => <option key={s} value={s}>{s || "Any status"}</option>)}
        </select>
        <button className="btn" onClick={refresh}>Refresh</button>
      </div>
      <ErrorBox error={actionError} />
      <ErrorBox error={error} />
      {loading && <div className="page-loading">Loading…</div>}
      {data && data.length === 0 && (
        <Empty>
          No task records. Records exist only for tasks submitted after the
          3.5 task-store migration; older tasks were queue-only.
        </Empty>
      )}
      {data && data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Created</th><th>Source</th><th>Operation</th><th>Connector</th>
              <th>Identity</th><th>Status</th><th>Attempts</th><th>Last error</th><th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((t) => (
              <tr key={t.taskId}>
                <td>{fmtDate(t.createdDate)}</td>
                <td>{t.sourceType}</td>
                <td>{t.operationType}</td>
                <td>{t.connectorType}</td>
                <td className="mono">{t.identityId}</td>
                <td><StatusPill value={t.status} /></td>
                <td>{t.attemptCount}</td>
                <td className="error-cell" title={t.lastError ?? ""}>{t.lastError ?? "—"}</td>
                <td className="actions">
                  {(t.status === "dead-lettered" || t.status === "cancelled") && (
                    <button className="btn" onClick={() => act(t.taskId, "retry")}>Retry</button>
                  )}
                  {(t.status === "queued" || t.status === "retry-scheduled") && (
                    <button className="btn btn-danger" onClick={() => act(t.taskId, "cancel")}>Cancel</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
