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
