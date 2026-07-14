"""BMCU Link ingest + read API routes (observe-only adapter)."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled, is_auth_enabled, verify_websocket_token
from backend.app.core.database import async_session, get_db
from backend.app.core.permissions import Permission
from backend.app.models.bmcu_link_device import BMCULinkDevice
from backend.app.models.bmcu_link_event import BMCULinkEvent
from backend.app.models.user import User
from backend.app.schemas.bmcu_link import (
    BMCULinkDeviceResponse,
    BMCULinkEnvelope,
    BMCULinkEventResponse,
    BMCULinkIngestResponse,
)
from backend.app.services.bmcu_link import bmcu_link_enabled, bmcu_link_service, get_enum_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bmcu-link", tags=["bmcu-link"])

# Mirrors backend/app/api/routes/websocket.py close-code conventions
# (private-use range 4000-4999 per RFC 6455).
_WS_CLOSE_UNAUTHORIZED = 4401
_WS_CLOSE_DISABLED = 4404


def _parse_envelopes(payload) -> list[BMCULinkEnvelope]:
    if isinstance(payload, list):
        return [BMCULinkEnvelope.model_validate(item) for item in payload]
    return [BMCULinkEnvelope.model_validate(payload)]


@router.websocket("/ws")
async def bmcu_link_websocket(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """Ingest WebSocket for the Pico bridge.

    Auth mirrors /ws: token verified BEFORE accept() (close 4401), and the
    feature toggle is checked before accept too (close 4404).
    """
    if not bmcu_link_enabled():
        await websocket.close(code=_WS_CLOSE_DISABLED)
        return

    try:
        async with async_session() as db:
            auth_required = await is_auth_enabled(db)
    except Exception:  # fail-closed like websocket.py
        logger.error("BMCU Link WS auth probe failed; refusing connection", exc_info=True)
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return

    if auth_required:
        if not token:
            await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
            return
        principal = await verify_websocket_token(token)
        if principal is None:
            await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
            return

    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
                if isinstance(payload, list) and len(payload) > bmcu_link_service.MAX_BATCH:
                    await websocket.send_json(
                        {"type": "error", "detail": f"batch exceeds {bmcu_link_service.MAX_BATCH} envelopes"}
                    )
                    continue
                envelopes = _parse_envelopes(payload)
            except (ValueError, ValidationError) as e:
                await websocket.send_json({"type": "error", "detail": str(e)[:500]})
                continue
            result = await bmcu_link_service.ingest(envelopes)
            await websocket.send_json(
                {"type": "ack", "accepted": result.accepted, "deduplicated": result.deduplicated}
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("BMCU Link WS connection error")


@router.post("/ingest", response_model=BMCULinkIngestResponse)
async def ingest_envelopes(
    body: BMCULinkEnvelope | list[BMCULinkEnvelope],
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    if not bmcu_link_enabled():
        raise HTTPException(status_code=404, detail="BMCU Link is disabled")
    envelopes = body if isinstance(body, list) else [body]
    if len(envelopes) > bmcu_link_service.MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"batch exceeds {bmcu_link_service.MAX_BATCH} envelopes")
    return await bmcu_link_service.ingest(envelopes)


@router.get("/devices")
async def list_devices(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    if not bmcu_link_enabled():
        return {"enabled": False, "devices": []}
    result = await db.execute(select(BMCULinkDevice).order_by(BMCULinkDevice.device_id))
    devices = [BMCULinkDeviceResponse.model_validate(d) for d in result.scalars().all()]
    return {"enabled": True, "devices": devices}


@router.get("/devices/{device_id}", response_model=BMCULinkDeviceResponse)
async def get_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    result = await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.get("/devices/{device_id}/events", response_model=list[BMCULinkEventResponse])
async def list_device_events(
    device_id: str,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    query = select(BMCULinkEvent).where(BMCULinkEvent.device_id == device_id)
    if kind:
        query = query.where(BMCULinkEvent.kind == kind)
    query = query.order_by(BMCULinkEvent.server_received_at.desc(), BMCULinkEvent.id.desc())
    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/devices/{device_id}/transactions")
async def list_device_transactions(
    device_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    """Recent printer transactions, grouped by transaction_id in Python over
    a windowed query (transactions span only a handful of events each)."""
    query = (
        select(BMCULinkEvent)
        .where(BMCULinkEvent.device_id == device_id)
        .where(BMCULinkEvent.transaction_id.isnot(None))
        .order_by(BMCULinkEvent.server_received_at.desc(), BMCULinkEvent.id.desc())
        .limit(limit * 20)  # window: assume <=20 events per transaction on average
    )
    result = await db.execute(query)
    groups: dict[str, list[BMCULinkEvent]] = {}
    order: list[str] = []
    for event in result.scalars().all():
        txn = event.transaction_id
        if txn not in groups:
            if len(order) >= limit:
                continue
            groups[txn] = []
            order.append(txn)
        groups[txn].append(event)
    return [
        {
            "transaction_id": txn,
            "events": [BMCULinkEventResponse.model_validate(e) for e in reversed(groups[txn])],
        }
        for txn in order
    ]


@router.get("/enums")
async def get_enums(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    return get_enum_registry()
