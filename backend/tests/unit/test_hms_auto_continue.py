"""Auto-resume for the HMS faults that always clear by resuming.

Anchored on the live 2026-08-07 pause: item 224 swapped tray 3 -> tray 1 and
the printer stopped at layer 0 with

    0700_8006  sev 3  actions ["CONTINUE", "CHECK_ASSISTANT"]
    0700_0006  sev 1  (no description)

The operator's fix is the Resume button, every time. These tests pin that this
module presses it — and, more importantly, pin every case where it must NOT.
"""

import pytest

from backend.app.services.hms_auto_continue import (
    CONTINUE_NOW,
    GIVE_UP,
    HmsAutoContinue,
    PausedError,
)

JOB = "1487384347"

# The two entries exactly as the printer reported them.
FEED = PausedError(
    short_code="0700_8006",
    full_code="07008006",
    severity=3,
    actions=("CONTINUE", "CHECK_ASSISTANT"),
    job_id=JOB,
)
COMPANION = PausedError(
    short_code="0700_0006",
    full_code="0700210000020006",
    severity=1,
    actions=(),
    job_id=JOB,
)
# AMS firmware-mismatch notice; sev 4, always present on this rig.
AMS_INFO = PausedError(
    short_code="0500_0044",
    full_code="0500040000010044",
    severity=4,
    actions=(),
    job_id=JOB,
)
LIVE_PAUSE = [AMS_INFO, COMPANION, FEED]


@pytest.fixture
def watch():
    # Explicit values so the tests do not drift with the env defaults.
    return HmsAutoContinue(delay_s=30.0, max_attempts=3, settle_s=45.0)


def tick(watch, t, *, paused=True, errors=None, connected=True, printer_id=1):
    return watch.tick(
        printer_id,
        now=t,
        connected=connected,
        paused=paused,
        errors=LIVE_PAUSE if errors is None else errors,
    )


class TestTheLiveCase:
    def test_nothing_happens_inside_the_grace_window(self, watch):
        """30s of room for an operator who is awake to decide first."""
        assert tick(watch, 1000.0) == []
        assert tick(watch, 1029.0) == []

    def test_resumes_once_the_grace_window_passes(self, watch):
        tick(watch, 1000.0)
        actions = tick(watch, 1030.0)
        assert [a.kind for a in actions] == [CONTINUE_NOW]
        a = actions[0]
        # The identifiers the firmware needs; a wrong/missing full_code is
        # silently rejected by the printer (#1830).
        assert (a.short_code, a.full_code, a.job_id) == ("0700_8006", "07008006", JOB)
        assert a.attempts == 0

    def test_companion_fatal_does_not_block(self, watch):
        """0700_0006 is sev 1 and rides along with 0700_8006 every time. Read
        as an independent fatal fault it would block the exact case this module
        exists for."""
        tick(watch, 1000.0)
        assert [a.kind for a in tick(watch, 1030.0)] == [CONTINUE_NOW]

    def test_settle_window_before_spending_another_attempt(self, watch):
        tick(watch, 1000.0)
        tick(watch, 1030.0)
        watch.mark_attempted(1, 1030.0)
        # Still paused, but the resume needs time to re-feed filament.
        assert tick(watch, 1050.0) == []
        assert [a.kind for a in tick(watch, 1075.0)] == [CONTINUE_NOW]


class TestBudget:
    def _spend(self, watch, n, t0=1000.0):
        tick(watch, t0)
        t = t0 + 30.0
        for _ in range(n):
            assert [a.kind for a in tick(watch, t)] == [CONTINUE_NOW]
            watch.mark_attempted(1, t)
            t += 50.0
        return t

    def test_three_attempts_then_give_up(self, watch):
        t = self._spend(watch, 3)
        actions = tick(watch, t)
        assert [a.kind for a in actions] == [GIVE_UP]
        assert actions[0].attempts == 3

    def test_give_up_notifies_once(self, watch):
        t = self._spend(watch, 3)
        assert [a.kind for a in tick(watch, t)] == [GIVE_UP]
        watch.mark_notified(1)
        assert tick(watch, t + 60.0) == []
        assert tick(watch, t + 600.0) == []

    def test_budget_follows_the_job_not_the_pause(self, watch):
        """A print that resumes and pauses again later keeps counting toward
        the same budget — otherwise a fault that recurs every layer resumes
        forever."""
        t = self._spend(watch, 2)
        tick(watch, t, paused=False)  # resumed, printing again
        tick(watch, t + 100.0)  # pauses again, same job
        assert [a.kind for a in tick(watch, t + 130.0)] == [CONTINUE_NOW]
        watch.mark_attempted(1, t + 130.0)
        assert [a.kind for a in tick(watch, t + 300.0)] == [GIVE_UP]

    def test_new_job_gets_a_fresh_budget(self, watch):
        self._spend(watch, 3)
        nxt = [PausedError("0700_8006", "07008006", 3, ("CONTINUE",), job_id="9999")]
        tick(watch, 3000.0, errors=nxt)
        assert [a.kind for a in tick(watch, 3030.0, errors=nxt)] == [CONTINUE_NOW]


class TestRefusals:
    def test_not_paused(self, watch):
        assert tick(watch, 1000.0, paused=False) == []
        assert tick(watch, 1030.0, paused=False) == []

    def test_disconnected_printer_is_not_evidence(self, watch):
        assert tick(watch, 1000.0, connected=False) == []
        assert tick(watch, 1030.0, connected=False) == []

    def test_reconnect_restarts_the_grace_window(self, watch):
        """A blackout must not be counted as time spent paused — otherwise the
        first tick after a reconnect resumes instantly."""
        tick(watch, 1000.0)
        tick(watch, 1010.0, connected=False)
        tick(watch, 1020.0)  # reconnected: pause clock starts again here
        assert tick(watch, 1045.0) == []
        assert [a.kind for a in tick(watch, 1051.0)] == [CONTINUE_NOW]

    def test_code_not_on_the_allowlist(self, watch):
        """Never "anything with a CONTINUE button" — an unknown fault resumed
        blindly is how a jam becomes a print in mid-air."""
        other = [PausedError("0300_0100", "03000100", 3, ("CONTINUE",), job_id=JOB)]
        tick(watch, 1000.0, errors=other)
        assert tick(watch, 1030.0, errors=other) == []

    def test_allowlisted_code_without_a_continue_action(self, watch):
        """The firmware decides what is offered; if it did not offer CONTINUE
        this time, we do not invent it."""
        no_action = [PausedError("0700_8006", "07008006", 3, ("CHECK_ASSISTANT",), job_id=JOB)]
        tick(watch, 1000.0, errors=no_action)
        assert tick(watch, 1030.0, errors=no_action) == []

    def test_unrelated_serious_error_blocks(self, watch):
        """Resuming into a genuinely broken machine is worse than waiting."""
        blocked = [*LIVE_PAUSE, PausedError("0300_0200", "03000200", 2, (), job_id=JOB)]
        tick(watch, 1000.0, errors=blocked)
        assert tick(watch, 1030.0, errors=blocked) == []

    def test_blocker_clearing_lets_it_proceed(self, watch):
        blocked = [*LIVE_PAUSE, PausedError("0300_0200", "03000200", 1, (), job_id=JOB)]
        tick(watch, 1000.0, errors=blocked)
        assert tick(watch, 1030.0, errors=blocked) == []
        assert [a.kind for a in tick(watch, 1040.0)] == [CONTINUE_NOW]

    def test_info_severity_errors_do_not_block(self, watch):
        """0500_0044 (sev 4) is present on this rig permanently."""
        tick(watch, 1000.0, errors=[AMS_INFO, FEED])
        assert [a.kind for a in tick(watch, 1030.0, errors=[AMS_INFO, FEED])] == [CONTINUE_NOW]

    def test_paused_with_no_hms_at_all(self, watch):
        """A user-initiated pause must never be auto-resumed."""
        tick(watch, 1000.0, errors=[])
        assert tick(watch, 1030.0, errors=[]) == []

    def test_disabled_by_env(self, watch, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_HMS_AUTO_CONTINUE", "0")
        tick(watch, 1000.0)
        assert tick(watch, 1030.0) == []


class TestEnvOverrides:
    def test_codes_are_configurable(self, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_HMS_AUTO_CONTINUE_CODES", "0300_0100")
        w = HmsAutoContinue(delay_s=0.0, max_attempts=3, settle_s=45.0)
        assert tick(w, 1000.0) == []
        other = [PausedError("0300_0100", "03000100", 3, ("CONTINUE",), job_id=JOB)]
        assert [a.kind for a in tick(w, 1000.0, errors=other)] == [CONTINUE_NOW]

    def test_companion_list_is_configurable(self, monkeypatch):
        """Drop 0700_0006 from the companions and its sev 1 blocks again —
        the escape hatch if that code ever turns out to mean something."""
        monkeypatch.setenv("BAMBUDDY_HMS_AUTO_CONTINUE_COMPANION_CODES", "")
        w = HmsAutoContinue(delay_s=0.0, max_attempts=3, settle_s=45.0)
        assert tick(w, 1000.0) == []

    def test_garbage_env_falls_back_to_defaults(self, monkeypatch):
        """A typo in a systemd drop-in must not crash the watch loop."""
        monkeypatch.setenv("BAMBUDDY_HMS_AUTO_CONTINUE_MAX_ATTEMPTS", "three")
        monkeypatch.setenv("BAMBUDDY_HMS_AUTO_CONTINUE_DELAY_S", "soon")
        w = HmsAutoContinue()
        assert w.max_attempts == 3
        assert w.delay_s == 30.0


class TestStatusTranslation:
    """`_paused_errors_for` reads real HMSError objects. A wrong field name
    here disables the whole feature silently — exactly how the auto-clear
    resume branch sat dead behind a `gcode_state` attribute that never
    existed. Build the real objects, not mocks."""

    def _errors(self):
        from backend.app.services.bambu_mqtt import HMSError

        return [
            # As parsed from the 32-bit print_error path: 8-char full_code.
            HMSError(
                code="0x8006",
                attr=117473286,
                module=7,
                severity=3,
                actions=["CONTINUE", "CHECK_ASSISTANT"],
                job_id=JOB,
                full_code="07008006",
            ),
            # As parsed from the 64-bit hms[] array: 16-char full_code.
            HMSError(
                code="0x20006",
                attr=117448960,
                module=7,
                severity=1,
                job_id=JOB,
                full_code="0700210000020006",
            ),
        ]

    def test_short_codes_and_fields_survive_translation(self):
        from backend.app.main import _paused_errors_for

        class Status:
            hms_errors = None

        Status.hms_errors = self._errors()
        out = _paused_errors_for(Status())
        assert [e.short_code for e in out] == ["0700_8006", "0700_0006"]
        assert out[0].actions == ("CONTINUE", "CHECK_ASSISTANT")
        assert out[0].full_code == "07008006"
        assert out[1].severity == 1
        assert out[0].job_id == JOB

    def test_translated_status_drives_a_resume(self, watch):
        """End to end over the real objects: the live pause resumes."""
        from backend.app.main import _paused_errors_for

        class Status:
            hms_errors = None

        Status.hms_errors = self._errors()
        errs = _paused_errors_for(Status())
        tick(watch, 1000.0, errors=errs)
        assert [a.kind for a in tick(watch, 1030.0, errors=errs)] == [CONTINUE_NOW]

    def test_status_without_hms_errors_attribute(self):
        from backend.app.main import _paused_errors_for

        class Bare:
            pass

        assert _paused_errors_for(Bare()) == []


class TestMultiplePrinters:
    def test_budgets_are_per_printer(self, watch):
        tick(watch, 1000.0, printer_id=1)
        tick(watch, 1000.0, printer_id=2)
        for t in (1030.0, 1080.0, 1130.0):
            assert [a.kind for a in tick(watch, t, printer_id=1)] == [CONTINUE_NOW]
            watch.mark_attempted(1, t)
        assert [a.kind for a in tick(watch, 1180.0, printer_id=1)] == [GIVE_UP]
        assert [a.kind for a in tick(watch, 1180.0, printer_id=2)] == [CONTINUE_NOW]
