"""Typed payload codecs whose layouts are normative in BMB1 revision 1."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .bmcu_decoder import ValidatedBMCUFrame, validate_embedded_frame
from .errors import InvalidMessage


class _Reader:
    __slots__ = ("data", "at")

    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.at + size > len(self.data):
            raise InvalidMessage("truncated payload")
        result = self.data[self.at:self.at + size]
        self.at += size
        return result

    def integer(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def finish(self) -> None:
        if self.at != len(self.data):
            raise InvalidMessage("unexpected trailing payload")


def _bounded_utf8(reader: _Reader, length_size: int, maximum: int) -> str:
    size = reader.integer(length_size)
    if size > maximum:
        raise InvalidMessage("string exceeds protocol bound")
    try:
        return reader.take(size).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidMessage("invalid UTF-8") from exc


def _text(value: str, maximum: int, length_size: int = 1) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > maximum:
        raise InvalidMessage("string exceeds protocol bound")
    return len(raw).to_bytes(length_size, "big") + raw


@dataclass(frozen=True, slots=True)
class ServerChallenge:
    nonce: bytes

    def encode(self) -> bytes:
        if len(self.nonce) != 32:
            raise InvalidMessage("SERVER_CHALLENGE nonce must be 32 bytes")
        return self.nonce

    @classmethod
    def decode(cls, payload: bytes) -> "ServerChallenge":
        result = cls(bytes(payload))
        result.encode()
        return result


@dataclass(frozen=True, slots=True)
class HelloAccepted:
    persisted_through_sequence: int
    ack_timeout_ms: int
    ping_interval_ms: int

    def encode(self) -> bytes:
        return struct.pack(">QII", self.persisted_through_sequence, self.ack_timeout_ms, self.ping_interval_ms)

    @classmethod
    def decode(cls, payload: bytes) -> "HelloAccepted":
        if len(payload) != 16:
            raise InvalidMessage("HELLO_ACCEPTED payload must be 16 bytes")
        return cls(*struct.unpack(">QII", payload))


@dataclass(frozen=True, slots=True)
class Ping:
    token: int

    def encode(self) -> bytes:
        return self.token.to_bytes(8, "big")

    @classmethod
    def decode(cls, payload: bytes) -> "Ping":
        if len(payload) != 8:
            raise InvalidMessage("PING/PONG payload must be 8 bytes")
        return cls(int.from_bytes(payload, "big"))


@dataclass(frozen=True, slots=True)
class ProtocolErrorMessage:
    code: int
    detail: str = ""

    def encode(self) -> bytes:
        return self.code.to_bytes(2, "big") + _text(self.detail, 160, 2)

    @classmethod
    def decode(cls, payload: bytes) -> "ProtocolErrorMessage":
        r = _Reader(payload)
        code = r.integer(2)
        detail = _bounded_utf8(r, 2, 160)
        r.finish()
        return cls(code, detail)


@dataclass(frozen=True, slots=True)
class HelloLink:
    link_index: int
    link_id: str


@dataclass(frozen=True, slots=True)
class HelloReplayRange:
    pico_boot_id: int
    oldest_available_sequence: int
    newest_available_sequence: int


@dataclass(frozen=True, slots=True)
class Hello:
    device_id: str
    firmware: str
    links: tuple[HelloLink, ...]
    replay_ranges: tuple[HelloReplayRange, ...]
    mac: bytes

    def transcript(self) -> bytes:
        if len(self.links) > 2:
            raise InvalidMessage("v1 supports at most two links")
        out = bytearray(_text(self.device_id, 63) + _text(self.firmware, 63))
        out.append(len(self.links))
        seen: set[int] = set()
        for link in self.links:
            if link.link_index in seen or not 0 <= link.link_index <= 0xFF:
                raise InvalidMessage("invalid or duplicate link index")
            seen.add(link.link_index)
            out.append(link.link_index)
            out.extend(_text(link.link_id, 31))
        if not 1 <= len(self.replay_ranges) <= 8:
            raise InvalidMessage("HELLO requires one to eight replay boot ranges")
        out.append(len(self.replay_ranges))
        seen_boots: set[int] = set()
        for item in self.replay_ranges:
            if item.pico_boot_id in seen_boots:
                raise InvalidMessage("duplicate replay boot range")
            if (item.oldest_available_sequence == 0) != (item.newest_available_sequence == 0):
                raise InvalidMessage("empty replay range must use two zeros")
            if item.oldest_available_sequence > item.newest_available_sequence:
                raise InvalidMessage("replay range is reversed")
            seen_boots.add(item.pico_boot_id)
            out.extend(item.pico_boot_id.to_bytes(8, "big"))
            out.extend(item.oldest_available_sequence.to_bytes(8, "big"))
            out.extend(item.newest_available_sequence.to_bytes(8, "big"))
        return bytes(out)

    def encode(self) -> bytes:
        if len(self.mac) != 32:
            raise InvalidMessage("HELLO HMAC must be 32 bytes")
        return self.transcript() + self.mac

    @classmethod
    def decode(cls, payload: bytes) -> "Hello":
        r = _Reader(payload)
        device = _bounded_utf8(r, 1, 63)
        firmware = _bounded_utf8(r, 1, 63)
        count = r.integer(1)
        if count > 2:
            raise InvalidMessage("v1 supports at most two links")
        links = tuple(HelloLink(r.integer(1), _bounded_utf8(r, 1, 31)) for _ in range(count))
        range_count = r.integer(1)
        if not 1 <= range_count <= 8:
            raise InvalidMessage("HELLO requires one to eight replay boot ranges")
        ranges = tuple(HelloReplayRange(r.integer(8), r.integer(8), r.integer(8)) for _ in range(range_count))
        mac = r.take(32)
        r.finish()
        result = cls(device, firmware, links, ranges, mac)
        result.transcript()  # duplicate-index and bound validation
        return result


@dataclass(frozen=True, slots=True)
class LinkStateMessage:
    observed_at_us: int
    state: int
    reason: int

    def encode(self) -> bytes:
        return struct.pack(">QBBH", self.observed_at_us, self.state, self.reason, 0)

    @classmethod
    def decode(cls, payload: bytes) -> "LinkStateMessage":
        if len(payload) != 12:
            raise InvalidMessage("LINK_STATE payload must be 12 bytes")
        observed, state, reason, _reserved = struct.unpack(">QBBH", payload)
        return cls(observed, state, reason)


@dataclass(frozen=True, slots=True)
class TransportDrop:
    observed_at_us: int
    first_sequence: int
    last_sequence: int
    count: int
    reason: int

    def encode(self) -> bytes:
        return struct.pack(
            ">QQQIB3s", self.observed_at_us, self.first_sequence, self.last_sequence,
            self.count, self.reason, b"\0\0\0",
        )

    @classmethod
    def decode(cls, payload: bytes) -> "TransportDrop":
        if len(payload) != 32:
            raise InvalidMessage("TRANSPORT_DROP payload must be 32 bytes")
        observed, first, last, count, reason, _reserved = struct.unpack(">QQQIB3s", payload)
        return cls(observed, first, last, count, reason)


@dataclass(frozen=True, slots=True)
class Reject:
    sequence: int
    reason: int


@dataclass(frozen=True, slots=True)
class Ack:
    pico_boot_id: int
    persisted_through_sequence: int
    rejects: tuple[Reject, ...] = ()
    scope: int = 0xFF

    def encode(self) -> bytes:
        if self.scope != 0xFF or len(self.rejects) > 0xFF:
            raise InvalidMessage("ACK scope/count invalid")
        out = bytearray(struct.pack(
            ">QBBHQ", self.pico_boot_id, self.scope, len(self.rejects), 0,
            self.persisted_through_sequence,
        ))
        for reject in self.rejects:
            out.extend(struct.pack(">QB", reject.sequence, reject.reason))
        return bytes(out)

    @classmethod
    def decode(cls, payload: bytes) -> "Ack":
        if len(payload) < 20:
            raise InvalidMessage("truncated ACK")
        boot, scope, count, _reserved, watermark = struct.unpack(">QBBHQ", payload[:20])
        if scope != 0xFF or len(payload) != 20 + count * 9:
            raise InvalidMessage("ACK scope or reject count invalid")
        rejects = tuple(
            Reject(*struct.unpack(">QB", payload[20 + i * 9:29 + i * 9]))
            for i in range(count)
        )
        return cls(boot, watermark, rejects, scope)


@dataclass(frozen=True, slots=True)
class TLV:
    tag: int
    value_type: int
    value: bytes


def encode_tlvs(values: tuple[TLV, ...]) -> bytes:
    out = bytearray()
    for item in values:
        if len(item.value) > 0xFFFF:
            raise InvalidMessage("TLV value too long")
        out.extend(struct.pack(">BBH", item.tag, item.value_type, len(item.value)))
        out.extend(item.value)
    return bytes(out)


def decode_tlvs(payload: bytes, maximum_total: int = 4096) -> tuple[TLV, ...]:
    if len(payload) > maximum_total:
        raise InvalidMessage("TLV payload exceeds bound")
    r, result = _Reader(payload), []
    while r.at < len(payload):
        tag, typ, size = r.integer(1), r.integer(1), r.integer(2)
        result.append(TLV(tag, typ, r.take(size)))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PicoLog:
    log_sequence: int
    uptime_ms: int
    severity: int
    component: str
    message: str
    detail: bytes = b""

    def encode(self) -> bytes:
        component, message = self.component.encode(), self.message.encode()
        if len(component) > 40 or len(message) > 320 or len(self.detail) > 512:
            raise InvalidMessage("PICO_LOG field exceeds bound")
        return (
            struct.pack(">QQBBHH", self.log_sequence, self.uptime_ms, self.severity,
                        len(component), len(message), len(self.detail))
            + component + message + self.detail
        )

    @classmethod
    def decode(cls, payload: bytes) -> "PicoLog":
        if len(payload) < 22:
            raise InvalidMessage("truncated PICO_LOG")
        r = _Reader(payload)
        seq, uptime, severity = r.integer(8), r.integer(8), r.integer(1)
        component_size, message_size, detail_size = r.integer(1), r.integer(2), r.integer(2)
        if component_size > 40 or message_size > 320 or detail_size > 512:
            raise InvalidMessage("PICO_LOG field exceeds bound")
        try:
            component = r.take(component_size).decode()
            message = r.take(message_size).decode()
        except UnicodeDecodeError as exc:
            raise InvalidMessage("invalid PICO_LOG UTF-8") from exc
        detail = r.take(detail_size)
        r.finish()
        decode_tlvs(detail, 512)
        return cls(seq, uptime, severity, component, message, detail)


@dataclass(frozen=True, slots=True)
class Control:
    command_sequence: int
    issued_at_us: int
    ttl_ms: int
    command: int
    arguments: bytes
    mac: bytes

    def unsigned_payload(self) -> bytes:
        if len(self.arguments) > 128:
            raise InvalidMessage("CONTROL arguments exceed bound")
        return struct.pack(
            ">QQIBB", self.command_sequence, self.issued_at_us, self.ttl_ms,
            self.command, len(self.arguments),
        ) + self.arguments

    def encode(self) -> bytes:
        if len(self.mac) != 32:
            raise InvalidMessage("CONTROL HMAC must be 32 bytes")
        return self.unsigned_payload() + self.mac

    @classmethod
    def decode(cls, payload: bytes) -> "Control":
        if len(payload) < 54:
            raise InvalidMessage("truncated CONTROL")
        r = _Reader(payload)
        seq, issued, ttl, command, size = (
            r.integer(8), r.integer(8), r.integer(4), r.integer(1), r.integer(1)
        )
        if size > 128:
            raise InvalidMessage("CONTROL arguments exceed bound")
        arguments, mac = r.take(size), r.take(32)
        r.finish()
        return cls(seq, issued, ttl, command, arguments, mac)


@dataclass(frozen=True, slots=True)
class ControlResult:
    command_sequence: int
    result: int
    detail: str
    mac: bytes

    def unsigned_payload(self) -> bytes:
        detail = self.detail.encode("utf-8")
        if len(detail) > 160:
            raise InvalidMessage("CONTROL_RESULT detail exceeds bound")
        return struct.pack(">QBBH", self.command_sequence, self.result, 0, len(detail)) + detail

    def encode(self) -> bytes:
        if len(self.mac) != 32:
            raise InvalidMessage("CONTROL_RESULT HMAC must be 32 bytes")
        return self.unsigned_payload() + self.mac

    @classmethod
    def decode(cls, payload: bytes) -> "ControlResult":
        if len(payload) < 44:
            raise InvalidMessage("truncated CONTROL_RESULT")
        r = _Reader(payload)
        seq, result, _reserved, size = r.integer(8), r.integer(1), r.integer(1), r.integer(2)
        if size > 160:
            raise InvalidMessage("CONTROL_RESULT detail exceeds bound")
        try:
            detail = r.take(size).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidMessage("invalid CONTROL_RESULT UTF-8") from exc
        mac = r.take(32)
        r.finish()
        return cls(seq, result, detail, mac)


def encode_bmcu_frame(value: ValidatedBMCUFrame) -> bytes:
    return struct.pack(">QH", value.received_at_us, len(value.wire_bytes)) + value.wire_bytes


def decode_bmcu_frame(payload: bytes, validator) -> ValidatedBMCUFrame:
    if len(payload) < 10:
        raise InvalidMessage("truncated BMCU_FRAME")
    received, size = struct.unpack(">QH", payload[:10])
    if size != len(payload) - 10:
        raise InvalidMessage("embedded wire length mismatch")
    return validate_embedded_frame(received, payload[10:], validator)
