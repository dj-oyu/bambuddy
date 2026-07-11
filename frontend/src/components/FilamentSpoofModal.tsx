/**
 * Filament Spoof Runout Backup modal.
 *
 * Opens from a slot's action menu ("Set runout backup"). The clicked slot is
 * the PRIMARY (the nearly-empty spool); the user picks a BACKUP slot holding
 * the same material type that takes over automatically on runout.
 *
 * Implementation note (NOT surfaced in the UI copy): the backend registers
 * the backup on the printer under the primary's colour so the firmware
 * auto-switches, while bambuddy keeps displaying the backup's real colour.
 *
 * Incompatible materials are filtered out entirely (the firmware would purge
 * PLA into a PETG print otherwise); the confirm button stays disabled until
 * a backup is selected.
 *
 * Theme-aware via CSS variables, matching AmsBackupModal.
 */
import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { AMSUnit } from '../api/client';
import { canonicalFilamentType, getAmsLabel, normalizeColor } from '../utils/amsHelpers';

export interface SpoofSlotPick {
  amsId: number;
  slotIdx: number;
}

interface FilamentSpoofModalProps {
  isOpen: boolean;
  /** The nearly-empty spool the backup will cover. */
  primary: SpoofSlotPick;
  amsUnits: AMSUnit[] | undefined;
  /** {ams_id: extruder_id} from printer status (0 = right, 1 = left). */
  amsExtruderMap?: Record<string, number>;
  /** True for H2D-class dual-nozzle machines. */
  isDualNozzle?: boolean;
  /**
   * FTS (Filament Track Switch) installed: any slot routes to either extruder,
   * so per-extruder candidate filtering must be skipped (#13).
   */
  extruderAgnostic?: boolean;
  pending: boolean;
  onConfirm: (backup: SpoofSlotPick) => void;
  onClose: () => void;
}

/** "AMS-A 2" / "HT-A 1" — unit label plus 1-based slot number. */
function slotLabel(amsId: number, slotIdx: number, trayCount: number): string {
  return `${getAmsLabel(amsId, trayCount)} ${slotIdx + 1}`;
}

export function FilamentSpoofModal({
  isOpen,
  primary,
  amsUnits,
  amsExtruderMap,
  isDualNozzle = false,
  extruderAgnostic = false,
  pending,
  onConfirm,
  onClose,
}: FilamentSpoofModalProps) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<SpoofSlotPick | null>(null);

  // Reset selection whenever the modal targets a different primary slot.
  useEffect(() => {
    setSelected(null);
  }, [primary.amsId, primary.slotIdx, isOpen]);

  // Close on Escape key while the modal is open (window-level capture,
  // matching AmsBackupModal).
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  // Theme-aware tokens, matching AmsBackupModal.
  const modalBg = 'var(--bg-secondary)';
  const sectionBg = 'var(--bg-primary)';
  const borderColor = 'var(--border-color)';
  const textPrimary = 'var(--text-primary)';
  const textSecondary = 'var(--text-secondary)';

  const primaryUnit = amsUnits?.find((u) => u.id === primary.amsId);
  const primaryTray = primaryUnit?.tray[primary.slotIdx];
  const primaryType = canonicalFilamentType(primaryTray?.tray_type || undefined);
  const primaryK = primaryTray?.k ?? null;
  const primarySlotLabel = slotLabel(
    primary.amsId,
    primary.slotIdx,
    primaryUnit?.tray.length ?? 4,
  );

  // Per-extruder scoping (#13, H2D-class): a backup must live on the same
  // nozzle side as the primary — the firmware can't rotate across extruders.
  // Skip when single-nozzle, when the primary's extruder is unknown, or when
  // an FTS makes slots extruder-agnostic.
  const primaryExtruder = amsExtruderMap?.[String(primary.amsId)];
  const doExtruderFilter =
    isDualNozzle && !extruderAgnostic && primaryExtruder != null;

  // Candidate backups: same (canonical) material type, same extruder side, not
  // the primary itself, not already engaged as a spoofed backup. Incompatible
  // materials are filtered out entirely rather than shown disabled.
  const candidates: Array<{
    amsId: number;
    slotIdx: number;
    label: string;
    color: string | null;
    name: string;
    k: number | null;
  }> = [];
  for (const unit of amsUnits || []) {
    if (doExtruderFilter) {
      const candExtruder = amsExtruderMap?.[String(unit.id)];
      if (candExtruder != null && candExtruder !== primaryExtruder) continue;
    }
    unit.tray.forEach((tray, slotIdx) => {
      if (!tray?.tray_type) return;
      if (unit.id === primary.amsId && slotIdx === primary.slotIdx) return;
      if (tray.is_spoofed_backup) return;
      if (canonicalFilamentType(tray.tray_type || undefined) !== primaryType) return;
      candidates.push({
        amsId: unit.id,
        slotIdx,
        label: slotLabel(unit.id, slotIdx, unit.tray.length),
        color: tray.tray_color ?? null,
        name: tray.tray_sub_brands || tray.tray_type || '',
        k: tray.k ?? null,
      });
    });
  }

  // Design rule D: non-blocking flow-behaviour warning when the selected
  // backup's calibration (k) differs from the primary's by more than 0.01.
  const selectedCandidate = selected
    ? candidates.find((c) => c.amsId === selected.amsId && c.slotIdx === selected.slotIdx)
    : undefined;
  const showKWarning =
    selectedCandidate != null &&
    primaryK != null &&
    selectedCandidate.k != null &&
    Math.abs(primaryK - selectedCandidate.k) > 0.01;

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
      onClick={onClose}
      data-testid="filament-spoof-modal"
    >
      <div
        className="rounded-xl w-full max-w-md max-h-[90vh] overflow-hidden shadow-xl flex flex-col"
        style={{ backgroundColor: modalBg }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="filament-spoof-modal-title"
      >
        <div
          className="flex items-center justify-between px-5 py-3 border-b"
          style={{ borderColor }}
        >
          <h2
            id="filament-spoof-modal-title"
            className="text-base font-semibold"
            style={{ color: textPrimary }}
          >
            {t('printers.filamentSpoof.modalTitle')}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-md transition-colors hover:bg-black/10"
            style={{ color: textSecondary }}
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div
          className="px-5 py-3 border-b"
          style={{ borderColor, backgroundColor: sectionBg }}
        >
          <div className="text-sm font-medium flex items-center gap-2" style={{ color: textPrimary }}>
            <span
              className="inline-block w-3.5 h-3.5 rounded-full border border-black/20 flex-shrink-0"
              style={{ backgroundColor: normalizeColor(primaryTray?.tray_color || undefined) }}
            />
            {t('printers.filamentSpoof.primaryLabel', { slot: primarySlotLabel })}
          </div>
          <p className="text-xs mt-1" style={{ color: textSecondary }}>
            {t('printers.filamentSpoof.modalHelp')}
          </p>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {candidates.length === 0 ? (
            <p className="text-sm text-center py-6" style={{ color: textSecondary }}>
              {t('printers.filamentSpoof.noCandidates', { type: primaryTray?.tray_type || '?' })}
            </p>
          ) : (
            <>
              <p className="text-xs mb-2" style={{ color: textSecondary }}>
                {t('printers.filamentSpoof.sameTypeOnly', { type: primaryTray?.tray_type || '?' })}
              </p>
              <div className="space-y-1">
                {candidates.map((c) => {
                  const isSelected = selected?.amsId === c.amsId && selected?.slotIdx === c.slotIdx;
                  return (
                    <button
                      key={`${c.amsId}-${c.slotIdx}`}
                      type="button"
                      onClick={() => setSelected({ amsId: c.amsId, slotIdx: c.slotIdx })}
                      className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg border text-left text-sm transition-colors ${
                        isSelected ? 'border-bambu-green' : 'hover:bg-black/10'
                      }`}
                      style={{
                        color: textPrimary,
                        borderColor: isSelected ? undefined : borderColor,
                        backgroundColor: isSelected ? sectionBg : undefined,
                      }}
                      aria-pressed={isSelected}
                    >
                      <span
                        className="inline-block w-3.5 h-3.5 rounded-full border border-black/20 flex-shrink-0"
                        style={{ backgroundColor: normalizeColor(c.color || undefined) }}
                      />
                      <span className="font-medium">{c.label}</span>
                      <span className="truncate" style={{ color: textSecondary }}>
                        {c.name}
                      </span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {showKWarning && (
          <div
            className="px-5 py-2 border-t text-xs"
            style={{ borderColor, color: '#b45309', backgroundColor: sectionBg }}
            role="status"
            data-testid="spoof-k-warning"
          >
            {t('printers.filamentSpoof.kDiffWarning')}
          </div>
        )}

        <div
          className="flex items-center justify-end gap-2 px-5 py-3 border-t"
          style={{ borderColor, backgroundColor: sectionBg }}
        >
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-sm transition-colors hover:bg-black/10"
            style={{ color: textSecondary }}
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            disabled={!selected || pending}
            onClick={() => selected && onConfirm(selected)}
            className="px-3 py-1.5 rounded-lg text-sm font-medium bg-bambu-green text-white transition-opacity disabled:opacity-50"
          >
            {t('printers.filamentSpoof.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
