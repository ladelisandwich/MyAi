import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { ErrorAlert } from "../components/ui";

export default function LoginPage({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/auth/login", { username, password });
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
        <div className="brand"><img src="/icons/icon.svg" alt="" />HomeMesh AI</div>
        <p className="muted small" style={{ textAlign: "center" }}>Sign in to your private assistant.</p>
        <label className="field">Username
          <input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus />
        </label>
        <label className="field">Password
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <ErrorAlert error={error} />
        <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="faint tiny" style={{ textAlign: "center", margin: 0 }}>Forgot the password? Run <code>homemesh user reset-password</code> on the coordinator computer.</p>
      </form>
    </div>
  );
}
