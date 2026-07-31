"""Lifecycle-managed asyncio TCP listener for BMB1."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque

from backend.app.core.config import settings
from backend.app.core.database import async_session

from .persistence import BinaryPersistence
from .provisioning import load_key_file, save_key_file, validate_device_id
from .registry import SessionRegistry
from .session import BinarySession

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
        self._connections: set[asyncio.Task] = set()
        self._keys: dict[str, bytes] = {}
        self._auth_failures = defaultdict(deque)
        self.control_lock = asyncio.Lock()
        self.provisioning_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._server is not None or not settings.bmcu_binary_enabled:
            return
        self._keys = configured_keys()
        await self.persistence.rehydrate_current_state()
        self._server = await asyncio.start_server(
            self._accept,
            settings.bmcu_binary_host,
            settings.bmcu_binary_port,
            limit=32 + 4096,
        )
        logger.info("BMCU binary listener started on %s:%d", settings.bmcu_binary_host, settings.bmcu_binary_port)

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
        if self._server is None:
            return
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
            session = self.registry.get(device_id)
            if session is not None:
                await session.close()


binary_transport_server = BinaryTransportServer()
