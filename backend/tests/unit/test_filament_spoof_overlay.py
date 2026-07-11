"""Unit tests for the pure filament-spoof overlay (backend.app.services.filament_spoof)."""

import backend.app.services.filament_spoof as fs
from backend.app.services.filament_spoof import apply_spoof_overlay, strip_spoof_meta


def _units():
    """A minimal ams unit list with one backup slot that firmware reports as spoofed."""
    return [
        {
            "id": 0,
            "tray": [
                {"id": 0, "tray_info_idx": "GFA00", "tray_color": "FF0000FF", "tray_sub_brands": "PLA Basic"},
                # Backup slot: firmware shows the PRIMARY's identity (spoofed).
                {"id": 1, "tray_info_idx": "GFA00", "tray_color": "FF0000FF", "tray_sub_brands": "PLA Basic"},
            ],
        }
    ]


def _spoof(**overrides):
    base = {
        "backup_ams_id": 0,
        "backup_tray_id": 1,
        "primary_ams_id": 0,
        "primary_tray_id": 0,
        "real_tray_color": "002E96FF",
        "real_tray_sub_brands": "PLA Matte",
        "spoof_tray_info_idx": "GFA00",
        "spoof_tray_color": "FF0000FF",
        "state": "ENGAGED",
    }
    base.update(overrides)
    return base


def test_rewrite_on_match(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    n = apply_spoof_overlay(units, [_spoof()])
    assert n == 1
    backup = units[0]["tray"][1]
    # Real color/sub-brand overlaid; tray_info_idx stays spoofed.
    assert backup["tray_color"] == "002E96FF"
    assert backup["tray_sub_brands"] == "PLA Matte"
    assert backup["tray_info_idx"] == "GFA00"
    assert backup["_spoof"] == {"ams_id": 0, "tray_id": 0, "state": "active"}
    # Non-backup slot untouched.
    assert units[0]["tray"][0]["tray_color"] == "FF0000FF"
    assert "_spoof" not in units[0]["tray"][0]


def test_failsafe_color_mismatch(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    units[0]["tray"][1]["tray_color"] = "00FF00FF"  # user swapped spool
    n = apply_spoof_overlay(units, [_spoof()])
    assert n == 0
    assert "_spoof" not in units[0]["tray"][1]


def test_failsafe_preset_mismatch(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    units[0]["tray"][1]["tray_info_idx"] = "GFB99"  # different preset
    n = apply_spoof_overlay(units, [_spoof()])
    assert n == 0


def test_alpha_channel_normalization(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    # Firmware reports 6-char while spoof stored 8-char (or vice versa).
    units[0]["tray"][1]["tray_color"] = "ff0000"  # lowercase, no alpha
    n = apply_spoof_overlay(units, [_spoof(spoof_tray_color="FF0000FF")])
    assert n == 1


def test_disabled_env_identity(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "0")
    units = _units()
    before = str(units)
    n = apply_spoof_overlay(units, [_spoof()])
    assert n == 0
    assert str(units) == before


def test_idempotent_double_apply(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    assert apply_spoof_overlay(units, [_spoof()]) == 1
    # Second pass: live color is now the real color, no longer matches spoof key.
    assert apply_spoof_overlay(units, [_spoof()]) == 0
    assert units[0]["tray"][1]["tray_color"] == "002E96FF"


def test_empty_spoofs_noop(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    assert apply_spoof_overlay(units, []) == 0


def test_malformed_tray_does_not_raise(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = [
        {"id": 0, "tray": [None, "garbage", {"id": 1}]},
        "not-a-dict",
        None,
    ]
    # Missing/None fields must never raise.
    assert apply_spoof_overlay(units, [_spoof()]) == 0
    assert apply_spoof_overlay(None, [_spoof()]) == 0
    assert apply_spoof_overlay(units, [None, {"backup_ams_id": None}]) == 0


def test_strip_spoof_meta():
    units = _units()
    units[0]["tray"][1]["_spoof"] = {"primary_ams_id": 0, "primary_tray_id": 0}
    strip_spoof_meta(units)
    assert "_spoof" not in units[0]["tray"][1]
    # Idempotent + defensive on junk.
    strip_spoof_meta(units)
    strip_spoof_meta(None)
    strip_spoof_meta(["junk", {"tray": [None]}])


def test_stale_marker_removed_when_no_longer_spoofed(monkeypatch):
    # Finding #11: a slot that still carries a marker but is no longer overlaid
    # this pass must have the marker stripped (badge/menu ghost after release).
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    units[0]["tray"][1]["_spoof"] = {"ams_id": 0, "tray_id": 0, "state": "active"}
    units[0]["tray"][1]["tray_color"] = "00FF00FF"  # user swapped: no longer matches
    n = apply_spoof_overlay(units, [_spoof()])
    assert n == 0
    assert "_spoof" not in units[0]["tray"][1]


def test_stale_marker_removed_with_empty_spoofs(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    units[0]["tray"][1]["_spoof"] = {"ams_id": 0, "tray_id": 0, "state": "active"}
    assert apply_spoof_overlay(units, []) == 0
    assert "_spoof" not in units[0]["tray"][1]


def test_pending_overlay_noop_until_firmware_echo(monkeypatch):
    # A PENDING spoof whose backup slot still shows its REAL identity (firmware
    # hasn't echoed the write yet) must NOT be overlaid, and carry no marker.
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")
    units = _units()
    units[0]["tray"][1]["tray_info_idx"] = "GFB11"   # still the backup's real id
    units[0]["tray"][1]["tray_color"] = "002E96FF"
    n = apply_spoof_overlay(units, [_spoof(state="PENDING")])
    assert n == 0
    assert "_spoof" not in units[0]["tray"][1]


def test_normalize_color_helper():
    assert fs._normalize_color("002e96ff") == "002E96"
    assert fs._normalize_color(" 002E96 ") == "002E96"
    assert fs._normalize_color("002E96") == "002E96"
    assert fs._normalize_color(None) is None
    assert fs._normalize_color("") is None
