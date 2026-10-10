from django.urls import path

from nexa_core.organizations import views

org = "organizations/<uuid:organization_id>/"

urlpatterns = [
    path("organizations/", views.OrganizationListView.as_view(), name="organizations"),
    path(org, views.OrganizationDetailView.as_view(), name="organization-detail"),
    path(org + "leave/", views.LeaveOrganizationView.as_view(), name="organization-leave"),
    path(org + "members/", views.MemberListView.as_view(), name="members"),
    path(
        org + "members/<uuid:membership_id>/",
        views.MemberDetailView.as_view(),
        name="member-detail",
    ),
    path(org + "invitations/", views.InvitationListView.as_view(), name="invitations"),
    path(
        org + "invitations/<uuid:invitation_id>/",
        views.InvitationDetailView.as_view(),
        name="invitation-detail",
    ),
    path(org + "teams/", views.TeamListView.as_view(), name="teams"),
    path(org + "teams/<uuid:team_id>/", views.TeamDetailView.as_view(), name="team-detail"),
    path(
        org + "teams/<uuid:team_id>/members/",
        views.TeamMemberListView.as_view(),
        name="team-members",
    ),
    path(
        org + "teams/<uuid:team_id>/members/<uuid:membership_id>/",
        views.TeamMemberDetailView.as_view(),
        name="team-member-detail",
    ),
    path(org + "roles/", views.RoleListView.as_view(), name="roles"),
    path(org + "roles/<uuid:role_id>/", views.RoleDetailView.as_view(), name="role-detail"),
    # Invitation links: the token identifies the organization, so these are not org-scoped.
    path("invitations/preview/", views.InvitationPreviewView.as_view(), name="invitation-preview"),
    path("invitations/accept/", views.InvitationAcceptView.as_view(), name="invitation-accept"),
    path("invitations/decline/", views.InvitationDeclineView.as_view(), name="invitation-decline"),
]
