import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute, PublicOnlyRoute } from "./auth/routes";
import { AppShell } from "./components/AppShell";
import { ChatRoute } from "./pages/ChatRoute";
import { DashboardPage } from "./pages/DashboardPage";
import { DocumentDetailPage } from "./pages/DocumentDetailPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { MemoriesPage } from "./pages/MemoriesPage";
import { InvitationPage } from "./pages/InvitationPage";
import { OrganizationPage } from "./pages/OrganizationPage";
import { PlaygroundPage } from "./pages/PlaygroundPage";
import {
  NotificationSettings,
  OrganizationSettings,
  ProfileSettings,
  SecuritySettings,
  SessionSettings,
  SettingsLayout,
} from "./pages/SettingsPage";
import { StatusPage } from "./pages/StatusPage";
import {
  ForgotPasswordPage,
  LoginPage,
  RegisterPage,
  ResetPasswordPage,
  VerifyEmailPage,
} from "./pages/auth";

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/status" element={<StatusPage />} />

          {/* Public: usable whether or not someone is signed in. */}
          <Route path="/verify-email" element={<VerifyEmailPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/invitations/accept" element={<InvitationPage />} />

          <Route element={<PublicOnlyRoute />}>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />
            <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          </Route>

          <Route element={<ProtectedRoute />}>
            <Route element={<AppShell />}>
              <Route path="/dashboard" element={<DashboardPage />} />
              {/* One route for both URLs, so opening a conversation does not remount the page. */}
              <Route path="/chat/:conversationId?" element={<ChatRoute />} />
              <Route path="/documents" element={<DocumentsPage />} />
              <Route path="/documents/:id" element={<DocumentDetailPage />} />
              <Route path="/memories" element={<MemoriesPage />} />
              <Route path="/playground" element={<PlaygroundPage />} />
              <Route path="/organizations/:organizationId" element={<OrganizationPage />} />
              <Route path="/settings" element={<SettingsLayout />}>
                <Route index element={<Navigate to="/settings/profile" replace />} />
                <Route path="profile" element={<ProfileSettings />} />
                <Route path="security" element={<SecuritySettings />} />
                <Route path="sessions" element={<SessionSettings />} />
                <Route path="organizations" element={<OrganizationSettings />} />
                <Route path="notifications" element={<NotificationSettings />} />
              </Route>
            </Route>
          </Route>

          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
