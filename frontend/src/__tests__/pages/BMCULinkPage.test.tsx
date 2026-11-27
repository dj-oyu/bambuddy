import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BMCULinkPage } from '../../pages/BMCULinkPage';
import { api, bmcuLinkApi, type Printer } from '../../api/client';
import { bmcuMonitorsApi } from '../../api/bmcuMonitors';
import { SlotStateGrid } from '../../components/bmcu/SlotStateGrid';
import type { BMCUMonitorDetail, BMCUMonitorSummary, BMCUTimelineResponse } from '../../api/bmcuMonitors';
import fixture from '../fixtures/bmcuMonitorApi.json';

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
  return { ...actual, api: { ...actual.api, getPrinters: vi.fn(), getBMCUGcodeWindow: vi.fn() }, bmcuLinkApi: { ...actual.bmcuLinkApi, getEvents: vi.fn() } };
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
      transaction_id: null, data: { event_name: 'printer_transaction', severity: 3 },
    }]);
    vi.mocked(bmcuMonitorsApi.list).mockResolvedValue(fixture.list as BMCUMonitorSummary[]);
    vi.mocked(bmcuMonitorsApi.get).mockResolvedValue(fixture.detail as BMCUMonitorDetail);
    vi.mocked(bmcuMonitorsApi.streamDiagnostics).mockResolvedValue({
      deviceId: fixture.detail.deviceId, health: 'online', sourceAddress: '192.0.2.10',
      queueDepth: 2, counters: { datagrams_received: 120, records_persisted: 450, gaps_detected: 4 },
    });
    vi.mocked(bmcuMonitorsApi.timeline).mockResolvedValue(fixture.timeline as BMCUTimelineResponse);
  });

  it('renders monitor, loader state, and integrated timeline', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><BMCULinkPage /></QueryClientProvider>);

    expect(await screen.findByText('Pico loader')).toBeTruthy();
    await waitFor(() => expect(bmcuMonitorsApi.timeline).toHaveBeenCalled());
    expect(screen.getByText('Loader state')).toBeTruthy();
    const monitorCard = screen.getByRole('button', { name: /Pico loader/ }).parentElement!;
    expect(within(monitorCard).getByText('Monitor status')).toBeTruthy();
    expect(within(monitorCard).getByText('UDP stream')).toBeTruthy();
    expect(await within(monitorCard).findByText('192.0.2.10')).toBeTruthy();
    expect(screen.getByText('Missing sequences detected')).toBeTruthy();
    expect(screen.getByText('Loader timeline')).toBeTruthy();
    expect(screen.getByText('control error')).toBeTruthy();
    expect(screen.queryByText('Pico hardware')).toBeNull();
    expect(bmcuMonitorsApi.metrics).not.toHaveBeenCalled();
    expect(await screen.findByText('printer_transaction')).toBeTruthy();
    expect(bmcuLinkApi.getEvents).toHaveBeenCalledWith(fixture.detail.deviceId, { kind: undefined, limit: 50, offset: 0 });
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
    const context = { canvas: { width: 640, height: 280 }, clearRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn() };
    const canvas = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D);
    const bytes = new Uint8Array(new Float32Array([0, 0, 1, 1, -1]).buffer);
    const encoded = btoa(String.fromCharCode(...bytes));
    vi.mocked(api.getBMCUGcodeWindow).mockResolvedValue({
      archiveId: 1, jobName: 'Test job', plate: 1, reportedLayer: 3, totalLayers: 10, progress: 20,
      firstLayer: 1, lastLayer: 2, layers: [encoded, ''], layerFreshness: 'unknown',
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><BMCULinkPage /></QueryClientProvider>);
    const option = await screen.findByRole('option', { name: 'My printer' });
    expect(api.getBMCUGcodeWindow).not.toHaveBeenCalled();
    fireEvent.change(option.closest('select')!, { target: { value: '1' } });
    expect(await screen.findByText('Layers 1–2')).toBeTruthy();
    expect(api.getBMCUGcodeWindow).toHaveBeenCalledWith(1, expect.any(AbortSignal));
    expect(screen.getByText(/Layer and progress report freshness is unknown/)).toBeTruthy();
    expect(screen.getByRole('img', { name: 'Planned extrusion paths, layers 1 to 2' })).toBeTruthy();
    expect(screen.getByText('Tool unknown')).toBeTruthy();
    expect(context.stroke).toHaveBeenCalled();
    canvas.mockRestore();
  });

  it('marks an aging loader snapshot as stale between updates', () => {
    const link = { ...(fixture.detail as BMCUMonitorDetail).links[0], state: 'online', statusAgeS: 14 } as const;
    const { rerender } = render(<SlotStateGrid links={[link]} />);
    expect(screen.getByText('Live')).toBeTruthy();

    rerender(<SlotStateGrid links={[link]} elapsedSeconds={2} />);
    expect(screen.getByText('Stale')).toBeTruthy();
    expect(screen.getByText('Last STATUS 16s ago')).toBeTruthy();
  });
});
