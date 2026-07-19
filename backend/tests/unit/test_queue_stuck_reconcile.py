"""Tests for the printing→pending queue-item safety net and the HMS retry
dispatch gate.

Incident (2026-07-13): a 409D-rejected dispatch left queue item 99 in
'printing' while the printer sat IDLE — the HMS auto-clear had rate-limited
itself into permanent silence, and nothing else owned the stuck-item shape.
`reconcile_stuck_queue_items` rescues it regardless of cause; the scheduler
gate makes the retry tracker's backoff real (without it the 30s tick would
re-dispatch immediately once the item is pending again).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.main import reconcile_stuck_queue_items


def _client(printer_state, ams_status_main=0, connected=True):
    return SimpleNamespace(
        state=SimpleNamespace(state=printer_state, ams_status_main=ams_status_main, connected=connected)
    )


def _item(age_s: float, tz_aware: bool = False):
    started = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    if not tz_aware:
        started = started.replace(tzinfo=None)
    return SimpleNamespace(id=1, status="printing", started_at=started)


@pytest.fixture
def db_session():
    result = MagicMock()
    result.rowcount = 1
    result.scalars.return_value.all.return_value = []

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
    return FakeSession, result


def _run(FakeSession, state_client, hold=False):
    import asyncio

    with (
        patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=state_client),
        patch("backend.app.services.print_scheduler.scheduler.printer_in_dispatch_hold", return_value=hold),
        patch("backend.app.main.async_session", FakeSession),
    ):
        # asyncio.run (not get_event_loop) — under asyncio_mode=auto the
        # ambient loop belongs to pytest-asyncio and may be closed between
        # test modules; a fresh loop per call is hermetic.
        return asyncio.run(reconcile_stuck_queue_items(1))


class TestReconcileStuckQueueItems:
    def test_old_stuck_item_requeued(self, db_session):
        FakeSession, result = db_session
        item = _item(age_s=700)
        result.scalars.return_value.all.return_value = [item]
        assert _run(FakeSession, _client("IDLE")) == 1
        assert item.status == "pending"
        assert item.started_at is None
        assert FakeSession.committed

    def test_tz_aware_started_at_also_works(self, db_session):
        FakeSession, result = db_session
        item = _item(age_s=700, tz_aware=True)
        result.scalars.return_value.all.return_value = [item]
        assert _run(FakeSession, _client("IDLE")) == 1

    def test_young_item_left_alone(self, db_session):
        FakeSession, result = db_session
        item = _item(age_s=120)
        result.scalars.return_value.all.return_value = [item]
        assert _run(FakeSession, _client("IDLE")) == 0
        assert item.status == "printing"
        assert not FakeSession.committed

    def test_missing_started_at_left_alone(self, db_session):
        FakeSession, result = db_session
        item = SimpleNamespace(id=1, status="printing", started_at=None)
        result.scalars.return_value.all.return_value = [item]
        assert _run(FakeSession, _client("IDLE")) == 0

    def test_running_printer_skipped_entirely(self, db_session):
        FakeSession, result = db_session
        result.scalars.return_value.all.return_value = [_item(age_s=700)]
        assert _run(FakeSession, _client("RUNNING")) == 0
        assert not FakeSession.committed

    def test_dispatch_hold_skips(self, db_session):
        FakeSession, result = db_session
        result.scalars.return_value.all.return_value = [_item(age_s=700)]
        assert _run(FakeSession, _client("IDLE"), hold=True) == 0

    def test_disconnected_skips(self, db_session):
        FakeSession, result = db_session
        result.scalars.return_value.all.return_value = [_item(age_s=700)]
        assert _run(FakeSession, _client("IDLE", connected=False)) == 0

    def test_ams_motion_skips(self, db_session):
        FakeSession, result = db_session
        result.scalars.return_value.all.return_value = [_item(age_s=700)]
        assert _run(FakeSession, _client("IDLE", ams_status_main=768)) == 0

    def test_ams_busy_hard_stuck_override(self, db_session):
        """2026-07-19: BMCU wedged at ams_status_main=3 after a 409D-rejected
        start keeps the AMS-busy gate closed forever. Past the hard-stuck
        threshold (30 min) that one gate is overridden."""
        FakeSession, result = db_session
        item = _item(age_s=2000)
        result.scalars.return_value.all.return_value = [item]
        assert _run(FakeSession, _client("IDLE", ams_status_main=3)) == 1
        assert item.status == "pending"

    def test_ams_busy_below_hard_threshold_still_skips(self, db_session):
        FakeSession, result = db_session
        item = _item(age_s=1200)  # > 600s soft, < 1800s hard
        result.scalars.return_value.all.return_value = [item]
        assert _run(FakeSession, _client("IDLE", ams_status_main=3)) == 0
        assert item.status == "printing"

    def test_hard_threshold_does_not_override_other_gates(self, db_session):
        """Only the AMS-busy gate is overridable — disconnected/hold/running
        stay hard requirements no matter how old the item is."""
        FakeSession, result = db_session
        result.scalars.return_value.all.return_value = [_item(age_s=5000)]
        assert _run(FakeSession, _client("IDLE", connected=False)) == 0
        result.scalars.return_value.all.return_value = [_item(age_s=5000)]
        assert _run(FakeSession, _client("RUNNING")) == 0
        result.scalars.return_value.all.return_value = [_item(age_s=5000)]
        assert _run(FakeSession, _client("IDLE"), hold=True) == 0

    def test_cas_loss_not_counted(self, db_session):
        # A concurrent terminal write wins the row: rowcount=0 -> skip, no commit.
        FakeSession, result = db_session
        result.scalars.return_value.all.return_value = [_item(age_s=700)]
        result.rowcount = 0
        assert _run(FakeSession, _client("IDLE")) == 0
        assert not FakeSession.committed


class TestSchedulerDispatchGate:
    def _idle(self, allowed: bool) -> bool:
        from backend.app.services.print_scheduler import scheduler

        state = SimpleNamespace(state="IDLE")
        with (
            patch("backend.app.services.print_scheduler.printer_manager.is_connected", return_value=True),
            patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=state),
            patch("backend.app.services.print_scheduler.printer_manager.is_awaiting_plate_clear", return_value=False),
            patch("backend.app.services.hms_retry.hms_retry.dispatch_allowed", return_value=allowed),
        ):
            return scheduler._is_printer_idle(1)

    def test_backoff_holds_dispatch(self):
        assert self._idle(allowed=False) is False

    def test_allowed_dispatches(self):
        assert self._idle(allowed=True) is True
