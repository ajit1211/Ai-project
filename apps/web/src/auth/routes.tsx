import { Navigate, Outlet, useLocation } from "react-router";

import { useAuth } from "./AuthContext";

/** Renders its child routes only for signed-in users; everyone else goes to /login. */
export function ProtectedRoute() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <p className="muted center">Loading…</p>;
  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <Outlet />;
}

/** Login and registration make no sense for a signed-in user: send them to the dashboard. */
export function PublicOnlyRoute() {
  const { status } = useAuth();
  if (status === "loading") return <p className="muted center">Loading…</p>;
  if (status === "authenticated") return <Navigate to="/dashboard" replace />;
  return <Outlet />;
}
