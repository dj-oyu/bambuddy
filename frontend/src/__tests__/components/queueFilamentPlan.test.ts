import { describe, expect, it } from 'vitest';
import type { FilamentPlanItem, PlannedFilament } from '../../api/client';
import {
  buildHotendSegments,
  buildUnloadMarkers,
  unloadOptionsFor,
  type PlannedJob,
} from '../../components/queueFilamentPlan';

const PURPLE: PlannedFilament = {
  tray: 4, ams_id: 1, tray_id: 0, type: 'PETG', color: '#D6ABFF', label: 'AMS1-A',
};
const GRAY: PlannedFilament = {
  tray: 1, ams_id: 0, tray_id: 1, type: 'PETG', color: '#808080', label: 'AMS0-B',
};
const WHITE: PlannedFilament = {
  tray: 3, ams_id: 0, tray_id: 3, type: 'PLA', color: '#FFFFFF', label: 'AMS0-D',
};

function planItem(over: Partial<FilamentPlanItem> & { item_id: number }): FilamentPlanItem {
  return {
    status: 'pending',
    position: over.item_id,
    trays: [],
    trays_source: 'planned',
    filaments: [],
    unload_edit: null,
    effective_mode: 'auto',
    editable: true,
    unload_at_start: false,
    unload_at_end: false,
    swap_from: null,
    swap_from_filaments: [],
    ...over,
  };
}

/** Six jobs, one hour each, back to back — the chain the timeline forecasts. */
function jobs(count: number, startMs = 0): PlannedJob[] {
  return Array.from({ length: count }, (_, i) => ({
    itemId: 201 + i,
    startMs: startMs + i * 3_600_000,
    endMs: startMs + (i + 1) * 3_600_000,
  }));
}

function planMap(items: FilamentPlanItem[]) {
  return new Map(items.map((i) => [i.item_id, i]));
}

describe('buildHotendSegments', () => {
  it('merges consecutive jobs on the same filament into one run', () => {
    const segments = buildHotendSegments(
      jobs(2),
      planMap([
        planItem({ item_id: 201, trays: [4], filaments: [PURPLE] }),
        planItem({ item_id: 202, trays: [4], filaments: [PURPLE], unload_at_start: false }),
      ])
    );
    expect(segments).toHaveLength(1);
    expect(segments[0].filaments).toEqual([PURPLE]);
    // Two printing runs inside one continuous loaded stretch.
    expect(segments[0].solid).toHaveLength(2);
    expect(segments[0].open).toBe(true);
  });

  it('splits at a swap and keeps the previous filament loaded up to it', () => {
    const segments = buildHotendSegments(
      jobs(2),
      planMap([
        planItem({ item_id: 201, trays: [4], filaments: [PURPLE] }),
        planItem({ item_id: 202, trays: [1], filaments: [GRAY], unload_at_start: true }),
      ])
    );
    expect(segments).toHaveLength(2);
    expect(segments[0].filaments).toEqual([PURPLE]);
    expect(segments[0].toMs).toBe(3_600_000); // ends where 202 begins
    expect(segments[0].open).toBe(false);
    expect(segments[1].filaments).toEqual([GRAY]);
  });

  it('closes the run at a tail unload, leaving the hotend empty', () => {
    const segments = buildHotendSegments(
      jobs(2),
      planMap([
        planItem({ item_id: 201, trays: [1], filaments: [GRAY], unload_at_end: true }),
        planItem({ item_id: 202, trays: [3], filaments: [WHITE], unload_at_start: false }),
      ])
    );
    expect(segments[0].open).toBe(false);
    expect(segments[0].toMs).toBe(3_600_000);
    expect(segments[1].filaments).toEqual([WHITE]);
  });

  it('marks the last run open when nothing pulls the filament back', () => {
    const segments = buildHotendSegments(
      jobs(1),
      planMap([planItem({ item_id: 201, trays: [3], filaments: [WHITE] })])
    );
    expect(segments[0].open).toBe(true);
  });

  it('treats an unknown swap as a break, not as continuous filament', () => {
    const segments = buildHotendSegments(
      jobs(2),
      planMap([
        planItem({ item_id: 201, trays: [4], filaments: [PURPLE] }),
        planItem({ item_id: 202, trays: null, filaments: [], unload_at_start: null }),
      ])
    );
    expect(segments).toHaveLength(2);
    expect(segments[1].unknown).toBe(true);
  });
});

describe('buildUnloadMarkers', () => {
  it('places a marker where the swap actually happens', () => {
    const markers = buildUnloadMarkers(
      jobs(3),
      planMap([
        planItem({ item_id: 201, trays: [4], filaments: [PURPLE] }),
        planItem({ item_id: 202, trays: [1], filaments: [GRAY], unload_at_start: true, swap_from: [4], swap_from_filaments: [PURPLE] }),
        planItem({ item_id: 203, trays: [1], filaments: [GRAY] }),
      ])
    );
    expect(markers).toHaveLength(1);
    expect(markers[0].atMs).toBe(3_600_000);
    expect(markers[0].kind).toBe('swap');
    expect(markers[0].prevItemId).toBe(201);
    expect(markers[0].nextItemId).toBe(202);
    expect(markers[0].fromFilaments).toEqual([PURPLE]);
    expect(markers[0].toFilaments).toEqual([GRAY]);
  });

  it('merges a tail unload and the next job start into one marker', () => {
    // The forecast chain has no gap, so both land on the same instant; two
    // markers stacked on one pixel would be unclickable.
    const markers = buildUnloadMarkers(
      jobs(2),
      planMap([
        planItem({ item_id: 201, trays: [1], filaments: [GRAY], unload_at_end: true }),
        planItem({ item_id: 202, trays: [3], filaments: [WHITE], unload_at_start: true, swap_from_filaments: [GRAY] }),
      ])
    );
    expect(markers).toHaveLength(1);
    expect(markers[0].prevItemId).toBe(201);
    expect(markers[0].nextItemId).toBe(202);
  });

  it('flags an unresolved swap as unknown', () => {
    const markers = buildUnloadMarkers(
      jobs(1),
      planMap([planItem({ item_id: 201, trays: null, filaments: [], unload_at_start: null })])
    );
    expect(markers[0].kind).toBe('unknown');
  });

  it('emits nothing when the filament never moves', () => {
    const markers = buildUnloadMarkers(
      jobs(2),
      planMap([
        planItem({ item_id: 201, trays: [4], filaments: [PURPLE] }),
        planItem({ item_id: 202, trays: [4], filaments: [PURPLE] }),
      ])
    );
    expect(markers).toHaveLength(0);
  });
});

describe('unloadOptionsFor', () => {
  const marker = {
    key: 'mk', atMs: 3_600_000, kind: 'swap' as const,
    prevItemId: 201, nextItemId: 202,
    fromFilaments: [PURPLE], toFilaments: [GRAY],
  };

  it('offers all four placements when both sides are editable', () => {
    const options = unloadOptionsFor(
      marker,
      planMap([planItem({ item_id: 201 }), planItem({ item_id: 202 })])
    );
    expect(options.map((o) => o.id)).toEqual(['carry', 'force-start', 'tail', 'raw']);
    // Both rows are on 'auto', so 'carry' is already in effect and writes nothing.
    expect(options[0].selected).toBe(true);
    expect(options[0].writes).toEqual([]);
  });

  it('normalizes the other side of the boundary when picking a placement', () => {
    const options = unloadOptionsFor(
      marker,
      planMap([
        planItem({ item_id: 201, effective_mode: 'end', unload_edit: 'end' }),
        planItem({ item_id: 202 }),
      ])
    );
    const forceStart = options.find((o) => o.id === 'force-start')!;
    // Forcing a pull-back at 202's start makes 201's tail unload redundant.
    expect(forceStart.writes).toEqual([
      { itemId: 202, mode: 'start' },
      { itemId: 201, mode: 'auto' },
    ]);
  });

  it('reflects the mode already in effect', () => {
    const options = unloadOptionsFor(
      marker,
      planMap([
        planItem({ item_id: 201, effective_mode: 'end', unload_edit: 'end' }),
        planItem({ item_id: 202 }),
      ])
    );
    expect(options.find((o) => o.id === 'tail')!.selected).toBe(true);
    expect(options.find((o) => o.id === 'tail')!.writes).toEqual([]);
    expect(options.find((o) => o.id === 'carry')!.selected).toBe(false);
    expect(options.find((o) => o.id === 'carry')!.writes).toEqual([{ itemId: 201, mode: 'auto' }]);
  });

  it('drops placements that would need a job already running', () => {
    const options = unloadOptionsFor(
      marker,
      planMap([
        planItem({ item_id: 201, status: 'printing', editable: false }),
        planItem({ item_id: 202 }),
      ])
    );
    // 201 has already started, so nothing can be written to its tail.
    expect(options.map((o) => o.id)).toEqual(['carry', 'force-start']);
  });

  it('offers only tail-side placements for the last job in the queue', () => {
    const lastMarker = { ...marker, nextItemId: null, toFilaments: [] };
    const options = unloadOptionsFor(lastMarker, planMap([planItem({ item_id: 201 })]));
    expect(options.map((o) => o.id)).toEqual(['carry', 'tail', 'raw']);
  });
});
