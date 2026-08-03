import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.api.routes import bmcu_link, bmcu_monitors
from backend.app.core.database import Base
from backend.app.models.bmcu_binary import (
    BMCUBinaryDevice,
    BMCUBinaryDiagnostic,
    BMCUBinaryLink,
    BMCUBinaryLossRange,
    BMCUBinaryRecord,
)
from backend.app.schemas.bmcu_binary import (
    LinkSnapshot,
    MetricPoint,
    MonitorDetail,
    MonitorSummary,
    TimelineResponse,
)
from backend.app.services.bmcu_binary.bmcu_decoder import (
    BMCUHello,
    decode_semantic,
    decode_wire_frame,
    validate_alpha3_wire_frame,
)
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.framing import IncrementalFrameParser
from backend.app.services.bmcu_binary.messages import decode_bmcu_frame
from backend.app.services.bmcu_binary.server import binary_transport_server
from backend.app.services.bmcu_binary.storage_keys import u64_decimal

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


FIXTURES = Path(__file__).parents[1] / "fixtures" / "bmcu_binary"


def _status_wire():
    frame = IncrementalFrameParser().feed((FIXTURES / "bmcu_status.bin").read_bytes())[0]
    return decode_bmcu_frame(frame.payload, validate_alpha3_wire_frame).wire_bytes


def _status():
    return decode_semantic(decode_wire_frame(_status_wire()))


def _event():
    frame = IncrementalFrameParser().feed((FIXTURES / "bmcu_event.bin").read_bytes())[0]
    wire = decode_bmcu_frame(frame.payload, validate_alpha3_wire_frame).wire_bytes
    return decode_semantic(decode_wire_frame(wire))


@pytest_asyncio.fixture
async def bmcu_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[table for table in Base.metadata.sorted_tables if table.name.startswith("bmcu_binary_")],
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime(2026, 8, 3)
    async with factory() as db:
        db.add(
            BMCUBinaryDevice(
                device_id="pico-bmcu-bridge",
                firmware="1",
                pico_boot_id="0000000000000001",
                first_seen_at=now,
                last_seen_at=now,
                last_ack_sequence="00000000000000000000",
                oldest_available_sequence="00000000000000000000",
                newest_available_sequence="00000000000000000003",
            )
        )
        db.add(BMCUBinaryLink(device_id="pico-bmcu-bridge", link_index=0, link_id="bmcu-a"))
        await db.commit()
    async with factory() as db:
        yield db
    await engine.dispose()


@pytest.fixture(autouse=True)
def clean_state():
    persistence = binary_transport_server.persistence
    persistence.current_state.clear()
    persistence.current_state_seen.clear()
    persistence.last_hello.clear()
    persistence.bmcu_hello_epoch.clear()
    yield
    persistence.current_state.clear()
    persistence.current_state_seen.clear()
    persistence.last_hello.clear()
    persistence.bmcu_hello_epoch.clear()


@pytest.mark.asyncio
async def test_link_snapshot_reports_stale_when_no_status_seen(bmcu_db) -> None:
    persistence = binary_transport_server.persistence
    persistence.current_state[("pico-bmcu-bridge", 0)] = _event()
    persistence.current_state_seen[("pico-bmcu-bridge", 0)] = time.monotonic()
    result = await bmcu_monitors.detail("pico-bmcu-bridge", db=bmcu_db, _=None)
    link = result.links[0]
    assert link.motion is None
    assert link.state == "stale"
    assert (link.currentSlot, link.pullPercent, link.pressure, link.activeMask) == (None, None, None, 0)


@pytest.mark.asyncio
async def test_link_snapshot_dates_the_loader_view_by_the_status_itself(bmcu_db) -> None:
    """The device row's last_seen_at tracks the transport, which keeps moving on
    EVENT traffic while STATUS is starved, so it cannot date the loader view."""
    persistence = binary_transport_server.persistence
    persistence.current_state[("pico-bmcu-bridge", 0)] = _status()
    persistence.current_state_seen[("pico-bmcu-bridge", 0)] = time.monotonic() - 7200
    link = (await bmcu_monitors.detail("pico-bmcu-bridge", db=bmcu_db, _=None)).links[0]
    assert link.state == "stale"
    assert 7195 <= link.statusAgeS <= 7205
    assert link.lastSeenAt < datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=110)
    # The values are still returned; the age is what tells a caller not to read
    # them as the live slot.
    assert link.currentSlot is not None


@pytest.mark.asyncio
async def test_link_snapshot_has_no_age_when_no_status_was_ever_decoded(bmcu_db) -> None:
    link = (await bmcu_monitors.detail("pico-bmcu-bridge", db=bmcu_db, _=None)).links[0]
    assert (link.statusAgeS, link.lastSeenAt, link.state) == (None, None, "stale")


@pytest.mark.asyncio
async def test_link_snapshot_reports_status_values(bmcu_db) -> None:
    persistence = binary_transport_server.persistence
    status = _status()
    persistence.current_state[("pico-bmcu-bridge", 0)] = status
    persistence.current_state_seen[("pico-bmcu-bridge", 0)] = time.monotonic()
    link = (await bmcu_monitors.detail("pico-bmcu-bridge", db=bmcu_db, _=None)).links[0]
    assert link.state == "online"
    assert link.currentSlot == status.current_slot
    assert link.activeMask == status.online_mask
    assert link.motion == "15,16,17,18"
    assert link.pressure == status.pressure
    assert link.faultCount == status.crc_error + status.frame_error


@pytest.mark.asyncio
async def test_timeline_default_window_returns_newest_rows(bmcu_db) -> None:
    wire = _status_wire()
    for index, day in enumerate((1, 2, 3)):
        bmcu_db.add(
            BMCUBinaryRecord(
                device_id="pico-bmcu-bridge",
                pico_boot_id="0000000000000001",
                transport_sequence=u64_decimal(index + 1),
                link_index=0,
                flags=0,
                message_type=MessageType.BMCU_FRAME,
                received_at_us=u64_decimal(1000 + index),
                server_received_at=datetime(2026, 8, day),
                bmcu_kind=2,
                raw_payload=b"\x00",
                raw_bmcu_frame=wire,
            )
        )
    await bmcu_db.commit()
    result = await bmcu_monitors.timeline("pico-bmcu-bridge", from_time=None, to_time=None, limit=2, db=bmcu_db, _=None)
    times = sorted({point["at"] for point in result["points"]})
    assert times == [datetime(2026, 8, 2), datetime(2026, 8, 3)]
    assert result["points"][0]["at"] == datetime(2026, 8, 2)
    assert result["downsampled"] is True


@pytest.mark.asyncio
async def test_timeline_one_sided_window_stays_oldest_first(bmcu_db) -> None:
    """`from` alone is a paginating caller: it must keep getting the rows just
    after the bound, not the newest ones."""
    wire = _status_wire()
    for index, day in enumerate((1, 2, 3)):
        bmcu_db.add(
            BMCUBinaryRecord(
                device_id="pico-bmcu-bridge",
                pico_boot_id="0000000000000001",
                transport_sequence=u64_decimal(index + 1),
                link_index=0,
                flags=0,
                message_type=MessageType.BMCU_FRAME,
                received_at_us=u64_decimal(1000 + index),
                server_received_at=datetime(2026, 8, day),
                bmcu_kind=2,
                raw_payload=b"\x00",
                raw_bmcu_frame=wire,
            )
        )
    await bmcu_db.commit()
    result = await bmcu_monitors.timeline(
        "pico-bmcu-bridge", from_time=datetime(2026, 8, 1), to_time=None, limit=2, db=bmcu_db, _=None
    )
    times = sorted({point["at"] for point in result["points"]})
    assert times == [datetime(2026, 8, 1), datetime(2026, 8, 2)]


async def _fill_window(db, *, minutes: int, kinds=(2,)):
    """One BMCU_FRAME row per minute per kind, oldest first."""
    wire = _status_wire()
    sequence = 0
    for minute in range(minutes):
        for kind in kinds:
            sequence += 1
            db.add(
                BMCUBinaryRecord(
                    device_id="pico-bmcu-bridge",
                    pico_boot_id="0000000000000001",
                    transport_sequence=u64_decimal(sequence),
                    link_index=0,
                    flags=0,
                    message_type=MessageType.BMCU_FRAME,
                    received_at_us=u64_decimal(1000 + sequence),
                    server_received_at=datetime(2026, 8, 3, 0, 0) + timedelta(minutes=minute),
                    bmcu_kind=kind,
                    raw_payload=b"\x00",
                    raw_bmcu_frame=wire,
                )
            )
    await db.commit()


@pytest.mark.asyncio
async def test_timeline_bounded_window_reaches_the_end_of_the_range(bmcu_db) -> None:
    """The range buttons send both bounds. Truncating at `limit` answered with
    the oldest minutes of the window, so the page never showed the present."""
    await _fill_window(bmcu_db, minutes=600)
    result = await bmcu_monitors.timeline(
        "pico-bmcu-bridge",
        from_time=datetime(2026, 8, 3, 0, 0),
        to_time=datetime(2026, 8, 3, 10, 0),
        limit=100,
        db=bmcu_db,
        _=None,
    )
    times = sorted({point["at"] for point in result["points"]})
    assert times[-1] == datetime(2026, 8, 3, 9, 59)
    assert times[0] <= datetime(2026, 8, 3, 0, 10)
    assert len(times) <= 100
    assert result["downsampled"] is True


@pytest.mark.asyncio
async def test_timeline_window_keeps_the_rare_kind_when_another_floods(bmcu_db) -> None:
    """EVENT outnumbers STATUS by orders of magnitude on the live bridge; a
    single stride across both would sample the loader state away."""
    wire = _status_wire()
    sequence = 0
    for minute in range(200):
        for kind, repeats in ((2, 1), (3, 20)):
            for repeat in range(repeats):
                sequence += 1
                bmcu_db.add(
                    BMCUBinaryRecord(
                        device_id="pico-bmcu-bridge",
                        pico_boot_id="0000000000000001",
                        transport_sequence=u64_decimal(sequence),
                        link_index=0,
                        flags=0,
                        message_type=MessageType.BMCU_FRAME,
                        received_at_us=u64_decimal(1000 + sequence),
                        server_received_at=datetime(2026, 8, 3) + timedelta(minutes=minute, seconds=repeat),
                        bmcu_kind=kind,
                        raw_payload=b"\x00",
                        raw_bmcu_frame=wire,
                    )
                )
    await bmcu_db.commit()
    result = await bmcu_monitors.timeline(
        "pico-bmcu-bridge",
        from_time=datetime(2026, 8, 3),
        to_time=datetime(2026, 8, 3, 4),
        limit=100,
        db=bmcu_db,
        _=None,
    )
    # Every stored row decodes to the same STATUS fixture, so count rows, not
    # kinds: the assertion is that the window is spanned, not truncated.
    times = sorted({point["at"] for point in result["points"]})
    # Rows stop at minute 199 (03:19); the window runs to 04:00.
    assert times[-1] >= datetime(2026, 8, 3, 3, 19)
    assert times[0] <= datetime(2026, 8, 3, 0, 30)


@pytest.mark.asyncio
async def test_timeline_small_window_is_returned_whole(bmcu_db) -> None:
    await _fill_window(bmcu_db, minutes=5)
    result = await bmcu_monitors.timeline(
        "pico-bmcu-bridge",
        from_time=datetime(2026, 8, 3, 0, 0),
        to_time=datetime(2026, 8, 3, 1, 0),
        limit=100,
        db=bmcu_db,
        _=None,
    )
    assert len({point["at"] for point in result["points"]}) == 5
    assert result["downsampled"] is False


@pytest.mark.asyncio
async def test_metrics_honours_the_requested_window(bmcu_db) -> None:
    """`from`/`to` used to be dropped silently, so every range button rendered
    the same newest rows."""
    for minute in range(400):
        bmcu_db.add(
            BMCUBinaryDiagnostic(
                device_id="pico-bmcu-bridge",
                pico_boot_id="0000000000000001",
                transport_sequence=u64_decimal(minute + 1),
                recorded_at=datetime(2026, 8, 3) + timedelta(minutes=minute),
                payload=b"",
            )
        )
    await bmcu_db.commit()
    inside = await bmcu_monitors.metrics(
        "pico-bmcu-bridge",
        from_time=datetime(2026, 8, 3, 0, 0),
        to_time=datetime(2026, 8, 3, 1, 0),
        limit=500,
        db=bmcu_db,
        _=None,
    )
    assert [point["at"] for point in inside] == [
        datetime(2026, 8, 3) + timedelta(minutes=minute) for minute in reversed(range(61))
    ]
    empty = await bmcu_monitors.metrics(
        "pico-bmcu-bridge",
        from_time=datetime(2026, 8, 4),
        to_time=datetime(2026, 8, 5),
        limit=500,
        db=bmcu_db,
        _=None,
    )
    assert empty == []


@pytest.mark.asyncio
async def test_metrics_without_a_window_still_returns_the_newest_rows(bmcu_db) -> None:
    """An unbounded caller wants the latest samples, not a sample of the whole
    retention, so thinning must stay scoped to a bounded window."""
    for minute in range(300):
        bmcu_db.add(
            BMCUBinaryDiagnostic(
                device_id="pico-bmcu-bridge",
                pico_boot_id="0000000000000001",
                transport_sequence=u64_decimal(minute + 1),
                recorded_at=datetime(2026, 8, 3) + timedelta(minutes=minute),
                payload=b"",
            )
        )
    await bmcu_db.commit()
    points = await bmcu_monitors.metrics(
        "pico-bmcu-bridge", from_time=None, to_time=None, limit=3, db=bmcu_db, _=None
    )
    assert [point["at"] for point in points] == [
        datetime(2026, 8, 3) + timedelta(minutes=minute) for minute in (299, 298, 297)
    ]


@pytest.mark.asyncio
async def test_metrics_window_is_sampled_not_truncated(bmcu_db) -> None:
    for minute in range(1000):
        bmcu_db.add(
            BMCUBinaryDiagnostic(
                device_id="pico-bmcu-bridge",
                pico_boot_id="0000000000000001",
                transport_sequence=u64_decimal(minute + 1),
                recorded_at=datetime(2026, 8, 3) + timedelta(minutes=minute),
                payload=b"",
            )
        )
    await bmcu_db.commit()
    points = await bmcu_monitors.metrics(
        "pico-bmcu-bridge",
        from_time=datetime(2026, 8, 3),
        to_time=datetime(2026, 8, 3) + timedelta(minutes=999),
        limit=100,
        db=bmcu_db,
        _=None,
    )
    assert len(points) <= 100
    # Newest sample present (the summary tiles read it) and the window spanned.
    assert points[0]["at"] == datetime(2026, 8, 3) + timedelta(minutes=999)
    assert points[-1]["at"] <= datetime(2026, 8, 3) + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_bmcu_link_device_reports_real_capabilities_and_drops(bmcu_db) -> None:
    persistence = binary_transport_server.persistence
    status = _status()
    persistence.current_state[("pico-bmcu-bridge", 0)] = status
    persistence.current_state_seen[("pico-bmcu-bridge", 0)] = time.monotonic()
    persistence.last_hello[("pico-bmcu-bridge", 0)] = BMCUHello(3, 0x2A, 1, 2, 1000)
    persistence.bmcu_hello_epoch["pico-bmcu-bridge"] = 7
    bmcu_db.add(
        BMCUBinaryLossRange(
            device_id="pico-bmcu-bridge",
            pico_boot_id="0000000000000001",
            report_sequence=u64_decimal(5),
            first_sequence=u64_decimal(2),
            last_sequence=u64_decimal(4),
            dropped_count=u64_decimal(3),
            reason=1,
            recorded_at=datetime(2026, 8, 3),
        )
    )
    await bmcu_db.commit()
    payload = await bmcu_link.device("pico-bmcu-bridge", db=bmcu_db, _=None)
    assert payload["capabilities"] == 0x2A
    assert payload["protocol_min"] == 3 and payload["protocol_max"] == 3
    assert payload["bmcu_boot_session"] == 7
    assert payload["dropped_count"] == 3
    assert payload["last_status"]["pressure"] == status.pressure
    link = (await bmcu_monitors.detail("pico-bmcu-bridge", db=bmcu_db, _=None)).links[0]
    assert payload["last_status"]["motion"] == link.motion
    assert payload["last_status"]["current_slot"] == link.currentSlot
    assert payload["last_status"]["pull_pct"] == link.pullPercent


def test_link_snapshot_allows_null_motion() -> None:
    payload = LinkSnapshot(
        linkIndex=0,
        linkId="bmcu-a",
        state="stale",
        currentSlot=None,
        activeMask=0,
        motion=None,
        pullPercent=None,
        pressure=None,
        faultCount=0,
        lastSeenAt=None,
    ).model_dump(mode="json")
    assert payload["motion"] is None
    detail = MonitorDetail(
        deviceId="pico",
        displayName="pico",
        firmware="1",
        health="online",
        lastSeenAt=None,
        bootId=None,
        linkCount=1,
        onlineLinks=0,
        ackSequence="00000000000000000000",
        replayPending=0,
        anomalyCount=0,
        firstSeenAt=None,
        links=[LinkSnapshot(**payload)],
    ).model_dump(mode="json")
    assert detail["links"][0]["motion"] is None
