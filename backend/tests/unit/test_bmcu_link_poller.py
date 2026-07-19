"""BMCU Link Pico /api/status poller tests."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.bmcu_link_device import BMCULinkDevice
from backend.app.models.bmcu_link_event import BMCULinkEvent
from backend.app.services.bmcu_link import BMCULinkService
from backend.app.services.bmcu_link_poller import (
    BMCULinkPoller,
    bmcu_link_poll_interval,
    bmcu_link_poll_url,
)


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
    svc._printing_printers = lambda: []
    return svc


def make_body(**overrides):
    body = {
        "wifi": {"state": "online", "ip": "192.168.1.50"},
        "bmcu": {
            "link": "online",
            "tick_hz": 18_000_000,
            "status": {
                "hw_tick32": 1234,
                "tx_drop": 0,
                "rx_drop": 0,
                "crc_error": 0,
                "frame_error": 0,
                "current_slot": 255,
                "inserted_mask": 3,
                "online_mask": 3,
                "motion": [0, 0, 0, 0],
                "pull_pct": [10, 20, 0, 0],
                "pressure": 512,
                "led_mode": 0,
                "control_error": 0,
            },
            "snapshot": [],
            "channels": [None, None, None, None],
            "events": [],
            "sensors": {},
            "decoder_crc_errors": 0,
            "decoder_frame_errors": 0,
        },
    }
    bmcu_over = overrides.pop("bmcu", {})
    body.update(overrides)
    if isinstance(body.get("bmcu"), dict):
        body["bmcu"].update(bmcu_over)
    return body


def make_event(tick=100, record_type=4, severity=1, source=2, payload="00ff", **extra):
    return {
        "hw_tick32": tick,
        "record_type": record_type,
        "severity": severity,
        "source": source,
        "payload_length": 4,
        "payload": payload,
        "event_name": "state_change",
        **extra,
    }


def make_poller(service, bodies, clock=None):
    """Poller with a scripted fetch: pops from `bodies`; an Exception raises."""

    async def fetch():
        item = bodies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    fake_now = [1000.0]
    poller = BMCULinkPoller(
        "http://192.168.1.50/api/status",
        service=service,
        fetch=fetch,
        clock=(clock or (lambda: fake_now[0])),
    )
    poller._fake_now = fake_now
    return poller


async def event_rows(service):
    async with service._sessionmaker()() as db:
        return (await db.execute(select(BMCULinkEvent).order_by(BMCULinkEvent.id))).scalars().all()


# ------------------------------------------------------------------ config


def test_poll_url_normalization(monkeypatch):
    monkeypatch.delenv("BAMBUDDY_BMCU_LINK_POLL_URL", raising=False)
    assert bmcu_link_poll_url() is None
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK_POLL_URL", "")
    assert bmcu_link_poll_url() is None
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK_POLL_URL", "192.168.1.50")
    assert bmcu_link_poll_url() == "http://192.168.1.50/api/status"
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK_POLL_URL", "http://pico.local/")
    assert bmcu_link_poll_url() == "http://pico.local/api/status"
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK_POLL_URL", "http://pico.local/custom/path")
    assert bmcu_link_poll_url() == "http://pico.local/custom/path"


def test_poll_interval_clamped(monkeypatch):
    monkeypatch.delenv("BAMBUDDY_BMCU_LINK_POLL_INTERVAL", raising=False)
    assert bmcu_link_poll_interval() == BMCULinkPoller.INTERVAL_DEFAULT
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK_POLL_INTERVAL", "0.1")
    assert bmcu_link_poll_interval() == BMCULinkPoller.INTERVAL_MIN
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK_POLL_INTERVAL", "60")
    assert bmcu_link_poll_interval() == BMCULinkPoller.INTERVAL_MAX
    monkeypatch.setenv("BAMBUDDY_BMCU_LINK_POLL_INTERVAL", "junk")
    assert bmcu_link_poll_interval() == BMCULinkPoller.INTERVAL_DEFAULT


def test_device_id_derivation():
    p = BMCULinkPoller("http://192.168.1.50/api/status", service=object())
    assert p.device_id == "pico-192.168.1.50"
    p = BMCULinkPoller("http://x/api/status", device_id="custom", service=object())
    assert p.device_id == "custom"


# ------------------------------------------------------------- first poll


@pytest.mark.asyncio
async def test_first_poll_registers_device_and_status(service):
    poller = make_poller(service, [make_body()])
    assert await poller.poll_once()
    await service.flush()

    async with service._sessionmaker()() as db:
        device = (
            await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == "pico-192.168.1.50"))
        ).scalar_one()
        assert device.link_state == "online"
        assert device.mode == "poll"
        assert device.last_status is not None

    rows = await event_rows(service)
    assert [r.kind for r in rows] == ["hello", "status"]
    # Status data is a flat superset the settings tab can render generically.
    import json

    data = json.loads(rows[1].data)
    assert data["inserted_mask"] == 3
    assert data["link"] == "online"
    assert data["wifi_state"] == "online"


@pytest.mark.asyncio
async def test_unchanged_status_emits_nothing(service):
    poller = make_poller(service, [make_body(), make_body(), make_body()])
    for _ in range(3):
        # Only hw_tick32 (volatile) differs between polls
        await poller.poll_once()
    await service.flush()
    rows = await event_rows(service)
    assert [r.kind for r in rows] == ["hello", "status"]
    assert service._last_seen.get("pico-192.168.1.50") is not None


@pytest.mark.asyncio
async def test_changed_status_emits_once(service):
    b1 = make_body()
    b2 = make_body()
    b2["bmcu"]["status"]["hw_tick32"] = 9999  # volatile: ignored
    b3 = make_body()
    b3["bmcu"]["status"]["inserted_mask"] = 7  # semantic change
    poller = make_poller(service, [b1, b2, b3])
    for _ in range(3):
        await poller.poll_once()
    await service.flush()
    rows = await event_rows(service)
    assert [r.kind for r in rows] == ["hello", "status", "status"]


@pytest.mark.asyncio
async def test_status_heartbeat(service):
    poller = make_poller(service, [make_body(), make_body()])
    await poller.poll_once()
    poller._fake_now[0] += BMCULinkPoller.STATUS_HEARTBEAT_S + 1
    await poller.poll_once()
    await service.flush()
    rows = await event_rows(service)
    assert [r.kind for r in rows] == ["hello", "status", "status"]


# ------------------------------------------------------------- event ring


@pytest.mark.asyncio
async def test_ring_dedup(service):
    ring = [make_event(tick=t) for t in (100, 200, 300)]
    b1 = make_body(bmcu={"events": list(ring)})
    b2 = make_body(bmcu={"events": list(ring)})  # identical ring re-served
    b3 = make_body(bmcu={"events": list(ring) + [make_event(tick=400)]})
    poller = make_poller(service, [b1, b2, b3])
    for _ in range(3):
        await poller.poll_once()
    await service.flush()
    rows = [r for r in await event_rows(service) if r.kind == "event"]
    assert len(rows) == 4  # 3 initial + 1 new, no re-ingest


@pytest.mark.asyncio
async def test_ring_reset_after_empty(service):
    ring = [make_event(tick=100)]
    bodies = [
        make_body(bmcu={"events": list(ring)}),
        make_body(bmcu={"events": []}),  # ring emptied (Pico reboot)
        make_body(bmcu={"events": list(ring)}),  # same fingerprint reappears
    ]
    poller = make_poller(service, bodies)
    for _ in range(3):
        await poller.poll_once()
    await service.flush()
    rows = [r for r in await event_rows(service) if r.kind == "event"]
    assert len(rows) == 2  # accepted again after reset


@pytest.mark.asyncio
async def test_high_severity_event_is_anomaly(service, ws_mock, notif_mock):
    b = make_body(bmcu={"events": [make_event(tick=100, severity=4)]})
    poller = make_poller(service, [b])
    await poller.poll_once()
    await service.flush()
    rows = await event_rows(service)
    assert "anomaly" in [r.kind for r in rows]
    anomaly_msgs = [
        c.args[0] for c in ws_mock.broadcast.await_args_list if c.args[0].get("type") == "bmcu_link_anomaly"
    ]
    assert anomaly_msgs
    notif_mock.on_bmcu_link_anomaly.assert_awaited()


# --------------------------------------------------------------- bmcu link


@pytest.mark.asyncio
async def test_bmcu_stale_transition_anomaly(service):
    bodies = [
        make_body(),
        make_body(bmcu={"link": "stale"}),
        make_body(bmcu={"link": "stale"}),  # no duplicate anomaly
        make_body(),  # recovery emits a status envelope
    ]
    poller = make_poller(service, bodies)
    for _ in range(4):
        await poller.poll_once()
    await service.flush()
    rows = await event_rows(service)
    kinds = [r.kind for r in rows]
    assert kinds.count("anomaly") == 1
    # hello + stale-transition status + anomaly + recovery status
    assert kinds.count("status") >= 3


# ----------------------------------------------------------------- failure


@pytest.mark.asyncio
async def test_fetch_failure_and_backoff(service):
    bodies = [make_body(), RuntimeError("boom"), RuntimeError("boom"), make_body()]
    poller = make_poller(service, bodies)
    assert await poller.poll_once()
    assert poller._next_delay() == poller.interval

    assert not await poller.poll_once()
    assert poller._fail_count == 1
    d1 = poller._next_delay()
    assert d1 >= poller.interval * 2

    assert not await poller.poll_once()
    assert poller._fail_count == 2
    assert poller._next_delay() >= poller.interval * 4
    assert poller._next_delay() <= BMCULinkPoller.MAX_BACKOFF_S + poller.interval

    # Recovery resets backoff and forces a status emit
    assert await poller.poll_once()
    assert poller._fail_count == 0
    await service.flush()
    rows = await event_rows(service)
    assert [r.kind for r in rows] == ["hello", "status", "status"]


@pytest.mark.asyncio
async def test_null_tolerance(service):
    bodies = [
        {"wifi": None, "bmcu": None},
        {},
        make_body(bmcu={"status": None, "channels": None, "events": None}),
    ]
    poller = make_poller(service, bodies)
    for _ in range(3):
        assert await poller.poll_once()
    assert service._last_seen.get("pico-192.168.1.50") is not None


@pytest.mark.asyncio
async def test_ingest_error_does_not_raise(service, monkeypatch):
    poller = make_poller(service, [make_body()])
    monkeypatch.setattr(service, "ingest", AsyncMock(side_effect=RuntimeError("db down")))
    assert await poller.poll_once()  # translation/ingest errors swallowed
