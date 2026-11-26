"""Bounded wakeups for BMCU live consumers; telemetry ingest never awaits them."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SharedSnapshot:
    generation: int
    built_at: float
    event: str
    signature: tuple[Any, ...]


class LiveBroker:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._generation: dict[str, int] = {}
        self._snapshots: dict[str, SharedSnapshot] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    def subscribe(self, device_id: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=1)
        self._subscribers[device_id].add(queue)
        return queue

    def unsubscribe(self, device_id: str, queue: asyncio.Queue) -> None:
        listeners = self._subscribers.get(device_id)
        if listeners is not None:
            listeners.discard(queue)
            if not listeners:
                self._subscribers.pop(device_id, None)
                self._generation.pop(device_id, None)
                self._snapshots.pop(device_id, None)
                timer = self._timers.pop(device_id, None)
                if timer:
                    timer.cancel()

    def notify(self, device_id: str) -> None:
        listeners = self._subscribers.get(device_id)
        if not listeners:
            return
        self._generation[device_id] = self._generation.get(device_id, 0) + 1
        self._signal(device_id)

    def _wake(self, device_id: str) -> None:
        self._timers.pop(device_id, None)
        self._signal(device_id)

    def _signal(self, device_id: str) -> None:
        for queue in tuple(self._subscribers.get(device_id, ())):
            if queue.empty():
                queue.put_nowait(None)

    def snapshot(self, device_id: str, build: Callable[[], dict]) -> SharedSnapshot:
        """Serialize once per device per 250 ms, shared by subscribers.

        An idle snapshot expires after one second so time-based stale/offline
        transitions remain current without polling the database.
        """
        generation = self._generation.get(device_id, 0)
        now = time.monotonic()
        cached = self._snapshots.get(device_id)
        if cached:
            age = now - cached.built_at
            if cached.generation == generation and age < 1:
                return cached
            if age < 0.25:
                if device_id not in self._timers:
                    self._timers[device_id] = asyncio.get_running_loop().call_later(0.25 - age, self._wake, device_id)
                return cached
        payload = build()
        signature = (
            payload["detail"]["health"],
            tuple((link["linkIndex"], link["state"]) for link in payload["detail"]["links"]),
        )
        result = SharedSnapshot(
            generation, now, "event: snapshot\ndata: " + json.dumps(payload, separators=(",", ":")) + "\n\n", signature
        )
        self._snapshots[device_id] = result
        return result


live_broker = LiveBroker()
