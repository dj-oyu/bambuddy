import { memo, useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, Cable, Clock3, RadioTower, RefreshCw } from 'lucide-react';
import { bmcuMonitorsApi } from '../api/bmcuMonitors';
import { BMCUHealthBadge, MonitorCard } from '../components/bmcu/MonitorCard';
import { formatBMCUAge, formatBMCUDateTime, bmcuTimestamp } from '../components/bmcu/bmcuTime';
import { HardwareMetrics } from '../components/bmcu/HardwareMetrics';
import { LoaderTimeline } from '../components/bmcu/LoaderTimeline';
import { SlotStateGrid } from '../components/bmcu/SlotStateGrid';
import { StreamDiagnostics } from '../components/bmcu/StreamDiagnostics';
import { BMCUDeviceEvents } from '../components/BMCULinkSettings';
import { Button } from '../components/Button';
import { Card, CardContent } from '../components/Card';
import { useBMCULiveStream } from '../hooks/useBMCULiveStream';

const ranges = ['1h', '6h', '24h', '7d'] as const;
type Range = typeof ranges[number];
const rangeMs: Record<Range, number> = { '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000 };
const liveIntervalMs = 3000;
const updateTimeoutMs = 3 * liveIntervalMs;
const historyIntervalMs = 30000;
const emptyPoints: [] = [];
const HistoryTimeline = memo(LoaderTimeline);
const HistoryMetrics = memo(HardwareMetrics);
const DeviceEvents = memo(function DeviceEvents({ deviceId }: { deviceId: string }) {
  return <BMCUDeviceEvents device={{ device_id: deviceId }} />;
});

function rollingWindow(range: Range) {
  const now = Date.now();
  return {
    from: new Date(now - rangeMs[range]).toISOString(),
    to: new Date(now).toISOString(),
    resolution: range === '7d' ? '15m' : range === '24h' ? '5m' : '1m',
  };
}

export function BMCULinkPage() {
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<string | null>(null);
  const [range, setRange] = useState<Range>('6h');
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const monitors = useQuery({
    queryKey: ['bmcu-monitors'], queryFn: bmcuMonitorsApi.list, refetchInterval: historyIntervalMs,
  });
  const selectedMonitor = monitors.data?.find((monitor) => monitor.deviceId === selection) ?? monitors.data?.[0];
  const selectedId = selectedMonitor?.deviceId;
  const liveConnected = useBMCULiveStream(selectedId);
  const detail = useQuery({
    queryKey: ['bmcu-monitor', selectedId],
    queryFn: () => bmcuMonitorsApi.get(selectedId!),
    enabled: !!selectedId,
    refetchInterval: liveConnected ? false : liveIntervalMs,
  });
  const stream = useQuery({
    queryKey: ['bmcu-monitor-stream', selectedId],
    queryFn: () => bmcuMonitorsApi.streamDiagnostics(selectedId!),
    enabled: !!selectedId,
    refetchInterval: liveConnected ? false : liveIntervalMs,
  });
  const timeline = useQuery({
    queryKey: ['bmcu-monitor-timeline', selectedId, range],
    queryFn: () => bmcuMonitorsApi.timeline(selectedId!, rollingWindow(range)),
    enabled: !!selectedId,
    refetchInterval: historyIntervalMs,
  });
  const metrics = useQuery({
    queryKey: ['bmcu-monitor-metrics', selectedId, range],
    queryFn: () => bmcuMonitorsApi.metrics(selectedId!, rollingWindow(range)),
    enabled: !!selectedId,
    refetchInterval: historyIntervalMs,
  });

  const monitor = detail.data ?? selectedMonitor;
  const monitorQuery = detail.data ? detail : monitors;
  const listOutdated = monitors.isError || now - monitors.dataUpdatedAt > 3 * historyIntervalMs;
  const monitorOutdated = detail.isError || (
    now - monitorQuery.dataUpdatedAt > updateTimeoutMs && !(liveConnected && monitor?.health !== 'online')
  );
  const streamOutdated = stream.isError || (
    now - stream.dataUpdatedAt > updateTimeoutMs && !(liveConnected && stream.data && stream.data.health !== 'online')
  );
  const refreshing = monitors.isFetching || detail.isFetching || stream.isFetching || timeline.isFetching || metrics.isFetching;
  const liveError = monitors.error || detail.error;
  const historyError = timeline.error || metrics.error;

  function refresh() {
    void monitors.refetch();
    if (selectedId) {
      void detail.refetch();
      void stream.refetch();
      void timeline.refetch();
      void metrics.refetch();
      void queryClient.invalidateQueries({ queryKey: ['bmcu-link-events', selectedId] });
    }
  }

  return (
    <div className="min-h-screen bg-bambu-dark p-4 md:p-6">
      <div className="mx-auto max-w-[1600px]">
        <header className="mb-5 flex flex-wrap items-center justify-between gap-4 border-b border-bambu-dark-tertiary pb-4">
          <h1 className="flex items-center gap-3 text-2xl font-bold text-white">
            <RadioTower className="h-7 w-7 text-bambu-green" />
            BMCU Link
          </h1>
          <div className="flex flex-wrap items-center gap-3">
            <span className="flex items-center gap-1.5 text-xs text-bambu-gray">
              <Clock3 className="h-3.5 w-3.5" />
              {liveConnected ? 'Live updates' : `Reconnecting · checking every ${liveIntervalMs / 1000}s`}
            </span>
            <Button variant="secondary" size="sm" onClick={refresh}>
              <RefreshCw className={`h-4 w-4 ${refreshing ? 'motion-safe:animate-spin' : ''}`} />
              Refresh
            </Button>
          </div>
        </header>
        {liveError && (
          <div role="alert" className="mb-4 rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-sm text-amber-300">
            Monitor status could not be refreshed. {liveError.message}
          </div>
        )}
        {monitors.isLoading ? (
          <div className="flex min-h-64 items-center justify-center text-bambu-gray">
            <Activity className="mr-2 h-5 w-5 motion-safe:animate-pulse" />
            Loading monitor state…
          </div>
        ) : !monitor ? (
          !monitors.isError && (
            <div className="rounded-xl border border-dashed border-bambu-dark-tertiary p-12 text-center text-bambu-gray">
              <RadioTower className="mx-auto mb-3 h-10 w-10" />
              No BMCU Monitor has connected yet.
            </div>
          )
        ) : (
          <div className="grid items-start gap-5 xl:grid-cols-[17rem_minmax(0,1fr)]">
            <aside className="space-y-2" aria-label="Monitors">
              <h2 className="mb-3 text-sm font-semibold text-bambu-gray-light">Monitors</h2>
              {monitors.data?.map((item) => (
                <MonitorCard key={item.deviceId} monitor={item.deviceId === selectedId && detail.data ? detail.data : item} active={item.deviceId === selectedId} outdated={item.deviceId === selectedId ? monitorOutdated : listOutdated} onSelect={setSelection} />
              ))}
            </aside>
            <div className="min-w-0 space-y-5">
              <div className="grid gap-3 lg:grid-cols-2">
                <Card className="h-full border-l-2 border-l-bambu-green">
                  <CardContent className="space-y-4 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <h2 className="flex items-center gap-2 font-semibold text-white">
                        <Activity className="h-4 w-4 text-bambu-green" />
                        Monitor status
                      </h2>
                      <BMCUHealthBadge health={monitor.health} outdated={monitorOutdated} />
                    </div>
                    <div className="flex items-baseline gap-2">
                      <Cable className="h-4 w-4 self-center text-bambu-gray" />
                      <span className="font-mono text-3xl font-semibold tabular-nums text-white">{monitor.onlineLinks}/{monitor.linkCount}</span>
                      <span className="text-sm text-bambu-gray">{monitorOutdated ? 'links online at last check' : 'links online'}</span>
                    </div>
                    <dl className="grid gap-3 border-t border-bambu-dark-tertiary pt-3 text-xs sm:grid-cols-2">
                      <div>
                        <dt className="text-bambu-gray">Last device report</dt>
                        <dd className="mt-1 text-white" title={monitor.lastSeenAt ? formatBMCUDateTime(monitor.lastSeenAt) : undefined}>
                          {monitor.lastSeenAt ? formatBMCUAge((now - bmcuTimestamp(monitor.lastSeenAt)) / 1000) : 'Never seen'}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-bambu-gray">Last status check (JST)</dt>
                        <dd className="mt-1 text-white">{formatBMCUDateTime(monitorQuery.dataUpdatedAt)}</dd>
                      </div>
                    </dl>
                  </CardContent>
                </Card>
                <StreamDiagnostics data={stream.data} outdated={streamOutdated} error={stream.error} />
              </div>
              {detail.data ? (
                <SlotStateGrid deviceId={selectedId} liveConnected={liveConnected} links={detail.data.links} elapsedSeconds={Math.max(0, now - detail.dataUpdatedAt) / 1000} outdated={monitorOutdated} />
              ) : detail.isLoading && <p className="text-sm text-bambu-gray">Loading loader state…</p>}
              <section aria-label="History" className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-3 border-t border-bambu-dark-tertiary pt-4">
                  <h2 className="text-base font-semibold text-white">History</h2>
                  <div className="flex gap-0.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-1" aria-label="History range" role="group">
                    {ranges.map((item) => (
                      <button key={item} type="button" aria-pressed={range === item} onClick={() => setRange(item)}
                        className={`rounded-md px-3 py-1 text-xs font-medium ${range === item ? 'bg-bambu-green/20 text-bambu-green' : 'text-bambu-gray-light hover:bg-bambu-dark-tertiary'}`}>
                        {item}
                      </button>
                    ))}
                  </div>
                </div>
                {historyError && <p role="alert" className="text-sm text-amber-400">History could not be refreshed. {historyError.message}</p>}
                <HistoryTimeline points={timeline.data?.points ?? emptyPoints} />
                <HistoryMetrics points={metrics.data ?? emptyPoints} />
              </section>
              {selectedId && (
                <section className="rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-4">
                  <p className="mb-2 text-xs text-bambu-gray">Event history refreshes every 15s.</p>
                  <DeviceEvents key={selectedId} deviceId={selectedId} />
                </section>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
