"""BMB1 TCP framing with a bounded incremental receive buffer."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .constants import HEADER_SIZE, MAGIC, MAX_PAYLOAD_SIZE, VERSION
from .errors import InvalidHeader, PayloadTooLarge

_HEADER = struct.Struct(">4sBBHIQQB3s")


@dataclass(frozen=True, slots=True)
class FrameHeader:
    message_type: int
    flags: int = 0
    payload_length: int = 0
    transport_sequence: int = 0
    pico_boot_id: int = 0
    link_index: int = 0

    def encode(self) -> bytes:
        if not 0 <= self.message_type <= 0xFF:
            raise InvalidHeader("message type out of range")
        if not 0 <= self.flags <= 0xFFFF:
            raise InvalidHeader("flags out of range")
        if not 0 <= self.payload_length <= MAX_PAYLOAD_SIZE:
            raise PayloadTooLarge("payload exceeds 4096-byte limit")
        if not 0 <= self.link_index <= 0xFF:
            raise InvalidHeader("link index out of range")
        return _HEADER.pack(
            MAGIC,
            VERSION,
            self.message_type,
            self.flags,
            self.payload_length,
            self.transport_sequence,
            self.pico_boot_id,
            self.link_index,
            b"\0\0\0",
        )


@dataclass(frozen=True, slots=True)
class Frame:
    header: FrameHeader
    payload: bytes


def decode_header(data: bytes | bytearray | memoryview) -> FrameHeader:
    if len(data) != HEADER_SIZE:
        raise InvalidHeader("header must be exactly 32 bytes")
    magic, version, typ, flags, size, seq, boot, link, _reserved = _HEADER.unpack(data)
    if magic != MAGIC or version != VERSION:
        raise InvalidHeader("invalid magic or protocol version")
    if size > MAX_PAYLOAD_SIZE:
        raise PayloadTooLarge("payload exceeds 4096-byte limit")
    # Reserved bytes/bits are explicitly ignored by v1 receivers.
    return FrameHeader(typ, flags, size, seq, boot, link)


def encode_frame(header: FrameHeader, payload: bytes | bytearray | memoryview = b"") -> bytes:
    raw = bytes(payload)
    if len(raw) > MAX_PAYLOAD_SIZE:
        raise PayloadTooLarge("payload exceeds 4096-byte limit")
    normalized = FrameHeader(
        header.message_type,
        header.flags,
        len(raw),
        header.transport_sequence,
        header.pico_boot_id,
        header.link_index,
    )
    return normalized.encode() + raw


class IncrementalFrameParser:
    """Consumes arbitrary TCP chunks while retaining at most one bounded frame."""

    __slots__ = ("_buffer", "_expected")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected: int | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[Frame]:
        view = memoryview(chunk)
        frames: list[Frame] = []
        offset = 0
        while offset < len(view):
            target = HEADER_SIZE if self._expected is None else self._expected
            take = min(target - len(self._buffer), len(view) - offset)
            self._buffer.extend(view[offset : offset + take])
            offset += take
            if self._expected is None and len(self._buffer) == HEADER_SIZE:
                header = decode_header(self._buffer)
                self._expected = HEADER_SIZE + header.payload_length
                if header.payload_length == 0:
                    frames.append(Frame(header, b""))
                    self._buffer.clear()
                    self._expected = None
            elif self._expected is not None and len(self._buffer) == self._expected:
                header = decode_header(memoryview(self._buffer)[:HEADER_SIZE])
                frames.append(Frame(header, bytes(memoryview(self._buffer)[HEADER_SIZE:])))
                self._buffer.clear()
                self._expected = None
        return frames
