from fastapi.testclient import TestClient


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "DocIntel"
    assert body["environment"] == "test"
    assert body["version"]


def test_health_also_served_unversioned(client: TestClient) -> None:
    assert client.get("/health").json() == client.get("/api/v1/health").json()
