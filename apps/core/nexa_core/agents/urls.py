from django.urls import URLPattern, path

from nexa_core.agents import views

urlpatterns: list[URLPattern] = [
    path("agents/", views.AgentCollectionView.as_view(), name="agents"),
    path("agents/<uuid:agent_id>/", views.AgentDetailView.as_view(), name="agent"),
]
