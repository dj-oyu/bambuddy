"""Transactional, idempotent BMB1 persistence and global durable ACK."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.models.bmcu_binary import (
    BMCUBinaryBoot, BMCUBinaryDevice, BMCUBinaryDiagnostic, BMCUBinaryLog,
    BMCUBinaryRecord,
)

from .bmcu_decoder import decode_semantic, decode_wire_frame, validate_alpha3_wire_frame
from .constants import MessageType
from .framing import Frame
from .messages import Hello, PicoLog, decode_bmcu_frame
from .storage_keys import advance_contiguous, u64_decimal, u64_hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BinaryPersistence:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self.current_state = {}

    async def register_hello(self, hello: Hello, boot_id: int) -> int:
        now = _utcnow()
        boot_key = u64_hex(boot_id)
        current_range = next((item for item in hello.replay_ranges if item.pico_boot_id == boot_id), None)
        if current_range is None:
            raise ValueError("HELLO replay table omits current boot")
        async with self._session_factory() as db:
            row = (await db.execute(
                select(BMCUBinaryDevice).where(BMCUBinaryDevice.device_id == hello.device_id)
            )).scalar_one_or_none()
            if row is None:
                row = BMCUBinaryDevice(
                    device_id=hello.device_id, firmware=hello.firmware,
                    pico_boot_id=boot_key, first_seen_at=now, last_seen_at=now,
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
            for replay_range in hello.replay_ranges:
                replay_key = u64_hex(replay_range.pico_boot_id)
                replay_boot = (await db.execute(select(BMCUBinaryBoot).where(
                    BMCUBinaryBoot.device_id == hello.device_id,
                    BMCUBinaryBoot.pico_boot_id == replay_key,
                ))).scalar_one_or_none()
                if replay_boot is None:
                    floor = max(0, replay_range.oldest_available_sequence - 1)
                    replay_boot = BMCUBinaryBoot(
                        device_id=hello.device_id, pico_boot_id=replay_key,
                        last_ack_sequence=u64_decimal(floor),
                        first_seen_at=now, last_seen_at=now,
                    )
                    db.add(replay_boot)
                else:
                    floor = max(0, replay_range.oldest_available_sequence - 1)
                    if int(replay_boot.last_ack_sequence) < floor:
                        replay_boot.last_ack_sequence = u64_decimal(floor)
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

        async with self._session_factory() as db:
            device = (await db.execute(
                select(BMCUBinaryDevice).where(BMCUBinaryDevice.device_id == device_id)
            )).scalar_one()
            boot = (await db.execute(select(BMCUBinaryBoot).where(
                BMCUBinaryBoot.device_id == device_id,
                BMCUBinaryBoot.pico_boot_id == boot_key,
            ))).scalar_one_or_none()
            if boot is None:
                boot = BMCUBinaryBoot(
                    device_id=device_id, pico_boot_id=boot_key,
                    last_ack_sequence="00000000000000000000",
                    first_seen_at=now, last_seen_at=now,
                )
                db.add(boot)
                await db.flush()
            existing = (await db.execute(
                select(BMCUBinaryRecord.id).where(
                    BMCUBinaryRecord.device_id == device_id,
                    BMCUBinaryRecord.pico_boot_id == boot_key,
                    BMCUBinaryRecord.transport_sequence == sequence_key,
                )
            )).scalar_one_or_none()
            if existing is None:
                db.add(BMCUBinaryRecord(
                    device_id=device_id, pico_boot_id=boot_key,
                    transport_sequence=sequence_key, link_index=frame.header.link_index,
                    flags=frame.header.flags, message_type=frame.header.message_type,
                    received_at_us=u64_decimal(received_at_us) if received_at_us is not None else None,
                    server_received_at=now,
                    bmcu_version=bmcu.version if bmcu else None,
                    bmcu_kind=bmcu.kind if bmcu else None,
                    bmcu_sequence=bmcu.sequence if bmcu else None,
                    raw_payload=frame.payload, raw_bmcu_frame=wire,
                ))
                if frame.header.message_type == MessageType.PICO_DIAGNOSTIC:
                    db.add(BMCUBinaryDiagnostic(
                        device_id=device_id, pico_boot_id=boot_key,
                        transport_sequence=sequence_key, recorded_at=now, payload=frame.payload,
                    ))
                if log:
                    db.add(BMCUBinaryLog(
                        device_id=device_id, pico_boot_id=boot_key,
                        transport_sequence=sequence_key, log_sequence=u64_decimal(log.log_sequence),
                        uptime_ms=u64_decimal(log.uptime_ms), severity=log.severity,
                        component=log.component, message=log.message, detail=log.detail,
                        recorded_at=now,
                    ))
                await db.flush()

            watermark = int(boot.last_ack_sequence)
            sequences = (await db.execute(
                select(BMCUBinaryRecord.transport_sequence).where(
                    BMCUBinaryRecord.device_id == device_id,
                    BMCUBinaryRecord.pico_boot_id == boot_key,
                    BMCUBinaryRecord.transport_sequence > u64_decimal(watermark),
                ).order_by(BMCUBinaryRecord.transport_sequence)
            )).scalars()
            watermark = advance_contiguous(watermark, sequences)
            boot.last_ack_sequence = u64_decimal(watermark)
            boot.last_seen_at = now
            if device.pico_boot_id == boot_key:
                device.last_ack_sequence = u64_decimal(watermark)
            device.last_seen_at = now
            await db.commit()

        if semantic is not None and received_at_us is not None:
            self.current_state[(device_id, frame.header.link_index)] = semantic
        return watermark
