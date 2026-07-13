"""Tests for main._tray_report_is_transient_empty — the restart-unlink guard.

Background (2026-07-13): on every bambuddy restart, _previous_ams_hash resets
so the first AMS report always fires on_ams_change. A BMCU reports blank tray
data (tray_uuid all zeros, empty tray_type, state=3) for a few seconds after
boot, which the auto-unlink fingerprint compare treated as evidence and
DELETEd every slot assignment (Spoolman analog too). The predicate classifies
such reports as "re-detection in progress, not evidence" — fail-safe: keep.
"""

import pytest

from backend.app.main import _tray_report_is_transient_empty


BMCU_BOOT_TRAY = {
    "id": 0,
    "tray_type": "",
    "tray_color": "",
    "tray_uuid": "00000000000000000000000000000000",
    "tag_uid": "0000000000000000",
    "state": 3,  # BMCU always reports 3, even mid-boot
}


class TestTrayReportIsTransientEmpty:
    def test_bmcu_boot_blank_tray_is_transient(self):
        assert _tray_report_is_transient_empty(BMCU_BOOT_TRAY) is True

    def test_missing_state_blank_type_is_transient(self):
        assert _tray_report_is_transient_empty({"tray_type": ""}) is True
        assert _tray_report_is_transient_empty({}) is True

    @pytest.mark.parametrize("state", [9, 10])
    def test_explicit_firmware_empty_state_is_authoritative(self, state):
        tray = dict(BMCU_BOOT_TRAY, state=state)
        assert _tray_report_is_transient_empty(tray) is False

    @pytest.mark.parametrize("state", ["9", "10"])
    def test_explicit_empty_state_as_string(self, state):
        tray = dict(BMCU_BOOT_TRAY, state=state)
        assert _tray_report_is_transient_empty(tray) is False

    def test_populated_tray_is_not_transient(self):
        tray = dict(BMCU_BOOT_TRAY, tray_type="PETG", tray_color="0000FFFF")
        assert _tray_report_is_transient_empty(tray) is False

    def test_type_without_color_is_not_transient(self):
        # parse_ams_tray also rejects this shape, but it carries identity —
        # not the blank boot report the guard targets.
        assert _tray_report_is_transient_empty({"tray_type": "PLA", "state": 3}) is False

    def test_env_toggle_restores_upstream_behaviour(self, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_KEEP_ASSIGNMENTS_ON_EMPTY_TRAY", "0")
        assert _tray_report_is_transient_empty(BMCU_BOOT_TRAY) is False

    def test_unparseable_state_treated_as_unknown(self):
        tray = dict(BMCU_BOOT_TRAY, state="garbage")
        assert _tray_report_is_transient_empty(tray) is True
