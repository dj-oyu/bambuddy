import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BMCULinkPage } from '../../pages/BMCULinkPage';
import { bmcuLinkApi } from '../../api/client';
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
  return { ...actual, bmcuLinkApi: { ...actual.bmcuLinkApi, getEvents: vi.fn() } };
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

  it('marks an aging loader snapshot as stale between updates', () => {
    const link = { ...(fixture.detail as BMCUMonitorDetail).links[0], state: 'online', statusAgeS: 14 } as const;
    const { rerender } = render(<SlotStateGrid links={[link]} />);
    expect(screen.getByText('Live')).toBeTruthy();

    rerender(<SlotStateGrid links={[link]} elapsedSeconds={2} />);
    expect(screen.getByText('Stale')).toBeTruthy();
    expect(screen.getByText('Last STATUS 16s ago')).toBeTruthy();
  });
});
