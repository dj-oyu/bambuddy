/**
 * Render tests for the Filament Spoof Runout Backup modal.
 *
 * Covers #13 (per-extruder candidate scoping), filament-type equivalence via
 * canonicalFilamentType, and the design-rule-D K-difference soft warning.
 * Copy stays mechanism-free — tests assert on slot labels / testids, not
 * translated strings.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';

import { render } from '../utils';
import { FilamentSpoofModal } from '../../components/FilamentSpoofModal';

function tray(over: Record<string, unknown>) {
  return {
    id: 0,
    tray_color: '#000000',
    tray_type: 'PLA',
    tray_sub_brands: 'PLA Basic',
    tray_id_name: null,
    tray_info_idx: 'GFA00',
    remain: 50,
    k: 0.02,
    cali_idx: null,
    tag_uid: null,
    tray_uuid: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    drying_temp: null,
    drying_time: null,
    state: 11,
    ...over,
  };
}

function unit(id: number, trays: Array<Record<string, unknown>>) {
  return {
    id,
    humidity: null,
    temp: null,
    is_ams_ht: false,
    tray: trays.map((t, i) => tray({ id: i, ...t })),
    serial_number: '',
    sw_ver: '',
    dry_time: 0,
    dry_status: 0,
    dry_sub_status: 0,
    dry_sf_reason: [],
    dry_target_temp: null,
    dry_filament: null,
    module_type: 'ams',
  } as never;
}

describe('FilamentSpoofModal', () => {
  it('lists a same-type candidate from another slot', () => {
    render(
      <FilamentSpoofModal
        isOpen
        primary={{ amsId: 0, slotIdx: 0 }}
        amsUnits={[unit(0, [{ tray_type: 'PLA' }, { tray_type: 'PLA' }])]}
        pending={false}
        onConfirm={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    // Candidate is slot 2 of AMS-A → label "AMS-A 2".
    expect(screen.getByText('AMS-A 2')).toBeInTheDocument();
  });

  it('matches material by canonical type (PA-CF ≡ PA12-CF)', () => {
    render(
      <FilamentSpoofModal
        isOpen
        primary={{ amsId: 0, slotIdx: 0 }}
        amsUnits={[unit(0, [
          { tray_type: 'PA-CF', tray_info_idx: 'GFN00' },
          { tray_type: 'PA12-CF', tray_info_idx: 'GFN01' },
        ])]}
        pending={false}
        onConfirm={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText('AMS-A 2')).toBeInTheDocument();
  });

  it('#13: on dual-nozzle, hides candidates on the other extruder side', () => {
    render(
      <FilamentSpoofModal
        isOpen
        primary={{ amsId: 0, slotIdx: 0 }}
        amsUnits={[
          unit(0, [{ tray_type: 'PLA' }]),
          unit(1, [{ tray_type: 'PLA' }]), // other extruder
          unit(2, [{ tray_type: 'PLA' }]), // same extruder as primary
        ]}
        amsExtruderMap={{ '0': 0, '1': 1, '2': 0 }}
        isDualNozzle
        pending={false}
        onConfirm={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    // Single-tray units render as "HT-x". Same-side (unit 2) is a candidate;
    // other-side (unit 1) is filtered out.
    expect(screen.getByText('HT-C 1')).toBeInTheDocument();
    expect(screen.queryByText('HT-B 1')).not.toBeInTheDocument();
  });

  it('#13: FTS (extruder-agnostic) allows candidates across extruder sides', () => {
    render(
      <FilamentSpoofModal
        isOpen
        primary={{ amsId: 0, slotIdx: 0 }}
        amsUnits={[
          unit(0, [{ tray_type: 'PLA' }]),
          unit(1, [{ tray_type: 'PLA' }]),
        ]}
        amsExtruderMap={{ '0': 0, '1': 1 }}
        isDualNozzle
        extruderAgnostic
        pending={false}
        onConfirm={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText('HT-B 1')).toBeInTheDocument();
  });

  it('rule D: shows the K-difference warning only after a diverging backup is selected', () => {
    render(
      <FilamentSpoofModal
        isOpen
        primary={{ amsId: 0, slotIdx: 0 }}
        amsUnits={[unit(0, [
          { tray_type: 'PLA', k: 0.02 },
          { tray_type: 'PLA', k: 0.08 }, // |0.02 - 0.08| = 0.06 > 0.01
        ])]}
        pending={false}
        onConfirm={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('spoof-k-warning')).toBeNull();
    fireEvent.click(screen.getByText('AMS-A 2'));
    expect(screen.getByTestId('spoof-k-warning')).toBeInTheDocument();
  });

  it('rule D: no warning when K values are within tolerance', () => {
    render(
      <FilamentSpoofModal
        isOpen
        primary={{ amsId: 0, slotIdx: 0 }}
        amsUnits={[unit(0, [
          { tray_type: 'PLA', k: 0.02 },
          { tray_type: 'PLA', k: 0.025 }, // within 0.01
        ])]}
        pending={false}
        onConfirm={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText('AMS-A 2'));
    expect(screen.queryByTestId('spoof-k-warning')).toBeNull();
  });
});
