import { AlertTriangle, Cable, CircleGauge, Clock } from 'lucide-react';
import type { BMCULinkSnapshot } from '../../api/bmcuMonitors';
import { Card, CardContent, CardHeader } from '../Card';

/** "3 分前" style age for the loader snapshot. Anything above a day is reported
 * in days: the point is that the value is not current, not the exact age. */
function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

export function SlotStateGrid({ links }: { links: BMCULinkSnapshot[] }) {
  const orderedLinks = [...links].sort((a, b) => a.linkId.localeCompare(b.linkId, undefined, { numeric: true, sensitivity: 'base' }));
  return <Card><CardHeader><h2 className="font-semibold text-white">Loader state</h2></CardHeader><CardContent className="grid gap-3 sm:grid-cols-2">
    {orderedLinks.map((link) => {
      // A stale snapshot must not be painted as the live slot. The bridge keeps
      // sending events and diagnostics while STATUS is starved, so the link can
      // look connected for hours with a slot view from the previous day.
      const live = link.state === 'online';
      // `no_data` is not a stale reading, it is the absence of one. Every field
      // below is null in that case and must render as unknown, not as zero.
      const known = link.state !== 'no_data';
      const age = link.statusAgeS == null ? null : formatAge(link.statusAgeS);
      return <div key={link.linkIndex} className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/40 p-4">
        <div className="flex items-center justify-between"><span className="flex items-center gap-2 font-medium text-white"><Cable className="h-4 w-4" />{link.linkId}</span><span className={link.state === 'online' ? 'text-emerald-400' : link.state === 'stale' ? 'text-amber-400' : 'text-bambu-gray'}>{link.state === 'no_data' ? 'no data' : link.state}</span></div>
        <div className={`mt-4 grid grid-cols-4 gap-2 ${live ? '' : 'opacity-50'}`}>{[0, 1, 2, 3].map((slot) => <div key={slot} className={`rounded-md border px-2 py-3 text-center ${live && link.currentSlot === slot ? 'border-bambu-green bg-bambu-green/15 text-white' : link.currentSlot === slot ? 'border-amber-500/40 text-bambu-gray-light' : 'border-bambu-dark-tertiary text-bambu-gray'}`}><div className="text-[10px] uppercase">Slot</div><div className="font-semibold">{slot + 1}</div></div>)}</div>
        <div className={`mt-4 grid grid-cols-3 gap-2 text-xs ${live ? '' : 'opacity-50'}`}><div><span className="block text-bambu-gray">Motion</span><span className="text-white">{link.motion == null ? '—' : link.motion.join(', ')}</span></div><div><span className="block text-bambu-gray">Pull</span><span className="text-white">{link.pullPercent == null ? '—' : `${link.pullPercent}%`}</span></div><div><span className="block text-bambu-gray">Pressure</span><span className="text-white">{link.pressure ?? '—'}</span></div></div>
        {!live && <div className="mt-3 flex items-center gap-1.5 rounded bg-amber-500/10 px-2 py-1.5 text-xs text-amber-300"><Clock className="h-3.5 w-3.5 flex-shrink-0" />{!known ? 'No loader state has ever been received for this link.' : age == null ? 'No loader state has been received for this link.' : `Loader state is ${age} old — the bridge has sent no STATUS since.`}</div>}
        <div className="mt-3 flex items-center justify-between text-xs text-bambu-gray"><span className="flex items-center gap-1"><CircleGauge className="h-3.5 w-3.5" />{link.activeMask == null ? 'filament —' : `filament 0x${link.activeMask.toString(16).padStart(2, '0')}`}</span><span className={link.faultCount ? 'flex items-center gap-1 text-red-400' : 'flex items-center gap-1'}><AlertTriangle className="h-3.5 w-3.5" />{link.faultCount == null ? 'faults —' : `${link.faultCount} faults`}</span></div>
      </div>;
    })}
  </CardContent></Card>;
}
