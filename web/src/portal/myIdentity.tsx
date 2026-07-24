/**
 * "Who am I?" — linking the signed-in Entra user to an identity record.
 *
 * Approver-binding task (closes the gap flagged since 3.2): the link is now
 * SERVER-SIDE and enforced. Linking calls identity-service's
 * POST /identities/{id}/claim, which binds the caller's token oid to the
 * record (first claim wins; 409 if someone else already claimed it).
 * access-request-service resolves and enforces the binding on every
 * approval decision — localStorage here is a display cache only and grants
 * nothing.
 *
 * On load the banner first auto-resolves via
 * GET /identities/by-entra-object-id/{oid} (oid = the MSAL account's
 * localAccountId), so an already-claimed user is recognised in any
 * browser, cleared storage or not.
 *
 * Residual, documented gap: nothing verifies the human behind the token
 * corresponds to the HR record being claimed (identities carry no UPN/email
 * attribute to match). First-claim-wins bounds the damage; attribute-matched
 * auto-claim is the v-next once feeds supply a UPN.
 */
import { useEffect, useState } from "react";
import { useMsal } from "@azure/msal-react";
import { api, ApiError } from "../api/client";
import { Identity } from "../api/types";
import { ErrorBox } from "../components/bits";

const KEY = "iga.myIdentityId";
const NAME_KEY = "iga.myIdentityName";

export function getMyIdentityId(): string | null {
  return localStorage.getItem(KEY);
}

export function getMyIdentityName(): string | null {
  return localStorage.getItem(NAME_KEY);
}

export function clearMyIdentity() {
  localStorage.removeItem(KEY);
  localStorage.removeItem(NAME_KEY);
}

function cache(i: Identity) {
  localStorage.setItem(KEY, i.identityId);
  localStorage.setItem(NAME_KEY, i.displayName);
}

export function IdentityLinkBanner({ onLinked }: { onLinked: () => void }) {
  const { accounts } = useMsal();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Identity[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(true);

  // Auto-resolve an existing server-side claim before asking the user.
  useEffect(() => {
    let cancelled = false;
    const oid = accounts[0]?.localAccountId;
    if (!oid) { setResolving(false); return; }
    api.get<Identity>(`/api/identity/identities/by-entra-object-id/${oid}`)
      .then((i) => { if (!cancelled) { cache(i); onLinked(); } })
      .catch(() => { /* 404 = not claimed yet — show the picker */ })
      .finally(() => { if (!cancelled) setResolving(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      setResults(await api.get<Identity[]>(`/api/identity/identities?q=${encodeURIComponent(q)}&limit=20`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function link(i: Identity) {
    setError(null);
    try {
      const claimed = await api.post<Identity>(`/api/identity/identities/${i.identityId}/claim`);
      cache(claimed);
      onLinked();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(`Cannot link: ${err.detail}`);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  }

  if (resolving) return <div className="page-loading">Checking for an existing identity link…</div>;

  return (
    <div className="link-banner">
      <h3>Link your identity record</h3>
      <p>
        Search for your HR identity record and claim it. The claim is
        server-enforced and permanent (first claim wins): approval decisions
        are only accepted from the account that claimed the approver's record.
      </p>
      <form className="filter-bar" onSubmit={search}>
        <input placeholder="Your name…" value={q} onChange={(e) => setQ(e.target.value)} required />
        <button className="btn btn-primary" type="submit">Search</button>
      </form>
      <ErrorBox error={error} />
      {results && results.length === 0 && <p>No matches.</p>}
      {results && results.length > 0 && (
        <ul className="picker-list">
          {results.map((i) => (
            <li key={i.identityId}>
              <button className="btn" onClick={() => link(i)} disabled={!!i.entraObjectId}>
                {i.entraObjectId ? "Claimed" : "Claim"}
              </button>
              <span>{i.displayName}</span>
              <span className="mono">{i.correlationKey}</span>
              <span>{i.department ?? ""}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
