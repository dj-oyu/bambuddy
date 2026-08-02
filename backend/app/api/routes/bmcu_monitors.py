"""Read/control API for authenticated binary BMCU Monitors."""

import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.bmcu_binary import (
    BMCUBinaryDevice,
    BMCUBinaryDiagnostic,
    BMCUBinaryLink,
    BMCUBinaryLog,
    BMCUBinaryRecord,
)
from backend.app.models.user import User
from backend.app.schemas.bmcu_binary import (
    ControlRequest,
    LinkSnapshot,
    MetricPoint,
    MonitorDetail,
    MonitorSummary,
    TimelineResponse,
)
from backend.app.services.bmcu_binary.bmcu_decoder import BMCUStatus, decode_semantic, decode_wire_frame
from backend.app.services.bmcu_binary.constants import MessageType
from backend.app.services.bmcu_binary.messages import decode_tlvs, typed_tlv_value
from backend.app.services.bmcu_binary.server import binary_transport_server
from backend.app.services.bmcu_binary.state_view import status_snapshot
from backend.app.services.bmcu_binary.storage_keys import u64_decimal
from backend.app.services.bmcu_binary.timeline import anomaly_inputs, timeline_points

router = APIRouter(prefix="/bmcu-monitors", tags=["bmcu-monitors"])
ReadAccess = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ)
ControlAccess = RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL)
_STALE_AFTER_S = 15.0


async def _device(db, device_id):
    row = (
        await db.execute(select(BMCUBinaryDevice).where(BMCUBinaryDevice.device_id == device_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "BMCU Monitor not found")
    return row


def _summary(row, link_count=0):
    connected = binary_transport_server.registry.get(row.device_id) is not None
    return MonitorSummary(
        deviceId=row.device_id,
        displayName=row.device_id,
        firmware=row.firmware,
        health="online" if connected else "offline",
        lastSeenAt=row.last_seen_at,
        bootId=row.pico_boot_id,
        linkCount=link_count,
        onlineLinks=link_count if connected else 0,
        ackSequence=row.last_ack_sequence,
        replayPending=max(0, int(row.newest_available_sequence) - int(row.last_ack_sequence)),
        anomalyCount=0,
    )


@router.get("", response_model=list[MonitorSummary])
async def devices(db: AsyncSession = Depends(get_db), _: User | None = ReadAccess):
    rows = (await db.execute(select(BMCUBinaryDevice).order_by(BMCUBinaryDevice.device_id))).scalars()
    counts = dict(
        (
            await db.execute(
                select(BMCUBinaryLink.device_id, func.count(BMCUBinaryLink.id)).group_by(BMCUBinaryLink.device_id)
            )
        ).all()
    )
    return [_summary(row, counts.get(row.device_id, 0)) for row in rows]


@router.get("/{device_id}", response_model=MonitorDetail)
async def detail(device_id: str, db: AsyncSession = Depends(get_db), _: User | None = ReadAccess):
    row = await _device(db, device_id)
    stored_links = (
        (
            await db.execute(
                select(BMCUBinaryLink).where(BMCUBinaryLink.device_id == device_id).order_by(BMCUBinaryLink.link_index)
            )
        )
        .scalars()
        .all()
    )
    persistence = binary_transport_server.persistence
    links = []
    for stored in stored_links:
        key = (device_id, stored.link_index)
        value = persistence.current_state.get(key)
        if not isinstance(value, BMCUStatus):
            value = None
        if value is None:
            snapshot, state, faults = None, "stale", 0
        else:
            age_s = time.monotonic() - persistence.current_state_seen.get(key, time.monotonic())
            snapshot = status_snapshot(value, age_s)
            state = "online" if age_s < _STALE_AFTER_S else "stale"
            faults = value.crc_error + value.frame_error
        links.append(
            LinkSnapshot(
                linkIndex=stored.link_index,
                linkId=stored.link_id or f"bmcu-{stored.link_index}",
                state=state,
                currentSlot=snapshot["current_slot"] if snapshot else None,
                activeMask=snapshot["online_mask"] if snapshot else 0,
                motion=snapshot["motion"] if snapshot else None,
                pullPercent=snapshot["pull_pct"] if snapshot else None,
                pressure=snapshot["pressure"] if snapshot else None,
                faultCount=faults,
                lastSeenAt=row.last_seen_at,
            )
        )
    return MonitorDetail(**_summary(row, len(stored_links)).model_dump(), firstSeenAt=row.first_seen_at, links=links)


@router.get("/{device_id}/timeline", response_model=TimelineResponse)
async def timeline(
    device_id: str,
    from_time: datetime | None = Query(None, alias="from"),
    to_time: datetime | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    _: User | None = ReadAccess,
):
    await _device(db, device_id)
    query = select(BMCUBinaryRecord).where(
        BMCUBinaryRecord.device_id == device_id,
        BMCUBinaryRecord.message_type == MessageType.BMCU_FRAME,
    )
    if from_time:
        query = query.where(BMCUBinaryRecord.server_received_at >= from_time)
    if to_time:
        query = query.where(BMCUBinaryRecord.server_received_at <= to_time)
    # Only a caller that asked for no window at all gets the newest-first fetch.
    # A one-sided `from`/`to` keeps ascending "first rows after the bound"
    # semantics, which is what such a caller is paginating with.
    explicit_window = from_time is not None or to_time is not None
    if explicit_window:
        order = (BMCUBinaryRecord.server_received_at, BMCUBinaryRecord.transport_sequence)
    else:
        # Without an explicit window the useful view is the most recent one; the
        # ascending fetch returned the oldest rows ever stored.
        order = (BMCUBinaryRecord.server_received_at.desc(), BMCUBinaryRecord.transport_sequence.desc())
    rows = (await db.execute(query.order_by(*order).limit(limit + 1))).scalars().all()
    downsampled = len(rows) > limit
    rows = rows[:limit]
    if not explicit_window:
        rows = list(reversed(rows))
    output = []
    for row in rows:
        if not row.raw_bmcu_frame or row.received_at_us is None:
            continue
        semantic = decode_semantic(decode_wire_frame(row.raw_bmcu_frame))
        if semantic is None:
            continue
        points = timeline_points(semantic, int(row.received_at_us), row.link_index)
        anomalies = anomaly_inputs(semantic, int(row.received_at_us), row.link_index)
        for item_index, item in enumerate((*points, *anomalies)):
            value = item.value
            category = getattr(item, "category", getattr(item, "kind", "event"))
            severity_value = item.severity or 0
            severity = (
                "critical"
                if severity_value >= 5
                else "error"
                if severity_value >= 4
                else "warning"
                if severity_value >= 3
                else "info"
            )
            output.append(
                {
                    "id": (
                        f"{row.pico_boot_id}:{row.transport_sequence}:{category}:"
                        f"{item.slot if item.slot is not None else 'bridge'}:{item_index}"
                    ),
                    "at": row.server_received_at,
                    "linkIndex": row.link_index,
                    "slot": item.slot,
                    "pullPercent": value if category == "pull_pct" and isinstance(value, int) else None,
                    "pressure": value if category == "pressure" and isinstance(value, int) else None,
                    "motion": str(value) if category in ("motion", "state_change") else None,
                    "kind": category,
                    "label": category.replace("_", " "),
                    "severity": severity,
                    "source": item.source,
                    "anomaly": severity_value >= 3,
                    "missingData": category == "missing_data",
                }
            )
    start = from_time or (output[0]["at"] if output else datetime.now())
    end = to_time or (output[-1]["at"] if output else start)
    return {"points": output, "from": start, "to": end, "downsampled": downsampled}


@router.get("/{device_id}/metrics", response_model=list[MetricPoint])
async def metrics(
    device_id: str,
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    _: User | None = ReadAccess,
):
    await _device(db, device_id)
    rows = (
        await db.execute(
            select(BMCUBinaryDiagnostic)
            .where(BMCUBinaryDiagnostic.device_id == device_id)
            .order_by(BMCUBinaryDiagnostic.recorded_at.desc())
            .limit(limit)
        )
    ).scalars()
    tag_fields = {
        4: "heapFreeBytes",
        7: "temperatureC",
        11: "loopDelayUs",
        12: "loopGapAvgUs",
        13: "loopGapP95Us",
        14: "loopGapP99Us",
        17: "wifiRssiDbm",
        28: "ackAgeMs",
        31: "transportEncodeAvgUs",
        34: "replayPending",
        36: "transportSendAvgUs",
        37: "transportSendMaxUs",
        40: "journalBytes",
        49: "gcLastUs",
        50: "gcMaxUs",
    }
    output = []
    for row in rows:
        point = {"at": row.recorded_at}
        values = {item.tag: typed_tlv_value(item) for item in decode_tlvs(row.payload)}
        for tag, field in tag_fields.items():
            point[field] = values.get(tag)
        if point.get("temperatureC") is not None:
            point["temperatureC"] = point["temperatureC"] / 1000
        point["uartBacklog"] = max(values.get(64, 0), values.get(72, 0)) if values else None
        point["uartBacklogMax"] = max(values.get(65, 0), values.get(73, 0)) if values else None
        point["uartErrors"] = sum(values.get(tag, 0) for tag in (67, 68, 75, 76, 81, 83))
        point["uartDrainBytes"] = values.get(80, 0) + values.get(82, 0)
        point["uartOverflowTotal"] = values.get(81, 0) + values.get(83, 0)
        point["uartServiceDelayUs"] = max(values.get(71, 0), values.get(79, 0))
        point["uartCrcErrorsTotal"] = values.get(67, 0) + values.get(75, 0)
        point["uartSequenceGapsTotal"] = values.get(69, 0) + values.get(77, 0)
        output.append(point)
    return output


@router.get("/{device_id}/logs")
async def logs(
    device_id: str,
    severity: int | None = Query(None, ge=0, le=5),
    component: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _: User | None = ReadAccess,
):
    await _device(db, device_id)
    query = select(BMCUBinaryLog).where(BMCUBinaryLog.device_id == device_id)
    if severity is not None:
        query = query.where(BMCUBinaryLog.severity >= severity)
    if component:
        query = query.where(BMCUBinaryLog.component == component[:40])
    rows = (await db.execute(query.order_by(BMCUBinaryLog.recorded_at.desc()).limit(limit))).scalars()
    return [
        {
            "transport_sequence": row.transport_sequence,
            "recorded_at": row.recorded_at,
            "uptime_ms": row.uptime_ms,
            "severity": row.severity,
            "component": row.component,
            "message": row.message,
            "detail_hex": row.detail.hex(),
        }
        for row in rows
    ]


@router.post("/{device_id}/control")
async def control(
    device_id: str,
    body: ControlRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = ControlAccess,
):
    row = await _device(db, device_id)
    session = binary_transport_server.registry.get(device_id)
    if session is None:
        raise HTTPException(409, "BMCU Monitor is offline")
    try:
        arguments = bytes.fromhex(body.arguments_hex)
    except ValueError as exc:
        raise HTTPException(422, "arguments_hex is invalid") from exc
    if len(arguments) > 128:
        raise HTTPException(422, "arguments exceed 128 bytes")
    known_link = (
        await db.execute(
            select(BMCUBinaryLink.id).where(
                BMCUBinaryLink.device_id == device_id,
                BMCUBinaryLink.link_index == body.link_index,
            )
        )
    ).scalar_one_or_none()
    if known_link is None:
        raise HTTPException(422, "unknown link_index")
    async with binary_transport_server.control_lock:
        await db.refresh(row)
        sequence = int(row.control_sequence) + 1
        row.control_sequence = u64_decimal(sequence)
        await db.commit()
        await session.send_control(
            link_index=body.link_index,
            command_sequence=sequence,
            ttl_ms=body.ttl_ms,
            command=body.command,
            arguments=arguments,
        )
    return {"command_sequence": row.control_sequence}
