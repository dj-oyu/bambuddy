"""Regression: the firmware-truth map must be fed from the INCOMING AMS
message only — never from the merged (previously overlaid) state.

A partial push omitting the backup tray's color would otherwise poison
_fw_tray_identity with {spoof_idx + real_color}, making _reconcile see
"identity changed" and spuriously release a healthy ENGAGED spoof."""

from unittest.mock import MagicMock

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client():
    c = BambuMQTTClient(ip_address="127.0.0.1", serial_number="TESTSERIAL01", access_code="12345678")
    c._client = MagicMock()
    c.state.connected = True
    return c


def _full_push(color="002E96FF", idx="GFG00"):
    return {"ams": [{"id": "0", "tray": [
        {"id": "2", "tray_info_idx": idx, "tray_color": color, "tray_type": "PETG"},
    ]}]}


def test_fw_identity_not_polluted_by_overlaid_merge(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    c = _client()
    c.set_active_spoofs([{
        "state": "ENGAGED",
        "backup_ams_id": 0, "backup_tray_id": 2,
        "primary_ams_id": 0, "primary_tray_id": 0,
        "spoof_tray_info_idx": "GFG00", "spoof_tray_color": "002E96FF",
        "real_tray_color": "000000FF", "real_tray_sub_brands": "",
    }])

    # Full push: firmware reports the spoofed identity (blue).
    c._handle_ams_data(_full_push())
    assert c._fw_tray_identity[(0, 2)]["tray_color"] == "002E96FF"
    # Overlay rewrote the live view to the real color.
    assert c.state.raw_data["ams"][0]["tray"][0]["tray_color"] == "000000FF"

    # Partial push for the same tray WITHOUT identity fields: the merge
    # inherits the overlaid (real) color, but the fw map must keep BLUE.
    c._handle_ams_data({"ams": [{"id": "0", "tray": [{"id": "2", "remain": 50}]}]})
    assert c._fw_tray_identity[(0, 2)]["tray_color"] == "002E96FF", \
        "fw truth polluted by overlaid merge — would spuriously release the spoof"

    # A genuine identity change in the incoming message DOES update the map.
    c._handle_ams_data(_full_push(color="000000FF"))
    assert c._fw_tray_identity[(0, 2)]["tray_color"] == "000000FF"
