"""Filament load/unload plan for a printer's queue (private fork).

The deferred-unload patch strips the sliced tail unload from every injected job
and lets the NEXT job's start G-code perform the swap. That makes "when does
filament actually move?" a property of the whole queue chain rather than of a
single row — and walking that chain needs each item's tray mapping, which
pending rows do not carry: ``ams_mapping`` stays NULL until the scheduler
computes it at dispatch.

So the queue UI cannot answer the question from the rows alone. This module
resolves the chain server-side: it plans the mapping the scheduler *would*
compute for each pending item (same matcher, nothing persisted), walks the
withheld-unload state forward, and reports where an unload or load actually
happens. The queue Gantt's hotend lane, its swap markers, and the queue-row
unload badges all render from this one answer.

Unknown is a first-class result. When a mapping can't be resolved the plan says
``None`` for that item's start-unload rather than ``False`` — "we don't know"
must not render as "nothing happens".
"""

import json
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings

logger = logging.getLogger(__name__)

# Chain states for "what is sitting in the hotend at this point".
#   ("empty",)            -> nothing loaded
#   ("known", [trays])    -> these trays are loaded
#   ("unknown",)          -> something is loaded but we can't name it
_EMPTY = ("empty", None)
_UNKNOWN = ("unknown", None)


def resolve_unload_mode(unload_edit: str | None, defer_unload: bool | None, gcode_injection: bool) -> str:
    """Collapse the tri-state legacy flag + the 4-way selector into one mode.

    Mirrors the dispatch-time resolution in ``print_scheduler._start_print`` so
    the plan and the actual G-code edit can never disagree; the scheduler
    imports this rather than keeping its own copy.

    Returns one of ``auto`` / ``start`` / ``end`` / ``none`` /
    ``start-less-defer`` (the legacy ``defer_unload=True`` without injection).
    """
    if unload_edit is not None:
        return unload_edit
    if defer_unload is True:
        return "auto" if gcode_injection else "start-less-defer"
    if defer_unload is False:
        return "end"
    return "auto"


def tail_is_deferred(mode: str, gcode_injection: bool) -> bool:
    """Whether the sliced "pull back filament to AMS" block gets stripped."""
    env_ok = os.environ.get("BAMBUDDY_DEFER_TAIL_UNLOAD", "1") != "0"
    if mode in ("none", "end"):
        return False
    if mode == "start-less-defer":
        return env_ok
    # 'auto' and 'start' both strip the tail; 'start' additionally forces a
    # pull-back at the head of its own job.
    return bool(gcode_injection) and env_ok


def _tray_label(ams_id: int, tray_id: int, is_external: bool) -> str:
    """Machine-side slot designator (``AMS0-B``), not a translated string."""
    if is_external:
        return "EXT"
    if ams_id >= 128:  # AMS-HT: one tray, addressed by unit
        return f"HT{ams_id}"
    return f"AMS{ams_id}-{chr(ord('A') + tray_id)}"


def _normalize(mapping_raw: str | None) -> list[int] | None:
    from backend.app.services.print_scheduler import PrintScheduler

    return PrintScheduler._normalize_ams_mapping(mapping_raw)


async def _withheld_entry(db: AsyncSession, printer_id: int) -> dict | None:
    from backend.app.services.print_scheduler import PrintScheduler

    key = f"{PrintScheduler._DEFERRED_UNLOAD_KEY_PREFIX}{printer_id}"
    result = await db.execute(select(Settings).where(Settings.key == key))
    row = result.scalar_one_or_none()
    if not row or not row.value:
        return None
    try:
        entry = json.loads(row.value)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


async def build_filament_plan(db: AsyncSession, printer_id: int) -> dict[str, Any]:
    """Resolve the whole load/unload chain for one printer's queue.

    Returns a JSON-ready dict. Never raises on a missing printer status or an
    unreadable 3MF — those degrade to ``trays: null`` / ``unload_at_start:
    null`` so the UI can say "unknown" instead of inventing a plan.
    """
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager

    status = printer_manager.get_status(printer_id)

    # tray catalogue: global tray id -> what is physically in that slot
    trays_by_id: dict[int, dict[str, Any]] = {}
    if status is not None:
        try:
            for f in scheduler._build_loaded_filaments(status):
                gid = f["global_tray_id"]
                trays_by_id[gid] = {
                    "tray": gid,
                    "ams_id": f["ams_id"],
                    "tray_id": f["tray_id"],
                    "type": f.get("type"),
                    "color": f.get("color"),
                    "label": _tray_label(f["ams_id"], f["tray_id"], f.get("is_external", False)),
                }
        except Exception as e:  # printer status shapes vary across models
            logger.warning("Filament plan: could not read loaded filaments on printer %s: %s", printer_id, e)

    def describe(trays: list[int] | None) -> list[dict[str, Any]]:
        if not trays:
            return []
        return [
            trays_by_id.get(
                gid, {"tray": gid, "ams_id": None, "tray_id": None, "type": None, "color": None, "label": f"#{gid}"}
            )
            for gid in trays
        ]

    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status.in_(("printing", "pending")))
        .order_by(PrintQueueItem.position, PrintQueueItem.id)
    )
    rows = list(result.scalars().all())
    active = [i for i in rows if i.status == "printing"]
    pending = [i for i in rows if i.status == "pending"]

    entry = await _withheld_entry(db, printer_id)
    withheld_trays = _normalize(entry.get("ams_mapping")) if entry else None
    carry: tuple[str, list[int] | None]
    if entry is None:
        # No withheld unload: either nothing ran yet, or the last job kept its
        # own tail unload. Both mean the hotend is empty going in.
        carry = _EMPTY
    elif withheld_trays is None:
        carry = _UNKNOWN
    else:
        carry = ("known", withheld_trays)

    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # The running job is on the plan so the Gantt can draw the filament it is
    # consuming. Its *start* swap already happened and is no longer knowable
    # from here, but its tail is not: the 3MF was injected at dispatch, so
    # whether it pulls the filament back is already decided and the lane needs
    # it — without it the run would be drawn straight through the next job.
    for item in active:
        trays = _normalize(item.ams_mapping)
        mode = resolve_unload_mode(item.unload_edit, item.defer_unload, bool(item.gcode_injection))
        items.append(
            {
                "item_id": item.id,
                "status": item.status,
                "position": item.position,
                "trays": trays,
                "trays_source": "stored" if trays else "unknown",
                "filaments": describe(trays),
                "unload_edit": item.unload_edit,
                "effective_mode": mode,
                "editable": False,
                "start_action": None,
                "end_action": "none" if tail_is_deferred(mode, bool(item.gcode_injection)) else "unload",
                "unload_at_start": None,
                "unload_at_end": not tail_is_deferred(mode, bool(item.gcode_injection)),
                "swap_from": None,
                "swap_from_filaments": [],
            }
        )

    for index, item in enumerate(pending):
        trays = _normalize(item.ams_mapping)
        source = "stored" if trays else "unknown"
        if trays is None:
            # What the scheduler will compute at dispatch. Planned, not saved —
            # persisting here would race the scheduler's own recompute.
            try:
                computed = await scheduler._compute_ams_mapping_for_printer(db, printer_id, item)
            except Exception as e:
                logger.warning("Filament plan: mapping preview failed for item %s: %s", item.id, e)
                computed = None
            if computed:
                trays = _normalize(json.dumps(computed))
                source = "planned" if trays else "unknown"

        mode = resolve_unload_mode(item.unload_edit, item.defer_unload, bool(item.gcode_injection))
        deferred = tail_is_deferred(mode, bool(item.gcode_injection))
        forced_start = mode == "start"

        # What physically happens when this job starts. The distinction the
        # booleans below can't carry: loading into an EMPTY hotend cuts
        # nothing, because the previous job already pulled its filament back —
        # whereas a swap cuts the resident filament first. The chart draws
        # scissors for one and not the other, so the difference has to survive
        # the API.
        carry_kind, carry_trays = carry
        if carry_kind == "empty":
            start_action: str | None = "swap" if forced_start else "load"
        elif forced_start:
            start_action = "swap"
        elif carry_kind == "unknown" or trays is None:
            start_action = None
        elif carry_trays != trays:
            start_action = "swap"
        else:
            start_action = "none"

        end_action = "none" if deferred else "unload"
        # Derived, so the badges and the chart can never disagree about it.
        at_start = None if start_action is None else start_action == "swap"

        entry_out = {
            "item_id": item.id,
            "status": item.status,
            "position": item.position,
            "trays": trays,
            "trays_source": source,
            "filaments": describe(trays),
            "unload_edit": item.unload_edit,
            "effective_mode": mode,
            "editable": True,
            "start_action": start_action,
            "end_action": end_action,
            "unload_at_start": at_start,
            "unload_at_end": end_action == "unload",
            "swap_from": carry_trays if carry_kind == "known" else None,
            "swap_from_filaments": describe(carry_trays) if carry_kind == "known" else [],
        }
        items.append(entry_out)

        if source == "unknown":
            warnings.append(
                {
                    "item_id": item.id,
                    "code": "mapping_unresolved",
                    "detail": "No AMS slot matches this job's filament, so its swap can't be planned.",
                }
            )

        # A start-swap sets the nozzle temperature from the INCOMING filament,
        # so pulling a hotter material out at the new material's temperature is
        # the risky shape (PETG left in the nozzle, PLA job heating to 220C).
        if at_start and carry_kind == "known":
            prev_types = {f.get("type") for f in describe(carry_trays) if f.get("type")}
            next_types = {f.get("type") for f in describe(trays) if f.get("type")}
            if prev_types and next_types and prev_types != next_types:
                warnings.append(
                    {
                        "item_id": item.id,
                        "code": "material_change_at_start",
                        "detail": (
                            f"{'/'.join(sorted(prev_types))} is pulled out at the "
                            f"{'/'.join(sorted(next_types))} start temperature."
                        ),
                    }
                )

        carry = ("known", trays) if deferred and trays is not None else (_UNKNOWN if deferred else _EMPTY)

        if index == len(pending) - 1 and deferred:
            warnings.append(
                {
                    "item_id": item.id,
                    "code": "filament_left_loaded",
                    "detail": "Last job in the queue: its tail unload is stripped and nothing follows to perform it.",
                }
            )

    return {
        "printer_id": printer_id,
        "withheld": {
            "active": entry is not None,
            "item_id": entry.get("item_id") if entry else None,
            "trays": withheld_trays,
            "filaments": describe(withheld_trays),
        },
        "items": items,
        "warnings": warnings,
    }
