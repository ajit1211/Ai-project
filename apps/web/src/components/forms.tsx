import type { ReactNode } from "react";

import { ApiError } from "../api/client";

export function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {error && (
        <span className="field-error" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}

export function Alert({ kind, children }: { kind: "error" | "success" | "info"; children: ReactNode }) {
  return (
    <div className={`alert alert-${kind}`} role={kind === "error" ? "alert" : "status"}>
      {children}
    </div>
  );
}

/** A user-facing message for any failure, with request ID for support. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.requestId ? `${error.message} (ref ${error.requestId})` : error.message;
  }
  return "Something went wrong. Check your connection and try again.";
}

/** The token carried in the URL fragment of an email link. Cleared once read. */
export function readHashToken(): string | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  if (token) window.history.replaceState(null, "", window.location.pathname);
  return token;
}
