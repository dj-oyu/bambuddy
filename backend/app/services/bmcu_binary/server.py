"""Lifecycle-managed asyncio TCP listener for BMB1."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.core.database import async_session
from backend.app.models.bmcu_binary import BMCUBinaryDevice

from .persistence import BinaryPersistence
from .provisioning import load_key_file, save_key_file, validate_device_id
from .registry import SessionRegistry
from .session import BinarySession
from .stream_v2 import StreamReceiver

logger = logging.getLogger(__name__)


def configured_keys() -> dict[str, bytes]:
    try:
        raw = json.loads(settings.bmcu_binary_keys)
        if not isinstance(raw, dict):
            raise ValueError
        result = {}
        for device_id, encoded in raw.items():
            key = bytes.fromhex(encoded)
            if not isinstance(device_id, str) or len(device_id.encode()) > 63 or len(key) != 32:
                raise ValueError
            result[device_id] = key
        result.update(load_key_file())
        return result
    except (TypeError, ValueError, OSError, UnicodeError, json.JSONDecodeError):
        logger.error("BMCU binary device key configuration is invalid")
        return {}


class BinaryTransportServer:
    def __init__(self) -> None:
        self.registry = SessionRegistry()
        self.persistence = BinaryPersistence(async_session)
        self._server = None
        self.udp = None
        self._connections: set[asyncio.Task] = set()
        self._keys: dict[str, bytes] = {}
        self._auth_failures = defaultdict(deque)
        self.control_lock = asyncio.Lock()
        self.provisioning_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._server is not None or self.udp is not None:
            return
        if not settings.bmcu_binary_enabled and not settings.bmcu_stream_udp:
            return
        self._keys = configured_keys()
        await self.persistence.rehydrate_current_state()
        if settings.bmcu_binary_enabled:
            self._server = await asyncio.start_server(
                self._accept, settings.bmcu_binary_host, settings.bmcu_binary_port, limit=32 + 4096
            )
            logger.info(
                "BMCU binary TCP listener started on %s:%d", settings.bmcu_binary_host, settings.bmcu_binary_port
            )
        if settings.bmcu_stream_udp:
            async with async_session() as db:
                known = set((await db.execute(select(BMCUBinaryDevice.device_id))).scalars()) | set(self._keys)
            udp = StreamReceiver(
                self.persistence,
                known,
                accept_new=settings.bmcu_stream_accept_new,
                queue_size=settings.bmcu_stream_queue_size,
                batch_size=settings.bmcu_stream_batch_size,
                flush_seconds=settings.bmcu_stream_flush_seconds,
            )
            self.udp = udp
            try:
                await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: udp, local_addr=(settings.bmcu_binary_host, settings.bmcu_binary_port)
                )
                await udp.restore_pending()
            except BaseException:
                await udp.close()
                self.udp = None
                if self._server is not None:
                    self._server.close()
                    await self._server.wait_closed()
                    self._server = None
                raise
            logger.info(
                "BMCU stream UDP listener started on %s:%d", settings.bmcu_binary_host, settings.bmcu_binary_port
            )

    async def _accept(self, reader, writer) -> None:
        task = asyncio.current_task()
        assert task is not None
        peer = writer.get_extra_info("peername")
        peer_key = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        now = time.monotonic()
        if len(self._auth_failures) > 1024:
            self._auth_failures = defaultdict(
                deque,
                {
                    key: deque(value for value in values if value >= now - 60)
                    for key, values in self._auth_failures.items()
                    if any(value >= now - 60 for value in values)
                },
            )
        failures = self._auth_failures[peer_key]
        while failures and failures[0] < now - 60:
            failures.popleft()
        if len(self._connections) >= settings.bmcu_binary_max_connections or len(failures) >= 10:
            writer.close()
            await writer.wait_closed()
            return
        self._connections.add(task)
        session = BinarySession(
            reader,
            writer,
            key_provider=self._keys.get,
            persistence=self.persistence,
            registry=self.registry,
            auth_timeout=settings.bmcu_binary_auth_timeout_s,
            idle_timeout=settings.bmcu_binary_idle_timeout_s,
            write_timeout=settings.bmcu_binary_write_timeout_s,
        )
        try:
            await session.run()
        finally:
            if session.device_id is None:
                failures.append(time.monotonic())
            self._connections.discard(task)

    async def stop(self) -> None:
        if self.udp is not None:
            await self.udp.close()
            self.udp = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        sessions = self.registry.sessions()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)

    def provisioning_keys(self) -> dict[str, bytes]:
        return dict(self._keys)

    async def set_device_key(self, device_id: str, key: bytes) -> None:
        device_id = validate_device_id(device_id)
        if len(key) != 32:
            raise ValueError("device key must be 256 bits")
        async with self.provisioning_lock:
            updated = dict(self._keys)
            updated[device_id] = key
            await asyncio.to_thread(save_key_file, updated)
            self._keys = updated
            if self.udp is not None:
                self.udp.known_devices.add(device_id)
            session = self.registry.get(device_id)
            if session is not None:
                await session.close()


binary_transport_server = BinaryTransportServer()
