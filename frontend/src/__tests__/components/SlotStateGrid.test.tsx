import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BMCULinkSnapshot } from '../../api/bmcuMonitors';
import { SlotStateGrid } from '../../components/bmcu/SlotStateGrid';

const link = (linkIndex: number, linkId: string, overrides: Partial<BMCULinkSnapshot> = {}): BMCULinkSnapshot => ({
  linkIndex,
  linkId,
  state: 'online',
  currentSlot: null,
  activeMask: null,
  motion: null,
  pullPercent: null,
  pressure: null,
  faultCount: null,
  statusAgeS: 0,
  lastSeenAt: null,
  ...overrides,
});

describe('SlotStateGrid', () => {
  it('shows loader links in natural link-id order', () => {
    render(<SlotStateGrid links={[
      link(1, 'bmcu-b'),
      link(0, 'bmcu-a'),
    ]} />);

    const loaderLabels = screen
      .getAllByText((content) => content.startsWith('bmcu-'))
      .map((element) => element.textContent);

    expect(loaderLabels).toEqual(['bmcu-a', 'bmcu-b']);
  });

  it('marks a stale slot view as old instead of painting it live', () => {
    // The bridge keeps the transport busy with events while STATUS is starved,
    // so a day-old slot was rendered as the active one.
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { state: 'stale', currentSlot: 0, statusAgeS: 5 * 3600 })]} />);

    expect(screen.getByText(/5h old/)).toBeTruthy();
    expect(screen.getByText('1').parentElement?.className).not.toContain('bg-bambu-green');
  });

  it('keeps highlighting the current slot while the link is live', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { currentSlot: 0, statusAgeS: 2 })]} />);

    expect(screen.queryByText(/old/)).toBeNull();
    expect(screen.getByText('1').parentElement?.className).toContain('bg-bambu-green');
  });

  it('reports a day-old snapshot in days', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { state: 'stale', currentSlot: 0, statusAgeS: 33 * 3600 })]} />);

    expect(screen.getByText(/1d old/)).toBeTruthy();
  });

  it('says so when a link never reported loader state', () => {
    render(<SlotStateGrid links={[link(1, 'bmcu-b', { state: 'no_data', statusAgeS: null })]} />);

    expect(screen.getByText(/No loader state has ever been received/)).toBeTruthy();
    expect(screen.getByText('no data')).toBeTruthy();
  });

  it('renders an unreported mask as unknown, not as an empty loader', () => {
    // 0 is four empty channels and is a real answer; null is no answer. The
    // grid printed "filament 0x00" for both until issue #3.
    render(<SlotStateGrid links={[link(1, 'bmcu-b', { state: 'no_data' })]} />);

    expect(screen.getByText('filament —')).toBeTruthy();
    expect(screen.getByText('faults —')).toBeTruthy();
    expect(screen.queryByText(/0x00/)).toBeNull();
  });

  it('still prints a genuinely empty loader as 0x00', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { activeMask: 0, faultCount: 0, statusAgeS: 1 })]} />);

    expect(screen.getByText(/filament 0x00/)).toBeTruthy();
    expect(screen.getByText(/0 faults/)).toBeTruthy();
  });

  it('renders the four motion values instead of a stringified tuple', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { motion: [0, 2, 0, 0], statusAgeS: 1 })]} />);

    expect(screen.getByText('0, 2, 0, 0')).toBeTruthy();
  });
});
