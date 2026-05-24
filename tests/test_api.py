"""End-to-end API tests using FastAPI's TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from webhook_relay.api import create_app
from webhook_relay.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/api.db",
        worker_poll_interval_seconds=0.01,
        signing_secret="api-test",
    )
    # We disable the worker so we don't race with assertions.
    app = create_app(settings, run_worker=False)
    with TestClient(app) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_register_endpoint_and_enqueue(client: TestClient) -> None:
    r = client.post(
        "/endpoints",
        json={"id": "ep1", "url": "https://example.test/hook"},
    )
    assert r.status_code == 201, r.text

    r = client.post(
        "/events",
        json={
            "endpoint_id": "ep1",
            "event_type": "order.created",
            "payload": {"order_id": 42},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"].startswith("evt_")

    # Look up the event
    e = client.get(f"/events/{body['event_id']}").json()
    assert e["status"] == "pending"
    assert e["attempts"] == 0
    assert e["payload"] == {"order_id": 42}


def test_enqueue_unknown_endpoint_400(client: TestClient) -> None:
    r = client.post(
        "/events",
        json={"endpoint_id": "ghost", "event_type": "x", "payload": {}},
    )
    assert r.status_code == 400


def test_stats(client: TestClient) -> None:
    client.post("/endpoints", json={"id": "ep-stats", "url": "https://example.test/x"})
    client.post(
        "/events",
        json={"endpoint_id": "ep-stats", "event_type": "x", "payload": {}},
    )
    s = client.get("/stats").json()
    assert s["pending"] >= 1


def test_replay_endpoint(client: TestClient) -> None:
    client.post("/endpoints", json={"id": "ep-replay", "url": "https://example.test/r"})
    enq = client.post(
        "/events", json={"endpoint_id": "ep-replay", "event_type": "x", "payload": {}}
    ).json()
    r = client.post(f"/events/{enq['event_id']}/replay")
    assert r.status_code == 200
