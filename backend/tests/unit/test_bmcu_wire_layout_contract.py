"""Pin the decoders to docs/bmcu_wire_layout.json.

The layout file is the shared contract carried identically by this repository
and the BMCU-Link Pico repository. Bambuddy is its third hand-written consumer:
``bmcu_decoder`` and ``framing`` each spell the offsets out again in struct
formats. These tests place a unique marker at every documented offset and assert
the decoder returns it, so an offset can only change in both places at once.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from app.services.bmcu_binary.bmcu_decoder import decode_semantic, decode_wire_frame
from app.services.bmcu_binary.framing import decode_header
from app.services.bmcu_binary.messages import decode_bmcu_frame

LAYOUT = json.loads((Path(__file__).resolve().parents[3] / "docs" / "bmcu_wire_layout.json").read_text())
STRUCTURES = LAYOUT["structures"]

_WIDTHS = {"u8": 1, "u16": 2, "u32": 4, "u64": 8}


def _marked(structure: dict, size: int) -> tuple[bytearray, dict[str, int]]:
    """Fill a buffer so each scalar field carries a distinct recoverable value."""
    buffer, expected = bytearray(size), {}
    endian = "little" if structure["endianness"] == "little" else "big"
    for position, field in enumerate(structure["fields"], start=1):
        width = _WIDTHS.get(field["type"])
        if width is None or field["offset"] < 0:
            continue
        count = field.get("count", 1)
        if count > 1:
            values = tuple(position * 16 + index for index in range(count))
            buffer[field["offset"] : field["offset"] + count] = bytes(values)
            expected[field["name"]] = values
            continue
        value = int.from_bytes(bytes(range(position, position + width)), endian)
        buffer[field["offset"] : field["offset"] + width] = value.to_bytes(width, endian)
        expected[field["name"]] = value
    return buffer, expected


def _wire(kind: int, payload: bytes) -> bytes:
    """Wrap a payload in a BMCU link frame, per the link_wire structure."""
    from app.services.bmcu_binary.bmcu_decoder import SYNC

    body = bytes((0x83, kind)) + (7).to_bytes(2, "little") + bytes((len(payload),)) + payload
    crc = 0xFFFF
    for byte in body:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return SYNC + body + crc.to_bytes(2, "little")


def _decoded(kind: int, structure_name: str):
    structure = STRUCTURES[structure_name]
    payload, expected = _marked(structure, structure["size"])
    return decode_semantic(decode_wire_frame(_wire(kind, bytes(payload)))), expected


def test_link_wire_header_and_trailer_sizes_match_the_document() -> None:
    structure = STRUCTURES["link_wire"]
    frame = decode_wire_frame(_wire(2, b"\x00" * STRUCTURES["status_payload"]["size"]))
    assert len(frame.raw) == structure["header_size"] + len(frame.payload) + structure["trailer_size"]
    assert frame.sequence == 7  # little-endian u16 at offset 4


@pytest.mark.parametrize("field", [f["name"] for f in STRUCTURES["status_payload"]["fields"]])
def test_status_payload_offsets(field: str) -> None:
    status, expected = _decoded(2, "status_payload")
    assert getattr(status, field) == expected[field]


def test_event_payload_offsets() -> None:
    structure = STRUCTURES["event_payload"]
    payload = bytearray(structure["size"])
    payload[0:4] = (0x11223344).to_bytes(4, "little")
    payload[4], payload[5], payload[6], payload[7] = 4, 2, 1, 6
    payload[8:14] = bytes((9, 8, 7, 0, 6, 0))
    event = decode_semantic(decode_wire_frame(_wire(3, bytes(payload))))
    assert (event.hw_tick32, event.record_type, event.severity, event.source) == (0x11223344, 4, 2, 1)
    assert event.payload == bytes((9, 8, 7, 0, 6, 0))
    # record_type 4 is a state change: field, slot, previous value, value.
    assert (event.field, event.slot, event.previous_value, event.value) == (9, 8, 7, 6)


def test_full_status_record_offsets() -> None:
    structure = STRUCTURES["full_status_record"]
    payload, expected = _marked(structure, structure["size"])
    record_data = bytes(range(0x40, 0x50))
    payload[10:26] = record_data
    record = decode_semantic(decode_wire_frame(_wire(115, bytes(payload))))
    for field in ("snapshot_id", "record_index", "record_count", "record_type", "record_flags", "hw_tick32"):
        assert getattr(record, field) == expected[field], field
    assert record.record_data == record_data


def test_bmb1_envelope_offsets() -> None:
    structure = STRUCTURES["bmb1_envelope"]
    header, expected = _marked(structure, structure["size"])
    header[0:4] = b"BMB1"
    header[4] = 1
    header[8:12] = (1024).to_bytes(4, "big")  # bounded below the 4096-byte payload limit
    expected["payload_length"] = 1024
    decoded = decode_header(bytes(header))
    for wire_name, attribute in (
        ("message_type", "message_type"),
        ("flags", "flags"),
        ("payload_length", "payload_length"),
        ("transport_sequence", "transport_sequence"),
        ("pico_boot_id", "pico_boot_id"),
        ("link_index", "link_index"),
    ):
        assert getattr(decoded, attribute) == expected[wire_name], wire_name


def test_bmcu_frame_prefix_offsets() -> None:
    structure = STRUCTURES["bmcu_frame_prefix"]
    wire = _wire(2, b"\x00" * STRUCTURES["status_payload"]["size"])
    payload = struct.pack(">QH", 0x0102030405060708, len(wire)) + wire
    assert structure["size"] == 10
    frame = decode_bmcu_frame(payload, lambda raw: True)
    assert frame.received_at_us == 0x0102030405060708
    assert frame.wire_bytes == wire
