"""Unit tests for the deferred-unload patch (_strip_tail_unload).

The machine end G-code of Bambu-sliced files pulls the filament back to the
AMS after every job; the deferred-unload patch strips that block during queue
G-code injection. These tests lock the contract against real A1 mini slicer
output and verify the fail-safe direction (no match → no change).
"""

import zipfile

from backend.app.utils.threemf_tools import _strip_tail_unload, inject_gcode_into_3mf

from .test_gcode_injection import _make_temp_path

# Verbatim from an A1 mini 3mf sliced by Bambu Studio (machine_end_gcode
# dated 20260513), trimmed around the unload block.
A1_MINI_END_GCODE = """\
; MACHINE_END_GCODE_START
M140 S0 ; turn off bed
M106 S0 ; turn off fan
M106 P2 S0 ; turn off remote part cooling fan
M106 P3 S0 ; turn off chamber cooling fan

;G1 X27 F15000 ; wipe

; pull back filament to AMS
M620 S255
G1 X181 F12000
T255
G1 X0 F18000
G1 X-13.0 F3000
G1 X0 F18000 ; wipe
M621 S255

M104 S0 ; turn off hotend
M400 ; wait all motion done
; MACHINE_END_GCODE_END
; EXECUTABLE_BLOCK_END
"""


class TestStripTailUnload:
    def test_strips_real_a1_mini_block(self):
        body = "G28\nG1 X0\n" + A1_MINI_END_GCODE
        new, block = _strip_tail_unload(body)
        assert block is not None
        assert "T255" in block and "M620 S255" in block and "M621 S255" in block
        assert "pull back filament to AMS" not in new
        assert "T255" not in new
        # Surrounding gcode is untouched
        assert "M104 S0 ; turn off hotend" in new
        assert new.startswith("G28\n")

    def test_second_pass_is_noop(self):
        new, _ = _strip_tail_unload("G28\n" + A1_MINI_END_GCODE)
        again, block = _strip_tail_unload(new)
        assert block is None
        assert again == new

    def test_no_end_marker_is_noop(self):
        body = "G28\n; pull back filament to AMS\nM620 S255\nT255\nM621 S255\n"
        new, block = _strip_tail_unload(body)
        assert block is None
        assert new == body

    def test_block_without_t255_is_noop(self):
        body = (
            "; MACHINE_END_GCODE_START\n"
            "; pull back filament to AMS\n"
            "M620 S255\n"
            "M621 S255\n"
        )
        new, block = _strip_tail_unload(body)
        assert block is None
        assert new == body

    def test_missing_block_is_noop(self):
        # e.g. a model/profile whose end gcode has no AMS pull-back
        body = "; MACHINE_END_GCODE_START\nM140 S0\nM104 S0\n"
        new, block = _strip_tail_unload(body)
        assert block is None
        assert new == body

    def test_only_strips_after_end_marker(self):
        # An identical block before the machine-end marker (e.g. inside a
        # filament-change macro) must not be touched.
        change_block = "; pull back filament to AMS\nM620 S255\nT255\nM621 S255\n"
        body = change_block + A1_MINI_END_GCODE
        new, block = _strip_tail_unload(body)
        assert block is not None
        assert new.startswith(change_block)


class TestInjectWithStrip:
    def _make_3mf(self, gcode: str):
        path = _make_temp_path()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", gcode)
            import hashlib

            md5 = hashlib.md5(gcode.encode()).hexdigest().upper()
            zf.writestr("Metadata/plate_1.gcode.md5", md5)
        return path

    def test_strip_only_rewrites_md5(self):
        import hashlib

        src = self._make_3mf("G28\n" + A1_MINI_END_GCODE)
        try:
            result: dict = {}
            out = inject_gcode_into_3mf(src, 1, None, None, strip_tail_unload=result)
            assert out is not None
            assert result.get("block")
            with zipfile.ZipFile(out) as zf:
                data = zf.read("Metadata/plate_1.gcode")
                sidecar = zf.read("Metadata/plate_1.gcode.md5").decode()
            assert b"T255" not in data
            assert hashlib.md5(data).hexdigest().upper() == sidecar
            out.unlink()
        finally:
            src.unlink()

    def test_no_change_returns_none(self):
        # No snippets and nothing to strip → skip the re-zip entirely
        src = self._make_3mf("G28\nM400\n")
        try:
            result: dict = {}
            out = inject_gcode_into_3mf(src, 1, None, None, strip_tail_unload=result)
            assert out is None
            assert "block" not in result
        finally:
            src.unlink()

    def test_default_behavior_unchanged(self):
        # Without the new param, empty snippets still return None (old contract)
        src = self._make_3mf("G28\n" + A1_MINI_END_GCODE)
        try:
            assert inject_gcode_into_3mf(src, 1, None, None) is None
        finally:
            src.unlink()


# ---------------------------------------------------- tri-state defer_unload


class TestDeferUnloadTriState:
    """Explicit per-item defer_unload (private fork UI feature): None=auto
    (follow gcode_injection), True=force strip, False=keep tail unload."""

    @staticmethod
    def _decide(defer_unload, gcode_injection, env="1"):
        # Mirrors the dispatch-time expression in print_scheduler.py.
        requested = defer_unload if defer_unload is not None else bool(gcode_injection)
        return requested and env != "0"

    def test_auto_follows_gcode_injection(self):
        assert self._decide(None, True) is True
        assert self._decide(None, False) is False

    def test_explicit_true_without_injection(self):
        assert self._decide(True, False) is True

    def test_explicit_false_wins_over_injection(self):
        assert self._decide(False, True) is False

    def test_env_kill_switch_always_wins(self):
        assert self._decide(True, True, env="0") is False

    def test_scheduler_source_matches_contract(self):
        import inspect

        from backend.app.services import print_scheduler

        src = inspect.getsource(print_scheduler)
        assert "mode = item.unload_edit" in src
        assert 'os.environ.get("BAMBUDDY_DEFER_TAIL_UNLOAD", "1") != "0"' in src


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_queue_create_roundtrips_defer_unload(async_client, db_session):
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(filename="x.3mf", file_path="archive/x.3mf", file_size=1)
    db_session.add(archive)
    await db_session.commit()

    resp = await async_client.post(
        "/api/v1/queue/",
        json={"archive_id": archive.id, "defer_unload": True, "skip_filament_check": True},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()
    assert item["defer_unload"] is True

    # PATCH pending item to explicit False
    resp = await async_client.patch(f"/api/v1/queue/{item['id']}", json={"defer_unload": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["defer_unload"] is False

    # default is None (auto)
    resp = await async_client.post(
        "/api/v1/queue/", json={"archive_id": archive.id, "skip_filament_check": True}
    )
    assert resp.json()["defer_unload"] is None


@pytest.mark.asyncio
async def test_deferred_unload_state_endpoint(async_client, db_session):
    from backend.app.models.settings import Settings

    resp = await async_client.get("/api/v1/queue/printer/1/deferred-unload-state")
    assert resp.status_code == 200
    assert resp.json() == {"withheld": False, "item_id": None, "trays": None}

    db_session.add(
        Settings(
            key="deferred_unload_state:1",
            value='{"item_id": 42, "ams_mapping": "[-1, -1, 3]", "block": "M620 S255"}',
        )
    )
    await db_session.commit()
    resp = await async_client.get("/api/v1/queue/printer/1/deferred-unload-state")
    body = resp.json()
    assert body["withheld"] is True and body["item_id"] == 42 and body["trays"] == [3]


# ------------------------------------------------- unload_edit 4-mode (fork)


class TestForcedStartUnload:
    """unload_edit="start": the canonical pull-back block is inserted right
    before the start swap (M620 M), guaranteeing an unload at this job's
    start even when the printer's tray state is desynced."""

    START = "; EXECUTABLE_BLOCK_START\nG28 X\nG1 X0.0 F30000\nM620 M ;enable remap\nM620 S3A   ; switch material if AMS exist\n    T3\nM621 S3A\nG1 Z5\n"

    def test_injects_before_remap_enable(self):
        from backend.app.utils.threemf_tools import _FORCED_START_UNLOAD_BLOCK, _inject_start_unload

        out, injected = _inject_start_unload(self.START)
        assert injected is True
        assert out.index(_FORCED_START_UNLOAD_BLOCK) < out.index("M620 M")
        # pull-back precedes the swap load
        assert out.index("M620 S255") < out.index("M620 S3A")
        assert "T255" in out

    def test_no_swap_is_noop(self):
        from backend.app.utils.threemf_tools import _inject_start_unload

        out, injected = _inject_start_unload("; EXECUTABLE_BLOCK_START\nG28\nG1 X0\n")
        assert injected is False and "M620 S255" not in out

    def test_falls_back_to_plain_swap_line(self):
        from backend.app.utils.threemf_tools import _inject_start_unload

        gc = "; EXECUTABLE_BLOCK_START\nG28 X\nM620 S2A\nT2\nM621 S2A\n"
        out, injected = _inject_start_unload(gc)
        assert injected is True
        assert out.index("M620 S255") < out.index("M620 S2A")


class TestUnloadEditModeMapping:
    """Dispatch-time mapping of unload_edit -> (tail strip, forced start)."""

    @staticmethod
    def _decide(unload_edit, defer_unload, gcode_injection, env="1"):
        mode = unload_edit
        if mode is None:
            if defer_unload is True:
                mode = "auto" if gcode_injection else "start-less-defer"
            elif defer_unload is False:
                mode = "end"
            else:
                mode = "auto"
        env_ok = env != "0"
        if mode in ("none", "end"):
            return False, False
        if mode == "start":
            return bool(gcode_injection) and env_ok, True
        if mode == "start-less-defer":
            return env_ok, False
        return bool(gcode_injection) and env_ok, False

    def test_none_touches_nothing(self):
        assert self._decide("none", None, True) == (False, False)

    def test_end_keeps_tail(self):
        assert self._decide("end", True, True) == (False, False)

    def test_start_forces_pullback_tail_auto(self):
        assert self._decide("start", None, True) == (True, True)
        assert self._decide("start", None, False) == (False, True)

    def test_auto_legacy(self):
        assert self._decide("auto", None, True) == (True, False)
        assert self._decide(None, None, False) == (False, False)
        assert self._decide(None, True, False) == (True, False)
        assert self._decide(None, False, True) == (False, False)

    def test_env_kill_switch(self):
        assert self._decide("start", None, True, env="0") == (False, True)


@pytest.mark.asyncio
async def test_queue_create_roundtrips_unload_edit(async_client, db_session):
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(filename="y.3mf", file_path="archive/y.3mf", file_size=1)
    db_session.add(archive)
    await db_session.commit()
    resp = await async_client.post(
        "/api/v1/queue/",
        json={"archive_id": archive.id, "unload_edit": "start", "skip_filament_check": True},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()
    assert item["unload_edit"] == "start"
    resp = await async_client.patch(f"/api/v1/queue/{item['id']}", json={"unload_edit": "none"})
    assert resp.json()["unload_edit"] == "none"
    resp = await async_client.post(
        "/api/v1/queue/", json={"archive_id": archive.id, "unload_edit": "bogus"}
    )
    assert resp.status_code == 422
