"""Age out BMB1 telemetry.

The bridge writes on the order of 50k rows an hour (drop reports alone were
~20k/h before merging), and nothing ever deleted them: the BMCU tables reached
440 MB of a 451 MB database while the actual print data was ~11 MB. These rows
are diagnostics with a short useful life — the UI reads a 1-hour timeline and
the ACK watermark only looks above the last acknowledged sequence — so keeping
them past a couple of days buys nothing.

Deletes run in bounded batches: the transport server shares this event loop and
the SQLite writer lock, so one large DELETE would stall ingest.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from backend.app.core import database as _database
from backend.app.models.bmcu_binary import (
    BMCUBinaryDiagnostic,
    BMCUBinaryLog,
    BMCUBinaryLossRange,
    BMCUBinaryRecord,
)

logger = logging.getLogger(__name__)

_TABLES = (
    (BMCUBinaryRecord, BMCUBinaryRecord.server_received_at),
    (BMCUBinaryLossRange, BMCUBinaryLossRange.recorded_at),
    (BMCUBinaryLog, BMCUBinaryLog.recorded_at),
    (BMCUBinaryDiagnostic, BMCUBinaryDiagnostic.recorded_at),
)


def _retention_hours() -> float:
    """Hours of telemetry to keep; 0 (or invalid) disables pruning entirely."""
    try:
        return max(0.0, float(os.getenv("BAMBUDDY_BMCU_RETENTION_HOURS", "48")))
    except ValueError:
        return 0.0


def _interval_s() -> float:
    try:
        return max(60.0, float(os.getenv("BAMBUDDY_BMCU_RETENTION_INTERVAL_S", "3600")))
    except ValueError:
        return 3600.0


def _batch_size() -> int:
    try:
        return max(100, int(os.getenv("BAMBUDDY_BMCU_RETENTION_BATCH", "2000")))
    except ValueError:
        return 2000


async def prune_once(session_factory=None) -> dict[str, int]:
    """Delete rows older than the retention window. Returns per-table counts."""
    hours = _retention_hours()
    if not hours:
        return {}
    factory = session_factory or _database.async_session
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
    batch, removed = _batch_size(), {}
    for model, column in _TABLES:
        total = 0
        while True:
            async with factory() as db:
                ids = (
                    (await db.execute(select(model.id).where(column < cutoff).order_by(model.id).limit(batch)))
                    .scalars()
                    .all()
                )
                if not ids:
                    break
                await db.execute(delete(model).where(model.id.in_(ids)))
                await db.commit()
            total += len(ids)
            if len(ids) < batch:
                break
            # Yield between batches so ingest keeps the writer lock moving.
            await asyncio.sleep(0)
        if total:
            removed[model.__tablename__] = total
    return removed


class BMCURetentionService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start_scheduler(self) -> None:
        if self._task is not None or not _retention_hours():
            return
        logger.info("Starting BMCU telemetry retention sweeper (%.1fh)", _retention_hours())
        self._task = asyncio.create_task(self._loop())

    def stop_scheduler(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("Stopped BMCU telemetry retention sweeper")

    async def _loop(self) -> None:
        while True:
            try:
                removed = await prune_once()
                if removed:
                    logger.info("BMCU retention pruned %s", removed)
                await asyncio.sleep(_interval_s())
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("BMCU retention sweep failed: %s", exc)
                await asyncio.sleep(60)


bmcu_retention_service = BMCURetentionService()
