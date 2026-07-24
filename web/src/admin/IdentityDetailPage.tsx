/** Admin console — identity detail with the append-only history view
 * (REQ-UI-021; history from identity-service's REQ-COR-ID-004 log). */
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { HistoryEvent, Identity } from "../api/types";
import { ErrorBox, StatusPill, fmtDate, useLoad } from "../components/bits";

const CORE_FIELDS = [
  "displayName", "correlationKey", "status", "department", "jobTitle",
  "managerIdentityId", "startDate", "terminationDate", "employeeId",
  "givenName", "familyName", "sourceSystemId", "createdDate", "lastModifiedDate",
];

function changedKeys(ev: HistoryEvent): string[] {
  if (!ev.before || !ev.after) return [];
  const keys = new Set([...Object.keys(ev.before), ...Object.keys(ev.after)]);
  return [...keys].filter(
    (k) => JSON.stringify(ev.before?.[k]) !== JSON.stringify(ev.after?.[k]) && k !== "lastModifiedDate",
  );
}

export default function IdentityDetailPage() {
  const { id } = useParams();
  const identity = useLoad<Identity>(() => api.get(`/api/identity/identities/${id}`), [id]);
  const history = useLoad<HistoryEvent[]>(() => api.get(`/api/identity/identities/${id}/history`), [id]);

  return (
    <section>
      <ErrorBox error={identity.error} />
      {identity.data && (
        <>
          <h2>
            {identity.data.displayName} <StatusPill value={identity.data.status} />
          </h2>
          <dl className="detail-grid">
            {CORE_FIELDS.map((f) => (
              <div key={f}>
                <dt>{f}</dt>
                <dd className="mono">{String(identity.data?.[f] ?? "—")}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
      <h3>History</h3>
      <ErrorBox error={history.error} />
      {history.data && (
        <table className="data-table">
          <thead>
            <tr><th>When</th><th>Event</th><th>Actor</th><th>Changed</th></tr>
          </thead>
          <tbody>
            {history.data.map((ev) => (
              <tr key={ev.id}>
                <td>{fmtDate(ev.timestamp)}</td>
                <td>{ev.eventType}</td>
                <td>{ev.actor}</td>
                <td className="mono">
                  {changedKeys(ev).map((k) => (
                    <div key={k}>
                      {k}: {JSON.stringify(ev.before?.[k] ?? null)} → {JSON.stringify(ev.after?.[k] ?? null)}
                    </div>
                  ))}
                  {ev.eventType === "IdentityCreated" && "(record created)"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
