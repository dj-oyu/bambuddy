from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.database import Base
from backend.app.models.bmcu_binary import (
    BMCUBinaryDiagnostic,
    BMCUBinaryLog,
    BMCUBinaryLossRange,
    BMCUBinaryRecord,
)
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.retention import prune_once
from backend.app.services.bmcu_binary.storage_keys import u64_decimal


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[table for table in Base.metadata.sorted_tables if table.name.startswith("bmcu_binary_")],
        )
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _record(index, age_hours):
    return BMCUBinaryRecord(
        device_id="pico",
        pico_boot_id="0000000000000001",
        transport_sequence=u64_decimal(index),
        link_index=0,
        flags=0,
        message_type=MessageType.BMCU_FRAME,
        received_at_us=u64_decimal(index),
        server_received_at=_now() - timedelta(hours=age_hours),
        bmcu_kind=2,
        raw_payload=b"\x00",
        raw_bmcu_frame=None,
    )


async def _seed(factory):
    async with factory() as db:
        for index in range(1, 6):
            db.add(_record(index, 100))
        for index in range(6, 9):
            db.add(_record(index, 1))
        db.add(
            BMCUBinaryLossRange(
                device_id="pico",
                pico_boot_id="0000000000000001",
                report_sequence=u64_decimal(1),
                first_sequence=u64_decimal(1),
                last_sequence=u64_decimal(4),
                dropped_count=u64_decimal(4),
                reason=1,
                recorded_at=_now() - timedelta(hours=100),
            )
        )
        db.add(
            BMCUBinaryLog(
                device_id="pico",
                pico_boot_id="0000000000000001",
                transport_sequence=u64_decimal(2),
                log_sequence=u64_decimal(2),
                uptime_ms=u64_decimal(5),
                severity=1,
                component="boot",
                message="old",
                detail=b"",
                recorded_at=_now() - timedelta(hours=100),
            )
        )
        db.add(
            BMCUBinaryDiagnostic(
                device_id="pico",
                pico_boot_id="0000000000000001",
                transport_sequence=u64_decimal(3),
                payload=b"",
                recorded_at=_now() - timedelta(hours=100),
            )
        )
        await db.commit()


async def _count(factory, model):
    async with factory() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio
async def test_prune_removes_only_rows_past_the_window(factory, monkeypatch) -> None:
    monkeypatch.setenv("BAMBUDDY_BMCU_RETENTION_HOURS", "48")
    await _seed(factory)
    removed = await prune_once(factory)
    assert removed["bmcu_binary_records"] == 5
    assert await _count(factory, BMCUBinaryRecord) == 3
    assert await _count(factory, BMCUBinaryLossRange) == 0
    assert await _count(factory, BMCUBinaryLog) == 0
    assert await _count(factory, BMCUBinaryDiagnostic) == 0


@pytest.mark.asyncio
async def test_prune_is_disabled_by_zero_hours(factory, monkeypatch) -> None:
    monkeypatch.setenv("BAMBUDDY_BMCU_RETENTION_HOURS", "0")
    await _seed(factory)
    assert await prune_once(factory) == {}
    assert await _count(factory, BMCUBinaryRecord) == 8


@pytest.mark.asyncio
async def test_prune_walks_batches_smaller_than_the_backlog(factory, monkeypatch) -> None:
    monkeypatch.setenv("BAMBUDDY_BMCU_RETENTION_HOURS", "48")
    monkeypatch.setenv("BAMBUDDY_BMCU_RETENTION_BATCH", "100")
    async with factory() as db:
        for index in range(1, 251):
            db.add(_record(index, 100))
        await db.commit()
    removed = await prune_once(factory)
    assert removed["bmcu_binary_records"] == 250
    assert await _count(factory, BMCUBinaryRecord) == 0
