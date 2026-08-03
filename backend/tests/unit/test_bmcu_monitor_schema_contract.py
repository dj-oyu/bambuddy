"""Pin the decoders to the schema the BMCU monitor serves at /api/schema.json.

The bridge self-describes: ``GET /api/schema.json`` returns every enum and every
structure layout needed to decode the binary endpoints without reading the
device source. BMCU_BINARY_TRANSPORT_V1.md section 12 makes that the consumer's
entry point and says plainly that no copy of the generator's input belongs here
-- bambuddy used to carry ``docs/bmcu_wire_layout.json`` as a hand-maintained
mirror, and it had already drifted from the device in three places.

``monitor_schema.json`` is therefore a *capture*, not a contract: verbatim
served bytes, refreshed by ``scripts/fetch_bmcu_schema.py`` and never edited by
hand. Its ``revision`` is the staleness signal -- the device bumps it whenever a
structure or an enum moves, so a capture whose revision still matches the
bridge's is known to describe the wire in front of us.

Bambuddy remains a hand-written consumer: ``bmcu_decoder`` and ``framing``
spell the offsets out again in struct formats. These tests place a unique marker
at every offset the schema documents and assert the decoder returns it, so an
offset can only change in both places at once.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from backend.app.services.bmcu_binary.bmcu_decoder import (
    STATUS_SCHEMA_REVISION,
    decode_channel_flags,
    decode_semantic,
    decode_wire_frame,
)
from backend.app.services.bmcu_binary.framing import decode_header
from backend.app.services.bmcu_binary.messages import decode_bmcu_frame

SCHEMA = json.loads((Path(__file__).parents[1] / "fixtures" / "bmcu_binary" / "monitor_schema.json").read_text())
STRUCTURES = SCHEMA["structures"]
_CHANNEL_FLAGS = next(f for f in STRUCTURES["status_payload"]["fields"] if f["name"] == "channel_flags")

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
    from backend.app.services.bmcu_binary.bmcu_decoder import SYNC

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


def test_status_payload_size_is_the_length_the_decoder_requires() -> None:
    """A STATUS of any other length must not be read as this layout.

    The payload grew from 27 to 31 bytes when the per-channel flags byte landed.
    Every field before it kept its offset, so a short frame decodes *correctly*
    field by field and silently reports no latches at all -- which is why the
    length is checked rather than inferred.
    """
    structure = STRUCTURES["status_payload"]
    assert structure["size"] == 31
    last = max(structure["fields"], key=lambda field: field["offset"])
    assert last["offset"] + last.get("count", 1) == structure["size"]


def test_capture_revision_matches_the_decoder_it_pins() -> None:
    """The capture and the decoder must name the same schema revision.

    ``revision`` moves whenever a structure or an enum does. Refreshing the
    capture without reading what moved would silently repoint these assertions
    at a wire format nobody checked, so the decoder states which revision it was
    written against and the two are compared here.
    """
    assert SCHEMA["revision"] == STATUS_SCHEMA_REVISION


@pytest.mark.parametrize("bitfield", [b["name"] for b in _CHANNEL_FLAGS["bitfields"] if b["name"] != "reserved"])
def test_channel_flag_bit_positions(bitfield: str) -> None:
    """Each documented bit is read from its own shift, and only from there."""
    spec = next(b for b in _CHANNEL_FLAGS["bitfields"] if b["name"] == bitfield)
    saturated = spec["mask"] << spec["shift"]
    assert getattr(decode_channel_flags(saturated), bitfield) == spec["mask"]
    # Every other documented bit reads zero from a byte that sets only this one.
    for other in _CHANNEL_FLAGS["bitfields"]:
        if other["name"] not in (bitfield, "reserved"):
            assert getattr(decode_channel_flags(saturated), other["name"]) == 0


def test_reserved_channel_flag_bits_do_not_leak_into_a_named_field() -> None:
    """Bit 7 is sent as zero today; a bridge that sets it must not read as a latch."""
    flags = decode_channel_flags(0xFF)
    assert (flags.ks, flags.low, flags.jam, flags.dm_fail, flags.loaded, flags.tail) == (3, 1, 1, 1, 1, 1)


def test_channel_flags_are_read_per_channel_not_shared() -> None:
    """Four bytes, four channels: the latches must not be broadcast across slots."""
    payload = bytearray(STRUCTURES["status_payload"]["size"])
    payload[27:31] = bytes((0b0010_0001, 0b0000_0100, 0, 0b0100_0000))
    status = decode_semantic(decode_wire_frame(_wire(2, bytes(payload))))
    assert [channel.loaded for channel in status.channels] == [1, 0, 0, 0]
    assert [channel.ks for channel in status.channels] == [1, 0, 0, 0]
    assert [channel.low for channel in status.channels] == [0, 1, 0, 0]
    assert [channel.tail for channel in status.channels] == [0, 0, 0, 1]


def test_status_without_the_flags_byte_reports_unknown_not_healthy() -> None:
    """A 27-byte STATUS still decodes, and says nothing about the latches.

    Every retained record written before the bridge firmware grew the flags byte
    is 27 bytes. Rejecting them would blank the loader history over the whole
    retention window; zero-filling them would state that four channels are fault
    free on the authority of a frame that never mentioned them.
    """
    payload = bytearray(STRUCTURES["status_payload"]["size"] - 4)
    payload[12] = 2  # current_slot, so the frame is recognisably decoded
    status = decode_semantic(decode_wire_frame(_wire(2, bytes(payload))))
    assert status.current_slot == 2
    assert status.channel_flags is None
    assert status.channels == ()


@pytest.mark.parametrize("size", [26, 28, 30, 32])
def test_status_of_an_undocumented_length_is_not_guessed_at(size: int) -> None:
    payload = bytes(size)
    assert decode_semantic(decode_wire_frame(_wire(2, payload))) is None


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
