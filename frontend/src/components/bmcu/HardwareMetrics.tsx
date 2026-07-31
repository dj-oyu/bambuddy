import { Cpu, MemoryStick, Radio, Thermometer } from 'lucide-react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { BMCUMetricPoint } from '../../api/bmcuMonitors';
import { Card, CardContent, CardHeader } from '../Card';

export function HardwareMetrics({ points }: { points: BMCUMetricPoint[] }) {
  const latest = points.at(-1);
  const data = points.map((p) => ({ ...p, time: new Date(p.at).getTime(), heapFreeKb: p.heapFreeBytes == null ? null : Math.round(p.heapFreeBytes / 1024) }));
  const summaries = [
    { label: 'Free heap', value: latest?.heapFreeBytes == null ? '—' : `${Math.round(latest.heapFreeBytes / 1024)} KB`, icon: MemoryStick },
    { label: 'Pico temp.', value: latest?.temperatureC == null ? '—' : `${latest.temperatureC.toFixed(1)} °C`, icon: Thermometer },
    { label: 'Loop p99', value: latest?.loopGapP99Us == null ? '—' : `${latest.loopGapP99Us} µs`, icon: Cpu },
    { label: 'Wi-Fi RSSI', value: latest?.wifiRssiDbm == null ? '—' : `${latest.wifiRssiDbm} dBm`, icon: Radio },
  ];
  return <Card><CardHeader><h2 className="font-semibold text-white">Pico hardware</h2><p className="text-xs text-bambu-gray">Window values are charted; cumulative counters are shown as totals or rates.</p></CardHeader><CardContent>
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{summaries.map(({ label, value, icon: Icon }) => <div key={label} className="rounded-lg bg-bambu-dark/50 p-3"><Icon className="mb-2 h-4 w-4 text-bambu-green" /><div className="text-xs text-bambu-gray">{label}</div><div className="mt-1 font-semibold text-white">{value}</div></div>)}</div>
    {data.length > 1 && <div className="mt-5 h-44" aria-label="Pico hardware metrics chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={data}><CartesianGrid strokeDasharray="3 3" stroke="#334155" /><XAxis dataKey="time" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} stroke="#94a3b8" /><YAxis stroke="#94a3b8" /><Tooltip labelFormatter={(v) => new Date(Number(v)).toLocaleString()} /><Line dataKey="heapFreeKb" name="Free heap (KB)" stroke="#22c55e" dot={false} /><Line dataKey="loopGapP99Us" name="Loop p99 (µs)" stroke="#f59e0b" dot={false} /><Line dataKey="transportSendMaxUs" name="Send max (µs)" stroke="#ef4444" dot={false} /></LineChart></ResponsiveContainer></div>}
    {latest && <div className="mt-4 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
      <div className="rounded bg-bambu-dark/40 p-2 text-bambu-gray">GC <span className="float-right text-white">{latest.gcCount ?? 0} total · max {latest.gcMaxUs ?? '—'} µs</span></div>
      <div className="rounded bg-bambu-dark/40 p-2 text-bambu-gray">UART overflow <span className="float-right text-white">{latest.uartOverflowTotal ?? 0} total</span></div>
      <div className="rounded bg-bambu-dark/40 p-2 text-bambu-gray">CRC errors <span className="float-right text-white">{latest.uartCrcErrorsTotal ?? 0} total</span></div>
      <div className="rounded bg-bambu-dark/40 p-2 text-bambu-gray">UART backlog <span className="float-right text-white">{latest.uartBacklog ?? '—'} B</span></div>
    </div>}
  </CardContent></Card>;
}
