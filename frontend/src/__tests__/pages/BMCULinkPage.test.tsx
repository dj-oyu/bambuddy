import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BMCULinkPage } from '../../pages/BMCULinkPage';
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
  });
});
