import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { AlertTriangle, ArrowRight, Check, HelpCircle } from 'lucide-react';
import type { PlannedFilament } from '../api/client';
import {
  describeFilaments,
  filamentColor,
  inkOn,
  type HotendSegment,
  type MarkerKind,
  type UnloadMarker,
  type UnloadOption,
  type UnloadOptionId,
} from './queueFilamentPlan';

/** Rendering for the queue Gantt's filament lane (private fork): the hotend's
 *  own occupancy over time, plus the markers that are the edit surface for
 *  where a swap happens. Shaping logic lives in `queueFilamentPlan.ts`. */

/* ---------------------------------------------------------------------- */
/* Rendering                                                               */
/* ---------------------------------------------------------------------- */

function Swatch({ filaments }: { filaments: PlannedFilament[] }) {
  if (!filaments.length) {
    return <span className="inline-block w-3 h-3 rounded-sm border border-bambu-dark-tertiary bg-bambu-dark" />;
  }
  return (
    <span className="inline-flex gap-0.5">
      {filaments.map((f) => (
        <span
          key={f.tray}
          className="inline-block w-3 h-3 rounded-sm border border-white/25"
          style={{ background: filamentColor(f) }}
        />
      ))}
    </span>
  );
}

/** The marker's picture of the operation.
 *
 *  The A1 cuts before it retracts — the tail pull-back block drives the head to
 *  the cutter at X181, and the AMS change routine cuts the resident filament
 *  before loading the next one. So scissors are literal here, not decorative,
 *  and their absence is information too: loading into a hotend the previous job
 *  already emptied cuts nothing.
 *
 *  Which side the thread reaches into says which job runs the operation — the
 *  one whose G-code contains it. */
function FilamentCutGlyph({ kind }: { kind: MarkerKind }) {
  const cuts = kind !== 'load';
  const threadOut = kind !== 'load';
  const threadIn = kind === 'load' || kind === 'swap';
  return (
    <svg
      viewBox="0 0 24 20"
      className="w-6 h-5 overflow-visible"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      aria-hidden
    >
      {threadOut && (
        <line
          x1="0"
          y1="10"
          x2="7.6"
          y2="10"
          strokeDasharray={kind === 'unknown' ? '2 2' : undefined}
          className="filament-cut-thread filament-cut-thread-out"
        />
      )}
      {threadIn && (
        <line
          x1="16.8"
          y1="10"
          x2="24"
          y2="10"
          className="filament-cut-thread filament-cut-thread-in"
        />
      )}
      {cuts ? (
        <>
          <line x1="8.6" y1="6.6" x2="17" y2="13" className="filament-cut-blade filament-cut-blade-a" />
          <line x1="8.6" y1="13.4" x2="17" y2="7" className="filament-cut-blade filament-cut-blade-b" />
          <circle cx="7.5" cy="5.9" r="1.35" />
          <circle cx="7.5" cy="14.1" r="1.35" />
        </>
      ) : (
        <polyline points="9.5,6.4 14.8,10 9.5,13.6" />
      )}
    </svg>
  );
}

const MARKER_TONE: Record<MarkerKind, string> = {
  // Shape carries the meaning now, so colour only sets emphasis: a swap is the
  // consequential one, a load into an empty hotend the most benign.
  swap: 'bg-cyan-500/15 border-cyan-400/60 text-cyan-300',
  unload: 'bg-emerald-500/15 border-emerald-400/60 text-emerald-300',
  load: 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray',
  unknown: 'bg-orange-500/15 border-orange-400/70 text-orange-300',
};

const MARKER_LABEL: Record<MarkerKind, string> = {
  swap: 'queue.filament.event.swap',
  unload: 'queue.filament.event.unload',
  load: 'queue.filament.event.load',
  unknown: 'queue.filament.event.unknown',
};

/** Same glyph at legend size, so the key and the chart can't drift apart. */
export function FilamentMarkerLegendIcon({ kind }: { kind: MarkerKind }) {
  return (
    <span className={`grid place-items-center w-7 h-6 rounded border shrink-0 ${MARKER_TONE[kind]}`}>
      <FilamentCutGlyph kind={kind} />
    </span>
  );
}

interface HotendLaneProps {
  segments: HotendSegment[];
  markers: UnloadMarker[];
  rangeStartMs: number;
  rangeMs: number;
  height: number;
  warningItemIds: Set<number>;
  /** Marker that was just edited — snips once so the change is felt. */
  pulsingKey: string | null;
  onMarkerClick: (marker: UnloadMarker, anchor: DOMRect) => void;
  t: (key: string, options?: Record<string, unknown>) => string;
}

export function HotendLane({
  segments,
  markers,
  rangeStartMs,
  rangeMs,
  height,
  warningItemIds,
  pulsingKey,
  onMarkerClick,
  t,
}: HotendLaneProps) {
  const rangeEndMs = rangeStartMs + rangeMs;
  const pctOf = (ms: number) => ((ms - rangeStartMs) / rangeMs) * 100;

  return (
    <div className="relative" style={{ height }}>
      {segments.map((seg) => {
        const startMs = Math.max(rangeStartMs, seg.fromMs);
        const endMs = Math.min(rangeEndMs, seg.open ? rangeEndMs : seg.toMs);
        if (endMs <= rangeStartMs || startMs >= rangeEndMs) return null;
        const color = filamentColor(seg.filaments[0]);
        const label = describeFilaments(seg.filaments);
        return (
          <div key={seg.key}>
            {/* Carried-over body: loaded but no job consuming it. */}
            <div
              className="absolute rounded-sm border-y border-white/20"
              style={{
                left: `${pctOf(startMs)}%`,
                width: `${((endMs - startMs) / rangeMs) * 100}%`,
                top: 6,
                height: height - 12,
                backgroundImage: `repeating-linear-gradient(135deg, ${color} 0 4px, transparent 4px 9px)`,
                opacity: seg.unknown ? 0.45 : 0.85,
              }}
              title={
                seg.open
                  ? `${label} · ${t('queue.filament.stillLoaded')}`
                  : `${label} · ${t('queue.filament.carriedOver')}`
              }
            />
            {/* Printing runs. */}
            {seg.solid.map(([s, e], i) => {
              const cs = Math.max(rangeStartMs, s);
              const ce = Math.min(rangeEndMs, e);
              if (ce <= rangeStartMs || cs >= rangeEndMs) return null;
              return (
                <div
                  key={`${seg.key}-solid-${i}`}
                  className="absolute rounded-sm border border-white/25 flex items-center px-1.5 overflow-hidden"
                  style={{
                    left: `${pctOf(cs)}%`,
                    width: `${((ce - cs) / rangeMs) * 100}%`,
                    top: 6,
                    height: height - 12,
                    background: color,
                    color: inkOn(color),
                  }}
                  title={label}
                >
                  {i === 0 && (
                    <span className="text-[10px] font-medium truncate leading-none">{label}</span>
                  )}
                </div>
              );
            })}
            {seg.open && (
              <div
                className="absolute text-[10px] text-orange-600 dark:text-orange-400 flex items-center gap-1 pointer-events-none"
                style={{ left: `calc(${pctOf(Math.min(endMs, rangeEndMs))}% - 4px)`, top: height / 2 - 7 }}
              >
                <AlertTriangle className="w-3 h-3" />
              </div>
            )}
          </div>
        );
      })}

      {markers.map((mk) => {
        if (mk.atMs < rangeStartMs || mk.atMs > rangeEndMs) return null;
        const flagged =
          (mk.prevItemId != null && warningItemIds.has(mk.prevItemId)) ||
          (mk.nextItemId != null && warningItemIds.has(mk.nextItemId));
        const tone = MARKER_TONE[flagged ? 'unknown' : mk.kind];
        const what = t(MARKER_LABEL[mk.kind]);
        return (
          <div key={mk.key}>
            {/* The glyph slides off the boundary when it has to share the
                instant; this keeps the moment itself readable. */}
            {mk.offsetPx !== 0 && (
              <div
                className="absolute top-1 bottom-1 border-l border-dashed border-white/15 pointer-events-none"
                style={{ left: `${pctOf(mk.atMs)}%` }}
                aria-hidden
              />
            )}
            <button
              type="button"
              onClick={(e) => onMarkerClick(mk, e.currentTarget.getBoundingClientRect())}
              title={`${what} · ${describeFilaments(mk.fromFilaments)} → ${describeFilaments(mk.toFilaments)} · ${t('queue.filament.editSwap')}`}
              className={`filament-cut absolute z-10 grid place-items-center w-7 h-6 rounded border focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-400 ${tone} ${
                pulsingKey === mk.key ? 'filament-cut-snip' : ''
              }`}
              style={{ left: `${pctOf(mk.atMs)}%`, top: height / 2 - 12, marginLeft: -14 + mk.offsetPx }}
            >
              <FilamentCutGlyph kind={mk.kind} />
              <span className="sr-only">{`${what} — ${t('queue.filament.editSwap')}`}</span>
            </button>
          </div>
        );
      })}
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/* Popover                                                                 */
/* ---------------------------------------------------------------------- */

interface UnloadModePopoverProps {
  marker: UnloadMarker;
  anchor: DOMRect;
  options: UnloadOption[];
  busy: boolean;
  canEdit: boolean;
  warnings: string[];
  onPick: (option: UnloadOption) => void;
  onClose: () => void;
  t: (key: string, options?: Record<string, unknown>) => string;
}

const OPTION_LABELS: Record<UnloadOptionId, { label: string; desc: string }> = {
  carry: { label: 'queue.filament.mode.carry', desc: 'queue.filament.mode.carryDesc' },
  'force-start': { label: 'queue.filament.mode.forceStart', desc: 'queue.filament.mode.forceStartDesc' },
  tail: { label: 'queue.filament.mode.tail', desc: 'queue.filament.mode.tailDesc' },
  raw: { label: 'queue.filament.mode.raw', desc: 'queue.filament.mode.rawDesc' },
};

export function UnloadModePopover({
  marker,
  anchor,
  options,
  busy,
  canEdit,
  warnings,
  onPick,
  onClose,
  t,
}: UnloadModePopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: anchor.left, top: anchor.bottom + 8 });

  useLayoutEffect(() => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const margin = 8;
    let left = anchor.left + anchor.width / 2 - box.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - box.width - margin));
    let top = anchor.bottom + margin;
    if (top + box.height > window.innerHeight - margin) {
      top = Math.max(margin, anchor.top - box.height - margin);
    }
    setPos({ left, top });
  }, [anchor]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      role="dialog"
      aria-label={t('queue.filament.editSwap')}
      className="fixed z-50 w-[19rem] rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary shadow-2xl overflow-hidden"
      style={{ left: pos.left, top: pos.top }}
    >
      <div className="px-3 py-2.5 border-b border-bambu-dark-tertiary">
        <div className="flex items-center gap-2 text-xs text-white">
          <Swatch filaments={marker.fromFilaments} />
          <span className="truncate">{describeFilaments(marker.fromFilaments)}</span>
          <ArrowRight className="w-3 h-3 shrink-0 text-bambu-gray" />
          <Swatch filaments={marker.toFilaments} />
          <span className="truncate">{describeFilaments(marker.toFilaments)}</span>
        </div>
        {marker.kind === 'unknown' && (
          <div className="mt-1.5 flex items-start gap-1.5 text-[11px] text-orange-600 dark:text-orange-400">
            <HelpCircle className="w-3 h-3 mt-0.5 shrink-0" />
            <span>{t('queue.filament.unknownSwap')}</span>
          </div>
        )}
      </div>

      {warnings.length > 0 && (
        <div className="px-3 py-2 border-b border-bambu-dark-tertiary bg-orange-500/5 space-y-1">
          {warnings.map((w) => (
            <div key={w} className="flex items-start gap-1.5 text-[11px] text-orange-600 dark:text-orange-400">
              <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {canEdit ? (
        <div className="py-1">
          {options.map((opt) => (
            <button
              key={opt.id}
              type="button"
              disabled={busy || opt.disabled}
              onClick={() => onPick(opt)}
              title={opt.disabled ? t('queue.filament.lockedByRunningJob') : undefined}
              className={`w-full text-left px-3 py-2 flex gap-2.5 items-start transition-colors ${
                opt.disabled ? 'opacity-45 cursor-not-allowed' : 'hover:bg-bambu-dark disabled:cursor-wait'
              }`}
            >
              <span
                className={`mt-0.5 w-4 h-4 rounded-full border shrink-0 flex items-center justify-center ${
                  opt.selected ? 'border-bambu-green bg-bambu-green/20' : 'border-bambu-dark-tertiary'
                }`}
              >
                {opt.selected && <Check className="w-2.5 h-2.5 text-bambu-green" />}
              </span>
              <span className="min-w-0">
                <span className="block text-xs text-white leading-tight">
                  {t(OPTION_LABELS[opt.id].label)}
                  {opt.selected && opt.disabled && (
                    <span className="ml-1.5 text-[10px] text-bambu-gray">
                      {t('queue.filament.inEffectLocked')}
                    </span>
                  )}
                </span>
                <span className="block text-[11px] text-bambu-gray leading-snug mt-0.5">
                  {t(OPTION_LABELS[opt.id].desc)}
                </span>
              </span>
            </button>
          ))}
        </div>
      ) : (
        <div className="px-3 py-3 text-[11px] text-bambu-gray">{t('queue.filament.notEditable')}</div>
      )}
    </div>
  );
}
