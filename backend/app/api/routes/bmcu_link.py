"""Settings compatibility reads backed exclusively by BMB1 persistence."""

import json
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.bmcu_binary import (
    BMCUBinaryDevice,
    BMCUBinaryLog,
    BMCUBinaryLossRange,
    BMCUBinaryRecord,
)
from backend.app.models.user import User
from backend.app.services.bmcu_binary.bmcu_decoder import BMCUStatus
from backend.app.services.bmcu_binary.provisioning import (
    generate_device_key,
    key_file_path,
    load_key_file,
    validate_device_id,
)
from backend.app.services.bmcu_binary.server import binary_transport_server
from backend.app.services.bmcu_binary.state_view import status_snapshot
from backend.app.services.network_utils import get_all_interface_ips

router = APIRouter(prefix="/bmcu-link", tags=["bmcu-link"])
ReadAccess = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ)
ProvisionAccess = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE)
_DEFAULT_PROTOCOL = 1


class RotateProvisioningKey(BaseModel):
    device_id: str = Field(min_length=1, max_length=63)


def _connection_endpoints() -> list[dict[str, str]]:
    host = settings.bmcu_binary_host
    if host not in ("0.0.0.0", "::"):
        addresses = [host]
    else:
        addresses = []
        for interface in get_all_interface_ips():
            address = interface.get("ip")
            if address and not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    return [{"ip": address, "tcp_url": f"tcp://{address}:{settings.bmcu_binary_port}"} for address in addresses]


def _link_state(device_id):
    """Lowest-link-index HELLO/STATUS pair known for the device, if any."""
    persistence = binary_transport_server.persistence
    hellos = sorted((key for key in persistence.last_hello if key[0] == device_id), key=lambda item: item[1])
    hello = persistence.last_hello[hellos[0]] if hellos else None
    statuses = sorted(
        (
            key
            for key, value in persistence.current_state.items()
            if key[0] == device_id and isinstance(value, BMCUStatus)
        ),
        key=lambda item: item[1],
    )
    if not statuses:
        return hello, None
    key = statuses[0]
    value = persistence.current_state[key]
    age_s = time.monotonic() - persistence.current_state_seen.get(key, time.monotonic())
    return hello, status_snapshot(value, age_s)


def _control_key_set_at(device_id):
    """Best available timestamp: the mtime of the shared key file, reported only
    for devices whose key actually lives in that file. Env-provisioned keys have
    no timestamp at all, and the shared file has no per-device one, so anything
    else would be a fabricated per-device value."""
    try:
        if device_id not in load_key_file():
            return None
        return datetime.fromtimestamp(key_file_path().stat().st_mtime, tz=UTC)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _device(row, count=0, drops=0):
    hello, last_status = _link_state(row.device_id)
    protocol = hello.protocol_version if hello else None
    return {
        "id": row.id,
        "device_id": row.device_id,
        "name": row.device_id,
        "firmware": row.firmware,
        "protocol_min": protocol if protocol is not None else _DEFAULT_PROTOCOL,
        "protocol_max": protocol if protocol is not None else _DEFAULT_PROTOCOL,
        "capabilities": hello.capabilities if hello else 0,
        "mode": "production_monitor",
        "link_state": "online" if binary_transport_server.registry.get(row.device_id) else "offline",
        "pico_boot_session": row.pico_boot_id,
        # No BMCU boot id exists on the wire; this counts observed BMCU HELLOs.
        "bmcu_boot_session": binary_transport_server.persistence.bmcu_hello_epoch.get(row.device_id, 0),
        "last_seen_at": row.last_seen_at,
        "first_seen_at": row.first_seen_at,
        "last_status": last_status,
        "envelope_count": count,
        "dropped_count": drops,
        "created_at": row.first_seen_at,
        "control_key_set_at": _control_key_set_at(row.device_id),
    }


async def _dropped_counts(db, device_id=None):
    # dropped_count is a zero-padded decimal String(20). Aggregate in SQL: this
    # table grows monotonically (hundreds of thousands of rows already), so
    # pulling every row through the ORM per request is not affordable. CAST to
    # INTEGER is exact for any realistic gap total (int64), and a single range
    # wide enough to overflow it would mean the sequence space itself wrapped.
    query = select(
        BMCUBinaryLossRange.device_id,
        func.sum(func.cast(BMCUBinaryLossRange.dropped_count, Integer)),
    ).group_by(BMCUBinaryLossRange.device_id)
    if device_id is not None:
        query = query.where(BMCUBinaryLossRange.device_id == device_id)
    return {row_device: int(dropped or 0) for row_device, dropped in (await db.execute(query)).all()}


@router.get("/connection-info")
async def connection_info(_: User | None = ReadAccess):
    return {
        "auth_enabled": True,
        "telemetry_scope": "BMB1-HMAC",
        "port": settings.bmcu_binary_port,
        "endpoints": _connection_endpoints(),
    }


@router.get("/provisioning")
async def provisioning(response: Response, _: User | None = ProvisionAccess):
    response.headers["Cache-Control"] = "no-store"
    return {
        "enabled": settings.bmcu_binary_enabled,
        "port": settings.bmcu_binary_port,
        "endpoints": _connection_endpoints(),
        "devices": [
            {"device_id": device_id, "key_hex": key.hex()}
            for device_id, key in sorted(binary_transport_server.provisioning_keys().items())
        ],
    }


@router.post("/provisioning/rotate")
async def rotate_provisioning_key(
    body: RotateProvisioningKey,
    response: Response,
    _: User | None = ProvisionAccess,
):
    response.headers["Cache-Control"] = "no-store"
    try:
        device_id = validate_device_id(body.device_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    key = generate_device_key()
    await binary_transport_server.set_device_key(device_id, key)
    return {
        "device_id": device_id,
        "key_hex": key.hex(),
        "rotated": True,
    }


@router.get("/devices")
async def devices(db: AsyncSession = Depends(get_db), _: User | None = ReadAccess):
    rows = (await db.execute(select(BMCUBinaryDevice).order_by(BMCUBinaryDevice.device_id))).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(BMCUBinaryRecord.device_id, func.count(BMCUBinaryRecord.id)).group_by(BMCUBinaryRecord.device_id)
            )
        ).all()
    )
    drops = await _dropped_counts(db)
    return {
        "enabled": settings.bmcu_binary_enabled,
        "devices": [_device(row, counts.get(row.device_id, 0), drops.get(row.device_id, 0)) for row in rows],
    }


@router.get("/devices/{device_id}")
async def device(device_id: str, db: AsyncSession = Depends(get_db), _: User | None = ReadAccess):
    row = (
        await db.execute(select(BMCUBinaryDevice).where(BMCUBinaryDevice.device_id == device_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "BMCU Monitor not found")
    count = (
        await db.execute(select(func.count(BMCUBinaryRecord.id)).where(BMCUBinaryRecord.device_id == device_id))
    ).scalar() or 0
    drops = await _dropped_counts(db, device_id)
    return _device(row, count, drops.get(device_id, 0))


@router.get("/devices/{device_id}/events")
async def events(
    device_id: str,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User | None = ReadAccess,
):
    query = select(BMCUBinaryLog).where(BMCUBinaryLog.device_id == device_id)
    if kind:
        query = query.where(BMCUBinaryLog.component == kind)
    rows = (
        await db.execute(
            query.order_by(BMCUBinaryLog.recorded_at.desc(), BMCUBinaryLog.id.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return [
        {
            "id": row.id,
            "device_id": row.device_id,
            "link_id": "monitor",
            "pico_boot_session": row.pico_boot_id,
            "bmcu_boot_session": 0,
            "uart_sequence": 0,
            "transport_sequence": int(row.transport_sequence),
            "kind": "pico_log",
            "kind_id": 20,
            "protocol": 1,
            "received_at_us": int(row.uptime_ms) * 1000,
            "received_at": None,
            "server_received_at": row.recorded_at,
            "transaction_id": None,
            "data": json.dumps(
                {
                    "severity": row.severity,
                    "component": row.component,
                    "message": row.message,
                    "detail_hex": row.detail.hex(),
                }
            ),
        }
        for row in rows
    ]


@router.get("/devices/{device_id}/transactions")
async def transactions(device_id: str, _: User | None = ReadAccess):
    return []


@router.get("/enums")
async def enums(_: User | None = ReadAccess):
    return {"registry_version": 1}
