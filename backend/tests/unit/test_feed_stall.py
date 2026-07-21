"""Unit tests for the BMCU feed-stall detector (2026-07-21 air-print incident).

Ground truth from the incident telemetry: healthy channels idle around
pull_pct 40-55; the failing slot 4 (index 3) sat at pull_pct 0 with
ams_motion 2 (on-use) for hours while the printer air-printed to a normal
FINISH. Detection must be a compound condition — pull_pct alone is not an
anomaly — and every degenerate input must dissolve the episode (fail-safe:
a spurious pause scars a healthy print).
"""

from backend.app.services.feed_stall import (
    FeedStallDetector,
    FeedStallTrigger,
    FeedStallWarning,
)


def _status(pull=(41, 49, 48, 0), motion=(0, 0, 0, 2), slot=3):
    return {"pull_pct": list(pull), "motion": list(motion), "current_slot": slot}


def _detector(**kw):
    defaults = dict(starve_pct=5, neutral_pct=20, after_s=30.0, warn_after_s=15.0, max_age_s=20.0)
    defaults.update(kw)
    return FeedStallDetector(**defaults)


def _obs(d, now, *, status=..., printing=True, layer=100, age=1.0):
    if status is ...:
        status = _status()
    return d.observe(1, printing=printing, layer_num=layer, status=status, age_s=age, now=now)


class TestWarningStage:
    def test_warns_after_confirm_window(self):
        d = _detector()
        healthy = _status(pull=(41, 49, 48, 50))
        assert _obs(d, 0.0, status=healthy) is None
        degraded = _status(pull=(41, 49, 48, 12))
        assert _obs(d, 5.0, status=degraded, layer=101) is None  # below warn_after_s
        ev = _obs(d, 21.0, status=degraded, layer=102)
        assert isinstance(ev, FeedStallWarning)
        assert ev.slot == 3
        assert ev.degraded_layer == 101  # layer where the decline started
        assert ev.pull_pct == 12

    def test_warning_fires_once_per_episode(self):
        d = _detector()
        degraded = _status(pull=(41, 49, 48, 10))
        _obs(d, 0.0, status=degraded)
        ev = _obs(d, 16.0, status=degraded)
        assert isinstance(ev, FeedStallWarning)
        d.mark_warned(1)
        assert _obs(d, 25.0, status=degraded) is None

    def test_recovery_rearms_warning(self):
        d = _detector()
        degraded = _status(pull=(41, 49, 48, 10))
        _obs(d, 0.0, status=degraded)
        assert isinstance(_obs(d, 16.0, status=degraded), FeedStallWarning)
        d.mark_warned(1)
        _obs(d, 20.0, status=_status(pull=(41, 49, 48, 50)))  # recovered
        _obs(d, 30.0, status=degraded)
        assert isinstance(_obs(d, 46.0, status=degraded), FeedStallWarning)

    def test_no_warning_when_not_on_use(self):
        d = _detector()
        # Idle channels legitimately sit anywhere; motion 0 = idle.
        s = _status(pull=(0, 49, 48, 50), motion=(0, 0, 0, 2), slot=3)
        _obs(d, 0.0, status=s)
        assert _obs(d, 20.0, status=s) is None


class TestPauseStage:
    def test_incident_shape_triggers(self):
        """Slot 4 pinned at 0% while on-use — the actual 2026-07-21 telemetry."""
        d = _detector()
        assert _obs(d, 0.0, layer=140) is None
        assert _obs(d, 10.0, layer=140) is None
        ev = _obs(d, 31.0, layer=141)
        assert isinstance(ev, FeedStallTrigger)
        assert ev.slot == 3
        assert ev.current_layer == 141
        assert ev.degraded_layer == 140
        assert ev.starved_for_s >= 30.0

    def test_trigger_latches_until_marked(self):
        d = _detector()
        _obs(d, 0.0)
        assert isinstance(_obs(d, 31.0), FeedStallTrigger)
        # Not marked (pause failed): retries next tick.
        assert isinstance(_obs(d, 36.0), FeedStallTrigger)
        d.mark_notified(1)
        assert _obs(d, 41.0) is None

    def test_brief_dip_does_not_trigger(self):
        d = _detector()
        _obs(d, 0.0)
        _obs(d, 10.0, status=_status(pull=(41, 49, 48, 45)))  # buffer recovered
        assert _obs(d, 40.0) is None  # new episode, clock restarted

    def test_warning_precedes_trigger(self):
        d = _detector()
        _obs(d, 0.0)
        ev = _obs(d, 16.0)
        assert isinstance(ev, FeedStallWarning)  # starved implies degraded
        d.mark_warned(1)
        assert isinstance(_obs(d, 31.0), FeedStallTrigger)

    def test_slot_change_restarts_episode(self):
        d = _detector()
        _obs(d, 0.0, status=_status(pull=(0, 49, 48, 0), motion=(2, 0, 0, 0), slot=0))
        # Filament swap moved to slot 3; starvation clock must restart.
        assert _obs(d, 31.0, status=_status()) is None


class TestFailSafeGates:
    def test_not_printing_resets(self):
        d = _detector()
        _obs(d, 0.0)
        _obs(d, 10.0, printing=False)
        assert _obs(d, 40.0) is None  # episode dissolved while paused/idle

    def test_stale_telemetry_resets(self):
        d = _detector()
        _obs(d, 0.0)
        _obs(d, 10.0, age=120.0)  # resync flood: minutes-old data
        assert _obs(d, 40.0) is None

    def test_missing_status_resets(self):
        d = _detector()
        _obs(d, 0.0)
        _obs(d, 10.0, status=None)
        assert _obs(d, 40.0) is None

    def test_malformed_fields_no_crash_no_trigger(self):
        d = _detector()
        for bad in (
            {"pull_pct": "x", "motion": [0], "current_slot": 0},
            {"pull_pct": [0], "motion": [2], "current_slot": 5},
            {"pull_pct": [0], "motion": [2], "current_slot": None},
            {"pull_pct": [None, 0], "motion": [2, 2], "current_slot": 0},
            {},
        ):
            assert _obs(d, 0.0, status=bad) is None
            assert _obs(d, 100.0, status=bad) is None
