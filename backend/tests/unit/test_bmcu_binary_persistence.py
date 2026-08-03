import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.database import Base
from backend.app.models.bmcu_binary import (
    BMCUBinaryBoot,
    BMCUBinaryDevice,
    BMCUBinaryLossRange,
    BMCUBinaryRecord,
)
from backend.app.services.bmcu_binary import persistence as persistence_module
from backend.app.services.bmcu_binary.bmcu_decoder import BMCUEvent, BMCUStatus, crc16_ccitt_false
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.errors import BinaryProtocolError
from backend.app.services.bmcu_binary.framing import Frame, FrameHeader, IncrementalFrameParser
from backend.app.services.bmcu_binary.messages import (
    Hello,
    HelloLink,
    HelloReplayRange,
    TransportDrop,
    ValidatedBMCUFrame,
    encode_bmcu_frame,
)
from backend.app.services.bmcu_binary.persistence import BinaryPersistence
from backend.app.services.bmcu_binary.storage_keys import u64_decimal, u64_hex

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bmcu_binary"


@pytest.mark.asyncio
async def test_drop_range_advances_global_ack_idempotently(tmp_path) -> None:
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bmcu.db'}")
    async with test_engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[table for table in Base.metadata.sorted_tables if table.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    persistence = BinaryPersistence(factory)
    status = IncrementalFrameParser().feed((FIXTURES / "bmcu_status.bin").read_bytes())[0]
    boot = status.header.pico_boot_id
    hello = Hello(
        "monitor-drop",
        "1",
        (HelloLink(0, "bmcu-a"),),
        (HelloReplayRange(boot, 1, 1),),
        b"x" * 32,
    )
    assert await persistence.register_hello(hello, boot) == 0
    status = type(status)(
        FrameHeader(
            status.header.message_type,
            status.header.flags,
            len(status.payload),
            1,
            boot,
            status.header.link_index,
        ),
        status.payload,
    )
    assert await persistence.persist("monitor-drop", status) == 1
    drop_payload = TransportDrop(100, 2, 4, 3, 1).encode()
    drop = type(status)(
        FrameHeader(
            MessageType.TRANSPORT_DROP,
            payload_length=len(drop_payload),
            transport_sequence=2,
            pico_boot_id=boot,
            link_index=0xFF,
        ),
        drop_payload,
    )
    assert await persistence.persist("monitor-drop", drop) == 4
    assert await persistence.persist("monitor-drop", drop) == 4
    async with factory() as db:
        stored_boot = (await db.execute(select(BMCUBinaryBoot))).scalar_one()
        stored_device = (await db.execute(select(BMCUBinaryDevice))).scalar_one()
        assert int(stored_boot.newest_available_sequence) == 4
        assert int(stored_device.newest_available_sequence) == 4
        assert len((await db.execute(select(BMCUBinaryRecord))).scalars().all()) == 2
        assert len((await db.execute(select(BMCUBinaryLossRange))).scalars().all()) == 1

    next_boot = boot + 1
    next_hello = Hello(
        "monitor-drop",
        "1",
        (HelloLink(0, "bmcu-a"),),
        (
            HelloReplayRange(next_boot, 1, 1),
            HelloReplayRange(boot, 1, 4),
        ),
        b"x" * 32,
    )
    assert await persistence.register_hello(next_hello, next_boot) == 0
    historical_payload = TransportDrop(200, 4, 5, 2, 1).encode()
    historical_drop = type(status)(
        FrameHeader(
            MessageType.TRANSPORT_DROP,
            payload_length=len(historical_payload),
            transport_sequence=4,
            pico_boot_id=boot,
            link_index=0xFF,
        ),
        historical_payload,
    )
    with pytest.raises(ValueError, match="exceeds HELLO advertised range"):
        await persistence.persist("monitor-drop", historical_drop)
    await test_engine.dispose()


def _frame(name, sequence, boot=None, link=0):
    frame = IncrementalFrameParser().feed((FIXTURES / name).read_bytes())[0]
    return type(frame)(
        FrameHeader(
            frame.header.message_type,
            frame.header.flags,
            len(frame.payload),
            sequence,
            boot if boot is not None else frame.header.pico_boot_id,
            link,
        ),
        frame.payload,
    )


async def _persistence(tmp_path, device_id, newest=10):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bmcu.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[table for table in Base.metadata.sorted_tables if table.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    persistence = BinaryPersistence(factory)
    boot = IncrementalFrameParser().feed((FIXTURES / "bmcu_status.bin").read_bytes())[0].header.pico_boot_id
    hello = Hello(device_id, "1", (HelloLink(0, "bmcu-a"),), (HelloReplayRange(boot, 1, newest),), b"x" * 32)
    await persistence.register_hello(hello, boot)
    return engine, factory, persistence, boot


@pytest.mark.asyncio
async def test_event_frame_does_not_erase_status_snapshot(tmp_path) -> None:
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-state")
    await persistence.persist("monitor-state", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-state", _frame("bmcu_event.bin", 2, boot))
    state = persistence.current_state[("monitor-state", 0)]
    assert isinstance(state, BMCUStatus)
    assert state.pull_pct == (19, 20, 21, 22)
    assert isinstance(persistence.last_event[("monitor-state", 0)], BMCUEvent)
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_status_record_does_not_erase_status_snapshot(tmp_path) -> None:
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-full")
    await persistence.persist("monitor-full", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-full", _frame("bmcu_full_status.bin", 2, boot))
    assert isinstance(persistence.current_state[("monitor-full", 0)], BMCUStatus)
    await engine.dispose()


@pytest.mark.asyncio
async def test_strict_status_state_toggle_restores_last_frame_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BAMBUDDY_BMCU_STRICT_STATUS_STATE", "0")
    monkeypatch.setattr(persistence_module, "_STRICT_STATE", False)
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-loose")
    await persistence.persist("monitor-loose", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-loose", _frame("bmcu_event.bin", 2, boot))
    assert isinstance(persistence.current_state[("monitor-loose", 0)], BMCUEvent)
    await engine.dispose()


@pytest.mark.asyncio
async def test_rehydrate_skips_non_status_newest_row(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-rehydrate")
    await persistence.persist("monitor-rehydrate", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-rehydrate", _frame("bmcu_event.bin", 2, boot))
    fresh = BinaryPersistence(factory)
    await fresh.rehydrate_current_state()
    assert isinstance(fresh.current_state[("monitor-rehydrate", 0)], BMCUStatus)
    await engine.dispose()


@pytest.mark.asyncio
async def test_rehydrate_survives_corrupt_stored_frame(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-corrupt")
    await persistence.persist("monitor-corrupt", _frame("bmcu_status.bin", 1, boot))
    async with factory() as db:
        db.add(
            BMCUBinaryRecord(
                device_id="monitor-corrupt",
                pico_boot_id=u64_hex(boot),
                transport_sequence=u64_decimal(9),
                link_index=1,
                flags=0,
                message_type=MessageType.BMCU_FRAME,
                received_at_us=u64_decimal(1),
                server_received_at=datetime(2026, 8, 3),
                bmcu_kind=2,
                raw_payload=b"\x00",
                raw_bmcu_frame=b"garbage-not-a-frame",
            )
        )
        await db.commit()
    fresh = BinaryPersistence(factory)
    await fresh.rehydrate_current_state()
    assert isinstance(fresh.current_state[("monitor-corrupt", 0)], BMCUStatus)
    assert ("monitor-corrupt", 1) not in fresh.current_state
    await engine.dispose()


@pytest.mark.asyncio
async def test_transport_drop_out_of_range_raises_protocol_error(tmp_path) -> None:
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-drop-error", newest=1)
    await persistence.persist("monitor-drop-error", _frame("bmcu_status.bin", 1, boot))
    other_boot = boot + 1
    hello = Hello(
        "monitor-drop-error",
        "1",
        (HelloLink(0, "bmcu-a"),),
        (HelloReplayRange(other_boot, 1, 1), HelloReplayRange(boot, 1, 1)),
        b"x" * 32,
    )
    await persistence.register_hello(hello, other_boot)
    payload = TransportDrop(200, 4, 5, 2, 1).encode()
    status = IncrementalFrameParser().feed((FIXTURES / "bmcu_status.bin").read_bytes())[0]
    drop = type(status)(
        FrameHeader(
            MessageType.TRANSPORT_DROP,
            payload_length=len(payload),
            transport_sequence=4,
            pico_boot_id=boot,
            link_index=0xFF,
        ),
        payload,
    )
    with pytest.raises(BinaryProtocolError, match="exceeds HELLO advertised range"):
        await persistence.persist("monitor-drop-error", drop)
    await engine.dispose()


def _bmcu_hello_frame(sequence, boot, link=0):
    """BMCU HELLO (kind 1) — no fixture file exists for it."""
    payload = struct.pack("<BHBBI", 3, 0x00FF, 1, 4, 1_000_000)
    body = bytes((0x83, 1)) + (1).to_bytes(2, "little") + bytes((len(payload),)) + payload
    wire = b"\xa5\x5a" + body + crc16_ccitt_false(body).to_bytes(2, "little")
    body = encode_bmcu_frame(ValidatedBMCUFrame(200_000 + sequence, wire))
    return Frame(
        FrameHeader(MessageType.BMCU_FRAME, 0, len(body), sequence, boot, link),
        body,
    )


@pytest.mark.asyncio
async def test_rehydrate_restores_hello_and_epoch(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-hello")
    await persistence.persist("monitor-hello", _bmcu_hello_frame(1, boot))
    await persistence.persist("monitor-hello", _frame("bmcu_status.bin", 2, boot))
    fresh = BinaryPersistence(factory)
    await fresh.rehydrate_current_state()
    hello = fresh.last_hello[("monitor-hello", 0)]
    assert (hello.protocol_version, hello.capabilities) == (3, 0x00FF)
    # HELLO count is the stand-in boot session; it must survive a restart.
    assert fresh.bmcu_hello_epoch["monitor-hello"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_rehydrate_does_not_let_a_busy_link_starve_a_quiet_one(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persistence_module, "_REHYDRATE_SCAN_LIMIT", 1)
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-links", newest=10)
    await persistence.persist("monitor-links", _frame("bmcu_status.bin", 1, boot, link=1))
    for sequence in (2, 3, 4):
        await persistence.persist("monitor-links", _frame("bmcu_status.bin", sequence, boot, link=0))
    fresh = BinaryPersistence(factory)
    await fresh.rehydrate_current_state()
    assert isinstance(fresh.current_state[("monitor-links", 0)], BMCUStatus)
    assert isinstance(fresh.current_state[("monitor-links", 1)], BMCUStatus)
    await engine.dispose()


@pytest.mark.asyncio
async def test_rehydrated_state_carries_the_records_real_age(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-age")
    await persistence.persist("monitor-age", _frame("bmcu_status.bin", 1, boot))
    async with factory() as db:
        row = (await db.execute(select(BMCUBinaryRecord))).scalars().one()
        row.server_received_at = persistence_module._utcnow() - timedelta(hours=2)
        await db.commit()
    fresh = BinaryPersistence(factory)
    await fresh.rehydrate_current_state()
    age_s = time.monotonic() - fresh.current_state_seen[("monitor-age", 0)]
    # An hours-old record must not be presented as a fresh reading after a restart.
    assert age_s > 7000
    await engine.dispose()


def _drop_frame(boot, first, last, count, reason=1):
    """The protocol requires the header sequence to be the first dropped one."""
    payload = TransportDrop(100 + first, first, last, count, reason).encode()
    return Frame(
        FrameHeader(MessageType.TRANSPORT_DROP, 0, len(payload), first, boot, 0xFF),
        payload,
    )


async def _loss_rows(factory):
    async with factory() as db:
        return (await db.execute(select(BMCUBinaryLossRange).order_by(BMCUBinaryLossRange.id))).scalars().all()


@pytest.mark.asyncio
async def test_contiguous_drop_reports_merge_into_one_range(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-merge", newest=40)
    await persistence.persist("monitor-merge", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-merge", _drop_frame(boot, 10, 13, 4))
    await persistence.persist("monitor-merge", _drop_frame(boot, 14, 17, 4))
    await persistence.persist("monitor-merge", _drop_frame(boot, 18, 20, 3))
    rows = await _loss_rows(factory)
    assert len(rows) == 1
    assert (int(rows[0].first_sequence), int(rows[0].last_sequence)) == (10, 20)
    assert int(rows[0].dropped_count) == 11
    await engine.dispose()


@pytest.mark.asyncio
async def test_non_contiguous_drop_report_starts_a_new_range(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-gap", newest=40)
    await persistence.persist("monitor-gap", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-gap", _drop_frame(boot, 10, 13, 4))
    await persistence.persist("monitor-gap", _drop_frame(boot, 20, 22, 3))
    rows = await _loss_rows(factory)
    assert [(int(row.first_sequence), int(row.last_sequence)) for row in rows] == [(10, 13), (20, 22)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_overlapping_re_report_does_not_inflate_the_dropped_count(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-overlap", newest=40)
    await persistence.persist("monitor-overlap", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-overlap", _drop_frame(boot, 10, 15, 6))
    await persistence.persist("monitor-overlap", _drop_frame(boot, 13, 18, 6))
    rows = await _loss_rows(factory)
    assert len(rows) == 1
    assert (int(rows[0].first_sequence), int(rows[0].last_sequence)) == (10, 18)
    assert int(rows[0].dropped_count) == 9
    await engine.dispose()


@pytest.mark.asyncio
async def test_merge_toggle_keeps_one_row_per_report(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persistence_module, "_MERGE_LOSS_RANGES", False)
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-nomerge", newest=40)
    await persistence.persist("monitor-nomerge", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-nomerge", _drop_frame(boot, 10, 13, 4))
    await persistence.persist("monitor-nomerge", _drop_frame(boot, 14, 17, 4))
    assert len(await _loss_rows(factory)) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_status_global_refreshes_the_realtime_view(tmp_path) -> None:
    """A bridge can stop sending STATUS while its periodic snapshot keeps
    arriving; GLOBAL carries the same fields, so the view must follow it."""
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-global")
    await persistence.persist("monitor-global", _frame("bmcu_status.bin", 1, boot))
    stale = persistence.current_state[("monitor-global", 0)]
    await persistence.persist("monitor-global", _frame("bmcu_full_status_global.bin", 2, boot))
    state = persistence.current_state[("monitor-global", 0)]
    assert isinstance(state, BMCUStatus)
    assert state is not stale
    assert (state.current_slot, state.inserted_mask, state.online_mask) == (0, 15, 11)
    assert state.motion == (2, 0, 0, 0)
    assert state.pull_pct == (59, 46, 56, 53)
    await engine.dispose()


@pytest.mark.asyncio
async def test_counters_record_completes_the_snapshot_it_belongs_to(tmp_path) -> None:
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-counters")
    await persistence.persist("monitor-counters", _frame("bmcu_full_status_global.bin", 1, boot))
    assert persistence.current_state[("monitor-counters", 0)].crc_error == 0
    await persistence.persist("monitor-counters", _frame("bmcu_full_status_counters.bin", 2, boot))
    state = persistence.current_state[("monitor-counters", 0)]
    assert (state.tx_drop, state.rx_drop, state.crc_error, state.frame_error) == (175, 0, 197, 777)
    # The loader values still come from GLOBAL; COUNTERS only fills the totals.
    assert state.motion == (2, 0, 0, 0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_counters_from_another_snapshot_does_not_redate_the_view(tmp_path) -> None:
    """Re-dating an older GLOBAL would present a stale loader view as current,
    which is the bug this whole path exists to avoid."""
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-mismatch")
    await persistence.persist("monitor-mismatch", _frame("bmcu_full_status_global.bin", 1, boot))
    key = ("monitor-mismatch", 0)
    seen = persistence.current_state_seen[key]
    persistence.full_status_global[key] = replace(persistence.full_status_global[key], snapshot_id=4097)
    await persistence.persist("monitor-mismatch", _frame("bmcu_full_status_counters.bin", 2, boot))
    assert persistence.current_state_seen[key] == seen
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_status_state_toggle_leaves_the_status_view_frozen(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persistence_module, "_FULL_STATUS_STATE", False)
    engine, _factory, persistence, boot = await _persistence(tmp_path, "monitor-frozen")
    await persistence.persist("monitor-frozen", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-frozen", _frame("bmcu_full_status_global.bin", 2, boot))
    assert persistence.current_state[("monitor-frozen", 0)].pull_pct == (19, 20, 21, 22)
    await engine.dispose()


@pytest.mark.asyncio
async def test_rehydrate_prefers_a_snapshot_newer_than_the_last_status(tmp_path) -> None:
    engine, factory, persistence, boot = await _persistence(tmp_path, "monitor-rehydrate")
    await persistence.persist("monitor-rehydrate", _frame("bmcu_status.bin", 1, boot))
    await persistence.persist("monitor-rehydrate", _frame("bmcu_full_status_global.bin", 2, boot))
    await persistence.persist("monitor-rehydrate", _frame("bmcu_full_status_counters.bin", 3, boot))
    restored = BinaryPersistence(factory)
    await restored.rehydrate_current_state()
    state = restored.current_state[("monitor-rehydrate", 0)]
    assert state.pull_pct == (59, 46, 56, 53)
    assert state.crc_error == 197
    await engine.dispose()
