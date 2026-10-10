/** Settings: Profile, Security, Sessions, Organizations, Notifications. */

import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate, useSearchParams } from "react-router";

import { ApiError, api } from "../api/client";
import type { Organization, SessionInfo, User } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Alert, Field, describeError } from "../components/forms";

export function SettingsLayout() {
  return (
    <>
      <h1>Settings</h1>
      <div className="settings">
        <nav className="tabs" aria-label="Settings">
          <NavLink to="/settings/profile">Profile</NavLink>
          <NavLink to="/settings/security">Security</NavLink>
          <NavLink to="/settings/sessions">Sessions</NavLink>
          <NavLink to="/settings/organizations">Organizations</NavLink>
          <NavLink to="/settings/notifications">Notifications</NavLink>
        </nav>
        <div className="card section">
          <Outlet />
        </div>
      </div>
    </>
  );
}

function useFeedback() {
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  return {
    feedback,
    ok: (text: string) => {
      setFeedback({ kind: "success", text });
    },
    fail: (err: unknown) => {
      setFeedback({ kind: "error", text: describeError(err) });
    },
    clear: () => {
      setFeedback(null);
    },
  };
}

export function ProfileSettings() {
  const { user, reloadUser } = useAuth();
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [timezone, setTimezone] = useState(user?.profile.timezone ?? "UTC");
  const [locale, setLocale] = useState(user?.profile.locale ?? "en");
  const { feedback, ok, fail, clear } = useFeedback();

  const submit = async () => {
    clear();
    try {
      const { message } = await api<User>("/users/me/", {
        method: "PATCH",
        body: { full_name: fullName, timezone, locale },
      });
      await reloadUser();
      ok(message ?? "Profile updated.");
    } catch (err) {
      fail(err);
    }
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h2>Profile</h2>
      {feedback && <Alert kind={feedback.kind}>{feedback.text}</Alert>}
      <Field label="Email">
        <input value={user?.email ?? ""} disabled />
      </Field>
      <Field label="Full name">
        <input
          value={fullName}
          onChange={(e) => {
            setFullName(e.target.value);
          }}
        />
      </Field>
      <Field label="Time zone (IANA name, e.g. Asia/Kolkata)">
        <input
          value={timezone}
          onChange={(e) => {
            setTimezone(e.target.value);
          }}
        />
      </Field>
      <Field label="Language">
        <input
          value={locale}
          onChange={(e) => {
            setLocale(e.target.value);
          }}
        />
      </Field>
      <button type="submit" className="btn btn-primary">
        Save
      </button>
    </form>
  );
}

export function SecuritySettings() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [deactivatePassword, setDeactivatePassword] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const { feedback, ok, fail, clear } = useFeedback();

  const changePassword = async () => {
    clear();
    setError(null);
    try {
      const { message } = await api("/auth/change-password/", {
        method: "POST",
        body: { current_password: current, new_password: next },
      });
      setCurrent("");
      setNext("");
      ok(message ?? "Password changed.");
    } catch (err) {
      if (err instanceof ApiError && err.details.length) setError(err);
      else fail(err);
    }
  };

  const deactivate = async () => {
    if (!window.confirm("Deactivate your account? You will be signed out everywhere.")) return;
    try {
      await api("/users/me/deactivate/", { method: "POST", body: { password: deactivatePassword } });
      await logout();
      await navigate("/login");
    } catch (err) {
      fail(err);
    }
  };

  return (
    <>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void changePassword();
        }}
      >
        <h2>Change password</h2>
        {feedback && <Alert kind={feedback.kind}>{feedback.text}</Alert>}
        <Field label="Current password" error={error?.fieldMessage("current_password")}>
          <input
            type="password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => {
              setCurrent(e.target.value);
            }}
          />
        </Field>
        <Field label="New password" error={error?.fieldMessage("new_password")}>
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={10}
            value={next}
            onChange={(e) => {
              setNext(e.target.value);
            }}
          />
        </Field>
        <button type="submit" className="btn btn-primary">
          Change password
        </button>
        <p className="muted small">Your other devices will be signed out.</p>
      </form>
      <hr />
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void deactivate();
        }}
      >
        <h2>Deactivate account</h2>
        <Field label="Confirm your password">
          <input
            type="password"
            autoComplete="current-password"
            required
            value={deactivatePassword}
            onChange={(e) => {
              setDeactivatePassword(e.target.value);
            }}
          />
        </Field>
        <button type="submit" className="btn btn-danger">
          Deactivate my account
        </button>
      </form>
    </>
  );
}

export function SessionSettings() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const { feedback, fail, clear } = useFeedback();

  const load = () =>
    api<SessionInfo[]>("/sessions/")
      .then(({ data }) => {
        setSessions(data);
      })
      .catch(fail);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once on mount
  }, []);

  const revoke = async (session: SessionInfo) => {
    clear();
    try {
      await api(`/sessions/${session.id}/`, { method: "DELETE" });
      if (session.is_current) {
        await logout();
        await navigate("/login");
        return;
      }
      await load();
    } catch (err) {
      fail(err);
    }
  };

  const logoutAll = async () => {
    try {
      await api("/sessions/logout-all/", { method: "POST" });
      await logout();
      await navigate("/login");
    } catch (err) {
      fail(err);
    }
  };

  return (
    <>
      <h2>Active sessions</h2>
      {feedback && <Alert kind={feedback.kind}>{feedback.text}</Alert>}
      <ul className="list">
        {sessions.map((session) => (
          <li key={session.id} className="list-row">
            <div>
              <strong>{session.device}</strong>
              {session.is_current && <span className="badge badge-up">This device</span>}
              <div className="muted small">
                Last active {new Date(session.last_seen_at).toLocaleString()}
                {session.ip_address ? ` · ${session.ip_address}` : ""}
              </div>
            </div>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={() => {
                void revoke(session);
              }}
            >
              {session.is_current ? "Sign out" : "Revoke"}
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="btn btn-danger"
        onClick={() => {
          void logoutAll();
        }}
      >
        Sign out of all devices
      </button>
    </>
  );
}

export function OrganizationSettings() {
  const { organizations, activeOrganizationId, reloadOrganizations, switchOrganization } = useAuth();
  const [params] = useSearchParams();
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(params.get("create") === "1");
  const { feedback, ok, fail, clear } = useFeedback();

  const create = async () => {
    clear();
    try {
      const { data } = await api<Organization>("/organizations/", { method: "POST", body: { name } });
      await reloadOrganizations();
      await switchOrganization(data.id);
      setName("");
      setCreating(false);
      ok(`Created ${data.name}.`);
    } catch (err) {
      fail(err);
    }
  };

  const leave = async (org: Organization) => {
    if (!window.confirm(`Leave ${org.name}?`)) return;
    clear();
    try {
      await api(`/organizations/${org.id}/leave/`, { method: "POST" });
      const remaining = await reloadOrganizations();
      if (org.id === activeOrganizationId && remaining[0]) await switchOrganization(remaining[0].id);
      ok(`You left ${org.name}.`);
    } catch (err) {
      fail(err);
    }
  };

  return (
    <>
      <h2>Organizations</h2>
      {feedback && <Alert kind={feedback.kind}>{feedback.text}</Alert>}
      <ul className="list">
        {organizations.map((org) => (
          <li key={org.id} className="list-row">
            <div>
              <strong>{org.name}</strong>
              {org.id === activeOrganizationId && <span className="badge badge-up">Active</span>}
              <div className="muted small">
                {org.kind === "personal" ? "Personal workspace" : `${org.role?.name ?? ""} · `}
                {org.kind === "team" && <Link to={`/organizations/${org.id}`}>Members</Link>}
              </div>
            </div>
            {org.kind === "team" && (
              <button
                type="button"
                className="btn btn-quiet"
                onClick={() => {
                  void leave(org);
                }}
              >
                Leave
              </button>
            )}
          </li>
        ))}
      </ul>
      {creating ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          <Field label="Organization name">
            <input
              autoFocus
              required
              maxLength={120}
              value={name}
              onChange={(e) => {
                setName(e.target.value);
              }}
            />
          </Field>
          <div className="actions">
            <button type="submit" className="btn btn-primary">
              Create
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setCreating(false);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          className="btn"
          onClick={() => {
            setCreating(true);
          }}
        >
          + Create organization
        </button>
      )}
    </>
  );
}

export function NotificationSettings() {
  const { user, reloadUser } = useAuth();
  const prefs = user?.profile.preferences.notifications ?? {};
  const [invitations, setInvitations] = useState(prefs.invitations_email ?? true);
  const [updates, setUpdates] = useState(prefs.product_updates_email ?? false);
  const { feedback, ok, fail, clear } = useFeedback();

  const save = async () => {
    clear();
    try {
      await api("/users/me/", {
        method: "PATCH",
        body: {
          preferences: {
            notifications: { invitations_email: invitations, product_updates_email: updates },
          },
        },
      });
      await reloadUser();
      ok("Notification settings saved.");
    } catch (err) {
      fail(err);
    }
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h2>Email notifications</h2>
      {feedback && <Alert kind={feedback.kind}>{feedback.text}</Alert>}
      <label className="check">
        <input
          type="checkbox"
          checked={invitations}
          onChange={(e) => {
            setInvitations(e.target.checked);
          }}
        />
        Organization invitations
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={updates}
          onChange={(e) => {
            setUpdates(e.target.checked);
          }}
        />
        Product updates
      </label>
      <p className="muted small">
        Security alerts (new device sign-ins, password changes) are always sent.
      </p>
      <button type="submit" className="btn btn-primary">
        Save
      </button>
    </form>
  );
}
