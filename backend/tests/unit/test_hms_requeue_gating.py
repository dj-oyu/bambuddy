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
    item = SimpleNamespace(id=1, status="printing", started_at="x")
    result = MagicMock()
    # transition()'s CAS branches on rowcount — a bare MagicMock is truthy and
    # would silently take the APPLIED path; make it explicit (tests flip it to
    # 0 to exercise the mismatch arm).
    result.rowcount = 1
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
    FakeSession.result = result
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

    def test_ams_motion_blocks_requeue_with_grace_disabled(self, db_session, monkeypatch):
        """BMCU mid-filament-change (ams_status_main=1) with idle gcode_state
        must not be treated as a dead job when the latched-status override is
        turned off (BAMBUDDY_HMS_REQUEUE_AMS_GRACE_S=0 → pre-override behavior)."""
        monkeypatch.setattr("backend.app.main._HMS_REQUEUE_AMS_GRACE_S", 0.0)
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

    def test_ams_clears_midpoll_then_requeues(self, db_session, monkeypatch):
        """Real filament load: AMS busy on the first checks, then at rest —
        the poll loop must pick that up and requeue without the override."""
        monkeypatch.setattr("backend.app.main._HMS_REQUEUE_AMS_GRACE_S", 9999.0)
        FakeSession, item = db_session
        clients = [
            _client("IDLE", ams_status_main=1),
            _client("IDLE", ams_status_main=1),
            _client("IDLE", ams_status_main=0),
        ]
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_client",
                side_effect=clients,
            ),
            patch("backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold", return_value=False),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "pending"
        assert FakeSession.committed is True

    def test_ams_latched_overridden_after_grace(self, db_session, monkeypatch):
        """The 2026-07-21 wedge: ams_status_main latched nonzero forever after
        a 409D-rejected start. Once the grace window elapses with AMS-busy as
        the only blocker, the requeue must proceed."""
        import backend.app.main as main_mod

        monkeypatch.setattr(main_mod, "_HMS_REQUEUE_AMS_GRACE_S", 100.0)
        ticks = iter([0.0, 0.0, 50.0, 200.0])  # deadline calc, then poll checks
        monkeypatch.setattr(main_mod, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        FakeSession, item = db_session
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_client",
                return_value=_client("IDLE", ams_status_main=3),
            ),
            patch("backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold", return_value=False),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "pending"
        assert FakeSession.committed is True

    def test_ams_latched_override_still_respects_dispatch_hold(self, db_session, monkeypatch):
        """Grace elapsed but a new dispatch hold became active in the meantime:
        the override re-check must veto the requeue (only the AMS gate is
        overridable; the hold callable is re-consulted live)."""
        import backend.app.main as main_mod

        monkeypatch.setattr(main_mod, "_HMS_REQUEUE_AMS_GRACE_S", 100.0)
        ticks = iter([0.0, 200.0])
        monkeypatch.setattr(main_mod, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        FakeSession, item = db_session
        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_client",
                return_value=_client("IDLE", ams_status_main=3),
            ),
            patch(
                "backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold",
                side_effect=[False, True],
            ),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"
        assert FakeSession.committed is False

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

    def test_cas_mismatch_leaves_item_untouched(self, db_session):
        """A concurrent terminal write between the SELECT and the CAS UPDATE
        (rowcount 0) must not mirror 'pending' onto the item or commit."""
        FakeSession, item = db_session
        FakeSession.result.rowcount = 0
        with (
            patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=_client("IDLE")),
            patch("backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold", return_value=False),
            patch("backend.app.main.async_session", FakeSession),
        ):
            asyncio.run(_requeue_print_rejected_by_hms(1, "0500_409D"))
        assert item.status == "printing"
        assert item.started_at == "x"
        assert FakeSession.committed is False
