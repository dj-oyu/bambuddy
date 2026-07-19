"""BMCU Link adapter service (observe-only).

A Raspberry Pi Pico 2 W bridge pushes JSON envelopes (schema
"bmcu.management.v2") to bambuddy over HTTP/WebSocket; bambuddy is the
server. This service deduplicates, batches inserts, tracks per-device
link state (online/stale/offline), prunes old rows, and fans out
websocket broadcasts / notifications.

Fail-safe by design: every ingest/watchdog error is logged and
swallowed — a misbehaving bridge must never break the app.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select

from backend.app.core.config import settings
from backend.app.core.websocket import ws_manager
from backend.app.models.bmcu_link_device import BMCULinkDevice
from backend.app.models.bmcu_link_event import BMCULinkEvent
from backend.app.schemas.bmcu_link import (
    BMCULinkEnvelope,
    BMCULinkHelloData,
    BMCULinkIngestResponse,
    BMCULinkPersistedKey,
    BMCULinkRejected,
)

logger = logging.getLogger(__name__)

_ENUM_REGISTRY_BUNDLED = Path(__file__).parent / "bmcu_link_enums.json"


def bmcu_link_enabled() -> bool:
    """Feature toggle: BAMBUDDY_BMCU_LINK=0 disables the whole adapter."""
    return os.environ.get("BAMBUDDY_BMCU_LINK", "1") != "0"


def get_enum_registry() -> dict[str, Any]:
    """Enum registry (kind/severity/outcome/reason id→name maps).

    A file at <data dir>/bmcu_link_enums.json overrides the bundled copy so
    firmware-side enum additions don't require a bambuddy release.
    """
    override = settings.base_dir / "bmcu_link_enums.json"
    for path in (override, _ENUM_REGISTRY_BUNDLED):
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                # The firmware-distributed bmcu_link_enum_registry.json nests
                # its tables under "enums"; flatten to the id→name maps the
                # frontend consumes, keeping registry metadata alongside.
                if isinstance(raw.get("enums"), dict):
                    return {
                        "registry_version": raw.get("registry_version", 0),
                        "wire_protocol": raw.get("wire_protocol"),
                        **raw["enums"],
                    }
                return raw
        except Exception as e:
            logger.warning("Failed to load BMCU Link enum registry from %s: %s", path, e)
    return {"registry_version": 0}


ANOMALY_KINDS = {"anomaly", "dropped", "transport_drop"}


class BMCULinkService:
    FLUSH_INTERVAL_S = 2.0
    FLUSH_BATCH_SIZE = 200
    DEDUP_WINDOW = 4096
    STALE_AFTER_S = 10
    OFFLINE_AFTER_S = 30
    RETENTION_DAYS = 14
    RETENTION_MAX_ROWS_PER_DEVICE = 200_000
    SUMMARY_THROTTLE_S = 2.0
    PENDING_CAP = 10_000
    RETENTION_INTERVAL_S = 15 * 60
    RETENTION_DELETE_BATCH = 5_000
    ANOMALY_NOTIFY_COOLDOWN_S = 60.0  # per-device latch on provider notifications
    MAX_TRACKED_DEVICES = 16  # in-memory device cap (ingest may be unauthenticated)
    MAX_LINKS_PER_DEVICE = 8  # per-device link.id cap (same rationale as device cap)
    MAX_BATCH = 500  # max envelopes per ingest call / WS message
    EVICT_AFTER_S = 60 * 60  # drop in-memory state for devices offline this long

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []
        # (device_id, link_id) -> (boot_key, deque of dedup keys, set of dedup keys)
        self._dedup: dict[tuple[str, str], tuple[tuple[str, int], deque, set]] = {}
        # (device_id, link_id) -> (pico_boot_session, transport_sequence)
        # of the newest row confirmed flushed to DB (replay-buffer watermark).
        self._persisted_key: dict[tuple[str, str], tuple[str, int]] = {}
        # (device_id, link_id) -> pico_boot_session in which a staged row was
        # dropped before commit. While set, the watermark must NOT advance
        # for that session — otherwise the Pico would trim replay-buffer
        # entries covering the gap. A new boot session clears it.
        self._watermark_gap: dict[tuple[str, str], str] = {}
        self._last_seen: dict[str, float] = {}  # device_id -> monotonic
        self._link_state: dict[str, str] = {}  # device_id -> online|stale|offline
        self._last_status: dict[str, str] = {}  # device_id -> JSON, persisted on flush
        self._last_status_broadcast: dict[str, float] = {}
        self._envelope_counts: dict[str, int] = {}  # unflushed increments
        # dropped_count semantics: firmware reports a CUMULATIVE-since-boot
        # value, so per-envelope summing would double-count. We track the
        # latest value per (device, boot session); on a boot-session change
        # the previous session's final value is folded into a running total.
        # device.dropped_count = DB baseline + folded sessions + current latest.
        self._dropped_session: dict[str, tuple[str, int]] = {}  # device -> (pico_boot_session, latest)
        self._dropped_folded: dict[str, int] = {}  # device -> folded past-session totals (this process)
        self._dropped_baseline: dict[str, int] = {}  # device -> DB value captured on first flush
        self._dropped_dirty: set[str] = set()
        self._last_anomaly_notify: dict[str, float] = {}  # device -> monotonic latch
        self._known_devices: set[str] = set()  # device_ids seen (hello upserted)
        self._server_dropped = 0  # envelopes dropped by PENDING_CAP
        self._rejected_device_envelopes = 0  # rejected by MAX_TRACKED_DEVICES
        self._device_cap_warned = False
        self._flush_lock = asyncio.Lock()
        self._flush_retry_pending = False  # one implicit retry after a failed flush
        self._prune_task: asyncio.Task | None = None
        self._last_flush = time.monotonic()
        self._last_retention = time.monotonic()
        # Test seams
        self._clock = time.monotonic
        self._session_factory = None  # override for tests; default core async_session

    # ------------------------------------------------------------------ infra

    def _sessionmaker(self):
        if self._session_factory is not None:
            return self._session_factory
        from backend.app.core.database import async_session

        return async_session

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    # ------------------------------------------------------------------ dedup

    def _dedup_key(self, env: BMCULinkEnvelope) -> tuple:
        # Issue #2 contract: transport_sequence (u64, unique per envelope) is
        # the key; bmcu_boot_session/BMCU sequence are diagnostics only.
        return (
            env.device_id,
            env.link.id,
            env.link.pico_boot_session,
            env.link.dedup_sequence,
        )

    def _dedup_entry(self, env: BMCULinkEnvelope) -> tuple[str, deque, set]:
        """Per-(device, link) dedup window; a Pico boot-session change resets
        it — transport sequences restart after reboot, old keys are stale.

        Known trade (issue #2): legacy senders without transport_sequence can
        be falsely deduplicated when the BMCU reboots mid-Pico-session (u16
        sequence restarts inside a live window). Production bridges and the
        commissioning poller are immune (monotonic transport_sequence)."""
        boot_key = env.link.pico_boot_session
        entry = self._dedup.get((env.device_id, env.link.id))
        if entry is None or entry[0] != boot_key:
            entry = (boot_key, deque(), set())
            self._dedup[(env.device_id, env.link.id)] = entry
        return entry

    def _check_duplicate(self, env: BMCULinkEnvelope) -> bool:
        """Duplicate check WITHOUT recording (record after successful staging
        so a transient ingest error doesn't permanently eat the envelope)."""
        _, _, seen = self._dedup_entry(env)
        return self._dedup_key(env) in seen

    def _record_key(self, env: BMCULinkEnvelope) -> None:
        _, dq, seen = self._dedup_entry(env)
        key = self._dedup_key(env)
        if key in seen:
            return
        dq.append(key)
        seen.add(key)
        while len(dq) > self.DEDUP_WINDOW:
            seen.discard(dq.popleft())

    def is_duplicate(self, env: BMCULinkEnvelope) -> bool:
        """Check + record the envelope in the per-device dedup window."""
        if self._check_duplicate(env):
            return True
        self._record_key(env)
        return False

    def mark_seen(self, device_id: str) -> None:
        self._last_seen[device_id] = self._clock()

    def persisted_keys(self, pairs: set[tuple[str, str]]) -> list[BMCULinkPersistedKey]:
        """Replay-buffer watermarks for the given (device_id, link_id) pairs.
        The Pico may discard buffered envelopes up to (and including) these
        keys; anything newer must be replayed on reconnect."""
        out = []
        for pair in sorted(pairs):
            wm = self._persisted_key.get(pair)
            if wm is not None:
                out.append(
                    BMCULinkPersistedKey(
                        link_id=pair[1],
                        pico_boot_session=wm[0],
                        transport_sequence=wm[1],
                    )
                )
        return out

    # ----------------------------------------------------------------- ingest

    def _device_admitted(self, device_id: str) -> bool:
        """Cap in-memory tracked devices — ingest can be unauthenticated when
        auth is off, so an attacker-chosen device_id must not grow state."""
        if device_id in self._last_seen or len(self._last_seen) < self.MAX_TRACKED_DEVICES:
            return True
        self._rejected_device_envelopes += 1
        if not self._device_cap_warned:
            self._device_cap_warned = True
            logger.warning(
                "BMCU Link device cap (%d) reached; rejecting envelopes from new device %r",
                self.MAX_TRACKED_DEVICES,
                device_id,
            )
        return False

    def _link_admitted(self, env: BMCULinkEnvelope) -> bool:
        """Cap distinct link ids per device — link.id is wire-controlled and
        each (device, link) pair holds a dedup window in memory."""
        pair = (env.device_id, env.link.id)
        if pair in self._dedup:
            return True
        links = sum(1 for k in self._dedup if k[0] == env.device_id)
        if links < self.MAX_LINKS_PER_DEVICE:
            return True
        self._rejected_device_envelopes += 1
        return False

    def _unrecord_dropped_row(self, row: dict) -> None:
        """A staged row was dropped before commit: un-record its dedup key so
        a Pico replay is not silently deduplicated, and freeze the watermark
        for its boot session so the replay buffer is not trimmed past it."""
        pair = (row["device_id"], row["link_id"])
        boot = row["pico_boot_session"]
        entry = self._dedup.get(pair)
        if entry is not None and entry[0] == boot:
            seq = row["transport_sequence"] if row["transport_sequence"] is not None else row["uart_sequence"]
            entry[2].discard((row["device_id"], row["link_id"], boot, seq))
        self._watermark_gap[pair] = boot

    async def ingest(self, envelopes: list[BMCULinkEnvelope]) -> BMCULinkIngestResponse:
        if len(envelopes) > self.MAX_BATCH:
            logger.warning("BMCU Link ingest batch of %d exceeds MAX_BATCH=%d; truncating",
                           len(envelopes), self.MAX_BATCH)
            envelopes = envelopes[: self.MAX_BATCH]
        accepted = 0
        deduplicated = 0
        rejected: list[BMCULinkRejected] = []
        for idx, env in enumerate(envelopes):
            tseq = env.link.transport_sequence
            try:
                if not self._device_admitted(env.device_id):
                    rejected.append(
                        BMCULinkRejected(index=idx, transport_sequence=tseq, code="device_cap", retryable=False)
                    )
                    continue
                if not self._link_admitted(env):
                    rejected.append(
                        BMCULinkRejected(index=idx, transport_sequence=tseq, code="link_cap", retryable=False)
                    )
                    continue
                if self._check_duplicate(env):
                    deduplicated += 1
                    continue
                await self._ingest_one(env)
                # Record the dedup key only after successful staging (fix #6)
                self._record_key(env)
                accepted += 1
            except Exception:
                logger.exception("BMCU Link ingest failed for device %s", getattr(env, "device_id", "?"))
                rejected.append(
                    BMCULinkRejected(index=idx, transport_sequence=tseq, code="internal", retryable=True)
                )
        try:
            if len(self._pending) >= self.FLUSH_BATCH_SIZE:
                await self.flush()
        except Exception:
            logger.exception("BMCU Link flush after ingest failed")
        return BMCULinkIngestResponse(
            accepted=accepted,
            deduplicated=deduplicated,
            rejected=rejected,
            persisted=self.persisted_keys({(e.device_id, e.link.id) for e in envelopes}),
        )

    async def _ingest_one(self, env: BMCULinkEnvelope) -> None:
        now_dt = self._utcnow()
        kind = env.frame.kind
        data = env.data or {}

        transaction_id = None
        if kind == "printer_transaction":
            txn = data.get("transaction_id")
            if txn is not None:
                transaction_id = str(txn)

        row = {
            "device_id": env.device_id,
            "link_id": env.link.id,
            "pico_boot_session": env.link.pico_boot_session,
            # Transport-link envelopes carry no BMCU session; the events
            # column is NOT NULL, so store 0 (diagnostics-only field).
            "bmcu_boot_session": env.link.bmcu_boot_session if env.link.bmcu_boot_session is not None else 0,
            "uart_sequence": env.link.uart_sequence if env.link.uart_sequence is not None else 0,
            "transport_sequence": env.link.transport_sequence,
            "kind": kind,
            "kind_id": env.frame.kind_id,
            "protocol": env.frame.protocol,
            "received_at_us": env.received_at_us,
            "received_at": (
                env.received_at.astimezone(timezone.utc).replace(tzinfo=None) if env.received_at else None
            ),
            "server_received_at": now_dt,
            "transaction_id": transaction_id,
            "data": json.dumps(data),
        }
        if len(self._pending) >= self.PENDING_CAP:
            # Drop-oldest so fresh data survives a DB stall; count and shout.
            self._unrecord_dropped_row(self._pending.pop(0))
            self._server_dropped += 1
            with contextlib.suppress(Exception):
                await ws_manager.broadcast(
                    {
                        "type": "bmcu_link_anomaly",
                        "device_id": env.device_id,
                        "kind": "server_dropped",
                        "server_dropped": self._server_dropped,
                    }
                )
        self._pending.append(row)

        self.mark_seen(env.device_id)
        self._envelope_counts[env.device_id] = self._envelope_counts.get(env.device_id, 0) + 1

        # Any envelope from an offline/stale device brings it back online.
        prev_state = self._link_state.get(env.device_id)
        self._link_state[env.device_id] = "online"
        if prev_state in ("stale", "offline"):
            await self._broadcast_state(env.device_id, "online")

        if kind == "hello":
            await self._handle_hello(env, now_dt)
        elif kind == "status":
            self._last_status[env.device_id] = json.dumps(data)
            mono = self._clock()
            if mono - self._last_status_broadcast.get(env.device_id, 0.0) >= self.SUMMARY_THROTTLE_S:
                self._last_status_broadcast[env.device_id] = mono
                with contextlib.suppress(Exception):
                    await ws_manager.broadcast(
                        {"type": "bmcu_link_status", "device_id": env.device_id, "data": data}
                    )

        dropped = data.get("dropped_count")
        if kind in ANOMALY_KINDS or dropped:
            if dropped:
                # Contract: dropped_count is cumulative per PICO boot and
                # never resets otherwise — keep the latest per Pico session,
                # folding finished sessions into a running total.
                boot_key = env.link.pico_boot_session
                prev = self._dropped_session.get(env.device_id)
                if prev is not None and prev[0] != boot_key:
                    self._dropped_folded[env.device_id] = self._dropped_folded.get(env.device_id, 0) + prev[1]
                    prev = None
                latest = max(int(dropped), prev[1] if prev else 0)
                self._dropped_session[env.device_id] = (boot_key, latest)
                self._dropped_dirty.add(env.device_id)
            with contextlib.suppress(Exception):
                await ws_manager.broadcast(
                    {"type": "bmcu_link_anomaly", "device_id": env.device_id, "kind": kind, "data": data}
                )
            await self._notify_anomaly(env.device_id, kind, data)

    async def _handle_hello(self, env: BMCULinkEnvelope, now_dt: datetime) -> None:
        """Upsert the device row immediately (own session) on hello frames."""
        hello = BMCULinkHelloData.model_validate(env.data or {})
        is_new = False
        async with self._sessionmaker()() as db:
            result = await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == env.device_id))
            device = result.scalar_one_or_none()
            if device is None:
                is_new = True
                device = BMCULinkDevice(device_id=env.device_id, first_seen_at=now_dt)
                db.add(device)
            device.firmware = hello.firmware
            device.protocol_min = hello.protocol_min
            device.protocol_max = hello.protocol_max
            device.capabilities = json.dumps(hello.capabilities)
            device.mode = env.mode or hello.mode
            device.link_state = "online"
            device.pico_boot_session = env.link.pico_boot_session
            # A transport HELLO (link.id == "transport") has no BMCU session;
            # keep the last known one instead of clobbering it with None.
            if env.link.bmcu_boot_session is not None:
                device.bmcu_boot_session = env.link.bmcu_boot_session
            device.last_seen_at = now_dt
            if device.first_seen_at is None:
                device.first_seen_at = now_dt
            await db.commit()
        self._known_devices.add(env.device_id)
        if is_new:
            with contextlib.suppress(Exception):
                await ws_manager.broadcast(
                    {
                        "type": "bmcu_link_device_registered",
                        "device_id": env.device_id,
                        "firmware": hello.firmware,
                        "mode": hello.mode,
                    }
                )

    # ------------------------------------------------------------------ flush

    async def flush(self) -> None:
        """Bulk-insert pending event rows and persist cached device state."""
        async with self._flush_lock:
            rows = self._pending
            self._pending = []
            statuses = self._last_status
            self._last_status = {}
            env_counts = self._envelope_counts
            self._envelope_counts = {}
            dropped_dirty = self._dropped_dirty
            self._dropped_dirty = set()
            self._last_flush = self._clock()
            if not rows and not statuses and not env_counts and not dropped_dirty:
                return
            try:
                async with self._sessionmaker()() as db:
                    if rows:
                        await db.execute(insert(BMCULinkEvent).values(rows))
                    device_ids = set(statuses) | set(env_counts) | dropped_dirty
                    if device_ids:
                        result = await db.execute(
                            select(BMCULinkDevice).where(BMCULinkDevice.device_id.in_(device_ids))
                        )
                        now_dt = self._utcnow()
                        for device in result.scalars().all():
                            did = device.device_id
                            if did in statuses:
                                device.last_status = statuses[did]
                            device.envelope_count = (device.envelope_count or 0) + env_counts.get(did, 0)
                            if did in dropped_dirty:
                                # baseline (pre-restart DB total) captured once;
                                # thereafter total = baseline + folded + latest.
                                if did not in self._dropped_baseline:
                                    self._dropped_baseline[did] = device.dropped_count or 0
                                session = self._dropped_session.get(did)
                                device.dropped_count = (
                                    self._dropped_baseline[did]
                                    + self._dropped_folded.get(did, 0)
                                    + (session[1] if session else 0)
                                )
                            if did in self._last_seen:
                                device.last_seen_at = now_dt
                            device.link_state = self._link_state.get(did, device.link_state)
                    await db.commit()
                self._flush_retry_pending = False
                # Rows are committed: advance the per-(device, link) replay
                # watermark to the newest row of each pair (ingest order) —
                # unless this boot session has a drop gap, in which case the
                # watermark stays put so the Pico keeps replaying the gap.
                for r in rows:
                    pair = (r["device_id"], r["link_id"])
                    boot = r["pico_boot_session"]
                    gap = self._watermark_gap.get(pair)
                    if gap == boot:
                        continue
                    if gap is not None:
                        del self._watermark_gap[pair]  # new session: gap moot
                    seq = r["transport_sequence"] if r["transport_sequence"] is not None else r["uart_sequence"]
                    self._persisted_key[pair] = (boot, seq)
            except Exception:
                if not self._flush_retry_pending and rows:
                    # One implicit retry: put the rows back for the next flush,
                    # respecting PENDING_CAP (drop-oldest).
                    self._flush_retry_pending = True
                    self._pending = rows + self._pending
                    overflow = len(self._pending) - self.PENDING_CAP
                    if overflow > 0:
                        for dropped in self._pending[:overflow]:
                            self._unrecord_dropped_row(dropped)
                        del self._pending[:overflow]
                        self._server_dropped += overflow
                    self._dropped_dirty |= dropped_dirty
                    logger.exception("BMCU Link flush failed; re-queued %d rows for one retry", len(rows))
                else:
                    logger.exception("BMCU Link flush failed (%d rows lost)", len(rows))

    # --------------------------------------------------------------- watchdog

    async def watchdog_tick(self) -> None:
        """1s cadence: state transitions, time-based flush, retention prune."""
        try:
            now = self._clock()
            for device_id, seen in list(self._last_seen.items()):
                elapsed = now - seen
                if elapsed >= self.OFFLINE_AFTER_S:
                    target = "offline"
                elif elapsed >= self.STALE_AFTER_S:
                    target = "stale"
                else:
                    target = "online"
                current = self._link_state.get(device_id, "offline")
                if target != current:
                    self._link_state[device_id] = target
                    await self._broadcast_state(device_id, target)
                    await self._persist_state(device_id, target)
                    if target == "offline":
                        await self._notify_offline(device_id)
                elif target == "offline" and elapsed >= self.EVICT_AFTER_S:
                    self._evict_device(device_id)

            if self._pending and now - self._last_flush >= self.FLUSH_INTERVAL_S:
                await self.flush()

            if now - self._last_retention >= self.RETENTION_INTERVAL_S:
                self._last_retention = now
                # Prune in its own task so bounded-but-slow DELETEs never
                # occupy the 1s tick; skip if the previous prune still runs.
                if self._prune_task is None or self._prune_task.done():
                    self._prune_task = asyncio.create_task(self._prune_retention())
        except Exception:
            logger.exception("BMCU Link watchdog tick failed")

    def _evict_device(self, device_id: str) -> None:
        """Drop in-memory state for a long-offline device (DB rows stay)."""
        for keyed in (self._dedup, self._persisted_key, self._watermark_gap):
            for key in [k for k in keyed if k[0] == device_id]:
                keyed.pop(key, None)
        for store in (
            self._last_seen,
            self._link_state,
            self._last_status,
            self._last_status_broadcast,
            self._envelope_counts,
            self._dropped_session,
            self._dropped_folded,
            self._dropped_baseline,
            self._last_anomaly_notify,
        ):
            store.pop(device_id, None)
        self._dropped_dirty.discard(device_id)
        self._known_devices.discard(device_id)
        logger.info("BMCU Link evicted in-memory state for long-offline device %s", device_id)

    async def _broadcast_state(self, device_id: str, state: str) -> None:
        with contextlib.suppress(Exception):
            await ws_manager.broadcast(
                {"type": "bmcu_link_device_state", "device_id": device_id, "state": state}
            )

    async def _persist_state(self, device_id: str, state: str) -> None:
        try:
            async with self._sessionmaker()() as db:
                result = await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == device_id))
                device = result.scalar_one_or_none()
                if device is not None:
                    device.link_state = state
                    await db.commit()
        except Exception:
            logger.exception("BMCU Link failed to persist state for %s", device_id)

    async def _prune_retention(self) -> None:
        """Bounded deletes: drop rows older than RETENTION_DAYS and rows
        beyond RETENTION_MAX_ROWS_PER_DEVICE (oldest first)."""
        try:
            async with self._sessionmaker()() as db:
                cutoff = self._utcnow() - timedelta(days=self.RETENTION_DAYS)
                # Age prune in bounded batches, yielding between each so the
                # SQLite write lock is released and other writers get a turn.
                for _ in range(20):  # hard bound: 100k rows per prune pass
                    stale_ids = (
                        select(BMCULinkEvent.id)
                        .where(BMCULinkEvent.server_received_at < cutoff)
                        .limit(self.RETENTION_DELETE_BATCH)
                    )
                    result = await db.execute(delete(BMCULinkEvent).where(BMCULinkEvent.id.in_(stale_ids)))
                    await db.commit()
                    await asyncio.sleep(0)
                    if (result.rowcount or 0) < self.RETENTION_DELETE_BATCH:
                        break

                counts = await db.execute(
                    select(BMCULinkEvent.device_id, func.count(BMCULinkEvent.id)).group_by(BMCULinkEvent.device_id)
                )
                for device_id, count in counts.all():
                    excess = count - self.RETENTION_MAX_ROWS_PER_DEVICE
                    if excess > 0:
                        oldest = (
                            select(BMCULinkEvent.id)
                            .where(BMCULinkEvent.device_id == device_id)
                            .order_by(BMCULinkEvent.server_received_at.asc(), BMCULinkEvent.id.asc())
                            .limit(min(excess, self.RETENTION_DELETE_BATCH))
                        )
                        await db.execute(delete(BMCULinkEvent).where(BMCULinkEvent.id.in_(oldest)))
                        await db.commit()
                        await asyncio.sleep(0)
        except Exception:
            logger.exception("BMCU Link retention prune failed")

    # ---------------------------------------------------------- notifications

    def _printing_printers(self) -> list[tuple[int, str]]:
        """(id, name) of printers currently in an active print state."""
        try:
            from backend.app.services.printer_lifecycle import print_process_active
            from backend.app.services.printer_manager import printer_manager

            printers = []
            for printer_id, state in printer_manager.get_all_statuses().items():
                if print_process_active(state):
                    info = printer_manager.get_printer(printer_id)
                    printers.append((printer_id, info.name if info else f"printer {printer_id}"))
            return printers
        except Exception:
            logger.exception("BMCU Link failed to inspect printer states")
            return []

    async def _notify_offline(self, device_id: str) -> None:
        """Offline-while-printing notification (silent when nothing prints)."""
        try:
            printing = self._printing_printers()
            if not printing:
                return
            from backend.app.services.notification_service import notification_service

            # Scope providers to the first printing printer so printer-scoped
            # providers for idle printers don't get paged.
            printer_id = printing[0][0]
            names = [name for _, name in printing]
            async with self._sessionmaker()() as db:
                await notification_service.on_bmcu_link_device_offline(
                    device_id, device_id, names, db, printer_id=printer_id
                )
        except Exception:
            logger.exception("BMCU Link offline notification failed for %s", device_id)

    async def _notify_anomaly(self, device_id: str, kind: str, data: dict) -> None:
        try:
            # Per-device latch: a misbehaving bridge streaming anomaly frames
            # must not flood notification providers (broadcasts stay immediate).
            now = self._clock()
            if now - self._last_anomaly_notify.get(device_id, float("-inf")) < self.ANOMALY_NOTIFY_COOLDOWN_S:
                return
            self._last_anomaly_notify[device_id] = now

            from backend.app.services.notification_service import notification_service

            # Scope to a printing printer when there is one; otherwise None
            # (tradeoff: with no print running, printer-scoped providers also
            # receive the anomaly — better than silently dropping it).
            printing = self._printing_printers()
            printer_id = printing[0][0] if printing else None
            summary = json.dumps(data)[:500]
            async with self._sessionmaker()() as db:
                await notification_service.on_bmcu_link_anomaly(device_id, kind, summary, db, printer_id=printer_id)
        except Exception:
            logger.exception("BMCU Link anomaly notification failed for %s", device_id)


bmcu_link_service = BMCULinkService()
