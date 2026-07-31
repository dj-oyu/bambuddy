import asyncio

from backend.app.services.bmcu_binary.auth import control_mac, derive_session_key, hello_mac
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.framing import FrameHeader, IncrementalFrameParser, encode_frame
from backend.app.services.bmcu_binary.messages import (
    Control, ControlResult, Hello, HelloLink, HelloReplayRange, Ping,
)
from backend.app.services.bmcu_binary.registry import SessionRegistry
from backend.app.services.bmcu_binary.session import BinarySession


class Reader:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


class Writer:
    def __init__(self):
        self.output = bytearray()
        self.closed = False

    def write(self, data):
        self.output.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class Persistence:
    def __init__(self):
        self.registered = []
        self.persisted = []
        self.control_results = []

    async def register_hello(self, hello, boot):
        self.registered.append((hello.device_id, boot))
        return 9

    async def persist(self, device_id, frame):
        self.persisted.append((device_id, frame.header.transport_sequence))
        return frame.header.transport_sequence

    async def persist_control_result(self, device_id, boot, result):
        self.control_results.append((device_id, boot, result.command_sequence))


def test_authenticated_session_and_ping(monkeypatch) -> None:
    challenge, key, boot = bytes(range(32)), b"k" * 32, 123
    ranges = (HelloReplayRange(boot, 10, 10),)
    unsigned = Hello("monitor-1", "fw", (HelloLink(0, "bmcu-a"),), ranges, b"\0" * 32)
    hello = Hello(
        unsigned.device_id, unsigned.firmware, unsigned.links, ranges,
        hello_mac(key, challenge, boot, unsigned.transcript()),
    )
    result_base = ControlResult(3, 0, "ok", b"\0" * 32)
    result_header = FrameHeader(
        MessageType.CONTROL_RESULT, payload_length=len(result_base.unsigned_payload()) + 32,
        pico_boot_id=boot, link_index=0,
    )
    result = ControlResult(
        3, 0, "ok",
        control_mac(derive_session_key(key, challenge, boot), result_header.encode(),
                    result_base.unsigned_payload()),
    )
    incoming = (
        encode_frame(FrameHeader(MessageType.HELLO, pico_boot_id=boot, link_index=0xFF), hello.encode())
        + encode_frame(FrameHeader(MessageType.PING, pico_boot_id=boot, link_index=0xFF), Ping(55).encode())
        + encode_frame(
            FrameHeader(MessageType.PICO_DIAGNOSTIC, transport_sequence=10,
                        pico_boot_id=boot, link_index=0xFF),
            b"",
        )
        + encode_frame(result_header, result.encode())
    )
    reader, writer, persistence, registry = Reader([incoming]), Writer(), Persistence(), SessionRegistry()
    monkeypatch.setattr("backend.app.services.bmcu_binary.session.os.urandom", lambda _n: challenge)
    session = BinarySession(
        reader, writer, key_provider=lambda device: key if device == "monitor-1" else None,
        persistence=persistence, registry=registry, auth_timeout=1, idle_timeout=1, write_timeout=1,
    )
    asyncio.run(session.run())
    frames = IncrementalFrameParser().feed(writer.output)
    assert [f.header.message_type for f in frames] == [
        MessageType.SERVER_CHALLENGE, MessageType.HELLO_ACCEPTED, MessageType.PONG,
        MessageType.ACK,
    ]
    assert persistence.registered == [("monitor-1", boot)]
    assert persistence.persisted == [("monitor-1", 10)]
    assert persistence.control_results == [("monitor-1", boot, 3)]
    assert writer.closed


def test_telemetry_before_hello_is_rejected(monkeypatch) -> None:
    challenge = bytes(range(32))
    incoming = encode_frame(
        FrameHeader(MessageType.PICO_DIAGNOSTIC, transport_sequence=1, pico_boot_id=1, link_index=0xFF),
        b"",
    )
    reader, writer = Reader([incoming]), Writer()
    monkeypatch.setattr("backend.app.services.bmcu_binary.session.os.urandom", lambda _n: challenge)
    session = BinarySession(
        reader, writer, key_provider=lambda _device: None, persistence=Persistence(),
        registry=SessionRegistry(), auth_timeout=1, idle_timeout=1, write_timeout=1,
    )
    asyncio.run(session.run())
    frames = IncrementalFrameParser().feed(writer.output)
    assert [f.header.message_type for f in frames] == [MessageType.SERVER_CHALLENGE]
    assert writer.closed


def test_control_timestamp_is_session_relative(monkeypatch) -> None:
    writer = Writer()
    session = BinarySession(
        Reader([]), writer, key_provider=lambda _device: None, persistence=Persistence(),
        registry=SessionRegistry(), auth_timeout=1, idle_timeout=1, write_timeout=1,
    )
    session.session_key = b"s" * 32
    session.authenticated_boot_id = 99
    session.control_epoch_ns = 1_000_000_000
    monkeypatch.setattr("backend.app.services.bmcu_binary.session.time.monotonic_ns", lambda: 1_005_000_000)
    asyncio.run(session.send_control(
        link_index=0, command_sequence=3, ttl_ms=5000, command=1, arguments=b"\x02",
    ))
    frame = IncrementalFrameParser().feed(writer.output)[0]
    control = Control.decode(frame.payload)
    assert frame.header.message_type == MessageType.CONTROL
    assert control.issued_at_us == 5000
