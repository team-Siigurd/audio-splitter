from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_reject_non_sharepoint_source():
    response = client.post(
        "/split",
        json={"url": "https://example.com/file.mp3", "segment_seconds": 900},
    )
    assert response.status_code == 400
