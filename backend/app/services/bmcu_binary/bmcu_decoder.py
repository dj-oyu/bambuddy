"""Embedded BMCU wire-frame validation boundary.

The normative BMB1 document intentionally transports the original frame, but
does not define that frame's header or CRC algorithm. Callers therefore supply
the shared BMCU validator; this module never guesses a wire format.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .errors import InvalidBMCUFrame

SYNC = b"\xA5\x5A"
SUPPORTED_VERSIONS = frozenset((0x83, 0x01))
MAX_PAYLOAD_SIZE = 57
MAX_EMBEDDED_WIRE_SIZE = 66


@dataclass(frozen=True, slots=True)
class ValidatedBMCUFrame:
    received_at_us: int
    wire_bytes: bytes


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
