"""PICO_BAMBUDDY_ENVELOPE.md (alpha.3) contract-alignment tests:
sequence alias, link.id in the dedup key, persisted watermark, transport_drop,
firmware enum-registry format."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.bmcu_link_event import BMCULinkEvent
from backend.app.schemas.bmcu_link import BMCULinkEnvelope
from backend.app.services.bmcu_link import BMCULinkService, get_enum_registry


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
    svc._printing_printers = lambda: []
    return svc


def make_envelope(seq=1, link_id=None, kind="event", **link_extra):
    link = {
        "state": "online",
        "sequence": seq,  # contract spelling
        "pico_boot_session": "boot-a",
        "bmcu_boot_session": 1,
        **link_extra,
    }
    if link_id is not None:
        link["id"] = link_id
    return {
        "schema": "bmcu.management.v2",
        "registry_version": "alpha.3",
        "device_id": "pico-1",
        "mode": "production_monitor",
        "received_at_us": 1000 + seq,
        "link": link,
        "frame": {"kind": kind, "kind_id": 3, "protocol": 131},
        "data": {"x": seq},
    }


# ----------------------------------------------------------------- schema


def test_sequence_alias_both_spellings():
    env = BMCULinkEnvelope.model_validate(make_envelope(seq=42))
    assert env.link.uart_sequence == 42
    assert env.link.id == "default"
    old = make_envelope()
    old["link"].pop("sequence")
    old["link"]["uart_sequence"] = 7
    env = BMCULinkEnvelope.model_validate(old)
    assert env.link.uart_sequence == 7


def test_link_id_and_queue_depth():
    env = BMCULinkEnvelope.model_validate(make_envelope(link_id="bmcu-2", queue_depth=5))
    assert env.link.id == "bmcu-2"
    assert env.link.queue_depth == 5
    assert env.mode == "production_monitor"
    assert env.registry_version == "alpha.3"


# ------------------------------------------------------------------ dedup


@pytest.mark.asyncio
async def test_same_sequence_different_link_not_deduped(service):
    envs = [
        BMCULinkEnvelope.model_validate(make_envelope(seq=1, link_id="a")),
        BMCULinkEnvelope.model_validate(make_envelope(seq=1, link_id="b")),
        BMCULinkEnvelope.model_validate(make_envelope(seq=1, link_id="a")),  # dup
    ]
    result = await service.ingest(envs)
    assert result.accepted == 2
    assert result.deduplicated == 1


# ------------------------------------------------------------- watermark


@pytest.mark.asyncio
async def test_persisted_watermark_advances_on_flush(service):
    envs = [BMCULinkEnvelope.model_validate(make_envelope(seq=s)) for s in (1, 2, 3)]
    result = await service.ingest(envs)
    assert result.persisted == []  # nothing flushed yet

    await service.flush()
    result = await service.ingest([BMCULinkEnvelope.model_validate(make_envelope(seq=4))])
    assert len(result.persisted) == 1
    wm = result.persisted[0]
    assert wm.link_id == "default"
    assert wm.sequence == 3  # newest committed row, not the staged seq=4
    assert wm.pico_boot_session == "boot-a"
    assert wm.bmcu_boot_session == 1

    await service.flush()
    keys = service.persisted_keys({("pico-1", "default")})
    assert keys[0].sequence == 4


@pytest.mark.asyncio
async def test_watermark_not_advanced_on_failed_flush(service, monkeypatch):
    envs = [BMCULinkEnvelope.model_validate(make_envelope(seq=1))]
    await service.ingest(envs)

    class Boom:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(service, "_session_factory", Boom())
    await service.flush()  # swallowed, one retry queued
    assert service.persisted_keys({("pico-1", "default")}) == []


@pytest.mark.asyncio
async def test_link_id_stored_in_rows(service):
    await service.ingest([BMCULinkEnvelope.model_validate(make_envelope(seq=1, link_id="bmcu-2"))])
    await service.flush()
    async with service._sessionmaker()() as db:
        row = (await db.execute(select(BMCULinkEvent))).scalars().one()
        assert row.link_id == "bmcu-2"
        assert row.uart_sequence == 1


@pytest.mark.asyncio
async def test_link_cap_per_device(service):
    for i in range(BMCULinkService.MAX_LINKS_PER_DEVICE):
        r = await service.ingest([BMCULinkEnvelope.model_validate(make_envelope(seq=1, link_id=f"l{i}"))])
        assert r.accepted == 1
    r = await service.ingest([BMCULinkEnvelope.model_validate(make_envelope(seq=1, link_id="overflow"))])
    assert r.accepted == 0
    # Existing links keep working
    r = await service.ingest([BMCULinkEnvelope.model_validate(make_envelope(seq=2, link_id="l0"))])
    assert r.accepted == 1


@pytest.mark.asyncio
async def test_watermark_frozen_after_pending_drop(service):
    service.PENDING_CAP = 2
    envs = [BMCULinkEnvelope.model_validate(make_envelope(seq=s)) for s in (1, 2, 3)]
    await service.ingest(envs)  # seq=1 dropped by cap, 2..3 staged
    await service.flush()
    # Watermark must not advance past the seq=1 gap in this boot session.
    assert service.persisted_keys({("pico-1", "default")}) == []
    # Replay of the dropped seq=1 must not be deduplicated.
    r = await service.ingest([BMCULinkEnvelope.model_validate(make_envelope(seq=1))])
    assert r.accepted == 1 and r.deduplicated == 0

    # A new boot session clears the gap; watermark advances again.
    new = make_envelope(seq=1)
    new["link"]["pico_boot_session"] = "boot-b"
    await service.ingest([BMCULinkEnvelope.model_validate(new)])
    await service.flush()
    keys = service.persisted_keys({("pico-1", "default")})
    assert keys and keys[0].pico_boot_session == "boot-b"


# --------------------------------------------------------- transport_drop


@pytest.mark.asyncio
async def test_transport_drop_counts_dropped(service, ws_mock):
    env = make_envelope(seq=1, kind="transport_drop")
    env["data"] = {"dropped_count": 7, "reason": "queue_overflow"}
    await service.ingest([BMCULinkEnvelope.model_validate(env)])
    assert service._dropped_session["pico-1"][1] == 7
    anomalies = [
        c.args[0] for c in ws_mock.broadcast.await_args_list if c.args[0].get("type") == "bmcu_link_anomaly"
    ]
    assert anomalies and anomalies[0]["kind"] == "transport_drop"


# ------------------------------------------------------------- registry


def test_bundled_registry_flattened():
    reg = get_enum_registry()
    assert reg["registry_version"] == "alpha.3"
    assert reg["kind"]["2"] == "status"
    assert reg["severity"]["5"] == "critical"
    assert reg["sensor_validity"]["4"] == "fault"
    assert "enums" not in reg


def test_flat_override_registry_still_supported(tmp_path, monkeypatch):
    (tmp_path / "bmcu_link_enums.json").write_text('{"registry_version": 1, "kind": {"0": "hello"}}')
    monkeypatch.setattr("backend.app.services.bmcu_link.settings.base_dir", tmp_path)
    reg = get_enum_registry()
    assert reg == {"registry_version": 1, "kind": {"0": "hello"}}
