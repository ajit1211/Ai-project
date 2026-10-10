import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { api } from "../api/client";
import type { InvitationPreview, Organization } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Alert, describeError, readHashToken } from "../components/forms";

export function InvitationPage() {
  const { status, user, reloadOrganizations, switchOrganization } = useAuth();
  const navigate = useNavigate();
  const [token] = useState(() => readHashToken() ?? sessionStorage.getItem("nexa:invitation"));
  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    // Keep the token across the sign-in round trip, in session storage only.
    sessionStorage.setItem("nexa:invitation", token);
    api<InvitationPreview>("/invitations/preview/", { method: "POST", body: { token }, auth: false })
      .then(({ data }) => {
        setPreview(data);
      })
      .catch((err: unknown) => {
        setError(describeError(err));
      });
  }, [token]);

  const accept = async () => {
    setBusy(true);
    try {
      const { data } = await api<Organization>("/invitations/accept/", {
        method: "POST",
        body: { token },
      });
      sessionStorage.removeItem("nexa:invitation");
      await reloadOrganizations();
      await switchOrganization(data.id);
      await navigate("/dashboard");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  const decline = async () => {
    setBusy(true);
    try {
      await api("/invitations/decline/", { method: "POST", body: { token }, auth: false });
      sessionStorage.removeItem("nexa:invitation");
      setPreview(null);
      setError("Invitation declined.");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  const wrongAccount = user && preview && user.email !== preview.email;
  return (
    <main className="auth-page">
      <div className="card auth-card">
        <h1 className="brand">NEXA</h1>
        <h2>Organization invitation</h2>
        {!token && <Alert kind="error">This link is missing its token.</Alert>}
        {error && <Alert kind="error">{error}</Alert>}
        {preview && (
          <>
            <p>
              <strong>{preview.inviter_name}</strong> invited <strong>{preview.email}</strong> to
              join <strong>{preview.organization_name}</strong> as {preview.role_name}.
            </p>
            {status === "authenticated" && !wrongAccount && (
              <div className="actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => {
                    void accept();
                  }}
                >
                  Accept
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  onClick={() => {
                    void decline();
                  }}
                >
                  Decline
                </button>
              </div>
            )}
            {status === "authenticated" && wrongAccount && (
              <Alert kind="info">
                You are signed in as {user.email}. Sign out and sign in as {preview.email} to
                accept.
              </Alert>
            )}
            {status === "anonymous" && (
              <p>
                <Link className="btn btn-primary" to="/login" state={{ from: "/invitations/accept" }}>
                  Sign in to accept
                </Link>{" "}
                <Link to="/register">or create an account</Link>
              </p>
            )}
          </>
        )}
      </div>
    </main>
  );
}
