"""Transactional, idempotent BMB1 persistence and global durable ACK."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from backend.app.models.bmcu_binary import (
    BMCUBinaryBoot,
    BMCUBinaryControlResult,
    BMCUBinaryDevice,
    BMCUBinaryDiagnostic,
    BMCUBinaryLink,
    BMCUBinaryLog,
    BMCUBinaryLossRange,
    BMCUBinaryRecord,
)

from .bmcu_decoder import (
    BMCUEvent,
    BMCUHello,
    BMCUStatus,
    decode_semantic,
    decode_wire_frame,
    validate_alpha3_wire_frame,
)
from .constants import MessageType
from .errors import InvalidBMCUFrame, InvalidTransportDrop
from .framing import Frame
from .messages import ControlResult, Hello, PicoLog, TransportDrop, decode_bmcu_frame
from .storage_keys import advance_contiguous_with_losses, u64_decimal, u64_hex

# Realtime per-link values live only in BMCUStatus (kind 2). Historically any
# decoded frame overwrote current_state, so the EVENT flood (~37:1) erased the
# status snapshot within milliseconds. Set to "0" to restore that last-frame-wins
# behaviour if firmware ever carries realtime data in a non-STATUS kind.
_STRICT_STATE = os.getenv("BAMBUDDY_BMCU_STRICT_STATUS_STATE", "1") != "0"
_REHYDRATE_SCAN_LIMIT = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BinaryPersistence:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self.current_state = {}
        self.current_state_seen = {}
        self.last_event = {}
        self.last_event_seen = {}
        self.last_hello = {}
        # There is no BMCU boot id on the wire or in the schema; this monotonic
        # counter of observed BMCU HELLO frames is the honest stand-in.
        self.bmcu_hello_epoch = {}
        self._hello_lock = asyncio.Lock()

    async def register_hello(self, hello: Hello, boot_id: int) -> int:
        async with self._hello_lock:
            return await self._register_hello_unlocked(hello, boot_id)

    async def _register_hello_unlocked(self, hello: Hello, boot_id: int) -> int:
        now = _utcnow()
        boot_key = u64_hex(boot_id)
        current_range = next((item for item in hello.replay_ranges if item.pico_boot_id == boot_id), None)
        if current_range is None:
            raise ValueError("HELLO replay table omits current boot")
        async with self._session_factory() as db:
            row = (
                await db.execute(select(BMCUBinaryDevice).where(BMCUBinaryDevice.device_id == hello.device_id))
            ).scalar_one_or_none()
            if row is None:
                row = BMCUBinaryDevice(
                    device_id=hello.device_id,
                    firmware=hello.firmware,
                    pico_boot_id=boot_key,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_ack_sequence=u64_decimal(max(0, current_range.oldest_available_sequence - 1)),
                    oldest_available_sequence=u64_decimal(current_range.oldest_available_sequence),
                    newest_available_sequence=u64_decimal(current_range.newest_available_sequence),
                )
                db.add(row)
            else:
                if row.pico_boot_id != boot_key:
                    row.last_ack_sequence = u64_decimal(max(0, current_range.oldest_available_sequence - 1))
                row.firmware, row.pico_boot_id, row.last_seen_at = hello.firmware, boot_key, now
                row.oldest_available_sequence = u64_decimal(current_range.oldest_available_sequence)
                row.newest_available_sequence = u64_decimal(current_range.newest_available_sequence)
            current_boot = None
            existing_links = {
                item.link_index: item
                for item in (
                    await db.execute(select(BMCUBinaryLink).where(BMCUBinaryLink.device_id == hello.device_id))
                ).scalars()
            }
            for link in hello.links:
                stored = existing_links.get(link.link_index)
                if stored is None:
                    db.add(
                        BMCUBinaryLink(
                            device_id=hello.device_id,
                            link_index=link.link_index,
                            link_id=link.link_id,
                        )
                    )
                else:
                    stored.link_id = link.link_id
            for replay_range in hello.replay_ranges:
                replay_key = u64_hex(replay_range.pico_boot_id)
                replay_boot = (
                    await db.execute(
                        select(BMCUBinaryBoot).where(
                            BMCUBinaryBoot.device_id == hello.device_id,
                            BMCUBinaryBoot.pico_boot_id == replay_key,
                        )
                    )
                ).scalar_one_or_none()
                if replay_boot is None:
                    floor = max(0, replay_range.oldest_available_sequence - 1)
                    replay_boot = BMCUBinaryBoot(
                        device_id=hello.device_id,
                        pico_boot_id=replay_key,
                        last_ack_sequence=u64_decimal(floor),
                        oldest_available_sequence=u64_decimal(replay_range.oldest_available_sequence),
                        newest_available_sequence=u64_decimal(replay_range.newest_available_sequence),
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    db.add(replay_boot)
                else:
                    floor = max(0, replay_range.oldest_available_sequence - 1)
                    if int(replay_boot.last_ack_sequence) < floor:
                        replay_boot.last_ack_sequence = u64_decimal(floor)
                    replay_boot.oldest_available_sequence = u64_decimal(replay_range.oldest_available_sequence)
                    replay_boot.newest_available_sequence = u64_decimal(replay_range.newest_available_sequence)
                    replay_boot.last_seen_at = now
                if replay_range.pico_boot_id == boot_id:
                    current_boot = replay_boot
            await db.commit()
            assert current_boot is not None
            return int(current_boot.last_ack_sequence)

    async def persist(self, device_id: str, frame: Frame) -> int:
        """Commit one durable record and return the global contiguous watermark."""
        sequence = frame.header.transport_sequence
        if sequence <= 0:
            raise ValueError("durable record requires nonzero transport sequence")
        now = _utcnow()
        boot_key = u64_hex(frame.header.pico_boot_id)
        sequence_key = u64_decimal(sequence)
        semantic = None
        received_at_us = None
        wire = None
        bmcu = None
        if frame.header.message_type == MessageType.BMCU_FRAME:
            embedded = decode_bmcu_frame(frame.payload, validate_alpha3_wire_frame)
            received_at_us, wire = embedded.received_at_us, embedded.wire_bytes
            bmcu = decode_wire_frame(wire)
            semantic = decode_semantic(bmcu)
        log = PicoLog.decode(frame.payload) if frame.header.message_type == MessageType.PICO_LOG else None
        drop = TransportDrop.decode(frame.payload) if frame.header.message_type == MessageType.TRANSPORT_DROP else None
        if drop and drop.first_sequence != sequence:
            raise InvalidTransportDrop("TRANSPORT_DROP header sequence must equal first dropped sequence")

        async with self._session_factory() as db:
            device = (
                await db.execute(select(BMCUBinaryDevice).where(BMCUBinaryDevice.device_id == device_id))
            ).scalar_one()
            boot = (
                await db.execute(
                    select(BMCUBinaryBoot).where(
                        BMCUBinaryBoot.device_id == device_id,
                        BMCUBinaryBoot.pico_boot_id == boot_key,
                    )
                )
            ).scalar_one_or_none()
            if boot is None:
                boot = BMCUBinaryBoot(
                    device_id=device_id,
                    pico_boot_id=boot_key,
                    last_ack_sequence="00000000000000000000",
                    oldest_available_sequence="00000000000000000000",
                    newest_available_sequence=u64_decimal(sequence),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(boot)
                await db.flush()
            reported_newest = max(sequence, drop.last_sequence if drop else sequence)
            if device.pico_boot_id == boot_key:
                if reported_newest > int(boot.newest_available_sequence):
                    boot.newest_available_sequence = u64_decimal(reported_newest)
                if reported_newest > int(device.newest_available_sequence):
                    device.newest_available_sequence = u64_decimal(reported_newest)
            elif drop and drop.last_sequence > int(boot.newest_available_sequence):
                raise InvalidTransportDrop("TRANSPORT_DROP exceeds HELLO advertised range")
            existing = (
                await db.execute(
                    select(BMCUBinaryRecord.id).where(
                        BMCUBinaryRecord.device_id == device_id,
                        BMCUBinaryRecord.pico_boot_id == boot_key,
                        BMCUBinaryRecord.transport_sequence == sequence_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    BMCUBinaryRecord(
                        device_id=device_id,
                        pico_boot_id=boot_key,
                        transport_sequence=sequence_key,
                        link_index=frame.header.link_index,
                        flags=frame.header.flags,
                        message_type=frame.header.message_type,
                        received_at_us=u64_decimal(received_at_us) if received_at_us is not None else None,
                        server_received_at=now,
                        bmcu_version=bmcu.version if bmcu else None,
                        bmcu_kind=bmcu.kind if bmcu else None,
                        bmcu_sequence=bmcu.sequence if bmcu else None,
                        raw_payload=frame.payload,
                        raw_bmcu_frame=wire,
                    )
                )
                if frame.header.message_type == MessageType.PICO_DIAGNOSTIC:
                    db.add(
                        BMCUBinaryDiagnostic(
                            device_id=device_id,
                            pico_boot_id=boot_key,
                            transport_sequence=sequence_key,
                            recorded_at=now,
                            payload=frame.payload,
                        )
                    )
                if log:
                    db.add(
                        BMCUBinaryLog(
                            device_id=device_id,
                            pico_boot_id=boot_key,
                            transport_sequence=sequence_key,
                            log_sequence=u64_decimal(log.log_sequence),
                            uptime_ms=u64_decimal(log.uptime_ms),
                            severity=log.severity,
                            component=log.component,
                            message=log.message,
                            detail=log.detail,
                            recorded_at=now,
                        )
                    )
                if drop and drop.first_sequence:
                    db.add(
                        BMCUBinaryLossRange(
                            device_id=device_id,
                            pico_boot_id=boot_key,
                            report_sequence=sequence_key,
                            first_sequence=u64_decimal(drop.first_sequence),
                            last_sequence=u64_decimal(drop.last_sequence),
                            dropped_count=u64_decimal(drop.count),
                            reason=drop.reason,
                            recorded_at=now,
                        )
                    )
                await db.flush()

            watermark = int(boot.last_ack_sequence)
            sequences = (
                await db.execute(
                    select(BMCUBinaryRecord.transport_sequence)
                    .where(
                        BMCUBinaryRecord.device_id == device_id,
                        BMCUBinaryRecord.pico_boot_id == boot_key,
                        BMCUBinaryRecord.transport_sequence > u64_decimal(watermark),
                    )
                    .order_by(BMCUBinaryRecord.transport_sequence)
                )
            ).scalars()
            losses = (
                await db.execute(
                    select(
                        BMCUBinaryLossRange.first_sequence,
                        BMCUBinaryLossRange.last_sequence,
                    ).where(
                        BMCUBinaryLossRange.device_id == device_id,
                        BMCUBinaryLossRange.pico_boot_id == boot_key,
                        BMCUBinaryLossRange.last_sequence > u64_decimal(watermark),
                    )
                )
            ).all()
            watermark = advance_contiguous_with_losses(watermark, sequences, losses)
            boot.last_ack_sequence = u64_decimal(watermark)
            boot.last_seen_at = now
            if device.pico_boot_id == boot_key:
                device.last_ack_sequence = u64_decimal(watermark)
            device.last_seen_at = now
            await db.commit()

        if semantic is not None and received_at_us is not None:
            key = (device_id, frame.header.link_index)
            now_m = time.monotonic()
            if isinstance(semantic, BMCUStatus) or not _STRICT_STATE:
                self.current_state[key] = semantic
                self.current_state_seen[key] = now_m
            if isinstance(semantic, BMCUEvent):
                self.last_event[key] = semantic
                self.last_event_seen[key] = now_m
            elif isinstance(semantic, BMCUHello):
                self.last_hello[key] = semantic
                self.bmcu_hello_epoch[device_id] = self.bmcu_hello_epoch.get(device_id, 0) + 1
        return watermark

    async def persist_control_result(self, device_id: str, boot_id: int, result: ControlResult) -> None:
        boot_key, command_key, now = u64_hex(boot_id), u64_decimal(result.command_sequence), _utcnow()
        async with self._session_factory() as db:
            existing = (
                await db.execute(
                    select(BMCUBinaryControlResult.id).where(
                        BMCUBinaryControlResult.device_id == device_id,
                        BMCUBinaryControlResult.pico_boot_id == boot_key,
                        BMCUBinaryControlResult.command_sequence == command_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    BMCUBinaryControlResult(
                        device_id=device_id,
                        pico_boot_id=boot_key,
                        command_sequence=command_key,
                        result=result.result,
                        detail=result.detail,
                        recorded_at=now,
                    )
                )
                await db.commit()

    async def _newest_semantic(self, db, device_id: str, link_index: int, kind: int):
        """Newest decodable semantic of one BMCU kind for one link.

        Queried per link rather than with a single global scan: STATUS rates
        differ wildly between links, so a shared row budget lets a busy link
        starve a quiet one out of ever being restored.
        """
        rows = (
            await db.execute(
                select(BMCUBinaryRecord)
                .where(
                    BMCUBinaryRecord.message_type == MessageType.BMCU_FRAME,
                    BMCUBinaryRecord.raw_bmcu_frame.isnot(None),
                    BMCUBinaryRecord.bmcu_kind == kind,
                    BMCUBinaryRecord.device_id == device_id,
                    BMCUBinaryRecord.link_index == link_index,
                )
                .order_by(BMCUBinaryRecord.server_received_at.desc())
                .limit(_REHYDRATE_SCAN_LIMIT)
            )
        ).scalars()
        for row in rows:
            try:
                semantic = decode_semantic(decode_wire_frame(row.raw_bmcu_frame))
            except InvalidBMCUFrame:
                continue
            if semantic is not None:
                return semantic, row.server_received_at
        return None, None

    async def rehydrate_current_state(self) -> None:
        """Restore latest decoded per-link state before accepting Pico sessions."""
        async with self._session_factory() as db:
            links = (
                await db.execute(
                    select(BMCUBinaryRecord.device_id, BMCUBinaryRecord.link_index)
                    .where(
                        BMCUBinaryRecord.message_type == MessageType.BMCU_FRAME,
                        BMCUBinaryRecord.raw_bmcu_frame.isnot(None),
                    )
                    .group_by(BMCUBinaryRecord.device_id, BMCUBinaryRecord.link_index)
                )
            ).all()
            now_m, now = time.monotonic(), _utcnow()
            for device_id, link_index in links:
                key = (device_id, link_index)
                status, received_at = await self._newest_semantic(db, device_id, link_index, 2)
                if isinstance(status, BMCUStatus):
                    self.current_state[key] = status
                    # Age the restored snapshot by how old the record actually
                    # is, so a restart cannot present hours-old values as fresh.
                    age_s = 0.0
                    if received_at is not None:
                        # server_received_at is stored naive-UTC, like _utcnow().
                        stamped = received_at.replace(tzinfo=None) if received_at.tzinfo else received_at
                        age_s = max(0.0, (now - stamped).total_seconds())
                    self.current_state_seen[key] = now_m - age_s
                hello, _ = await self._newest_semantic(db, device_id, link_index, 1)
                if isinstance(hello, BMCUHello):
                    self.last_hello[key] = hello
            # No BMCU boot id exists on the wire, so the HELLO count stands in
            # for a boot session. Seed it from history to stay monotonic across
            # bambuddy restarts.
            epochs = (
                await db.execute(
                    select(BMCUBinaryRecord.device_id, func.count())
                    .where(
                        BMCUBinaryRecord.message_type == MessageType.BMCU_FRAME,
                        BMCUBinaryRecord.bmcu_kind == 1,
                    )
                    .group_by(BMCUBinaryRecord.device_id)
                )
            ).all()
            for device_id, count in epochs:
                self.bmcu_hello_epoch[device_id] = int(count or 0)
