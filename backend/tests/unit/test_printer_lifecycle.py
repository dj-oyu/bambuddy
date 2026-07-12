"""Unit tests for the printer_lifecycle transition facade.

The facade must decide races in SQL (UPDATE ... WHERE status IN + rowcount),
not python check-then-commit — the cross-session tests here are the proof.
"""

import logging
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.printer_lifecycle import (
    TransitionOutcome,
    force_transition,
    transition,
)


@pytest.fixture
async def session_maker():
    # NOTE: aiosqlite :memory: hands every "session" the same underlying
    # connection — cross-session tests that need real commit isolation must
    # use the file-backed `file_session_maker` fixture instead.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def file_session_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_item(session_maker, status="pending"):
    async with session_maker() as db:
        item = PrintQueueItem(status=status)
        db.add(item)
        await db.commit()
        return item.id


async def read_status(session_maker, item_id):
    async with session_maker() as db:
        return (await db.execute(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))).scalar_one()


class TestTransitionCAS:
    async def test_cas_success_updates_row_and_mirrors_item(self, session_maker):
        item_id = await make_item(session_maker)
        started = datetime(2026, 7, 12, 3, 0, 0)
        async with session_maker() as db:
            item = await db.get(PrintQueueItem, item_id)
            result = await transition(
                db,
                item_id,
                to_status="printing",
                from_states=("pending",),
                reason="dispatch",
                caller="test",
                extra={"started_at": started},
                item=item,
            )
            assert result
            assert result.outcome is TransitionOutcome.APPLIED
            assert item.status == "printing"
            assert item.started_at == started
        async with session_maker() as db:
            row = await db.get(PrintQueueItem, item_id)
            assert row.status == "printing"
            assert row.started_at == started

    async def test_cas_mismatch_leaves_row_and_item_untouched(self, session_maker):
        item_id = await make_item(session_maker, status="cancelled")
        async with session_maker() as db:
            item = await db.get(PrintQueueItem, item_id)
            result = await transition(
                db,
                item_id,
                to_status="printing",
                from_states=("pending",),
                reason="dispatch",
                caller="test",
                extra={"error_message": "should not land"},
                item=item,
            )
            assert not result
            assert result.outcome is TransitionOutcome.STATE_MISMATCH
            assert result.observed_status == "cancelled"
            # neither status nor extra columns are mirrored on mismatch
            assert item.status == "cancelled"
            assert item.error_message is None
        assert await read_status(session_maker, item_id) == "cancelled"

    async def test_cross_session_race_decided_by_where_clause(self, session_maker):
        """#1853 shape: session B cancels after session A loaded the row."""
        item_id = await make_item(session_maker)
        async with session_maker() as db_a:
            stale = await db_a.get(PrintQueueItem, item_id)
            assert stale.status == "pending"

            async with session_maker() as db_b:
                await force_transition(
                    db_b, item_id, to_status="cancelled", reason="user cancel", caller="test-b"
                )

            result = await transition(
                db_a,
                item_id,
                to_status="printing",
                from_states=("pending",),
                reason="dispatch",
                caller="test-a",
                item=stale,
            )
            assert result.outcome is TransitionOutcome.STATE_MISMATCH
            assert result.observed_status == "cancelled"
        assert await read_status(session_maker, item_id) == "cancelled"

    async def test_not_found_for_deleted_row(self, session_maker):
        async with session_maker() as db:
            result = await transition(
                db, 424242, to_status="printing", from_states=("pending",), reason="dispatch", caller="test"
            )
            assert result.outcome is TransitionOutcome.NOT_FOUND
            assert result.observed_status is None

    async def test_multiple_from_states_match(self, session_maker):
        item_id = await make_item(session_maker, status="printing")
        async with session_maker() as db:
            result = await transition(
                db,
                item_id,
                to_status="failed",
                from_states=("pending", "printing"),
                reason="error path",
                caller="test",
            )
            assert result
        assert await read_status(session_maker, item_id) == "failed"

    async def test_empty_from_states_rejected(self, session_maker):
        async with session_maker() as db:
            with pytest.raises(ValueError, match="from_states"):
                await transition(db, 1, to_status="failed", from_states=(), reason="r", caller="test")

    async def test_commit_false_invisible_until_caller_commits(self, file_session_maker):
        # needs a file-backed DB: the shared in-memory connection would show
        # uncommitted changes to every "session"
        session_maker = file_session_maker
        item_id = await make_item(session_maker)
        async with session_maker() as db:
            result = await transition(
                db,
                item_id,
                to_status="printing",
                from_states=("pending",),
                reason="batched",
                caller="test",
                commit=False,
            )
            assert result
            # other sessions still see the old value pre-commit
            assert await read_status(session_maker, item_id) == "pending"
            await db.commit()
        assert await read_status(session_maker, item_id) == "printing"

    async def test_extra_column_whitelist(self, session_maker):
        async with session_maker() as db:
            with pytest.raises(ValueError, match="printer_id"):
                await transition(
                    db,
                    1,
                    to_status="failed",
                    from_states=("pending",),
                    reason="r",
                    caller="test",
                    extra={"printer_id": 5},
                )


class TestForceTransition:
    async def test_force_clobbers_concurrent_state(self, session_maker):
        item_id = await make_item(session_maker, status="cancelled")
        async with session_maker() as db:
            item = await db.get(PrintQueueItem, item_id)
            result = await force_transition(
                db,
                item_id,
                to_status="failed",
                reason="stop regardless",
                caller="test",
                extra={"error_message": "boom"},
                item=item,
            )
            assert result
            assert result.from_states is None
            assert item.status == "failed"
            assert item.error_message == "boom"
        async with session_maker() as db:
            row = await db.get(PrintQueueItem, item_id)
            assert row.status == "failed"
            assert row.error_message == "boom"

    async def test_force_not_found(self, session_maker):
        async with session_maker() as db:
            result = await force_transition(db, 424242, to_status="failed", reason="r", caller="test")
            assert result.outcome is TransitionOutcome.NOT_FOUND


class TestLifecycleLogging:
    async def test_applied_logs_info_with_context(self, session_maker, caplog):
        item_id = await make_item(session_maker)
        with caplog.at_level(logging.INFO, logger="backend.app.services.printer_lifecycle"):
            async with session_maker() as db:
                await transition(
                    db,
                    item_id,
                    to_status="printing",
                    from_states=("pending",),
                    reason="dispatch",
                    caller="scheduler",
                )
        [record] = [r for r in caplog.records if "PQ_LIFECYCLE" in r.getMessage()]
        assert record.levelno == logging.INFO
        message = record.getMessage()
        assert f"item={item_id}" in message
        assert "pending->printing" in message
        assert "outcome=applied" in message
        assert "reason=dispatch" in message
        assert "caller=scheduler" in message

    async def test_mismatch_logs_warning(self, session_maker, caplog):
        item_id = await make_item(session_maker, status="cancelled")
        with caplog.at_level(logging.INFO, logger="backend.app.services.printer_lifecycle"):
            async with session_maker() as db:
                await transition(
                    db,
                    item_id,
                    to_status="printing",
                    from_states=("pending",),
                    reason="dispatch",
                    caller="scheduler",
                )
        [record] = [r for r in caplog.records if "PQ_LIFECYCLE" in r.getMessage()]
        assert record.levelno == logging.WARNING
        assert "outcome=state_mismatch" in record.getMessage()

    async def test_force_success_log_format(self, session_maker, caplog):
        item_id = await make_item(session_maker)
        with caplog.at_level(logging.INFO, logger="backend.app.services.printer_lifecycle"):
            async with session_maker() as db:
                await force_transition(db, item_id, to_status="cancelled", reason="stop", caller="route")
        [record] = [r for r in caplog.records if "PQ_LIFECYCLE" in r.getMessage()]
        assert record.levelno == logging.INFO
        assert "FORCE->cancelled" in record.getMessage()
        assert "outcome=applied" in record.getMessage()
