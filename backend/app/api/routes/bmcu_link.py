"""BMCU Link ingest + read API routes (observe-only adapter)."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import (
    RequireAdminIfAuthEnabled,
    RequirePermissionIfAuthEnabled,
    is_auth_enabled,
    require_permission_if_auth_enabled,
    verify_websocket_token,
)
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
    BMCULinkRejected,
)
from backend.app.services.bmcu_link import bmcu_link_enabled, bmcu_link_service, get_enum_registry
from backend.app.services.long_lived_tokens import BMCU_LINK_TELEMETRY_SCOPE, verify_token as verify_long_lived_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bmcu-link", tags=["bmcu-link"])

# Mirrors backend/app/api/routes/websocket.py close-code conventions
# (private-use range 4000-4999 per RFC 6455).
_WS_CLOSE_UNAUTHORIZED = 4401
_WS_CLOSE_DISABLED = 4404
# 500 envelopes * ~1 KiB each with headroom; ingest can be unauthenticated,
# so cap the buffered body instead of trusting Content-Length.
_MAX_INGEST_BODY = 2 * 1024 * 1024


def _extract_transport_sequence(item) -> int | None:
    try:
        v = item.get("link", {}).get("transport_sequence")
        return int(v) if v is not None else None
    except Exception:
        return None


def _parse_envelopes_partial(payload) -> tuple[list[tuple[int, BMCULinkEnvelope]], list[BMCULinkRejected]]:
    """Partial-accept parse (issue #2): invalid items become `rejected`
    entries (0-based batch index, non-retryable) instead of failing the
    whole batch; valid items keep their original index for error mapping."""
    items = payload if isinstance(payload, list) else [payload]
    parsed: list[tuple[int, BMCULinkEnvelope]] = []
    rejected: list[BMCULinkRejected] = []
    for idx, item in enumerate(items):
        try:
            parsed.append((idx, BMCULinkEnvelope.model_validate(item)))
        except ValidationError:
            rejected.append(
                BMCULinkRejected(
                    index=idx,
                    transport_sequence=_extract_transport_sequence(item) if isinstance(item, dict) else None,
                    code="validation_error",
                    retryable=False,
                )
            )
    return parsed, rejected


async def _ingest_partial(payload) -> BMCULinkIngestResponse:
    parsed, rejected = _parse_envelopes_partial(payload)
    result = await bmcu_link_service.ingest([env for _, env in parsed])
    # Service rejected-indices are relative to the surviving list; map back
    # to original batch positions.
    for r in result.rejected:
        r.index = parsed[r.index][0]
    result.rejected = sorted(rejected + result.rejected, key=lambda r: r.index)
    return result


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
        # Bearer header is preferred (keeps the token out of URLs/logs);
        # query token remains supported for clients without header control.
        auth_header = websocket.headers.get("authorization", "")
        if not token and auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
        if not token:
            await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
            return
        # Two accepted credentials (issue #2): the UI's ephemeral WS token,
        # or a device-scoped long-lived telemetry token (`bblt_...`, scope
        # bmcu_link:telemetry) provisioned to the Pico bridge. The telemetry
        # token is valid ONLY here and on POST /ingest — never elsewhere.
        principal = await verify_websocket_token(token)
        if principal is None and token.startswith("bblt_"):
            async with async_session() as db:
                record = await verify_long_lived_token(db, token, scope=BMCU_LINK_TELEMETRY_SCOPE)
            principal = "" if record is not None else None
        if principal is None:
            await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
            return

    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except ValueError as e:
                # Whole message unreadable — the only non-partial error path.
                await websocket.send_json({"type": "error", "detail": str(e)[:500]})
                continue
            if isinstance(payload, list) and len(payload) > bmcu_link_service.MAX_BATCH:
                await websocket.send_json(
                    {"type": "error", "detail": f"batch exceeds {bmcu_link_service.MAX_BATCH} envelopes"}
                )
                continue
            result = await _ingest_partial(payload)
            await websocket.send_json(
                {
                    "type": "ack",
                    "accepted": result.accepted,
                    "deduplicated": result.deduplicated,
                    "rejected": [r.model_dump() for r in result.rejected],
                    # Replay-buffer watermark (PICO_BAMBUDDY_ENVELOPE.md §2):
                    # the bridge may discard only envelopes at or below these
                    # keys. Lags accepted counts because rows batch-flush.
                    "persisted": [k.model_dump() for k in result.persisted],
                }
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("BMCU Link WS connection error")


# Fallback checker for POST /ingest when the caller is not presenting a
# device telemetry token (browser JWT / API key paths keep working).
_ingest_permission_checker = require_permission_if_auth_enabled(Permission.INVENTORY_UPDATE)


async def _require_telemetry_or_permission(request: Request) -> None:
    """POST /ingest auth (issue #2): accept a device-scoped telemetry token
    (``Authorization: Bearer bblt_...``, scope bmcu_link:telemetry) as an
    alternative to the INVENTORY_UPDATE permission path. The telemetry token
    grants nothing outside the two ingest endpoints."""
    from fastapi.security import HTTPAuthorizationCredentials

    auth_header = request.headers.get("authorization", "")
    bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    if bearer and bearer.startswith("bblt_"):
        async with async_session() as db:
            if not await is_auth_enabled(db):
                return None
            record = await verify_long_lived_token(db, bearer, scope=BMCU_LINK_TELEMETRY_SCOPE)
        if record is not None:
            return None
        raise HTTPException(status_code=401, detail="Invalid telemetry token")
    credentials = (
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=bearer) if bearer else None
    )
    await _ingest_permission_checker(
        credentials=credentials, x_api_key=request.headers.get("x-api-key")
    )
    return None


@router.post("/ingest", response_model=BMCULinkIngestResponse)
async def ingest_envelopes(
    request: Request,
    _: None = Depends(_require_telemetry_or_permission),
):
    """Ingest one envelope, a JSON array, or NDJSON (one envelope per line,
    Content-Type: application/x-ndjson) — the WebSocket-unavailable fallback
    from PICO_BAMBUDDY_TRANSPORT.md §6. Partial accept per issue #2."""
    if not bmcu_link_enabled():
        raise HTTPException(status_code=404, detail="BMCU Link is disabled")
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    if len(body) > _MAX_INGEST_BODY:
        raise HTTPException(status_code=413, detail="request body too large")
    if "ndjson" in content_type:
        payload = []
        for line in body.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload.append(json.loads(line))
            except ValueError:
                payload.append(None)  # rejected as validation_error by index
    else:
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {str(e)[:200]}")
    if isinstance(payload, list) and len(payload) > bmcu_link_service.MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"batch exceeds {bmcu_link_service.MAX_BATCH} envelopes")
    return await _ingest_partial(payload)


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


# --- CONTROL key provisioning (firmware issue #2) --------------------------
#
# Admin-only. The plaintext key is returned exactly once for entry into the
# Pico commissioning UI, stored Fernet-encrypted, and never logged. CONTROL
# command sending is NOT wired up yet (firmware Phase 5 is telemetry-only);
# provisioning exists so the shared secret and sequence state are ready.


@router.post("/devices/{device_id}/control-key", status_code=201)
async def provision_control_key(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAdminIfAuthEnabled(),
):
    """Generate (or rotate) the device's CONTROL key. Response contains the
    plaintext key once; it is never retrievable again."""
    if not bmcu_link_enabled():
        raise HTTPException(status_code=404, detail="BMCU Link is disabled")
    result = await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    from backend.app.services.bmcu_link_control import ControlContractError, set_control_key

    rotated = device.control_key_encrypted is not None
    try:
        key = await set_control_key(db, device)
    except ControlContractError as e:
        # e.g. at-rest encryption unavailable — refuse rather than store plaintext
        raise HTTPException(status_code=503, detail=str(e))
    logger.info("BMCU Link control key %s for device %s", "rotated" if rotated else "provisioned", device_id)
    return {
        "device_id": device_id,
        "control_key": key,  # shown once, never persisted in plaintext
        "rotated": rotated,
        "set_at": device.control_key_set_at.isoformat() if device.control_key_set_at else None,
    }


@router.delete("/devices/{device_id}/control-key", status_code=204)
async def revoke_control_key_route(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAdminIfAuthEnabled(),
):
    """Revoke the device's CONTROL key. CONTROL becomes impossible until a
    new key is provisioned (fail-safe)."""
    if not bmcu_link_enabled():
        raise HTTPException(status_code=404, detail="BMCU Link is disabled")
    result = await db.execute(select(BMCULinkDevice).where(BMCULinkDevice.device_id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    from backend.app.services.bmcu_link_control import revoke_control_key

    await revoke_control_key(db, device)
    logger.info("BMCU Link control key revoked for device %s", device_id)
