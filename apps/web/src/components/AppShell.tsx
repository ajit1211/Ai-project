import { Link, NavLink, Outlet, useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { OrgSwitcher } from "./OrgSwitcher";

export function AppShell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const signOut = () => {
    void logout().then(() => {
      void navigate("/login");
    });
  };

  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/dashboard" className="brand">
          NEXA
        </Link>
        <OrgSwitcher />
        <nav className="topnav" aria-label="Main">
          <NavLink to="/dashboard">Dashboard</NavLink>
          <NavLink to="/chat">Chat</NavLink>
          <NavLink to="/documents">Documents</NavLink>
          <NavLink to="/memories">Memories</NavLink>
          <NavLink to="/agents">Agents</NavLink>
          <NavLink to="/playground">Playground</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <div className="user-menu">
          <span className="muted">{user?.email}</span>
          <button type="button" className="btn btn-quiet" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      <main className="page">
        <Outlet />
      </main>
    </div>
  );
}
