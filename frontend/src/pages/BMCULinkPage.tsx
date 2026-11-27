import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, Clock3, RadioTower, RefreshCw } from 'lucide-react';
import { bmcuMonitorsApi } from '../api/bmcuMonitors';
import { api } from '../api/client';
import { MonitorCard } from '../components/bmcu/MonitorCard';
import { LoaderHistory } from '../components/bmcu/LoaderHistory';
import { GcodeWindow } from '../components/bmcu/GcodeWindow';
import { SlotStateGrid } from '../components/bmcu/SlotStateGrid';
import { Button } from '../components/Button';
import { useBMCULiveStream } from '../hooks/useBMCULiveStream';

const liveIntervalMs = 3000;
const updateTimeoutMs = 3 * liveIntervalMs;
const historyIntervalMs = 30000;
export function BMCULinkPage() {
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const monitors = useQuery({
    queryKey: ['bmcu-monitors'], queryFn: bmcuMonitorsApi.list, refetchInterval: historyIntervalMs,
  });
  const printers = useQuery({ queryKey: ['bmcu-preview-printers'], queryFn: api.getPrinters });
  // With multiple bridges or printers there is no explicit pairing, so leave
  // colors unknown instead of assigning another machine's filament.
  const solePrinter = monitors.data?.length === 1 && printers.data?.length === 1 ? printers.data[0].id : undefined;
  const filamentStatus = useQuery({
    queryKey: ['bmcu-filament-printer', solePrinter],
    queryFn: () => api.getPrinterStatus(solePrinter!),
    enabled: solePrinter != null,
    refetchInterval: 15000,
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
  const monitor = detail.data ?? selectedMonitor;
  const monitorQuery = detail.data ? detail : monitors;
  const listOutdated = monitors.isError || now - monitors.dataUpdatedAt > 3 * historyIntervalMs;
  const monitorOutdated = detail.isError || (
    now - monitorQuery.dataUpdatedAt > updateTimeoutMs && !(liveConnected && monitor?.health !== 'online')
  );
  const streamOutdated = stream.isError || (
    now - stream.dataUpdatedAt > updateTimeoutMs && !(liveConnected && stream.data && stream.data.health !== 'online')
  );
  const refreshing = monitors.isFetching || detail.isFetching || stream.isFetching;
  const liveError = monitors.error || detail.error;

  function refresh() {
    void monitors.refetch();
    if (selectedId) {
      void detail.refetch();
      void stream.refetch();
      void queryClient.invalidateQueries({ queryKey: ['bmcu-history', selectedId] });
      void queryClient.invalidateQueries({ queryKey: ['bmcu-history-observations', selectedId] });
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
          <div className="grid items-start gap-5 xl:grid-cols-[22rem_minmax(0,1fr)]">
            <aside className="space-y-2" aria-label="Monitors">
              <h2 className="mb-3 text-sm font-semibold text-bambu-gray-light">Monitors</h2>
              {monitors.data?.map((item) => (
                <MonitorCard key={item.deviceId} monitor={item.deviceId === selectedId && detail.data ? detail.data : item} active={item.deviceId === selectedId} outdated={item.deviceId === selectedId ? monitorOutdated : listOutdated} onSelect={setSelection} stream={stream.data} streamOutdated={streamOutdated} streamError={stream.error} checkedAt={monitorQuery.dataUpdatedAt} now={now} />
              ))}
            </aside>
            <div className="min-w-0 space-y-5">
              <div className="grid items-start gap-5 2xl:grid-cols-2">
                {detail.data ? (
                  <SlotStateGrid deviceId={selectedId} liveConnected={liveConnected} links={detail.data.links} elapsedSeconds={Math.max(0, now - detail.dataUpdatedAt) / 1000} outdated={monitorOutdated} filamentUnits={!filamentStatus.isError && filamentStatus.data?.connected ? filamentStatus.data.ams ?? undefined : undefined} />
                ) : detail.isLoading ? <p className="text-sm text-bambu-gray">Loading loader state…</p> : <div />}
                <GcodeWindow now={now} />
              </div>
              {selectedId && <LoaderHistory key={selectedId} deviceId={selectedId} links={detail.data?.links ?? []} />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
