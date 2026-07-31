import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Activity, RefreshCw, RadioTower } from 'lucide-react';
import { bmcuMonitorsApi } from '../api/bmcuMonitors';
import { HardwareMetrics } from '../components/bmcu/HardwareMetrics';
import { LoaderTimeline } from '../components/bmcu/LoaderTimeline';
import { MonitorCard } from '../components/bmcu/MonitorCard';
import { SlotStateGrid } from '../components/bmcu/SlotStateGrid';
import { Button } from '../components/Button';

type Range = '1h' | '6h' | '24h' | '7d';
const rangeMs: Record<Range, number> = { '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000 };

export function BMCULinkPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [range, setRange] = useState<Range>('6h');
  const monitors = useQuery({ queryKey: ['bmcu-monitors'], queryFn: bmcuMonitorsApi.list, refetchInterval: 10000 });
  useEffect(() => {
    if (!selectedId && monitors.data?.length) setSelectedId(monitors.data[0].deviceId);
    if (selectedId && monitors.data && !monitors.data.some((m) => m.deviceId === selectedId)) setSelectedId(monitors.data[0]?.deviceId ?? null);
  }, [monitors.data, selectedId]);
  const queryRange = useMemo(() => { const to = new Date(); return { from: new Date(to.getTime() - rangeMs[range]).toISOString(), to: to.toISOString(), resolution: range === '7d' ? '15m' : range === '24h' ? '5m' : '1m' }; }, [range]);
  const detail = useQuery({ queryKey: ['bmcu-monitor', selectedId], queryFn: () => bmcuMonitorsApi.get(selectedId!), enabled: !!selectedId, refetchInterval: 10000 });
  const timeline = useQuery({ queryKey: ['bmcu-monitor-timeline', selectedId, range], queryFn: () => bmcuMonitorsApi.timeline(selectedId!, queryRange), enabled: !!selectedId, refetchInterval: 10000 });
  const metrics = useQuery({ queryKey: ['bmcu-monitor-metrics', selectedId, range], queryFn: () => bmcuMonitorsApi.metrics(selectedId!, queryRange), enabled: !!selectedId, refetchInterval: 15000 });
  const failed = monitors.error || detail.error || timeline.error || metrics.error;
  return <div className="min-h-screen bg-bambu-dark p-4 md:p-6"><div className="mx-auto max-w-[1600px]">
    <header className="mb-6 flex flex-wrap items-center justify-between gap-4"><div><h1 className="flex items-center gap-3 text-2xl font-bold text-white"><RadioTower className="h-7 w-7 text-bambu-green" />BMCU Link</h1><p className="mt-1 text-sm text-bambu-gray">Loader state, transport health and Pico hardware at a glance.</p></div><div className="flex items-center gap-2">{(['1h', '6h', '24h', '7d'] as Range[]).map((item) => <button key={item} onClick={() => setRange(item)} className={`rounded-md px-3 py-1.5 text-sm ${range === item ? 'bg-bambu-green text-white' : 'bg-bambu-dark-secondary text-bambu-gray-light'}`}>{item}</button>)}<Button variant="secondary" size="sm" onClick={() => void Promise.all([monitors.refetch(), detail.refetch(), timeline.refetch(), metrics.refetch()])}><RefreshCw className="h-4 w-4" />Refresh</Button></div></header>
    {failed && <div role="alert" className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">BMCU Monitor data could not be loaded. {String((failed as Error).message ?? failed)}</div>}
    {monitors.isLoading ? <div className="flex min-h-64 items-center justify-center text-bambu-gray"><Activity className="mr-2 h-5 w-5 animate-pulse" />Loading monitor state…</div> : !monitors.data?.length ? <div className="rounded-xl border border-dashed border-bambu-dark-tertiary p-16 text-center text-bambu-gray"><RadioTower className="mx-auto mb-3 h-10 w-10" />No BMCU Monitor has connected yet.<br /><span className="text-xs">Provisioning and collected device logs remain in Settings.</span></div> : <div className="grid gap-5 xl:grid-cols-[19rem_minmax(0,1fr)]"><aside className="space-y-3">{monitors.data.map((m) => <MonitorCard key={m.deviceId} monitor={m} active={m.deviceId === selectedId} onSelect={() => setSelectedId(m.deviceId)} />)}</aside><main className="min-w-0 space-y-5">{detail.data && <SlotStateGrid links={detail.data.links} />}<LoaderTimeline points={timeline.data?.points ?? []} /><HardwareMetrics points={metrics.data ?? []} /></main></div>}
  </div></div>;
}
