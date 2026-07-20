"""GET /bmcu-link/connection-info — bridge endpoint URLs (LAN, no tailnet)."""

from collections import namedtuple
import socket

import pytest

from backend.app.api.routes.bmcu_link import _lan_ipv4_addresses

Addr = namedtuple("Addr", ["family", "address"])


def _fake_if_addrs():
    return {
        "lo": [Addr(socket.AF_INET, "127.0.0.1")],
        "eth0": [
            Addr(socket.AF_INET, "192.168.1.33"),
            Addr(socket.AF_INET6, "fe80::1"),
        ],
        "tailscale0": [Addr(socket.AF_INET, "100.74.116.110")],
        "docker0": [Addr(socket.AF_INET, "172.17.0.1")],
        "wlan0": [Addr(socket.AF_INET, "100.99.0.5")],  # CGNAT even on a real ifname
        "eth1": [Addr(socket.AF_INET, "169.254.10.10")],  # link-local
    }


def test_lan_ipv4_filters_tailscale_loopback_and_virtual(monkeypatch):
    import psutil

    monkeypatch.setattr(psutil, "net_if_addrs", _fake_if_addrs)
    assert _lan_ipv4_addresses() == ["192.168.1.33"]


def test_lan_ipv4_enumeration_failure_is_empty(monkeypatch):
    import psutil

    def boom():
        raise OSError("nope")

    monkeypatch.setattr(psutil, "net_if_addrs", boom)
    assert _lan_ipv4_addresses() == []


@pytest.mark.asyncio
async def test_connection_info_endpoint(async_client, monkeypatch):
    import psutil

    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "1")
    monkeypatch.setattr(psutil, "net_if_addrs", _fake_if_addrs)
    resp = await async_client.get("/api/v1/bmcu-link/connection-info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["telemetry_scope"] == "bmcu_link:telemetry"
    assert isinstance(body["auth_enabled"], bool)
    assert len(body["endpoints"]) == 1
    ep = body["endpoints"][0]
    assert ep["ip"] == "192.168.1.33"
    assert ep["ws_url"].startswith("ws://192.168.1.33:")
    assert ep["ws_url"].endswith("/api/v1/bmcu-link/ws")
    assert ep["ingest_url"].startswith("http://192.168.1.33:")
    assert ep["ingest_url"].endswith("/api/v1/bmcu-link/ingest")


@pytest.mark.asyncio
async def test_connection_info_404_when_disabled(async_client, monkeypatch):
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK", "0")
    resp = await async_client.get("/api/v1/bmcu-link/connection-info")
    assert resp.status_code == 404
