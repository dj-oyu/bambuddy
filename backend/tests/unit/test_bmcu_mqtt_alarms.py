"""BMCU alarm persistence and confirmed MQTT delivery."""

import json
import struct
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base
from backend.app.models.bmcu_binary import BMCUBinaryAlarmOutbox, BMCUBinaryRecord
from backend.app.services.bmcu_binary.alarms import AlarmDelivery, make_alarm, publish_confirmed
from backend.app.services.bmcu_binary.bmcu_decoder import BMCUEvent, crc16_ccitt_false
from backend.app.services.bmcu_binary.constants import Flag, MessageType, RecordType, StateField
from backend.app.services.bmcu_binary.framing import Frame, FrameHeader
from backend.app.services.bmcu_binary.messages import ValidatedBMCUFrame, encode_bmcu_frame
from backend.app.services.bmcu_binary.persistence import BinaryPersistence
from backend.app.services.bmcu_binary.retention import prune_once
from backend.app.services.bmcu_binary.stream_v2 import persist_batch, validate_frame


def motion_frame(seq, value):
    union = (
        bytes((StateField.MOTION_FAULT, 1)) + (1 - value).to_bytes(2, "little") + value.to_bytes(2, "little") + b"\0\0"
    )
    payload = struct.pack("<IBBBB", 42, RecordType.STATE_CHANGE, 4 if value else 2, 5, 6) + union
    body = bytes((0x83, 3)) + seq.to_bytes(2, "little") + bytes((len(payload),)) + payload
    wire = b"\xa5\x5a" + body + crc16_ccitt_false(body).to_bytes(2, "little")
    wrapped = encode_bmcu_frame(ValidatedBMCUFrame(200_000, wire))
    return Frame(FrameHeader(MessageType.BMCU_FRAME, transport_sequence=seq, pico_boot_id=1, link_index=0), wrapped)


def heartbeat():
    from backend.app.services.bmcu_binary.stream_v2 import HEARTBEAT

    payload = struct.pack(">IQQB", 5000, 100, 1, 4) + b"test" + b"\x01\x00\x01a\x02"
    return Frame(FrameHeader(HEARTBEAT, pico_boot_id=1, link_index=255), payload)


def item(frame):
    import time

    return (frame, validate_frame(frame), "127.0.0.1", time.monotonic())


@pytest.mark.asyncio
async def test_motion_transitions_are_committed_with_source_and_deduplicated(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alarm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence, states = BinaryPersistence(factory), {}
    batch = [item(heartbeat()), item(motion_frame(1, 1)), item(motion_frame(2, 0))]
    await persist_batch(persistence, "pico", batch, states, Counter())
    await persist_batch(persistence, "pico", batch, states, Counter())
    async with factory() as db:
        alarms = (
            (await db.execute(select(BMCUBinaryAlarmOutbox).order_by(BMCUBinaryAlarmOutbox.transport_sequence)))
            .scalars()
            .all()
        )
        records = (await db.execute(select(BMCUBinaryRecord))).scalars().all()
        assert len(alarms) == len(records) == 2
        assert [json.loads(row.payload)["active"] for row in alarms] == [True, False]
        assert all(row.delivered_at is None for row in alarms)
        assert len({row.event_id for row in alarms}) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_tcp_replay_is_stored_without_generating_a_new_alarm(tmp_path):
    from backend.app.services.bmcu_binary.messages import Hello, HelloLink, HelloReplayRange

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence = BinaryPersistence(factory)
    hello = Hello("pico", "1", (HelloLink(0, "a"),), (HelloReplayRange(1, 1, 2),), b"x" * 32)
    await persistence.register_hello(hello, 1)
    frame = motion_frame(1, 1)
    await persistence.persist("pico", Frame(replace(frame.header, flags=int(Flag.REPLAY)), frame.payload))
    await persistence.persist("pico", motion_frame(2, 1))
    async with factory() as db:
        assert len((await db.execute(select(BMCUBinaryRecord))).scalars().all()) == 2
        alarms = (await db.execute(select(BMCUBinaryAlarmOutbox))).scalars().all()
        assert len(alarms) == 1
        assert json.loads(alarms[0].payload)["transport_sequence"] == "00000000000000000002"
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_keeps_event_until_confirmed_puback(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'send.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    event = BMCUEvent(0, RecordType.STATE_CHANGE, 4, 5, b"", StateField.MOTION_FAULT, 1, 0, 1)
    row = make_alarm("pico", motion_frame(1, 1), event, datetime.now(UTC).replace(tzinfo=None))
    async with factory() as db:
        db.add(row)
        await db.commit()
    info = SimpleNamespace(rc=0, wait_for_publish=Mock(), is_published=Mock(return_value=False))
    client = SimpleNamespace(publish=Mock(return_value=info))
    relay = SimpleNamespace(enabled=True, connected=True, client=client, topic_prefix="bambuddy")
    worker = AlarmDelivery(factory, relay)
    assert not await worker.deliver_one()
    async with factory() as db:
        assert (await db.get(BMCUBinaryAlarmOutbox, row.event_id)).delivered_at is None
    info.is_published.return_value = True
    assert await worker.deliver_one()
    assert not await worker.deliver_one()
    # Waiting again on the same in-flight packet must not accumulate Paho
    # messages when the broker connection stays open but PUBACK is delayed.
    assert client.publish.call_count == 1
    topic, payload = client.publish.call_args.args
    assert topic == "bambuddy/bmcu/alarms"
    assert json.loads(payload)["event_id"] == row.event_id
    assert client.publish.call_args.kwargs == {"qos": 1, "retain": False}
    async with factory() as db:
        assert (await db.get(BMCUBinaryAlarmOutbox, row.event_id)).delivered_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_preserves_insertion_order_for_equal_timestamps(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ordering.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with factory() as db:
        for event_id in ("z", "a"):
            db.add(BMCUBinaryAlarmOutbox(event_id=event_id, device_id="pico", pico_boot_id="0",
                                        transport_sequence="0", payload=json.dumps({"event_id": event_id}),
                                        created_at=now))
        await db.commit()
    info = SimpleNamespace(rc=0, wait_for_publish=Mock(), is_published=Mock(return_value=True))
    client = SimpleNamespace(publish=Mock(return_value=info))
    worker = AlarmDelivery(factory, SimpleNamespace(enabled=True, connected=True, client=client, topic_prefix="test"))
    assert await worker.deliver_one()
    assert await worker.deliver_one()
    assert [json.loads(call.args[1])["event_id"] for call in client.publish.call_args_list] == ["z", "a"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_publisher_refuses_offline_broker_and_failed_publish():
    relay = SimpleNamespace(enabled=True, connected=False, client=None, topic_prefix="test")
    assert not await publish_confirmed(relay, "{}")
    relay.connected = True
    relay.client = SimpleNamespace(publish=Mock(return_value=SimpleNamespace(rc=4)))
    assert not await publish_confirmed(relay, "{}")


@pytest.mark.asyncio
async def test_retention_never_removes_undelivered_alarm(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retain.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=100)
    event = BMCUEvent(0, RecordType.STATE_CHANGE, 4, 5, b"", StateField.MOTION_FAULT, 1, 0, 1)
    pending = make_alarm("pico", motion_frame(1, 1), event, old)
    delivered = make_alarm("pico", motion_frame(2, 1), event, old)
    delivered.delivered_at = old
    async with factory() as db:
        db.add_all([pending, delivered])
        await db.commit()
    monkeypatch.setenv("BAMBUDDY_BMCU_RETENTION_HOURS", "48")
    await prune_once(factory)
    async with factory() as db:
        assert await db.get(BMCUBinaryAlarmOutbox, pending.event_id) is not None
        assert await db.get(BMCUBinaryAlarmOutbox, delivered.event_id) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_mqtt_qos1_puback_with_local_broker():
    import asyncio
    import threading

    import paho.mqtt.client as mqtt

    delivered = []

    async def broker(reader, writer):
        async def packet():
            first = (await reader.readexactly(1))[0]
            remaining, multiplier = 0, 1
            while True:
                byte = (await reader.readexactly(1))[0]
                remaining += (byte & 127) * multiplier
                if not byte & 128:
                    break
                multiplier *= 128
            return first, await reader.readexactly(remaining)

        try:
            kind, _ = await packet()
            assert kind == 0x10
            writer.write(b"\x20\x02\x00\x00")
            await writer.drain()
            kind, data = await packet()
            assert kind & 0xF0 == 0x30 and (kind >> 1) & 3 == 1
            topic_len = int.from_bytes(data[:2], "big")
            topic = data[2 : 2 + topic_len].decode()
            mid_pos = 2 + topic_len
            delivered.append((topic, json.loads(data[mid_pos + 2 :])))
            writer.write(b"\x40\x02" + data[mid_pos : mid_pos + 2])
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(broker, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    connected = threading.Event()
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    client.on_connect = lambda _client, _userdata, _flags, _reason, _properties: connected.set()
    client.connect_async("127.0.0.1", port, 10)
    client.loop_start()
    try:
        assert await asyncio.to_thread(connected.wait, 3)
        relay = SimpleNamespace(enabled=True, connected=True, client=client, topic_prefix="test")
        assert await publish_confirmed(relay, '{"event_id":"test-id"}', timeout=2)
        assert delivered == [("test/bmcu/alarms", {"event_id": "test-id"})]
    finally:
        client.disconnect()
        await asyncio.to_thread(client.loop_stop)
        server.close()
        await server.wait_closed()


def test_health_retained_status_expires_and_marks_stale_loader_unknown(monkeypatch):
    import time

    from backend.app.services.bmcu_binary.server import binary_transport_server
    from backend.app.services.bmcu_binary.stream_v2 import StreamState

    state = StreamState(
        heartbeat_at=time.monotonic(), heartbeat_interval=5, boot_id=1, links={0: ("bmcu-a", 2), 1: ("bmcu-b", 4)}
    )
    receiver = SimpleNamespace(states={"pico/a": state}, device_counters={"pico/a": {"gaps_detected": 3}})
    relay = SimpleNamespace(enabled=True, connected=True, topic_prefix="test", _publish=Mock())
    monkeypatch.setattr(binary_transport_server, "udp", receiver)
    monkeypatch.setattr(
        binary_transport_server.persistence,
        "current_state_seen",
        {
            ("pico/a", 0): time.monotonic(),
            ("pico/a", 1): time.monotonic(),
        },
    )
    worker = AlarmDelivery(relay=relay)
    worker.publish_health()
    topic, payload = relay._publish.call_args.args
    assert topic == "test/bmcu/pico%2Fa/status"
    assert relay._publish.call_args.kwargs == {"retain": True}
    assert payload["availability"] == "online"
    assert [link["state"] for link in payload["links"]] == ["online", "unknown"]
    assert payload["gaps_detected_total"] == 3
    assert datetime.fromisoformat(payload["valid_until"]) > datetime.fromisoformat(payload["observed_at"])
