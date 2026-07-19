"""BMCU Link flush batching tests (size- and time-based)."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.bmcu_link_event import BMCULinkEvent
from backend.app.schemas.bmcu_link import BMCULinkEnvelope
from backend.app.services.bmcu_link import BMCULinkService


def make_env(seq, device_id="dev1"):
    return BMCULinkEnvelope.model_validate(
        {
            "schema": "bmcu.management.v2",
            "device_id": device_id,
            "received_at_us": 1000 + seq,
            "link": {
                "state": "online",
                "uart_sequence": seq,
                "pico_boot_session": "picoA",
                "bmcu_boot_session": 1,
            },
            "frame": {"kind": "status", "kind_id": 2, "protocol": 2},
            "data": {"seq": seq},
        }
    )


@pytest.fixture
def service(test_engine, monkeypatch):
    svc = BMCULinkService()
    svc._session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    fake_now = [1000.0]
    svc._clock = lambda: fake_now[0]
    svc._last_flush = fake_now[0]
    svc._fake_now = fake_now  # test handle
    monkeypatch.setattr("backend.app.services.bmcu_link.ws_manager", AsyncMock())
    return svc


async def _event_count(svc):
    async with svc._sessionmaker()() as db:
        return (await db.execute(select(func.count(BMCULinkEvent.id)))).scalar()


@pytest.mark.asyncio
async def test_no_flush_below_batch_size(service):
    await service.ingest([make_env(i) for i in range(199)])
    assert len(service._pending) == 199
    assert await _event_count(service) == 0


@pytest.mark.asyncio
async def test_flush_at_batch_size(service):
    await service.ingest([make_env(i) for i in range(200)])
    assert len(service._pending) == 0
    assert await _event_count(service) == 200


@pytest.mark.asyncio
async def test_time_based_flush_via_watchdog_tick(service):
    await service.ingest([make_env(i) for i in range(5)])
    assert await _event_count(service) == 0

    # Not yet past FLUSH_INTERVAL_S
    service._fake_now[0] += 1.0
    await service.watchdog_tick()
    assert await _event_count(service) == 0

    # Past FLUSH_INTERVAL_S -> tick flushes
    service._fake_now[0] += 2.5
    await service.watchdog_tick()
    assert await _event_count(service) == 5
    assert len(service._pending) == 0


def make_dropped_env(seq, dropped, pico="picoA", bmcu=1):
    return BMCULinkEnvelope.model_validate(
        {
            "schema": "bmcu.management.v2",
            "device_id": "dev1",
            "received_at_us": 1000 + seq,
            "link": {
                "state": "online",
                "uart_sequence": seq,
                "pico_boot_session": pico,
                "bmcu_boot_session": bmcu,
            },
            "frame": {"kind": "dropped"},
            "data": {"dropped_count": dropped},
        }
    )


@pytest.mark.asyncio
async def test_dropped_count_cumulative_not_summed(service, db_session):
    from backend.app.models.bmcu_link_device import BMCULinkDevice

    db_session.add(BMCULinkDevice(device_id="dev1", link_state="online", dropped_count=0))
    await db_session.commit()

    # Firmware reports cumulative values 3 then 7 within one boot session
    await service.ingest([make_dropped_env(1, 3), make_dropped_env(2, 7)])
    await service.flush()
    async with service._sessionmaker()() as db:
        device = await db.get(BMCULinkDevice, 1)
        assert device.dropped_count == 7  # latest, NOT 3+7

    # BMCU reboot alone does NOT reset the cumulative (issue #2 contract:
    # dropped_count is per PICO boot); a stale lower value keeps the max.
    await service.ingest([make_dropped_env(3, 2, bmcu=2)])
    await service.flush()
    async with service._sessionmaker()() as db:
        device = await db.get(BMCULinkDevice, 1)
        assert device.dropped_count == 7

    # New PICO boot session: previous final value folds in, new run starts
    await service.ingest([make_dropped_env(1, 2, pico="picoB")])
    await service.flush()
    async with service._sessionmaker()() as db:
        device = await db.get(BMCULinkDevice, 1)
        assert device.dropped_count == 7 + 2


@pytest.mark.asyncio
async def test_flush_failure_requeues_rows_once(service, monkeypatch):
    await service.ingest([make_env(i) for i in range(5)])
    assert len(service._pending) == 5

    good_factory = service._session_factory

    class BoomFactory:
        def __call__(self):
            raise RuntimeError("db down")

    service._session_factory = BoomFactory()
    await service.flush()
    # Failed flush re-queues the rows for one retry
    assert len(service._pending) == 5
    assert service._flush_retry_pending is True

    service._session_factory = good_factory
    await service.flush()
    assert len(service._pending) == 0
    assert await _event_count(service) == 5
    assert service._flush_retry_pending is False
