import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen } from '@testing-library/react';
import { render } from '../utils';
import { CameraTile } from '../../components/CameraTile';

describe('CameraTile', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(global, 'fetch').mockResolvedValue(new Response(null, { status: 200 }));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('renders the live stream URL in live mode', () => {
    render(
      <CameraTile
        printerId={42}
        printerName="X1C-Lab"
        mode="live"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    const img = screen.getByAltText('X1C-Lab') as HTMLImageElement;
    expect(img.src).toContain('/api/v1/printers/42/camera/stream');
    expect(img.src).toContain('fps=8');
  });

  it('renders the snapshot URL and refreshes on the interval', () => {
    render(
      <CameraTile
        printerId={7}
        printerName="P1S-Garage"
        mode="snapshot"
        snapshotIntervalMs={1000}
        connected
      />,
    );
    const initial = (screen.getByAltText('P1S-Garage') as HTMLImageElement).src;
    expect(initial).toContain('/api/v1/printers/7/camera/snapshot');

    act(() => {
      vi.advanceTimersByTime(1500);
    });
    const refreshed = (screen.getByAltText('P1S-Garage') as HTMLImageElement).src;
    expect(refreshed).toContain('/api/v1/printers/7/camera/snapshot');
    expect(refreshed).not.toBe(initial);
  });

  it('shows an offline placeholder when not connected', () => {
    render(
      <CameraTile
        printerId={1}
        printerName="A1-Offline"
        mode="live"
        snapshotIntervalMs={5000}
        connected={false}
      />,
    );
    expect(screen.queryByAltText('A1-Offline')).toBeNull();
  });

  it('shows the paused placeholder in paused mode', () => {
    render(
      <CameraTile
        printerId={9}
        printerName="H2D-Booth"
        mode="paused"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    expect(screen.queryByAltText('H2D-Booth')).toBeNull();
  });

  it('POSTs /camera/stop when leaving live mode', async () => {
    const fetchMock = vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(null, { status: 200 }),
    );
    const { rerender } = render(
      <CameraTile
        printerId={11}
        printerName="X1C-Stop"
        mode="live"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    fetchMock.mockClear();

    rerender(
      <CameraTile
        printerId={11}
        printerName="X1C-Stop"
        mode="snapshot"
        snapshotIntervalMs={5000}
        connected
      />,
    );

    const stopCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/api/v1/printers/11/camera/stop'),
    );
    expect(stopCalls.length).toBeGreaterThan(0);
  });
});
