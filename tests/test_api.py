"""HTTP-level tests (require fastapi; run with `pytest`)."""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

client = TestClient(create_app())


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["listings"] == 40


def test_search_happy_path_returns_request_id_header():
    r = client.post("/api/search", json={"query": "meeting room for 6 in BKC tomorrow morning", "reference_time": "2026-10-05T10:00:00+05:30", "parser": "rules"})
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == r.headers["x-request-id"]
    assert body["status"] in {"ok", "partial", "no_exact_match"}


@pytest.mark.parametrize("payload", [{"query": ""}, {"query": "   "}, {"query": "x" * 501}, {}, {"query": "ok", "limit": 99}])
def test_invalid_requests_are_422(payload):
    r = client.post("/api/search", json=payload)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_listing_lookup():
    assert client.get("/api/listings/L001").status_code == 200
    assert client.get("/api/listings/NOPE").status_code == 404
    assert len(client.get("/api/listings").json()) == 40


def test_ui_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "SpaceScout" in r.text
