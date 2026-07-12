"""Regression tests for the shared spoof status-field contract.

The badge fields (is_spoofed_backup / spoof_state / spoof_primary) were
initially mapped only in the WebSocket serializer, leaving the REST route —
the one the frontend actually polls — emitting schema defaults. Both
serializers now delegate to filament_spoof.spoof_status_fields /
pending_spoof_map; these tests pin that shared contract, plus the wiring.
"""

from unittest.mock import MagicMock

from backend.app.services.filament_spoof import pending_spoof_map, spoof_status_fields


def test_marker_maps_to_active():
    tray = {"id": 2, "_spoof": {"ams_id": 0, "tray_id": 0, "state": "active"}}
    state, primary = spoof_status_fields(tray, 0, 2, {})
    assert state == "active"
    assert primary == {"ams_id": 0, "tray_id": 0}


def test_pending_snapshot_maps_to_pending():
    pending = {(0, 2): {"ams_id": 0, "tray_id": 0}}
    state, primary = spoof_status_fields({"id": 2}, 0, 2, pending)
    assert state == "pending"
    assert primary == {"ams_id": 0, "tray_id": 0}


def test_no_marker_no_pending_is_none():
    assert spoof_status_fields({"id": 1}, 0, 1, {}) == (None, None)


def test_defensive_on_junk():
    assert spoof_status_fields({"id": "x"}, "y", None, {}) == (None, None)


def test_pending_spoof_map_from_client_snapshot():
    client = MagicMock()
    client._active_spoofs = [
        {"state": "PENDING", "backup_ams_id": 0, "backup_tray_id": 2,
         "primary_ams_id": 0, "primary_tray_id": 0},
        {"state": "ENGAGED", "backup_ams_id": 0, "backup_tray_id": 3,
         "primary_ams_id": 0, "primary_tray_id": 1},
    ]
    m = pending_spoof_map(client)
    assert m == {(0, 2): {"ams_id": 0, "tray_id": 0}}  # ENGAGED excluded
    assert pending_spoof_map(None) == {}


def test_both_serializers_use_the_shared_helper():
    # Wiring pin: both status surfaces must delegate to the shared contract.
    for path in (
        "backend/app/api/routes/printers.py",
        "backend/app/services/printer_manager.py",
    ):
        src = open(path).read()
        assert "spoof_status_fields(" in src, f"{path} must use the shared helper"
