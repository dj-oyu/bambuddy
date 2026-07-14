"""BMCU Link feature-toggle tests (BAMBUDDY_BMCU_LINK)."""

import inspect

import pytest

from backend.app.services.bmcu_link import bmcu_link_enabled


def make_envelope():
    return {
        "schema": "bmcu.management.v2",
        "device_id": "dev1",
        "received_at_us": 1,
        "link": {"state": "online", "uart_sequence": 1, "pico_boot_session": "p", "bmcu_boot_session": 1},
        "frame": {"kind": "status"},
        "data": {},
    }


def test_toggle_default_on(monkeypatch):
    monkeypatch.delenv("BAMBUDDY_BMCU_LINK", raising=False)
    assert bmcu_link_enabled() is True
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "1")
    assert bmcu_link_enabled() is True
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "0")
    assert bmcu_link_enabled() is False


@pytest.mark.asyncio
async def test_ingest_404_when_disabled(async_client, monkeypatch):
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "0")
    resp = await async_client.post("/api/v1/bmcu-link/ingest", json=make_envelope())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_devices_disabled_response(async_client, monkeypatch):
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "0")
    resp = await async_client.get("/api/v1/bmcu-link/devices")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "devices": []}


@pytest.mark.asyncio
async def test_ingest_works_when_enabled(async_client, monkeypatch):
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "1")
    resp = await async_client.post("/api/v1/bmcu-link/ingest", json=make_envelope())
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] + body["deduplicated"] == 1


def test_watchdog_start_is_guarded_by_toggle():
    """Lifespan must not start the watchdog when the toggle is off."""
    from backend.app import main

    src = inspect.getsource(main.lifespan)
    assert "if bmcu_link_enabled():" in src
    guard_idx = src.index("if bmcu_link_enabled():")
    start_idx = src.index("start_bmcu_link_watchdog()")
    assert start_idx > guard_idx


@pytest.mark.asyncio
async def test_ingest_batch_cap_413(async_client, monkeypatch):
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "1")
    batch = [make_envelope() for _ in range(501)]
    resp = await async_client.post("/api/v1/bmcu-link/ingest", json=batch)
    assert resp.status_code == 413


def test_ws_closes_4404_when_disabled(monkeypatch):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from backend.app.main import app

    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "0")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/bmcu-link/ws"):
            pass
    assert exc.value.code == 4404
