"""Race regressions for the queue-route status writes migrated to
printer_lifecycle (batch 2 of the strangler-fig migration).

Both tests simulate the on_print_complete handler (a separate DB session)
winning the row between the route's initial SELECT and its CAS UPDATE:

* /stop must not clobber a terminal status written concurrently — the CAS
  mismatches, the route keeps the completion and still reports the stop.
  (The FORCE part of /stop's documented polarity is about printer
  *connectivity*, not about racing other status writers.)
* /cancel must 400 instead of cancelling an item that got dispatched (or
  completed) after its python-level guard passed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.api.routes.print_queue import cancel_queue_item, stop_queue_item
from backend.app.core.database import Base
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer


@pytest.fixture
async def queue_case():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as db:
        printer = Printer(
            name="Printer",
            serial_number="SERIAL-1",
            ip_address="127.0.0.1",
            access_code="access-code",
            model="X1C",
        )
        db.add(printer)
        await db.flush()
        item = PrintQueueItem(
            printer_id=printer.id,
            status="pending",
            bed_levelling=True,
            flow_cali=False,
            vibration_cali=True,
            layer_inspect=False,
            timelapse=False,
            use_ams=True,
            nozzle_offset_cali=True,
        )
        db.add(item)
        await db.commit()
        case = SimpleNamespace(session_maker=session_maker, item_id=item.id, printer_id=printer.id)

    try:
        yield case
    finally:
        await engine.dispose()


class _RacingSession:
    """Delegate to a real session, but after the FIRST execute (the route's
    initial SELECT) run `interject` — a concurrent writer in its own session."""

    def __init__(self, real, interject):
        self._real = real
        self._interject = interject
        self._fired = False

    async def execute(self, *a, **kw):
        result = await self._real.execute(*a, **kw)
        if not self._fired:
            self._fired = True
            await self._interject()
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _set_status(session_maker, item_id, status):
    async with session_maker() as other:
        await other.execute(update(PrintQueueItem).where(PrintQueueItem.id == item_id).values(status=status))
        await other.commit()


async def _final_status(session_maker, item_id):
    async with session_maker() as db:
        return (await db.get(PrintQueueItem, item_id)).status


@pytest.mark.asyncio
async def test_stop_does_not_clobber_concurrent_completion(queue_case):
    await _set_status(queue_case.session_maker, queue_case.item_id, "printing")

    async def complete_wins():
        await _set_status(queue_case.session_maker, queue_case.item_id, "completed")

    with (
        patch(
            "backend.app.services.printer_manager.printer_manager.stop_print",
            MagicMock(return_value=True),
        ),
        patch("backend.app.main.mark_printer_stopped_by_user", MagicMock()),
    ):
        async with queue_case.session_maker() as db:
            racing = _RacingSession(db, complete_wins)
            response = await stop_queue_item(queue_case.item_id, db=racing, auth_result=(None, True))

    assert response["message"] == "Print stopped"
    assert await _final_status(queue_case.session_maker, queue_case.item_id) == "completed"


@pytest.mark.asyncio
async def test_cancel_races_concurrent_dispatch_and_400s(queue_case):
    async def dispatch_wins():
        await _set_status(queue_case.session_maker, queue_case.item_id, "printing")

    async with queue_case.session_maker() as db:
        racing = _RacingSession(db, dispatch_wins)
        with pytest.raises(HTTPException) as exc:
            await cancel_queue_item(queue_case.item_id, db=racing, auth_result=(None, True))

    assert exc.value.status_code == 400
    assert "printing" in exc.value.detail
    assert await _final_status(queue_case.session_maker, queue_case.item_id) == "printing"
