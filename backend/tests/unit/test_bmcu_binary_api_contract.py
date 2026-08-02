import json
from datetime import datetime
from pathlib import Path

from backend.app.schemas.bmcu_binary import MetricPoint, MonitorDetail, MonitorSummary, TimelineResponse

FIXTURE = json.loads((Path(__file__).parents[3] / "frontend/src/__tests__/fixtures/bmcuMonitorApi.json").read_text())


def test_monitor_contract_uses_frontend_stable_camel_case() -> None:
    now = datetime(2026, 7, 31)
    payload = MonitorDetail(
        deviceId="pico",
        displayName="pico",
        firmware="1",
        health="online",
        lastSeenAt=now,
        bootId="0000000000000001",
        linkCount=1,
        onlineLinks=1,
        ackSequence="00000000000000000001",
        replayPending=0,
        anomalyCount=0,
        firstSeenAt=now,
        links=[],
    ).model_dump(mode="json")
    assert payload["deviceId"] == "pico"
    assert "device_id" not in payload


def test_timeline_contract_serializes_from_alias() -> None:
    now = datetime(2026, 7, 31)
    payload = TimelineResponse(points=[], **{"from": now}, to=now, downsampled=False)
    assert payload.model_dump(by_alias=True)["from"] == now


def test_backend_and_frontend_share_complete_contract_fixture() -> None:
    assert [MonitorSummary.model_validate(item).model_dump(mode="json") for item in FIXTURE["list"]] == FIXTURE["list"]
    assert MonitorDetail.model_validate(FIXTURE["detail"]).model_dump(mode="json") == FIXTURE["detail"]
    timeline = TimelineResponse.model_validate(FIXTURE["timeline"])
    assert timeline.model_dump(mode="json", by_alias=True) == FIXTURE["timeline"]
    assert len({point.id for point in timeline.points}) == len(timeline.points)
    # The API omits unknown metrics (exclude_none); a null in the fixture means
    # "absent on the wire", so compare against the fixture with nulls dropped.
    assert [
        MetricPoint.model_validate(item).model_dump(mode="json", exclude_none=True) for item in FIXTURE["metrics"]
    ] == [{k: v for k, v in item.items() if v is not None} for item in FIXTURE["metrics"]]
