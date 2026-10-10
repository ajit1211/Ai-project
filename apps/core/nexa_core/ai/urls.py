from django.urls import URLPattern, path

from nexa_core.ai.views import UsageView

urlpatterns: list[URLPattern] = [
    path("ai/usage/", UsageView.as_view(), name="ai-usage"),
]
