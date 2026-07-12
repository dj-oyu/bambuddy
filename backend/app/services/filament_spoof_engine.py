"""Engine for the filament-spoof runout-backup feature.

Coordinates the DB rows (``FilamentSpoof``), the live MQTT client, and the pure
overlay (``filament_spoof.apply_spoof_overlay``):

* ``engage`` writes the primary slot's identity onto the backup slot's firmware
  (so Bambu's AMS Filament Backup auto-switches when the primary runs out),
  snapshots the backup's real identity + K profile, persists a PENDING row, and
  installs the client-side overlay snapshot + write-guard. The BMCU third-party
  AMS sometimes silently drops ``ams_filament_setting`` writes, so the row stays
  PENDING until firmware CONFIRMS the spoofed identity (``_reconcile``), then it
  becomes ENGAGED. If firmware never confirms within a bounded window the row is
  marked FAILED and the guard/snapshot are removed.
* ``release`` optionally restores the backup's real identity + K in firmware and
  marks the row RELEASED. Release works even when the feature is disabled.
* Runout detection: only when the backup slot becomes active *because the
  primary ran out mid-print* (prev active == primary AND state RUNNING) do we
  auto-release WITHOUT a firmware write.
* ``adopt`` records an EXISTING firmware-level spoof (e.g. a delayed BMCU write
  applied after its row was released) from a user-declared real identity —
  row created directly ENGAGED, no firmware write.
* Confirm loop: while PENDING, the spoof write is RESENT every
  BAMBUDDY_SPOOF_RESEND_INTERVAL_S (BMCU accepts writes probabilistically) and
  a deferred reconcile guarantees the timeout fires even with no AMS traffic.
* Startup/connect revalidation & confirmation: driven off live firmware truth,
  never off the overlaid view. Rows are only RELEASED on POSITIVE evidence.
  Reconciles are single-flight per printer (debounced against the AMS push rate).

Firmware truth: the engine NEVER reads ``client.state.raw_data['ams']`` for
identity comparisons — the overlay has already rewritten it. It reads the
pre-overlay identity captured by ``client.get_fw_tray_identity(ams, tray)``.

Fail-safe throughout: any uncertainty → do nothing (keep rows, skip writes).
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.core.database import async_session
from backend.app.models.filament_spoof import FilamentSpoof
from backend.app.services.filament_spoof import (
    _matches_spoof,
    _normalize_color,
    _spoof_enabled,
)
from backend.app.utils.filament_ids import (
    filament_id_to_setting_id,
    setting_id_to_filament_id,
)

logger = logging.getLogger(__name__)


def _resend_interval_s() -> float:
    """Seconds between spoof-write resends while PENDING (BMCU drops writes
    probabilistically — observed 2026-07-12: 5 identical writes needed)."""
    try:
        return float(os.environ.get("BAMBUDDY_SPOOF_RESEND_INTERVAL_S", "20"))
    except (ValueError, TypeError):
        return 20.0


# Release an ENGAGED row only after this many consecutive mismatching
# reconciles spanning at least this many seconds — printer/BMCU reboots
# briefly report empty or stale identities that must not count as evidence.
_RELEASE_MISMATCH_STREAK = 3
_RELEASE_MISMATCH_SPAN_S = 30.0


def _confirm_timeout_s() -> float:
    """Seconds to wait for firmware to confirm a spoof write before FAILED."""
    try:
        return float(os.environ.get("BAMBUDDY_SPOOF_CONFIRM_TIMEOUT_S", "90"))
    except (ValueError, TypeError):
        return 90.0


def _canonical_type(ftype) -> str:
    """Same-material equivalence key (reuses the scheduler's canonicaliser)."""
    try:
        from backend.app.services.print_scheduler import _canonical_filament_type

        return _canonical_filament_type((ftype or "").strip())
    except Exception:
        return (ftype or "").strip().upper()


def _global_tray_id(ams_id: int, tray_id: int) -> int:
    """Encode (ams_id, tray_id) to the firmware's global tray index.

    Shared convention with bambu_mqtt's tray_now decode: regular AMS 0-15 =
    ams*4+tray, AMS-HT 128-135 = ams (single tray).
    """
    return ams_id * 4 + tray_id if ams_id < 128 else ams_id


def _row_to_spoof_dict(row: FilamentSpoof) -> dict:
    """Project a FilamentSpoof row into the dict consumed by apply_spoof_overlay
    and the status surface (printer_manager reads ``_active_spoofs``)."""
    return {
        "backup_ams_id": row.backup_ams_id,
        "backup_tray_id": row.backup_tray_id,
        "primary_ams_id": row.primary_ams_id,
        "primary_tray_id": row.primary_tray_id,
        "real_tray_color": row.real_tray_color,
        "real_tray_sub_brands": row.real_tray_sub_brands,
        "spoof_tray_info_idx": row.spoof_tray_info_idx,
        "spoof_tray_color": row.spoof_tray_color,
        "state": row.state,
    }


class FilamentSpoofError(Exception):
    """Raised on invalid engage/release requests.

    ``status`` maps to an HTTP status the API route surfaces directly.
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class FilamentSpoofEngine:
    """Singleton coordinating filament spoofs across printers."""

    def __init__(self):
        # printer_manager is imported lazily to avoid an import cycle.
        self._printer_manager = None
        # Sync snapshot cache for consumers that can't await (VP manager.start()).
        self._snapshots: dict[int, list[dict]] = {}
        # Single-flight guard: one in-flight _reconcile per printer (the AMS
        # message rate would otherwise open a DB session per push and let two
        # reconciles interleave nondeterministically).
        self._reconcile_inflight: set[int] = set()
        # Confirm-loop task per backup slot so a release+re-engage doesn't
        # stack duplicate resend timers.
        self._confirm_tasks: dict[tuple, asyncio.Task] = {}
        # Consecutive-mismatch tracker for the hardened ENGAGED-release rule.
        self._mismatch_streak: dict[tuple, tuple] = {}

    # ---- infrastructure -------------------------------------------------

    def _pm(self):
        if self._printer_manager is None:
            from backend.app.services.printer_manager import printer_manager

            self._printer_manager = printer_manager
        return self._printer_manager

    def _get_client(self, printer_id: int):
        return self._pm().get_client(printer_id)

    @staticmethod
    def _fw_identity(client, ams_id: int, tray_id: int) -> dict | None:
        """Pre-overlay firmware identity for (ams, tray), or None if unknown."""
        if client is None:
            return None
        getter = getattr(client, "get_fw_tray_identity", None)
        if getter is None:
            return None
        try:
            return getter(ams_id, tray_id)
        except Exception:
            return None

    def get_active_snapshot(self, printer_id: int) -> list[dict]:
        """Sync accessor for the current PENDING+ENGAGED spoof snapshot.

        Used by the virtual-printer manager on instance (re)start to seed the
        bridge without awaiting the DB. Returns a copy; [] when none/unknown.
        """
        return list(self._snapshots.get(printer_id, []))

    async def start(self):
        """Revalidate all active spoofs against firmware state on boot.

        No-op when the feature is disabled (kill switch): the overlay/guard are
        not installed, but existing rows are left intact for later cleanup.
        """
        if not _spoof_enabled():
            logger.info("FilamentSpoofEngine.start: feature disabled, skipping")
            return
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(FilamentSpoof.printer_id)
                    .where(FilamentSpoof.state.in_(("ENGAGED", "PENDING")))
                    .distinct()
                )
                printer_ids = [pid for (pid,) in result.all()]
            for pid in printer_ids:
                await self.revalidate(pid)
                await self.refresh_client(pid)
                # Ensure reloaded PENDING rows resolve even if the printer
                # pushes no AMS updates (confirmation-timeout safety net).
                self._schedule_deferred_reconcile(pid, _confirm_timeout_s() + 5.0)
        except Exception:
            logger.warning("FilamentSpoofEngine.start revalidation failed", exc_info=True)

    # ---- snapshot / guard installation ----------------------------------

    async def _load_active(self, db, printer_id: int) -> list[FilamentSpoof]:
        """ENGAGED + PENDING rows (the overlay treats both as active)."""
        result = await db.execute(
            select(FilamentSpoof).where(
                FilamentSpoof.printer_id == printer_id,
                FilamentSpoof.state.in_(("ENGAGED", "PENDING")),
            )
        )
        return list(result.scalars().all())

    def _push_snapshot(self, printer_id: int, rows: list[FilamentSpoof]) -> None:
        """Install snapshot + guard + hooks onto the live client (and VP)."""
        spoofs = [_row_to_spoof_dict(r) for r in rows]
        self._snapshots[printer_id] = spoofs

        client = self._get_client(printer_id)
        if client is None:
            logger.info("[%s] Spoof snapshot push: %d spoof(s), client=MISSING", printer_id, len(spoofs))
        else:
            logger.debug("[%s] Spoof snapshot push: %d spoof(s)", printer_id, len(spoofs))
        if client is not None:
            client.set_active_spoofs(spoofs)
            if not spoofs:
                # No active spoofs → uninstall guard/hooks (fail-safe: the client
                # behaves exactly as upstream when nothing is spoofed).
                client._spoof_write_guard = None
                client._on_tray_now_change = None
                client._on_fw_identity_update = None
            else:
                guarded = {(r.backup_ams_id, r.backup_tray_id) for r in rows}
                client._spoof_write_guard = (
                    lambda ams_id, tray_id, _g=guarded: (ams_id, tray_id) in _g
                )
                client._on_tray_now_change = (
                    lambda ams_id, tray_id, prev_global, gcode_state, _pid=printer_id:
                    self._on_tray_now_change(_pid, ams_id, tray_id, prev_global, gcode_state)
                )
                # Confirmation/revalidation hook: fires once per AMS message while
                # spoofs are active (bambu_mqtt gates on _active_spoofs).
                client._on_fw_identity_update = (
                    lambda _pid=printer_id: self._on_fw_identity_update(_pid)
                )

        # Fan out to the virtual-printer slicer-facing view (finding #4).
        try:
            from backend.app.services.virtual_printer import virtual_printer_manager

            virtual_printer_manager.set_active_spoofs(printer_id, spoofs)
        except Exception:
            logger.debug("[%s] VP spoof fan-out skipped", printer_id, exc_info=True)

    async def refresh_client(self, printer_id: int, rows: list[FilamentSpoof] | None = None) -> None:
        """Push the current active-spoof snapshot + write-guard onto the client.

        ``rows`` may be passed to avoid a redundant DB load (e.g. right after
        revalidate). When the feature is disabled, install an empty snapshot so
        the overlay/guard stay off.
        """
        if not _spoof_enabled():
            self._snapshots[printer_id] = []
            client = self._get_client(printer_id)
            if client is not None:
                client.set_active_spoofs([])
                client._spoof_write_guard = None
                client._on_tray_now_change = None
                client._on_fw_identity_update = None
            return
        if rows is None:
            async with async_session() as db:
                rows = await self._load_active(db, printer_id)
        self._push_snapshot(printer_id, rows)

    # ---- engage ---------------------------------------------------------

    def _iter_slot_ids(self, client):
        """Yield (ams_id, tray_id) for every physical tray the client knows."""
        state = getattr(client, "state", None)
        raw = getattr(state, "raw_data", None) if state else None
        units = (raw or {}).get("ams", []) if isinstance(raw, dict) else []
        if not isinstance(units, list):
            return
        for unit in units:
            if not isinstance(unit, dict):
                continue
            try:
                uid = int(unit.get("id"))
            except (ValueError, TypeError):
                continue
            for tray in unit.get("tray", []) or []:
                if not isinstance(tray, dict):
                    continue
                try:
                    tid = int(tray.get("id"))
                except (ValueError, TypeError):
                    continue
                yield uid, tid

    def _native_partners(self, client, ams_id: int, tray_id: int, idx, color) -> list[tuple]:
        """Other slots whose FIRMWARE identity equals (idx, color).

        Uses firmware truth (get_fw_tray_identity) so overlaid backups don't
        masquerade. Excludes the (ams, tray) itself.
        """
        partners = []
        norm = _normalize_color(color)
        for a, t in self._iter_slot_ids(client):
            if (a, t) == (ams_id, tray_id):
                continue
            fw = self._fw_identity(client, a, t)
            if not fw:
                continue
            if fw.get("tray_info_idx") == idx and _normalize_color(fw.get("tray_color")) == norm:
                partners.append((a, t))
        return partners

    def _extruder_of(self, client, ams_id: int):
        state = getattr(client, "state", None)
        amap = getattr(state, "ams_extruder_map", None) if state else None
        if not isinstance(amap, dict):
            return None
        return amap.get(str(ams_id), amap.get(ams_id))

    async def _slot_has_active_spoof(self, db, printer_id: int, ams_id: int, tray_id: int) -> bool:
        """True if (ams, tray) is the backup OR primary of an active spoof."""
        result = await db.execute(
            select(FilamentSpoof).where(
                FilamentSpoof.printer_id == printer_id,
                FilamentSpoof.state.in_(("ENGAGED", "PENDING")),
            )
        )
        for r in result.scalars().all():
            if (r.backup_ams_id, r.backup_tray_id) == (ams_id, tray_id):
                return True
            if (r.primary_ams_id, r.primary_tray_id) == (ams_id, tray_id):
                return True
        return False

    async def adopt(
        self, printer_id: int, primary: tuple, backup: tuple, real: dict
    ) -> FilamentSpoof:
        """Adopt an EXISTING firmware-level spoof without writing to the printer.

        For when the backup slot already carries the primary's identity on the
        firmware (e.g. a delayed BMCU write applied after the row was released)
        but bambuddy has no record of the slot's physical reality. ``real`` is
        the user-declared truth for the backup slot: tray_info_idx, tray_type,
        tray_sub_brands, tray_color, nozzle_temp_min, nozzle_temp_max.

        Firmware is not touched (aside from ensuring the backup toggle is ON);
        the row is created directly in ENGAGED state since the firmware already
        echoes the spoofed identity.
        """
        if not _spoof_enabled():
            raise FilamentSpoofError("Runout backup is disabled on this server", status=503)

        p_ams, p_tray = int(primary[0]), int(primary[1])
        b_ams, b_tray = int(backup[0]), int(backup[1])
        if (p_ams, p_tray) == (b_ams, b_tray):
            raise FilamentSpoofError("Primary and backup slots must differ", status=409)

        client = self._get_client(printer_id)
        if client is None or not getattr(client.state, "connected", False):
            raise FilamentSpoofError("Printer not connected", status=409)

        primary_fw = self._fw_identity(client, p_ams, p_tray)
        backup_fw = self._fw_identity(client, b_ams, b_tray)
        if primary_fw is None or backup_fw is None:
            raise FilamentSpoofError("Slot state not available", status=409)
        # Adoption precondition: the firmware must ALREADY show the primary's
        # identity on the backup slot — otherwise this is a normal engage.
        if not _matches_spoof(
            backup_fw, primary_fw.get("tray_info_idx"), primary_fw.get("tray_color")
        ):
            raise FilamentSpoofError(
                "Backup slot does not carry the primary's identity on the printer; "
                "use a normal engage instead",
                status=409,
            )
        real_color = (real.get("tray_color") or "").strip()
        if not real_color:
            raise FilamentSpoofError("Real tray_color is required", status=422)
        if _normalize_color(real_color) == _normalize_color(primary_fw.get("tray_color")):
            raise FilamentSpoofError(
                "Real color equals the primary's color — nothing to adopt", status=409
            )

        async with async_session() as db:
            if await self._slot_has_active_spoof(db, printer_id, b_ams, b_tray) or \
               await self._slot_has_active_spoof(db, printer_id, p_ams, p_tray):
                raise FilamentSpoofError("Slot already participates in a runout backup", status=409)
            now = datetime.now(timezone.utc)
            row = FilamentSpoof(
                printer_id=printer_id,
                backup_ams_id=b_ams, backup_tray_id=b_tray,
                primary_ams_id=p_ams, primary_tray_id=p_tray,
                real_tray_info_idx=real.get("tray_info_idx") or backup_fw.get("tray_info_idx"),
                real_tray_type=real.get("tray_type"),
                real_tray_sub_brands=real.get("tray_sub_brands") or "",
                real_tray_color=real_color,
                real_nozzle_temp_min=real.get("nozzle_temp_min"),
                real_nozzle_temp_max=real.get("nozzle_temp_max"),
                spoof_tray_info_idx=primary_fw.get("tray_info_idx"),
                spoof_tray_color=primary_fw.get("tray_color"),
                state="ENGAGED",
                engaged_at=now,
                confirmed_at=now,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)

        self._ensure_backup_enabled(client, printer_id)

        await self.refresh_client(printer_id)
        logger.info(
            "[%s] Filament spoof ADOPTED: backup AMS %s tray %s carries primary AMS %s tray %s "
            "(real color %s declared by user; no firmware write)",
            printer_id, b_ams, b_tray, p_ams, p_tray, real_color,
        )
        return row

    async def engage(self, printer_id: int, primary: tuple, backup: tuple, force: bool = False):
        """Engage a spoof: backup slot impersonates the primary slot's identity.

        Returns a FilamentSpoof row (state PENDING) on success, or a dict
        ``{"native": True, ...}`` when the spools already match (design rule A).
        Raises FilamentSpoofError (with .status) on any rejection.
        """
        if not _spoof_enabled():
            raise FilamentSpoofError("Runout backup is disabled on this server", status=503)

        p_ams, p_tray = int(primary[0]), int(primary[1])
        b_ams, b_tray = int(backup[0]), int(backup[1])

        if (p_ams, p_tray) == (b_ams, b_tray):
            raise FilamentSpoofError("Primary and backup slots must differ", status=409)

        client = self._get_client(printer_id)
        if client is None or not getattr(client.state, "connected", False):
            raise FilamentSpoofError("Printer not connected", status=409)

        primary_live = self._find_live_tray(client, p_ams, p_tray)
        backup_live = self._find_live_tray(client, b_ams, b_tray)
        if primary_live is None:
            raise FilamentSpoofError(f"Primary slot AMS {p_ams} tray {p_tray} not found", status=404)
        if backup_live is None:
            raise FilamentSpoofError(f"Backup slot AMS {b_ams} tray {b_tray} not found", status=404)

        # --- no chains / hybrids: neither slot may already carry a spoof -----
        async with async_session() as db:
            if await self._slot_has_active_spoof(db, printer_id, b_ams, b_tray):
                raise FilamentSpoofError("Backup slot already participates in a runout backup", status=409)
            if await self._slot_has_active_spoof(db, printer_id, p_ams, p_tray):
                raise FilamentSpoofError("Primary slot already participates in a runout backup", status=409)

        # --- same material (canonical equivalence) --------------------------
        p_type = _canonical_type(primary_live.get("tray_type"))
        b_type = _canonical_type(backup_live.get("tray_type"))
        if not p_type or not b_type or p_type != b_type:
            raise FilamentSpoofError(
                f"Filament type mismatch: primary={primary_live.get('tray_type')!r} "
                f"backup={backup_live.get('tray_type')!r}",
                status=409,
            )

        # --- same extruder (H2D-class); unknown/single-nozzle → allow -------
        p_ext = self._extruder_of(client, p_ams)
        b_ext = self._extruder_of(client, b_ams)
        if p_ext is not None and b_ext is not None and p_ext != b_ext:
            raise FilamentSpoofError("Primary and backup slots are on different extruders", status=409)

        # --- backup must not currently be the active/printing tray ----------
        active = getattr(client.state, "tray_now", 255)
        if active == _global_tray_id(b_ams, b_tray):
            raise FilamentSpoofError("Backup slot is currently the active tray", status=409)

        # Firmware identities (pre-overlay truth).
        p_fw = self._fw_identity(client, p_ams, p_tray) or {
            "tray_info_idx": primary_live.get("tray_info_idx"),
            "tray_color": primary_live.get("tray_color"),
        }
        b_fw = self._fw_identity(client, b_ams, b_tray) or {
            "tray_info_idx": backup_live.get("tray_info_idx"),
            "tray_color": backup_live.get("tray_color"),
        }

        # --- design rule A: identical identity → native backup, no spoof ----
        if b_fw.get("tray_info_idx") == p_fw.get("tray_info_idx") and _normalize_color(
            b_fw.get("tray_color")
        ) == _normalize_color(p_fw.get("tray_color")):
            self._ensure_backup_enabled(client, printer_id)
            logger.info("[%s] Native backup (identical identity), no spoof row created", printer_id)
            return {"native": True, "primary_ams_id": p_ams, "primary_tray_id": p_tray,
                    "backup_ams_id": b_ams, "backup_tray_id": b_tray}

        # --- design rule B: native-group lock -------------------------------
        p_partners = self._native_partners(client, p_ams, p_tray, p_fw.get("tray_info_idx"), p_fw.get("tray_color"))
        if p_partners:
            raise FilamentSpoofError(
                "The primary spool is already backed up by another matching spool", status=409
            )
        b_partners = self._native_partners(client, b_ams, b_tray, b_fw.get("tray_info_idx"), b_fw.get("tray_color"))
        if b_partners:
            if not force:
                raise FilamentSpoofError(
                    "This spool currently backs up another spool of the same color; "
                    "pass force to reassign it",
                    status=409,
                )
            # force refused mid-print (fail-safe: mapping may reference the slot).
            if str(getattr(client.state, "state", "")).upper() == "RUNNING":
                raise FilamentSpoofError(
                    "Cannot reassign this backup spool while a print is running", status=409
                )

        # --- snapshot the backup's REAL identity + K before overwriting -----
        real_idx = backup_live.get("tray_info_idx")
        real_type = backup_live.get("tray_type")
        real_sub = backup_live.get("tray_sub_brands")
        real_color = backup_live.get("tray_color")
        real_tmin = backup_live.get("nozzle_temp_min")
        real_tmax = backup_live.get("nozzle_temp_max")
        real_cali = backup_live.get("cali_idx")
        real_k = backup_live.get("k")

        # The spoofed identity = the primary's.
        spoof_idx = primary_live.get("tray_info_idx")
        spoof_type = primary_live.get("tray_type")
        spoof_sub = primary_live.get("tray_sub_brands")
        spoof_color = primary_live.get("tray_color")
        spoof_tmin = primary_live.get("nozzle_temp_min")
        spoof_tmax = primary_live.get("nozzle_temp_max")

        # Versioned setting_id from the primary slot's persisted preset —
        # BMCU drops writes without it (see _setting_id_for_slot).
        async with async_session() as db:
            spoof_setting_id = await self._setting_id_for_slot(
                db, printer_id, p_ams, p_tray, spoof_idx or ""
            )

        ok = client.ams_set_filament_setting(
            b_ams, b_tray,
            spoof_idx or "", spoof_type or "", spoof_sub or "", spoof_color or "",
            int(spoof_tmin) if spoof_tmin is not None else 0,
            int(spoof_tmax) if spoof_tmax is not None else 0,
            setting_id=spoof_setting_id,
            bypass_spoof_guard=True,
        )
        if not ok:
            raise FilamentSpoofError("Failed to send spoof identity to printer", status=502)

        # Elicit a prompt firmware echo so PENDING confirms (or fails) quickly,
        # and guarantee the confirmation timeout is evaluated even if the idle
        # printer pushes no AMS updates on its own.
        try:
            client.request_status_update()
        except Exception:
            logger.debug("[%s] post-engage pushall request failed", printer_id, exc_info=True)
        self._schedule_confirm_loop(printer_id, b_ams, b_tray)

        # design rule C: re-assert the backup slot's real K profile so the
        # identity write doesn't clobber it (best-effort).
        self._reassert_cali(client, b_ams, b_tray, real_cali, spoof_idx)

        # Ensure AMS Filament Backup (auto_switch_filament) is ON.
        self._ensure_backup_enabled(client, printer_id)

        now = datetime.now(timezone.utc)
        async with async_session() as db:
            row = FilamentSpoof(
                printer_id=printer_id,
                backup_ams_id=b_ams, backup_tray_id=b_tray,
                primary_ams_id=p_ams, primary_tray_id=p_tray,
                real_tray_info_idx=real_idx, real_tray_type=real_type,
                real_tray_sub_brands=real_sub, real_tray_color=real_color,
                real_nozzle_temp_min=int(real_tmin) if real_tmin is not None else None,
                real_nozzle_temp_max=int(real_tmax) if real_tmax is not None else None,
                real_cali_idx=str(real_cali) if real_cali is not None else None,
                real_k=float(real_k) if isinstance(real_k, (int, float)) else None,
                extruder_id=int(b_ext) if b_ext is not None else None,
                spoof_tray_info_idx=spoof_idx, spoof_tray_color=spoof_color,
                state="PENDING", engaged_at=now,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)

        await self.refresh_client(printer_id)
        logger.info(
            "[%s] Filament spoof PENDING: backup AMS %s tray %s impersonates primary AMS %s tray %s "
            "(awaiting firmware confirmation)",
            printer_id, b_ams, b_tray, p_ams, p_tray,
        )
        return row

    def _reassert_cali(self, client, ams_id, tray_id, cali_idx, filament_id) -> None:
        """Re-assert a slot's K/calibration profile via extrusion_cali_sel."""
        if cali_idx is None:
            return
        try:
            client.extrusion_cali_sel(ams_id, tray_id, int(cali_idx), filament_id or "")
        except Exception:
            logger.debug("[%s?] extrusion_cali_sel re-assert failed", ams_id, exc_info=True)

    @staticmethod
    def _find_live_tray(client, ams_id: int, tray_id: int) -> dict | None:
        """Backup's full tray dict from live state (used ONLY at engage time,
        when the slot is not yet spoofed so raw_data == firmware truth)."""
        from backend.app.services.filament_spoof import _find_tray

        state = getattr(client, "state", None) if client else None
        raw = getattr(state, "raw_data", None) if state else None
        units = (raw or {}).get("ams", []) if isinstance(raw, dict) else []
        return _find_tray(units, ams_id, tray_id)

    @staticmethod
    def _is_backup_enabled(client) -> bool:
        state = getattr(client, "state", None)
        if state is None:
            return False
        return bool(getattr(state, "ams_filament_backup", None))

    def _ensure_backup_enabled(self, client, printer_id: int) -> None:
        """Turn on AMS Filament Backup unless state already shows it ON."""
        if not self._is_backup_enabled(client):
            try:
                client.set_ams_filament_backup(True)
            except Exception:
                logger.warning("[%s] failed to enable AMS filament backup", printer_id, exc_info=True)

    # ---- release --------------------------------------------------------

    async def release(self, printer_id: int, backup: tuple, restore: bool = True) -> FilamentSpoof | None:
        """Release a spoof; optionally restore the backup's real firmware
        identity + K. Works even when the feature is disabled (cleanup path)."""
        b_ams, b_tray = int(backup[0]), int(backup[1])
        async with async_session() as db:
            result = await db.execute(
                select(FilamentSpoof).where(
                    FilamentSpoof.printer_id == printer_id,
                    FilamentSpoof.backup_ams_id == b_ams,
                    FilamentSpoof.backup_tray_id == b_tray,
                    FilamentSpoof.state.in_(("ENGAGED", "PENDING")),
                )
            )
            rows = list(result.scalars().all())
            if not rows:
                return None
            # Defensively handle >1 (shouldn't happen with the uniqueness intent).
            row = rows[0]

            if restore:
                client = self._get_client(printer_id)
                fw = self._fw_identity(client, b_ams, b_tray) if client else None
                # Only write back if firmware STILL shows the spoofed identity —
                # otherwise the user already changed it; don't clobber.
                if client is not None and fw is not None and _matches_spoof(
                    fw, row.spoof_tray_info_idx, row.spoof_tray_color
                ):
                    # Best-effort (BMCU writes can silently fail); don't block.
                    client.ams_set_filament_setting(
                        b_ams, b_tray,
                        row.real_tray_info_idx or "", row.real_tray_type or "",
                        row.real_tray_sub_brands or "", row.real_tray_color or "",
                        int(row.real_nozzle_temp_min) if row.real_nozzle_temp_min is not None else 0,
                        int(row.real_nozzle_temp_max) if row.real_nozzle_temp_max is not None else 0,
                        setting_id=await self._setting_id_for_slot(
                            db, printer_id, b_ams, b_tray, row.real_tray_info_idx or ""
                        ),
                        bypass_spoof_guard=True,
                    )
                    if row.real_cali_idx is not None:
                        self._reassert_cali(client, b_ams, b_tray, row.real_cali_idx, row.real_tray_info_idx)

            for r in rows:
                r.state = "RELEASED"
                r.released_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)

        await self.refresh_client(printer_id)
        logger.info("[%s] Filament spoof RELEASED: backup AMS %s tray %s (restore=%s)", printer_id, b_ams, b_tray, restore)
        return row

    # ---- runout detection ----------------------------------------------

    def _on_tray_now_change(self, printer_id: int, ams_id: int, tray_id: int,
                            prev_global: int, gcode_state) -> None:
        """Sync entry from the MQTT thread; schedule async runout handling."""
        try:
            self._pm()._schedule_async(
                self._handle_runout(printer_id, ams_id, tray_id, prev_global, gcode_state)
            )
        except Exception:
            logger.debug("[%s] failed to schedule spoof runout handler", printer_id, exc_info=True)

    async def _handle_runout(self, printer_id: int, ams_id: int, tray_id: int,
                             prev_global: int, gcode_state) -> None:
        """Auto-release a spoof ONLY on a genuine mid-print runout switch.

        Genuine runout = the newly-active tray is a spoof's BACKUP slot AND the
        previously-active tray was that spoof's PRIMARY slot AND a print is
        RUNNING. Any other tray_now change is ignored (fail-safe: keep ENGAGED;
        the overlay fail-safe key already handles identity drift).

        No firmware write: the backup is now actively printing.
        """
        if str(gcode_state or "").upper() != "RUNNING":
            return
        async with async_session() as db:
            result = await db.execute(
                select(FilamentSpoof).where(
                    FilamentSpoof.printer_id == printer_id,
                    FilamentSpoof.backup_ams_id == ams_id,
                    FilamentSpoof.backup_tray_id == tray_id,
                    FilamentSpoof.state.in_(("ENGAGED", "PENDING")),
                )
            )
            rows = list(result.scalars().all())
            if not rows:
                return
            released_any = False
            for row in rows:
                if prev_global != _global_tray_id(row.primary_ams_id, row.primary_tray_id):
                    # Backup became active but NOT because the primary ran out.
                    logger.info(
                        "[%s] tray_now→backup AMS %s tray %s but prev(%s)≠primary; keeping ENGAGED",
                        printer_id, ams_id, tray_id, prev_global,
                    )
                    continue
                row.state = "RELEASED"
                row.released_at = datetime.now(timezone.utc)
                released_any = True
                logger.info(
                    "[%s] Filament spoof auto-RELEASED on runout: backup AMS %s tray %s became active",
                    printer_id, ams_id, tray_id,
                )
            if released_any:
                await db.commit()
        if released_any:
            await self.refresh_client(printer_id)

    # ---- confirmation / revalidation -----------------------------------

    def _on_fw_identity_update(self, printer_id: int) -> None:
        """Sync entry from the MQTT thread on each AMS message; reconcile.

        Single-flight: while one reconcile is in flight for this printer,
        further AMS messages are dropped (the next message after it finishes
        re-triggers). Prevents per-push DB churn and interleaved reconciles.
        """
        if printer_id in self._reconcile_inflight:
            return
        self._reconcile_inflight.add(printer_id)

        async def _run() -> None:
            try:
                await self._reconcile(printer_id)
            finally:
                self._reconcile_inflight.discard(printer_id)

        try:
            self._pm()._schedule_async(_run())
        except Exception:
            self._reconcile_inflight.discard(printer_id)
            logger.debug("[%s] failed to schedule spoof reconcile", printer_id, exc_info=True)

    async def _setting_id_for_slot(
        self, db, printer_id: int, ams_id: int, tray_id: int, tray_info_idx: str
    ) -> str:
        """Best VERSIONED setting_id for a slot's preset.

        BMCU firmware silently drops ams_filament_setting whose setting_id
        lacks the version suffix (observed 2026-07-12: "GFSG00_03" applied,
        "GFSG00" and "" were ignored). The versioned id is only known from
        cloud presets; bambuddy persists the last one written per slot in
        slot_preset_mappings. Fall back to the unversioned derivation.
        """
        try:
            from backend.app.models.slot_preset import SlotPresetMapping

            result = await db.execute(
                select(SlotPresetMapping.preset_id).where(
                    SlotPresetMapping.printer_id == printer_id,
                    SlotPresetMapping.ams_id == ams_id,
                    SlotPresetMapping.tray_id == tray_id,
                )
            )
            preset_id = result.scalar_one_or_none()
            if preset_id:
                base = preset_id.split("_")[0]
                # Only trust the mapping if it belongs to the same preset.
                if setting_id_to_filament_id(base) == (tray_info_idx or ""):
                    return preset_id
        except Exception:
            logger.debug(
                "[%s] slot preset lookup failed for AMS %s tray %s",
                printer_id, ams_id, tray_id, exc_info=True,
            )
        return filament_id_to_setting_id(tray_info_idx or "")

    def _schedule_confirm_loop(self, printer_id: int, b_ams: int, b_tray: int) -> None:
        """Resend the spoof write every interval until confirmed or timed out.

        BMCU acceptance of ams_filament_setting is probabilistic (an identical
        write can be silently dropped several times before applying), so a
        single write + timeout would spuriously FAIL engagements that a couple
        of resends would have landed. The message-driven reconcile still does
        the actual PENDING→ENGAGED/FAILED transitions; this loop only re-fires
        the write and reconciles between messages.
        """

        async def _loop() -> None:
            interval = max(_resend_interval_s(), 1.0)
            try:
                while True:
                    await asyncio.sleep(interval)
                    await self._reconcile(printer_id)
                    async with async_session() as db:
                        result = await db.execute(
                            select(FilamentSpoof).where(
                                FilamentSpoof.printer_id == printer_id,
                                FilamentSpoof.backup_ams_id == b_ams,
                                FilamentSpoof.backup_tray_id == b_tray,
                                FilamentSpoof.state == "PENDING",
                            )
                        )
                        row = result.scalars().first()
                        if row is None:
                            return  # ENGAGED / FAILED / released — done.
                        setting_id = await self._setting_id_for_slot(
                            db, printer_id, row.primary_ams_id, row.primary_tray_id,
                            row.spoof_tray_info_idx or "",
                        )
                    client = self._get_client(printer_id)
                    if client is None:
                        continue
                    if self._fw_identity(client, row.primary_ams_id, row.primary_tray_id) is None:
                        continue  # no data — keep waiting, timeout will handle it
                    # Mirror the initial engage write exactly: type/sub_brands/
                    # temps come from the PRIMARY's live tray (the primary slot
                    # is never overlaid, so raw_data is genuine for it).
                    from backend.app.services.filament_spoof import _find_tray

                    primary_tray = _find_tray(
                        (getattr(client.state, "raw_data", None) or {}).get("ams", []),
                        row.primary_ams_id, row.primary_tray_id,
                    ) or {}
                    tmin = primary_tray.get("nozzle_temp_min", row.real_nozzle_temp_min)
                    tmax = primary_tray.get("nozzle_temp_max", row.real_nozzle_temp_max)
                    logger.info(
                        "[%s] Resending spoof write for AMS %s tray %s (still PENDING)",
                        printer_id, b_ams, b_tray,
                    )
                    client.ams_set_filament_setting(
                        b_ams, b_tray,
                        row.spoof_tray_info_idx or "",
                        primary_tray.get("tray_type") or row.real_tray_type or "",
                        primary_tray.get("tray_sub_brands") or "",
                        row.spoof_tray_color or "",
                        int(tmin) if tmin is not None else 0,
                        int(tmax) if tmax is not None else 0,
                        setting_id=setting_id,
                        bypass_spoof_guard=True,
                    )
                    try:
                        client.request_status_update()
                    except Exception:
                        pass
            except Exception:
                logger.debug("[%s] spoof confirm loop aborted", printer_id, exc_info=True)
            finally:
                # Only clear our own registration — a cancelled stale loop must
                # not evict the replacement task's entry.
                if self._confirm_tasks.get(key) is asyncio.current_task():
                    self._confirm_tasks.pop(key, None)

        # One loop per backup slot: cancel a stale loop from a prior engage so
        # release+re-engage doesn't stack duplicate resend timers.
        key = (printer_id, b_ams, b_tray)
        old = self._confirm_tasks.pop(key, None)
        if old is not None and not old.done():
            old.cancel()
        try:
            self._confirm_tasks[key] = asyncio.get_running_loop().create_task(_loop())
        except RuntimeError:
            try:
                self._pm()._schedule_async(_loop())
            except Exception:
                logger.debug("[%s] failed to schedule confirm loop", printer_id, exc_info=True)

    def _schedule_deferred_reconcile(self, printer_id: int, delay_s: float) -> None:
        """Reconcile after a delay, independent of AMS message arrival.

        The message-driven hook is the primary confirmation path, but an idle
        printer (or a quiet BMCU) may push nothing for minutes — without this
        timer a PENDING row would never hit its confirmation timeout.
        """

        async def _later() -> None:
            try:
                await asyncio.sleep(delay_s)
                await self._reconcile(printer_id)
            except Exception:
                logger.debug("[%s] deferred spoof reconcile failed", printer_id, exc_info=True)

        try:
            asyncio.get_running_loop().create_task(_later())
        except RuntimeError:
            # No running loop (sync caller) — fall back to the manager's loop.
            try:
                self._pm()._schedule_async(_later())
            except Exception:
                logger.debug("[%s] failed to schedule deferred reconcile", printer_id, exc_info=True)

    async def _reconcile(self, printer_id: int) -> None:
        """Confirm PENDING rows and revalidate ENGAGED rows off firmware truth.

        PENDING: firmware now shows the spoofed identity → ENGAGED. Past the
        confirmation timeout without a match → FAILED (guard/snapshot removed).
        ENGAGED: only RELEASE on POSITIVE evidence (firmware present AND identity
        ≠ spoofed). Absent data → keep ENGAGED, retry next message.
        """
        client = self._get_client(printer_id)
        if client is None:
            return
        changed = False
        now = datetime.now(timezone.utc)
        timeout = _confirm_timeout_s()
        async with async_session() as db:
            rows = await self._load_active(db, printer_id)
            for row in rows:
                fw = self._fw_identity(client, row.backup_ams_id, row.backup_tray_id)
                matches = fw is not None and _matches_spoof(fw, row.spoof_tray_info_idx, row.spoof_tray_color)
                if row.state == "PENDING":
                    if matches:
                        row.state = "ENGAGED"
                        row.confirmed_at = now
                        changed = True
                        logger.info(
                            "[%s] Filament spoof CONFIRMED: backup AMS %s tray %s now ENGAGED",
                            printer_id, row.backup_ams_id, row.backup_tray_id,
                        )
                    elif row.engaged_at is not None:
                        age = (now - row.engaged_at.replace(tzinfo=timezone.utc)).total_seconds()
                        if age > timeout:
                            row.state = "FAILED"
                            row.released_at = now
                            changed = True
                            logger.warning(
                                "[%s] Filament spoof FAILED to confirm within %.0fs: backup AMS %s tray %s "
                                "(firmware never accepted the write; power-cycle may be required)",
                                printer_id, timeout, row.backup_ams_id, row.backup_tray_id,
                            )
                elif row.state == "ENGAGED":
                    # Positive-evidence release only — hardened against printer
                    # REBOOT transients (observed 2026-07-12: during BMCU boot
                    # the tray is briefly reported with an empty/changed identity,
                    # which released a healthy row even though the persisted
                    # spoof came back seconds later):
                    #   * an empty tray_info_idx is re-detection, not evidence;
                    #   * a genuine mismatch must persist across several
                    #     consecutive reconciles AND a minimum wall-clock span.
                    if self._note_identity_mismatch(printer_id, row, fw, matches, now):
                        row.state = "RELEASED"
                        row.released_at = now
                        changed = True
            if changed:
                await db.commit()
        if changed:
            await self.refresh_client(printer_id)

    def _note_identity_mismatch(self, printer_id: int, row, fw, matches: bool, now) -> bool:
        """Hardened ENGAGED-release rule. Returns True when release is warranted.

        Printer/BMCU reboots briefly report empty or stale identities that must
        not count as evidence (observed 2026-07-12: a healthy row was released
        during a power-cycle even though the persisted spoof came back seconds
        later). Release requires a REAL identity mismatch observed on
        _RELEASE_MISMATCH_STREAK consecutive reconciles spanning at least
        _RELEASE_MISMATCH_SPAN_S seconds; empty tray_info_idx (re-detection) and
        matches reset the streak.
        """
        key = (printer_id, row.backup_ams_id, row.backup_tray_id)
        if matches or fw is None or not (fw.get("tray_info_idx") or "").strip():
            self._mismatch_streak.pop(key, None)
            return False
        count, first_ts = self._mismatch_streak.get(key, (0, now))
        count += 1
        self._mismatch_streak[key] = (count, first_ts)
        elapsed = (now - first_ts).total_seconds()
        if count >= _RELEASE_MISMATCH_STREAK and elapsed >= _RELEASE_MISMATCH_SPAN_S:
            self._mismatch_streak.pop(key, None)
            logger.info(
                "[%s] Filament spoof revalidation dropped (firmware identity changed, "
                "%d observations over %.0fs): backup AMS %s tray %s",
                printer_id, count, elapsed, row.backup_ams_id, row.backup_tray_id,
            )
            return True
        logger.debug(
            "[%s] spoof identity mismatch %d/%d (%.0fs) for AMS %s tray %s — holding",
            printer_id, count, _RELEASE_MISMATCH_STREAK, elapsed,
            row.backup_ams_id, row.backup_tray_id,
        )
        return False

    async def revalidate(self, printer_id: int) -> int:
        """Positive-evidence revalidation of ENGAGED/PENDING rows on connect.

        Returns the number of rows transitioned. Never releases on absence of
        firmware data (finding #2). No printer write.
        """
        client = self._get_client(printer_id)
        if client is None:
            return 0
        changed = 0
        now = datetime.now(timezone.utc)
        async with async_session() as db:
            rows = await self._load_active(db, printer_id)
            for row in rows:
                fw = self._fw_identity(client, row.backup_ams_id, row.backup_tray_id)
                if fw is None:
                    # No firmware data yet — keep the row, retry on next message.
                    continue
                matches = _matches_spoof(fw, row.spoof_tray_info_idx, row.spoof_tray_color)
                if row.state == "PENDING" and matches:
                    row.state = "ENGAGED"
                    row.confirmed_at = now
                    changed += 1
                elif row.state == "ENGAGED" and not matches:
                    if self._note_identity_mismatch(printer_id, row, fw, matches, now):
                        row.state = "RELEASED"
                        row.released_at = now
                        changed += 1
            if changed:
                await db.commit()
        return changed

    async def list_active(self, printer_id: int) -> list[FilamentSpoof]:
        async with async_session() as db:
            return await self._load_active(db, printer_id)


filament_spoof_engine = FilamentSpoofEngine()
