/**
 * Who is signed in, which organization is active, and the actions that change that.
 *
 * On load the provider tries to refresh the session from the HttpOnly cookie; until that
 * answers, `status` is "loading" and protected routes show nothing rather than flashing
 * the login page at a signed-in user.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { api, onSessionLost, refreshSession, setAccessToken } from "../api/client";
import type { Organization, TokenPayload, User } from "../api/types";

export type AuthStatus = "loading" | "anonymous" | "authenticated";

export interface AuthState {
  status: AuthStatus;
  user: User | null;
  organizations: Organization[];
  activeOrganizationId: string | null;
}

export interface AuthActions {
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  switchOrganization: (organizationId: string) => Promise<void>;
  reloadUser: () => Promise<void>;
  reloadOrganizations: () => Promise<Organization[]>;
}

export type AuthContextValue = AuthState & AuthActions;

const anonymous: AuthState = {
  status: "anonymous",
  user: null,
  organizations: [],
  activeOrganizationId: null,
};

export const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ ...anonymous, status: "loading" });

  const loadAccount = useCallback(async (token: TokenPayload): Promise<void> => {
    const [me, orgs] = await Promise.all([
      api<User>("/users/me/"),
      api<Organization[]>("/organizations/"),
    ]);
    setState({
      status: "authenticated",
      user: me.data,
      organizations: orgs.data,
      activeOrganizationId: token.active_organization_id,
    });
  }, []);

  useEffect(() => {
    onSessionLost(() => {
      setState(anonymous);
    });
    const cancelled = { current: false };
    void (async () => {
      const token = await refreshSession();
      if (cancelled.current) return;
      if (!token) {
        setState(anonymous);
        return;
      }
      try {
        await loadAccount(token);
      } catch {
        setState(anonymous);
      }
    })();
    return () => {
      cancelled.current = true;
      onSessionLost(null);
    };
  }, [loadAccount]);

  const login = useCallback(
    async (email: string, password: string) => {
      const { data } = await api<TokenPayload>("/auth/login/", {
        method: "POST",
        body: { email, password, client: "web" },
        auth: false,
      });
      setAccessToken(data.access_token);
      await loadAccount(data);
    },
    [loadAccount],
  );

  const logout = useCallback(async () => {
    try {
      await api("/auth/logout/", { method: "POST" });
    } finally {
      setAccessToken(null);
      setState(anonymous);
    }
  }, []);

  const switchOrganization = useCallback(async (organizationId: string) => {
    const { data } = await api<TokenPayload>("/auth/switch-organization/", {
      method: "POST",
      body: { organization_id: organizationId },
    });
    setAccessToken(data.access_token);
    setState((current) => ({ ...current, activeOrganizationId: data.active_organization_id }));
  }, []);

  const reloadUser = useCallback(async () => {
    const { data } = await api<User>("/users/me/");
    setState((current) => ({ ...current, user: data }));
  }, []);

  const reloadOrganizations = useCallback(async () => {
    const { data } = await api<Organization[]>("/organizations/");
    setState((current) => ({ ...current, organizations: data }));
    return data;
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ ...state, login, logout, switchOrganization, reloadUser, reloadOrganizations }),
    [state, login, logout, switchOrganization, reloadUser, reloadOrganizations],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
