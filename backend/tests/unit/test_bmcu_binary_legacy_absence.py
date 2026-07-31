from pathlib import Path


ROOT = Path(__file__).parents[3]


def test_legacy_json_ingest_routes_and_modules_are_absent() -> None:
    routes = (ROOT / "backend/app/api/routes/bmcu_link.py").read_text()
    assert '@router.websocket("/ws")' not in routes
    assert '@router.post("/ingest"' not in routes
    assert '@router.post("/ndjson"' not in routes
    for relative in (
        "backend/app/schemas/bmcu_link.py",
        "backend/app/services/bmcu_link.py",
        "backend/app/services/bmcu_link_poller.py",
        "backend/app/services/bmcu_link_control.py",
        "backend/app/models/bmcu_link_device.py",
        "backend/app/models/bmcu_link_event.py",
    ):
        assert not (ROOT / relative).exists()
