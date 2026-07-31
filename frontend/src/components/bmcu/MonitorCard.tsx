import { Activity, AlertTriangle, Cable, Clock3, Database, Radio } from 'lucide-react';
import type { BMCUMonitorSummary, MonitorHealth } from '../../api/bmcuMonitors';
import { Card, CardContent } from '../Card';
import { formatBMCUDateTime } from './bmcuTime';

const healthStyle: Record<MonitorHealth, string> = {
  online: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  stale: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  offline: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  incompatible: 'bg-red-500/15 text-red-400 border-red-500/30',
  unknown: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
};

export function MonitorCard({ monitor, active, onSelect }: { monitor: BMCUMonitorSummary; active: boolean; onSelect: () => void }) {
  return <button type="button" onClick={onSelect} className="w-full text-left">
    <Card className={`transition-colors ${active ? 'border-bambu-green ring-1 ring-bambu-green/40' : 'hover:border-bambu-gray-dark'}`}>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0"><div className="flex items-center gap-2"><Radio className="h-4 w-4 text-bambu-green" /><h2 className="truncate font-semibold text-white">{monitor.displayName || monitor.deviceId}</h2></div><p className="mt-1 truncate text-xs text-bambu-gray">{monitor.deviceId} · {monitor.firmware}</p></div>
          <span className={`rounded-full border px-2 py-0.5 text-xs ${healthStyle[monitor.health]}`}>{monitor.health}</span>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2 text-xs text-bambu-gray-light">
          <span className="flex items-center gap-1.5"><Cable className="h-3.5 w-3.5" />{monitor.onlineLinks}/{monitor.linkCount} links</span>
          <span className="flex items-center gap-1.5"><Activity className="h-3.5 w-3.5" />ACK {monitor.ackSequence}</span>
          <span className="flex items-center gap-1.5"><Database className="h-3.5 w-3.5" />{monitor.replayPending} replay</span>
          <span className="flex items-center gap-1.5"><AlertTriangle className="h-3.5 w-3.5" />{monitor.anomalyCount} anomalies</span>
        </div>
        <div className="mt-3 flex items-center gap-1.5 text-[11px] text-bambu-gray"><Clock3 className="h-3 w-3" />{monitor.lastSeenAt ? formatBMCUDateTime(monitor.lastSeenAt) : 'Never seen'}</div>
      </CardContent>
    </Card>
  </button>;
}
