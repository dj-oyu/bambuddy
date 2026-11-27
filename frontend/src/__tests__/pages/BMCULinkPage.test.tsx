import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BMCULinkPage } from '../../pages/BMCULinkPage';
import { api, bmcuLinkApi, type Printer } from '../../api/client';
import { bmcuMonitorsApi } from '../../api/bmcuMonitors';
import { SlotStateGrid } from '../../components/bmcu/SlotStateGrid';
import type { BMCUMonitorDetail, BMCUMonitorSummary, BMCUTimelineResponse } from '../../api/bmcuMonitors';
import fixture from '../fixtures/bmcuMonitorApi.json';

vi.mock('../../components/bmcu/VirtualPrintCamera', () => ({
  VirtualPrintCamera: ({ gcodeUrl, reportedLayer, toolColor }: { gcodeUrl: string; reportedLayer: number; toolColor: (tool: number) => string }) => <div role="img" aria-label="Virtual print" data-url={gcodeUrl} data-layer={reportedLayer} data-color={toolColor(1)} />,
}));

vi.mock('../../api/bmcuMonitors', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/bmcuMonitors')>();
  return {
    ...actual,
    bmcuMonitorsApi: {
      list: vi.fn(),
      get: vi.fn(),
      streamDiagnostics: vi.fn(),
      timeline: vi.fn(),
      metrics: vi.fn(),
    },
  };
});

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return { ...actual, api: { ...actual.api, getPrinters: vi.fn(), getBMCUPrintContext: vi.fn(), getPrinterStatus: vi.fn(), getArchiveFilamentRequirements: vi.fn() }, bmcuLinkApi: { ...actual.bmcuLinkApi, getEvents: vi.fn() } };
});

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ComposedChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  ReferenceArea: () => null,
  Line: () => null,
  Scatter: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  Cell: () => null,
}));

describe('BMCULinkPage', () => {
  beforeEach(() => {
    vi.mocked(api.getPrinters).mockResolvedValue([]);
    vi.mocked(bmcuLinkApi.getEvents).mockResolvedValue([{
      id: -1, device_id: fixture.detail.deviceId, kind: 'event', kind_id: 3,
      protocol: 1, received_at_us: 1000, server_received_at: new Date().toISOString(),
      transaction_id: null, link_id: 'bmcu-0', data: { event_name: 'printer_transaction', severity: 3, payload: 'first' },
    }, {
      id: -2, device_id: fixture.detail.deviceId, kind: 'event', kind_id: 3,
      protocol: 1, received_at_us: 999, server_received_at: new Date().toISOString(),
      transaction_id: null, link_id: 'bmcu-0', data: { event_name: 'printer_transaction', severity: 3, payload: 'second' },
    }, {
      id: -3, device_id: fixture.detail.deviceId, kind: 'event', kind_id: 3,
      protocol: 1, received_at_us: 998, server_received_at: new Date().toISOString(),
      transaction_id: null, link_id: 'bmcu-1', data: { event_name: 'state_change', severity: 1, field: 4, slot: 1, previous_value: 0, value: 2 },
    }]);
    vi.mocked(bmcuMonitorsApi.list).mockResolvedValue(fixture.list as BMCUMonitorSummary[]);
    vi.mocked(bmcuMonitorsApi.get).mockResolvedValue(fixture.detail as BMCUMonitorDetail);
    vi.mocked(bmcuMonitorsApi.streamDiagnostics).mockResolvedValue({
      deviceId: fixture.detail.deviceId, health: 'online', sourceAddress: '192.0.2.10',
      queueDepth: 2, counters: { datagrams_received: 120, records_persisted: 450, gaps_detected: 4 },
    });
    vi.mocked(bmcuMonitorsApi.timeline).mockResolvedValue({ ...fixture.timeline, points: [{ ...fixture.timeline.points[1], at: new Date().toISOString() }] } as BMCUTimelineResponse);
  });

  it('renders monitor, loader state, and integrated timeline', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><BMCULinkPage /></QueryClientProvider>);

    expect(await screen.findByText('Pico loader')).toBeTruthy();
    await waitFor(() => expect(bmcuLinkApi.getEvents).toHaveBeenCalled());
    expect(screen.getByText('Loader state')).toBeTruthy();
    const monitorCard = screen.getByRole('button', { name: /Pico loader/ }).parentElement!;
    expect(within(monitorCard).queryByText('Monitor status')).toBeNull();
    expect(within(monitorCard).queryByText('UDP stream')).toBeNull();
    expect(within(monitorCard).getByText('UDP online')).toBeTruthy();
    expect(await within(monitorCard).findByText('192.0.2.10')).toBeTruthy();
    fireEvent.click(within(monitorCard).getByText('Connection details'));
    expect(within(monitorCard).getByText('Missing sequences detected')).toBeTruthy();
    expect(screen.getByText('History')).toBeTruthy();
    expect(screen.queryByText('Loader timeline')).toBeNull();
    expect(screen.queryByText('Pico hardware')).toBeNull();
    expect(bmcuMonitorsApi.metrics).not.toHaveBeenCalled();
    expect(await screen.findByText('printer transaction')).toBeTruthy();
    expect(screen.getByText('×2')).toBeTruthy();
    expect(screen.getByText('motion · slot 2: idle → in use')).toBeTruthy();
    expect(await screen.findByText('control error')).toBeTruthy();
    expect(bmcuLinkApi.getEvents).toHaveBeenCalledWith(fixture.detail.deviceId, expect.objectContaining({ limit: 101, offset: 0, fromTime: expect.any(String), toTime: expect.any(String) }));
    fireEvent.click(screen.getByText('printer transaction'));
    expect(screen.getByText(/"payload": "first"/)).toBeTruthy();
    expect(screen.getByText(/"payload": "second"/)).toBeTruthy();
  });

  it('shows diagnostics inside the selected monitor card when switching devices', async () => {
    const first = fixture.list[0] as BMCUMonitorSummary;
    const second = { ...first, deviceId: 'second-pico', displayName: 'Second Pico' };
    vi.mocked(bmcuMonitorsApi.list).mockResolvedValue([first, second]);
    vi.mocked(bmcuMonitorsApi.get).mockImplementation(async (id) => ({ ...fixture.detail, deviceId: id } as BMCUMonitorDetail));
    vi.mocked(bmcuMonitorsApi.streamDiagnostics).mockImplementation(async (id) => ({
      deviceId: id, health: 'online', sourceAddress: id === 'second-pico' ? '192.0.2.20' : '192.0.2.10',
      queueDepth: 0, counters: {},
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><BMCULinkPage /></QueryClientProvider>);
    await screen.findByText('192.0.2.10');
    const secondButton = screen.getByRole('button', { name: /Second Pico/ });
    fireEvent.click(secondButton);
    expect(await within(secondButton.parentElement!).findByText('192.0.2.20')).toBeTruthy();
    expect(screen.queryByText('192.0.2.10')).toBeNull();
  });

  it('only loads planned G-code for an explicitly selected printer', async () => {
    vi.mocked(api.getPrinters).mockResolvedValue([{ id: 1, name: 'My printer' } as Printer]);
    vi.mocked(api.getBMCUPrintContext).mockResolvedValue({ archiveId: 1, jobName: 'Test job', plate: 2, reportedLayer: 3, totalLayers: 10, progress: 20 });
    vi.mocked(api.getPrinterStatus).mockResolvedValue({ connected: true, current_archive_id: 1, ams_mapping: [0, 1], ams: [{ id: 0, tray: [{ id: 1, tray_color: 'FEC600FF' }] }] } as never);
    vi.mocked(api.getArchiveFilamentRequirements).mockResolvedValue({ filaments: [] } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><BMCULinkPage /></QueryClientProvider>);
    const option = await screen.findByRole('option', { name: 'My printer' });
    expect(api.getBMCUPrintContext).not.toHaveBeenCalled();
    fireEvent.change(option.closest('select')!, { target: { value: '1' } });
    const view = await screen.findByRole('img', { name: 'Virtual print' });
    expect(api.getBMCUPrintContext).toHaveBeenCalledWith(1, expect.any(AbortSignal));
    expect(view.getAttribute('data-url')).toContain('/archives/1/gcode?plate=2');
    expect(view.getAttribute('data-layer')).toBe('3');
    await waitFor(() => expect(view.getAttribute('data-color')).toBe('#FEC600'));
    act(() => client.setQueryData(['bmcu-print-context', '1'], {
      archiveId: 1, jobName: 'Test job', plate: 2, reportedLayer: 4, totalLayers: 10, progress: 30,
    }));
    await waitFor(() => expect(view.getAttribute('data-layer')).toBe('4'));
    expect(screen.getByRole('img', { name: 'Virtual print' })).toBe(view);
  });

  it('marks an aging loader snapshot as stale between updates', () => {
    const link = { ...(fixture.detail as BMCUMonitorDetail).links[0], state: 'online', statusAgeS: 14 } as const;
    const { rerender } = render(<SlotStateGrid links={[link]} />);
    expect(screen.getByText('Live')).toBeTruthy();

    rerender(<SlotStateGrid links={[link]} elapsedSeconds={2} />);
    expect(screen.getByText('Stale')).toBeTruthy();
    expect(screen.getByTitle('BMCU status 16s ago')).toBeTruthy();
    expect(screen.queryByText(/Last STATUS/)).toBeNull();
  });

  it('animates only the selected live loading or unloading command', () => {
    const link = { ...(fixture.detail as BMCUMonitorDetail).links[0], state: 'online', statusAgeS: 1, currentSlot: 1, motion: [0, 1, 0, 0] } as const;
    const { container, rerender } = render(<SlotStateGrid links={[link]} />);
    expect(screen.getByText('Loading requested')).toBeTruthy();
    expect(container.querySelectorAll('.bmcu-route-moving')).toHaveLength(1);
    rerender(<SlotStateGrid links={[{ ...link, motion: [0, 2, 0, 0] }]} />);
    expect(screen.getByText('In use')).toBeTruthy();
    expect(container.querySelectorAll('.bmcu-route-moving')).toHaveLength(0);
    rerender(<SlotStateGrid links={[{ ...link, motion: [0, 4, 0, 0] }]} />);
    expect(screen.getByText('Unloading requested')).toBeTruthy();
    expect(container.querySelector('.bmcu-route-moving')?.getAttribute('style')).toContain('bmcu-route-reverse');
    rerender(<SlotStateGrid links={[link]} elapsedSeconds={20} />);
    expect(container.querySelectorAll('.bmcu-route-moving')).toHaveLength(0);
  });

  it('visualizes the buffer and distinguishes pressure readings from protocol codes', () => {
    const link = { ...(fixture.detail as BMCUMonitorDetail).links[0], state: 'online', statusAgeS: 1, currentSlot: 1, pullPercent: 51, pressure: 1310 } as const;
    const { rerender } = render(<SlotStateGrid links={[link]} />);
    expect(screen.getByRole('img', { name: 'Buffer position 51%, Near neutral; 50% is neutral' })).toBeTruthy();
    expect(screen.getByRole('img', { name: 'Pressure response: 2%' })).toBeTruthy();
    expect(screen.queryByText(/Selected slot:/)).toBeNull();
    const marker = screen.getByRole('img', { name: 'Buffer position 51%, Near neutral; 50% is neutral' }).querySelector('rect');
    const middle = Number(marker?.getAttribute('x'));
    rerender(<SlotStateGrid links={[{ ...link, pullPercent: 80, pressure: Math.floor(30 * 65535 / 50) }]} />);
    expect(Number(marker?.getAttribute('x'))).toBeGreaterThan(middle);
    expect(screen.getByRole('img', { name: 'Pressure response: 60%' })).toBeTruthy();
    rerender(<SlotStateGrid links={[{ ...link, pressure: 0xffff }]} />);
    expect(screen.getByRole('img', { name: 'Pressure response: Unavailable' })).toBeTruthy();
    rerender(<SlotStateGrid links={[{ ...link, pressure: 0xf06f }]} />);
    expect(screen.getByRole('img', { name: 'Pressure response: Jam reported' })).toBeTruthy();
    rerender(<SlotStateGrid links={[{ ...link, pressure: 0x4700 }]} />);
    expect(screen.getByRole('img', { name: 'Pressure response: Controller code' })).toBeTruthy();
  });
});
