import { act, render, screen } from '@testing-library/react';
import { Profiler } from 'react';
import { describe, expect, it, vi } from 'vitest';
import type { BMCULinkSnapshot, BMCUMonitorDetail } from '../../api/bmcuMonitors';
import type { AMSUnit } from '../../api/client';
import { SlotStateGrid } from '../../components/bmcu/SlotStateGrid';
import { publishLoader } from '../../components/bmcu/liveUpdates';
import fixture from '../fixtures/bmcuMonitorApi.json';

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
  it('updates live values without a React render or rebuilding the loader nodes', () => {
    const renders = vi.fn();
    const deviceId = 'live-pico';
    const initial = link(0, 'bmcu-a', { currentSlot: 0, motion: [0, 0, 0, 0] });
    const { unmount } = render(<Profiler id="loader" onRender={renders}><SlotStateGrid deviceId={deviceId} liveConnected links={[initial]} /></Profiler>);
    const firstSlot = screen.getByText('1').parentElement;
    const before = renders.mock.calls.length;
    const detail = { ...fixture.detail, deviceId, links: [link(0, 'bmcu-a', { currentSlot: 1, motion: [0, 1, 0, 0] })] } as BMCUMonitorDetail;
    act(() => publishLoader(detail));
    expect(renders).toHaveBeenCalledTimes(before);
    expect(screen.getByText('1').parentElement).toBe(firstSlot);
    expect(firstSlot?.dataset.selected).toBe('false');
    expect(screen.getByText('2').parentElement?.dataset.selected).toBe('true');
    expect(screen.getByText('Loading requested')).toBeTruthy();
    unmount();
    act(() => publishLoader(detail));
  });

  it('switches back to polling data after the stream disconnects', () => {
    const deviceId = 'live-pico';
    const initial = link(0, 'bmcu-a', { currentSlot: 0 });
    const { rerender } = render(<SlotStateGrid deviceId={deviceId} liveConnected links={[initial]} />);
    const detail = { ...fixture.detail, deviceId, links: [link(0, 'bmcu-a', { currentSlot: 1 })] } as BMCUMonitorDetail;
    act(() => publishLoader(detail));
    expect(screen.getByText('2').parentElement?.dataset.selected).toBe('true');
    rerender(<SlotStateGrid deviceId={deviceId} liveConnected={false} links={[initial]} />);
    expect(screen.getByText('1').parentElement?.dataset.selected).toBe('true');
    expect(screen.getByText('2').parentElement?.dataset.selected).toBe('false');
  });

  it('treats device-supplied labels as text', () => {
    const unsafe = '<img src=x onerror=alert(1)>';
    const { container } = render(<SlotStateGrid links={[link(0, unsafe)]} />);
    expect(screen.getByText(unsafe)).toBeTruthy();
    expect(container.querySelector('img')).toBeNull();
  });
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

    expect(screen.getByLabelText('Stale: BMCU status 5h ago')).toBeTruthy();
    expect(screen.getByText('1').parentElement?.dataset.selected).toBe('false');
  });

  it('keeps highlighting the current slot while the link is live', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { currentSlot: 0, statusAgeS: 2 })]} />);

    expect(screen.queryByText(/old/)).toBeNull();
    expect(screen.getByText('1').parentElement?.dataset.selected).toBe('true');
  });

  it('reports a day-old snapshot in days', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { state: 'stale', currentSlot: 0, statusAgeS: 33 * 3600 })]} />);

    expect(screen.getByLabelText('Stale: BMCU status 1d ago')).toBeTruthy();
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
    expect(screen.getByText('CRC / frame errors: unknown')).toBeTruthy();
    expect(screen.queryByText(/0x00/)).toBeNull();
  });

  it('still prints a genuinely empty loader as 0x00', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { activeMask: 0, faultCount: 0, statusAgeS: 1 })]} />);

    expect(screen.getByText(/filament 0x00/)).toBeTruthy();
    expect(screen.getByText('CRC / frame errors: 0 (total)')).toBeTruthy();
  });

  it('shows the selected motion state and retains raw values in diagnostics', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { currentSlot: 1, motion: [0, 2, 0, 0], statusAgeS: 1 })]} />);

    expect(screen.getByText('In use')).toBeTruthy();
    expect(screen.getByText(/Motion 0, 2, 0, 0/)).toBeTruthy();
  });
  it('distinguishes selected, empty, unknown and stale filament reports', () => {
    const { rerender } = render(<SlotStateGrid links={[link(0, 'bmcu-a', { currentSlot: 0, activeMask: 1 })]} />);
    expect(screen.getByLabelText('Slot 1: selected, filament present')).toBeTruthy();
    expect(screen.getByLabelText('Slot 2: filament empty')).toBeTruthy();
    rerender(<SlotStateGrid links={[link(0, 'bmcu-a', { state: 'stale', currentSlot: 0, activeMask: null, statusAgeS: 60 })]} />);
    expect(screen.getByLabelText('Slot 1: last selected, filament unknown, last reported')).toBeTruthy();
  });

  it('colors only present slots using the uniquely reported AMS number', () => {
    const units = [{ id: 1, tray: [{ id: 0, tray_color: 'FEC600FF', tray_type: 'PLA' }] }] as AMSUnit[];
    const linked = link(0, 'bmcu-a', { amsIndex: 1, currentSlot: 0, activeMask: 1 });
    const { rerender } = render(<SlotStateGrid links={[linked]} filamentUnits={units} />);
    const slot = screen.getByLabelText('Slot 1: selected, filament present, PLA, #FEC600');
    expect(slot.querySelector('circle')?.getAttribute('fill')).toBe('#FEC600');
    expect(slot.textContent).toBe('1');
    rerender(<SlotStateGrid links={[{ ...linked, activeMask: 0 }]} filamentUnits={units} />);
    expect(screen.getByLabelText('Slot 1: selected, filament empty').querySelector('circle')?.getAttribute('fill')).toBe('none');
    rerender(<SlotStateGrid links={[linked, link(1, 'bmcu-b', { amsIndex: 1 })]} filamentUnits={units} />);
    expect(screen.getByLabelText('Slot 1: selected, filament present').querySelector('circle')?.getAttribute('fill')).toBe('#798a8d');
  });

});
