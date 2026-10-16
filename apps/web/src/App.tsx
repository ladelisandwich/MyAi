import { useCallback, useEffect, useState } from "react";
import { api, onUnauthorized } from "./api/client";
import type { AuthStatus, User } from "./api/types";
import { ErrorAlert, Spinner, ToastProvider } from "./components/ui";
import { StoredHubUpdateNotice } from "./components/UpdatePanel";
import { EventsProvider, useEvents } from "./lib/events";
import { navigate, useRoute } from "./lib/router";
import ChatPage from "./pages/Chat";
import DashboardPage from "./pages/Dashboard";
import JobsPage from "./pages/Jobs";
import KnowledgePage from "./pages/Knowledge";
import LoginPage from "./pages/Login";
import MemoryPage from "./pages/Memory";
import ModelsPage from "./pages/Models";
import NodesPage from "./pages/Nodes";
import SettingsPage from "./pages/Settings";
import SetupPage from "./pages/Setup";

const NAV = [
  { page: "chat", label: "Chat", icon: "💬" },
  { page: "dashboard", label: "Dashboard", icon: "📊" },
  { page: "nodes", label: "Computers", icon: "🖥️" },
  { page: "models", label: "Models", icon: "🧠" },
  { page: "memory", label: "Memory", icon: "📌" },
  { page: "knowledge", label: "Knowledge", icon: "📚" },
  { page: "jobs", label: "Jobs & Activity", icon: "⚙️" },
  { page: "settings", label: "Settings", icon: "🔧" },
];

function Shell({ user, onLogout }: { user: User; onLogout: () => void }) {
  const route = useRoute();
  const { connected } = useEvents();
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => setMenuOpen(false), [route.page, route.rest.join("/")]);
  const current = NAV.find((n) => n.page === route.page) ?? NAV[0];
  let page;
  switch (route.page) {
    case "dashboard": page = <DashboardPage />; break;
    case "nodes": page = <NodesPage />; break;
    case "models": page = <ModelsPage />; break;
    case "memory": page = <MemoryPage />; break;
    case "knowledge": page = <KnowledgePage />; break;
    case "jobs": page = <JobsPage route={route} />; break;
    case "settings": page = <SettingsPage user={user} />; break;
    default: page = <ChatPage route={route} />;
  }
  return (
    <div className="app">
      <div className="topbar">
        <button className="btn ghost icon-only" aria-label="Open menu" onClick={() => setMenuOpen(true)}>☰</button>
        <span className="title">{current.label}</span>
        <span className={`dot ${connected ? "ok" : "warn"}`} title={connected ? "Live updates connected" : "Reconnecting…"} />
      </div>
      {menuOpen && <div className="scrim" onClick={() => setMenuOpen(false)} />}
      <aside className={`sidebar ${menuOpen ? "open" : ""}`} aria-label="Main navigation">
        <div className="brand"><img src="/icons/icon.svg" alt="" />HomeMesh AI</div>
        <nav className="nav">
          {NAV.map((n) => (
            <a key={n.page} href={`#/${n.page}`} className={route.page === n.page || (n.page === "chat" && !NAV.some((x) => x.page === route.page)) ? "active" : ""}>
              <span className="icon" aria-hidden="true">{n.icon}</span>{n.label}
            </a>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="row">
            <span className="truncate">{user.display_name || user.username}</span>
            <button className="btn ghost sm" onClick={onLogout}>Sign out</button>
          </div>
          <div className="row small" style={{ marginTop: 6 }}>
            <span className={`dot ${connected ? "ok" : "warn"}`} />
            <span>{connected ? "Live updates on" : "Reconnecting…"}</span>
          </div>
        </div>
      </aside>
      <main className="main">{page}</main>
    </div>
  );
}

export default function App() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.get<AuthStatus>("/api/v1/auth/status"));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return onUnauthorized(() => void refresh());
  }, [refresh]);

  const logout = async () => {
    try { await api.post("/api/v1/auth/logout"); } catch { /* ignore */ }
    navigate("/chat");
    await refresh();
  };

  let body;
  if (error && !status) {
    body = (
      <div className="auth-wrap"><div className="card auth-card stack">
        <div className="brand"><img src="/icons/icon.svg" alt="" />HomeMesh AI</div>
        <ErrorAlert error={error} />
        {/* The hub may be restarting for its own update: show the help saved before it went down. */}
        <StoredHubUpdateNotice />
        <button className="btn primary" onClick={() => void refresh()}>Try again</button>
      </div></div>
    );
  } else if (!status) {
    body = <div className="auth-wrap"><Spinner label="Connecting to HomeMesh…" /></div>;
  } else if (status.setup_required) {
    body = <SetupPage onDone={refresh} />;
  } else if (!status.authenticated || !status.user) {
    body = <LoginPage onDone={refresh} />;
  } else {
    body = <EventsProvider><Shell user={status.user} onLogout={logout} /></EventsProvider>;
  }
  return <ToastProvider>{body}</ToastProvider>;
}
