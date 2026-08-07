"""Read/control API for authenticated binary BMCU Monitors."""

import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
    GuideNote,
    GuideThreshold,
    LinkSnapshot,
    MetricPoint,
    MonitorDetail,
    MonitorGuide,
    MonitorSummary,
    TimelineResponse,
)
from backend.app.services.bmcu_binary.bmcu_decoder import (
    SEMANTIC_KINDS,
    BMCUStatus,
    decode_semantic,
    decode_wire_frame,
)
from backend.app.services.bmcu_binary.constants import MessageType, StateField, state_field_name
from backend.app.services.bmcu_binary.guide import READING_NOTES, THRESHOLDS
from backend.app.services.bmcu_binary.messages import decode_tlvs, typed_tlv_value
from backend.app.services.bmcu_binary.sampling import sample_strides
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
        # Null, not 0: the warning/critical aggregation this field is meant to
        # carry is not implemented, and 0 would state that the device reports
        # nothing wrong. It reports plenty; nobody has counted it.
        anomalyCount=None,
    )


@router.get(
    "",
    response_model=list[MonitorSummary],
    summary="Which bridges exist, and is each one connected right now?",
)
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


@router.get(
    "/guide",
    response_model=MonitorGuide,
    summary="How should these numbers be read?",
    description=(
        "Reading notes and thresholds that OpenAPI has no place for -- which null means unknown, "
        "which counters are only meaningful as a delta, which field actually says whether the loader "
        "view is current. Static; no device is involved. Declared before /{device_id} so the literal "
        "path wins the match."
    ),
)
async def guide(_: User | None = ReadAccess):
    return MonitorGuide(
        readingNotes=[GuideNote(**note) for note in READING_NOTES],
        thresholds=[
            GuideThreshold(
                metric=item["metric"],
                warnAbove=item.get("warn_above"),
                warnBelow=item.get("warn_below"),
                unit=item.get("unit"),
                note=item["note"],
            )
            for item in THRESHOLDS
        ],
    )


@router.get(
    "/{device_id}",
    response_model=MonitorDetail,
    summary="What is each loader on this bridge doing, and how old is that answer?",
)
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
    now = datetime.now(UTC).replace(tzinfo=None)
    for stored in stored_links:
        key = (device_id, stored.link_index)
        value = persistence.current_state.get(key)
        if not isinstance(value, BMCUStatus):
            value = None
        if value is None:
            # Nothing has ever been decoded for this link. Reporting `stale`
            # here made that indistinguishable from a link that went quiet, and
            # every loader field then had to carry a stand-in value -- 0 for the
            # mask, 0 faults -- that reads exactly like a healthy empty loader.
            snapshot, state, faults, age_s = None, "no_data", None, None
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
                # activeMask is filament presence (online_mask), not the
                # hardware channel mask; see BMCUStatus for the distinction.
                activeMask=snapshot["online_mask"] if snapshot else None,
                motion=snapshot["motion"] if snapshot else None,
                pullPercent=snapshot["pull_pct"] if snapshot else None,
                pressure=snapshot["pressure"] if snapshot else None,
                faultCount=faults,
                # How old the values above are. The device row's last_seen_at
                # reports the transport, which keeps advancing on EVENT and
                # diagnostic traffic while STATUS is starved, so it cannot say
                # whether this loader view is current. Without the age the page
                # painted a snapshot from the previous day as the live slot.
                statusAgeS=None if age_s is None else round(age_s, 1),
                lastSeenAt=None if age_s is None else now - timedelta(seconds=age_s),
            )
        )
    return MonitorDetail(**_summary(row, len(stored_links)).model_dump(), firstSeenAt=row.first_seen_at, links=links)


async def _sampled_window(db, conditions, limit):
    """Rows spanning the whole window, at most ``limit`` of them.

    Sampling is per BMCU kind (see ``sampling.sample_strides``) and anchored at
    the newest row of each kind, so the present is always represented even when
    the stride does not divide the window evenly.
    """
    counts = dict(
        (
            await db.execute(
                select(BMCUBinaryRecord.bmcu_kind, func.count()).where(*conditions).group_by(BMCUBinaryRecord.bmcu_kind)
            )
        ).all()
    )
    ascending = (BMCUBinaryRecord.server_received_at, BMCUBinaryRecord.transport_sequence)
    total = sum(counts.values())
    if total == 0:
        return [], False
    if total <= limit:
        return (
            await db.execute(select(BMCUBinaryRecord).where(*conditions).order_by(*ascending))
        ).scalars().all(), False
    strides = sample_strides(counts, limit)
    numbered = (
        select(
            BMCUBinaryRecord,
            func.row_number()
            .over(
                partition_by=BMCUBinaryRecord.bmcu_kind,
                order_by=(
                    BMCUBinaryRecord.server_received_at.desc(),
                    BMCUBinaryRecord.transport_sequence.desc(),
                ),
            )
            .label("rn"),
        )
        .where(*conditions)
        .subquery()
    )
    record = aliased(BMCUBinaryRecord, numbered)
    known = [(numbered.c.bmcu_kind == kind, value) for kind, value in strides.items() if kind is not None]
    stride = case(*known, else_=strides.get(None, 1)) if known else strides.get(None, 1)
    # Fetch newest-first so the hard LIMIT can only ever drop the oldest end of
    # the sample. Every kind is guaranteed at least one row, so the sample can
    # exceed `limit` by up to one row per kind, and trimming that overshoot from
    # the newest end would reintroduce the bug this function exists to fix.
    rows = (
        (
            await db.execute(
                select(record)
                .where((numbered.c.rn - 1) % stride == 0)
                .order_by(numbered.c.server_received_at.desc(), numbered.c.transport_sequence.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows)), True


@router.get(
    "/{device_id}/timeline",
    response_model=TimelineResponse,
    summary="What did the loaders do over a window?",
    description=(
        "Points are decoded from retained BMCU frames. A bounded window (both `from` and `to`) is "
        "thinned so the answer spans the whole window and reaches the present; `downsampled` says "
        "whether that happened. Thinning is by row stride per BMCU kind, not by a time interval, so "
        "the gap between consecutive points is not a fixed resolution."
    ),
)
async def timeline(
    device_id: str,
    from_time: datetime | None = Query(None, alias="from"),
    to_time: datetime | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    _: User | None = ReadAccess,
):
    await _device(db, device_id)
    conditions = [
        BMCUBinaryRecord.device_id == device_id,
        BMCUBinaryRecord.message_type == MessageType.BMCU_FRAME,
        # Kinds this decoder does not model render nothing, and on the live
        # bridge they are a third of the stored frames. Excluding them here
        # keeps them from spending the row budget on invisible rows.
        BMCUBinaryRecord.bmcu_kind.in_(SEMANTIC_KINDS),
    ]
    if from_time:
        conditions.append(BMCUBinaryRecord.server_received_at >= from_time)
    if to_time:
        conditions.append(BMCUBinaryRecord.server_received_at <= to_time)
    if from_time is not None and to_time is not None:
        # A bounded window is a chart request: thin the window out so the answer
        # spans it and reaches the present. Truncating at `limit` instead
        # returned the first minutes of the range, which is why every range
        # button on the BMCU Link page rendered hours-old data.
        rows, downsampled = await _sampled_window(db, conditions, limit)
    else:
        # A one-sided `from`/`to` keeps ascending "first rows after the bound"
        # semantics, which is what such a caller is paginating with. Without any
        # bound the useful view is the most recent one.
        explicit_window = from_time is not None or to_time is not None
        if explicit_window:
            order = (BMCUBinaryRecord.server_received_at, BMCUBinaryRecord.transport_sequence)
        else:
            order = (BMCUBinaryRecord.server_received_at.desc(), BMCUBinaryRecord.transport_sequence.desc())
        query = select(BMCUBinaryRecord).where(*conditions)
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
            # STATE_CHANGE says *which* field moved; without it every state
            # change reached the client as an identical "state change" and a
            # latching motion_fault could not be told from a slot selection
            # (2026-08-07). None = a field this build has no name for, which
            # is a different answer from "not a state change".
            field = getattr(item, "field", None)
            field_name = state_field_name(field)
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
                    # The motion enum as a number. `str(value)` here put "2" on
                    # the wire and made every consumer parse it back.
                    #
                    # A state_change only carries a motion value when the field
                    # that moved IS motion; filing every state change here put
                    # `motion_fault=1` on the wire as "motion state 1".
                    "motion": (
                        value
                        if isinstance(value, int)
                        and (category == "motion" or (category == "state_change" and field == StateField.MOTION))
                        else None
                    ),
                    "field": field,
                    "fieldName": field_name,
                    "kind": category,
                    "label": f"{category.replace('_', ' ')}: {field_name}"
                    if field_name
                    else category.replace("_", " "),
                    "severity": severity,
                    "source": item.source,
                    "anomaly": severity_value >= 3,
                    "missingData": category == "missing_data",
                }
            )
    start = from_time or (output[0]["at"] if output else datetime.now())
    end = to_time or (output[-1]["at"] if output else start)
    return {"points": output, "from": start, "to": end, "downsampled": downsampled}


async def _sampled_diagnostics(db, conditions, limit, sample):
    """Newest-first diagnostics that still cover the requested window.

    Diagnostics arrive about every 15 s, so a 24 h range holds far more samples
    than a chart can carry; without thinning, `limit` alone answered every range
    with the same newest two hours. Thinning applies only to a bounded window: a
    caller that named no window is asking for the latest samples, not for a
    sample of the whole retention.
    """
    newest_first = BMCUBinaryDiagnostic.recorded_at.desc()
    if not sample:
        return (
            (await db.execute(select(BMCUBinaryDiagnostic).where(*conditions).order_by(newest_first).limit(limit)))
            .scalars()
            .all()
        )
    total = (await db.execute(select(func.count()).select_from(BMCUBinaryDiagnostic).where(*conditions))).scalar() or 0
    if total <= limit:
        return (
            (await db.execute(select(BMCUBinaryDiagnostic).where(*conditions).order_by(newest_first))).scalars().all()
        )
    stride = max(1, -(-total // limit))
    numbered = (
        select(BMCUBinaryDiagnostic, func.row_number().over(order_by=newest_first).label("rn"))
        .where(*conditions)
        .subquery()
    )
    row = aliased(BMCUBinaryDiagnostic, numbered)
    return (
        (
            await db.execute(
                select(row)
                .where((numbered.c.rn - 1) % stride == 0)
                .order_by(numbered.c.recorded_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


@router.get(
    "/{device_id}/metrics",
    response_model=list[MetricPoint],
    summary="How healthy is the bridge itself over a window?",
    description="Bridge-side diagnostics, not loader state. Absent counters are omitted rather than zeroed.",
)
async def metrics(
    device_id: str,
    from_time: datetime | None = Query(None, alias="from"),
    to_time: datetime | None = Query(None, alias="to"),
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    _: User | None = ReadAccess,
):
    await _device(db, device_id)
    conditions = [BMCUBinaryDiagnostic.device_id == device_id]
    if from_time:
        conditions.append(BMCUBinaryDiagnostic.recorded_at >= from_time)
    if to_time:
        conditions.append(BMCUBinaryDiagnostic.recorded_at <= to_time)
    rows = await _sampled_diagnostics(db, conditions, limit, from_time is not None and to_time is not None)
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


@router.get(
    "/{device_id}/logs",
    summary="What did the bridge log, newest first?",
)
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


@router.post(
    "/{device_id}/control",
    summary="Send one CONTROL command to a link.",
)
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
