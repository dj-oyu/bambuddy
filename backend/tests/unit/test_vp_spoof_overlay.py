"""Tests for the VP MQTT bridge filament-spoof integration.

Three surfaces are covered:

1. Overlay-in-cache: while a runout-backup spoof is ENGAGED the printer reports
   the backup tray with the spoofed (primary) identity. The bridge rewrites the
   slicer-facing cache back to the tray's real color/brand via the REAL
   ``filament_spoof.apply_spoof_overlay`` (no longer a parallel test stub), then
   strips the non-wire ``_spoof`` metadata before caching. Any overlay error
   must leave the cached state untouched (fail-safe).

2. Snapshot pull on start (Finding #4): a freshly-(re)started bridge seeds its
   ``_active_spoofs`` from the engine's ``get_active_snapshot`` so a VP restart
   during an engaged spoof is correct immediately.

3. Slicer write suppression (Finding #9b): a slicer ``ams_filament_setting``
   targeting a spoof-guarded backup slot is dropped instead of forwarded to the
   real printer; a normal command / unguarded slot still forwards.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import backend.app.services.virtual_printer.mqtt_bridge as mqtt_bridge_mod
from backend.app.services.virtual_printer.mqtt_bridge import MQTTBridge
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer

H2D_SERIAL = "0948BB540200427"
VP_SERIAL = "09400A391800003"

# The real overlay keeps tray_info_idx spoofed but rewrites color + sub_brands
# back to the real values, so pick distinct color values that survive the
# alpha-stripping normalization in filament_spoof._normalize_color.
REAL_COLOR = "00FF00FF"
REAL_BRAND = "PLA Matte"
SPOOF_COLOR = "FF0000FF"
SPOOF_IDX = "GFA00"


def _make_server() -> SimpleMQTTServer:
    return SimpleMQTTServer(
        serial=VP_SERIAL,
        access_code="deadbeef",
        cert_path=Path("/tmp/unused.crt"),  # nosec B108
        key_path=Path("/tmp/unused.key"),  # nosec B108
        model="O1D",
        bind_address="192.168.255.16",
        vp_name="vp1",
    )


def _make_bridge(printer_manager=None) -> MQTTBridge:
    bridge = MQTTBridge(
        vp_id=1,
        vp_name="vp1",
        vp_serial=VP_SERIAL,
        target_printer_id=42,
        mqtt_server=_make_server(),
        printer_manager=printer_manager if printer_manager is not None else MagicMock(),
    )
    # Bypass start()/bind — _on_printer_raw only needs the target serial for
    # the push_status caching path exercised here.
    bridge._target_serial = H2D_SERIAL
    return bridge


def _make_spoof() -> dict:
    return {
        "backup_ams_id": 0,
        "backup_tray_id": 1,
        "primary_ams_id": 0,
        "primary_tray_id": 0,
        "real_tray_color": REAL_COLOR,
        "real_tray_sub_brands": REAL_BRAND,
        "spoof_tray_info_idx": SPOOF_IDX,
        "spoof_tray_color": SPOOF_COLOR,
        "state": "ENGAGED",
    }


def _make_push_status(tray_color: str = SPOOF_COLOR, tray_info_idx: str = SPOOF_IDX) -> bytes:
    """A raw push_status payload whose AMS 0 / tray 1 carries the spoofed identity."""
    payload = {
        "print": {
            "command": "push_status",
            "sequence_id": "1",
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {
                                "id": "0",
                                "tray_info_idx": SPOOF_IDX,
                                "tray_color": SPOOF_COLOR,
                                "tray_sub_brands": "PLA Basic",
                            },
                            {
                                "id": "1",
                                "tray_info_idx": tray_info_idx,
                                "tray_color": tray_color,
                                "tray_sub_brands": "PLA Basic",
                            },
                        ],
                    }
                ],
                "tray_exist_bits": "3",
                "power_on_flag": True,
            },
        }
    }
    return json.dumps(payload).encode()


def _cached_tray(bridge: MQTTBridge, ams_idx: int, tray_idx: int) -> dict:
    state = bridge._latest_print_state
    assert state is not None
    return state["ams"]["ams"][ams_idx]["tray"][tray_idx]


class TestSpoofOverlayInCache:
    """Exercises the REAL filament_spoof overlay through the bridge cache path."""

    def test_engaged_spoof_rewrites_cached_color_and_strips_meta(self):
        bridge = _make_bridge()
        bridge.set_active_spoofs([_make_spoof()])

        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", _make_push_status())

        tray = _cached_tray(bridge, 0, 1)
        # Real overlay: color + sub_brand rewritten, tray_info_idx kept spoofed.
        assert tray["tray_color"] == REAL_COLOR
        assert tray["tray_sub_brands"] == REAL_BRAND
        assert tray["tray_info_idx"] == SPOOF_IDX
        assert "_spoof" not in tray
        # Primary slot (not the backup) is untouched.
        primary = _cached_tray(bridge, 0, 0)
        assert primary["tray_color"] == SPOOF_COLOR
        assert "_spoof" not in primary

    def test_identity_mismatch_leaves_payload_unchanged(self):
        bridge = _make_bridge()
        bridge.set_active_spoofs([_make_spoof()])

        # Live tray no longer carries the spoofed identity (user swapped it).
        bridge._on_printer_raw(
            f"device/{H2D_SERIAL}/report",
            _make_push_status(tray_color="0000FFFF", tray_info_idx="GFB99"),
        )

        tray = _cached_tray(bridge, 0, 1)
        assert tray["tray_color"] == "0000FFFF"
        assert tray["tray_info_idx"] == "GFB99"
        assert "_spoof" not in tray

    def test_overlay_exception_leaves_payload_unchanged(self, monkeypatch):
        # The real overlay never raises, so inject a boom via the module-level
        # binding the bridge now uses to prove the fail-safe try/except.
        def _boom(units, spoofs):
            raise RuntimeError("spoof engine exploded")

        monkeypatch.setattr(mqtt_bridge_mod, "apply_spoof_overlay", _boom)
        bridge = _make_bridge()
        bridge.set_active_spoofs([_make_spoof()])

        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", _make_push_status())

        # Cache still populated, spoofed color intact — fail-safe.
        tray = _cached_tray(bridge, 0, 1)
        assert tray["tray_color"] == SPOOF_COLOR
        assert "_spoof" not in tray

    def test_no_active_spoofs_skips_overlay_entirely(self, monkeypatch):
        called = MagicMock()
        monkeypatch.setattr(mqtt_bridge_mod, "apply_spoof_overlay", called)
        bridge = _make_bridge()

        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", _make_push_status())

        called.assert_not_called()
        tray = _cached_tray(bridge, 0, 1)
        assert tray["tray_color"] == SPOOF_COLOR

    def test_set_active_spoofs_stores_copy(self):
        bridge = _make_bridge()
        spoofs = [_make_spoof()]
        bridge.set_active_spoofs(spoofs)
        spoofs.append({"state": "ENGAGED"})
        assert len(bridge._active_spoofs) == 1


class TestBridgePullsSnapshotOnStart:
    """Finding #4 — a (re)started bridge seeds _active_spoofs from the engine."""

    def _patch_engine_snapshot(self, monkeypatch, snapshot):
        # get_active_snapshot may not exist yet (engine agent adds it in
        # parallel) — setattr creates/overrides it so the test is decoupled
        # from that external dependency.
        import backend.app.services.filament_spoof_engine as eng

        monkeypatch.setattr(eng.filament_spoof_engine, "get_active_snapshot", lambda pid: snapshot, raising=False)
        return eng

    def test_start_pulls_snapshot_from_engine(self, monkeypatch):
        snapshot = [_make_spoof()]
        self._patch_engine_snapshot(monkeypatch, snapshot)

        # printer_manager.get_client → None so _resolve_client early-returns
        # cleanly and start() only exercises the pull + refresh-loop scheduling.
        pm = MagicMock()
        pm.get_client.return_value = None
        bridge = _make_bridge(printer_manager=pm)

        async def _run():
            await bridge.start()
            try:
                assert len(bridge._active_spoofs) == 1
                assert bridge._active_spoofs[0]["backup_tray_id"] == 1
            finally:
                await bridge.stop()

        asyncio.run(_run())

    def test_pull_is_defensive_when_snapshot_raises(self, monkeypatch):
        import backend.app.services.filament_spoof_engine as eng

        def _boom(pid):
            raise RuntimeError("engine not ready")

        monkeypatch.setattr(eng.filament_spoof_engine, "get_active_snapshot", _boom, raising=False)
        bridge = _make_bridge()
        bridge._pull_active_spoofs_from_engine()
        assert bridge._active_spoofs == []

    def test_pull_noop_when_engine_import_absent(self):
        # No engine patch, empty snapshot default: pull leaves spoofs empty and
        # does not raise even if get_active_snapshot is undefined.
        bridge = _make_bridge()
        bridge._active_spoofs = []
        bridge._pull_active_spoofs_from_engine()
        assert isinstance(bridge._active_spoofs, list)


class TestSpoofGuardedForwardSuppression:
    """Finding #9b — slicer ams_filament_setting to a guarded backup slot is dropped."""

    def _server_with_spoof(self):
        server = _make_server()
        bridge = _make_bridge()
        bridge.set_active_spoofs([_make_spoof()])  # guards AMS 0 / tray 1
        server.set_bridge(bridge)
        return server, bridge

    def test_guarded_ams_filament_setting_is_flagged_for_drop(self):
        server, _ = self._server_with_spoof()
        data = {
            "print": {
                "command": "ams_filament_setting",
                "ams_id": 0,
                "tray_id": 1,
                "tray_info_idx": "GFB99",
                "tray_color": "0000FFFF",
            }
        }
        assert server._is_spoof_guarded_write(data) is True

    def test_unguarded_slot_is_forwarded(self):
        server, _ = self._server_with_spoof()
        # Same AMS, a different (unguarded) tray → forward as before.
        data = {"print": {"command": "ams_filament_setting", "ams_id": 0, "tray_id": 0}}
        assert server._is_spoof_guarded_write(data) is False

    def test_non_filament_setting_command_is_forwarded(self):
        server, _ = self._server_with_spoof()
        data = {"print": {"command": "ams_control", "ams_id": 0, "tray_id": 1}}
        assert server._is_spoof_guarded_write(data) is False

    def test_unparseable_target_forwards_failsafe(self):
        server, _ = self._server_with_spoof()
        # Missing tray_id → can't parse target → forward (fail-safe).
        data = {"print": {"command": "ams_filament_setting", "ams_id": 0}}
        assert server._is_spoof_guarded_write(data) is False

    def test_no_spoof_active_forwards(self):
        server = _make_server()
        bridge = _make_bridge()  # no active spoofs
        server.set_bridge(bridge)
        data = {"print": {"command": "ams_filament_setting", "ams_id": 0, "tray_id": 1}}
        assert server._is_spoof_guarded_write(data) is False

    def test_bridge_is_guarded_backup_slot_matches_by_int(self):
        bridge = _make_bridge()
        bridge.set_active_spoofs([_make_spoof()])
        assert bridge.is_guarded_backup_slot(0, 1) is True
        assert bridge.is_guarded_backup_slot(0, 0) is False
        assert bridge.is_guarded_backup_slot(1, 1) is False
