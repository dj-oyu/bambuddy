"""BMCU Link watchdog state-transition tests."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.bmcu_link_device import BMCULinkDevice
from backend.app.services.bmcu_link import BMCULinkService


@pytest.fixture
def ws_mock(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr("backend.app.services.bmcu_link.ws_manager", mock)
    return mock


@pytest.fixture
def notif_mock(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr("backend.app.services.notification_service.notification_service", mock)
    return mock


@pytest.fixture
def service(test_engine, ws_mock, notif_mock):
    svc = BMCULinkService()
    svc._session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    fake_now = [1000.0]
    svc._clock = lambda: fake_now[0]
    svc._last_flush = fake_now[0]
    svc._last_retention = fake_now[0]
    svc._fake_now = fake_now
    svc._printing_printers = lambda: []  # default: nothing printing
    return svc


def _state_broadcasts(ws_mock):
    return [
        call.args[0]
        for call in ws_mock.broadcast.await_args_list
        if call.args[0].get("type") == "bmcu_link_device_state"
    ]


@pytest.mark.asyncio
async def test_online_to_stale_to_offline(service, ws_mock, db_session):
    db_session.add(BMCULinkDevice(device_id="dev1", link_state="online"))
    await db_session.commit()

    service.mark_seen("dev1")
    service._link_state["dev1"] = "online"

    # < 10s: still online, no broadcast
    service._fake_now[0] += 5
    await service.watchdog_tick()
    assert _state_broadcasts(ws_mock) == []

    # > 10s: stale
    service._fake_now[0] += 6
    await service.watchdog_tick()
    msgs = _state_broadcasts(ws_mock)
    assert msgs == [{"type": "bmcu_link_device_state", "device_id": "dev1", "state": "stale"}]

    # Repeated tick in the same state: no duplicate broadcast (transition only)
    service._fake_now[0] += 1
    await service.watchdog_tick()
    assert len(_state_broadcasts(ws_mock)) == 1

    # > 30s: offline, persisted to DB
    service._fake_now[0] += 25
    await service.watchdog_tick()
    msgs = _state_broadcasts(ws_mock)
    assert msgs[-1]["state"] == "offline"

    async with service._sessionmaker()() as db:
        device = (await db.get(BMCULinkDevice, 1))
        assert device.link_state == "offline"


@pytest.mark.asyncio
async def test_offline_notification_only_when_printing(service, notif_mock):
    service.mark_seen("dev1")
    service._link_state["dev1"] = "online"

    # Nothing printing -> no notification
    service._fake_now[0] += 31
    await service.watchdog_tick()
    notif_mock.on_bmcu_link_device_offline.assert_not_awaited()

    # Second device goes offline while a printer is printing -> notification
    service._printing_printers = lambda: [(7, "A1 mini")]
    service.mark_seen("dev2")
    service._link_state["dev2"] = "online"
    service._fake_now[0] += 31
    await service.watchdog_tick()
    notif_mock.on_bmcu_link_device_offline.assert_awaited_once()
    call = notif_mock.on_bmcu_link_device_offline.await_args
    assert call.args[0] == "dev2"
    assert call.args[2] == ["A1 mini"]
    assert call.kwargs.get("printer_id") == 7  # scoped to the printing printer


@pytest.mark.asyncio
async def test_reingest_brings_device_back_online(service, ws_mock):
    from backend.app.schemas.bmcu_link import BMCULinkEnvelope

    service._link_state["dev1"] = "offline"
    service._last_seen["dev1"] = service._fake_now[0] - 100

    env = BMCULinkEnvelope.model_validate(
        {
            "schema": "bmcu.management.v2",
            "device_id": "dev1",
            "received_at_us": 1,
            "link": {"state": "online", "uart_sequence": 1, "pico_boot_session": "p", "bmcu_boot_session": 1},
            "frame": {"kind": "heartbeat"},
            "data": {},
        }
    )
    await service.ingest([env])
    assert service._link_state["dev1"] == "online"
    msgs = _state_broadcasts(ws_mock)
    assert msgs[-1]["state"] == "online"


def _anomaly_env(seq, device_id="dev1"):
    from backend.app.schemas.bmcu_link import BMCULinkEnvelope

    return BMCULinkEnvelope.model_validate(
        {
            "schema": "bmcu.management.v2",
            "device_id": device_id,
            "received_at_us": seq,
            "link": {"state": "online", "uart_sequence": seq, "pico_boot_session": "p", "bmcu_boot_session": 1},
            "frame": {"kind": "anomaly"},
            "data": {"reason": 1},
        }
    )


@pytest.mark.asyncio
async def test_anomaly_notification_latched_per_device(service, notif_mock, ws_mock):
    # Flood of anomaly frames -> exactly one provider notification per cooldown
    await service.ingest([_anomaly_env(seq) for seq in range(1, 6)])
    assert notif_mock.on_bmcu_link_anomaly.await_count == 1
    # Broadcasts remain immediate (one per anomaly envelope)
    anomaly_broadcasts = [
        c.args[0] for c in ws_mock.broadcast.await_args_list if c.args[0].get("type") == "bmcu_link_anomaly"
    ]
    assert len(anomaly_broadcasts) == 5

    # Within cooldown: still latched
    service._fake_now[0] += 30
    await service.ingest([_anomaly_env(10)])
    assert notif_mock.on_bmcu_link_anomaly.await_count == 1

    # Past cooldown: fires again
    service._fake_now[0] += 31
    await service.ingest([_anomaly_env(11)])
    assert notif_mock.on_bmcu_link_anomaly.await_count == 2

    # Independent device gets its own latch
    await service.ingest([_anomaly_env(1, device_id="devX")])
    assert notif_mock.on_bmcu_link_anomaly.await_count == 3


@pytest.mark.asyncio
async def test_device_cap_rejects_new_devices(service):
    service.MAX_TRACKED_DEVICES = 2
    r = await service.ingest([_anomaly_env(1, device_id="a"), _anomaly_env(1, device_id="b")])
    assert r.accepted == 2
    r = await service.ingest([_anomaly_env(1, device_id="c")])
    assert r.accepted == 0 and r.deduplicated == 0
    assert service._rejected_device_envelopes == 1
    # Known device still accepted
    r = await service.ingest([_anomaly_env(2, device_id="a")])
    assert r.accepted == 1


@pytest.mark.asyncio
async def test_long_offline_device_evicted(service):
    service.mark_seen("dev1")
    service._link_state["dev1"] = "online"
    service._fake_now[0] += 31
    await service.watchdog_tick()
    assert service._link_state["dev1"] == "offline"

    # Offline but under eviction threshold: state kept
    service._fake_now[0] += 60
    await service.watchdog_tick()
    assert "dev1" in service._last_seen

    # Offline past EVICT_AFTER_S: in-memory state dropped
    service._fake_now[0] += service.EVICT_AFTER_S
    await service.watchdog_tick()
    assert "dev1" not in service._last_seen
    assert "dev1" not in service._link_state
    assert "dev1" not in service._dedup
