import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { ErrorAlert } from "../components/ui";

export default function SetupPage({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== password2) {
      setError(new Error("The passwords do not match."));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/auth/setup", { setup_token: token.trim(), username, password, display_name: displayName });
      onDone();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth-wrap">
      <form className="card auth-card stack" onSubmit={submit}>
        <div className="brand"><img src="/icons/icon.svg" alt="" />Welcome to HomeMesh</div>
        <p className="muted small">Create the owner account. Only the owner can manage computers, models and data.</p>
        <label className="field">Setup token
          <input value={token} onChange={(e) => setToken(e.target.value)} required autoComplete="off" spellCheck={false} />
          <span className="hint">Shown in the coordinator's console when it starts, and stored in <code>data/secrets/setup-token.txt</code>. This prevents anyone else on your network from claiming this installation.</span>
        </label>
        <label className="field">Username
          <input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required pattern="[A-Za-z0-9._\-]{1,64}" />
        </label>
        <label className="field">Display name <span className="hint">optional</span>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </label>
        <label className="field">Password <span className="hint">at least 10 characters</span>
          <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={10} />
        </label>
        <label className="field">Repeat password
          <input type="password" autoComplete="new-password" value={password2} onChange={(e) => setPassword2(e.target.value)} required minLength={10} />
        </label>
        <ErrorAlert error={error} />
        <button className="btn primary" disabled={busy}>{busy ? "Creating…" : "Create owner account"}</button>
      </form>
    </div>
  );
}
