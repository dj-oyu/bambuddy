import { filterLayersByType, layersByFilament, parseGcodeToolpath, ToolpathType } from '../../lib/gcodeToolpath';
import { buildSegmentData, type SegmentData } from '../../lib/vendor/toolpathRenderer.js';

export type CameraResult = { data: SegmentData } | { error: string };

self.onmessage = async ({ data: { url, token } }: MessageEvent<{ url: string; token: string | null }>) => {
  try {
    const response = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!response.ok) throw new Error(`Could not load the job G-code (HTTP ${response.status}).`);
    const parsed = parseGcodeToolpath(await response.text(), { markedLayersOnly: true });
    if (!parsed.layers.length) throw new Error('This G-code has no explicit layer markers.');
    const extrusions = filterLayersByType(parsed.layers, new Set([ToolpathType.travel]));
    const data = buildSegmentData(layersByFilament(extrusions), parsed.defaultWidth);
    if (!data.nSeg || !data.bbox || data.hasNaN) throw new Error('No usable extrusion geometry in this plate.');
    const buffers = [...Object.values(data), ...Object.values(data.meta)]
      .filter((value): value is ArrayBufferView => ArrayBuffer.isView(value))
      .map((value) => value.buffer as ArrayBuffer);
    self.postMessage({ data } satisfies CameraResult, { transfer: buffers });
  } catch (err) {
    self.postMessage({ error: err instanceof Error ? err.message : 'Could not prepare the virtual camera.' } satisfies CameraResult);
  }
};
