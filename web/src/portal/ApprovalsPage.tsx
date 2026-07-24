/** Portal — my approvals queue (REQ-UI-032): steps awaiting the linked
 * identity's decision, from access-request-service's approver-side query.
 * Since the approver-binding task, decisions are server-enforced: the
 * service resolves the caller's token oid to their claimed identity and
 * rejects (403) anyone who isn't the step's assigned approver. The client
 * sends no "who I am" — the server derives it from the token. */
import { useState } from "react";
import { api } from "../api/client";
import { ApprovalStepView } from "../api/types";
import { Empty, ErrorBox, fmtDate, useLoad } from "../components/bits";
import { IdentityLinkBanner, getMyIdentityId, getMyIdentityName } from "./myIdentity";

export default function ApprovalsPage() {
  const [identityId, setIdentityId] = useState(getMyIdentityId());
  const [actionError, setActionError] = useState<string | null>(null);
  const { data, error, loading, refresh } = useLoad<ApprovalStepView[]>(
    () =>
      identityId
        ? api.get(`/api/requests/approval-steps?approverIdentityId=${identityId}&status=pending`)
        : Promise.resolve([]),
    [identityId],
  );

  if (!identityId) return <IdentityLinkBanner onLinked={() => setIdentityId(getMyIdentityId())} />;

  async function decide(step: ApprovalStepView, decision: "approve" | "reject") {
    setActionError(null);
    try {
      await api.post(
        `/api/requests/requests/${step.requestId}/line-items/${step.lineItemId}/approval-steps/${step.id}/decide`,
        { decision },
      );
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section>
      <h2>My approvals</h2>
      <p className="linked-as">
        Approving as <strong>{getMyIdentityName()}</strong>{" "}
        <button className="btn btn-ghost" onClick={refresh}>Refresh</button>
      </p>
      <ErrorBox error={actionError} />
      <ErrorBox error={error} />
      {loading && <div className="page-loading">Loading…</div>}
      {data && data.length === 0 && <Empty>Nothing waiting for your decision.</Empty>}
      {data && data.length > 0 && (
        <div className="card-list">
          {data.map((s) => (
            <div className="card" key={s.id}>
              <div className="card-head">
                <strong>{s.stepType} approval</strong>
                <span className="muted">{fmtDate(s.createdDate)}</span>
              </div>
              <p className="mono">{s.connectorType} · {s.entitlementRef}</p>
              <p>
                Requester: <span className="mono">{s.requesterIdentityId}</span>
              </p>
              {s.justification && <p>Justification: {s.justification}</p>}
              {s.actionable ? (
                <div className="actions">
                  <button className="btn btn-primary" onClick={() => decide(s, "approve")}>Approve</button>
                  <button className="btn btn-danger" onClick={() => decide(s, "reject")}>Reject</button>
                </div>
              ) : (
                <p className="muted">Waiting on an earlier step in this chain.</p>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
