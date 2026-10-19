from django.urls import path

from nexa_core.documents import views

urlpatterns = [
    path("documents/", views.DocumentCollectionView.as_view(), name="documents"),
    path("documents/<uuid:document_id>/", views.DocumentView.as_view(), name="document"),
    path(
        "documents/<uuid:document_id>/status/",
        views.DocumentStatusView.as_view(),
        name="document-status",
    ),
    path(
        "documents/<uuid:document_id>/content/",
        views.DocumentContentView.as_view(),
        name="document-content",
    ),
    path(
        "documents/<uuid:document_id>/preview/",
        views.DocumentPreviewView.as_view(),
        name="document-preview",
    ),
    path(
        "documents/<uuid:document_id>/download/",
        views.DocumentDownloadView.as_view(),
        name="document-download",
    ),
    path(
        "documents/<uuid:document_id>/retry/",
        views.DocumentRetryView.as_view(),
        name="document-retry",
    ),
]
