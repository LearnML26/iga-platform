/**
 * "Who am I?" — linking the signed-in Entra user to an identity record.
 *
 * DOCUMENTED GAP (flagged in roadmap/PHASES.md, not hidden): identity
 * records are HR-feed-sourced and carry no email/UPN attribute, and no
 * service maps an Entra user to an identity record. Until such a mapping
 * exists (e.g. a upn attribute mapped from a feed + a lookup endpoint),
 * the portal asks the user to find and link their own identity record
 * once (stored in localStorage). The link is a UI convenience only — it
 * grants nothing: every API call is authorised purely by the token's app
 * roles, exactly as before.
 */
import { useState } from "react";
import { api } from "../api/client";
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

export function IdentityLinkBanner({ onLinked }: { onLinked: () => void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Identity[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      setResults(await api.get<Identity[]>(`/api/identity/identities?q=${encodeURIComponent(q)}&limit=20`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function link(i: Identity) {
    localStorage.setItem(KEY, i.identityId);
    localStorage.setItem(NAME_KEY, i.displayName);
    onLinked();
  }

  return (
    <div className="link-banner">
      <h3>Link your identity record</h3>
      <p>
        The platform doesn't yet map your Entra sign-in to an HR identity
        record automatically (no email/UPN attribute exists on identities — a
        known gap). Search for your record once to link it in this browser.
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
              <button className="btn" onClick={() => link(i)}>Link</button>
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
