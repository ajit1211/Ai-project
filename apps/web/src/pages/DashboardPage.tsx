import { Link } from "react-router";

import { useAuth } from "../auth/AuthContext";

export function DashboardPage() {
  const { user, organizations, activeOrganizationId } = useAuth();
  const active = organizations.find((org) => org.id === activeOrganizationId);

  return (
    <>
      <h1>Welcome{user?.full_name ? `, ${user.full_name}` : ""}</h1>
      <div className="grid">
        <section className="card section">
          <h2>Active organization</h2>
          {active ? (
            <>
              <p className="big">{active.name}</p>
              <p className="muted">
                {active.kind === "personal" ? "Personal workspace" : `Your role: ${active.role?.name ?? ""}`}
              </p>
              {active.kind === "team" && <Link to={`/organizations/${active.id}`}>Manage members</Link>}
            </>
          ) : (
            <p className="muted">Pick an organization from the switcher above.</p>
          )}
        </section>
        <section className="card section">
          <h2>What's next</h2>
          <p>
            <Link to="/playground">Try the AI playground</Link>
          </p>
          <p className="muted">Set up your organization and invite your team.</p>
          <Link to="/settings/organizations">Organizations</Link>
        </section>
      </div>
    </>
  );
}
