"""Unit tests for the level-triggered HMS auto-clear retry tracker.

Recreates the 2026-07-13 incident shape: BMCU 0500_409D fires at print start,
the printer rejects the job and stays IDLE, every clear "succeeds" but the
next dispatch re-raises the code. The old edge-triggered rate limiter went
permanently silent after 3 attempts; the tracker must retry forever with
backoff, escalate to a human every N failures, and recover cleanly.
"""

import pytest

from backend.app.services.hms_retry import (
    ATTEMPT,
    ESCALATE,
    RECOVERED,
    HmsRetryTracker,
)

CODE = "0500_409D"
SEV_COMMON = 3


def make_tracker(**kw):
    defaults = dict(
        backoff_s=(60.0, 300.0, 900.0, 1800.0),
        settle_s=150.0,
        escalate_after=3,
        renotify_every=3,
        absent_grace_s=180.0,
    )
    defaults.update(kw)
    return HmsRetryTracker(**defaults)


def tick(tr, now, *, present=True, connected=True, active=False, severity=SEV_COMMON, other=()):
    return tr.tick(
        1,
        now=now,
        connected=connected,
        print_active=active,
        autoclear_present={CODE: severity} if present else {},
        other_serious=list(other),
    )


def fail_once(tr, now):
    """Drive one full attempt->settle-expiry->failure cycle. Returns (new_now, actions_at_failure)."""
    actions = tick(tr, now)
    assert [a.kind for a in actions] == [ATTEMPT]
    tr.mark_attempted(1, CODE, now)
    now += tr.settle_s + 1
    return now, tick(tr, now)


class TestRetryLoop:
    def test_first_observation_attempts_immediately(self):
        tr = make_tracker()
        actions = tick(tr, 1000.0)
        assert [a.kind for a in actions] == [ATTEMPT]

    def test_code_present_at_startup_is_detected(self):
        # Level trigger: no "new appearance" edge required (the old code
        # missed codes already latched when bambuddy started).
        tr = make_tracker()
        assert [a.kind for a in tick(tr, 5.0)] == [ATTEMPT]

    def test_settle_window_suppresses_further_attempts(self):
        tr = make_tracker()
        now = 1000.0
        tick(tr, now)
        tr.mark_attempted(1, CODE, now)
        assert tick(tr, now + 30) == []
        assert tick(tr, now + 149) == []

    def test_backoff_progression_and_cap(self):
        tr = make_tracker()
        now = 1000.0
        expected_waits = [60.0, 300.0, 900.0, 1800.0, 1800.0, 1800.0]
        for i, wait in enumerate(expected_waits):
            now, _ = fail_once(tr, now)
            ep = tr.episode_info(1, CODE)
            assert ep.failures == i + 1
            assert ep.next_attempt_at == pytest.approx(now + wait)
            # Not yet time: no attempt.
            assert all(a.kind != ATTEMPT for a in tick(tr, now + wait - 1))
            now += wait

    def test_never_gives_up(self):
        tr = make_tracker()
        now = 1000.0
        for _ in range(20):
            now, _ = fail_once(tr, now)
            now += 1800.0
        assert [a.kind for a in tick(tr, now)] == [ATTEMPT]

    def test_dispatch_gate_blocks_during_backoff_only(self):
        tr = make_tracker()
        now = 1000.0
        # No episode: allowed.
        assert tr.dispatch_allowed(1, now)
        tick(tr, now)
        tr.mark_attempted(1, CODE, now)
        # Pending attempt (settle): the dispatch IS the retry — allowed.
        assert tr.dispatch_allowed(1, now + 10)
        now += tr.settle_s + 1
        tick(tr, now)  # settle expired with code present -> failure #1
        # Waiting out backoff with code present: blocked.
        assert not tr.dispatch_allowed(1, now + 1)
        # Backoff expired: allowed again.
        assert tr.dispatch_allowed(1, now + 61)
        # Other printers unaffected.
        assert tr.dispatch_allowed(2, now + 1)


class TestEscalation:
    def drive_failures(self, tr, now, n):
        for _ in range(n):
            now, actions = fail_once(tr, now)
            now += 1800.0
        return now, actions

    def test_escalates_at_threshold_then_every_n(self):
        tr = make_tracker()
        now = 1000.0
        for i in range(1, 10):
            now, actions = fail_once(tr, now)
            kinds = [a.kind for a in actions]
            if i in (3, 6, 9):
                assert kinds == [ESCALATE], f"failure {i}"
                tr.mark_notified(1, CODE)
            else:
                assert ESCALATE not in kinds, f"failure {i}"
            now += 1800.0

    def test_failed_send_retries_next_tick(self):
        tr = make_tracker()
        now = 1000.0
        for i in range(3):
            now, actions = fail_once(tr, now)
            if i < 2:
                now += 1800.0
        assert [a.kind for a in actions] == [ESCALATE]
        # mark_notified NOT called (send failed) -> escalate again during backoff.
        again = tick(tr, now)
        assert [a.kind for a in again] == [ESCALATE]
        tr.mark_notified(1, CODE)
        assert ESCALATE not in [a.kind for a in tick(tr, now + 1)]

    def test_escalation_carries_context(self):
        tr = make_tracker()
        now = 1000.0
        for _ in range(3):
            now, actions = fail_once(tr, now)
            now += 1800.0
        (a,) = actions
        assert a.failures == 3
        assert a.since == 1000.0
        assert a.printer_id == 1 and a.code == CODE


class TestRecovery:
    def test_success_closes_silently_when_never_notified(self):
        tr = make_tracker()
        now = 1000.0
        tick(tr, now)
        tr.mark_attempted(1, CODE, now)
        # Print starts, code gone: closed, no notification owed.
        actions = tick(tr, now + 60, present=False, active=True)
        assert actions == []
        assert tr.episode_info(1, CODE) is None

    def test_recovery_notification_after_escalation(self):
        tr = make_tracker()
        now = 1000.0
        for _ in range(3):
            now, actions = fail_once(tr, now)
            now += 1800.0
        tr.mark_notified(1, CODE)
        actions = tick(tr, now, present=False, active=True)
        assert [a.kind for a in actions] == [RECOVERED]
        assert actions[0].failures == 3
        # Episode gone; nothing further.
        assert tick(tr, now + 60, present=False, active=True) == []

    def test_code_absent_without_print_closes_after_grace(self):
        tr = make_tracker()
        now = 1000.0
        tick(tr, now)
        tr.mark_attempted(1, CODE, now)
        now += tr.settle_s + 1
        # Code gone but printer idle (user cleared it by hand, queue empty).
        assert tick(tr, now, present=False) == []
        assert tr.episode_info(1, CODE) is not None  # grace running
        assert tick(tr, now + 181, present=False) == []
        assert tr.episode_info(1, CODE) is None

    def test_code_flicker_does_not_close_episode(self):
        # 409D drops off right after a clear and re-fires at the next
        # dispatch — a short absence must not count as recovery.
        tr = make_tracker()
        now = 1000.0
        tick(tr, now)
        tr.mark_attempted(1, CODE, now)
        tick(tr, now + 30, present=False)
        actions = tick(tr, now + 60, present=True)
        assert tr.episode_info(1, CODE) is not None
        assert actions == []  # still inside settle


class TestPowerAndConnectivity:
    def test_disconnect_freezes_episode(self):
        tr = make_tracker()
        now = 1000.0
        tick(tr, now)
        tr.mark_attempted(1, CODE, now)
        # Blackout past the settle deadline.
        assert tick(tr, now + 500, connected=False) == []
        ep = tr.episode_info(1, CODE)
        assert ep.failures == 0  # blackout did not count as a failure
        # Reconnect: settle window re-armed, still no failure.
        assert tick(tr, now + 600) == []
        assert tr.episode_info(1, CODE).failures == 0
        # New settle window expires with code still present -> now it fails.
        actions = tick(tr, now + 600 + tr.settle_s + 1)
        assert tr.episode_info(1, CODE).failures == 1

    def test_reconnect_with_code_gone_recovers(self):
        tr = make_tracker()
        now = 1000.0
        for _ in range(3):
            now, actions = fail_once(tr, now)
            now += 1800.0
        tr.mark_notified(1, CODE)
        tick(tr, now, connected=False)
        # User power-cycled the BMCU; printer comes back clean and printing.
        actions = tick(tr, now + 300, present=False, active=True)
        assert [a.kind for a in actions] == [RECOVERED]


class TestSeverityGate:
    def test_other_serious_error_blocks_and_escalates_immediately(self):
        tr = make_tracker()
        now = 1000.0
        actions = tick(tr, now, other=["0300_1234 (sev 1): heatbed failure"])
        kinds = [a.kind for a in actions]
        assert ATTEMPT not in kinds
        assert kinds == [ESCALATE]
        assert "serious/fatal" in actions[0].blocked_reason
        tr.mark_notified(1, CODE)
        # Latched while blocked; still no attempts.
        assert tick(tr, now + 60, other=["0300_1234 (sev 1): heatbed failure"]) == []
        # Dispatch held while blocked.
        assert not tr.dispatch_allowed(1, now + 60)

    def test_unblock_resumes_attempts(self):
        tr = make_tracker()
        now = 1000.0
        tick(tr, now, other=["0300_1234 (sev 1): heatbed failure"])
        tr.mark_notified(1, CODE)
        actions = tick(tr, now + 120)
        assert [a.kind for a in actions] == [ATTEMPT]

    def test_worsened_severity_blocks(self):
        tr = make_tracker()
        now = 1000.0
        tick(tr, now, severity=3)
        tr.mark_attempted(1, CODE, now)
        actions = tick(tr, now + 200, severity=2)
        assert [a.kind for a in actions] == [ESCALATE]
        assert "severity worsened" in actions[0].blocked_reason
        assert not tr.dispatch_allowed(1, now + 200)


class TestClosedEpisodeCallbacks:
    def test_mark_calls_after_close_are_noops(self):
        tr = make_tracker()
        now = 1000.0
        tick(tr, now)
        tr.mark_attempted(1, CODE, now)
        tick(tr, now + 60, present=False, active=True)  # closes episode
        assert tr.episode_info(1, CODE) is None
        # Late callbacks from an in-flight loop iteration must not crash or
        # resurrect the episode.
        tr.mark_attempted(1, CODE, now + 61)
        tr.mark_notified(1, CODE)
        assert tr.episode_info(1, CODE) is None

    def test_grace_close_after_escalation_reports_idle_recovery(self):
        # Code vanishes with the printer idle (manual fix / empty queue):
        # RECOVERED must carry print_active=False so the notification does
        # not claim "printing again".
        tr = make_tracker()
        now = 1000.0
        for _ in range(3):
            now, actions = fail_once(tr, now)
            now += 1800.0
        tr.mark_notified(1, CODE)
        tick(tr, now, present=False)
        actions = tick(tr, now + 181, present=False)
        assert [a.kind for a in actions] == [RECOVERED]
        assert actions[0].print_active is False

    def test_active_recovery_reports_printing(self):
        tr = make_tracker()
        now = 1000.0
        for _ in range(3):
            now, actions = fail_once(tr, now)
            now += 1800.0
        tr.mark_notified(1, CODE)
        actions = tick(tr, now, present=False, active=True)
        assert actions[0].print_active is True


class TestDispatchGateFailPermissive:
    def test_stale_absent_code_never_wedges(self):
        tr = make_tracker()
        now = 1000.0
        now, _ = fail_once(tr, now)
        # Code disappears: gate must open even mid-backoff.
        tick(tr, now + 1, present=False)
        assert tr.dispatch_allowed(1, now + 2)
