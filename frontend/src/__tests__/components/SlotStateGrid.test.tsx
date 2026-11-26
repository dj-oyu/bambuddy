import { act, render, screen } from '@testing-library/react';
import { Profiler } from 'react';
import { describe, expect, it, vi } from 'vitest';
import type { BMCULinkSnapshot, BMCUMonitorDetail } from '../../api/bmcuMonitors';
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
    expect(firstSlot?.className).not.toContain('bg-bambu-green');
    expect(screen.getByText('2').parentElement?.className).toContain('bg-bambu-green');
    expect(screen.getByText('0, 1, 0, 0')).toBeTruthy();
    unmount();
    act(() => publishLoader(detail));
  });

  it('switches back to polling data after the stream disconnects', () => {
    const deviceId = 'live-pico';
    const initial = link(0, 'bmcu-a', { currentSlot: 0 });
    const { rerender } = render(<SlotStateGrid deviceId={deviceId} liveConnected links={[initial]} />);
    const detail = { ...fixture.detail, deviceId, links: [link(0, 'bmcu-a', { currentSlot: 1 })] } as BMCUMonitorDetail;
    act(() => publishLoader(detail));
    expect(screen.getByText('2').parentElement?.className).toContain('bg-bambu-green');
    rerender(<SlotStateGrid deviceId={deviceId} liveConnected={false} links={[initial]} />);
    expect(screen.getByText('1').parentElement?.className).toContain('bg-bambu-green');
    expect(screen.getByText('2').parentElement?.className).not.toContain('bg-bambu-green');
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

    expect(screen.getByText('Last STATUS 5h ago')).toBeTruthy();
    expect(screen.getByText('1').parentElement?.className).not.toContain('bg-bambu-green');
  });

  it('keeps highlighting the current slot while the link is live', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { currentSlot: 0, statusAgeS: 2 })]} />);

    expect(screen.queryByText(/old/)).toBeNull();
    expect(screen.getByText('1').parentElement?.className).toContain('bg-bambu-green');
  });

  it('reports a day-old snapshot in days', () => {
    render(<SlotStateGrid links={[link(0, 'bmcu-a', { state: 'stale', currentSlot: 0, statusAgeS: 33 * 3600 })]} />);

    expect(screen.getByText('Last STATUS 1d ago')).toBeTruthy();
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
