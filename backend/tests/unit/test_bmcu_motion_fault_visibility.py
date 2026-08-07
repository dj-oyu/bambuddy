"""A latching motion_fault must be visible without hand-decoding frames.

2026-08-07: item 224 paused twice mid-swap (0700_8006 "unable to feed filament
into the extruder", then 0300_800B "the cutter is stuck"). The BMCU had a
motion_fault latched on channel 1 since 21:31 the previous evening — through
both pauses, and still set afterwards — and none of it reached the journal, the
timeline or the anomaly path. Recovering it meant decoding stored frames by
hand.

Nothing was missing from the registry: `state_field` has named 7 and 8 all
along, and the frontend knows them too. The projection was dropping `field`, so
every state change arrived as one indistinguishable "state change" whose value
was filed under `motion`.

Values below are the ones the device actually sent that night.
"""

from __future__ import annotations

import logging

import pytest

from backend.app.services.bmcu_binary.bmcu_decoder import BMCUEvent, BMCUStatus
from backend.app.services.bmcu_binary.constants import RecordType, StateField
from backend.app.services.bmcu_binary.timeline import anomaly_inputs, timeline_points

SLOT = 1


def state_change(field: int, value: int, previous: int | None = None, severity: int = 4) -> BMCUEvent:
    return BMCUEvent(
        hw_tick32=0,
        record_type=RecordType.STATE_CHANGE,
        severity=severity,
        source=5,  # safety
        payload=b"",
        field=field,
        slot=SLOT,
        previous_value=previous,
        value=value,
    )


# The live pair: set arrives as severity 4 (error), clear as 2 (notice).
MOTION_FAULT_SET = state_change(StateField.MOTION_FAULT, 1, previous=0, severity=4)
MOTION_FAULT_CLEAR = state_change(StateField.MOTION_FAULT, 0, previous=1, severity=2)
SLOT_CHANGE = state_change(StateField.SLOT, 1, previous=255, severity=1)
MOTION_CHANGE = state_change(StateField.MOTION, 2, previous=0, severity=1)


class TestTimelineCarriesTheField:
    def test_state_change_reports_which_field_moved(self):
        (point,) = timeline_points(MOTION_FAULT_SET, 1_000, 0)
        assert point.category == "state_change"
        assert point.field == StateField.MOTION_FAULT
        assert point.slot == SLOT
        assert point.value == 1

    def test_two_different_state_changes_are_distinguishable(self):
        """The whole defect in one assertion: these used to be identical."""
        (fault,) = timeline_points(MOTION_FAULT_SET, 1_000, 0)
        (slot,) = timeline_points(SLOT_CHANGE, 1_000, 0)
        assert fault.category == slot.category == "state_change"
        assert fault.field != slot.field

    def test_non_state_change_events_carry_no_field(self):
        boot = BMCUEvent(hw_tick32=0, record_type=RecordType.BOOT, severity=1, source=0, payload=b"")
        (point,) = timeline_points(boot, 1_000, 0)
        assert point.field is None

    def test_status_points_are_unchanged(self):
        status = BMCUStatus(
            hw_tick32=0,
            tx_drop=0,
            rx_drop=0,
            crc_error=0,
            frame_error=0,
            current_slot=1,
            inserted_mask=0b1111,
            online_mask=0b1010,
            motion=(0, 1, 0, 0),
            pull_pct=(46, 50, 49, 57),
            pressure=0,
            led_mode=0,
            control_error=0,
        )
        points = timeline_points(status, 1_000, 0)
        assert {p.category for p in points} == {"current_slot", "pressure", "motion", "pull_pct"}
        assert all(p.field is None for p in points)


class TestAnomalyPath:
    def test_motion_fault_set_is_its_own_anomaly(self):
        (anomaly,) = anomaly_inputs(MOTION_FAULT_SET, 1_000, 0)
        assert anomaly.kind == "motion_fault"
        assert anomaly.slot == SLOT
        assert anomaly.value == 1

    def test_clearing_is_not_an_anomaly(self):
        """Severity 2 is below the warning bar, and a fault going away is not
        a fault. Kept explicit because the firmware's severities are inverted
        for this pair."""
        assert anomaly_inputs(MOTION_FAULT_CLEAR, 1_000, 0) == ()

    def test_ordinary_warnings_still_report_as_reported_event(self):
        warning = BMCUEvent(hw_tick32=0, record_type=RecordType.SENSOR, severity=3, source=3, payload=b"")
        (anomaly,) = anomaly_inputs(warning, 1_000, 0)
        assert anomaly.kind == "reported_event"

    def test_a_high_severity_slot_change_is_not_mislabelled_a_motion_fault(self):
        (anomaly,) = anomaly_inputs(state_change(StateField.SLOT, 1, severity=4), 1_000, 0)
        assert anomaly.kind == "reported_event"


class TestJournal:
    """`journalctl -u bambuddy` is this deployment's primary source of truth,
    and it was silent about motion_fault entirely."""

    def _log(self, caplog, event):
        from backend.app.services.bmcu_binary.persistence import _log_motion_fault

        with caplog.at_level(logging.INFO, logger="backend.app.services.bmcu_binary.persistence"):
            _log_motion_fault("bmcu-monitor-a", 0, event)
        return caplog.records

    def test_set_logs_a_warning(self, caplog):
        (record,) = self._log(caplog, MOTION_FAULT_SET)
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "motion_fault SET" in message
        assert "slot 1" in message

    def test_clear_logs_at_info(self, caplog):
        (record,) = self._log(caplog, MOTION_FAULT_CLEAR)
        assert record.levelno == logging.INFO
        assert "motion_fault cleared" in record.getMessage()

    @pytest.mark.parametrize("event", [SLOT_CHANGE, MOTION_CHANGE])
    def test_other_state_changes_are_not_logged(self, caplog, event):
        """These arrive thousands of times an hour; logging them would bury the
        fault instead of surfacing it."""
        assert self._log(caplog, event) == []

    def test_non_state_change_events_are_not_logged(self, caplog):
        boot = BMCUEvent(hw_tick32=0, record_type=RecordType.BOOT, severity=1, source=0, payload=b"")
        assert self._log(caplog, boot) == []
