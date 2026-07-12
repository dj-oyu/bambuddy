"""Truth tables for the phase-3 busy/alive predicates in printer_lifecycle.

Each predicate has a documented polarity on uncertainty; the rows here pin
that polarity so a future edit that flips it (e.g. during an upstream merge)
fails loudly:

* print_process_active — FAIL-OPEN (#1890): uncertainty -> False (power may cut)
* evaluate_demonstrably_idle — FAIL-SAFE (BMCU 2026-07-12): uncertainty -> not idle
* active_job_stale — CONSERVATIVE (#1542/#1679): uncertainty -> not stale
"""

from types import SimpleNamespace

import pytest

from backend.app.services.printer_lifecycle import (
    ACTIVE_PRINT_STATES,
    TERMINAL_GCODE_STATES,
    IdleVerdict,
    active_job_stale,
    evaluate_demonstrably_idle,
    print_process_active,
)


def _state(state="IDLE", connected=True, ams_status_main=0, subtask_id="", subtask_name=""):
    return SimpleNamespace(
        state=state,
        connected=connected,
        ams_status_main=ams_status_main,
        subtask_id=subtask_id,
        subtask_name=subtask_name,
    )


class TestPrintProcessActive:
    @pytest.mark.parametrize("state", sorted(ACTIVE_PRINT_STATES))
    def test_active_states(self, state):
        assert print_process_active(_state(state)) is True

    @pytest.mark.parametrize("state", sorted(TERMINAL_GCODE_STATES) + ["", "unknown"])
    def test_inactive_states(self, state):
        assert print_process_active(_state(state)) is False

    def test_fail_open_on_uncertainty(self):
        # Disconnected / missing state must let the smart plug cut power.
        assert print_process_active(None) is False
        assert print_process_active(_state("RUNNING", connected=False)) is False

    def test_paused_print_counts_as_active(self):
        # #1890: a paused print is still loaded on the bed.
        assert print_process_active(_state("PAUSE")) is True


class TestEvaluateDemonstrablyIdle:
    def _verdict(self, printer_state, hold=False):
        return evaluate_demonstrably_idle(printer_state, in_dispatch_hold=lambda: hold)

    @pytest.mark.parametrize("state", sorted(TERMINAL_GCODE_STATES))
    def test_idle_when_all_gates_pass(self, state):
        v = self._verdict(_state(state))
        assert v.idle is True and bool(v) is True

    def test_gate1_no_client_state(self):
        v = self._verdict(None)
        assert not v and v.reason == "printer state unknown"

    def test_gate1_unknown_sentinel(self):
        v = self._verdict(_state("unknown"))
        assert not v and v.reason == "printer state unknown"

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING", ""])
    def test_gate2_non_terminal_is_silent(self, state):
        # Print visibly started (or degenerate "") — not idle, no log reason.
        v = self._verdict(_state(state))
        assert not v and v.reason == ""

    def test_gate3_dispatch_hold(self):
        v = self._verdict(_state("FINISH"), hold=True)
        assert not v and v.reason == "inside post-dispatch hold"

    def test_gate4_disconnected(self):
        v = self._verdict(_state("IDLE", connected=False))
        assert not v and v.reason == "printer not connected"

    def test_gate5_ams_busy(self):
        v = self._verdict(_state("IDLE", ams_status_main=3))
        assert not v and v.reason == "AMS busy (ams_status_main=3)"

    def test_hold_lookup_short_circuits(self):
        """printer_in_dispatch_hold pops expired holds as a side effect — it
        must only be consulted once gates 1-2 pass, as the original HMS
        requeue short-circuit did."""

        def boom():
            raise AssertionError("hold consulted before gates 1-2 passed")

        assert not evaluate_demonstrably_idle(None, in_dispatch_hold=boom)
        assert not evaluate_demonstrably_idle(_state("RUNNING"), in_dispatch_hold=boom)

    def test_gate_order_hold_before_connected(self):
        # Held AND disconnected must report the hold (gate 3 before gate 4).
        v = self._verdict(_state("IDLE", connected=False), hold=True)
        assert v.reason == "inside post-dispatch hold"

    def test_verdict_defaults(self):
        assert bool(IdleVerdict(True)) is True
        assert IdleVerdict(True).reason == ""


class TestActiveJobStale:
    @pytest.mark.parametrize("state", ["", "unknown", "UNKNOWN"])
    def test_pre_push_defaults_never_stale(self, state):
        # #1679: construction defaults are not evidence.
        assert active_job_stale("task-1", _state(state)) == (False, "")

    @pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED", "idle", "finish"])
    def test_terminal_state_is_stale(self, state):
        stale, reason = active_job_stale("task-1", _state(state))
        assert stale is True and reason == f"printer state {state.upper()}"

    def test_subtask_id_mismatch_is_stale(self):
        stale, reason = active_job_stale("task-1", _state("RUNNING", subtask_id="task-2", subtask_name="x"))
        assert stale is True and "subtask_id changed" in reason

    def test_running_with_empty_subtask_name_is_stale(self):
        stale, reason = active_job_stale("task-1", _state("RUNNING", subtask_id="task-1", subtask_name="  "))
        assert stale is True and reason == "printer subtask_name empty"

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING"])
    def test_matching_running_print_not_stale(self, state):
        assert active_job_stale("task-1", _state(state, subtask_id="task-1", subtask_name="job.3mf")) == (False, "")

    def test_missing_ids_on_either_side_skip_mismatch_check(self):
        # Conservative: no archive subtask_id, or printer hasn't reported one,
        # means the mismatch trigger can't fire — falls through to subtask_name.
        assert active_job_stale(None, _state("RUNNING", subtask_id="task-2", subtask_name="job.3mf")) == (False, "")
        assert active_job_stale("task-1", _state("RUNNING", subtask_id="", subtask_name="job.3mf")) == (False, "")

    def test_none_state_fields_treated_as_empty(self):
        stale, _ = active_job_stale("task-1", SimpleNamespace(state=None, subtask_id=None, subtask_name=None))
        assert stale is False
