"""Regenerate deterministic cross-repository BMB1 binary fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.app.services.bmcu_binary.auth import control_mac, derive_session_key, hello_mac
from backend.app.services.bmcu_binary.bmcu_decoder import crc16_ccitt_false
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.framing import FrameHeader, encode_frame
from backend.app.services.bmcu_binary.messages import (
    Ack, Control, ControlResult, Hello, HelloLink, HelloReplayRange, LinkStateMessage, PicoLog,
    Reject, TLV, TransportDrop, encode_bmcu_frame, encode_tlvs,
)
from backend.app.services.bmcu_binary.bmcu_decoder import ValidatedBMCUFrame

ROOT = Path(__file__).parent


def bmcu(kind: int, sequence: int, payload: bytes) -> bytes:
    body = bytes((0x83, kind)) + sequence.to_bytes(2, "little") + bytes((len(payload),)) + payload
    return b"\xA5\x5A" + body + crc16_ccitt_false(body).to_bytes(2, "little")


def main() -> None:
    boot = 0x0102030405060708
    key, challenge = bytes(range(32)), bytes(range(32, 64))
    unsigned = Hello(
        "pico-fixture", "1.0.0", (HelloLink(0, "bmcu-a"), HelloLink(1, "bmcu-b")),
        (HelloReplayRange(boot, 7, 42), HelloReplayRange(0x8877665544332211, 2, 9)),
        b"\0" * 32,
    )
    hello = Hello(
        unsigned.device_id, unsigned.firmware, unsigned.links, unsigned.replay_ranges,
        hello_mac(key, challenge, boot, unsigned.transcript()),
    )
    status_payload = bytes(range(27))
    event_payload = bytes(range(16))
    full_payload = bytes(range(26))
    unknown_payload = b"\xDE\xAD"
    records = {
        "server_challenge.bin": encode_frame(FrameHeader(MessageType.SERVER_CHALLENGE), challenge),
        "hello.bin": encode_frame(FrameHeader(MessageType.HELLO, pico_boot_id=boot, link_index=0xFF), hello.encode()),
        "link_state.bin": encode_frame(FrameHeader(MessageType.LINK_STATE, transport_sequence=8, pico_boot_id=boot), LinkStateMessage(123456, 2, 0).encode()),
        "transport_drop.bin": encode_frame(FrameHeader(MessageType.TRANSPORT_DROP, transport_sequence=9, pico_boot_id=boot, link_index=0xFF), TransportDrop(123500, 3, 6, 4, 1).encode()),
        "ack.bin": encode_frame(FrameHeader(MessageType.ACK, pico_boot_id=boot, link_index=0xFF), Ack(boot, 42).encode()),
        "ack_reject.bin": encode_frame(FrameHeader(MessageType.ACK, pico_boot_id=boot, link_index=0xFF), Ack(boot, 41, (Reject(42, 1),)).encode()),
        "diagnostic_unknown_tags.bin": encode_frame(FrameHeader(MessageType.PICO_DIAGNOSTIC, transport_sequence=10, pico_boot_id=boot, link_index=0xFF), encode_tlvs((TLV(240, 1, b"\x01"), TLV(241, 7, b"future")))),
        "pico_log_utf8.bin": encode_frame(FrameHeader(MessageType.PICO_LOG, transport_sequence=11, pico_boot_id=boot, link_index=0xFF), PicoLog(5, 9000, 3, "uart", "通信警告", encode_tlvs((TLV(1, 1, b"\x02"),))).encode()),
    }
    session_key = derive_session_key(key, challenge, boot)
    control_base = Control(77, 500000, 5000, 1, b"\x01", b"\0" * 32)
    control_header = FrameHeader(
        MessageType.CONTROL, payload_length=len(control_base.unsigned_payload()) + 32,
        pico_boot_id=boot, link_index=0,
    )
    control = Control(
        control_base.command_sequence, control_base.issued_at_us, control_base.ttl_ms,
        control_base.command, control_base.arguments,
        control_mac(session_key, control_header.encode(), control_base.unsigned_payload()),
    )
    records["control.bin"] = encode_frame(control_header, control.encode())
    result_base = ControlResult(77, 0, "accepted", b"\0" * 32)
    result_header = FrameHeader(
        MessageType.CONTROL_RESULT, payload_length=len(result_base.unsigned_payload()) + 32,
        transport_sequence=12, pico_boot_id=boot, link_index=0,
    )
    result = ControlResult(
        77, 0, "accepted",
        control_mac(session_key, result_header.encode(), result_base.unsigned_payload()),
    )
    records["control_result.bin"] = encode_frame(result_header, result.encode())
    records["pico_log_max.bin"] = encode_frame(
        FrameHeader(MessageType.PICO_LOG, transport_sequence=13, pico_boot_id=boot, link_index=0xFF),
        PicoLog(6, 9001, 4, "c" * 40, "m" * 320, encode_tlvs((TLV(2, 11, b"d" * 508),))).encode(),
    )
    records["recovered_replay.bin"] = encode_frame(
        FrameHeader(MessageType.PICO_LOG, flags=1 | 16, transport_sequence=2,
                    pico_boot_id=0x8877665544332211, link_index=0xFF),
        PicoLog(1, 500, 4, "boot", "recovered crash").encode(),
    )
    for name, kind, seq, payload in (
        ("bmcu_status.bin", 2, 1, status_payload),
        ("bmcu_event.bin", 3, 2, event_payload),
        ("bmcu_full_status.bin", 115, 3, full_payload),
        ("bmcu_unknown.bin", 126, 4, unknown_payload),
    ):
        wire = bmcu(kind, seq, payload)
        records[name] = encode_frame(
            FrameHeader(MessageType.BMCU_FRAME, transport_sequence=20 + seq, pico_boot_id=boot),
            encode_bmcu_frame(ValidatedBMCUFrame(100000 + seq, wire)),
        )
    records["concatenated.bin"] = records["link_state.bin"] + records["ack.bin"]
    records["truncated_header.bin"] = records["hello.bin"][:17]
    for name, raw in records.items():
        (ROOT / name).write_bytes(raw)
    manifest = {
        "format": "bmcu-binary-fixtures-v1",
        "files": {
            name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in sorted(records.items())
        },
        "auth": {
            "device_key_hex": key.hex(),
            "challenge_hex": challenge.hex(),
            "pico_boot_id": boot,
            "hello_hmac_hex": hello.mac.hex()
        }
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
