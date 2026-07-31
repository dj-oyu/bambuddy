import { AlertTriangle, Cable, CircleGauge } from 'lucide-react';
import type { BMCULinkSnapshot } from '../../api/bmcuMonitors';
import { Card, CardContent, CardHeader } from '../Card';

export function SlotStateGrid({ links }: { links: BMCULinkSnapshot[] }) {
  const orderedLinks = [...links].sort((a, b) => a.linkId.localeCompare(b.linkId, undefined, { numeric: true, sensitivity: 'base' }));
  return <Card><CardHeader><h2 className="font-semibold text-white">Loader state</h2></CardHeader><CardContent className="grid gap-3 sm:grid-cols-2">
    {orderedLinks.map((link) => <div key={link.linkIndex} className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/40 p-4">
      <div className="flex items-center justify-between"><span className="flex items-center gap-2 font-medium text-white"><Cable className="h-4 w-4" />{link.linkId}</span><span className={link.state === 'online' ? 'text-emerald-400' : link.state === 'stale' ? 'text-amber-400' : 'text-bambu-gray'}>{link.state}</span></div>
      <div className="mt-4 grid grid-cols-4 gap-2">{[0, 1, 2, 3].map((slot) => <div key={slot} className={`rounded-md border px-2 py-3 text-center ${link.currentSlot === slot ? 'border-bambu-green bg-bambu-green/15 text-white' : 'border-bambu-dark-tertiary text-bambu-gray'}`}><div className="text-[10px] uppercase">Slot</div><div className="font-semibold">{slot + 1}</div></div>)}</div>
      <div className="mt-4 grid grid-cols-3 gap-2 text-xs"><div><span className="block text-bambu-gray">Motion</span><span className="text-white">{link.motion ?? '—'}</span></div><div><span className="block text-bambu-gray">Pull</span><span className="text-white">{link.pullPercent == null ? '—' : `${link.pullPercent}%`}</span></div><div><span className="block text-bambu-gray">Pressure</span><span className="text-white">{link.pressure ?? '—'}</span></div></div>
      <div className="mt-3 flex items-center justify-between text-xs text-bambu-gray"><span className="flex items-center gap-1"><CircleGauge className="h-3.5 w-3.5" />mask 0x{link.activeMask.toString(16).padStart(2, '0')}</span><span className={link.faultCount ? 'flex items-center gap-1 text-red-400' : 'flex items-center gap-1'}><AlertTriangle className="h-3.5 w-3.5" />{link.faultCount} faults</span></div>
    </div>)}
  </CardContent></Card>;
}
