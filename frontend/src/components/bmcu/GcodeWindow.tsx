import { lazy, Suspense, useCallback, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { Card, CardContent, CardHeader } from '../Card';

const VirtualPrintCamera = lazy(() => import('./VirtualPrintCamera').then(({ VirtualPrintCamera }) => ({ default: VirtualPrintCamera })));

export function GcodeWindow({ now }: { now: number }) {
  const [printer, setPrinter] = useState('');
  const printers = useQuery({ queryKey: ['bmcu-preview-printers'], queryFn: api.getPrinters });
  const context = useQuery({
    queryKey: ['bmcu-print-context', printer],
    queryFn: ({ signal }) => api.getBMCUPrintContext(Number(printer), signal),
    enabled: printer !== '', retry: false, refetchInterval: 5000,
  });
  const status = useQuery({
    queryKey: ['bmcu-preview-printer-status', printer],
    queryFn: () => api.getPrinterStatus(Number(printer)),
    enabled: printer !== '', refetchInterval: 15000,
  });
  const plateFilaments = useQuery({
    queryKey: ['bmcu-preview-filaments', context.data?.archiveId, context.data?.plate],
    queryFn: () => api.getArchiveFilamentRequirements(context.data!.archiveId, context.data!.plate),
    enabled: !!context.data, staleTime: 300_000, retry: false,
  });
  const colors = useMemo(() => {
    const result = new Map<number, string>();
    for (const filament of plateFilaments.data?.filaments ?? []) {
      if (/^#[\da-f]{6}([\da-f]{2})?$/i.test(filament.color)) result.set(filament.slot_id - 1, filament.color.slice(0, 7));
    }
    const snapshot = status.data;
    const mapping = snapshot?.connected && snapshot.current_archive_id === context.data?.archiveId ? snapshot.ams_mapping : [];
    for (const [tool, mapped] of (mapping ?? []).entries()) {
      if (!Number.isInteger(mapped) || mapped < 0 || mapped >= 128) continue;
      const unit = snapshot?.ams?.find((item) => item.id === Math.floor(mapped / 4));
      const color = unit?.tray.find((tray) => tray.id === mapped % 4)?.tray_color;
      if (color && /^[\da-f]{6}([\da-f]{2})?$/i.test(color)) result.set(tool, `#${color.slice(0, 6)}`);
    }
    return result;
  }, [plateFilaments.data, status.data, context.data?.archiveId]);
  const toolColor = useCallback((tool: number) => colors.get(tool) ?? '#9aa8b8', [colors]);
  const stale = context.dataUpdatedAt > 0 && now - context.dataUpdatedAt > 30_000;
  const job = context.data;

  return <Card className="h-full">
    <CardHeader><h2 className="font-semibold text-white">Virtual camera</h2></CardHeader>
    <CardContent className="space-y-3">
      <label className="sr-only" htmlFor="bmcu-preview-printer">Printer</label>
      <select id="bmcu-preview-printer" value={printer} onChange={(event) => setPrinter(event.target.value)} className="w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-sm text-white">
        <option value="">Select printer…</option>
        {printers.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      {printers.isError && <p role="status" className="text-sm text-amber-400">Could not load printers.</p>}
      {!printer && <p className="text-sm text-bambu-gray">Select a printer to view its current print.</p>}
      {printer && context.isLoading && <p role="status" className="text-sm text-bambu-gray">Loading print…</p>}
      {printer && context.isError && <p role="status" className="text-sm text-amber-400">Virtual camera unavailable: {context.error.message}</p>}
      {printer && !context.isError && stale && <p role="status" className="text-sm text-amber-400">Printer updates delayed.</p>}
      {printer && !context.isError && job && <>
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-bambu-gray-light"><span className="truncate">{job.jobName || 'Current print'} · Plate {job.plate}</span><span>Layer {job.reportedLayer}/{job.totalLayers || '—'}{job.progress == null ? '' : ` · ${job.progress}%`}</span></div>
        <Suspense fallback={<p className="text-sm text-bambu-gray">Loading virtual camera…</p>}>
          <VirtualPrintCamera key={`${job.archiveId}:${job.plate}`} gcodeUrl={`${api.getArchiveGcode(job.archiveId)}?plate=${job.plate}`} reportedLayer={job.reportedLayer} toolColor={toolColor} />
        </Suspense>
        <p className="text-xs text-bambu-gray">Estimated from the print plan · drag to rotate</p>
      </>}
    </CardContent>
  </Card>;
}
