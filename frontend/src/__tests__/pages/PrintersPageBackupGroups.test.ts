/**
 * Tests for #1762 — `computeBackupGroups` strict identity rule.
 *
 * Slots pair ONLY when they share the same Bambu preset ID
 * (`tray_info_idx`). User-tagged spools without a preset never pair.
 * Empty slots are skipped; non-empty slots without a peer come back as
 * 1-member entries so the modal can list them as "Slots without a peer".
 */
import { describe, it, expect } from 'vitest';

import { computeBackupGroups } from '../../utils/amsHelpers';

function ams(id: number, tray: Array<{
  tray_type?: string | null;
  tray_sub_brands?: string | null;
  tray_color?: string | null;
  tray_info_idx?: string | null;
  is_spoofed_backup?: boolean;
  spoof_primary?: { ams_id: number; tray_id: number } | null;
}>) {
  return {
    id,
    tray: tray.map((t, i) => ({
      id: i,
      tray_type: t.tray_type ?? null,
      tray_sub_brands: t.tray_sub_brands ?? null,
      tray_color: t.tray_color ?? null,
      tray_info_idx: t.tray_info_idx ?? null,
      is_spoofed_backup: t.is_spoofed_backup,
      spoof_primary: t.spoof_primary,
    })),
  };
}

describe('computeBackupGroups', () => {
  it('returns empty list for missing/empty AMS input', () => {
    expect(computeBackupGroups(undefined, {}, false)).toEqual([]);
    expect(computeBackupGroups([], {}, false)).toEqual([]);
  });

  it('skips empty slots entirely', () => {
    const groups = computeBackupGroups(
      [ams(0, [
        { tray_type: null, tray_color: null, tray_info_idx: null },
        { tray_type: null, tray_color: null, tray_info_idx: null },
      ])],
      {},
      false,
    );
    expect(groups).toEqual([]);
  });

  it('groups two slots in different AMS units holding the same preset', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].presetId).toBe('GFA00');
    expect(groups[0].members.map((m) => m.globalTrayId)).toEqual([0, 4]);
  });

  it('STRICT rule: two slots without a preset never pair, even with matching material+colour', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#FF0000' }]),
        ams(1, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#FF0000' }]),
      ],
      {},
      false,
    );

    // Two lone slots — no pair.
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
    expect(groups.every((g) => g.presetId === null)).toBe(true);
  });

  it('does NOT group slots with different presets even if same material', () => {
    const groups = computeBackupGroups(
      [ams(0, [
        { tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' },
        { tray_type: 'PLA', tray_color: '#FFFFFF', tray_info_idx: 'GFA01' },
      ])],
      {},
      false,
    );
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
  });

  it('returns lone slots alongside pairs in the same list', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [
          { tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' },
          { tray_type: 'PETG', tray_color: '#0000FF', tray_info_idx: 'GFG99' },
        ]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    // 1 pair + 1 lone, pair first by sort order.
    expect(groups).toHaveLength(2);
    expect(groups[0].members).toHaveLength(2);
    expect(groups[1].members).toHaveLength(1);
    expect(groups[1].displayName).toContain('PETG');
  });

  it('on dual-extruder printers, scopes pairs per extruder side', () => {
    // ams 0 = right (0), ams 1 = left (1). Same preset on different sides:
    // each comes back as a 1-member entry.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      { '0': 0, '1': 1 },
      true,
    );
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
    expect(groups[0].extruder).toBe(0);
    expect(groups[1].extruder).toBe(1);
  });

  it('on dual-extruder printers, pairs slots on the same extruder side', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(2, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      { '0': 0, '1': 1, '2': 0 },
      true,
    );

    // Right-side pair (AMS 0 + 2), left-side lone (AMS 1).
    const rightPair = groups.find((g) => g.extruder === 0 && g.members.length === 2);
    expect(rightPair).toBeDefined();
    expect(rightPair!.members.map((m) => m.globalTrayId).sort((a, b) => a - b)).toEqual([0, 8]);
    const leftLone = groups.find((g) => g.extruder === 1);
    expect(leftLone).toBeDefined();
    expect(leftLone!.members).toHaveLength(1);
  });

  it('handles AMS-HT (single-tray, id >= 128) via getGlobalTrayId — pairs with regular AMS slot', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(128, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members.map((m) => m.globalTrayId).sort((a, b) => a - b)).toEqual([0, 128]);
  });

  it('STRICT colour rule: same preset, different colours do NOT pair', () => {
    // Reporter screenshot scenario — three PETG HF slots all sharing the
    // same Bambu profile ID (e.g. GFG99) but in three different colours
    // cannot back each other up; the firmware would correctly swap PETG HF
    // but the print would change colour mid-run.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG99' }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '#FF0000', tray_info_idx: 'GFG99' }]),
        ams(2, [{ tray_type: 'PETG', tray_color: '#00FF00', tray_info_idx: 'GFG99' }]),
      ],
      {},
      false,
    );
    // Three lone slots — no pair.
    expect(groups).toHaveLength(3);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
  });

  it('colour normalisation: 6-char and 8-char hex of the same RGB pair correctly', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '000000FF', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(2);
  });

  it('defensively dedupes duplicate ams.id entries (first wins)', () => {
    // Observed in the wild: status.ams sometimes contains the same ams.id
    // twice (VP-aggregated switch printers, MQTT partial-update edge cases).
    // The modal must NOT render the same slot label with conflicting
    // materials — first occurrence wins, second is dropped.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG99' }]),
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].displayName).toContain('PETG');
  });

  it('preserves display name + tray colour from the first slot for the modal swatch', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#1A1A1A', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#1A1A1A', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups[0].displayName).toBe('PLA Basic');
    expect(groups[0].trayColor).toBe('#1A1A1A');
  });

  // Explicit-pair pass — filament spoof runout backup. The firmware groups by
  // identical (spoofed) colour; bambuddy shows the backup's REAL colour, so a
  // tray with spoof metadata must be forced into its primary's group even
  // though the displayed colours differ.
  describe('explicit spoofed-backup pairs', () => {
    it('forces a spoofed backup into its primary group despite a different colour', () => {
      const groups = computeBackupGroups(
        [
          ams(0, [
            { tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' },
            {
              tray_type: 'PLA',
              tray_color: '#FF0000', // real colour ≠ primary's
              tray_info_idx: 'GFA00',
              is_spoofed_backup: true,
              spoof_primary: { ams_id: 0, tray_id: 0 },
            },
          ]),
        ],
        {},
        false,
      );
      expect(groups).toHaveLength(1);
      expect(groups[0].members.map((m) => m.globalTrayId)).toEqual([0, 1]);
      // Group swatch keeps the primary's colour; the spoofed member carries
      // its real colour for per-member rendering.
      expect(groups[0].trayColor).toBe('#000000');
      expect(groups[0].members[1].trayColor).toBe('#FF0000');
    });

    it('pairs across AMS units, with the backup listed even when it precedes the primary', () => {
      const groups = computeBackupGroups(
        [
          ams(0, [{
            tray_type: 'PETG',
            tray_color: '#00FF00',
            tray_info_idx: 'GFG00',
            is_spoofed_backup: true,
            spoof_primary: { ams_id: 1, tray_id: 0 },
          }]),
          ams(1, [{ tray_type: 'PETG', tray_color: '#0000FF', tray_info_idx: 'GFG00' }]),
        ],
        {},
        false,
      );
      expect(groups).toHaveLength(1);
      expect(groups[0].members.map((m) => m.globalTrayId).sort((a, b) => a - b)).toEqual([0, 4]);
    });

    it('falls back to colour-key logic when the spoofed primary slot is empty/missing', () => {
      const groups = computeBackupGroups(
        [
          ams(0, [
            { tray_type: null, tray_color: null, tray_info_idx: null }, // stale primary
            {
              tray_type: 'PLA',
              tray_color: '#FF0000',
              tray_info_idx: 'GFA00',
              is_spoofed_backup: true,
              spoof_primary: { ams_id: 0, tray_id: 0 },
            },
          ]),
        ],
        {},
        false,
      );
      // Not dropped — comes back as a lone 1-member entry.
      expect(groups).toHaveLength(1);
      expect(groups[0].members).toHaveLength(1);
      expect(groups[0].members[0].globalTrayId).toBe(1);
    });

    it('is_spoofed_backup without spoof_primary keeps the strict colour-key behaviour', () => {
      const groups = computeBackupGroups(
        [
          ams(0, [
            { tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' },
            {
              tray_type: 'PLA',
              tray_color: '#FF0000',
              tray_info_idx: 'GFA00',
              is_spoofed_backup: true,
              spoof_primary: null,
            },
          ]),
        ],
        {},
        false,
      );
      // Different colours, no explicit pair → two lone entries, unchanged.
      expect(groups).toHaveLength(2);
      expect(groups.every((g) => g.members.length === 1)).toBe(true);
    });

    it('#13: does NOT force a spoofed backup into a primary group on the other extruder side', () => {
      // ams 0 = right (0), ams 1 = left (1). The backup on the right side names
      // a primary on the left — the firmware can't rotate across extruders, so
      // it must fall back to a lone group rather than joining.
      const groups = computeBackupGroups(
        [
          ams(0, [{
            tray_type: 'PLA',
            tray_color: '#FF0000',
            tray_info_idx: 'GFA00',
            is_spoofed_backup: true,
            spoof_primary: { ams_id: 1, tray_id: 0 },
          }]),
          ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ],
        { '0': 0, '1': 1 },
        true,
      );
      // Two lone groups on different extruder sides — no cross-extruder join.
      expect(groups).toHaveLength(2);
      expect(groups.every((g) => g.members.length === 1)).toBe(true);
      const right = groups.find((g) => g.extruder === 0);
      const left = groups.find((g) => g.extruder === 1);
      expect(right).toBeDefined();
      expect(left).toBeDefined();
    });

    it('#13: still forces a spoofed backup into its primary group on the SAME extruder side', () => {
      const groups = computeBackupGroups(
        [
          ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
          ams(2, [{
            tray_type: 'PLA',
            tray_color: '#FF0000',
            tray_info_idx: 'GFA00',
            is_spoofed_backup: true,
            spoof_primary: { ams_id: 0, tray_id: 0 },
          }]),
        ],
        { '0': 0, '2': 0 },
        true,
      );
      expect(groups).toHaveLength(1);
      expect(groups[0].members).toHaveLength(2);
      expect(groups[0].members[1].trayColor).toBe('#FF0000');
    });

    it('no spoof metadata → behaviour identical to the strict colour-key rule', () => {
      const groups = computeBackupGroups(
        [
          ams(0, [
            { tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' },
            { tray_type: 'PLA', tray_color: '#FF0000', tray_info_idx: 'GFA00' },
          ]),
        ],
        {},
        false,
      );
      expect(groups).toHaveLength(2);
      expect(groups.every((g) => g.members.length === 1)).toBe(true);
    });
  });
});
