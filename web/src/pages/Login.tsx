// Sign-in against the development IdP. The demo users are the ones in backend/devidp/users.json.
import { useState } from "react";
import type { FormEvent } from "react";
import { errorMessage, signIn } from "../api";
import type { User } from "../types";

const DEMO_USERS = [
  ["cm-asha", "Asha Verma (cardmember)"],
  ["cm-ravi", "Ravi Menon (cardmember)"],
  ["mer-acme", "Acme Electronics staff (merchant)"],
  ["mer-skyline", "Skyline Travels staff (merchant)"],
  ["mer-pageturner", "PageTurner Books staff (merchant)"],
  ["rev-neha", "Neha (reviewer)"],
  ["aud-kabir", "Kabir (auditor)"],
];

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [username, setUsername] = useState("cm-asha");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await signIn(username, password));
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <h1>ApexResolve</h1>
      <p className="disclaimer">
        Student prototype. Not affiliated with or endorsed by American Express. Synthetic data only.
      </p>
      <form onSubmit={submit} className="card">
        <label>
          Demo user
          <select aria-label="Demo user" value={username} onChange={(e) => setUsername(e.target.value)}>
            {DEMO_USERS.map(([id, label]) => (
              <option key={id} value={id}>{label}</option>
            ))}
          </select>
        </label>
        <label>
          Demo password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off" />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy || password.length === 0}>Sign in</button>
      </form>
    </main>
  );
}
