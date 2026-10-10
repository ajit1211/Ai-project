/** Public pages: sign in, register, verify email, forgot and reset password. */

import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { ApiError, api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Alert, Field, describeError, readHashToken } from "../components/forms";

function AuthCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="auth-page">
      <div className="card auth-card">
        <h1 className="brand">NEXA</h1>
        <h2>{title}</h2>
        {children}
      </div>
    </main>
  );
}

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ApiError | string | null>(null);
  const [busy, setBusy] = useState(false);
  const from = (location.state as { from?: string } | null)?.from ?? "/dashboard";

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      await navigate(from, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err : describeError(err));
    } finally {
      setBusy(false);
    }
  };

  const unverified = error instanceof ApiError && error.code === "EMAIL_NOT_VERIFIED";
  return (
    <AuthCard title="Sign in">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {error && (
          <Alert kind="error">
            {typeof error === "string" ? error : error.message}
            {unverified && (
              <>
                {" "}
                <Link to="/verify-email">Resend the verification email.</Link>
              </>
            )}
          </Alert>
        )}
        <Field label="Email">
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
            }}
          />
        </Field>
        <Field label="Password">
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        </Field>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="muted">
        <Link to="/forgot-password">Forgot your password?</Link> · New here?{" "}
        <Link to="/register">Create an account</Link>
      </p>
    </AuthCard>
  );
}

export function RegisterPage() {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ApiError | string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const { message } = await api("/auth/register/", {
        method: "POST",
        body: { email, password, full_name: fullName },
        auth: false,
      });
      setDone(message ?? "Check your email to verify your account.");
    } catch (err) {
      setError(err instanceof ApiError ? err : describeError(err));
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <AuthCard title="Almost there">
        <Alert kind="success">{done}</Alert>
        <p className="muted">
          Didn't get it? <Link to="/verify-email">Resend the verification email.</Link>
        </p>
      </AuthCard>
    );
  }
  const field = (name: string) => (error instanceof ApiError ? error.fieldMessage(name) : undefined);
  return (
    <AuthCard title="Create your account">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {error && !(error instanceof ApiError && error.details.length) && (
          <Alert kind="error">{typeof error === "string" ? error : error.message}</Alert>
        )}
        <Field label="Full name" error={field("full_name")}>
          <input
            autoComplete="name"
            value={fullName}
            onChange={(e) => {
              setFullName(e.target.value);
            }}
          />
        </Field>
        <Field label="Email" error={field("email")}>
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
            }}
          />
        </Field>
        <Field label="Password (at least 10 characters)" error={field("password")}>
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={10}
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        </Field>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Creating…" : "Create account"}
        </button>
      </form>
      <p className="muted">
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </AuthCard>
  );
}

export function VerifyEmailPage() {
  const [token] = useState(() => readHashToken());
  const [state, setState] = useState<"verifying" | "verified" | "failed" | "resend">(
    token ? "verifying" : "resend",
  );
  const [message, setMessage] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    api("/auth/verify-email/", { method: "POST", body: { token }, auth: false })
      .then(({ message: text }) => {
        setMessage(text ?? "Email verified.");
        setState("verified");
      })
      .catch((err: unknown) => {
        setMessage(describeError(err));
        setState("failed");
      });
  }, [token]);

  const resend = async () => {
    setBusy(true);
    try {
      const { message: text } = await api("/auth/resend-verification/", {
        method: "POST",
        body: { email },
        auth: false,
      });
      setMessage(text ?? "If that account needs verifying, we have sent a new link.");
    } catch (err) {
      setMessage(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthCard title="Verify your email">
      {state === "verifying" && <p className="muted">Verifying…</p>}
      {state === "verified" && (
        <>
          <Alert kind="success">{message}</Alert>
          <Link className="btn btn-primary" to="/login">
            Sign in
          </Link>
        </>
      )}
      {(state === "failed" || state === "resend") && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void resend();
          }}
        >
          {message && <Alert kind={state === "failed" ? "error" : "info"}>{message}</Alert>}
          <p className="muted">Enter your email and we will send a new verification link.</p>
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
          <button type="submit" className="btn btn-primary" disabled={busy}>
            Send verification email
          </button>
        </form>
      )}
    </AuthCard>
  );
}

export function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const { message: text } = await api("/auth/forgot-password/", {
        method: "POST",
        body: { email },
        auth: false,
      });
      setMessage(text ?? "If an account exists for that email, we have sent a reset link.");
    } catch (err) {
      setMessage(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthCard title="Reset your password">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {message && <Alert kind="info">{message}</Alert>}
        <Field label="Email">
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
            }}
          />
        </Field>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          Send reset link
        </button>
      </form>
      <p className="muted">
        <Link to="/login">Back to sign in</Link>
      </p>
    </AuthCard>
  );
}

export function ResetPasswordPage() {
  const [token] = useState(() => readHashToken());
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ApiError | string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const { message } = await api("/auth/reset-password/", {
        method: "POST",
        body: { token, password },
        auth: false,
      });
      setDone(message ?? "Password updated.");
    } catch (err) {
      setError(err instanceof ApiError ? err : describeError(err));
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <AuthCard title="Choose a new password">
        <Alert kind="error">This link is missing its token. Request a new one.</Alert>
        <Link to="/forgot-password">Request a reset link</Link>
      </AuthCard>
    );
  }
  if (done) {
    return (
      <AuthCard title="Password updated">
        <Alert kind="success">{done}</Alert>
        <Link className="btn btn-primary" to="/login">
          Sign in
        </Link>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Choose a new password">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {error && typeof error === "string" && <Alert kind="error">{error}</Alert>}
        {error instanceof ApiError && !error.fieldMessage("password") && (
          <Alert kind="error">{error.message}</Alert>
        )}
        <Field
          label="New password"
          error={error instanceof ApiError ? error.fieldMessage("password") : undefined}
        >
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={10}
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        </Field>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          Update password
        </button>
      </form>
    </AuthCard>
  );
}
