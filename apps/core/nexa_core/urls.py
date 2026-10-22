"""URL routes for NEXA Core.

Core owns every `/api/v1` prefix except the Runtime ones listed in doc 18.2.
"""

from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

from nexa_core.agents.urls import urlpatterns as agent_urls
from nexa_core.ai.urls import urlpatterns as ai_urls
from nexa_core.audit.views import AuditLogListView
from nexa_core.documents.urls import urlpatterns as document_urls
from nexa_core.identity import views as identity_views
from nexa_core.identity.urls import urlpatterns as identity_urls
from nexa_core.organizations.urls import urlpatterns as organization_urls
from nexa_core.system import views as system_views

api_v1: list[URLPattern | URLResolver] = [
    *identity_urls,
    *organization_urls,
    *ai_urls,
    *document_urls,
    *agent_urls,
    path(
        "organizations/<uuid:organization_id>/audit-logs/",
        AuditLogListView.as_view(),
        name="audit-logs",
    ),
]

urlpatterns = [
    path("healthz", system_views.healthz, name="healthz"),
    path("readyz", system_views.readyz, name="readyz"),
    path("admin/", admin.site.urls),
    path(".well-known/jwks.json", identity_views.jwks_view, name="jwks"),
    path("api/v1/", include((api_v1, "api"), namespace="v1")),
]

handler404 = "nexa_core.system.views.not_found"
handler500 = "nexa_core.system.views.server_error"
