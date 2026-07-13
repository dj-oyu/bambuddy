"""Tests for the notify-only stall watcher decision core (main._stall_check).

An unrecovered RUNNING→PAUSE episode must fire exactly one notification after
the threshold; recovery, completion or any other state ends the episode and
re-arms detection. (Motivated by the 2026-07-13 runout loop that ran
unattended for 70+ minutes with no notification path covering PAUSE.)
"""

from unittest.mock import patch

import pytest

from backend.app import main as app_main


@pytest.fixture(autouse=True)
def _clean_state():
    with (
        patch.dict(app_main._stall_episodes, {}, clear=True),
        patch.dict(app_main._stall_prev_state, {}, clear=True),
        patch.object(app_main, "_STALL_NOTIFY_AFTER_S", 300.0),
    ):
        yield


class TestStallCheck:
    def test_fires_once_after_threshold(self):
        assert app_main._stall_check(1, "RUNNING", 0.0) is False
        assert app_main._stall_check(1, "PAUSE", 60.0) is False      # episode start
        assert app_main._stall_check(1, "PAUSE", 300.0) is False     # 240s elapsed
        assert app_main._stall_check(1, "PAUSE", 361.0) is True      # crossed 300s
        app_main._stall_mark_notified(1)                             # send succeeded
        assert app_main._stall_check(1, "PAUSE", 500.0) is False     # latched
        assert app_main._stall_episodes[1]["prev"] == "RUNNING"

    def test_failed_send_retries_next_tick(self):
        # The latch is only set after a SUCCESSFUL send; a transient failure
        # must not consume the episode's one alert.
        app_main._stall_check(1, "PAUSE", 0.0)
        assert app_main._stall_check(1, "PAUSE", 301.0) is True      # send fails
        assert app_main._stall_check(1, "PAUSE", 361.0) is True      # retried
        app_main._stall_mark_notified(1)
        assert app_main._stall_check(1, "PAUSE", 421.0) is False

    def test_recovery_ends_episode_and_rearms(self):
        app_main._stall_check(1, "RUNNING", 0.0)
        app_main._stall_check(1, "PAUSE", 10.0)
        assert app_main._stall_check(1, "RUNNING", 100.0) is False   # recovered
        assert 1 not in app_main._stall_episodes
        # A fresh pause counts from its own start, not the old one.
        assert app_main._stall_check(1, "PAUSE", 200.0) is False
        assert app_main._stall_check(1, "PAUSE", 450.0) is False     # 250s in
        assert app_main._stall_check(1, "PAUSE", 501.0) is True

    def test_pause_at_startup_has_unknown_prev(self):
        # Watcher restart mid-pause: still detect, origin unknown.
        assert app_main._stall_check(1, "PAUSE", 0.0) is False
        assert app_main._stall_check(1, "PAUSE", 301.0) is True
        assert app_main._stall_episodes[1]["prev"] == "unknown"

    def test_mark_notified_after_recovery_is_noop(self):
        app_main._stall_check(1, "PAUSE", 0.0)
        app_main._stall_check(1, "IDLE", 100.0)   # episode ended before mark
        app_main._stall_mark_notified(1)          # must not crash or recreate

    def test_terminal_states_never_fire(self):
        for state in ("IDLE", "FINISH", "FAILED", "RUNNING", "", None):
            assert app_main._stall_check(1, state, 1000.0) is False
        assert 1 not in app_main._stall_episodes

    def test_printers_tracked_independently(self):
        app_main._stall_check(1, "RUNNING", 0.0)
        app_main._stall_check(2, "RUNNING", 0.0)
        app_main._stall_check(1, "PAUSE", 10.0)
        app_main._stall_check(2, "PAUSE", 200.0)
        assert app_main._stall_check(1, "PAUSE", 311.0) is True
        assert app_main._stall_check(2, "PAUSE", 311.0) is False     # only 111s in
