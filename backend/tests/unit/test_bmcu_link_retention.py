"""BMCU Link retention pruning tests (age + per-device row bounds)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.bmcu_link_event import BMCULinkEvent
from backend.app.services.bmcu_link import BMCULinkService


def make_row(device_id, seq, age_days=0.0):
    return BMCULinkEvent(
        device_id=device_id,
        pico_boot_session="picoA",
        bmcu_boot_session=1,
        uart_sequence=seq,
        kind="status",
        received_at_us=seq,
        server_received_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days),
        data="{}",
    )


@pytest.fixture
def service(test_engine):
    svc = BMCULinkService()
    svc._session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    return svc


@pytest.mark.asyncio
async def test_prune_by_age(service, db_session):
    db_session.add_all(
        [
            make_row("dev1", 1, age_days=20),  # older than 14d -> pruned
            make_row("dev1", 2, age_days=15),  # pruned
            make_row("dev1", 3, age_days=1),  # kept
        ]
    )
    await db_session.commit()

    await service._prune_retention()

    async with service._sessionmaker()() as db:
        seqs = (await db.execute(select(BMCULinkEvent.uart_sequence))).scalars().all()
    assert sorted(seqs) == [3]


@pytest.mark.asyncio
async def test_prune_by_row_cap_per_device(service, db_session):
    service.RETENTION_MAX_ROWS_PER_DEVICE = 5
    # dev1: 8 recent rows with increasing age -> 3 oldest pruned; dev2 untouched
    db_session.add_all([make_row("dev1", seq, age_days=seq * 0.01) for seq in range(1, 9)])
    db_session.add_all([make_row("dev2", seq) for seq in range(1, 4)])
    await db_session.commit()

    await service._prune_retention()

    async with service._sessionmaker()() as db:
        dev1_seqs = (
            (await db.execute(select(BMCULinkEvent.uart_sequence).where(BMCULinkEvent.device_id == "dev1")))
            .scalars()
            .all()
        )
        dev2_count = (
            await db.execute(
                select(func.count(BMCULinkEvent.id)).where(BMCULinkEvent.device_id == "dev2")
            )
        ).scalar()
    # Oldest = largest age_days = seq 6,7,8 pruned; newest 5 (seq 1-5) kept
    assert sorted(dev1_seqs) == [1, 2, 3, 4, 5]
    assert dev2_count == 3
