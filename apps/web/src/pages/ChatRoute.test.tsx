import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/AuthContext";
import { ChatRoute } from "./ChatRoute";

// The chat's code cannot be fetched: a new deployment replaced it, or the network is gone.
vi.mock("./ChatPage", () => {
  throw new Error("Failed to fetch dynamically imported module");
});

const noop = () => Promise.resolve();
const auth: AuthContextValue = {
  status: "authenticated",
  user: null,
  organizations: [],
  activeOrganizationId: null,
  login: noop,
  logout: noop,
  switchOrganization: noop,
  reloadUser: noop,
  reloadOrganizations: () => Promise.resolve([]),
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("offers a reload instead of blanking the app when the chat cannot be loaded", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined); // React reports the caught error
  render(
    <AuthContext.Provider value={auth}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat/:conversationId?" element={<ChatRoute />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("The chat could not be loaded");
  expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
});
