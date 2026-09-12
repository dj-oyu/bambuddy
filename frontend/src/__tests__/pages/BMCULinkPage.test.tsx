import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BMCULinkPage } from '../../pages/BMCULinkPage';
import { bmcuLinkApi } from '../../api/client';
import { bmcuMonitorsApi } from '../../api/bmcuMonitors';
import type { BMCUMetricPoint, BMCUMonitorDetail, BMCUMonitorSummary, BMCUTimelineResponse } from '../../api/bmcuMonitors';
import fixture from '../fixtures/bmcuMonitorApi.json';

vi.mock('../../api/bmcuMonitors', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/bmcuMonitors')>();
  return {
    ...actual,
    bmcuMonitorsApi: {
      list: vi.fn(),
      get: vi.fn(),
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
    vi.mocked(bmcuMonitorsApi.timeline).mockResolvedValue(fixture.timeline as BMCUTimelineResponse);
    vi.mocked(bmcuMonitorsApi.metrics).mockResolvedValue(fixture.metrics as BMCUMetricPoint[]);
  });

  it('renders monitor, loader state, integrated timeline and hardware metrics', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><BMCULinkPage /></QueryClientProvider>);

    expect(await screen.findByText('Pico loader')).toBeTruthy();
    await waitFor(() => expect(bmcuMonitorsApi.timeline).toHaveBeenCalled());
    expect(screen.getByText('Loader state')).toBeTruthy();
    expect(screen.getByText('Loader timeline')).toBeTruthy();
    expect(screen.getByText('control error')).toBeTruthy();
    expect(screen.getByText('Pico hardware')).toBeTruthy();
    expect(screen.getByText('42.1 °C')).toBeTruthy();
    expect(screen.getByText('-55 dBm')).toBeTruthy();
    expect(await screen.findByText('printer_transaction')).toBeTruthy();
    expect(bmcuLinkApi.getEvents).toHaveBeenCalledWith(fixture.detail.deviceId, { kind: undefined, limit: 50, offset: 0 });
  });
});
