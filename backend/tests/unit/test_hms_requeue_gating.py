"""Regression tests for the HMS auto-clear requeue gates.

Incident (2026-07-12): after a print finished, the scheduler dispatched the
next queue item normally; the BMCU raised its usual 0500_409D while engaging
the feeder for the new job's filament load. The HMS auto-clear helper then
requeued the *live* item ('printing' → 'pending') because gcode_state still
read FINISH/IDLE (A1 state reporting lags the print command). The next
scheduler tick double-dispatched the item, aborting the first job
mid-filament-load — leaving the BMCU feeding two slots into the hub at once
(hard physical jam).

The fix: `_requeue_print_rejected_by_hms` must not requeue when
  - printer state is unknown (fail-safe),
  - the scheduler's post-dispatch hold is active (acceptance window — the
    wedge watchdog #1678 owns the dead-or-alive call there),
  - the AMS/BMCU is mid-motion (ams_status_main != 0).
It still requeues in the genuine 409D-rejection case: printer idle, no hold,
AMS at rest.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.main import _requeue_print_rejected_by_hms


def _client(printer_state, ams_status_main=0, connected=True):
    """printer_state maps to PrinterState.state (there is no gcode_state attr —
    the pre-fix helper read that nonexistent name and always got None)."""
    return SimpleNamespace(
        state=SimpleNamespace(state=printer_state, ams_status_main=ams_status_main, connected=connected)
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("backend.app.main.asyncio.sleep", instant)


@pytest.fixture
def db_session():
    """Mock async_session; returns the mock DB and a captured items list."""
    item = SimpleNamespace(status="printing", started_at="x")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [item]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, _q):
            return result

        async def commit(self):
            FakeSession.committed = True

    FakeSession.committed = False
    return FakeSession, item


class TestGates:
    def test_unknown_state_does_nothing(self, db_session):
        FakeSession, item = db_session
        with (
            patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=_client(None)),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"
        assert FakeSession.committed is False

    def test_unknown_sentinel_state_does_nothing(self, db_session):
        """PrinterState.state defaults to the string "unknown" — treat it like None."""
        FakeSession, item = db_session
        with (
            patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=_client("unknown")),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"
        assert FakeSession.committed is False

    def test_running_state_does_nothing(self, db_session):
        FakeSession, item = db_session
        with (
            patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=_client("RUNNING")),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"

    def test_dispatch_hold_blocks_requeue(self, db_session):
        """The incident scenario: gcode_state lags at FINISH right after a
        project_file was sent. The hold must veto the requeue."""
        FakeSession, item = db_session
        with (
            patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=_client("FINISH")),
            patch("backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold", return_value=True),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"
        assert FakeSession.committed is False

    def test_disconnected_blocks_requeue(self, db_session):
        """Disconnected printer: all state (incl. ams_status_main) may be stale
        from the dead session — never requeue on it."""
        FakeSession, item = db_session
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_client",
                return_value=_client("IDLE", connected=False),
            ),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"
        assert FakeSession.committed is False

    def test_ams_motion_blocks_requeue(self, db_session):
        """BMCU mid-filament-change (ams_status_main=1) with idle gcode_state
        must not be treated as a dead job."""
        FakeSession, item = db_session
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_client",
                return_value=_client("IDLE", ams_status_main=1),
            ),
            patch("backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold", return_value=False),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"

    def test_genuine_rejection_still_requeues(self, db_session):
        """Printer demonstrably idle, no hold, AMS at rest — the original
        409D-rejection rescue must keep working with no added delay."""
        FakeSession, item = db_session
        with (
            patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=_client("IDLE")),
            patch("backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold", return_value=False),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "pending"
        assert item.started_at is None
        assert FakeSession.committed is True
