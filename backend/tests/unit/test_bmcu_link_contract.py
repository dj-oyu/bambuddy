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


def make_envelope(seq=1, link_id=None, kind="event", tseq=None, **link_extra):
    link = {
        "state": "online",
        "sequence": seq,  # BMCU u16 spelling (diagnostics)
        "transport_sequence": tseq if tseq is not None else seq,
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
    assert wm.transport_sequence == 3  # newest committed row, not the staged seq=4
    assert wm.pico_boot_session == "boot-a"

    await service.flush()
    keys = service.persisted_keys({("pico-1", "default")})
    assert keys[0].transport_sequence == 4


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


@pytest.mark.asyncio
async def test_bmcu_sequence_wrap_not_deduped(service):
    """Same BMCU u16 sequence (wrap / shared full-status request seq) must
    not dedup when transport_sequence differs (issue #2 rationale)."""
    envs = [
        BMCULinkEnvelope.model_validate(make_envelope(seq=50, tseq=100)),
        BMCULinkEnvelope.model_validate(make_envelope(seq=50, tseq=101)),
        BMCULinkEnvelope.model_validate(make_envelope(seq=50, tseq=100)),  # true dup
    ]
    result = await service.ingest(envs)
    assert result.accepted == 2
    assert result.deduplicated == 1


def test_transport_sequence_optional_legacy():
    legacy = make_envelope(seq=7)
    del legacy["link"]["transport_sequence"]
    env = BMCULinkEnvelope.model_validate(legacy)
    assert env.link.transport_sequence is None
    assert env.link.dedup_sequence == 7
    # neither sequence present → invalid
    import pydantic

    bad = make_envelope()
    del bad["link"]["transport_sequence"]
    del bad["link"]["sequence"]
    with pytest.raises(pydantic.ValidationError):
        BMCULinkEnvelope.model_validate(bad)


@pytest.mark.asyncio
async def test_partial_accept_rejected_codes(service):
    from backend.app.api.routes.bmcu_link import _ingest_partial
    import backend.app.api.routes.bmcu_link as routes_mod

    orig = routes_mod.bmcu_link_service
    routes_mod.bmcu_link_service = service
    try:
        batch = [
            make_envelope(seq=1, tseq=1),
            {"garbage": True},
            make_envelope(seq=2, tseq=2),
        ]
        result = await _ingest_partial(batch)
        assert result.accepted == 2
        assert len(result.rejected) == 1
        r = result.rejected[0]
        assert (r.index, r.code, r.retryable) == (1, "validation_error", False)
    finally:
        routes_mod.bmcu_link_service = orig


@pytest.mark.asyncio
async def test_partial_accept_internal_retryable(service, monkeypatch):
    from backend.app.api.routes.bmcu_link import _ingest_partial
    import backend.app.api.routes.bmcu_link as routes_mod

    async def boom(env):
        raise RuntimeError("staging failed")

    monkeypatch.setattr(service, "_ingest_one", boom)
    orig = routes_mod.bmcu_link_service
    routes_mod.bmcu_link_service = service
    try:
        result = await _ingest_partial([make_envelope(seq=1, tseq=9)])
        assert result.accepted == 0
        r = result.rejected[0]
        assert (r.index, r.transport_sequence, r.code, r.retryable) == (0, 9, "internal", True)
    finally:
        routes_mod.bmcu_link_service = orig


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


# ------------------------------------------- Phase 5 transport HELLO interop


def make_transport_hello(tseq=0):
    """Transport HELLO per the Phase 5 interop spec: frame.kind == "hello",
    link.id == "transport", no BMCU session (describes the Pico itself)."""
    return {
        "schema": "bmcu.management.v2",
        "registry_version": "alpha.3",
        "device_id": "pico-1",
        "mode": "production_monitor",
        "received_at_us": 100,
        "link": {
            "state": "online",
            "transport_sequence": tseq,
            "pico_boot_session": "boot-a",
            "id": "transport",
        },
        "frame": {"kind": "hello"},
        "data": {"firmware": "pico-0.5.0", "capabilities": ["telemetry"], "dropped_count": 0},
    }


def test_transport_hello_validates_without_bmcu_session():
    env = BMCULinkEnvelope.model_validate(make_transport_hello())
    assert env.link.bmcu_boot_session is None
    assert env.link.id == "transport"
    assert env.frame.kind == "hello"


@pytest.mark.asyncio
async def test_transport_hello_accepted_and_watermarked(service):
    """The spec requires link.id == "transport" to be a normal per-link
    watermark target: accepted, persisted-ACKed, device row upserted."""
    result = await service.ingest([BMCULinkEnvelope.model_validate(make_transport_hello(tseq=0))])
    assert result.accepted == 1 and result.rejected == []

    await service.flush()
    keys = service.persisted_keys({("pico-1", "transport")})
    assert len(keys) == 1
    assert keys[0].link_id == "transport"
    assert keys[0].transport_sequence == 0

    from backend.app.models.bmcu_link_device import BMCULinkDevice

    async with service._sessionmaker()() as db:
        device = (
            await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == "pico-1"))
        ).scalar_one()
        assert device.firmware == "pico-0.5.0"
        assert device.bmcu_boot_session is None  # not fabricated


@pytest.mark.asyncio
async def test_transport_hello_does_not_clobber_bmcu_session(service):
    """A BMCU-link hello sets bmcu_boot_session; a later transport HELLO
    (no BMCU session) must keep the last known value."""
    bmcu_hello = make_envelope(seq=1, kind="hello")
    await service.ingest([BMCULinkEnvelope.model_validate(bmcu_hello)])
    await service.ingest([BMCULinkEnvelope.model_validate(make_transport_hello(tseq=0))])

    from backend.app.models.bmcu_link_device import BMCULinkDevice

    async with service._sessionmaker()() as db:
        device = (
            await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == "pico-1"))
        ).scalar_one()
        assert device.bmcu_boot_session == 1


# ------------------------------ rejected[] identity (Pico apply_ack contract)


def test_rejected_carries_link_identity_on_validation_error():
    """The alpha Pico's apply_ack() only quarantines a non-retryable reject
    when the entry carries link_id AND pico_boot_session; without them the
    record stays queued and is resent forever. Emit both whenever the
    offending item still exposes them."""
    from backend.app.api.routes.bmcu_link import _parse_envelopes_partial

    bad = make_envelope(seq=5)
    bad["frame"] = "not-an-object"  # schema violation, link block intact
    parsed, rejected = _parse_envelopes_partial([bad])
    assert parsed == [] and len(rejected) == 1
    r = rejected[0]
    assert r.code == "validation_error" and r.retryable is False
    assert r.link_id == "default"
    assert r.pico_boot_session == "boot-a"
    assert r.transport_sequence == 5


def test_rejected_identity_absent_when_unrecoverable():
    """Garbage that never had a link block: identity stays None rather than
    being invented (the bridge then falls back to its own index mapping)."""
    from backend.app.api.routes.bmcu_link import _parse_envelopes_partial

    _, rejected = _parse_envelopes_partial([{"schema": "x"}, "not-a-dict"])
    assert len(rejected) == 2
    for r in rejected:
        assert r.link_id is None and r.pico_boot_session is None


@pytest.mark.asyncio
async def test_link_cap_reject_carries_identity(service):
    """Service-side rejects (device_cap / link_cap / internal) always have a
    validated envelope, so identity is never missing there."""
    service.MAX_LINKS_PER_DEVICE = 1
    envs = [
        BMCULinkEnvelope.model_validate(make_envelope(seq=1, link_id="a")),
        BMCULinkEnvelope.model_validate(make_envelope(seq=2, link_id="b")),
    ]
    result = await service.ingest(envs)
    caps = [r for r in result.rejected if r.code == "link_cap"]
    assert caps, "expected a link_cap rejection"
    assert caps[0].link_id == "b"
    assert caps[0].pico_boot_session == "boot-a"
