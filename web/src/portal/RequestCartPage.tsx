/** Portal — request cart (REQ-UI-031): build a multi-line-item access
 * request against the requestable-entitlement space and submit it to
 * access-request-service. "Requestable entitlements" = entitlements
 * defined on active rbac-service roles (the only entitlement catalogue
 * the platform has — same registry-dual-purposing precedent as 3.1/3.2,
 * flagged in PHASES.md), plus a free-form entry for ad-hoc refs.
 */
import { useState } from "react";
import { api } from "../api/client";
import { AccessRequest, RoleAssignmentView, SourceSystem } from "../api/types";
import { Empty, ErrorBox, useLoad } from "../components/bits";
import { IdentityLinkBanner, getMyIdentityId, getMyIdentityName } from "./myIdentity";

interface RoleWithEntitlements {
  id: string;
  name: string;
  entitlements?: { targetSystemInstanceId: string; connectorType: string; entitlementRef: string }[];
}

interface CartItem {
  targetSystemInstanceId: string;
  connectorType: string;
  entitlementRef: string;
  justification: string;
}

export default function RequestCartPage() {
  const [identityId, setIdentityId] = useState(getMyIdentityId());
  const roles = useLoad<RoleWithEntitlements[]>(async () => {
    const list = await api.get<RoleWithEntitlements[]>("/api/rbac/roles?status=active&limit=100");
    return Promise.all(
      list.map(async (r) => ({
        ...r,
        entitlements: await api.get<RoleAssignmentView["entitlements"]>(`/api/rbac/roles/${r.id}/entitlements`),
      })),
    );
  });
  const systems = useLoad<SourceSystem[]>(() => api.get("/api/source/source-systems?limit=100"));

  const [cart, setCart] = useState<CartItem[]>([]);
  const [freeSystem, setFreeSystem] = useState("");
  const [freeRef, setFreeRef] = useState("");
  const [submitState, setSubmitState] = useState<{ error?: string; result?: AccessRequest }>({});

  if (!identityId) return <IdentityLinkBanner onLinked={() => setIdentityId(getMyIdentityId())} />;

  function addItem(item: Omit<CartItem, "justification">) {
    if (cart.some((c) => c.entitlementRef === item.entitlementRef && c.targetSystemInstanceId === item.targetSystemInstanceId)) return;
    setCart([...cart, { ...item, justification: "" }]);
  }

  function addFreeForm(e: React.FormEvent) {
    e.preventDefault();
    const sys = systems.data?.find((s) => s.id === freeSystem);
    if (!sys) return;
    addItem({ targetSystemInstanceId: sys.id, connectorType: sys.connectorType, entitlementRef: freeRef });
    setFreeRef("");
  }

  async function submit() {
    setSubmitState({});
    try {
      const result = await api.post<AccessRequest>("/api/requests/requests", {
        requesterIdentityId: identityId,
        lineItems: cart.map(({ justification, ...rest }) => ({
          ...rest,
          justification: justification || null,
        })),
      });
      setCart([]);
      setSubmitState({ result });
    } catch (e) {
      setSubmitState({ error: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <section>
      <h2>Request access</h2>
      <p className="linked-as">Requesting as <strong>{getMyIdentityName()}</strong></p>

      <h3>Catalogue (entitlements on active roles)</h3>
      <ErrorBox error={roles.error} />
      {roles.data && roles.data.every((r) => !r.entitlements?.length) && (
        <Empty>No requestable entitlements defined yet.</Empty>
      )}
      <div className="card-list">
        {roles.data?.filter((r) => r.entitlements?.length).map((r) => (
          <div className="card" key={r.id}>
            <strong>{r.name}</strong>
            <ul className="ent-list">
              {r.entitlements!.map((e, i) => (
                <li key={i}>
                  <button className="btn" onClick={() => addItem(e)}>Add</button>{" "}
                  <span className="mono">{e.connectorType} · {e.entitlementRef}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <h3>Ad-hoc entitlement</h3>
      <form className="filter-bar" onSubmit={addFreeForm}>
        <select value={freeSystem} onChange={(e) => setFreeSystem(e.target.value)} required>
          <option value="">Target system…</option>
          {systems.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <input
          placeholder="Entitlement ref (e.g. CN=Group,DC=corp)"
          value={freeRef}
          onChange={(e) => setFreeRef(e.target.value)}
          required
        />
        <button className="btn" type="submit">Add to cart</button>
      </form>

      <h3>Cart ({cart.length})</h3>
      {cart.length === 0 && <Empty>Nothing in the cart yet.</Empty>}
      {cart.map((c, idx) => (
        <div className="cart-row" key={idx}>
          <span className="mono">{c.connectorType} · {c.entitlementRef}</span>
          <input
            placeholder="Justification (optional)"
            value={c.justification}
            onChange={(e) => setCart(cart.map((x, i) => (i === idx ? { ...x, justification: e.target.value } : x)))}
          />
          <button className="btn btn-ghost" onClick={() => setCart(cart.filter((_, i) => i !== idx))}>
            Remove
          </button>
        </div>
      ))}
      {cart.length > 0 && (
        <button className="btn btn-primary" onClick={submit}>Submit request</button>
      )}
      <ErrorBox error={submitState.error ?? null} />
      {submitState.result && (
        <div className="success-box">
          Request {submitState.result.id} submitted — status {submitState.result.status}.
          {submitState.result.lineItems.map((li) => (
            <div key={li.id} className="mono">
              {li.entitlementRef}: {li.status}
              {li.approvalSteps.filter((s) => s.status === "pending").length > 0 &&
                ` (awaiting ${li.approvalSteps.filter((s) => s.status === "pending").map((s) => s.stepType).join(" → ")})`}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
