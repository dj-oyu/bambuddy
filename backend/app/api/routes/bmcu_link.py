"""Settings compatibility reads backed exclusively by BMB1 persistence."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.bmcu_binary import BMCUBinaryDevice, BMCUBinaryLog, BMCUBinaryRecord
from backend.app.models.user import User
from backend.app.services.bmcu_binary.server import binary_transport_server

router = APIRouter(prefix="/bmcu-link", tags=["bmcu-link"])
ReadAccess = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ)


def _device(row, count=0):
    return {
        "id": row.id, "device_id": row.device_id, "name": row.device_id,
        "firmware": row.firmware, "protocol_min": 1, "protocol_max": 1,
        "capabilities": 0, "mode": "production_monitor",
        "link_state": "online" if binary_transport_server.registry.get(row.device_id) else "offline",
        "pico_boot_session": row.pico_boot_id, "bmcu_boot_session": 0,
        "last_seen_at": row.last_seen_at, "first_seen_at": row.first_seen_at,
        "last_status": None, "envelope_count": count, "dropped_count": 0,
        "created_at": row.first_seen_at, "control_key_set_at": None,
    }


@router.get("/connection-info")
async def connection_info(_: User | None = ReadAccess):
    return {
        "auth_enabled": True, "telemetry_scope": "BMB1-HMAC",
        "port": settings.bmcu_binary_port,
        "endpoints": [{"ip": settings.bmcu_binary_host,
                       "tcp_url": f"tcp://{settings.bmcu_binary_host}:{settings.bmcu_binary_port}"}],
    }


@router.get("/devices")
async def devices(db: AsyncSession = Depends(get_db), _: User | None = ReadAccess):
    rows = (await db.execute(select(BMCUBinaryDevice).order_by(BMCUBinaryDevice.device_id))).scalars().all()
    counts = dict((await db.execute(
        select(BMCUBinaryRecord.device_id, func.count(BMCUBinaryRecord.id)).group_by(BMCUBinaryRecord.device_id)
    )).all())
    return {"enabled": settings.bmcu_binary_enabled,
            "devices": [_device(row, counts.get(row.device_id, 0)) for row in rows]}


@router.get("/devices/{device_id}")
async def device(device_id: str, db: AsyncSession = Depends(get_db), _: User | None = ReadAccess):
    row = (await db.execute(select(BMCUBinaryDevice).where(
        BMCUBinaryDevice.device_id == device_id
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "BMCU Monitor not found")
    count = (await db.execute(select(func.count(BMCUBinaryRecord.id)).where(
        BMCUBinaryRecord.device_id == device_id
    ))).scalar() or 0
    return _device(row, count)


@router.get("/devices/{device_id}/events")
async def events(
    device_id: str, kind: str | None = None, limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db),
    _: User | None = ReadAccess,
):
    query = select(BMCUBinaryLog).where(BMCUBinaryLog.device_id == device_id)
    if kind:
        query = query.where(BMCUBinaryLog.component == kind)
    rows = (await db.execute(query.order_by(
        BMCUBinaryLog.recorded_at.desc(), BMCUBinaryLog.id.desc()
    ).offset(offset).limit(limit))).scalars()
    return [{
        "id": row.id, "device_id": row.device_id, "link_id": "monitor",
        "pico_boot_session": row.pico_boot_id, "bmcu_boot_session": 0,
        "uart_sequence": 0, "transport_sequence": int(row.transport_sequence),
        "kind": "pico_log", "kind_id": 20, "protocol": 1,
        "received_at_us": int(row.uptime_ms) * 1000, "received_at": None,
        "server_received_at": row.recorded_at, "transaction_id": None,
        "data": json.dumps({"severity": row.severity, "component": row.component,
                            "message": row.message, "detail_hex": row.detail.hex()}),
    } for row in rows]


@router.get("/devices/{device_id}/transactions")
async def transactions(device_id: str, _: User | None = ReadAccess):
    return []


@router.get("/enums")
async def enums(_: User | None = ReadAccess):
    return {"registry_version": 1}
