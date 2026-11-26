"""Unacknowledged BMS2 UDP ingest. The callback never performs I/O."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from backend.app.models.bmcu_binary import (
    BMCUBinaryBoot,
    BMCUBinaryDevice,
    BMCUBinaryDiagnostic,
    BMCUBinaryLink,
    BMCUBinaryLog,
    BMCUBinaryLossRange,
    BMCUBinaryRecord,
    BMCUBinaryStreamCursor,
)

from .bmcu_decoder import (
    BMCUEvent,
    BMCUFullStatusRecord,
    BMCUStatus,
    decode_semantic,
    decode_wire_frame,
    validate_alpha3_wire_frame,
)
from .constants import DropReason, MessageType
from .errors import BinaryProtocolError, InvalidMessage
from .framing import Frame, decode_header
from .messages import LinkStateMessage, PicoLog, TransportDrop, decode_bmcu_frame, decode_tlvs
from .storage_keys import u64_decimal, u64_hex

logger = logging.getLogger(__name__)
HEARTBEAT = MessageType.HEARTBEAT
SEQUENCED = {
    int(MessageType.BMCU_FRAME),
    int(MessageType.LINK_STATE),
    int(MessageType.TRANSPORT_DROP),
    int(MessageType.PICO_DIAGNOSTIC),
    int(MessageType.PICO_LOG),
}
NETWORK_GAP, RECEIVER_OVERFLOW = DropReason.NETWORK_GAP, DropReason.RECEIVER_OVERFLOW


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Heartbeat:
    interval_ms: int
    uptime_ms: int
    next_sequence: int
    firmware: str
    links: tuple[tuple[int, str, int], ...]


def parse_heartbeat(data: bytes) -> Heartbeat:
    if len(data) < 22:
        raise InvalidMessage("truncated heartbeat")
    interval, uptime, next_sequence = struct.unpack_from(">IQQ", data)
    pos = 20

    def text(maximum):
        nonlocal pos
        if pos >= len(data):
            raise InvalidMessage("truncated heartbeat text")
        length = data[pos]
        pos += 1
        if not 1 <= length <= maximum or pos + length > len(data):
            raise InvalidMessage("invalid heartbeat text length")
        try:
            result = data[pos : pos + length].decode("ascii")
        except UnicodeDecodeError as exc:
            raise InvalidMessage("invalid heartbeat text") from exc
        pos += length
        return result

    firmware = text(31)
    if pos >= len(data):
        raise InvalidMessage("truncated heartbeat links")
    count = data[pos]
    pos += 1
    if count > 4 or not interval or not next_sequence:
        raise InvalidMessage("invalid heartbeat values")
    links = []
    for _ in range(count):
        if pos >= len(data):
            raise InvalidMessage("truncated heartbeat link")
        index = data[pos]
        pos += 1
        link_id = text(31)
        if pos >= len(data):
            raise InvalidMessage("truncated heartbeat link state")
        state = data[pos]
        pos += 1
        if index in [link[0] for link in links] or link_id in [link[1] for link in links]:
            raise InvalidMessage("duplicate heartbeat link")
        links.append((index, link_id, state))
    if pos != len(data):
        raise InvalidMessage("heartbeat trailing bytes")
    return Heartbeat(interval, uptime, next_sequence, firmware, tuple(links))


def parse_datagram(data: bytes) -> tuple[str, list[Frame]]:
    if len(data) > 1200 or len(data) < 10 or data[:4] != b"BMS2" or data[4] != 1:
        raise InvalidMessage("invalid BMS2 envelope")
    length = data[6]
    if not 1 <= length <= 63 or len(data) < 8 + length:
        raise InvalidMessage("invalid device id length")
    try:
        device_id = data[7 : 7 + length].decode("ascii")
    except UnicodeDecodeError as exc:
        raise InvalidMessage("invalid device id") from exc
    count, pos = data[7 + length], 8 + length
    if not count:
        raise InvalidMessage("empty BMS2 envelope")
    frames = []
    for _ in range(count):
        if pos + 32 > len(data):
            raise InvalidMessage("truncated BMB1 header")
        header = decode_header(data[pos : pos + 32])
        end = pos + 32 + header.payload_length
        if end > len(data):
            raise InvalidMessage("truncated BMB1 payload")
        frames.append(Frame(header, data[pos + 32 : end]))
        pos = end
    if pos != len(data):
        raise InvalidMessage("trailing BMS2 bytes")
    return device_id, frames


def validate_frame(frame: Frame):
    h = frame.header
    if h.message_type == HEARTBEAT:
        if h.transport_sequence or h.link_index != 255:
            raise InvalidMessage("invalid heartbeat header")
        return parse_heartbeat(frame.payload)
    if h.message_type not in SEQUENCED or not h.transport_sequence:
        raise InvalidMessage("invalid stream record type or sequence")
    if h.message_type in (MessageType.PICO_DIAGNOSTIC, MessageType.PICO_LOG) and h.link_index != 255:
        raise InvalidMessage("bridge record requires global scope")
    if h.message_type == MessageType.LINK_STATE:
        LinkStateMessage.decode(frame.payload)
    elif h.message_type == MessageType.PICO_DIAGNOSTIC:
        decode_tlvs(frame.payload)
    elif h.message_type == MessageType.PICO_LOG:
        PicoLog.decode(frame.payload)
    elif h.message_type == MessageType.TRANSPORT_DROP:
        drop = TransportDrop.decode(frame.payload)
        if drop.first_sequence != h.transport_sequence:
            raise InvalidMessage("invalid drop sequence")
    elif h.message_type == MessageType.BMCU_FRAME:
        embedded = decode_bmcu_frame(frame.payload, validate_alpha3_wire_frame)
        decode_semantic(decode_wire_frame(embedded.wire_bytes))
    return None


@dataclass
class StreamState:
    heartbeat_at: float = 0
    heartbeat_interval: float = 5
    source: str = ""
    boot_id: int = 0
    uptime_ms: int = 0
    links: dict = field(default_factory=dict)
    seen_sequences: dict = field(default_factory=dict)
    seen_boots: set = field(default_factory=set)

    def health(self):
        if not self.heartbeat_at:
            return "offline"
        age = time.monotonic() - self.heartbeat_at
        return "online" if age <= 3 * self.heartbeat_interval else "stale" if age <= 60 else "offline"


class StreamReceiver(asyncio.DatagramProtocol):
    def __init__(
        self,
        persistence,
        known_devices,
        *,
        accept_new=False,
        queue_size=8192,
        batch_size=512,
        flush_seconds=0.25,
        reorder_seconds=2,
    ):
        self.persistence = persistence
        self.known_devices = set(known_devices)
        self.accept_new = accept_new
        self.queue_size = queue_size
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self.reorder_seconds = reorder_seconds
        self.queues = {}
        self.workers = {}
        self.states = {}
        self.trackers = {}
        self.overflows = {}
        self.counters = Counter()
        self.device_counters = {}
        self.transport = None
        self.closing = False

    def connection_made(self, transport):
        self.transport = transport

    async def restore_pending(self):
        async with self.persistence._session_factory() as db:
            devices = list((await db.execute(select(BMCUBinaryStreamCursor.device_id))).scalars())
        for device in devices:
            if device not in self.known_devices or device in self.queues:
                continue
            queue = self.queues[device] = asyncio.Queue(self.queue_size)
            self.device_counters.setdefault(device, Counter())
            self.workers[device] = asyncio.create_task(self._worker(device, queue))

    def datagram_received(self, data, addr):
        self.counters["datagrams_received"] += 1
        try:
            device, frames = parse_datagram(data)
        except BinaryProtocolError:
            self.counters["envelope_rejects"] += 1
            return
        parsed = []
        rejected = 0
        for frame in frames:
            try:
                beat = validate_frame(frame)
            except (BinaryProtocolError, ValueError, struct.error):
                self.counters["record_rejects"] += 1
                rejected += 1
                continue
            parsed.append((frame, beat))
        if device not in self.known_devices:
            if not self.accept_new or not any(beat is not None for _, beat in parsed):
                self.counters["unknown_device"] += 1
                return
            self.known_devices.add(device)
        counters = self.device_counters.setdefault(device, Counter())
        counters["datagrams_received"] += 1
        counters["record_rejects"] += rejected
        if not parsed:
            return
        queue = self.queues.get(device)
        if queue is None:
            queue = self.queues[device] = asyncio.Queue(self.queue_size)
            self.workers[device] = asyncio.create_task(self._worker(device, queue))
        for frame, beat in parsed:
            if beat is not None:
                state = self.states.setdefault(device, StreamState())
                if frame.header.pico_boot_id != state.boot_id and frame.header.pico_boot_id in state.seen_boots:
                    continue
                if frame.header.pico_boot_id == state.boot_id and beat.uptime_ms < state.uptime_ms:
                    continue
                if frame.header.pico_boot_id != state.boot_id:
                    state.boot_id = frame.header.pico_boot_id
                    state.seen_boots.add(state.boot_id)
                    state.seen_sequences.clear()
                    clear_live_state(self.persistence, device)
                state.heartbeat_at = time.monotonic()
                state.heartbeat_interval = beat.interval_ms / 1000
                state.source = str(addr[0])
                state.uptime_ms = beat.uptime_ms
                state.links = {index: (link_id, link_state) for index, link_id, link_state in beat.links}
            try:
                queue.put_nowait((frame, beat, str(addr[0]), time.monotonic()))
            except asyncio.QueueFull:
                self.counters["ingest_overflow"] += 1
                counters["ingest_overflow"] += 1
                if beat is None:
                    key = (device, frame.header.pico_boot_id)
                    seq = frame.header.transport_sequence
                    ranges = self.overflows.setdefault(key, [])
                    if ranges and ranges[-1][1] + 1 == seq:
                        ranges[-1] = (ranges[-1][0], seq)
                    elif len(ranges) < self.queue_size:
                        ranges.append((seq, seq))

    async def _worker(self, device, queue):
        tracker = GapTracker(self.reorder_seconds)
        async with self.persistence._session_factory() as db:
            cursor = await db.get(BMCUBinaryStreamCursor, device)
            if cursor:
                saved = json.loads(cursor.state)
                tracker.high = {int(k): int(v) for k, v in saved["high"].items()}
                tracker.pending = {int(k): [tuple(item) for item in v] for k, v in saved["pending"].items()}
        self.trackers[device] = tracker
        initialized = set(tracker.high)
        while not self.closing or not queue.empty():
            try:
                item = await asyncio.wait_for(queue.get(), self.flush_seconds)
            except TimeoutError:
                if tracker.due(time.time()):
                    candidate = copy.deepcopy(tracker)
                    try:
                        await reconcile_losses(self.persistence, device, candidate, [], self.device_counters[device])
                        tracker = self.trackers[device] = candidate
                    except Exception:
                        logger.exception("BMCU UDP gap reconciliation failed for %s", device)
                continue
            batch = [item]
            deadline = asyncio.get_running_loop().time() + self.flush_seconds
            while len(batch) < self.batch_size:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(queue.get(), timeout))
                except TimeoutError:
                    break
            while True:
                try:
                    candidate = copy.deepcopy(tracker)
                    newly_initialized = set()
                    for frame, _, _, _ in batch:
                        boot = frame.header.pico_boot_id
                        if boot not in initialized and boot not in newly_initialized:
                            async with self.persistence._session_factory() as db:
                                maximum = (
                                    await db.execute(
                                        select(func.max(BMCUBinaryRecord.transport_sequence)).where(
                                            BMCUBinaryRecord.device_id == device,
                                            BMCUBinaryRecord.pico_boot_id == u64_hex(boot),
                                        )
                                    )
                                ).scalar()
                            candidate.high[boot] = int(maximum or 0)
                            newly_initialized.add(boot)
                    overflow = [
                        (key[1], first, last)
                        for key, ranges in self.overflows.items()
                        if key[0] == device
                        for first, last in ranges
                    ]
                    overflow_sizes = {key: len(ranges) for key, ranges in self.overflows.items() if key[0] == device}
                    started = time.monotonic()
                    await persist_batch(
                        self.persistence,
                        device,
                        batch,
                        self.states,
                        self.device_counters[device],
                        tracker=candidate,
                        overflow=overflow,
                    )
                    tracker = self.trackers[device] = candidate
                    initialized.update(newly_initialized)
                    for key, count in overflow_sizes.items():
                        ranges = self.overflows.get(key, [])
                        # A callback can extend the final interval while the batch is committing.
                        if ranges and count and len(ranges) >= count:
                            saved_last = next((end for boot, _, end in reversed(overflow) if boot == key[1]), None)
                            if saved_last is not None and ranges[count - 1][1] > saved_last:
                                first, last = ranges[count - 1]
                                ranges[count - 1] = (saved_last + 1, last)
                                count -= 1
                        del ranges[:count]
                        if not ranges:
                            self.overflows.pop(key, None)
                    self.device_counters[device]["last_batch_size"] = len(batch)
                    self.device_counters[device]["last_flush_ms"] = round((time.monotonic() - started) * 1000)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.counters["batch_failures"] += 1
                    self.device_counters[device]["batch_failures"] += 1
                    logger.exception("BMCU UDP batch failed for %s; retrying", device)
                    await asyncio.sleep(1)
            for _ in batch:
                queue.task_done()
        if tracker.due(time.time() + self.reorder_seconds):
            await reconcile_losses(
                self.persistence,
                device,
                tracker,
                [],
                self.device_counters[device],
                now=time.time() + self.reorder_seconds,
            )

    async def close(self):
        self.closing = True
        if self.transport:
            self.transport.close()
        await asyncio.gather(*self.workers.values())


def subtract_interval(intervals, first, last):
    result = []
    for start, end, *extra in intervals:
        if end < first or start > last:
            result.append((start, end, *extra))
        else:
            if start < first:
                result.append((start, first - 1, *extra))
            if end > last:
                result.append((last + 1, end, *extra))
    return result


def clear_live_state(persistence, device_id):
    for name in (
        "current_state",
        "current_state_seen",
        "full_status_global",
        "full_status_counters",
        "last_event",
        "last_event_seen",
        "last_hello",
    ):
        values = getattr(persistence, name)
        for key in list(values):
            if key[0] == device_id:
                values.pop(key, None)


async def persist_batch(persistence, device_id, batch, states, counters, *, tracker=None, overflow=()):
    """Store a device batch atomically. The shared live view changes only after commit."""
    now = utcnow()
    beat_items = [(frame, beat, source, received_m) for frame, beat, source, received_m in batch if beat is not None]
    frames = [frame for frame, beat, _, _ in batch if beat is None]
    # A datagram can arrive out of order; record identity, not arrival order,
    # controls deduplication and the semantic update order.
    arrival_times = {
        (f.header.pico_boot_id, f.header.transport_sequence): received_m
        for f, beat, _, received_m in batch
        if beat is None
    }
    unique = {(f.header.pico_boot_id, f.header.transport_sequence): f for f in frames}
    frames = sorted(unique.values(), key=lambda f: (f.header.pico_boot_id, f.header.transport_sequence))
    decoded = []
    async with persistence._session_factory() as db:
        device = (
            await db.execute(select(BMCUBinaryDevice).where(BMCUBinaryDevice.device_id == device_id))
        ).scalar_one_or_none()
        if device is None:
            if not beat_items:
                raise InvalidMessage("first stream record must include heartbeat")
            frame, beat, _, _ = beat_items[0]
            zero = u64_decimal(0)
            device = BMCUBinaryDevice(
                device_id=device_id,
                firmware=beat.firmware,
                pico_boot_id=u64_hex(frame.header.pico_boot_id),
                first_seen_at=now,
                last_seen_at=now,
                last_ack_sequence=zero,
                oldest_available_sequence=zero,
                newest_available_sequence=zero,
            )
            db.add(device)
            await db.flush()
        boots = {
            b.pico_boot_id: b
            for b in (await db.execute(select(BMCUBinaryBoot).where(BMCUBinaryBoot.device_id == device_id))).scalars()
        }
        links = {
            link.link_index: link
            for link in (
                await db.execute(select(BMCUBinaryLink).where(BMCUBinaryLink.device_id == device_id))
            ).scalars()
        }
        previous_state = states.get(device_id)
        if not beat_items and frames:
            newest_boot = frames[-1].header.pico_boot_id
            if (
                previous_state is None
                or newest_boot == previous_state.boot_id
                or newest_boot not in previous_state.seen_boots
            ):
                device.pico_boot_id = u64_hex(newest_boot)
        for frame, beat, _, received_m in beat_items:
            boot_key = u64_hex(frame.header.pico_boot_id)
            received_at = now - timedelta(seconds=max(0, time.monotonic() - received_m))
            device.firmware, device.pico_boot_id, device.last_seen_at = beat.firmware, boot_key, received_at
            # A v2 stream has no ACK. Do not present v1's previous watermark as a v2 ACK.
            device.last_ack_sequence = u64_decimal(0)
            device.oldest_available_sequence = u64_decimal(0)
            device.newest_available_sequence = u64_decimal(max(0, beat.next_sequence - 1))
            announced = {index: link_id for index, link_id, _ in beat.links}
            for index in list(links):
                if index not in announced:
                    await db.delete(links.pop(index))
                elif links[index].link_id != announced[index]:
                    # Avoid unique-link-ID conflicts when two links swap names.
                    links[index].link_id = f"\x01{index}"
            await db.flush()
            for index, link_id, _state in beat.links:
                if index in links:
                    links[index].link_id = link_id
                else:
                    links[index] = BMCUBinaryLink(device_id=device_id, link_index=index, link_id=link_id)
                    db.add(links[index])
            if boot_key not in boots:
                boots[boot_key] = BMCUBinaryBoot(
                    device_id=device_id,
                    pico_boot_id=boot_key,
                    last_ack_sequence=u64_decimal(0),
                    oldest_available_sequence=u64_decimal(0),
                    newest_available_sequence=u64_decimal(0),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(boots[boot_key])
        for boot_id in {frame.header.pico_boot_id for frame in frames}:
            boot_key = u64_hex(boot_id)
            if boot_key not in boots:
                boots[boot_key] = BMCUBinaryBoot(
                    device_id=device_id,
                    pico_boot_id=boot_key,
                    last_ack_sequence=u64_decimal(0),
                    oldest_available_sequence=u64_decimal(0),
                    newest_available_sequence=u64_decimal(0),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(boots[boot_key])
        # One indexed identity query per boot, rather than one query per record.
        existing = set()
        for boot_id in {f.header.pico_boot_id for f in frames}:
            sequences = [u64_decimal(f.header.transport_sequence) for f in frames if f.header.pico_boot_id == boot_id]
            rows = (
                await db.execute(
                    select(BMCUBinaryRecord.transport_sequence).where(
                        BMCUBinaryRecord.device_id == device_id,
                        BMCUBinaryRecord.pico_boot_id == u64_hex(boot_id),
                        BMCUBinaryRecord.transport_sequence.in_(sequences),
                    )
                )
            ).scalars()
            existing.update((boot_id, int(seq)) for seq in rows)
        for boot_id in {frame.header.pico_boot_id for frame in frames}:
            relevant = [frame for frame in frames if frame.header.pico_boot_id == boot_id]
            first = min(frame.header.transport_sequence for frame in relevant)
            last = max(
                TransportDrop.decode(frame.payload).last_sequence
                if frame.header.message_type == MessageType.TRANSPORT_DROP
                else frame.header.transport_sequence
                for frame in relevant
            )
            rows = list(
                (
                    await db.execute(
                        select(BMCUBinaryLossRange).where(
                            BMCUBinaryLossRange.device_id == device_id,
                            BMCUBinaryLossRange.pico_boot_id == u64_hex(boot_id),
                            BMCUBinaryLossRange.reason.in_((NETWORK_GAP, RECEIVER_OVERFLOW)),
                            BMCUBinaryLossRange.first_sequence <= u64_decimal(last),
                            BMCUBinaryLossRange.last_sequence >= u64_decimal(first),
                        )
                    )
                ).scalars()
            )
            for row in rows:
                spans = [(int(row.first_sequence), int(row.last_sequence))]
                for frame in relevant:
                    left = frame.header.transport_sequence
                    right = (
                        TransportDrop.decode(frame.payload).last_sequence
                        if frame.header.message_type == MessageType.TRANSPORT_DROP
                        else left
                    )
                    spans = subtract_interval(spans, left, right)
                if spans == [(int(row.first_sequence), int(row.last_sequence))]:
                    continue
                reason = row.reason
                await db.delete(row)
                await db.flush()
                for left, right in spans:
                    db.add(
                        BMCUBinaryLossRange(
                            device_id=device_id,
                            pico_boot_id=u64_hex(boot_id),
                            report_sequence=u64_decimal(left),
                            first_sequence=u64_decimal(left),
                            last_sequence=u64_decimal(right),
                            dropped_count=u64_decimal(right - left + 1),
                            reason=reason,
                            recorded_at=now,
                        )
                    )
                await db.flush()
        for frame in frames:
            h = frame.header
            key = (h.pico_boot_id, h.transport_sequence)
            if key in existing:
                continue
            boot_key, sequence = u64_hex(h.pico_boot_id), u64_decimal(h.transport_sequence)
            recorded_at = now - timedelta(seconds=max(0, time.monotonic() - arrival_times[key]))
            semantic = bmcu = wire = received = None
            if h.message_type == MessageType.BMCU_FRAME:
                embedded = decode_bmcu_frame(frame.payload, validate_alpha3_wire_frame)
                received, wire = embedded.received_at_us, embedded.wire_bytes
                bmcu = decode_wire_frame(wire)
                semantic = decode_semantic(bmcu)
            db.add(
                BMCUBinaryRecord(
                    device_id=device_id,
                    pico_boot_id=boot_key,
                    transport_sequence=sequence,
                    link_index=h.link_index,
                    flags=h.flags,
                    message_type=h.message_type,
                    received_at_us=u64_decimal(received) if received is not None else None,
                    server_received_at=recorded_at,
                    bmcu_version=bmcu.version if bmcu else None,
                    bmcu_kind=bmcu.kind if bmcu else None,
                    bmcu_sequence=bmcu.sequence if bmcu else None,
                    raw_payload=frame.payload,
                    raw_bmcu_frame=wire,
                )
            )
            if h.message_type == MessageType.PICO_DIAGNOSTIC:
                db.add(
                    BMCUBinaryDiagnostic(
                        device_id=device_id,
                        pico_boot_id=boot_key,
                        transport_sequence=sequence,
                        recorded_at=recorded_at,
                        payload=frame.payload,
                    )
                )
            elif h.message_type == MessageType.PICO_LOG:
                log = PicoLog.decode(frame.payload)
                db.add(
                    BMCUBinaryLog(
                        device_id=device_id,
                        pico_boot_id=boot_key,
                        transport_sequence=sequence,
                        log_sequence=u64_decimal(log.log_sequence),
                        uptime_ms=u64_decimal(log.uptime_ms),
                        severity=log.severity,
                        component=log.component,
                        message=log.message,
                        detail=log.detail,
                        recorded_at=recorded_at,
                    )
                )
            elif h.message_type == MessageType.TRANSPORT_DROP:
                drop = TransportDrop.decode(frame.payload)
                await persistence._store_loss_range(db, device_id, boot_key, sequence, drop, now)
            boot = boots[boot_key]
            boot.newest_available_sequence = u64_decimal(max(int(boot.newest_available_sequence), h.transport_sequence))
            boot.last_seen_at = now
            decoded.append((frame, semantic, received))
        if tracker is not None:
            now_s = time.time()
            observe_batch(tracker, batch, now_s)
            for boot, first, last in overflow:
                tracker.observe(boot, first, last, now_s, RECEIVER_OVERFLOW)
            await store_due(db, device_id, tracker, counters, now_s)
            await save_cursor(db, device_id, tracker)
        await db.commit()
    for frame, beat, source, received_m in beat_items:
        state = states.setdefault(device_id, StreamState())
        if state.boot_id != frame.header.pico_boot_id:
            if frame.header.pico_boot_id in state.seen_boots:
                continue
            state.boot_id = frame.header.pico_boot_id
            state.seen_boots.add(state.boot_id)
            state.seen_sequences.clear()
            clear_live_state(persistence, device_id)
        if not state.heartbeat_at:
            state.heartbeat_at = received_m
            state.heartbeat_interval = beat.interval_ms / 1000
            state.source, state.uptime_ms = source, beat.uptime_ms
            state.links = {index: (link_id, link_state) for index, link_id, link_state in beat.links}
    state = states.setdefault(device_id, StreamState())
    new_boot = next(
        (
            frame.header.pico_boot_id
            for frame, _, _, _ in batch
            if frame.header.pico_boot_id != state.boot_id and frame.header.pico_boot_id not in state.seen_boots
        ),
        None,
    )
    if new_boot is not None:
        state.boot_id = new_boot
        state.seen_boots.add(new_boot)
        state.seen_sequences.clear()
        clear_live_state(persistence, device_id)
    for frame, semantic, received in decoded:
        # Delayed datagrams must not roll the live view back.
        category = "status" if isinstance(semantic, (BMCUStatus, BMCUFullStatusRecord)) else type(semantic)
        key = (frame.header.link_index, category)
        if frame.header.pico_boot_id == state.boot_id and frame.header.transport_sequence > state.seen_sequences.get(
            key, 0
        ):
            state.seen_sequences[key] = frame.header.transport_sequence
            state_key = (device_id, frame.header.link_index)
            prior_seen = persistence.current_state_seen.get(state_key)
            persistence.apply_semantic(device_id, frame, semantic, received)
            arrived_m = arrival_times[(frame.header.pico_boot_id, frame.header.transport_sequence)]
            if state_key in persistence.current_state_seen and persistence.current_state_seen[state_key] != prior_seen:
                persistence.current_state_seen[state_key] = min(persistence.current_state_seen[state_key], arrived_m)
            if state_key in persistence.last_event_seen and isinstance(semantic, BMCUEvent):
                persistence.last_event_seen[state_key] = min(persistence.last_event_seen[state_key], arrived_m)
    counters["records_persisted"] += len(decoded)
    counters["batches"] += 1


class GapTracker:
    """Track missing sequence intervals without allocating one object per sequence."""

    def __init__(self, delay=2):
        self.delay = delay
        self.high = {}
        self.pending = {}  # boot -> (first, last, deadline, reason)

    def observe(self, boot, first, last, now, reason=None):
        high = self.high.get(boot, 0)
        intervals = self.pending.setdefault(boot, [])
        if first > high + 1:
            intervals.append((high + 1, first - 1, now + self.delay, NETWORK_GAP))
        if reason is None:
            intervals = subtract_interval(intervals, first, last)
        else:
            intervals = subtract_interval(intervals, first, last)
            intervals.append((first, last, now + self.delay, reason))
        self.pending[boot] = intervals
        self.high[boot] = max(high, last)

    def due(self, now):
        result = []
        for boot, intervals in self.pending.items():
            for first, last, deadline, reason in intervals:
                if deadline <= now:
                    result.append((boot, first, last, reason))
        return result

    def committed(self, due):
        for boot, first, last, reason in due:
            self.pending[boot] = [
                item for item in self.pending[boot] if (item[0], item[1], item[3]) != (first, last, reason)
            ]


def observe_batch(tracker, batch, now_s):
    for frame, beat, _, _ in batch:
        h = frame.header
        if beat:
            if beat.next_sequence > 1 and beat.next_sequence - 1 > tracker.high.get(h.pico_boot_id, 0):
                tracker.observe(h.pico_boot_id, beat.next_sequence, beat.next_sequence, now_s)
                tracker.high[h.pico_boot_id] = beat.next_sequence - 1
            continue
        first = h.transport_sequence
        last = (
            TransportDrop.decode(frame.payload).last_sequence if h.message_type == MessageType.TRANSPORT_DROP else first
        )
        tracker.observe(h.pico_boot_id, first, last, now_s)


async def save_cursor(db, device_id, tracker):
    data = json.dumps({"high": tracker.high, "pending": tracker.pending}, separators=(",", ":"))
    row = await db.get(BMCUBinaryStreamCursor, device_id)
    if row is None:
        db.add(BMCUBinaryStreamCursor(device_id=device_id, state=data))
    else:
        row.state = data


async def store_due(db, device_id, tracker, counters, now_s):
    due = tracker.due(now_s)
    if not due:
        return
    await db.flush()
    added = 0
    for boot_id, first, last, reason in due:
        boot_key = u64_hex(boot_id)
        spans = [(first, last)]
        stored = (
            await db.execute(
                select(BMCUBinaryRecord.transport_sequence).where(
                    BMCUBinaryRecord.device_id == device_id,
                    BMCUBinaryRecord.pico_boot_id == boot_key,
                    BMCUBinaryRecord.transport_sequence >= u64_decimal(first),
                    BMCUBinaryRecord.transport_sequence <= u64_decimal(last),
                )
            )
        ).scalars()
        for sequence in stored:
            spans = subtract_interval(spans, int(sequence), int(sequence))
        losses = (
            await db.execute(
                select(BMCUBinaryLossRange).where(
                    BMCUBinaryLossRange.device_id == device_id,
                    BMCUBinaryLossRange.pico_boot_id == boot_key,
                    BMCUBinaryLossRange.first_sequence <= u64_decimal(last),
                    BMCUBinaryLossRange.last_sequence >= u64_decimal(first),
                )
            )
        ).scalars()
        for loss in losses:
            spans = subtract_interval(spans, int(loss.first_sequence), int(loss.last_sequence))
        for left, right, *_ in spans:
            db.add(
                BMCUBinaryLossRange(
                    device_id=device_id,
                    pico_boot_id=boot_key,
                    report_sequence=u64_decimal(left),
                    first_sequence=u64_decimal(left),
                    last_sequence=u64_decimal(right),
                    dropped_count=u64_decimal(right - left + 1),
                    reason=reason,
                    recorded_at=utcnow(),
                )
            )
            added += right - left + 1
    tracker.committed(due)
    counters["gaps_detected"] += added


async def reconcile_losses(persistence, device_id, tracker, batch, counters, *, now=None):
    """Persist expired candidates and their cursor in one transaction."""
    now_s = time.time() if now is None else now
    observe_batch(tracker, batch, now_s)
    async with persistence._session_factory() as db:
        await store_due(db, device_id, tracker, counters, now_s)
        await save_cursor(db, device_id, tracker)
        await db.commit()
