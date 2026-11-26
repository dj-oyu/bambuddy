import asyncio
import hashlib
import json
import struct
import time
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base
from backend.app.models.bmcu_binary import BMCUBinaryDevice, BMCUBinaryLossRange, BMCUBinaryRecord
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.framing import Frame, FrameHeader
from backend.app.services.bmcu_binary.messages import LinkStateMessage, TransportDrop
from backend.app.services.bmcu_binary.persistence import BinaryPersistence
from backend.app.services.bmcu_binary.stream_v2 import (
    HEARTBEAT,
    NETWORK_GAP,
    GapTracker,
    StreamReceiver,
    StreamState,
    parse_datagram,
    persist_batch,
    reconcile_losses,
    validate_frame,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bmcu_stream"


def test_pico_fixtures_have_expected_envelope_and_heartbeat():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        content = (FIXTURES / name).read_bytes()
        assert len(content) == expected["bytes"]
        assert hashlib.sha256(content).hexdigest() == expected["sha256"]
    device, frames = parse_datagram((FIXTURES / "heartbeat.bin").read_bytes())
    assert device == "pico-bmcu-bridge"
    beat = validate_frame(frames[0])
    assert (beat.interval_ms, beat.next_sequence, beat.firmware) == (5000, 4, "alpha.3")
    assert beat.links == ((0, "bmcu-a", 2), (1, "bmcu-b", 3))
    _, records = parse_datagram((FIXTURES / "records.bin").read_bytes())
    assert [f.header.message_type for f in records] == [17, 16, 19]
    assert [f.header.transport_sequence for f in records] == [1, 2, 3]


def test_truncated_datagram_is_rejected():
    with pytest.raises(ValueError):
        parse_datagram((FIXTURES / "records.bin").read_bytes()[:-1])


def test_liveness_uses_heartbeat_age():
    state = StreamState(heartbeat_interval=5, heartbeat_at=time.monotonic() - 5)
    assert state.health() == "online"
    state.heartbeat_at = time.monotonic() - 16
    assert state.health() == "stale"
    state.heartbeat_at = time.monotonic() - 61
    assert state.health() == "offline"


def beat(next_sequence=1):
    payload = struct.pack(">IQQB", 5000, 100, next_sequence, 4) + b"test" + b"\x01\x00\x01a\x02"
    return Frame(FrameHeader(HEARTBEAT, pico_boot_id=1, link_index=255), payload)


def record(sequence):
    return Frame(
        FrameHeader(MessageType.LINK_STATE, transport_sequence=sequence, pico_boot_id=1, link_index=0),
        LinkStateMessage(100, 2, 0).encode(),
    )


def item(frame):
    return frame, validate_frame(frame), "127.0.0.1", time.monotonic()


@pytest.mark.asyncio
async def test_batch_gap_late_arrival_and_drop_report(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stream.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence = BinaryPersistence(factory)
    states, counters, tracker = {}, Counter(), GapTracker(delay=2)
    initial = [item(beat()), item(record(1)), item(record(4))]
    await persist_batch(persistence, "pico", initial, states, counters)
    await reconcile_losses(persistence, "pico", tracker, initial, counters, now=10)
    await reconcile_losses(persistence, "pico", tracker, [], counters, now=13)
    async with factory() as db:
        losses = (await db.execute(select(BMCUBinaryLossRange))).scalars().all()
        assert [(int(x.first_sequence), int(x.last_sequence), x.reason) for x in losses] == [(2, 3, NETWORK_GAP)]
    late = [item(record(2))]
    await persist_batch(persistence, "pico", late, states, counters)
    await reconcile_losses(persistence, "pico", tracker, late, counters, now=14)
    drop = Frame(
        FrameHeader(MessageType.TRANSPORT_DROP, transport_sequence=3, pico_boot_id=1, link_index=255),
        TransportDrop(100, 3, 3, 1, 1).encode(),
    )
    await persist_batch(persistence, "pico", [item(drop)], states, counters)
    await reconcile_losses(persistence, "pico", tracker, [item(drop)], counters, now=15)
    async with factory() as db:
        losses = (await db.execute(select(BMCUBinaryLossRange))).scalars().all()
        assert [(int(x.first_sequence), int(x.last_sequence), x.reason) for x in losses] == [(3, 3, 1)]
        assert len((await db.execute(select(BMCUBinaryRecord))).scalars().all()) == 4
        assert (await db.execute(select(BMCUBinaryDevice))).scalar_one().firmware == "test"
    await engine.dispose()


@pytest.mark.asyncio
async def test_udp_first_heartbeat_registration_and_batch(tmp_path):
    from backend.app.services.bmcu_binary.live import live_broker

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'udp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    receiver = StreamReceiver(BinaryPersistence(factory), set(), accept_new=True, flush_seconds=0.01)
    wakeups = live_broker.subscribe("pico-bmcu-bridge")
    receiver.datagram_received((FIXTURES / "heartbeat.bin").read_bytes(), ("127.0.0.1", 8000))
    await asyncio.wait_for(receiver.queues["pico-bmcu-bridge"].join(), 3)
    assert receiver.states["pico-bmcu-bridge"].health() == "online"
    assert wakeups.qsize() == 1
    live_broker.unsubscribe("pico-bmcu-bridge", wakeups)
    async with factory() as db:
        assert (await db.execute(select(BMCUBinaryDevice))).scalar_one().firmware == "alpha.3"
    await receiver.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_receiver_overflow_records_loss(tmp_path):
    from backend.app.services.bmcu_binary.framing import encode_frame
    from backend.app.services.bmcu_binary.stream_v2 import RECEIVER_OVERFLOW

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'overflow.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    receiver = StreamReceiver(
        BinaryPersistence(factory), {"pico"}, queue_size=1, flush_seconds=0.01, reorder_seconds=0.01
    )

    def datagram(frame):
        return b"BMS2\x01\x00\x04pico\x01" + encode_frame(frame.header, frame.payload)

    receiver.datagram_received(datagram(beat()), ("127.0.0.1", 8000))
    receiver.datagram_received(datagram(record(1)), ("127.0.0.1", 8000))
    assert receiver.counters["ingest_overflow"] == 1
    await asyncio.wait_for(receiver.queues["pico"].join(), 3)
    await asyncio.sleep(0.1)
    async with factory() as db:
        losses = (await db.execute(select(BMCUBinaryLossRange))).scalars().all()
        assert [(int(x.first_sequence), x.reason) for x in losses] == [(1, RECEIVER_OVERFLOW)]
    await receiver.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_udp_socket_receives_and_persists_records(tmp_path):
    from backend.app.services.bmcu_binary.framing import encode_frame

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'socket.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    loop = asyncio.get_running_loop()
    receiver = StreamReceiver(BinaryPersistence(factory), {"pico"}, flush_seconds=0.01)
    transport, _ = await loop.create_datagram_endpoint(lambda: receiver, local_addr=("127.0.0.1", 0))
    sender, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, remote_addr=transport.get_extra_info("sockname")
    )
    for frame in (beat(), *(record(i) for i in range(1, 101))):
        sender.sendto(b"BMS2\x01\x00\x04pico\x01" + encode_frame(frame.header, frame.payload))
    await asyncio.sleep(0.1)
    await asyncio.wait_for(receiver.queues["pico"].join(), 5)
    async with factory() as db:
        assert len((await db.execute(select(BMCUBinaryRecord))).scalars().all()) == 100
    assert receiver.device_counters["pico"]["records_persisted"] == 100
    assert receiver.device_counters["pico"]["batches"] < 100
    sender.close()
    await receiver.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_reordered_status_does_not_replace_newer_live_state(tmp_path):
    from backend.app.services.bmcu_binary.bmcu_decoder import BMCUStatus
    from backend.app.services.bmcu_binary.framing import IncrementalFrameParser

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'status.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence, states, counters = BinaryPersistence(factory), {}, Counter()
    raw = (FIXTURES.parent / "bmcu_binary" / "bmcu_status.bin").read_bytes()
    original = IncrementalFrameParser().feed(raw)[0]

    def status(seq):
        return Frame(
            FrameHeader(MessageType.BMCU_FRAME, transport_sequence=seq, pico_boot_id=1, link_index=0), original.payload
        )

    delayed = (*item(status(10))[:3], time.monotonic() - 30)
    await persist_batch(persistence, "pico", [item(beat()), delayed], states, counters)
    assert isinstance(persistence.current_state[("pico", 0)], BMCUStatus)
    last_time = persistence.current_state_seen[("pico", 0)]
    assert time.monotonic() - last_time >= 30
    await persist_batch(persistence, "pico", [item(status(9))], states, counters)
    assert persistence.current_state_seen[("pico", 0)] == last_time
    await engine.dispose()


@pytest.mark.asyncio
async def test_pending_gap_survives_receiver_restart(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence, counters = BinaryPersistence(factory), Counter()
    tracker = GapTracker(delay=0.02)
    await persist_batch(
        persistence, "pico", [item(beat()), item(record(1)), item(record(3))], {}, counters, tracker=tracker
    )
    async with factory() as db:
        assert not (await db.execute(select(BMCUBinaryLossRange))).scalars().all()
    await asyncio.sleep(0.03)
    receiver = StreamReceiver(persistence, {"pico"}, flush_seconds=0.01, reorder_seconds=0.02)
    await receiver.restore_pending()
    for _ in range(100):
        async with factory() as db:
            losses = (await db.execute(select(BMCUBinaryLossRange))).scalars().all()
        if losses:
            break
        await asyncio.sleep(0.01)
    assert [(int(x.first_sequence), int(x.last_sequence), x.reason) for x in losses] == [(2, 2, NETWORK_GAP)]
    await receiver.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_one_bad_embedded_frame_does_not_discard_other_records(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'malformed.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    receiver = StreamReceiver(BinaryPersistence(factory), {"pico-bmcu-bridge"}, flush_seconds=0.01)
    receiver.datagram_received((FIXTURES / "heartbeat.bin").read_bytes(), ("127.0.0.1", 8000))
    receiver.datagram_received((FIXTURES / "records.bin").read_bytes(), ("127.0.0.1", 8000))
    await asyncio.wait_for(receiver.queues["pico-bmcu-bridge"].join(), 3)
    # The shared fixture's EVENT has a CRC that does not validate; the
    # LINK_STATE and diagnostic alongside it must still be stored.
    assert receiver.device_counters["pico-bmcu-bridge"]["record_rejects"] == 1
    async with factory() as db:
        rows = (await db.execute(select(BMCUBinaryRecord))).scalars().all()
        assert {row.message_type for row in rows} == {MessageType.LINK_STATE, MessageType.PICO_DIAGNOSTIC}
    await receiver.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_existing_device_accepts_status_before_new_boot_heartbeat(tmp_path):
    from backend.app.services.bmcu_binary.bmcu_decoder import BMCUStatus
    from backend.app.services.bmcu_binary.framing import IncrementalFrameParser

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence, states, counters = BinaryPersistence(factory), {}, Counter()
    await persist_batch(persistence, "pico", [item(beat())], states, counters)
    original = IncrementalFrameParser().feed((FIXTURES.parent / "bmcu_binary" / "bmcu_status.bin").read_bytes())[0]
    new_frame = Frame(
        FrameHeader(MessageType.BMCU_FRAME, transport_sequence=1, pico_boot_id=2, link_index=0), original.payload
    )
    await persist_batch(persistence, "pico", [item(new_frame)], states, counters)
    assert states["pico"].boot_id == 2
    assert isinstance(persistence.current_state[("pico", 0)], BMCUStatus)
    async with factory() as db:
        assert (await db.execute(select(BMCUBinaryDevice))).scalar_one().pico_boot_id == "0000000000000002"
    await engine.dispose()


@pytest.mark.asyncio
async def test_pico_log_received_over_udp_is_saved_and_returned_by_api(tmp_path):
    from backend.app.api.routes.bmcu_monitors import logs
    from backend.app.services.bmcu_binary.framing import IncrementalFrameParser, encode_frame
    from backend.app.services.bmcu_binary.messages import PicoLog

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'log.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    receiver = StreamReceiver(BinaryPersistence(factory), {"pico"}, flush_seconds=0.01)
    original = IncrementalFrameParser().feed((FIXTURES.parent / "bmcu_binary" / "pico_log_utf8.bin").read_bytes())[0]
    frame = Frame(
        FrameHeader(MessageType.PICO_LOG, transport_sequence=1, pico_boot_id=1, link_index=255), original.payload
    )
    for current in (beat(), frame):
        receiver.datagram_received(
            b"BMS2\x01\x00\x04pico\x01" + encode_frame(current.header, current.payload), ("127.0.0.1", 8000)
        )
    await asyncio.wait_for(receiver.queues["pico"].join(), 3)
    async with factory() as db:
        result = await logs("pico", severity=None, component=None, limit=50, db=db, _=None)
    expected = PicoLog.decode(frame.payload)
    assert [(r["component"], r["message"], r["severity"]) for r in result] == [
        (expected.component, expected.message, expected.severity)
    ]
    await receiver.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_diagnostic_counters_ignore_late_frames_and_reset_on_reboot(tmp_path):
    from backend.app.services.bmcu_binary.messages import TLV, encode_tlvs

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'diag.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence, states, counters = BinaryPersistence(factory), {}, Counter()

    def diagnostic(seq, value):
        payload = encode_tlvs((TLV(90, 4, value.to_bytes(8, "big")),))
        return Frame(
            FrameHeader(MessageType.PICO_DIAGNOSTIC, transport_sequence=seq, pico_boot_id=1, link_index=255), payload
        )

    await persist_batch(persistence, "pico", [item(beat()), item(diagnostic(10, 100))], states, counters)
    await persist_batch(persistence, "pico", [item(diagnostic(9, 50))], states, counters)
    assert states["pico"].pico_metrics["datagramsSent"] == 100
    restart = Frame(FrameHeader(HEARTBEAT, pico_boot_id=2, link_index=255), beat().payload)
    await persist_batch(persistence, "pico", [item(restart)], states, counters)
    assert states["pico"].pico_metrics == {}
    await engine.dispose()


def test_link_continuity_restarts_after_loss_or_silent_heartbeat_gap():
    from backend.app.services.bmcu_binary.stream_v2 import Heartbeat, StreamState

    state = StreamState(heartbeat_interval=5)

    def observe(code, timestamp):
        beat = Heartbeat(5000, 1, 1, "test", ((0, "a", code), (1, "b", 2)))
        state.observe_links(beat, timestamp)
        state.heartbeat_at = timestamp

    observe(2, 10)
    observe(4, 11)
    observe(2, 12)
    assert state.link_online_since == {0: 12, 1: 10}
    observe(2, 33)
    assert state.link_online_since == {0: 33, 1: 33}
