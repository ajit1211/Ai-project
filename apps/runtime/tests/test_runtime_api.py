from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app

# Ports nothing listens on, so readiness checks fail fast and deterministically.
UNREACHABLE = RuntimeSettings(
    nexa_env="test",
    runtime_database_url="postgresql://nobody:nothing@127.0.0.1:1/none",
    redis_queue_url="redis://127.0.0.1:1/0",
)


class Signup(BaseModel):
    email: str
    age: int


def _app_with_test_routes() -> FastAPI:
    app = create_app(UNREACHABLE)

    @app.post("/test/signup")
    async def signup(payload: Signup) -> dict[str, str]:
        return {"email": payload.email}

    @app.get("/test/boom")
    async def boom() -> None:
        raise RuntimeError("unexpected")

    return app


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(_app_with_test_routes(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_healthz_reports_service_and_request_id(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["service"] == "runtime"
    assert response.headers["X-Request-ID"].startswith("req_")


def test_readyz_reports_unreachable_dependencies(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["database"]["status"] == "error"
    assert checks["redis"]["status"] == "error"


def test_unknown_route_returns_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist", headers={"X-Request-ID": "trace-9"})
    body = response.json()
    assert response.status_code == 404
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["request_id"] == "trace-9"


def test_validation_errors_become_422_with_field_errors(client: TestClient) -> None:
    response = client.post("/test/signup", json={"email": "ada@example.com", "age": "old"})
    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert [detail["field"] for detail in body["error"]["details"]] == ["age"]


def test_unhandled_errors_become_500_without_leaking_details(client: TestClient) -> None:
    response = client.get("/test/boom")
    body = response.json()
    assert response.status_code == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "unexpected" not in response.text
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_oversized_bodies_are_refused_before_parsing(client: TestClient) -> None:
    big = b'{"email": "' + b"a" * (5 * 1024 * 1024) + b'", "age": 1}'
    response = client.post(
        "/test/signup", content=big, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert response.headers["X-Request-ID"]


def test_chunked_bodies_are_capped_while_they_arrive(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:  # no Content-Length: sent chunked
        yield b'{"email": "'
        for _ in range(6):
            yield b"a" * (1024 * 1024)
        yield b'", "age": 1}'

    response = client.post(
        "/test/signup", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
