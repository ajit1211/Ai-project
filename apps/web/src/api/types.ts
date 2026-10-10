export interface UserProfile {
  avatar_url: string;
  timezone: string;
  locale: string;
  preferences: {
    theme?: "system" | "light" | "dark";
    notifications?: { invitations_email?: boolean; product_updates_email?: boolean };
  };
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  is_verified: boolean;
  created_at: string;
  profile: UserProfile;
}

export interface RoleRef {
  id: string;
  key: string;
  name: string;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
  kind: "personal" | "team";
  created_at: string;
  role?: RoleRef;
}

export interface OrganizationDetail extends Organization {
  settings: Record<string, unknown>;
  permissions: string[];
}

export interface Member {
  id: string;
  user: { id: string; email: string; full_name: string };
  role: RoleRef;
  status: "active" | "suspended";
  joined_at: string;
}

export interface Invitation {
  id: string;
  email: string;
  role: RoleRef;
  status: string;
  expires_at: string;
  sent_count: number;
  created_at: string;
}

export interface InvitationPreview {
  organization_name: string;
  inviter_name: string;
  role_name: string;
  email: string;
  expires_at: string;
}

export interface Role {
  id: string;
  key: string;
  name: string;
  description: string;
  is_system: boolean;
  permissions: string[];
}

export interface SessionInfo {
  id: string;
  device: string;
  client_type: "web" | "mobile";
  ip_address: string | null;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  is_current: boolean;
}

export interface TokenPayload {
  access_token: string;
  token_type: "Bearer";
  expires_in: number;
  session_id: string;
  active_organization_id: string | null;
  issued_at: string;
  user?: User;
}
