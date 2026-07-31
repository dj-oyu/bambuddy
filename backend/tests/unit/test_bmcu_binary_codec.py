import hashlib
import json
import random
from pathlib import Path

import pytest

from backend.app.services.bmcu_binary.auth import derive_session_key, verify_hello_mac
from backend.app.services.bmcu_binary.bmcu_decoder import validate_alpha3_wire_frame
from backend.app.services.bmcu_binary.constants import (
    MAX_PAYLOAD_SIZE, ControlCommand, ControlResultCode, DropReason, Flag,
    LinkReason, LinkState, LogSeverity, MessageType, ProtocolErrorCode,
    RejectReason, ValueType,
)
from backend.app.services.bmcu_binary.errors import InvalidMessage, PayloadTooLarge
from backend.app.services.bmcu_binary.framing import (
    FrameHeader, IncrementalFrameParser, decode_header, encode_frame,
)
from backend.app.services.bmcu_binary.messages import (
    Ack, ControlResult, Hello, HelloAccepted, PicoLog, Ping, ProtocolErrorMessage,
    ServerChallenge, decode_bmcu_frame, decode_tlvs,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bmcu_binary"


def test_fixture_manifest_hashes() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        raw = (FIXTURES / name).read_bytes()
        assert len(raw) == expected["bytes"]
        assert hashlib.sha256(raw).hexdigest() == expected["sha256"]


def test_python_constants_match_machine_registry() -> None:
    registry = json.loads((Path(__file__).parents[3] / "docs" / "bmcu_binary_registry.json").read_text())
    mappings = {
        "message_types": MessageType,
        "flags": Flag,
        "value_types": ValueType,
        "link_states": LinkState,
        "link_reasons": LinkReason,
        "log_severity": LogSeverity,
        "drop_reasons": DropReason,
        "reject_reasons": RejectReason,
        "control_commands": ControlCommand,
        "control_result_codes": ControlResultCode,
        "protocol_error_codes": ProtocolErrorCode,
    }
    for name, enum_type in mappings.items():
        assert registry[name] == {item.name: item.value for item in enum_type}


@pytest.mark.parametrize("name", ["hello.bin", "bmcu_status.bin", "pico_log_utf8.bin"])
def test_parser_accepts_every_split_point(name: str) -> None:
    raw = (FIXTURES / name).read_bytes()
    expected = IncrementalFrameParser().feed(raw)
    assert len(expected) == 1
    for split in range(len(raw) + 1):
        parser = IncrementalFrameParser()
        assert parser.feed(raw[:split]) + parser.feed(raw[split:]) == expected
        assert parser.buffered_bytes == 0


def test_concatenated_messages() -> None:
    frames = IncrementalFrameParser().feed((FIXTURES / "concatenated.bin").read_bytes())
    assert [f.header.message_type for f in frames] == [MessageType.LINK_STATE, MessageType.ACK]


def test_parser_progress_with_random_chunking() -> None:
    raw = (FIXTURES / "concatenated.bin").read_bytes() * 25
    for seed in range(20):
        rng, parser, frames, at = random.Random(seed), IncrementalFrameParser(), [], 0
        while at < len(raw):
            size = rng.randint(1, 73)
            frames.extend(parser.feed(raw[at:at + size]))
            at += size
            assert parser.buffered_bytes <= 32 + MAX_PAYLOAD_SIZE
        assert len(frames) == 50
        assert parser.buffered_bytes == 0


def test_oversize_rejected_after_header_without_payload_allocation() -> None:
    raw = bytearray(32)
    raw[:4] = b"BMB1"
    raw[4] = 1
    raw[8:12] = (MAX_PAYLOAD_SIZE + 1).to_bytes(4, "big")
    parser = IncrementalFrameParser()
    with pytest.raises(PayloadTooLarge):
        parser.feed(raw)
    assert parser.buffered_bytes == 32


def test_header_output_zeros_reserved_and_receiver_ignores_reserved() -> None:
    raw = bytearray(FrameHeader(MessageType.PING).encode())
    assert raw[29:32] == b"\0\0\0"
    raw[29:32] = b"\x01\x02\x03"
    assert decode_header(raw).message_type == MessageType.PING


def test_hello_known_answer() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    frame = IncrementalFrameParser().feed((FIXTURES / "hello.bin").read_bytes())[0]
    hello = Hello.decode(frame.payload)
    assert hello.mac.hex() == manifest["auth"]["hello_hmac_hex"]
    assert verify_hello_mac(
        bytes.fromhex(manifest["auth"]["device_key_hex"]),
        bytes.fromhex(manifest["auth"]["challenge_hex"]),
        frame.header.pico_boot_id,
        hello.transcript(),
        hello.mac,
    )
    assert len(derive_session_key(
        bytes.fromhex(manifest["auth"]["device_key_hex"]),
        bytes.fromhex(manifest["auth"]["challenge_hex"]),
        frame.header.pico_boot_id,
    )) == 32


def test_ack_global_scope_and_rejections() -> None:
    frame = IncrementalFrameParser().feed((FIXTURES / "ack_reject.bin").read_bytes())[0]
    ack = Ack.decode(frame.payload)
    assert ack.scope == frame.header.link_index == 0xFF
    assert ack.persisted_through_sequence == 41
    assert ack.rejects[0].sequence == 42


def test_embedded_bmcu_frames_are_crc_valid_and_preserved() -> None:
    for name in ("bmcu_status.bin", "bmcu_event.bin", "bmcu_full_status.bin", "bmcu_unknown.bin"):
        outer = IncrementalFrameParser().feed((FIXTURES / name).read_bytes())[0]
        decoded = decode_bmcu_frame(outer.payload, validate_alpha3_wire_frame)
        assert validate_alpha3_wire_frame(decoded.wire_bytes)
        damaged = bytearray(decoded.wire_bytes)
        damaged[-1] ^= 1
        assert not validate_alpha3_wire_frame(damaged)


def test_unknown_diagnostic_tags_are_preserved_for_skip() -> None:
    frame = IncrementalFrameParser().feed((FIXTURES / "diagnostic_unknown_tags.bin").read_bytes())[0]
    assert [item.tag for item in decode_tlvs(frame.payload)] == [240, 241]


def test_utf8_log_and_tlv_detail() -> None:
    frame = IncrementalFrameParser().feed((FIXTURES / "pico_log_utf8.bin").read_bytes())[0]
    log = PicoLog.decode(frame.payload)
    assert log.message == "通信警告"
    assert decode_tlvs(log.detail)[0].value == b"\x02"


def test_payload_and_message_bounds() -> None:
    with pytest.raises(PayloadTooLarge):
        encode_frame(FrameHeader(MessageType.PING), b"x" * 4097)
    with pytest.raises(InvalidMessage):
        PicoLog(1, 1, 1, "x" * 41, "ok").encode()


@pytest.mark.parametrize("value", [
    ServerChallenge(bytes(range(32))),
    HelloAccepted(42, 5000, 15000),
    Ping(0x0102030405060708),
    ProtocolErrorMessage(3, "不正なフレーム"),
    ControlResult(8, 0, "accepted", b"x" * 32),
])
def test_newly_specified_payload_roundtrips(value) -> None:
    assert type(value).decode(value.encode()) == value
