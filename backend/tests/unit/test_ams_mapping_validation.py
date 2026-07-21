"""Tests for stored-AMS-mapping validation (2026-07-19 wrong-colour incident).

A queue item created with an ams_mapping copied from a different job (e.g.
``[-1, 0]`` for a file that only uses slot 1) used to dispatch verbatim and
print on the wrong tray. Two guards now exist:

- ``validate_mapping_against_requirements`` — structural, shared by the API
  boundary (422 with machine-readable ``problems``) and the scheduler;
- ``PrintScheduler._validate_stored_ams_mapping`` — adds loaded-tray type and
  colour checks at dispatch time.

Both must fail safe: "cannot validate" (no reqs / no mapping) is never an
error, and every reported problem is a machine-readable dict so automated
recovery can act on the exact cause.
"""

import pytest

from backend.app.services.filament_requirements import validate_mapping_against_requirements
from backend.app.services.print_scheduler import PrintScheduler


def req(slot_id, ftype="PETG", color="#000000"):
    return {"slot_id": slot_id, "type": ftype, "color": color, "tray_info_idx": "", "used_grams": 10.0}


def loaded(tray, ftype="PETG", color="#000000"):
    return {
        "type": ftype,
        "color": color,
        "tray_info_idx": "",
        "ams_id": 0,
        "tray_id": tray,
        "is_ht": False,
        "is_external": False,
        "global_tray_id": tray,
        "extruder_id": None,
        "remain": -1,
    }


class TestStructuralValidation:
    def test_valid_single_slot(self):
        assert validate_mapping_against_requirements([1], [req(1)]) == []

    def test_valid_sparse_mapping(self):
        assert validate_mapping_against_requirements([-1, 0], [req(2, color="#FF0000")]) == []

    def test_incident_shape_stale_mapping(self):
        """[-1, 0] against a slot-1-only file: the exact 2026-07-19 incident."""
        problems = validate_mapping_against_requirements([-1, 0], [req(1)])
        issues = {p["issue"] for p in problems}
        assert "used_slot_unmapped" in issues
        assert "unused_slot_mapped" in issues
        unmapped = next(p for p in problems if p["issue"] == "used_slot_unmapped")
        assert unmapped["slot_id"] == 1
        assert unmapped["color"] == "#000000"

    def test_mapping_too_short(self):
        problems = validate_mapping_against_requirements([5], [req(1), req(3)])
        assert problems == [{"issue": "mapping_too_short", "expected_len": 3, "actual_len": 1}]

    def test_no_requirements_is_not_an_error(self):
        assert validate_mapping_against_requirements([-1, 0], []) == []

    def test_no_mapping_is_not_an_error(self):
        assert validate_mapping_against_requirements([], [req(1)]) == []

    def test_problems_are_json_serializable(self):
        import json

        problems = validate_mapping_against_requirements([-1, 0], [req(1)])
        json.dumps(problems)  # must not raise


class TestSchedulerTrayValidation:
    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    def test_valid_mapping_passes(self, scheduler):
        assert scheduler._validate_stored_ams_mapping([1], [req(1)], [loaded(0, color="#FF0000"), loaded(1)]) == []

    def test_mapped_tray_not_loaded(self, scheduler):
        problems = scheduler._validate_stored_ams_mapping([2], [req(1)], [loaded(0), loaded(1)])
        assert problems == [{"issue": "mapped_tray_not_loaded", "slot_id": 1, "tray": 2}]

    def test_type_mismatch(self, scheduler):
        problems = scheduler._validate_stored_ams_mapping([0], [req(1, ftype="PETG")], [loaded(0, ftype="PLA")])
        assert problems[0]["issue"] == "mapped_tray_type_mismatch"
        assert problems[0]["required_type"] == "PETG"
        assert problems[0]["loaded_type"] == "PLA"

    def test_color_mismatch_black_on_red_tray(self, scheduler):
        """Black part mapped to the red tray — what actually printed on 07-19."""
        problems = scheduler._validate_stored_ams_mapping(
            [0], [req(1, color="#000000")], [loaded(0, color="#FF0000"), loaded(1, color="#000000")]
        )
        assert problems == [
            {
                "issue": "mapped_tray_color_mismatch",
                "slot_id": 1,
                "tray": 0,
                "required_color": "#000000",
                "loaded_color": "#FF0000",
            }
        ]

    def test_similar_color_passes(self, scheduler):
        """Within the 40/channel similarity threshold — no false positive."""
        assert scheduler._validate_stored_ams_mapping([0], [req(1, color="#000000")], [loaded(0, color="#101010")]) == []

    def test_structural_problem_short_circuits_tray_checks(self, scheduler):
        problems = scheduler._validate_stored_ams_mapping([9], [req(1), req(2)], [loaded(0)])
        assert problems == [{"issue": "mapping_too_short", "expected_len": 2, "actual_len": 1}]

    def test_unmapped_used_slot_not_double_reported(self, scheduler):
        """A -1 on a used slot is reported structurally, not also as a tray problem."""
        problems = scheduler._validate_stored_ams_mapping([-1], [req(1)], [loaded(0)])
        assert [p["issue"] for p in problems] == ["used_slot_unmapped"]
