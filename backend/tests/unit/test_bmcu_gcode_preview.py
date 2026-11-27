import zipfile

import pytest

from backend.app.services.bmcu_gcode_preview import PreviewUnavailable, parse_plate


def archive(tmp_path, files):
    path = tmp_path / "job.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return path


def test_extracts_extrusion_layers_and_logical_tools(tmp_path):
    path = archive(
        tmp_path,
        {
            "Metadata/plate_1.gcode": """G90
M83
G1 X10 Y10
; CHANGE_LAYER
T1
G1 X20 Y10 E1
G1 E-1
G1 X21 E1
G1 X22 E1
; CHANGE_LAYER
G91
G1 X1 Y2 E1
"""
        },
    )
    plate, planned = parse_plate(path, None)
    assert plate == 1
    assert len(planned.layers) == 2
    assert list(planned.layers[0]) == [10, 10, 20, 10, 1, 21, 10, 22, 10, 1]
    assert list(planned.layers[1]) == [22, 10, 23, 12, 1]


def test_unknown_plate_is_not_guessed(tmp_path):
    path = archive(tmp_path, {"Metadata/plate_1.gcode": "", "Metadata/plate_2.gcode": ""})
    with pytest.raises(PreviewUnavailable, match="plate is unknown"):
        parse_plate(path, None)


def test_unsupported_motion_fails_closed(tmp_path):
    path = archive(tmp_path, {"Metadata/plate_1.gcode": ";LAYER_CHANGE\nG5 X10\n"})
    with pytest.raises(PreviewUnavailable, match="Unsupported"):
        parse_plate(path, 1)


def test_arc_is_approximated_as_extrusion(tmp_path):
    path = archive(tmp_path, {"Metadata/plate_1.gcode": "G1 X1 Y0\nM83\n;LAYER_CHANGE\nG3 X0 Y1 I-1 J0 E1\n"})
    _, planned = parse_plate(path, 1)
    path_data = planned.layers[0]
    assert len(path_data) > 5
    assert list(path_data[-5:-1])[-2:] == pytest.approx([0, 1])


@pytest.mark.asyncio
async def test_concurrent_viewers_share_one_parse(tmp_path, monkeypatch):
    import asyncio

    import backend.app.services.bmcu_gcode_preview as preview

    path = archive(tmp_path, {"Metadata/plate_1.gcode": ";LAYER_CHANGE\n"})
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def fake_worker(path, plate):
        calls.append((path, plate))
        started.set()
        await finish.wait()
        return {"plate": 1, "layers": [[]]}

    class Request:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(preview, "_run_parser", fake_worker)
    monkeypatch.setattr(preview, "_job_lock", None)
    monkeypatch.setattr(preview, "_cache_key", None)
    monkeypatch.setattr(preview, "_job_key", None)
    monkeypatch.setattr(preview, "_job_task", None)
    monkeypatch.setattr(preview, "_job_waiters", 0)
    first = asyncio.create_task(preview.preview_layers(path, 1, Request()))
    await started.wait()
    remaining = [asyncio.create_task(preview.preview_layers(path, 1, Request())) for _ in range(7)]
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    assert all(result["plate"] == 1 for result in await asyncio.gather(*remaining))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_route_requires_verified_print_and_rechecks_job(monkeypatch):
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend.app import main
    from backend.app.api.routes import archives, printers
    from backend.app.services import bmcu_gcode_preview as preview, print_log

    state = SimpleNamespace(
        connected=True,
        state="RUNNING",
        skipped_objects=[],
        layer_num=25,
        total_layers=100,
        progress=25,
        raw_data={},
        subtask_id="1",
        gcode_file="plate.gcode",
    )
    client = SimpleNamespace(state=state, _last_message_time=time.time())
    archive_row = SimpleNamespace(id=7, file_path="job.3mf")
    get_archive = AsyncMock(return_value=archive_row)
    check_archive = AsyncMock(return_value=archive_row)
    monkeypatch.setattr(printers.printer_manager, "get_client", lambda _: client)
    monkeypatch.setattr(printers, "resolve_plate_id", lambda _: 1)
    monkeypatch.setattr(main, "running_print_archive", get_archive)
    monkeypatch.setattr(
        print_log, "open_run_on_printer", AsyncMock(return_value=SimpleNamespace(id=42, archive_id=7, subtask_id="1"))
    )
    monkeypatch.setattr(archives, "_ensure_archive_visible", check_archive)
    monkeypatch.setattr(
        preview, "preview_layers", AsyncMock(return_value={"plate": 1, "layers": ["" for _ in range(25)]})
    )
    response = await printers.get_bmcu_gcode_window(1, object(), None, (None, True), object(), AsyncMock())
    assert (response["firstLayer"], response["lastLayer"]) == (5, 24)
    assert len(response["layers"]) == 20
    assert get_archive.await_count == 2
    assert all(call.kwargs == {"printing_only": True, "by_name": False} for call in get_archive.await_args_list)
    assert check_archive.await_count == 2
    state.progress = 0
    unknown = await printers.get_bmcu_gcode_window(1, object(), None, (None, True), object(), AsyncMock())
    assert unknown["progress"] is None
    state.raw_data = {"mc_percent": 0}
    zero = await printers.get_bmcu_gcode_window(1, object(), None, (None, True), object(), AsyncMock())
    assert zero["progress"] == 0


@pytest.mark.asyncio
async def test_route_refuses_name_only_archive(monkeypatch):
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from backend.app import main
    from backend.app.api.routes import printers

    state = SimpleNamespace(connected=True, state="RUNNING", skipped_objects=[], layer_num=3)
    client = SimpleNamespace(state=state, _last_message_time=time.time())
    monkeypatch.setattr(printers.printer_manager, "get_client", lambda _: client)
    get_archive = AsyncMock(return_value=None)
    monkeypatch.setattr(main, "running_print_archive", get_archive)
    with pytest.raises(HTTPException, match="securely matched"):
        await printers.get_bmcu_gcode_window(1, object(), None, (None, True), object(), AsyncMock())
    assert get_archive.await_args.kwargs["by_name"] is False


def test_conditional_travel_resynchronizes_without_inventing_a_segment(tmp_path):
    path = archive(
        tmp_path,
        {
            "Metadata/plate_1.gcode": """G90
M83
G1 X0 Y0
;LAYER_CHANGE
G1 X1 E1
; SKIPPABLE_START
M622 J1
G39.3 S1
G0 X20 Y20
M623
; SKIPPABLE_END
G1 X5 Y5
G1 X6 E1
"""
        },
    )
    _, result = parse_plate(path, 1)
    assert list(result.layers[0]) == [0, 0, 1, 0, 0, 5, 5, 6, 5, 0]


def test_unsupported_later_section_keeps_only_complete_previous_layers(tmp_path):
    path = archive(
        tmp_path, {"Metadata/plate_1.gcode": "G1 X0 Y0\nM83\n;LAYER_CHANGE\nG1 X1 E1\n;LAYER_CHANGE\nG5 X2\n"}
    )
    _, result = parse_plate(path, 1)
    assert len(result.layers) == 1
    assert result.truncated_reason is not None


@pytest.mark.asyncio
async def test_last_disconnected_viewer_cancels_worker(tmp_path, monkeypatch):
    import asyncio

    import backend.app.services.bmcu_gcode_preview as preview

    path = archive(tmp_path, {"Metadata/plate_1.gcode": ";LAYER_CHANGE\n"})
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_worker(path, plate):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class Request:
        async def is_disconnected(self):
            return False

    for key, value in (
        ("_job_lock", None),
        ("_cache_key", None),
        ("_cache_error", None),
        ("_job_key", None),
        ("_job_task", None),
        ("_job_waiters", 0),
    ):
        monkeypatch.setattr(preview, key, value)
    monkeypatch.setattr(preview, "_run_parser", fake_worker)
    viewer = asyncio.create_task(preview.preview_layers(path, 1, Request()))
    await started.wait()
    viewer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await viewer
    await asyncio.wait_for(cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_worker_emits_compact_layer_data(tmp_path):
    import base64
    from array import array

    from backend.app.services.bmcu_gcode_preview import _run_parser

    path = archive(tmp_path, {"Metadata/plate_1.gcode": "G1 X0 Y0\nM83\n;LAYER_CHANGE\nG1 X1 E1\n"})
    result = await _run_parser(path, 1)
    data = array("f")
    data.frombytes(base64.b64decode(result["layers"][0]))
    assert list(data) == [0, 0, 1, 0, 0]


@pytest.mark.asyncio
async def test_route_refuses_object_skips(monkeypatch):
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from backend.app.api.routes import printers

    state = SimpleNamespace(connected=True, state="RUNNING", skipped_objects=[1], layer_num=3)
    client = SimpleNamespace(state=state, _last_message_time=time.time())
    monkeypatch.setattr(printers.printer_manager, "get_client", lambda _: client)
    with pytest.raises(HTTPException, match="Object skips"):
        await printers.get_bmcu_gcode_window(1, object(), None, (None, True), object(), AsyncMock())


@pytest.mark.asyncio
async def test_changed_print_cancels_only_its_viewer(tmp_path, monkeypatch):
    import asyncio

    import backend.app.services.bmcu_gcode_preview as preview

    path = archive(tmp_path, {"Metadata/plate_1.gcode": ";LAYER_CHANGE\n"})
    started = asyncio.Event()
    finish = asyncio.Event()
    current = True

    async def fake_worker(path, plate):
        started.set()
        await finish.wait()
        return {"plate": 1, "layers": [""]}

    class Request:
        async def is_disconnected(self):
            return False

    for key, value in (
        ("_job_lock", None),
        ("_cache_key", None),
        ("_cache_error", None),
        ("_job_key", None),
        ("_job_task", None),
        ("_job_waiters", 0),
    ):
        monkeypatch.setattr(preview, key, value)
    monkeypatch.setattr(preview, "_run_parser", fake_worker)
    changed = asyncio.create_task(preview.preview_layers(path, 1, Request(), lambda: current))
    await started.wait()
    other = asyncio.create_task(preview.preview_layers(path, 1, Request()))
    await asyncio.sleep(0)
    current = False
    with pytest.raises(preview.PreviewContextChanged):
        await changed
    assert not other.done()
    finish.set()
    assert (await other)["plate"] == 1


def test_conditional_extrusion_does_not_create_a_misleading_layer(tmp_path):
    path = archive(
        tmp_path,
        {
            "Metadata/plate_1.gcode": """G90
M83
G1 X0 Y0
; LAYER_CHANGE
G1 X1 E1
; LAYER_CHANGE
; SKIPPABLE_START
G1 E2
; SKIPPABLE_END
G1 X2 Y2
G1 X3 E1
"""
        },
    )
    _, result = parse_plate(path, 1)
    assert len(result.layers) == 1
    assert result.truncated_reason == "Conditional extrusion is not supported"
