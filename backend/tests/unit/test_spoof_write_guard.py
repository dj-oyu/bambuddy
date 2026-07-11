"""Unit tests for the ams_set_filament_setting spoof write-guard."""

from unittest.mock import MagicMock

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client():
    c = BambuMQTTClient(ip_address="127.0.0.1", serial_number="TESTSERIAL01", access_code="12345678")
    c._client = MagicMock()
    c.state.connected = True
    return c


def test_guarded_slot_noops():
    c = _client()
    # Guard protects AMS 0 tray 1.
    c._spoof_write_guard = lambda ams_id, tray_id: (ams_id, tray_id) == (0, 1)

    ok = c.ams_set_filament_setting(0, 1, "GFA00", "PLA", "PLA Basic", "FF0000FF", 190, 230)
    assert ok is False
    c._client.publish.assert_not_called()

    # A different slot is not guarded → publishes.
    ok2 = c.ams_set_filament_setting(0, 2, "GFA00", "PLA", "PLA Basic", "FF0000FF", 190, 230)
    assert ok2 is True
    c._client.publish.assert_called_once()


def test_bypass_flag_writes_through_guard():
    c = _client()
    c._spoof_write_guard = lambda ams_id, tray_id: True  # guard everything

    ok = c.ams_set_filament_setting(
        0, 1, "GFA00", "PLA", "PLA Basic", "FF0000FF", 190, 230, bypass_spoof_guard=True
    )
    assert ok is True
    c._client.publish.assert_called_once()


def test_no_guard_installed_writes_normally():
    c = _client()
    assert c._spoof_write_guard is None
    ok = c.ams_set_filament_setting(0, 1, "GFA00", "PLA", "PLA Basic", "FF0000FF", 190, 230)
    assert ok is True
    c._client.publish.assert_called_once()


def test_broken_guard_fails_safe_and_allows_write():
    c = _client()

    def _boom(ams_id, tray_id):
        raise RuntimeError("guard blew up")

    c._spoof_write_guard = _boom
    ok = c.ams_set_filament_setting(0, 1, "GFA00", "PLA", "PLA Basic", "FF0000FF", 190, 230)
    assert ok is True
    c._client.publish.assert_called_once()
