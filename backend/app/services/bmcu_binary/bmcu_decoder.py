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

# The /api/schema.json revision these offsets were written against, pinned by
# tests/unit/test_bmcu_monitor_schema_contract.py against the captured schema.
# The device bumps it whenever a structure or an enum moves.
STATUS_SCHEMA_REVISION = 8

# STATUS payload lengths this module accepts. 31 is current: it appends a
# per-channel flags byte carrying the fault latches and the raw switch reading.
# 27 is the same layout without that byte, which is what every retained record
# written before the bridge firmware moved still contains, so history keeps
# decoding rather than becoming four bytes of silence.
STATUS_SIZE = 31
STATUS_SIZE_WITHOUT_CHANNEL_FLAGS = 27


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
class ChannelFlags:
    """One channel's fault latches and raw switch reading, from one flags byte.

    Decoded with explicit shifts. The firmware's own union in ``src/bmcu_link.h``
    is MCU-internal and its bitfield order is compiler-defined, so the wire
    layout is the only thing worth following here.
    """

    # 0=none, 1=both, 2=external only, 3=internal only. A single-microswitch
    # build only ever reports 0 or 1.
    ks: int
    # Pull fell below 40% during pressure-controlled use and the motor stopped.
    low: int
    # The jam variant of `low`; also raises HMS 0xF06F. Only ever set with it.
    jam: int
    # DM autoload stage 1 or 2 failed. Clears only on ks == 0, a full withdrawal.
    dm_fail: int
    # This channel owns the shared PTFE merger -- the flash-backed mutex on the
    # one output tube all four channels feed. Set in both LOADED and TAIL,
    # because both are ownership, and released only by a printer command: a key
    # reading empty demotes to TAIL instead. At most one channel sets it.
    loaded: int
    # Still owns the merger, but the filament has passed the online key so the
    # switch can no longer see it -- the runout state. The tail clears the switch
    # before the printer commands the retract, and that retract must still be
    # accepted. Only ever set together with `loaded`. Reserved and zero before
    # firmware revision 8, so reading it from an older bridge is harmless.
    tail: int


def decode_channel_flags(value: int) -> ChannelFlags:
    return ChannelFlags(
        value & 0x03,
        (value >> 2) & 0x01,
        (value >> 3) & 0x01,
        (value >> 4) & 0x01,
        (value >> 5) & 0x01,
        (value >> 6) & 0x01,
    )


@dataclass(frozen=True, slots=True)
class BMCUStatus:
    hw_tick32: int
    tx_drop: int
    rx_drop: int
    crc_error: int
    frame_error: int
    current_slot: int
    # Per the served schema's status_payload: inserted_mask is channel HARDWARE
    # presence, latched at boot from the PULL potentiometer; online_mask is
    # filament detection (the microswitch), gated by inserted_mask. Not
    # interchangeable.
    inserted_mask: int
    online_mask: int
    motion: tuple[int, int, int, int]
    pull_pct: tuple[int, int, int, int]
    pressure: int
    led_mode: int
    control_error: int
    # Raw per-channel flags bytes, or None when the frame predates them. None is
    # "this firmware never said", which is not the same answer as "no latch is
    # set", and the two must not be collapsed: a stored 27-byte frame would
    # otherwise report four healthy channels it knows nothing about.
    channel_flags: tuple[int, int, int, int] | None = None

    @property
    def channels(self) -> tuple[ChannelFlags, ...]:
        """Decoded per-channel flags; empty when the frame carried none."""
        if self.channel_flags is None:
            return ()
        return tuple(decode_channel_flags(value) for value in self.channel_flags)


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
    if frame.kind == 2 and len(p) in (STATUS_SIZE, STATUS_SIZE_WITHOUT_CHANNEL_FLAGS):
        values = struct.unpack("<IHHHHBBB4B4BHBB", p[:STATUS_SIZE_WITHOUT_CHANNEL_FLAGS])
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
            tuple(p[STATUS_SIZE_WITHOUT_CHANNEL_FLAGS:STATUS_SIZE]) if len(p) == STATUS_SIZE else None,
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
