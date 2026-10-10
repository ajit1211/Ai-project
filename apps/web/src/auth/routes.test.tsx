import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import { AuthContext, type AuthContextValue } from "./AuthContext";
import { ProtectedRoute, PublicOnlyRoute } from "./routes";

const noop = () => Promise.resolve();

function contextValue(status: AuthContextValue["status"]): AuthContextValue {
  return {
    status,
    user: null,
    organizations: [],
    activeOrganizationId: null,
    login: noop,
    logout: noop,
    switchOrganization: noop,
    reloadUser: noop,
    reloadOrganizations: () => Promise.resolve([]),
  };
}

function renderAt(path: string, status: AuthContextValue["status"]) {
  return render(
    <AuthContext.Provider value={contextValue(status)}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<PublicOnlyRoute />}>
            <Route path="/login" element={<p>Login page</p>} />
          </Route>
          <Route element={<ProtectedRoute />}>
            <Route path="/dashboard" element={<p>Dashboard page</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

describe("route guards", () => {
  afterEach(cleanup);

  it("sends anonymous visitors from protected pages to login", () => {
    renderAt("/dashboard", "anonymous");
    expect(screen.getByText("Login page")).toBeTruthy();
  });

  it("shows protected pages to signed-in users", () => {
    renderAt("/dashboard", "authenticated");
    expect(screen.getByText("Dashboard page")).toBeTruthy();
  });

  it("keeps signed-in users away from the login page", () => {
    renderAt("/login", "authenticated");
    expect(screen.getByText("Dashboard page")).toBeTruthy();
  });

  it("shows nothing decisive while the session is still loading", () => {
    renderAt("/dashboard", "loading");
    expect(screen.queryByText("Login page")).toBeNull();
    expect(screen.queryByText("Dashboard page")).toBeNull();
  });
});
