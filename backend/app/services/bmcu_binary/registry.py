"""One-active-session registry for authenticated monitor devices."""

from __future__ import annotations

import asyncio


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions = {}
        self._lock = asyncio.Lock()

    async def install(self, device_id: str, session) -> None:
        async with self._lock:
            previous = self._sessions.get(device_id)
            self._sessions[device_id] = session
        if previous is not None and previous is not session:
            await previous.close()

    async def remove(self, device_id: str, session) -> None:
        async with self._lock:
            if self._sessions.get(device_id) is session:
                self._sessions.pop(device_id, None)

    def get(self, device_id: str):
        return self._sessions.get(device_id)

    @property
    def count(self) -> int:
        return len(self._sessions)

    def sessions(self) -> tuple:
        return tuple(self._sessions.values())
