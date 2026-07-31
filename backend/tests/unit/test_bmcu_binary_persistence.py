from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.database import Base
from backend.app.models.bmcu_binary import BMCUBinaryLossRange, BMCUBinaryRecord
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.framing import FrameHeader, IncrementalFrameParser
from backend.app.services.bmcu_binary.messages import Hello, HelloLink, HelloReplayRange, TransportDrop
from backend.app.services.bmcu_binary.persistence import BinaryPersistence

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bmcu_binary"


@pytest.mark.asyncio
async def test_drop_range_advances_global_ack_idempotently(tmp_path) -> None:
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bmcu.db'}")
    async with test_engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[table for table in Base.metadata.sorted_tables if table.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    persistence = BinaryPersistence(factory)
    status = IncrementalFrameParser().feed((FIXTURES / "bmcu_status.bin").read_bytes())[0]
    boot = status.header.pico_boot_id
    hello = Hello(
        "monitor-drop",
        "1",
        (HelloLink(0, "bmcu-a"),),
        (HelloReplayRange(boot, 1, 5),),
        b"x" * 32,
    )
    assert await persistence.register_hello(hello, boot) == 0
    status = type(status)(
        FrameHeader(
            status.header.message_type,
            status.header.flags,
            len(status.payload),
            1,
            boot,
            status.header.link_index,
        ),
        status.payload,
    )
    assert await persistence.persist("monitor-drop", status) == 1
    drop_payload = TransportDrop(100, 2, 4, 3, 1).encode()
    drop = type(status)(
        FrameHeader(
            MessageType.TRANSPORT_DROP,
            payload_length=len(drop_payload),
            transport_sequence=2,
            pico_boot_id=boot,
            link_index=0xFF,
        ),
        drop_payload,
    )
    assert await persistence.persist("monitor-drop", drop) == 4
    assert await persistence.persist("monitor-drop", drop) == 4
    async with factory() as db:
        assert len((await db.execute(select(BMCUBinaryRecord))).scalars().all()) == 2
        assert len((await db.execute(select(BMCUBinaryLossRange))).scalars().all()) == 1
    await test_engine.dispose()
