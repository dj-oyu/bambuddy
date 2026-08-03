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
  /** This run begins with a swap, which happens inside the *next* job's start
   *  G-code — so the gap is carved off this segment's head. */
  trimStart: boolean;
  /** This run ends with a tail pull-back, which happens inside the *previous*
   *  job's end G-code — so the gap is carved off this segment's tail.
   *
   *  Which side the gap is carved from is the whole point: it puts the empty
   *  hotend inside the time range of the job whose G-code actually performs the
   *  unload, without needing a label to say so. */
  trimEnd: boolean;
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
  /** Horizontal nudge, in px, onto the middle of the gap this marker belongs
   *  to — left for a tail unload (carved off the previous band), right for a
   *  swap (carved off the next band's head). */
  offsetPx: number;
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
    // true or unknown: either way the resident filament comes out here, inside
    // this job's own start G-code.
    const startsWithSwap = plan.unload_at_start !== false;
    if (current && startsWithSwap) {
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
        trimStart: startsWithSwap,
        trimEnd: false,
      };
    }
    current.solid.push([job.startMs, job.endMs]);
    if (plan.unload_at_end === true) {
      current.trimEnd = true;
      close(current, job.endMs, false);
      current = null;
    }
  }
  if (current) close(current, current.solid[current.solid.length - 1][1], true);
  return segments;
}

/** Markers sit on the middle of the gap they belong to rather than on the raw
 *  instant, so the diamond reads as part of that gap. `EMPTY_GAP_PX / 2` in
 *  `QueueFilamentLane`. */
const MARKER_GAP_CENTRE_PX = 4;
/** Extra separation when two markers still land on the same instant. */
const COINCIDENT_NUDGE_PX = 6;

/** Markers at every moment filament actually moves.
 *
 *  Only where something is pulled out: a job's tail unload, and a start that
 *  swaps the resident filament. A start that merely loads into an already-empty
 *  hotend gets none — the gap the lane opens ahead of that segment already
 *  shows both that the hotend was empty and where it stopped being empty, and a
 *  marker on top of it would be one symbol too many. */
export function buildUnloadMarkers(
  jobs: PlannedJob[],
  planItems: Map<number, FilamentPlanItem>
): UnloadMarker[] {
  const markers: UnloadMarker[] = [];

  jobs.forEach((job, i) => {
    const plan = planItems.get(job.itemId);
    if (!plan) return;
    const nextJob = jobs[i + 1];
    const prevJob = jobs[i - 1];

    if (plan.end_action === 'unload') {
      markers.push({
        key: `mk-${job.itemId}-end`,
        atMs: job.endMs,
        kind: 'unload',
        prevItemId: job.itemId,
        nextItemId: nextJob ? nextJob.itemId : null,
        fromFilaments: plan.filaments,
        toFilaments: nextJob ? planItems.get(nextJob.itemId)?.filaments ?? [] : [],
        // The gap for a tail unload is carved off the previous band's end, so
        // it sits to the LEFT of this instant.
        offsetPx: -MARKER_GAP_CENTRE_PX,
      });
    }

    // 'none' carries the same filament on and 'load' is shown by the gap, so
    // only a swap (or an unresolvable one) needs a marker here.
    if (plan.start_action === 'swap' || plan.start_action === null) {
      markers.push({
        key: `mk-${job.itemId}-start`,
        atMs: job.startMs,
        kind: plan.start_action === null ? 'unknown' : 'swap',
        prevItemId: prevJob ? prevJob.itemId : null,
        nextItemId: job.itemId,
        fromFilaments: plan.swap_from_filaments,
        toFilaments: plan.filaments,
        // A swap's gap is carved off the next band's head — to the RIGHT.
        offsetPx: MARKER_GAP_CENTRE_PX,
      });
    }
  });

  // Two markers can still land on one instant (a tail unload followed by a
  // forced pull-back). Push them further apart, each staying on its own side.
  const byMs = new Map<number, UnloadMarker[]>();
  for (const marker of markers) {
    const group = byMs.get(marker.atMs) ?? [];
    group.push(marker);
    byMs.set(marker.atMs, group);
  }
  for (const group of byMs.values()) {
    if (group.length < 2) continue;
    for (const marker of group) {
      marker.offsetPx += marker.offsetPx < 0 ? -COINCIDENT_NUDGE_PX : COINCIDENT_NUDGE_PX;
    }
  }

  return markers.sort((a, b) => a.atMs - b.atMs || a.offsetPx - b.offsetPx);
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
