"""Pure, side-effect-free overlay for the filament-spoof runout-backup feature.

The firmware of a spoofed backup slot reports the *primary* slot's identity
(tray_info_idx + tray_color) because we wrote it there via ams_filament_setting.
``apply_spoof_overlay`` rewrites that live AMS view back to the backup slot's
REAL color / sub-brand so every downstream reader (bambuddy AMS card, scheduler,
spoolman sync, virtual-printer slicer view) sees the truth — while keeping the
spoofed ``tray_info_idx`` so firmware's AMS Filament Backup still auto-switches.

Fail-safe philosophy: any uncertainty → do nothing (leave firmware's value).
The overlay only fires when the live tray STILL exactly matches the spoofed
identity we wrote; if the user changed the spool or the firmware value drifted,
we leave it alone. Never raises.

Kill switch: ``BAMBUDDY_FILAMENT_SPOOF=0`` (also false/no/off) disables the
overlay entirely (the overlay becomes an identity no-op, but still cleans up
stale ``_spoof`` markers). The engine's release/DELETE path stays functional
even when disabled so operators can clean up.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Reuse the single canonical color normaliser (identity comparison) shared with
# the deficit/backup-peer logic so the overlay and that code agree exactly.
from backend.app.services.filament_deficit import _normalize_color_for_id


# States that the overlay treats as "active" (should be overlaid). PENDING is
# included: its fail-safe key naturally no-ops until firmware reports the new
# identity, at which point the overlay fires and the slot is confirmed.
_OVERLAY_STATES = (None, "ENGAGED", "PENDING")


def _spoof_enabled() -> bool:
    """Read the BAMBUDDY_FILAMENT_SPOOF env toggle (default enabled).

    Mirrors the BAMBUDDY_DEFER_TAIL_UNLOAD convention: any of "0"/"false"/"no"/
    "off" (case-insensitive) disables; everything else (including unset) enables.
    Read per-call (after the cheap empty-spoofs check) so tests can monkeypatch.
    """
    val = os.environ.get("BAMBUDDY_FILAMENT_SPOOF", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


# Exported as a module constant for callers that want a cheap gate at import.
FILAMENT_SPOOF_ENABLED = _spoof_enabled()


def _normalize_color(color) -> str | None:
    """Normalize a tray color for comparison. Thin wrapper for back-compat.

    Delegates to the shared ``_normalize_color_for_id``. Returns None for
    unusable input (rather than "") so idx/color guards read naturally.
    """
    if not isinstance(color, str):
        return None
    c = _normalize_color_for_id(color)
    return c or None


def _find_tray(units: list, ams_id, tray_id) -> dict | None:
    """Find the tray dict for (ams_id, tray_id) in an ams unit list.

    Matches the structure handled by apply_tray_exist_bits in bambu_mqtt.py:
    a list of ``{"id": ams_id, "tray": [{"id": tray_id, ...}]}`` dicts, where
    ids may arrive as ints or strings.
    """
    if not isinstance(units, list):
        return None
    try:
        ams_id = int(ams_id)
        tray_id = int(tray_id)
    except (ValueError, TypeError):
        return None
    for ams_unit in units:
        if not isinstance(ams_unit, dict):
            continue
        raw = ams_unit.get("id")
        try:
            uid = int(raw) if raw is not None else None
        except (ValueError, TypeError):
            continue
        if uid != ams_id:
            continue
        for tray in ams_unit.get("tray", []) or []:
            if not isinstance(tray, dict):
                continue
            traw = tray.get("id")
            try:
                tid = int(traw) if traw is not None else None
            except (ValueError, TypeError):
                continue
            if tid == tray_id:
                return tray
    return None


def _matches_spoof(live: dict, spoof_idx, spoof_color) -> bool:
    """Fail-safe match: does ``live`` STILL show the spoofed identity we wrote?

    Shared predicate used by the overlay AND by the engine (release / revalidate
    / confirmation) so all readers agree on what "still spoofed" means.
    """
    if not isinstance(live, dict) or not spoof_idx:
        return False
    if live.get("tray_info_idx") != spoof_idx:
        return False
    return _normalize_color(live.get("tray_color")) == _normalize_color(spoof_color)


def _strip_stale_markers(units: list, keep: set) -> None:
    """Remove ``_spoof`` markers from any tray not in ``keep`` (in place).

    ``keep`` is a set of ``(ams_id, tray_id)`` tuples overlaid this pass. This
    clears badge/menu ghosts after a release (finding #11). Cheap: only touches
    trays that actually carry a marker.
    """
    for ams_unit in units:
        if not isinstance(ams_unit, dict):
            continue
        raw = ams_unit.get("id")
        try:
            uid = int(raw) if raw is not None else None
        except (ValueError, TypeError):
            uid = None
        for tray in ams_unit.get("tray", []) or []:
            if not isinstance(tray, dict) or "_spoof" not in tray:
                continue
            traw = tray.get("id")
            try:
                tid = int(traw) if traw is not None else None
            except (ValueError, TypeError):
                tid = None
            if (uid, tid) not in keep:
                tray.pop("_spoof", None)


def apply_spoof_overlay(units: list, spoofs: list) -> int:
    """Rewrite spoofed backup slots in ``units`` back to their real identity.

    For each active (ENGAGED/PENDING) spoof, locate the backup tray and — only
    if the live tray still shows the spoofed identity — overlay the real color
    and sub-brand, and stamp ``tray["_spoof"]`` with the primary slot reference
    and confirmation state. The spoofed ``tray_info_idx`` is intentionally kept
    so firmware backup still fires.

    Also removes stale ``_spoof`` markers from trays not overlaid this pass
    (including when ``spoofs`` is empty), so released slots lose their badge.

    Mutates ``units`` in place. Returns the number of trays rewritten. Never
    raises.
    """
    if not isinstance(units, list):
        return 0

    # Empty / disabled fast paths still clean stale markers (cheap: the helper
    # only touches trays that carry a marker).
    if not spoofs or not _spoof_enabled():
        _strip_stale_markers(units, keep=set())
        return 0

    rewritten = 0
    keep: set = set()
    for spoof in spoofs:
        try:
            if not isinstance(spoof, dict):
                continue
            if spoof.get("state") not in _OVERLAY_STATES:
                continue

            b_ams = spoof.get("backup_ams_id")
            b_tray = spoof.get("backup_tray_id")
            tray = _find_tray(units, b_ams, b_tray)
            if tray is None:
                continue

            matches_fw = _matches_spoof(
                tray, spoof.get("spoof_tray_info_idx"), spoof.get("spoof_tray_color")
            )
            # Partial AMS pushes merge into the PREVIOUSLY OVERLAID tray (real
            # color), so the firmware-identity check fails even though the spoof
            # is healthy. Treat "already overlaid" as a match too, otherwise the
            # marker is stripped (badge/pairing vanish) on every partial push.
            already_overlaid = (
                tray.get("tray_info_idx") == spoof.get("spoof_tray_info_idx")
                and _normalize_color(tray.get("tray_color"))
                == _normalize_color(spoof.get("real_tray_color"))
            )
            if not (matches_fw or already_overlaid):
                # Firmware hasn't (yet) reported the spoofed identity (PENDING),
                # or the user swapped the spool / firmware drifted. Leave alone.
                continue

            # Overlay the real identity. Keep tray_info_idx spoofed.
            tray["tray_color"] = spoof.get("real_tray_color")
            tray["tray_sub_brands"] = spoof.get("real_tray_sub_brands")
            tray["_spoof"] = {
                "ams_id": spoof.get("primary_ams_id"),
                "tray_id": spoof.get("primary_tray_id"),
                # If firmware confirmed the identity the overlay fires; a PENDING
                # row that reaches here is effectively confirmed this pass.
                "state": "active",
            }
            try:
                keep.add((int(b_ams), int(b_tray)))
            except (ValueError, TypeError):
                pass
            rewritten += 1
        except Exception:
            # Defensive: a single malformed spoof must never break the AMS view.
            logger.debug("apply_spoof_overlay: skipping malformed spoof", exc_info=True)
            continue

    _strip_stale_markers(units, keep=keep)
    return rewritten


def strip_spoof_meta(units: list) -> None:
    """Remove the ``_spoof`` marker keys from an ams unit list (in place).

    Used by the virtual-printer bridge (slicer-facing view) and the external
    MQTT relay boundary so the internal marker never leaks onto the wire. The
    marker carries no firmware secrets, but it is bambuddy-internal. Never
    raises.
    """
    if not isinstance(units, list):
        return
    for ams_unit in units:
        if not isinstance(ams_unit, dict):
            continue
        for tray in ams_unit.get("tray", []) or []:
            if isinstance(tray, dict):
                tray.pop("_spoof", None)
