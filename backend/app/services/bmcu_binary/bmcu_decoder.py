"""Embedded BMCU wire-frame validation boundary.

The normative BMB1 document intentionally transports the original frame, but
does not define that frame's header or CRC algorithm. Callers therefore supply
the shared BMCU validator; this module never guesses a wire format.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from .errors import InvalidBMCUFrame

SYNC = b"\xa5\x5a"
SUPPORTED_VERSIONS = frozenset((0x83, 0x01))
MAX_PAYLOAD_SIZE = 57
MAX_EMBEDDED_WIRE_SIZE = 66


@dataclass(frozen=True, slots=True)
class ValidatedBMCUFrame:
    received_at_us: int
    wire_bytes: bytes


@dataclass(frozen=True, slots=True)
class BMCUEnvelope:
    version: int
    kind: int
    sequence: int
    payload: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class BMCUHello:
    protocol_version: int
    capabilities: int
    firmware_major: int
    firmware_minor: int
    tick_hz: int


@dataclass(frozen=True, slots=True)
class BMCUStatus:
    hw_tick32: int
    tx_drop: int
    rx_drop: int
    crc_error: int
    frame_error: int
    current_slot: int
    # Per docs/bmcu_wire_layout.json: inserted_mask is channel HARDWARE presence,
    # latched at boot from the PULL potentiometer; online_mask is filament
    # detection (the microswitch), gated by inserted_mask. Not interchangeable.
    inserted_mask: int
    online_mask: int
    motion: tuple[int, int, int, int]
    pull_pct: tuple[int, int, int, int]
    pressure: int
    led_mode: int
    control_error: int


@dataclass(frozen=True, slots=True)
class BMCUEvent:
    hw_tick32: int
    record_type: int
    severity: int
    source: int
    payload: bytes
    field: int | None = None
    slot: int | None = None
    previous_value: int | None = None
    value: int | None = None


@dataclass(frozen=True, slots=True)
class BMCUFullStatusRecord:
    snapshot_id: int
    record_index: int
    record_count: int
    record_type: int
    record_flags: int
    hw_tick32: int
    record_data: bytes


def validate_embedded_frame(
    received_at_us: int,
    wire_bytes: bytes,
    validator: Callable[[bytes], bool],
) -> ValidatedBMCUFrame:
    if not wire_bytes or len(wire_bytes) > MAX_EMBEDDED_WIRE_SIZE:
        raise InvalidBMCUFrame("embedded frame length out of range")
    try:
        valid = validator(wire_bytes)
    except Exception as exc:
        raise InvalidBMCUFrame("embedded frame validator failed") from exc
    if not valid:
        raise InvalidBMCUFrame("embedded frame failed validation")
    return ValidatedBMCUFrame(received_at_us, bytes(wire_bytes))


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def validate_alpha3_wire_frame(wire_bytes: bytes) -> bool:
    """Validate the canonical alpha.3/stable-v1 BMCU UART envelope."""
    if not 9 <= len(wire_bytes) <= MAX_EMBEDDED_WIRE_SIZE or wire_bytes[:2] != SYNC:
        return False
    body = wire_bytes[2:-2]
    if body[0] not in SUPPORTED_VERSIONS or body[4] > MAX_PAYLOAD_SIZE:
        return False
    if len(wire_bytes) != body[4] + 9:
        return False
    expected = int.from_bytes(wire_bytes[-2:], "little")
    return crc16_ccitt_false(body) == expected


def decode_wire_frame(wire_bytes: bytes) -> BMCUEnvelope:
    if not validate_alpha3_wire_frame(wire_bytes):
        raise InvalidBMCUFrame("invalid BMCU length, version, or CRC")
    size = wire_bytes[6]
    return BMCUEnvelope(
        wire_bytes[2],
        wire_bytes[3],
        int.from_bytes(wire_bytes[4:6], "little"),
        bytes(wire_bytes[7 : 7 + size]),
        bytes(wire_bytes),
    )


# The kinds decode_semantic can turn into a value: hello, status, event and
# full_status_record. Callers that query stored frames use it to skip kinds that
# carry no loader state — ping (18), pong (114), ack (127) and the request kinds
# are on the wire in quantity and would only spend a row budget. The full kind
# table is `kind` in the BMCU repository's docs/bmcu_link_enum_registry.json.
SEMANTIC_KINDS = frozenset((1, 2, 3, 115))

# full_status_record_type values this module reconstructs realtime state from.
# GLOBAL carries the same fields as a STATUS frame; COUNTERS carries the error
# totals STATUS reports inline. BMCU_LINK_PROTOCOL_ALPHA3.md section 6.2.
FULL_STATUS_GLOBAL = 1
FULL_STATUS_COUNTERS = 4


def status_from_full_status(record: BMCUFullStatusRecord, counters: bytes | None = None) -> BMCUStatus | None:
    """Rebuild a STATUS view from a FULL_STATUS GLOBAL record.

    The bridge can go for days without emitting a single STATUS frame while its
    periodic full snapshot keeps arriving. GLOBAL holds every field STATUS does
    except the error counters, which live in the COUNTERS record of the same
    snapshot, so the pair reconstructs the realtime view exactly rather than by
    inference.
    """
    data = record.record_data
    if record.record_type != FULL_STATUS_GLOBAL or len(data) < 16:
        return None
    tx_drop = rx_drop = crc_error = frame_error = 0
    if counters is not None and len(counters) >= 16:
        tx_drop, rx_drop, crc_error, frame_error = struct.unpack("<4I", counters[:16])
    return BMCUStatus(
        record.hw_tick32,
        tx_drop,
        rx_drop,
        crc_error,
        frame_error,
        data[0],
        data[1],
        data[2],
        tuple(data[4:8]),
        tuple(data[8:12]),
        int.from_bytes(data[12:14], "little"),
        data[14],
        data[3],
    )


def decode_semantic(frame: BMCUEnvelope):
    if frame.kind not in SEMANTIC_KINDS:
        return None
    p = frame.payload
    if frame.kind == 1 and len(p) == 9:
        protocol, capabilities, major, minor, tick_hz = struct.unpack("<BHBBI", p)
        return BMCUHello(protocol, capabilities, major, minor, tick_hz)
    if frame.kind == 2 and len(p) == 27:
        values = struct.unpack("<IHHHHBBB4B4BHBB", p)
        return BMCUStatus(
            values[0],
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
            values[6],
            values[7],
            tuple(values[8:12]),
            tuple(values[12:16]),
            values[16],
            values[17],
            values[18],
        )
    if frame.kind == 3 and len(p) == 16:
        hw_tick, record_type, severity, source, payload_size = struct.unpack("<IBBBB", p[:8])
        if payload_size > 8:
            raise InvalidBMCUFrame("EVENT union length exceeds 8")
        detail = bytes(p[8 : 8 + payload_size])
        if record_type == 4 and payload_size >= 6:
            return BMCUEvent(
                hw_tick,
                record_type,
                severity,
                source,
                detail,
                detail[0],
                detail[1],
                int.from_bytes(detail[2:4], "little"),
                int.from_bytes(detail[4:6], "little"),
            )
        return BMCUEvent(hw_tick, record_type, severity, source, detail)
    if frame.kind == 115 and len(p) == 26:
        return BMCUFullStatusRecord(
            int.from_bytes(p[:2], "little"),
            p[2],
            p[3],
            p[4],
            p[5],
            int.from_bytes(p[6:10], "little"),
            bytes(p[10:26]),
        )
    return None
