"""Unit tests for the filament load/unload plan (private fork).

The plan walks the deferred-unload chain across a printer's queue. The bug it
replaces lived in the frontend: it read ``ams_mapping`` off pending rows, which
is always NULL until dispatch, so the carry collapsed after the first item and
every later row claimed "no unload at start". These tests lock the chain: it
must survive a full queue of pending rows, and it must say *unknown* rather
than *nothing happens* when a mapping can't be resolved.
"""

import json
from unittest.mock import patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.services.filament_plan import (
    build_filament_plan,
    resolve_unload_mode,
    tail_is_deferred,
)


def _status_with_trays(trays):
    """Minimal PrinterState stand-in: ``trays`` maps global id -> (type, color)."""

    class _Status:
        raw_data = {"ams": [], "vt_tray": []}

    status = _Status()
    ams_units = {}
    for gid, (ftype, color) in trays.items():
        ams_id, tray_id = divmod(gid, 4)
        ams_units.setdefault(ams_id, []).append(
            {"id": tray_id, "tray_type": ftype, "tray_color": color, "tray_info_idx": "GFX00"}
        )
    status.raw_data = {
        "ams": [{"id": aid, "tray": sorted(t, key=lambda x: x["id"])} for aid, t in sorted(ams_units.items())],
        "vt_tray": [],
        "ams_extruder_map": {},
    }
    return status


class _PlanHarness:
    """Builds a queue on the real DB and runs the plan with a fake printer."""

    def __init__(self, db_session, printer_id, trays):
        self.db = db_session
        self.printer_id = printer_id
        self.status = _status_with_trays(trays)
        self.planned: dict[int, list[int]] = {}

    async def add(self, *, item_id, status="pending", position, ams_mapping=None, plan=None, **kwargs):
        row = PrintQueueItem(
            id=item_id,
            printer_id=self.printer_id,
            position=position,
            status=status,
            gcode_injection=kwargs.pop("gcode_injection", True),
            ams_mapping=json.dumps(ams_mapping) if ams_mapping is not None else None,
            **kwargs,
        )
        self.db.add(row)
        await self.db.commit()
        if plan is not None:
            self.planned[item_id] = plan
        return row

    async def withhold(self, item_id, trays):
        self.db.add(
            Settings(
                key=f"deferred_unload_state:{self.printer_id}",
                value=json.dumps({"item_id": item_id, "ams_mapping": json.dumps(trays), "block": "..."}),
            )
        )
        await self.db.commit()

    async def run(self):
        async def fake_compute(db, printer_id, item):
            return self.planned.get(item.id)

        with (
            patch("backend.app.services.printer_manager.printer_manager.get_status", return_value=self.status),
            patch(
                "backend.app.services.print_scheduler.scheduler._compute_ams_mapping_for_printer",
                side_effect=fake_compute,
            ),
        ):
            return await build_filament_plan(self.db, self.printer_id)


def _by_id(plan):
    return {i["item_id"]: i for i in plan["items"]}


def _codes(plan, item_id):
    return {w["code"] for w in plan["warnings"] if w["item_id"] == item_id}


@pytest.fixture
async def harness(db_session, printer_factory):
    printer = await printer_factory(name="a1", model="A1 Mini")
    return _PlanHarness(
        db_session,
        printer.id,
        {1: ("PETG", "808080FF"), 3: ("PLA", "FFFFFFFF"), 4: ("PETG", "D6ABFFFF")},
    )


class TestModeResolution:
    """The dispatch path imports these, so they are the single source of truth."""

    @pytest.mark.parametrize(
        "unload_edit,defer_unload,injection,expected",
        [
            (None, None, True, "auto"),
            (None, True, True, "auto"),
            (None, True, False, "start-less-defer"),
            (None, False, True, "end"),
            ("start", None, True, "start"),
            ("end", None, True, "end"),
            ("none", None, True, "none"),
        ],
    )
    def test_mode(self, unload_edit, defer_unload, injection, expected):
        assert resolve_unload_mode(unload_edit, defer_unload, injection) == expected

    def test_tail_kept_for_end_and_none(self):
        assert tail_is_deferred("end", True) is False
        assert tail_is_deferred("none", True) is False

    def test_tail_stripped_for_auto_and_start_when_injecting(self):
        assert tail_is_deferred("auto", True) is True
        assert tail_is_deferred("start", True) is True

    def test_no_injection_means_nothing_to_strip(self):
        assert tail_is_deferred("auto", False) is False

    def test_env_toggle_disables_stripping(self, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_DEFER_TAIL_UNLOAD", "0")
        assert tail_is_deferred("auto", True) is False


class TestChain:
    async def test_carry_survives_the_whole_pending_queue(self, harness):
        """The regression this module exists for.

        Five pending rows, none with a stored ``ams_mapping``. The swap belongs
        to the item that changes trays — not to the first row and then silence.
        """
        await harness.withhold(200, [4])
        await harness.add(item_id=201, position=2, plan=[4])
        await harness.add(item_id=202, position=3, plan=[-1, -1, 1])
        await harness.add(item_id=203, position=4, plan=[-1, -1, 1])
        await harness.add(item_id=204, position=5, plan=[-1, 3])
        await harness.add(item_id=205, position=6, plan=[-1, 3])

        items = _by_id(await harness.run())

        assert items[201]["unload_at_start"] is False  # tray 4 already loaded
        assert items[202]["unload_at_start"] is True  # 4 -> 1
        assert items[203]["unload_at_start"] is False
        assert items[204]["unload_at_start"] is True  # 1 -> 3
        assert items[205]["unload_at_start"] is False
        # Every tail is stripped under 'auto', so no job unloads at its end.
        assert [items[i]["unload_at_end"] for i in (201, 202, 203, 204, 205)] == [False] * 5

    async def test_planned_mapping_is_labelled_and_not_persisted(self, harness):
        await harness.withhold(200, [4])
        row = await harness.add(item_id=201, position=2, plan=[-1, -1, 1])

        items = _by_id(await harness.run())
        assert items[201]["trays"] == [1]
        assert items[201]["trays_source"] == "planned"
        assert items[201]["filaments"][0]["label"] == "AMS0-B"
        assert items[201]["filaments"][0]["type"] == "PETG"

        await harness.db.refresh(row)
        assert row.ams_mapping is None, "planned mapping must not be written back"

    async def test_stored_mapping_wins_over_planning(self, harness):
        await harness.withhold(200, [4])
        await harness.add(item_id=201, position=2, ams_mapping=[1], plan=[3])

        items = _by_id(await harness.run())
        assert items[201]["trays"] == [1]
        assert items[201]["trays_source"] == "stored"

    async def test_end_mode_empties_the_hotend_for_the_next_job(self, harness):
        await harness.withhold(200, [4])
        await harness.add(item_id=201, position=2, plan=[4], unload_edit="end")
        await harness.add(item_id=202, position=3, plan=[1])

        items = _by_id(await harness.run())
        assert items[201]["unload_at_end"] is True
        # 201 pulled its filament back, so 202 starts against an empty hotend
        # even though it uses a different tray.
        assert items[202]["unload_at_start"] is False

    async def test_start_mode_forces_an_unload_even_on_the_same_tray(self, harness):
        await harness.withhold(200, [4])
        await harness.add(item_id=201, position=2, plan=[4], unload_edit="start")

        items = _by_id(await harness.run())
        assert items[201]["unload_at_start"] is True

    async def test_no_withheld_state_means_nothing_to_unload(self, harness):
        await harness.add(item_id=201, position=2, plan=[4])

        items = _by_id(await harness.run())
        assert items[201]["unload_at_start"] is False
        assert items[201]["swap_from"] is None

    async def test_unresolved_mapping_reports_unknown_not_false(self, harness):
        """Fail-safe direction: 'we can't tell' must never render as 'nothing
        happens' — that is exactly how the old badge lied."""
        await harness.withhold(200, [4])
        await harness.add(item_id=201, position=2, plan=None)
        await harness.add(item_id=202, position=3, plan=[1])

        plan = await harness.run()
        items = _by_id(plan)
        assert items[201]["trays"] is None
        assert items[201]["unload_at_start"] is None
        assert "mapping_unresolved" in _codes(plan, 201)
        # 201 still holds *something*, so 202's swap is unknown rather than absent.
        assert items[202]["unload_at_start"] is None

    async def test_printing_item_is_reported_but_not_editable(self, harness):
        await harness.withhold(200, [4])
        await harness.add(item_id=200, status="printing", position=1, ams_mapping=[4])
        await harness.add(item_id=201, position=2, plan=[4])

        items = _by_id(await harness.run())
        assert items[200]["editable"] is False
        assert items[200]["unload_at_start"] is None
        assert items[200]["trays"] == [4]
        assert items[201]["editable"] is True


class TestWarnings:
    async def test_last_job_leaves_filament_loaded(self, harness):
        await harness.withhold(200, [4])
        await harness.add(item_id=201, position=2, plan=[4])
        await harness.add(item_id=202, position=3, plan=[4])

        plan = await harness.run()
        assert _codes(plan, 201) == set()
        assert "filament_left_loaded" in _codes(plan, 202)

    async def test_last_job_with_end_mode_is_clean(self, harness):
        await harness.withhold(200, [4])
        await harness.add(item_id=201, position=2, plan=[4], unload_edit="end")

        plan = await harness.run()
        assert "filament_left_loaded" not in _codes(plan, 201)

    async def test_material_change_at_start_is_flagged(self, harness):
        """PETG left in the nozzle while a PLA job heats to its own (lower)
        start temperature — the swap that actually risks a jam."""
        await harness.withhold(200, [4])  # PETG
        await harness.add(item_id=201, position=2, plan=[-1, 3])  # PLA

        plan = await harness.run()
        assert "material_change_at_start" in _codes(plan, 201)

    async def test_same_material_swap_is_not_flagged(self, harness):
        await harness.withhold(200, [4])  # PETG purple
        await harness.add(item_id=201, position=2, plan=[1])  # PETG gray

        plan = await harness.run()
        assert "material_change_at_start" not in _codes(plan, 201)


class TestEndpoint:
    async def test_unknown_printer_is_404(self, async_client):
        resp = await async_client.get("/api/v1/queue/printer/9999/filament-plan")
        assert resp.status_code == 404

    async def test_returns_plan_shape(self, async_client, printer_factory):
        printer = await printer_factory(name="plan-printer")
        resp = await async_client.get(f"/api/v1/queue/printer/{printer.id}/filament-plan")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["printer_id"] == printer.id
        assert body["items"] == []
        assert body["warnings"] == []
        assert body["withheld"]["active"] is False
