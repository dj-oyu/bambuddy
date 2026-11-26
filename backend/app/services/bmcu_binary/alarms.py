"""Durable BMCU motion-fault transitions and MQTT delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import paho.mqtt.client as mqtt
from sqlalchemy import literal_column, select

from backend.app.core.database import async_session
from backend.app.models.bmcu_binary import BMCUBinaryAlarmOutbox
from backend.app.services.mqtt_relay import mqtt_relay

from .bmcu_decoder import BMCUEvent
from .constants import LogSeverity, MessageType, RecordType, StateField
from .messages import PicoLog
from .storage_keys import u64_decimal, u64_hex

logger = logging.getLogger(__name__)


def make_alarm(device_id, frame, semantic, received_at):
    """Create a stable outbox event for a safety transition or critical log."""
    fields = {}
    if (
        isinstance(semantic, BMCUEvent)
        and semantic.record_type == RecordType.STATE_CHANGE
        and semantic.field == StateField.MOTION_FAULT
        and semantic.value is not None
    ):
        fields = {
            "kind": "motion_fault",
            "active": bool(semantic.value),
            "slot": semantic.slot,
            "previous_value": semantic.previous_value,
            "value": semantic.value,
            "severity": semantic.severity,
        }
    elif (
        isinstance(semantic, BMCUEvent)
        and semantic.record_type == RecordType.SAFETY_DECISION
        and semantic.severity >= 3
    ):
        fields = {
            "kind": "safety_decision",
            "severity": semantic.severity,
            "source": semantic.source,
            "detail_hex": semantic.payload.hex(),
        }
    elif frame.header.message_type == MessageType.PICO_LOG:
        log = PicoLog.decode(frame.payload)
        if log.severity >= LogSeverity.ERROR:
            fields = {
                "kind": "pico_log",
                "severity": log.severity,
                "component": log.component,
                "message": log.message,
                "log_sequence": u64_decimal(log.log_sequence),
            }
    if not fields:
        return None
    boot = u64_hex(frame.header.pico_boot_id)
    sequence = u64_decimal(frame.header.transport_sequence)
    identity = json.dumps([device_id, boot, sequence], separators=(",", ":"), ensure_ascii=True)
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    payload = {
        "event_id": event_id,
        **fields,
        "device_id": device_id,
        "pico_boot_id": boot,
        "transport_sequence": sequence,
        "link_index": frame.header.link_index,
        "received_at": received_at.replace(tzinfo=UTC).isoformat(),
    }
    return BMCUBinaryAlarmOutbox(
        event_id=event_id,
        device_id=device_id,
        pico_boot_id=boot,
        transport_sequence=sequence,
        payload=json.dumps(payload, separators=(",", ":")),
        created_at=received_at,
        delivered_at=None,
    )


def make_feed_alarm(device_id, boot_id, link_index, printer_id, event, now, kind):
    """Build an idempotent notification for one server-detected episode/stage."""
    elapsed = getattr(event, "starved_for_s", getattr(event, "degraded_for_s", 0.0))
    started_ms = round((now - elapsed) * 1000)
    identity = json.dumps(
        ["feed_stall", kind, printer_id, device_id, link_index, event.slot, started_ms],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    received_at = datetime.fromtimestamp(now, UTC).replace(tzinfo=None)
    payload = {
        "event_id": event_id,
        "kind": kind,
        "origin": "bambuddy",
        "device_id": device_id,
        "pico_boot_id": u64_hex(boot_id) if boot_id is not None else None,
        "transport_sequence": None,
        "link_index": link_index,
        "printer_id": printer_id,
        "slot": event.slot,
        "pull_percent": event.pull_pct,
        "duration_s": elapsed,
        "current_layer": event.current_layer,
        "degraded_layer": event.degraded_layer,
        "received_at": received_at.replace(tzinfo=UTC).isoformat(),
    }
    return BMCUBinaryAlarmOutbox(
        event_id=event_id,
        device_id=device_id,
        pico_boot_id=u64_hex(boot_id or 0),
        transport_sequence=u64_decimal(0),
        payload=json.dumps(payload, separators=(",", ":")),
        created_at=received_at,
        delivered_at=None,
    )


async def publish_confirmed(relay, payload: str, *, timeout: float = 5, pending: dict | None = None) -> bool:
    """Confirm broker PUBACK, without blocking the event loop or holding a DB lock.

    PUBACK is broker acceptance, not confirmation that an external device acted.
    A crash after PUBACK but before the DB update can cause an intentional retry.
    """
    client = relay.client
    if not relay.enabled or not relay.connected or client is None:
        return False
    prefix = relay.topic_prefix
    try:
        if pending is not None and pending.get("client") is client and pending.get("payload") == payload:
            info = pending["info"]
        else:
            if pending is not None:
                pending.clear()
            info = client.publish(f"{prefix}/bmcu/alarms", payload, qos=1, retain=False)
            if info.rc == mqtt.MQTT_ERR_SUCCESS and pending is not None:
                pending.update(client=client, prefix=prefix, payload=payload, info=info)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        await asyncio.to_thread(info.wait_for_publish, timeout=timeout)
        if pending and info.is_published() and pending.get("prefix") != prefix:
            # Finish the old in-flight packet before retrying under a newly
            # configured prefix; never accumulate duplicate Paho messages.
            pending.clear()
            return False
        confirmed = bool(
            info.is_published()
            and relay.client is client
            and relay.enabled
            and relay.connected
            and relay.topic_prefix == prefix
        )
        if confirmed and pending is not None:
            pending.clear()
        return confirmed
    except (RuntimeError, ValueError, OSError):
        if pending is not None and pending.get("info") is not None and pending["info"].rc != mqtt.MQTT_ERR_SUCCESS:
            pending.clear()
        logger.warning("BMCU MQTT alarm delivery failed; will retry", exc_info=True)
        return False


class AlarmDelivery:
    """Single bounded worker; pending rows survive disconnects and restarts."""

    def __init__(self, session_factory=async_session, relay=mqtt_relay):
        self._session_factory = session_factory
        self._relay = relay
        self._task = None
        self._health_task = None
        self._pending_publish: dict = {}
        self._derived: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._current_derived = None
        self.derived_overflow = 0
        self._wake = asyncio.Event()
        self._receiver_started_at = datetime.now(UTC).isoformat()

    def wake(self):
        self._wake.set()

    @property
    def derived_queue_depth(self):
        return self._derived.qsize() + int(self._current_derived is not None)

    def enqueue_derived(self, event) -> bool:
        try:
            self._derived.put_nowait(event)
        except asyncio.QueueFull:
            self.derived_overflow += 1
            logger.error("BMCU derived alarm queue is full; notification was not persisted")
            return False
        self.wake()
        return True

    async def persist_derived_one(self) -> bool:
        if self._current_derived is None:
            try:
                self._current_derived = self._derived.get_nowait()
            except asyncio.QueueEmpty:
                return False
        event = self._current_derived
        async with self._session_factory() as db:
            if await db.get(BMCUBinaryAlarmOutbox, event.event_id) is None:
                db.add(event)
                await db.commit()
        self._current_derived = None
        self._derived.task_done()
        return True

    async def deliver_one(self):
        if not self._relay.enabled or not self._relay.connected:
            return False
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(BMCUBinaryAlarmOutbox)
                    .where(BMCUBinaryAlarmOutbox.delivered_at.is_(None))
                    .order_by(BMCUBinaryAlarmOutbox.created_at, literal_column("bmcu_binary_alarm_outbox.rowid"))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            event_id, payload = row.event_id, row.payload
        if not await publish_confirmed(self._relay, payload, pending=self._pending_publish):
            return False
        async with self._session_factory() as db:
            row = await db.get(BMCUBinaryAlarmOutbox, event_id)
            if row and row.delivered_at is None:
                row.delivered_at = datetime.now(UTC).replace(tzinfo=None)
                await db.commit()
        return True

    async def _run(self):
        while True:
            try:
                if await self.persist_derived_one():
                    await asyncio.sleep(0)
                    continue
                if await self.deliver_one():
                    # Give ingestion and other services a chance to run even
                    # while catching up after a long broker outage.
                    await asyncio.sleep(0.05)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BMCU alarm worker failed; will retry")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    def publish_health(self):
        """Publish expiring retained v2 health; it is not an alarm ACK."""
        if not self._relay.enabled or not self._relay.connected:
            return
        # Deferred import avoids the transport server/persistence cycle.
        from .server import binary_transport_server

        receiver = binary_transport_server.udp
        if receiver is None:
            return
        now, now_m = datetime.now(UTC), time.monotonic()
        for device_id, state in tuple(receiver.states.items()):
            if not state.heartbeat_at:
                continue
            heartbeat_age = max(0.0, now_m - state.heartbeat_at)
            observed_at = now - timedelta(seconds=heartbeat_age)
            valid_until = observed_at + timedelta(seconds=3 * state.heartbeat_interval)
            links = []
            for index, (link_id, code) in sorted(state.links.items()):
                seen = binary_transport_server.persistence.current_state_seen.get((device_id, index))
                age = now_m - seen if seen is not None and seen <= now_m else None
                links.append(
                    {
                        "link_index": index,
                        "link_id": link_id,
                        "state": "online"
                        if state.health() == "online" and code == 2 and age is not None and age < 20
                        else "unknown",
                        "status_age_s": round(age, 1) if age is not None else None,
                        "valid_until": min(valid_until, now + timedelta(seconds=20 - age)).isoformat()
                        if age is not None
                        else None,
                    }
                )
            counters = receiver.device_counters.get(device_id, {})
            payload = {
                "device_id": device_id,
                "pico_boot_id": u64_hex(state.boot_id),
                "availability": state.health(),
                "observed_at": observed_at.isoformat(),
                "valid_until": valid_until.isoformat(),
                "receiver_started_at": self._receiver_started_at,
                "links": links,
                "gaps_detected_total": counters.get("gaps_detected", 0),
                "ingest_overflow_total": counters.get("ingest_overflow", 0),
            }
            self._relay._publish(
                f"{self._relay.topic_prefix}/bmcu/{quote(device_id, safe='')}/status", payload, retain=True
            )

    async def _health_loop(self):
        while True:
            try:
                self.publish_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BMCU MQTT health publication failed")
            await asyncio.sleep(5)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="bmcu-mqtt-alarms")
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_loop(), name="bmcu-mqtt-health")

    async def stop(self):
        if self._task and (self._current_derived is not None or not self._derived.empty()):
            try:
                await asyncio.wait_for(self._derived.join(), timeout=2)
            except TimeoutError:
                logger.error("BMCU derived alarms remain in memory at shutdown")
        if self._health_task:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
            self._health_task = None
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


alarm_delivery = AlarmDelivery()
