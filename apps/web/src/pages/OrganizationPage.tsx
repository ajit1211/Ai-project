/** Members and invitations of one organization, and (for those who may manage them) its AI
 * tools. The server decides what is allowed; the page only hides controls the current role
 * cannot use. */

import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router";

import { api } from "../api/client";
import type { Invitation, Member, OrganizationDetail, Role } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { AiToolsSettings } from "../components/AiToolsSettings";
import { Alert, Field, describeError } from "../components/forms";

export function OrganizationPage() {
  const { organizationId = "" } = useParams();
  const { activeOrganizationId } = useAuth();
  const [org, setOrg] = useState<OrganizationDetail | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [email, setEmail] = useState("");
  const [roleId, setRoleId] = useState("");
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const can = (permission: string) => org?.permissions.includes(permission) ?? false;

  const load = useCallback(
    () =>
      api<OrganizationDetail>(`/organizations/${organizationId}/`).then(({ data: detail }) => {
        setOrg(detail);
        const requests: Promise<void>[] = [];
        if (detail.permissions.includes("members.view")) {
          requests.push(
            api<Member[]>(`/organizations/${organizationId}/members/`).then(({ data }) => {
              setMembers(data);
            }),
          );
        }
        if (detail.permissions.includes("members.invite")) {
          requests.push(
            api<Invitation[]>(`/organizations/${organizationId}/invitations/`).then(({ data }) => {
              setInvitations(data);
            }),
          );
        }
        if (detail.permissions.includes("roles.view")) {
          requests.push(
            api<Role[]>(`/organizations/${organizationId}/roles/`).then(({ data }) => {
              setRoles(data);
              setRoleId((current) => current || (data.find((r) => r.key === "member")?.id ?? ""));
            }),
          );
        }
        return Promise.all(requests).then(() => undefined);
      }),
    [organizationId],
  );

  useEffect(() => {
    load().catch((err: unknown) => {
      setFeedback({ kind: "error", text: describeError(err) });
    });
  }, [load]);

  const run = async (action: () => Promise<unknown>, success: string) => {
    setFeedback(null);
    try {
      await action();
      await load();
      setFeedback({ kind: "success", text: success });
    } catch (err) {
      setFeedback({ kind: "error", text: describeError(err) });
    }
  };

  const invite = () =>
    run(async () => {
      await api(`/organizations/${organizationId}/invitations/`, {
        method: "POST",
        body: { email, role_id: roleId },
      });
      setEmail("");
    }, "Invitation sent.");

  if (!org) return feedback ? <Alert kind="error">{feedback.text}</Alert> : <p className="muted">Loading…</p>;

  return (
    <>
      <h1>{org.name}</h1>
      <p className="muted">Your role: {org.role?.name}</p>
      {feedback && <Alert kind={feedback.kind}>{feedback.text}</Alert>}

      {can("members.view") && (
        <section className="card section">
          <h2>Members</h2>
          <table className="table">
            <thead>
              <tr>
                <th>Member</th>
                <th>Role</th>
                <th>Status</th>
                {can("members.update") && <th />}
              </tr>
            </thead>
            <tbody>
              {members.map((member) => (
                <tr key={member.id}>
                  <td>
                    {member.user.full_name || member.user.email}
                    <div className="muted small">{member.user.email}</div>
                  </td>
                  <td>
                    {can("members.update") ? (
                      <select
                        value={member.role.id}
                        aria-label={`Role of ${member.user.email}`}
                        onChange={(e) => {
                          void run(
                            () =>
                              api(`/organizations/${organizationId}/members/${member.id}/`, {
                                method: "PATCH",
                                body: { role_id: e.target.value },
                              }),
                            "Role updated.",
                          );
                        }}
                      >
                        {roles.map((role) => (
                          <option key={role.id} value={role.id}>
                            {role.name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      member.role.name
                    )}
                  </td>
                  <td>{member.status}</td>
                  {can("members.update") && (
                    <td className="actions">
                      <button
                        type="button"
                        className="btn btn-quiet"
                        onClick={() => {
                          void run(
                            () =>
                              api(`/organizations/${organizationId}/members/${member.id}/`, {
                                method: "PATCH",
                                body: { status: member.status === "active" ? "suspended" : "active" },
                              }),
                            member.status === "active" ? "Member suspended." : "Member reactivated.",
                          );
                        }}
                      >
                        {member.status === "active" ? "Suspend" : "Reactivate"}
                      </button>
                      {can("members.remove") && (
                        <button
                          type="button"
                          className="btn btn-quiet"
                          onClick={() => {
                            if (!window.confirm(`Remove ${member.user.email}?`)) return;
                            void run(
                              () =>
                                api(`/organizations/${organizationId}/members/${member.id}/`, {
                                  method: "DELETE",
                                }),
                              "Member removed.",
                            );
                          }}
                        >
                          Remove
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {can("members.invite") && (
        <section className="card section">
          <h2>Invite someone</h2>
          <form
            className="inline-form"
            onSubmit={(event) => {
              event.preventDefault();
              void invite();
            }}
          >
            <Field label="Email">
              <input
                type="email"
                required
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                }}
              />
            </Field>
            <Field label="Role">
              <select
                value={roleId}
                onChange={(e) => {
                  setRoleId(e.target.value);
                }}
              >
                {roles.map((role) => (
                  <option key={role.id} value={role.id}>
                    {role.name}
                  </option>
                ))}
              </select>
            </Field>
            <button type="submit" className="btn btn-primary">
              Send invitation
            </button>
          </form>
          {invitations.length > 0 && (
            <>
              <h3>Pending invitations</h3>
              <ul className="list">
                {invitations.map((invitation) => (
                  <li key={invitation.id} className="list-row">
                    <div>
                      <strong>{invitation.email}</strong>
                      <div className="muted small">
                        {invitation.role.name} · expires{" "}
                        {new Date(invitation.expires_at).toLocaleDateString()}
                      </div>
                    </div>
                    <div className="actions">
                      <button
                        type="button"
                        className="btn btn-quiet"
                        onClick={() => {
                          void run(
                            () =>
                              api(`/organizations/${organizationId}/invitations/${invitation.id}/`, {
                                method: "POST",
                              }),
                            "Invitation sent again.",
                          );
                        }}
                      >
                        Resend
                      </button>
                      <button
                        type="button"
                        className="btn btn-quiet"
                        onClick={() => {
                          void run(
                            () =>
                              api(`/organizations/${organizationId}/invitations/${invitation.id}/`, {
                                method: "DELETE",
                              }),
                            "Invitation revoked.",
                          );
                        }}
                      >
                        Revoke
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      {/* The tool endpoints act on the active organization (the one the access token names). */}
      {can("ai.tool.manage") &&
        (organizationId === activeOrganizationId ? (
          <AiToolsSettings />
        ) : (
          <section className="card section">
            <h2>AI tools</h2>
            <p className="muted">Switch to this organization to manage the tools its AI may use.</p>
          </section>
        ))}
    </>
  );
}
