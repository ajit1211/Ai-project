import pytest
from django.test import Client
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView


def test_healthz_reports_service_and_request_id(client: Client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["service"] == "core"
    assert response.headers["X-Request-ID"].startswith("req_")


def test_incoming_request_id_is_echoed(client: Client) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "trace-123"})
    assert response.headers["X-Request-ID"] == "trace-123"


@pytest.mark.django_db
def test_readyz_reports_every_dependency(client: Client) -> None:
    response = client.get("/readyz")
    body = response.json()
    assert set(body["checks"]) == {"database", "redis", "celery"}
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["celery"]["critical"] is False
    # Redis may be absent where these tests run; readiness must still answer either way.
    assert response.status_code == (200 if body["checks"]["redis"]["status"] == "ok" else 503)


def test_unknown_route_returns_error_envelope(client: Client) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["request_id"] == response.headers["X-Request-ID"]


class _SignupSerializer(serializers.Serializer[dict[str, str]]):
    email = serializers.EmailField()


class _SignupView(APIView):
    permission_classes = (AllowAny,)

    def post(self, request: Request) -> Response:
        _SignupSerializer(data=request.data).is_valid(raise_exception=True)
        return Response(status=204)


def test_validation_errors_become_422_with_field_errors() -> None:
    request = APIRequestFactory().post("/signup", {"email": "not-an-email"}, format="json")
    response = _SignupView.as_view()(request)
    assert response.status_code == 422
    assert response.data["error"]["code"] == "VALIDATION_FAILED"
    assert response.data["error"]["details"] == [
        {"field": "email", "code": "invalid", "message": "Enter a valid email address."}
    ]


def test_endpoints_require_authentication_by_default() -> None:
    class _Private(APIView):
        def get(self, request: Request) -> Response:
            return Response({"secret": True})

    response = _Private.as_view()(APIRequestFactory().get("/private"))
    assert response.status_code == 401
    assert response.data["error"]["code"] == "AUTH_REQUIRED"
