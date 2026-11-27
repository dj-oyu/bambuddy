import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { bmcuLinkApi, type BMCULinkEvent } from '../../api/client';
import { bmcuMonitorsApi, type BMCULinkSnapshot, type BMCUTimelinePoint } from '../../api/bmcuMonitors';
import { Card, CardContent, CardHeader } from '../Card';
import { bmcuTimestamp, formatBMCUDateTime, formatBMCUTime } from './bmcuTime';

const ranges = { '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000 } as const;
type Range = keyof typeof ranges;
const pageSize = 100;
const fieldNames: Record<number, string> = { 1: 'slot', 2: 'filament sensors', 3: 'online links', 4: 'motion', 5: 'pressure', 6: 'LED', 7: 'control error', 8: 'motion fault' };
const motionNames: Record<number, string> = { 0: 'idle', 1: 'send out', 2: 'in use', 3: 'preparing to retract', 4: 'retract', 5: 'preparing to use', 6: 'stop' };
const severityNames = ['debug', 'info', 'notice', 'warning', 'error', 'critical'];

function payload(event: BMCULinkEvent): Record<string, unknown> {
  try {
    const value: unknown = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch { return {}; }
}

function describe(event: BMCULinkEvent) {
  const data = payload(event);
  const severity = typeof data.severity === 'number' ? data.severity : 1;
  const name = event.kind === 'pico_log' ? String(data.message || data.component || 'Pico log') : String(data.event_name || event.kind || 'event');
  const field = typeof data.field === 'number' ? fieldNames[data.field] || `field ${data.field}` : null;
  const slot = typeof data.slot === 'number' && data.slot >= 0 && data.slot < 4 ? ` · slot ${data.slot + 1}` : '';
  const value = (v: unknown) => typeof v === 'number' && data.field === 4 ? motionNames[v] || String(v) : String(v ?? '—');
  const title = name === 'state_change' && field ? `${field}${slot}: ${value(data.previous_value)} → ${value(data.value)}` : name.replaceAll('_', ' ');
  return { data, severity, name, title };
}

type Group = { key: string; lane: string; at: string; title: string; severity: number; events: BMCULinkEvent[]; samples: BMCUTimelinePoint[] };
function groupEvents(events: BMCULinkEvent[]): Group[] {
  const groups = new Map<string, Group>();
  let run = 0;
  for (const event of events) {
    const item = describe(event);
    const lane = event.link_id || 'Bridge';
    // Repetitive bus traffic is grouped per minute and lane, with every raw
    // record retained in the expandable details. State changes stay distinct.
    const repetitive = item.name === 'printer_transaction' || item.name === 'printer_long_transaction';
    if (!repetitive) run++;
    const key = repetitive ? `${run}:${lane}:${item.name}:${item.severity}:${Math.floor(bmcuTimestamp(event.server_received_at) / 60000)}` : `${event.kind}:${event.id}`;
    const existing = groups.get(key);
    if (existing) existing.events.push(event);
    else groups.set(key, { key, lane, at: event.server_received_at, title: item.title, severity: item.severity, events: [event], samples: [] });
  }
  return [...groups.values()];
}

export function LoaderHistory({ deviceId, links }: { deviceId: string; links: BMCULinkSnapshot[] }) {
  const [range, setRange] = useState<Range>('6h');
  const [page, setPage] = useState(0);
  const [anchor, setAnchor] = useState(Date.now);
  const [filter, setFilter] = useState<'all' | 'warning' | 'change'>('all');
  useEffect(() => {
    if (page !== 0) return;
    const timer = window.setInterval(() => setAnchor(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, [page]);
  const result = useQuery({
    queryKey: ['bmcu-history', deviceId, range, page, anchor],
    queryFn: () => bmcuLinkApi.getEvents(deviceId, {
      fromTime: new Date(anchor - ranges[range]).toISOString(), toTime: new Date(anchor).toISOString(),
      limit: pageSize + 1, offset: page * pageSize,
    }),
    retry: false,
  });
  const observationAnchor = Math.ceil(anchor / 30000) * 30000;
  const observations = useQuery({
    queryKey: ['bmcu-history-observations', deviceId, range, observationAnchor],
    queryFn: () => bmcuMonitorsApi.timeline(deviceId, {
      from: new Date(observationAnchor - ranges[range]).toISOString(), to: new Date(observationAnchor).toISOString(),
    }),
    staleTime: 30000, retry: false,
  });
  const events = useMemo(() => (result.data ?? []).slice(0, pageSize), [result.data]);
  const groups = useMemo(() => {
    const combined = groupEvents(events);
    const first = page === 0 || events.length === 0 ? anchor : bmcuTimestamp(events[0].server_received_at);
    const last = events.length === pageSize ? bmcuTimestamp(events[events.length - 1].server_received_at) : anchor - ranges[range];
    const samples = new Map<string, Group>();
    for (const point of observations.data?.points ?? []) {
      if (point.kind !== 'control_error' && point.kind !== 'bmcu_boot') continue;
      const at = bmcuTimestamp(point.at);
      if (at < last || at > first) continue;
      const lane = links.find((link) => link.linkIndex === point.linkIndex)?.linkId ?? 'Bridge';
      const key = `sample:${lane}:${point.kind}:${Math.floor(at / 60000)}`;
      const existing = samples.get(key);
      if (existing) existing.samples.push(point);
      else samples.set(key, { key, lane, at: point.at, title: point.label, severity: point.severity === 'critical' ? 5 : point.severity === 'error' ? 4 : point.severity === 'warning' ? 3 : 1, events: [], samples: [point] });
    }
    return [...combined, ...samples.values()].sort((a, b) => bmcuTimestamp(b.at) - bmcuTimestamp(a.at));
  }, [events, observations.data, links, page, anchor, range]);
  const visible = groups.filter((item) => filter === 'all' || (filter === 'warning' ? item.severity >= 3 : item.title.includes(' → ')));
  const lanes = useMemo(() => [...new Set([...links.map((item) => item.linkId), ...groups.map((item) => item.lane)].filter((name) => name && name !== 'Bridge' && name !== 'monitor')), 'Bridge'], [links, groups]);
  const columns = `repeat(${lanes.length}, minmax(22px, 1fr))`;
  const selectRange = (next: Range) => { setRange(next); setPage(0); setAnchor(Date.now()); };

  return <Card>
    <CardHeader className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-white">History</h2><p className="mt-1 text-xs text-bambu-gray">BMCU events and bridge logs · newest first</p></div>
        <div className="flex gap-0.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-1" aria-label="History range" role="group">{(Object.keys(ranges) as Range[]).map((item) => <button key={item} type="button" aria-pressed={range === item} onClick={() => selectRange(item)} className={`rounded-md px-3 py-1 text-xs ${range === item ? 'bg-bambu-green/20 text-bambu-green' : 'text-bambu-gray-light hover:bg-bambu-dark-tertiary'}`}>{item}</button>)}</div>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs" role="group" aria-label="Filter this page"><span className="text-bambu-gray">Filter page</span>{(['all', 'warning', 'change'] as const).map((item) => <button key={item} type="button" aria-pressed={filter === item} onClick={() => setFilter(item)} className={`rounded-full border px-2.5 py-1 ${filter === item ? 'border-bambu-green/60 text-bambu-green' : 'border-bambu-dark-tertiary text-bambu-gray-light'}`}>{item === 'all' ? 'All events' : item === 'warning' ? 'Warnings' : 'State changes'}</button>)}</div>
    </CardHeader>
    <CardContent>
      {result.isLoading && <p role="status" className="py-6 text-sm text-bambu-gray">Loading history…</p>}
      {result.isError && <p role="alert" className="py-4 text-sm text-amber-400">History could not be loaded: {result.error.message}</p>}
      {observations.isError && <p role="status" className="py-2 text-xs text-amber-400">Status observations unavailable.</p>}
      {!result.isLoading && !result.isError && !observations.isLoading && visible.length === 0 && <p className="py-8 text-center text-sm text-bambu-gray">{groups.length ? 'No events match this filter on this page.' : 'No events in this time range.'}</p>}
      {visible.length > 0 && <div className="overflow-x-auto"><div className="min-w-[480px]">
        <div className="grid grid-cols-[5rem_12rem_minmax(0,1fr)] gap-2 border-b border-bambu-dark-tertiary pb-2 text-[10px] uppercase tracking-wide text-bambu-gray"><span>Time · JST</span><div className="grid text-center" style={{ gridTemplateColumns: columns }}>{lanes.map((lane) => <span key={lane} className="truncate" title={lane}>{lane === 'Bridge' ? 'Pico' : lane}</span>)}</div><span>Event</span></div>
        {visible.map((item) => <div key={item.key} className="grid grid-cols-[5rem_12rem_minmax(0,1fr)] gap-2 text-xs">
          <span className="pt-3 tabular-nums text-bambu-gray" title={formatBMCUDateTime(item.at)}>{formatBMCUTime(item.at)}</span>
          <div className="grid self-stretch" style={{ gridTemplateColumns: columns }} aria-label={item.lane === 'monitor' ? 'Bridge' : item.lane}>{lanes.map((lane) => <div key={lane} className="relative flex justify-center"><span className="absolute inset-y-0 w-px bg-bambu-dark-tertiary" />{(item.lane === lane || (lane === 'Bridge' && (item.lane === 'monitor' || !lanes.includes(item.lane)))) && <span className={`relative mt-3 h-2.5 w-2.5 rounded-full border-2 ${item.severity >= 4 ? 'border-red-400 bg-red-400/30' : item.severity === 3 ? 'border-amber-400 bg-amber-400/30' : 'border-bambu-green bg-bambu-green/30'}`} />}</div>)}</div>
          <details className="border-b border-bambu-dark-tertiary/60 py-2"><summary className="cursor-pointer rounded py-1 text-bambu-gray-light hover:text-white"><span className="text-white">{item.title}</span>{item.events.length + item.samples.length > 1 && <span className="ml-2 rounded-full bg-bambu-dark-tertiary px-2 py-0.5 text-bambu-gray-light">×{item.events.length + item.samples.length}</span>}<span className="ml-2 text-bambu-gray">{severityNames[item.severity] || 'unknown'}{item.samples.length > 0 ? ' · status observation' : ''}</span></summary><div className="max-h-48 space-y-2 overflow-auto py-2">{item.events.map((event) => <div key={`${event.kind}:${event.id}`}><span className="text-bambu-gray">{formatBMCUDateTime(event.server_received_at)} · {event.link_id || 'Bridge'} · {event.kind}</span><pre className="mt-1 whitespace-pre-wrap break-all text-[11px] text-bambu-gray-light">{JSON.stringify(payload(event), null, 2)}</pre></div>)}{item.samples.map((point) => <div key={point.id} className="text-bambu-gray-light">{formatBMCUDateTime(point.at)} · {point.label}</div>)}</div></details>
        </div>)}
      </div></div>}
      <div className="mt-3 flex items-center justify-between gap-2 text-xs text-bambu-gray"><span>100 events per page · {page === 0 ? 'updates every 15s' : 'updates paused'}{observations.data?.downsampled ? ' · status observations sampled' : ''}</span><div className="flex items-center gap-3"><button type="button" className="disabled:opacity-40" disabled={page === 0} onClick={() => setPage((value) => value - 1)}>Newer</button><span>{page + 1}</span><button type="button" className="disabled:opacity-40" disabled={!result.data || result.data.length <= pageSize} onClick={() => setPage((value) => value + 1)}>Older</button></div></div>
    </CardContent>
  </Card>;
}
