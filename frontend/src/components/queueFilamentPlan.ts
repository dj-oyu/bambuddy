import type { FilamentPlanItem, PlannedFilament, UnloadEditMode } from '../api/client';

/** Shaping the server's filament plan into what the queue Gantt draws
 *  (private fork). Pure — see `QueueFilamentLane.tsx` for the rendering.
 *
 *  The deferred-unload patch strips each job's tail unload and lets the next
 *  job's start G-code do the swap, so a spool can sit in the hotend across a
 *  job boundary — invisible on a lane that only draws jobs. These helpers turn
 *  the plan into hotend occupancy (printing vs merely loaded vs never pulled
 *  back) and into the markers that mark, and edit, where filament moves. */

/** A job placed on the timeline. Structural, so the lane never imports the
 *  timeline's own scheduling types. */
export interface PlannedJob {
  itemId: number;
  startMs: number;
  endMs: number;
}

export interface HotendSegment {
  key: string;
  filaments: PlannedFilament[];
  fromMs: number;
  toMs: number;
  /** Nothing in the plan unloads this — it stays in the hotend past the queue. */
  open: boolean;
  /** Ranges where a job is actually printing with it. */
  solid: Array<[number, number]>;
  unknown: boolean;
}

export type MarkerKind = 'swap' | 'unload' | 'unknown';

export interface UnloadMarker {
  key: string;
  atMs: number;
  kind: MarkerKind;
  /** Job ending here, if any. */
  prevItemId: number | null;
  /** Job starting here, if any. */
  nextItemId: number | null;
  fromFilaments: PlannedFilament[];
  toFilaments: PlannedFilament[];
}

const NEUTRAL = '#6b7280';

export function filamentColor(f: PlannedFilament | undefined): string {
  return f?.color || NEUTRAL;
}

/** Readable ink for a swatch background. */
export function inkOn(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return '#ffffff';
  const v = parseInt(m[1], 16);
  const lum = (0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)) / 255;
  return lum > 0.6 ? '#111827' : '#ffffff';
}

export function describeFilaments(list: PlannedFilament[]): string {
  if (!list.length) return '—';
  return list.map((f) => [f.label, f.type].filter(Boolean).join(' ')).join(' + ');
}

/** Walk the planned jobs and derive what sits in the hotend over time.
 *
 *  Driven entirely by the server's `unload_at_start` / `unload_at_end` so the
 *  drawing can't disagree with the plan. `unload_at_start === null` (unknown)
 *  closes the current run too — an unknown swap must not render as continuous
 *  filament. */
export function buildHotendSegments(
  jobs: PlannedJob[],
  planItems: Map<number, FilamentPlanItem>
): HotendSegment[] {
  const segments: HotendSegment[] = [];
  let current: HotendSegment | null = null;

  const close = (seg: HotendSegment, endMs: number, open: boolean) => {
    seg.toMs = endMs;
    seg.open = open;
    segments.push(seg);
  };

  for (const job of jobs) {
    const plan = planItems.get(job.itemId);
    if (!plan) {
      if (current) close(current, job.startMs, false);
      current = null;
      continue;
    }
    if (current && plan.unload_at_start !== false) {
      close(current, job.startMs, false);
      current = null;
    }
    if (!current) {
      current = {
        key: `seg-${job.itemId}`,
        filaments: plan.filaments,
        fromMs: job.startMs,
        toMs: job.endMs,
        open: false,
        solid: [],
        unknown: plan.trays == null,
      };
    }
    current.solid.push([job.startMs, job.endMs]);
    if (plan.unload_at_end === true) {
      close(current, job.endMs, false);
      current = null;
    }
  }
  if (current) close(current, current.solid[current.solid.length - 1][1], true);
  return segments;
}

/** Markers at every moment filament actually moves. Two adjacent jobs share an
 *  instant (the chain has no gap), so a tail unload and the next job's forced
 *  pull-back merge into one marker rather than stacking on the same pixel. */
export function buildUnloadMarkers(
  jobs: PlannedJob[],
  planItems: Map<number, FilamentPlanItem>
): UnloadMarker[] {
  const byMs = new Map<number, UnloadMarker>();

  const put = (atMs: number, patch: Partial<UnloadMarker> & { kind: MarkerKind }) => {
    const existing = byMs.get(atMs);
    if (existing) {
      byMs.set(atMs, {
        ...existing,
        ...patch,
        prevItemId: patch.prevItemId ?? existing.prevItemId,
        nextItemId: patch.nextItemId ?? existing.nextItemId,
        fromFilaments: existing.fromFilaments.length ? existing.fromFilaments : patch.fromFilaments ?? [],
        toFilaments: patch.toFilaments?.length ? patch.toFilaments : existing.toFilaments,
        // An unknown swap outranks a plain unload: it needs the user's eye.
        kind: existing.kind === 'unknown' || patch.kind === 'unknown' ? 'unknown' : patch.kind,
      });
      return;
    }
    byMs.set(atMs, {
      key: `mk-${atMs}`,
      atMs,
      prevItemId: null,
      nextItemId: null,
      fromFilaments: [],
      toFilaments: [],
      ...patch,
    });
  };

  jobs.forEach((job, i) => {
    const plan = planItems.get(job.itemId);
    if (!plan) return;
    const nextJob = jobs[i + 1];
    const prevJob = jobs[i - 1];

    if (plan.unload_at_end === true) {
      put(job.endMs, {
        kind: 'unload',
        prevItemId: job.itemId,
        nextItemId: nextJob ? nextJob.itemId : null,
        fromFilaments: plan.filaments,
        toFilaments: nextJob ? planItems.get(nextJob.itemId)?.filaments ?? [] : [],
      });
    }
    if (plan.unload_at_start !== false && plan.unload_at_start !== undefined) {
      put(job.startMs, {
        kind: plan.unload_at_start === null ? 'unknown' : 'swap',
        prevItemId: prevJob ? prevJob.itemId : null,
        nextItemId: job.itemId,
        fromFilaments: plan.swap_from_filaments,
        toFilaments: plan.filaments,
      });
    }
  });

  return Array.from(byMs.values()).sort((a, b) => a.atMs - b.atMs);
}

/* ---------------------------------------------------------------------- */
/* Editing                                                                 */
/* ---------------------------------------------------------------------- */

export type UnloadOptionId = 'carry' | 'force-start' | 'tail' | 'raw';

export interface UnloadOption {
  id: UnloadOptionId;
  /** The placement currently in effect — read from the real modes on both
   *  sides, whether or not those rows can still be edited. */
  selected: boolean;
  /** A row this option would have to change has already started. */
  disabled: boolean;
  /** Rows to PATCH, in order. Empty when the option is already in effect. */
  writes: Array<{ itemId: number; mode: UnloadEditMode }>;
}

/** Where this boundary's unload currently happens, from both sides' modes. */
function currentPlacement(
  prev: FilamentPlanItem | undefined,
  next: FilamentPlanItem | undefined
): UnloadOptionId {
  if (next?.effective_mode === 'start') return 'force-start';
  if (prev?.effective_mode === 'none') return 'raw';
  if (prev?.effective_mode === 'end') return 'tail';
  return 'carry';
}

/** Which placements this marker can offer, and what each one writes.
 *
 *  A swap is a property of *two* rows — the job that could pull the filament
 *  back at its end, and the job that could force a pull-back at its start — so
 *  each option normalizes both sides, but only writes the rows that actually
 *  need to change.
 *
 *  Editability gates *writing*, never *reading*. A running job set to "unload
 *  at end" must still show as the selected placement; deriving the selection
 *  from editable rows only made it read as "automatic", contradicting the row
 *  badge and the lane. */
export function unloadOptionsFor(
  marker: UnloadMarker,
  planItems: Map<number, FilamentPlanItem>
): UnloadOption[] {
  const prev = marker.prevItemId != null ? planItems.get(marker.prevItemId) : undefined;
  const next = marker.nextItemId != null ? planItems.get(marker.nextItemId) : undefined;
  const current = currentPlacement(prev, next);

  const options: UnloadOption[] = [];
  const add = (id: UnloadOptionId, writes: UnloadOption['writes']) => {
    const blocked = writes.some((w) => !planItems.get(w.itemId)?.editable);
    options.push({ id, selected: current === id, disabled: blocked, writes: blocked ? [] : writes });
  };

  const carryWrites: UnloadOption['writes'] = [];
  if (prev && prev.effective_mode !== 'auto') carryWrites.push({ itemId: prev.item_id, mode: 'auto' });
  if (next && next.effective_mode === 'start') carryWrites.push({ itemId: next.item_id, mode: 'auto' });
  add('carry', carryWrites);

  if (next) {
    const writes: UnloadOption['writes'] = [];
    if (next.effective_mode !== 'start') writes.push({ itemId: next.item_id, mode: 'start' });
    if (prev && prev.effective_mode === 'end') writes.push({ itemId: prev.item_id, mode: 'auto' });
    add('force-start', writes);
  }

  if (prev) {
    const tailWrites: UnloadOption['writes'] = [];
    if (prev.effective_mode !== 'end') tailWrites.push({ itemId: prev.item_id, mode: 'end' });
    if (next && next.effective_mode === 'start') tailWrites.push({ itemId: next.item_id, mode: 'auto' });
    add('tail', tailWrites);

    add('raw', prev.effective_mode === 'none' ? [] : [{ itemId: prev.item_id, mode: 'none' }]);
  }

  return options;
}
