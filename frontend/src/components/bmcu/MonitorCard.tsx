import { memo } from 'react';
import { Activity, AlertTriangle, Cable, ChevronRight, Clock3, Database, Radio } from 'lucide-react';
import type { BMCUMonitorSummary, BMCUStreamDiagnostics, MonitorHealth } from '../../api/bmcuMonitors';
import { Card, CardContent } from '../Card';
import { bmcuTimestamp, formatBMCUAge, formatBMCUDateTime } from './bmcuTime';

const counters = [
  ['datagrams_received', 'Datagrams received'], ['records_persisted', 'Records stored'],
  ['gaps_detected', 'Missing sequences detected'], ['ingest_overflow', 'Queue overflow drops'],
] as const;

const healthStyle: Record<MonitorHealth, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
};

export function BMCUHealthBadge({ health, outdated = false }: { health: MonitorHealth; outdated?: boolean }) {
  const state = outdated ? 'stale' : health;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${healthStyle[state]}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
      {outdated ? 'Update delayed' : { online: 'Online', stale: 'Stale', offline: 'Offline' }[state]}
    </span>
  );
}

export const MonitorCard = memo(function MonitorCard({
  monitor, active, outdated, onSelect, stream, streamOutdated, streamError, checkedAt, now,
}: {
  monitor: BMCUMonitorSummary;
  active: boolean;
  outdated: boolean;
  onSelect: (deviceId: string) => void;
  stream?: BMCUStreamDiagnostics | null;
  streamOutdated: boolean;
  streamError: Error | null;
  checkedAt: number;
  now: number;
}) {
  return <Card className={`transition-colors ${active ? 'border-bambu-green/60 bg-bambu-green/5' : 'hover:border-bambu-gray-dark'}`}>
    <button type="button" aria-pressed={active} onClick={() => onSelect(monitor.deviceId)} className="w-full rounded-xl text-left">
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0"><div className="flex items-center gap-2"><Radio className="h-4 w-4 text-bambu-green" /><h2 className="truncate font-semibold text-white">{monitor.displayName || monitor.deviceId}</h2></div><p className="mt-1 truncate text-xs text-bambu-gray">{monitor.deviceId} · {monitor.firmware}</p></div>
          {active && <ChevronRight className="h-4 w-4 shrink-0 text-bambu-green" aria-hidden="true" />}
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-bambu-gray-light">
          <BMCUHealthBadge health={monitor.health} outdated={outdated} />
          <span className="flex items-center gap-1.5"><Cable className="h-3.5 w-3.5" />{monitor.onlineLinks}/{monitor.linkCount} links</span>
        </div>
        <div className="mt-3 space-y-1.5 text-xs text-bambu-gray-light">
          {monitor.ackSequence != null && <span className="flex items-center gap-1.5"><Activity className="h-3.5 w-3.5" />ACK {monitor.ackSequence}</span>}
          {monitor.replayPending != null && <span className="flex items-center gap-1.5"><Database className="h-3.5 w-3.5" />{monitor.replayPending} replay</span>}
          {monitor.anomalyCount != null && <span className="flex items-center gap-1.5"><AlertTriangle className="h-3.5 w-3.5" />{monitor.anomalyCount} anomalies</span>}
        </div>
        <div className="mt-3 flex items-center gap-1.5 text-[11px] text-bambu-gray" title={monitor.lastSeenAt ? formatBMCUDateTime(monitor.lastSeenAt) : undefined}><Clock3 className="h-3 w-3" />Last report {monitor.lastSeenAt ? formatBMCUAge((now - bmcuTimestamp(monitor.lastSeenAt)) / 1000) : 'never'}</div>
      </CardContent>
    </button>
    {active && <div className="space-y-3 px-4 pb-4 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2 text-bambu-gray-light">
        <span>UDP {streamError ? 'unavailable' : stream === undefined ? 'loading' : stream === null ? 'disabled' : streamOutdated ? 'update delayed' : stream.health}</span>
        {stream?.sourceAddress && <span className="font-mono">{stream.sourceAddress}</span>}
      </div>
      {streamError && <p role="status" className="text-amber-400">UDP diagnostics could not be refreshed.</p>}
      {stream && (stream.counters.gaps_detected != null || stream.counters.ingest_overflow != null) && <p className="text-bambu-gray">Since startup · Missing {stream.counters.gaps_detected?.toLocaleString() ?? '—'} · Dropped {stream.counters.ingest_overflow?.toLocaleString() ?? '—'}</p>}
      {stream && <details className="group border-t border-bambu-dark-tertiary pt-3">
        <summary className="cursor-pointer text-bambu-gray-light hover:text-white">Connection details</summary>
        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2">
          <div><dt className="text-bambu-gray">Queue depth</dt><dd className="mt-0.5 font-mono text-white">{stream.queueDepth.toLocaleString()}</dd></div>
          <div><dt className="text-bambu-gray">Status checked (JST)</dt><dd className="mt-0.5 text-white">{checkedAt ? formatBMCUDateTime(checkedAt) : '—'}</dd></div>
          {counters.map(([key, label]) => <div key={key}><dt className="text-bambu-gray">{label}</dt><dd className="mt-0.5 font-mono text-white">{stream.counters[key]?.toLocaleString() ?? '—'}</dd></div>)}
        </dl>
        <p className="mt-2 text-bambu-gray">Counters since receiver startup</p>
      </details>}
    </div>}
  </Card>;
});
