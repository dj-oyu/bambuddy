import { AlertCircle, AlertTriangle, Info } from 'lucide-react';
import { CartesianGrid, Cell, ComposedChart, Line, ReferenceArea, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from 'recharts';
import type { BMCUTimelinePoint } from '../../api/bmcuMonitors';
import { Card, CardContent, CardHeader } from '../Card';
import { bmcuTimestamp, formatBMCUDateTime, formatBMCUTime } from './bmcuTime';

const severityColor = { info: '#60a5fa', warning: '#f59e0b', error: '#ef4444', critical: '#dc2626' };

export function LoaderTimeline({ points }: { points: BMCUTimelinePoint[] }) {
  const continuousKinds = new Set(['pull_pct', 'pressure', 'motion', 'current_slot']);
  const data = points.map((point) => ({ ...point, time: bmcuTimestamp(point.at), eventY: point.anomaly || !continuousKinds.has(point.kind) ? (point.pullPercent ?? 50) : null }));
  return <Card>
    <CardHeader className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="font-semibold text-white">Loader timeline</h2><p className="text-xs text-bambu-gray">Recent events and anomalies share the same time axis.</p></div>
      <div className="flex gap-3 text-xs text-bambu-gray-light"><span className="flex items-center gap-1"><Info className="h-3 w-3 text-blue-400" />BMCU</span><span className="flex items-center gap-1"><AlertTriangle className="h-3 w-3 text-amber-400" />warning</span><span className="flex items-center gap-1"><AlertCircle className="h-3 w-3 text-red-400" />abnormal</span></div>
    </CardHeader>
    <CardContent>
      {data.length === 0 ? <div className="py-14 text-center text-bambu-gray">No timeline data in this range.</div> : <div className="h-72" aria-label="BMCU loader event and anomaly graph">
        <ResponsiveContainer width="100%" height="100%"><ComposedChart data={data} margin={{ top: 10, right: 12, bottom: 4, left: -12 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="time" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => formatBMCUTime(Number(v))} stroke="#94a3b8" />
          <YAxis domain={[0, 100]} stroke="#94a3b8" unit="%" />
          <Tooltip labelFormatter={(v) => formatBMCUDateTime(Number(v))} />
          {points.filter((p) => p.missingData).map((p) => { const at = bmcuTimestamp(p.at); return <ReferenceArea key={p.id} x1={at - 15000} x2={at + 15000} fill="#64748b" fillOpacity={0.18} />; })}
          <Line type="monotone" dataKey="pullPercent" name="Pull" stroke="#22c55e" dot={false} connectNulls={false} strokeWidth={2} />
          <Line type="monotone" dataKey="pressure" name="Pressure" stroke="#38bdf8" dot={false} connectNulls={false} />
          <Scatter dataKey="eventY" name="Event / anomaly">{data.map((point) => <Cell key={point.id} fill={severityColor[point.severity]} />)}</Scatter>
        </ComposedChart></ResponsiveContainer>
      </div>}
      <div className="mt-4 max-h-52 space-y-1 overflow-auto">{[...points].reverse().filter((p) => p.kind !== 'status' || p.anomaly).slice(0, 30).map((p) =>
        <div key={p.id} className="grid grid-cols-[5rem_5rem_1fr] items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-bambu-dark-tertiary/60"><span className="text-bambu-gray">{formatBMCUTime(p.at)}</span><span className="rounded px-1.5 py-0.5 text-center" style={{ color: severityColor[p.severity], backgroundColor: `${severityColor[p.severity]}18` }}>{p.source}</span><span className="text-bambu-gray-light">{p.label}</span></div>)}</div>
    </CardContent>
  </Card>;
}
