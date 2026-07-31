"""Authenticated bounded BMB1 TCP session."""

from __future__ import annotations

import asyncio
import hmac
import os
import time

from .auth import control_mac, derive_session_key, verify_hello_mac
from .constants import MessageType
from .errors import BinaryProtocolError
from .framing import Frame, FrameHeader, IncrementalFrameParser, encode_frame
from .messages import (
    Ack, Control, ControlResult, Hello, HelloAccepted, LinkStateMessage, PicoLog, Ping,
    ServerChallenge, TransportDrop, decode_tlvs,
)

GLOBAL_SCOPE = 0xFF
DURABLE_TYPES = frozenset((
    MessageType.BMCU_FRAME, MessageType.LINK_STATE, MessageType.TRANSPORT_DROP,
    MessageType.PICO_DIAGNOSTIC, MessageType.PICO_LOG,
))


class BinarySession:
    def __init__(
        self, reader, writer, *, key_provider, persistence, registry,
        auth_timeout: float, idle_timeout: float, write_timeout: float,
    ) -> None:
        self.reader, self.writer = reader, writer
        self.key_provider, self.persistence, self.registry = key_provider, persistence, registry
        self.auth_timeout, self.idle_timeout, self.write_timeout = auth_timeout, idle_timeout, write_timeout
        self.parser = IncrementalFrameParser()
        self.device_id: str | None = None
        self.session_key: bytes | None = None
        self.authenticated_boot_id: int | None = None
        self.control_epoch_ns: int | None = None
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._pending: list[Frame] = []

    async def run(self) -> None:
        challenge = os.urandom(32)
        await self._send(FrameHeader(MessageType.SERVER_CHALLENGE, link_index=GLOBAL_SCOPE),
                         ServerChallenge(challenge).encode())
        try:
            hello_frame = await asyncio.wait_for(self._next_frame(), self.auth_timeout)
            if hello_frame.header.message_type != MessageType.HELLO:
                raise BinaryProtocolError("HELLO required before telemetry")
            hello = Hello.decode(hello_frame.payload)
            if hello.replay_ranges[0].pico_boot_id != hello_frame.header.pico_boot_id:
                raise BinaryProtocolError("HELLO replay table must list current boot first")
            key = self.key_provider(hello.device_id)
            candidate_key = key if key is not None else b"\0" * 32
            verified = verify_hello_mac(
                candidate_key, challenge, hello_frame.header.pico_boot_id, hello.transcript(), hello.mac,
            )
            if key is None or not verified:
                raise BinaryProtocolError("authentication failed")
            self.device_id = hello.device_id
            self.authenticated_boot_id = hello_frame.header.pico_boot_id
            self.session_key = derive_session_key(key, challenge, hello_frame.header.pico_boot_id)
            watermark = await self.persistence.register_hello(hello, hello_frame.header.pico_boot_id)
            await self.registry.install(self.device_id, self)
            self.control_epoch_ns = time.monotonic_ns()
            await self._send(
                FrameHeader(MessageType.HELLO_ACCEPTED, pico_boot_id=hello_frame.header.pico_boot_id,
                            link_index=GLOBAL_SCOPE),
                HelloAccepted(watermark, 5000, 15000).encode(),
            )
            while not self._closed:
                await self._handle(await asyncio.wait_for(self._next_frame(), self.idle_timeout))
        except (EOFError, asyncio.TimeoutError, ConnectionError, BinaryProtocolError):
            pass
        finally:
            await self.close()
            if self.device_id:
                await self.registry.remove(self.device_id, self)

    async def _next_frame(self) -> Frame:
        while True:
            if self._pending:
                return self._pending.pop(0)
            chunk = await self.reader.read(4096)
            if not chunk:
                raise EOFError
            frames = self.parser.feed(chunk)
            if frames:
                # StreamReader may yield multiple frames. Preserve extras without
                # an unbounded side queue by feeding them through a small list.
                if len(frames) > 1:
                    self._pending.extend(frames[1:])
                return frames[0]

    async def _handle(self, frame: Frame) -> None:
        typ = frame.header.message_type
        if typ == MessageType.PING:
            ping = Ping.decode(frame.payload)
            await self._send(
                FrameHeader(MessageType.PONG, pico_boot_id=frame.header.pico_boot_id,
                            link_index=GLOBAL_SCOPE), ping.encode(),
            )
            return
        if typ == MessageType.PONG:
            Ping.decode(frame.payload)
            return
        if typ == MessageType.CONTROL_RESULT:
            if frame.header.transport_sequence != 0 or frame.header.pico_boot_id != self.authenticated_boot_id:
                raise BinaryProtocolError("CONTROL_RESULT must be current-session non-durable")
            result = ControlResult.decode(frame.payload)
            assert self.session_key is not None
            expected = control_mac(self.session_key, frame.header.encode(), result.unsigned_payload())
            if not hmac.compare_digest(expected, result.mac):
                raise BinaryProtocolError("invalid CONTROL_RESULT authentication")
            await self.persistence.persist_control_result(self.device_id, frame.header.pico_boot_id, result)
            return
        if typ not in DURABLE_TYPES or frame.header.transport_sequence == 0:
            raise BinaryProtocolError("unexpected or non-durable telemetry")
        if frame.header.pico_boot_id != self.authenticated_boot_id and not (frame.header.flags & 1):
            raise BinaryProtocolError("historical boot record requires REPLAY flag")
        if typ in (MessageType.PICO_DIAGNOSTIC, MessageType.PICO_LOG) and frame.header.link_index != GLOBAL_SCOPE:
            raise BinaryProtocolError("bridge record requires global link scope")
        if typ == MessageType.LINK_STATE:
            LinkStateMessage.decode(frame.payload)
        elif typ == MessageType.TRANSPORT_DROP:
            TransportDrop.decode(frame.payload)
        elif typ == MessageType.PICO_DIAGNOSTIC:
            decode_tlvs(frame.payload)
        elif typ == MessageType.PICO_LOG:
            PicoLog.decode(frame.payload)
        watermark = await self.persistence.persist(self.device_id, frame)
        ack = Ack(frame.header.pico_boot_id, watermark)
        await self._send(
            FrameHeader(MessageType.ACK, pico_boot_id=frame.header.pico_boot_id,
                        link_index=GLOBAL_SCOPE),
            ack.encode(),
        )

    async def send_control(
        self, *, link_index: int, command_sequence: int, ttl_ms: int,
        command: int, arguments: bytes = b"",
    ) -> None:
        if (
            self._closed or self.session_key is None or self.control_epoch_ns is None
            or self.authenticated_boot_id is None
        ):
            raise BinaryProtocolError("session is not authenticated")
        issued_at_us = max(0, (time.monotonic_ns() - self.control_epoch_ns) // 1000)
        unsigned = Control(command_sequence, issued_at_us, ttl_ms, command, arguments, b"\0" * 32)
        header = FrameHeader(
            MessageType.CONTROL, payload_length=len(unsigned.unsigned_payload()) + 32,
            pico_boot_id=self.authenticated_boot_id, link_index=link_index,
        )
        signed = Control(
            command_sequence, issued_at_us, ttl_ms, command, arguments,
            control_mac(self.session_key, header.encode(), unsigned.unsigned_payload()),
        )
        await self._send(header, signed.encode())

    async def _send(self, header: FrameHeader, payload: bytes) -> None:
        raw = encode_frame(header, payload)
        async with self._write_lock:
            self.writer.write(raw)
            await asyncio.wait_for(self.writer.drain(), self.write_timeout)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, AttributeError):
            pass
