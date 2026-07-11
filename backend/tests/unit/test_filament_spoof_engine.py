"""Unit tests for FilamentSpoofEngine (engage / release / runout / revalidate /
confirmation lifecycle).

The engine reads firmware TRUTH via ``client.get_fw_tray_identity`` (never the
overlaid ``raw_data``). The fake client below exposes that from its raw_data,
which — with no overlay running in the test — equals firmware truth.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401  (register all tables on Base.metadata)
from backend.app.core.database import Base
from backend.app.models.filament_spoof import FilamentSpoof
from backend.app.services import filament_spoof_engine as fse_mod
from backend.app.services.filament_spoof_engine import FilamentSpoofEngine, FilamentSpoofError


@pytest.fixture(autouse=True)
def _enable_feature(monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "1")


@pytest.fixture
async def session_maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(fse_mod, "async_session", maker)
    yield maker
    await engine.dispose()


def _fake_client(*, backup_type="PLA", primary_type="PLA", backup_present=True,
                 tray_now=255, backup_enabled=False, extra_trays=None, extruder_map=None):
    trays0 = [
        {"id": 0, "tray_info_idx": "GFA00", "tray_type": primary_type, "tray_sub_brands": "PLA Basic",
         "tray_color": "FF0000FF", "nozzle_temp_min": 190, "nozzle_temp_max": 230, "cali_idx": 3, "k": 0.02},
    ]
    if backup_present:
        trays0.append(
            {"id": 1, "tray_info_idx": "GFB11", "tray_type": backup_type, "tray_sub_brands": "PLA Matte",
             "tray_color": "002E96FF", "nozzle_temp_min": 195, "nozzle_temp_max": 235, "cali_idx": 7, "k": 0.025},
        )
    for t in (extra_trays or []):
        trays0.append(t)
    client = MagicMock()
    client.state.connected = True
    client.state.tray_now = tray_now
    client.state.state = "IDLE"
    client.state.ams_filament_backup = backup_enabled
    client.state.ams_extruder_map = extruder_map or {}
    client.state.raw_data = {"ams": [{"id": 0, "tray": trays0}]}

    def _fw(ams_id, tray_id):
        for unit in client.state.raw_data.get("ams", []):
            if int(unit.get("id")) != int(ams_id):
                continue
            for tr in unit.get("tray", []):
                if int(tr.get("id")) == int(tray_id):
                    return {"tray_info_idx": tr.get("tray_info_idx"), "tray_color": tr.get("tray_color")}
        return None

    client.get_fw_tray_identity = MagicMock(side_effect=_fw)
    client.ams_set_filament_setting = MagicMock(return_value=True)
    client.set_ams_filament_backup = MagicMock(return_value=True)
    client.extrusion_cali_sel = MagicMock(return_value=True)
    return client


def _engine_with(client):
    eng = FilamentSpoofEngine()
    pm = MagicMock()
    pm.get_client.return_value = client
    pm._schedule_async = MagicMock()
    eng._printer_manager = pm
    return eng


async def _rows(maker, printer_id=1):
    async with maker() as db:
        res = await db.execute(select(FilamentSpoof).where(FilamentSpoof.printer_id == printer_id))
        return list(res.scalars().all())


# ---- engage ------------------------------------------------------------

@pytest.mark.asyncio
async def test_engage_creates_pending_row(session_maker):
    client = _fake_client()
    eng = _engine_with(client)

    row = await eng.engage(1, (0, 0), (0, 1))

    assert row.state == "PENDING"  # not confirmed until firmware echoes
    call = client.ams_set_filament_setting.call_args
    assert call.args[0] == 0 and call.args[1] == 1
    assert call.args[2] == "GFA00"
    assert call.kwargs["bypass_spoof_guard"] is True
    client.set_ams_filament_backup.assert_called_once_with(True)
    # Rule C: re-asserted the backup slot's real K profile.
    client.extrusion_cali_sel.assert_called()
    assert row.real_tray_color == "002E96FF"
    assert row.real_cali_idx == "7"
    assert row.spoof_tray_color == "FF0000FF"
    assert row.spoof_tray_info_idx == "GFA00"
    assert len(await _rows(session_maker)) == 1
    client.set_active_spoofs.assert_called()


@pytest.mark.asyncio
async def test_engage_type_mismatch_no_writes(session_maker):
    client = _fake_client(backup_type="PETG")
    eng = _engine_with(client)
    with pytest.raises(FilamentSpoofError):
        await eng.engage(1, (0, 0), (0, 1))
    client.ams_set_filament_setting.assert_not_called()
    assert await _rows(session_maker) == []


@pytest.mark.asyncio
async def test_engage_disabled_raises_503(session_maker, monkeypatch):
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "0")
    client = _fake_client()
    eng = _engine_with(client)
    with pytest.raises(FilamentSpoofError) as ei:
        await eng.engage(1, (0, 0), (0, 1))
    assert ei.value.status == 503
    client.ams_set_filament_setting.assert_not_called()


@pytest.mark.asyncio
async def test_engage_extruder_mismatch_rejected(session_maker):
    # AMS 0 tray0 primary; add AMS 1 tray0 backup on a different extruder.
    client = _fake_client(extruder_map={"0": 0, "1": 1})
    client.state.raw_data["ams"].append({"id": 1, "tray": [
        {"id": 0, "tray_info_idx": "GFB11", "tray_type": "PLA", "tray_sub_brands": "PLA Matte",
         "tray_color": "002E96FF", "nozzle_temp_min": 195, "nozzle_temp_max": 235},
    ]})
    eng = _engine_with(client)
    with pytest.raises(FilamentSpoofError) as ei:
        await eng.engage(1, (0, 0), (1, 0))
    assert ei.value.status == 409
    client.ams_set_filament_setting.assert_not_called()


@pytest.mark.asyncio
async def test_engage_native_identical_short_circuit(session_maker):
    # Backup already has the primary's exact firmware identity.
    client = _fake_client()
    b = client.state.raw_data["ams"][0]["tray"][1]
    b["tray_info_idx"] = "GFA00"
    b["tray_color"] = "FF0000FF"
    eng = _engine_with(client)
    res = await eng.engage(1, (0, 0), (0, 1))
    assert isinstance(res, dict) and res.get("native") is True
    client.ams_set_filament_setting.assert_not_called()
    client.set_ams_filament_backup.assert_called_once_with(True)
    assert await _rows(session_maker) == []


@pytest.mark.asyncio
async def test_engage_backup_native_group_lock_needs_force(session_maker):
    # A third slot shares the backup's firmware identity → native partner.
    client = _fake_client(extra_trays=[
        {"id": 2, "tray_info_idx": "GFB11", "tray_type": "PLA", "tray_sub_brands": "PLA Matte",
         "tray_color": "002E96FF", "nozzle_temp_min": 195, "nozzle_temp_max": 235},
    ])
    eng = _engine_with(client)
    with pytest.raises(FilamentSpoofError) as ei:
        await eng.engage(1, (0, 0), (0, 1))
    assert ei.value.status == 409
    client.ams_set_filament_setting.assert_not_called()
    # force overrides (printer idle).
    row = await eng.engage(1, (0, 0), (0, 1), force=True)
    assert row.state == "PENDING"


@pytest.mark.asyncio
async def test_engage_force_refused_while_running(session_maker):
    client = _fake_client(extra_trays=[
        {"id": 2, "tray_info_idx": "GFB11", "tray_type": "PLA", "tray_sub_brands": "PLA Matte",
         "tray_color": "002E96FF", "nozzle_temp_min": 195, "nozzle_temp_max": 235},
    ])
    client.state.state = "RUNNING"
    eng = _engine_with(client)
    with pytest.raises(FilamentSpoofError) as ei:
        await eng.engage(1, (0, 0), (0, 1), force=True)
    assert ei.value.status == 409
    client.ams_set_filament_setting.assert_not_called()


@pytest.mark.asyncio
async def test_engage_refuses_chain(session_maker):
    # Existing spoof on the backup slot → engaging it again as primary chains.
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))  # backup=(0,1)
    client.ams_set_filament_setting.reset_mock()
    with pytest.raises(FilamentSpoofError) as ei:
        await eng.engage(1, (0, 1), (0, 0))  # primary=(0,1) already spoofed
    assert ei.value.status == 409
    client.ams_set_filament_setting.assert_not_called()


@pytest.mark.asyncio
async def test_engage_backup_is_active_rejected(session_maker):
    client = _fake_client(tray_now=1)
    eng = _engine_with(client)
    with pytest.raises(FilamentSpoofError):
        await eng.engage(1, (0, 0), (0, 1))
    client.ams_set_filament_setting.assert_not_called()


# ---- confirmation lifecycle -------------------------------------------

@pytest.mark.asyncio
async def test_pending_confirms_on_firmware_echo(session_maker):
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    # Firmware now reports the spoofed identity on the backup slot.
    b = client.state.raw_data["ams"][0]["tray"][1]
    b["tray_info_idx"] = "GFA00"
    b["tray_color"] = "FF0000FF"
    await eng._reconcile(1)
    rows = await _rows(session_maker)
    assert rows[0].state == "ENGAGED"
    assert rows[0].confirmed_at is not None


@pytest.mark.asyncio
async def test_pending_fails_on_timeout(session_maker, monkeypatch):
    monkeypatch.setenv("BAMBUDDY_SPOOF_CONFIRM_TIMEOUT_S", "1")
    client = _fake_client()
    eng = _engine_with(client)
    row = await eng.engage(1, (0, 0), (0, 1))
    # Backdate engaged_at beyond the timeout; firmware never echoed.
    async with session_maker() as db:
        r = await db.get(FilamentSpoof, row.id)
        r.engaged_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await db.commit()
    await eng._reconcile(1)
    rows = await _rows(session_maker)
    assert rows[0].state == "FAILED"


# ---- release -----------------------------------------------------------

@pytest.mark.asyncio
async def test_release_restores_identity(session_maker):
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    client.ams_set_filament_setting.reset_mock()
    # Firmware still shows the spoofed identity.
    b = client.state.raw_data["ams"][0]["tray"][1]
    b["tray_info_idx"] = "GFA00"
    b["tray_color"] = "FF0000FF"
    row = await eng.release(1, (0, 1), restore=True)
    assert row.state == "RELEASED"
    call = client.ams_set_filament_setting.call_args
    assert call.args[2] == "GFB11"  # real tray_info_idx
    assert call.args[5] == "002E96FF"  # real color
    assert call.kwargs["bypass_spoof_guard"] is True


@pytest.mark.asyncio
async def test_release_works_when_disabled(session_maker, monkeypatch):
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    monkeypatch.setenv("BAMBUDDY_FILAMENT_SPOOF", "0")
    row = await eng.release(1, (0, 1), restore=False)
    assert row is not None and row.state == "RELEASED"


# ---- runout ------------------------------------------------------------

@pytest.mark.asyncio
async def test_runout_release_only_on_genuine_switch(session_maker):
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    client.ams_set_filament_setting.reset_mock()

    # Backup (global 1) became active but prev was NOT the primary (global 0) or
    # not RUNNING → keep ENGAGED.
    await eng._handle_runout(1, 0, 1, prev_global=5, gcode_state="RUNNING")
    assert (await _rows(session_maker))[0].state == "PENDING"
    await eng._handle_runout(1, 0, 1, prev_global=0, gcode_state="IDLE")
    assert (await _rows(session_maker))[0].state == "PENDING"

    # Genuine runout: prev == primary (global 0) AND RUNNING.
    await eng._handle_runout(1, 0, 1, prev_global=0, gcode_state="RUNNING")
    assert (await _rows(session_maker))[0].state == "RELEASED"
    client.ams_set_filament_setting.assert_not_called()


# ---- revalidate --------------------------------------------------------

@pytest.mark.asyncio
async def test_revalidate_keeps_row_when_no_firmware_data(session_maker):
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    # Simulate no AMS data / disconnected: fw identity unknown.
    client.get_fw_tray_identity = MagicMock(return_value=None)
    changed = await eng.revalidate(1)
    assert changed == 0
    assert (await _rows(session_maker))[0].state == "PENDING"


@pytest.mark.asyncio
async def test_revalidate_drops_on_positive_evidence(session_maker):
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    # Confirm first, then user physically changed the spool.
    b = client.state.raw_data["ams"][0]["tray"][1]
    b["tray_info_idx"] = "GFA00"
    b["tray_color"] = "FF0000FF"
    await eng._reconcile(1)
    assert (await _rows(session_maker))[0].state == "ENGAGED"
    b["tray_info_idx"] = "GFZ99"
    b["tray_color"] = "00FF00FF"
    changed = await eng.revalidate(1)
    assert changed == 1
    assert (await _rows(session_maker))[0].state == "RELEASED"


# ---- BMCU write acceptance (setting_id) + confirmation timer ------------

@pytest.mark.asyncio
async def test_engage_write_includes_setting_id(session_maker):
    # BMCU silently drops ams_filament_setting without a setting_id; engage
    # must derive it from the spoofed preset (GFA00 -> GFSA00).
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    call = client.ams_set_filament_setting.call_args
    assert call.kwargs["setting_id"] == "GFSA00"


@pytest.mark.asyncio
async def test_engage_requests_prompt_firmware_echo(session_maker):
    client = _fake_client()
    client.request_status_update = MagicMock(return_value=True)
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    client.request_status_update.assert_called_once()


@pytest.mark.asyncio
async def test_release_restore_includes_setting_id(session_maker):
    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    # Firmware echoes the spoofed identity so restore's fail-safe passes.
    b = client.state.raw_data["ams"][0]["tray"][1]
    b["tray_info_idx"] = "GFA00"
    b["tray_color"] = "FF0000FF"
    await eng._reconcile(1)
    client.ams_set_filament_setting.reset_mock()
    await eng.release(1, (0, 1), restore=True)
    call = client.ams_set_filament_setting.call_args
    assert call is not None
    assert call.kwargs["setting_id"] == "GFSB11"  # real preset GFB11


@pytest.mark.asyncio
async def test_pending_timeout_fires_without_ams_messages(session_maker, monkeypatch):
    # Regression: PENDING must resolve via the deferred timer even when the
    # printer pushes no AMS updates (observed stuck-PENDING on idle BMCU).
    monkeypatch.setenv("BAMBUDDY_SPOOF_CONFIRM_TIMEOUT_S", "0")
    client = _fake_client()
    eng = _engine_with(client)

    delays = []
    orig = eng._schedule_deferred_reconcile

    def _capture(pid, delay_s):
        delays.append(delay_s)
        orig(pid, 0)  # run immediately in the test loop

    eng._schedule_deferred_reconcile = _capture
    await eng.engage(1, (0, 0), (0, 1))
    assert delays, "engage must schedule a deferred reconcile"
    await asyncio.sleep(0.05)  # let the deferred task run
    rows = await _rows(session_maker)
    assert rows[0].state == "FAILED"


@pytest.mark.asyncio
async def test_engage_uses_versioned_setting_id_from_slot_preset(session_maker):
    # BMCU drops writes whose setting_id lacks the version suffix; when the
    # primary slot has a persisted cloud preset mapping, use it verbatim.
    from backend.app.models.slot_preset import SlotPresetMapping

    async with session_maker() as db:
        db.add(SlotPresetMapping(
            printer_id=1, ams_id=0, tray_id=0,
            preset_id="GFSA00_05", preset_name="PLA Basic", preset_source="cloud",
        ))
        await db.commit()

    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    call = client.ams_set_filament_setting.call_args
    assert call.kwargs["setting_id"] == "GFSA00_05"


@pytest.mark.asyncio
async def test_engage_ignores_mismatched_slot_preset(session_maker):
    # A stale mapping for a DIFFERENT preset must not leak into the write.
    from backend.app.models.slot_preset import SlotPresetMapping

    async with session_maker() as db:
        db.add(SlotPresetMapping(
            printer_id=1, ams_id=0, tray_id=0,
            preset_id="GFSZ99_07", preset_name="Other", preset_source="cloud",
        ))
        await db.commit()

    client = _fake_client()
    eng = _engine_with(client)
    await eng.engage(1, (0, 0), (0, 1))
    call = client.ams_set_filament_setting.call_args
    assert call.kwargs["setting_id"] == "GFSA00"  # fallback derivation
