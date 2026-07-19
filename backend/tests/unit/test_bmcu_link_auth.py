"""BMCU Link auth-separation tests (firmware issue #2).

Covers: device-scoped telemetry tokens accepted on /ingest (and only
there), scope separation from camera_stream, and CONTROL key provisioning
routes. The auth_enabled toggle is a DB settings row.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.bmcu_link_device import BMCULinkDevice
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services.long_lived_tokens import BMCU_LINK_TELEMETRY_SCOPE, create_token


def make_envelope():
    return {
        "schema": "bmcu.management.v2",
        "device_id": "dev1",
        "received_at_us": 1,
        "link": {"state": "online", "uart_sequence": 1, "pico_boot_session": "p", "bmcu_boot_session": 1},
        "frame": {"kind": "status"},
        "data": {},
    }


async def enable_auth(db_session):
    db_session.add(Settings(key="auth_enabled", value="true"))
    await db_session.commit()


async def mint_token(db_session, scope):
    user = User(username="tokowner", email="t@example.com", password_hash="x")
    db_session.add(user)
    await db_session.commit()
    created = await create_token(db_session, user_id=user.id, name="pico", expires_in_days=30, scope=scope)
    return created.plaintext


@pytest.fixture
def patched_route_session(test_engine, monkeypatch):
    """bmcu_link.py binds async_session at import; point it at the test DB
    (conftest patches core.database/auth/main but not this module)."""
    from backend.app.api.routes import bmcu_link as route_module

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(route_module, "async_session", maker)
    return maker


@pytest.mark.asyncio
async def test_ingest_open_when_auth_disabled(async_client):
    resp = await async_client.post("/api/v1/bmcu-link/ingest", json=make_envelope())
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ingest_rejects_anonymous_when_auth_enabled(async_client, db_session):
    await enable_auth(db_session)
    resp = await async_client.post("/api/v1/bmcu-link/ingest", json=make_envelope())
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_ingest_accepts_telemetry_token(async_client, db_session, patched_route_session):
    await enable_auth(db_session)
    token = await mint_token(db_session, BMCU_LINK_TELEMETRY_SCOPE)
    resp = await async_client.post(
        "/api/v1/bmcu-link/ingest",
        json=make_envelope(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ingest_rejects_camera_scope_token(async_client, db_session, patched_route_session):
    """Scope separation: a camera_stream token must not authorize ingest."""
    await enable_auth(db_session)
    token = await mint_token(db_session, "camera_stream")
    resp = await async_client.post(
        "/api/v1/bmcu-link/ingest",
        json=make_envelope(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_telemetry_token_rejected_outside_ingest(async_client, db_session, patched_route_session):
    """The telemetry token is ingest-only: read endpoints treat it as an
    invalid credential, not as a permission grant."""
    await enable_auth(db_session)
    token = await mint_token(db_session, BMCU_LINK_TELEMETRY_SCOPE)
    resp = await async_client.get(
        "/api/v1/bmcu-link/devices",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_control_key_provision_rotate_revoke(async_client, db_session):
    db_session.add(BMCULinkDevice(device_id="pico-1"))
    await db_session.commit()

    resp = await async_client.post("/api/v1/bmcu-link/devices/pico-1/control-key")
    assert resp.status_code == 201
    body = resp.json()
    key1 = body["control_key"]
    assert len(key1) == 64 and bytes.fromhex(key1)
    assert body["rotated"] is False

    # Key is encrypted at rest — plaintext never lands in the row.
    device = (
        await db_session.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == "pico-1"))
    ).scalar_one()
    await db_session.refresh(device)
    assert device.control_key_encrypted and key1 not in device.control_key_encrypted

    resp2 = await async_client.post("/api/v1/bmcu-link/devices/pico-1/control-key")
    assert resp2.status_code == 201
    assert resp2.json()["rotated"] is True
    assert resp2.json()["control_key"] != key1

    resp3 = await async_client.delete("/api/v1/bmcu-link/devices/pico-1/control-key")
    assert resp3.status_code == 204
    await db_session.refresh(device)
    assert device.control_key_encrypted is None


@pytest.mark.asyncio
async def test_control_key_unknown_device_404(async_client):
    resp = await async_client.post("/api/v1/bmcu-link/devices/nope/control-key")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_device_response_never_exposes_key_material(async_client, db_session):
    db_session.add(BMCULinkDevice(device_id="pico-1", control_key_encrypted="fernet:xyz"))
    await db_session.commit()
    resp = await async_client.get("/api/v1/bmcu-link/devices/pico-1")
    assert resp.status_code == 200
    body = resp.json()
    assert "control_key" not in body and "control_key_encrypted" not in body
    assert "fernet" not in resp.text
