from django.urls import path

from nexa_core.identity import views

urlpatterns = [
    path("auth/register/", views.RegisterView.as_view(), name="auth-register"),
    path("auth/login/", views.LoginView.as_view(), name="auth-login"),
    path("auth/refresh/", views.RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("auth/verify-email/", views.VerifyEmailView.as_view(), name="auth-verify-email"),
    path(
        "auth/resend-verification/",
        views.ResendVerificationView.as_view(),
        name="auth-resend-verification",
    ),
    path("auth/forgot-password/", views.ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("auth/reset-password/", views.ResetPasswordView.as_view(), name="auth-reset-password"),
    path("auth/change-password/", views.ChangePasswordView.as_view(), name="auth-change-password"),
    path(
        "auth/switch-organization/",
        views.SwitchOrganizationView.as_view(),
        name="auth-switch-organization",
    ),
    path("users/me/", views.MeView.as_view(), name="users-me"),
    path("users/me/deactivate/", views.DeactivateAccountView.as_view(), name="users-me-deactivate"),
    path("sessions/", views.SessionListView.as_view(), name="sessions"),
    path("sessions/logout-all/", views.LogoutAllView.as_view(), name="sessions-logout-all"),
    path("sessions/<uuid:session_id>/", views.SessionDetailView.as_view(), name="session-detail"),
]
