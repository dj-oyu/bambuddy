import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type BMCUGcodeWindow } from '../../api/client';
import { Card, CardContent, CardHeader } from '../Card';

const toolColor = (tool: number) => `hsl(${(Math.abs(tool) * 137.508 + 145) % 360} 75% 65%)`;

function ToolpathCanvas({ data }: { data: BMCUGcodeWindow }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const layers = useMemo(() => data.layers.map((encoded) => {
    const binary = atob(encoded);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const view = new DataView(bytes.buffer);
    const values = new Float32Array(bytes.length / 4);
    for (let i = 0; i < values.length; i++) values[i] = view.getFloat32(i * 4, true);
    return values;
  }), [data.layers]);
  const tools = useMemo(() => {
    const seen = new Set<number>();
    for (const layer of layers) for (let i = 4; i < layer.length; i += 5) seen.add(layer[i]);
    return [...seen].sort((a, b) => a - b);
  }, [layers]);

  useEffect(() => {
    const ctx = canvas.current?.getContext('2d');
    if (!ctx) return;
    const width = ctx.canvas.width, height = ctx.canvas.height, pad = 18;
    ctx.clearRect(0, 0, width, height);
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const layer of layers) for (let i = 0; i + 4 < layer.length; i += 5) {
      minX = Math.min(minX, layer[i], layer[i + 2]); maxX = Math.max(maxX, layer[i], layer[i + 2]);
      minY = Math.min(minY, layer[i + 1], layer[i + 3]); maxY = Math.max(maxY, layer[i + 1], layer[i + 3]);
    }
    if (!Number.isFinite(minX)) return;
    const scale = Math.min((width - 2 * pad) / Math.max(maxX - minX, 1), (height - 2 * pad) / Math.max(maxY - minY, 1));
    const offsetX = (width - (maxX - minX) * scale) / 2;
    const offsetY = (height - (maxY - minY) * scale) / 2;
    ctx.lineWidth = 1.2;
    ctx.lineCap = 'round';
    layers.forEach((layer, index) => {
      ctx.globalAlpha = 0.2 + 0.8 * (index + 1) / layers.length;
      const layerTools = new Set<number>();
      for (let i = 4; i < layer.length; i += 5) layerTools.add(layer[i]);
      for (const tool of layerTools) {
        ctx.strokeStyle = toolColor(tool);
        ctx.beginPath();
        for (let i = 0; i + 4 < layer.length; i += 5) {
          if (layer[i + 4] !== tool) continue;
          ctx.moveTo(offsetX + (layer[i] - minX) * scale, height - offsetY - (layer[i + 1] - minY) * scale);
          ctx.lineTo(offsetX + (layer[i + 2] - minX) * scale, height - offsetY - (layer[i + 3] - minY) * scale);
        }
        ctx.stroke();
      }
    });
    ctx.globalAlpha = 1;
  }, [layers]);

  if (!tools.length) return <p className="p-4 text-sm text-bambu-gray">No extrusion paths in these layers.</p>;
  return <>
    <canvas ref={canvas} width={640} height={280} className="w-full rounded-lg bg-bambu-dark/60" role="img" aria-label={`Planned extrusion paths, layers ${data.firstLayer} to ${data.lastLayer}`} />
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-bambu-gray-light" aria-label="Planned logical tools">
      {tools.map((tool) => <span key={tool} className="flex items-center gap-1.5"><span aria-hidden="true" className="h-2 w-2 rounded-full" style={{ backgroundColor: toolColor(tool) }} />Tool {tool}</span>)}
    </div>
  </>;
}

export function GcodeWindow({ now }: { now: number }) {
  const [printer, setPrinter] = useState('');
  const printers = useQuery({ queryKey: ['bmcu-preview-printers'], queryFn: api.getPrinters });
  const preview = useQuery({
    queryKey: ['bmcu-gcode-window', printer],
    queryFn: ({ signal }) => api.getBMCUGcodeWindow(Number(printer), signal),
    enabled: printer !== '', retry: false,
    refetchInterval: (query) => query.state.error ? 60_000 : 15_000,
  });
  const stale = preview.dataUpdatedAt > 0 && now - preview.dataUpdatedAt > 30_000;

  return <Card className="h-full">
    <CardHeader><h2 className="font-semibold text-white">Recent layer preview</h2><p className="mt-1 text-xs text-bambu-gray">Planned extrusion paths · up to 20 layers before the reported layer</p></CardHeader>
    <CardContent className="space-y-3">
      <label className="block text-xs text-bambu-gray-light" htmlFor="bmcu-preview-printer">Printer</label>
      <select id="bmcu-preview-printer" value={printer} onChange={(event) => setPrinter(event.target.value)} className="w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-sm text-white">
        <option value="">Select printer…</option>
        {printers.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      {printers.isError && <p role="status" className="text-sm text-amber-400">Could not load printers.</p>}
      {!printer && <p className="text-sm text-bambu-gray">Select the printer associated with this BMCU to load its current job.</p>}
      {printer && preview.isLoading && <p role="status" className="text-sm text-bambu-gray">Preparing G-code preview…</p>}
      {printer && preview.isError && <p role="status" className="text-sm text-amber-400">Preview unavailable: {preview.error.message}</p>}
      {printer && !preview.isError && stale && <p role="status" className="text-sm text-amber-400">Preview is out of date; waiting for a fresh printer report.</p>}
      {printer && !preview.isError && !stale && preview.data && <>
        <p className="text-sm text-bambu-gray-light">{preview.data.jobName || 'Unnamed job'} · Plate {preview.data.plate}</p>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-bambu-gray-light"><span>Layers {preview.data.firstLayer}–{preview.data.lastLayer}</span><span>Reported layer {preview.data.reportedLayer}/{preview.data.totalLayers || '—'}</span><span>Reported progress {preview.data.progress == null ? '—' : `${preview.data.progress}%`}</span><span>As of {new Date(preview.dataUpdatedAt).toLocaleTimeString()}</span></div>
        <ToolpathCanvas data={preview.data} />
        {preview.data.truncatedReason && <p className="text-xs text-amber-400">Only earlier layers were parsed: {preview.data.truncatedReason}</p>}
        <p className="text-xs text-bambu-gray-light">Filament comparison: Pending — physical slot mapping and historical colors are not verified.</p>
        <p className="text-xs text-bambu-gray">From the original archive: injected or edited G-code may not be reflected. Planned paths only, not a camera or proof of extrusion. Colors identify logical tools, not actual filament colors. Object skips may not be reflected. Layer and progress report freshness is unknown.</p>
      </>}
    </CardContent>
  </Card>;
}
