"""Regression tests for write-guard-aware spoof consumers.

Covers the backend integration around the ams_set_filament_setting firmware
write-guard for the filament-runout-backup ("spoof") feature:

  * inventory.apply_spool_to_slot_via_mqtt honors a guarded (False) write:
    skips extrusion_cali_sel + slot-preset persistence, returns False.
  * printers._slot_is_guarded correctly identifies guarded slots.
  * printers.configure_ams_slot returns HTTP 409 when the write is guard-blocked
    and keeps HTTP 500 for a genuine send failure.
  * main._strip_spoof_for_relay removes the internal `_spoof` marker from the
    outgoing relay copy WITHOUT mutating the live printer state.
  * printers.engage_filament_spoof returns 503 when BAMBUDDY_FILAMENT_SPOOF is
    disabled, maps FilamentSpoofError.status through, passes `force`, and
    returns an engine-returned native dict verbatim.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# inventory.apply_spool_to_slot_via_mqtt — guarded write is honored
# ---------------------------------------------------------------------------


def _fake_spool():
    spool = MagicMock()
    spool.id = 42
    spool.material = "PLA"
    spool.brand = "Bambu"
    spool.subtype = "Basic"
    spool.rgba = "FF0000FF"
    spool.nozzle_temp_min = 190
    spool.nozzle_temp_max = 230
    spool.slicer_filament = "GFL99"
    spool.slicer_filament_name = "PLA Basic"
    spool.k_profiles = []  # no stored K-profiles → cali branch is the reset path
    return spool


@pytest.mark.asyncio
async def test_inventory_guarded_write_skips_cali_and_persist(monkeypatch):
    import backend.app.api.routes.inventory as inv_mod
    import backend.app.services.printer_manager as pm_mod
    import backend.app.services.slot_preset_writer as spw_mod

    # Client whose firmware write-guard blocks this slot: returns False.
    client = MagicMock()
    client.ams_set_filament_setting = MagicMock(return_value=False)
    client.extrusion_cali_sel = MagicMock()

    monkeypatch.setattr(pm_mod.printer_manager, "get_client", lambda pid: client)
    monkeypatch.setattr(pm_mod.printer_manager, "get_status", lambda pid: None)

    # Short-circuit the slicer-filament resolver (async) with a fixed result.
    monkeypatch.setattr(
        inv_mod,
        "resolve_slicer_filament",
        AsyncMock(return_value=("GFL99", "GFL99_setting", None)),
    )

    upsert = AsyncMock()
    monkeypatch.setattr(spw_mod, "upsert_slot_preset_for_spool", upsert)

    result = await inv_mod.apply_spool_to_slot_via_mqtt(
        db=MagicMock(),
        current_user=None,
        spool=_fake_spool(),
        printer_id=1,
        ams_id=0,
        tray_id=1,
    )

    assert result is False, "guarded write must report failure"
    client.ams_set_filament_setting.assert_called_once()
    client.extrusion_cali_sel.assert_not_called()
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_successful_write_still_persists(monkeypatch):
    """Sanity guard: a normal (True) write is NOT suppressed."""
    import backend.app.api.routes.inventory as inv_mod
    import backend.app.services.printer_manager as pm_mod
    import backend.app.services.slot_preset_writer as spw_mod

    client = MagicMock()
    client.ams_set_filament_setting = MagicMock(return_value=True)
    client.extrusion_cali_sel = MagicMock()

    monkeypatch.setattr(pm_mod.printer_manager, "get_client", lambda pid: client)
    monkeypatch.setattr(pm_mod.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(
        inv_mod,
        "resolve_slicer_filament",
        AsyncMock(return_value=("GFL99", "GFL99_setting", None)),
    )
    upsert = AsyncMock()
    monkeypatch.setattr(spw_mod, "upsert_slot_preset_for_spool", upsert)

    result = await inv_mod.apply_spool_to_slot_via_mqtt(
        db=MagicMock(),
        current_user=None,
        spool=_fake_spool(),
        printer_id=1,
        ams_id=0,
        tray_id=1,
    )

    assert result is True
    # No K-profile → reset-to-default cali_sel fires, and persistence runs.
    client.extrusion_cali_sel.assert_called_once()
    upsert.assert_awaited_once()


# ---------------------------------------------------------------------------
# printers._slot_is_guarded
# ---------------------------------------------------------------------------


def test_slot_is_guarded_via_closure():
    from backend.app.api.routes.printers import _slot_is_guarded

    client = MagicMock()
    client._spoof_write_guard = lambda ams_id, tray_id: (ams_id, tray_id) == (0, 1)
    client._active_spoofs = []
    assert _slot_is_guarded(client, 0, 1) is True
    assert _slot_is_guarded(client, 0, 2) is False


def test_slot_is_guarded_via_active_spoofs():
    from backend.app.api.routes.printers import _slot_is_guarded

    client = MagicMock()
    client._spoof_write_guard = None
    client._active_spoofs = [{"backup_ams_id": 0, "backup_tray_id": 1}]
    assert _slot_is_guarded(client, 0, 1) is True
    assert _slot_is_guarded(client, 1, 1) is False


def test_slot_is_guarded_defensive_missing_attrs():
    from backend.app.api.routes.printers import _slot_is_guarded

    class Bare:
        pass

    # Neither attr present → not guarded, no exception.
    assert _slot_is_guarded(Bare(), 0, 1) is False

    # Guard closure raising → treated as not guarded.
    client = MagicMock()

    def _boom(a, t):
        raise RuntimeError("boom")

    client._spoof_write_guard = _boom
    client._active_spoofs = None
    assert _slot_is_guarded(client, 0, 1) is False


# ---------------------------------------------------------------------------
# printers.configure_ams_slot — 409 vs 500
# ---------------------------------------------------------------------------


async def _call_configure(client, monkeypatch):
    import backend.app.api.routes.printers as printers_mod

    monkeypatch.setattr(printers_mod.printer_manager, "get_client", lambda pid: client)

    return await printers_mod.configure_ams_slot(
        printer_id=1,
        ams_id=0,
        tray_id=1,
        tray_info_idx="GFL99",  # non-empty → skips state-based resolution
        tray_type="PLA",
        tray_sub_brands="PLA Basic",
        tray_color="FF0000FF",
        nozzle_temp_min=190,
        nozzle_temp_max=230,
        cali_idx=-1,
        nozzle_diameter="0.4",
        setting_id="",
        kprofile_filament_id="",
        kprofile_setting_id="",
        k_value=0.0,
        db=MagicMock(),
        _=None,
    )


@pytest.mark.asyncio
async def test_configure_ams_slot_guarded_returns_409(monkeypatch):
    client = MagicMock()
    client.ams_set_filament_setting = MagicMock(return_value=False)
    client._spoof_write_guard = lambda ams_id, tray_id: True  # guarded
    client._active_spoofs = [{"backup_ams_id": 0, "backup_tray_id": 1}]

    with pytest.raises(HTTPException) as exc:
        await _call_configure(client, monkeypatch)
    assert exc.value.status_code == 409
    assert "runout backup" in exc.value.detail.lower()
    client.extrusion_cali_sel.assert_not_called()


@pytest.mark.asyncio
async def test_configure_ams_slot_genuine_failure_returns_500(monkeypatch):
    client = MagicMock()
    client.ams_set_filament_setting = MagicMock(return_value=False)
    client._spoof_write_guard = None  # not guarded → genuine send failure
    client._active_spoofs = []

    with pytest.raises(HTTPException) as exc:
        await _call_configure(client, monkeypatch)
    assert exc.value.status_code == 500


# ---------------------------------------------------------------------------
# main._strip_spoof_for_relay
# ---------------------------------------------------------------------------


def test_strip_spoof_for_relay_cleans_copy_not_live():
    from backend.app.main import _strip_spoof_for_relay

    live = [
        {
            "id": 0,
            "tray": [
                {"id": 0, "tray_color": "FF0000FF"},
                {"id": 1, "tray_color": "002E96FF", "_spoof": {"primary_ams_id": 0, "primary_tray_id": 0}},
            ],
        }
    ]

    out = _strip_spoof_for_relay(live)

    # Outgoing copy is cleaned.
    assert "_spoof" not in out[0]["tray"][1]
    # Live state is untouched (both the marker and object identity).
    assert live[0]["tray"][1]["_spoof"] == {"primary_ams_id": 0, "primary_tray_id": 0}
    assert out[0] is not live[0]
    assert out[0]["tray"][1] is not live[0]["tray"][1]


def test_strip_spoof_for_relay_passthrough_non_list():
    from backend.app.main import _strip_spoof_for_relay

    assert _strip_spoof_for_relay(None) is None
    assert _strip_spoof_for_relay({"ams": []}) == {"ams": []}


# ---------------------------------------------------------------------------
# printers.engage_filament_spoof — kill switch + status mapping + native dict
# ---------------------------------------------------------------------------


def _spoof_body(force=False):
    from backend.app.api.routes.printers import _FilamentSpoofRequest

    return _FilamentSpoofRequest(
        primary_ams_id=0, primary_tray_id=0,
        backup_ams_id=0, backup_tray_id=1,
        force=force,
    )


def _db_returning_printer():
    """Async DB mock whose execute() resolves to a truthy printer row."""
    db = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = MagicMock()  # printer found
    db.execute = AsyncMock(return_value=exec_result)
    return db


@pytest.mark.asyncio
async def test_engage_returns_503_when_disabled(monkeypatch):
    import backend.app.api.routes.printers as printers_mod

    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "0")

    with pytest.raises(HTTPException) as exc:
        await printers_mod.engage_filament_spoof(
            printer_id=1, body=_spoof_body(), _=None, db=MagicMock()
        )
    assert exc.value.status_code == 503
    assert "disabled" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_engage_maps_error_status_and_passes_force(monkeypatch):
    import backend.app.api.routes.printers as printers_mod
    import backend.app.services.filament_spoof_engine as fse_mod

    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")

    err = fse_mod.FilamentSpoofError("native group locked")
    err.status = 409
    engage_mock = AsyncMock(side_effect=err)
    monkeypatch.setattr(fse_mod.filament_spoof_engine, "engage", engage_mock)

    with pytest.raises(HTTPException) as exc:
        await printers_mod.engage_filament_spoof(
            printer_id=1, body=_spoof_body(force=True), _=None, db=_db_returning_printer()
        )
    assert exc.value.status_code == 409
    # force flag threaded through to the engine.
    assert engage_mock.await_args.kwargs.get("force") is True


@pytest.mark.asyncio
async def test_engage_returns_native_dict_verbatim(monkeypatch):
    import backend.app.api.routes.printers as printers_mod
    import backend.app.services.filament_spoof_engine as fse_mod

    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")

    native = {"native": True, "detail": "handled by firmware group"}
    monkeypatch.setattr(
        fse_mod.filament_spoof_engine, "engage", AsyncMock(return_value=native)
    )

    out = await printers_mod.engage_filament_spoof(
        printer_id=1, body=_spoof_body(), _=None, db=_db_returning_printer()
    )
    assert out == native
