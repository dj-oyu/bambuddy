import { useCallback, useLayoutEffect, useRef } from 'react';
import type { BMCULinkSnapshot } from '../../api/bmcuMonitors';
import type { AMSUnit } from '../../api/client';
import { Card, CardContent, CardHeader } from '../Card';
import { formatBMCUAge } from './bmcuTime';
import { subscribeLoader } from './liveUpdates';

// React owns the empty container. Its children are owned exclusively by this
// renderer, so live telemetry does not require React reconciliation.
type LoaderNodes = {
  root: HTMLDivElement;
  name: HTMLSpanElement;
  badge: HTMLSpanElement;
  slots: HTMLDivElement;
  slotNodes: HTMLDivElement[];
  spools: SVGCircleElement[];
  flowPaths: SVGPathElement[];
  flowLabel: HTMLDivElement;
  buffer: HTMLDivElement;
  bufferMarker: SVGRectElement;
  bufferSpring: SVGPathElement;
  pressure: HTMLDivElement;
  pressureState: HTMLSpanElement;
  pressureFill: HTMLDivElement;
  pressureTrack: HTMLDivElement;
  diagnostics: HTMLDivElement;
  warning: HTMLDivElement;
  filament: HTMLSpanElement;
  faults: HTMLSpanElement;
};

const node = <K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = '') => {
  const value = document.createElement(tag);
  value.className = className;
  if (text) value.textContent = text;
  return value;
};
const text = (element: HTMLElement, value: string) => { if (element.textContent !== value) element.textContent = value; };
const style = (element: HTMLElement, value: string) => { if (element.className !== value) element.className = value; };
const svgNode = <K extends keyof SVGElementTagNameMap>(tag: K, attributes: Record<string, string>) => {
  const result = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, value);
  return result;
};

// A spool is a simple disc with a hub, not a model of winding or remaining amount.
function buildSpool() {
  const icon = svgNode('svg', { viewBox: '0 0 48 48', class: 'mx-auto mt-2 h-11 w-11', 'aria-hidden': 'true' });
  const disc = svgNode('circle', { cx: '24', cy: '24', r: '19', stroke: '#8c9da1', 'stroke-width': '1.5' });
  icon.append(disc, svgNode('circle', { cx: '24', cy: '24', r: '5', fill: '#1a2225', stroke: '#8c9da1', 'stroke-width': '1' }));
  return { icon, disc };
}

function buildLoader(): LoaderNodes {
  const root = node('div', 'rounded-xl border border-bambu-dark-tertiary bg-bambu-dark/40 p-4 sm:p-5');
  const heading = node('div', 'flex flex-wrap items-center justify-between gap-2');
  const name = node('span', 'font-medium text-white');
  const badge = node('span');
  heading.append(name, badge);
  const deck = node('div', 'mt-4 px-2 sm:px-3');
  const slots = node('div');
  const spools: SVGCircleElement[] = [];
  const slotNodes = [0, 1, 2, 3].map((slot) => {
    const item = node('div');
    const { icon, disc } = buildSpool();
    spools.push(disc);
    item.setAttribute('role', 'img');
    item.append(node('div', 'text-center font-mono text-xs tabular-nums text-bambu-gray-light', String(slot + 1)), icon);
    slots.append(item);
    return item;
  });
  const routing = svgNode('svg', { viewBox: '0 0 400 66', preserveAspectRatio: 'none', class: 'block h-14 w-full', 'aria-hidden': 'true' });
  const css = svgNode('style', {});
  css.textContent = '@keyframes bmcu-route{to{stroke-dashoffset:-18}} @keyframes bmcu-route-reverse{to{stroke-dashoffset:18}} @media (prefers-reduced-motion:reduce){.bmcu-route-moving{animation:none!important}}';
  routing.append(css);
  const flowPaths = [50, 150, 250, 350].map((x) => {
    const d = `M${x} 0 V6 C${x} 27 200 10 200 34 V45`;
    routing.append(svgNode('path', { d, fill: 'none', stroke: '#586467', 'stroke-width': '4', 'vector-effect': 'non-scaling-stroke' }));
    return svgNode('path', { d, fill: 'none', stroke: 'transparent', 'stroke-width': '3', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' });
  });
  routing.append(...flowPaths);
  routing.append(svgNode('circle', { cx: '200', cy: '45', r: '5', fill: '#8c9da1' }),
    svgNode('path', { d: 'M200 50 V57 M192 57 H208 L204 64 H196 Z', fill: '#8c9da1', stroke: '#8c9da1', 'stroke-width': '2', 'vector-effect': 'non-scaling-stroke' }));
  const flowLabel = node('div', 'mt-1 text-center text-[11px] text-bambu-gray-light');
  deck.append(slots, routing, flowLabel);
  const gauges = node('div', 'mt-4 grid gap-3');
  gauges.style.gridTemplateColumns = 'repeat(auto-fit, minmax(min(100%, 9rem), 1fr))';
  const buffer = node('div', 'rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/35 p-3');
  buffer.setAttribute('role', 'img');
  const bufferHeading = node('div', 'whitespace-nowrap text-xs text-bambu-gray-light', 'Buffer position');
  const gauge = svgNode('svg', { viewBox: '0 0 200 36', class: 'mt-2 h-9 w-full', 'aria-hidden': 'true' });
  gauge.append(svgNode('path', { d: 'M5 8 H195 L191 28 H9 Z', fill: '#182024', stroke: '#455257' }),
    svgNode('path', { d: 'M10 18 H190', stroke: '#627279', 'stroke-width': '4', 'stroke-linecap': 'round' }),
    svgNode('path', { d: 'M10 17 H190', stroke: '#a2b1b5', 'stroke-width': '1' }),
    svgNode('path', { d: 'M100 4 V32', stroke: '#94a3b8', 'stroke-width': '1', 'stroke-dasharray': '2 3' }));
  for (let tick = 10; tick <= 190; tick += 18) {
    gauge.append(svgNode('path', { d: `M${tick} 29 v${tick === 100 ? 6 : 3}`, stroke: '#87959a' }));
  }
  const bufferSpring = svgNode('path', { d: '', fill: 'none', stroke: '#6ee7b7', 'stroke-width': '2', 'stroke-linejoin': 'round' });
  const bufferMarker = svgNode('rect', { x: '96', y: '8', width: '8', height: '20', rx: '3', fill: '#6ee7b7', stroke: '#163d32', 'stroke-width': '2' });
  gauge.append(bufferSpring, bufferMarker);
  buffer.append(bufferHeading, gauge);
  const pressure = node('div', 'rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/35 p-3');
  pressure.setAttribute('role', 'img');
  const pressureHeading = node('div', 'flex items-center justify-between gap-1 whitespace-nowrap text-xs');
  const pressureState = node('span', 'w-3 shrink-0 text-center font-medium');
  pressureState.setAttribute('aria-hidden', 'true');
  pressureHeading.append(node('span', 'text-bambu-gray-light', 'Pressure response'), pressureState);
  const pressureTrack = node('div', 'mt-4 h-2 overflow-hidden rounded-full bg-slate-600/70');
  const pressureFill = node('div', 'h-full rounded-full bg-emerald-400 motion-safe:transition-[width] motion-safe:duration-300');
  pressureTrack.append(pressureFill);
  pressure.append(pressureHeading, pressureTrack);
  gauges.append(buffer, pressure);
  const warning = node('div', 'mt-3 rounded bg-amber-500/10 px-2 py-1.5 text-xs text-amber-300');
  const details = node('details', 'mt-3 text-xs text-bambu-gray');
  const summary = node('summary', 'cursor-pointer hover:text-bambu-gray-light', 'Diagnostics');
  const diagnostics = node('div', 'mt-2 space-y-1 font-mono');
  const footer = node('div', 'flex flex-wrap items-center justify-between gap-2');
  const filament = node('span');
  const faults = node('span');
  footer.append(filament, faults);
  details.append(summary, diagnostics, footer);
  root.append(heading, deck, gauges, warning, details);
  return { root, name, badge, slots, slotNodes, spools, flowPaths, flowLabel, buffer, bufferMarker, bufferSpring, pressure, pressureState, pressureFill, pressureTrack, diagnostics, warning, filament, faults };
}

function updateLoader(view: LoaderNodes, link: BMCULinkSnapshot, elapsedSeconds: number, outdated: boolean, ams?: AMSUnit) {
  const ageSeconds = link.statusAgeS == null ? null : Math.max(0, link.statusAgeS + elapsedSeconds);
  const live = !outdated && link.state === 'online' && ageSeconds != null && ageSeconds < 15;
  const known = link.state !== 'no_data';
  text(view.name, link.linkId);
  text(view.badge, live ? 'Live' : known ? 'Stale' : 'no data');
  style(view.badge, `inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs ${live ? 'bg-emerald-500/10 text-emerald-400' : known ? 'bg-amber-500/10 text-amber-400' : 'bg-bambu-dark-tertiary text-bambu-gray-light'}`);
  view.badge.title = ageSeconds == null ? 'No BMCU status received' : `BMCU status ${formatBMCUAge(ageSeconds)}`;
  view.badge.setAttribute('aria-label', `${view.badge.textContent}: ${view.badge.title}`);
  const selected = Number.isInteger(link.currentSlot) && link.currentSlot != null && link.currentSlot >= 0 && link.currentSlot < 4 ? link.currentSlot : null;
  const currentMotion = selected == null ? undefined : link.motion?.[selected];
  const motionNames: Record<number, string> = { 0: 'Idle', 1: 'Loading requested', 2: 'In use', 3: 'Preparing to unload', 4: 'Unloading requested', 5: 'Preparing to use', 6: 'Stopped' };
  const motionState = selected == null ? (known && link.currentSlot == null ? 'No active slot' : 'Slot unknown') : currentMotion == null ? 'Motion unknown' : motionNames[currentMotion] ?? 'Motion unknown';
  text(view.flowLabel, live ? motionState : known ? `${motionState} · last reported` : 'No data');
  style(view.slots, `grid grid-cols-4 gap-1 ${live ? '' : 'opacity-70'}`);
  view.slotNodes.forEach((slot, index) => {
    const isSelected = selected === index;
    const present = link.activeMask == null ? null : (link.activeMask & (1 << index)) !== 0;
    const motion = link.motion?.[index];
    const tray = present ? ams?.tray.find((item) => item.id === index) : undefined;
    const color = tray?.tray_color && /^[\da-f]{6}([\da-f]{2})?$/i.test(tray.tray_color) ? `#${tray.tray_color.slice(0, 6)}` : null;
    const spool = view.spools[index];
    spool.setAttribute('fill', present ? color ?? '#798a8d' : 'none');
    spool.setAttribute('opacity', present == null ? '0.25' : present ? '1' : '0.35');
    spool.setAttribute('stroke', isSelected ? (live ? '#6ee7b7' : '#d49c47') : '#8c9da1');
    spool.setAttribute('stroke-width', isSelected ? '3' : '1.5');
    spool.setAttribute('stroke-dasharray', present == null ? '3 3' : 'none');
    const path = view.flowPaths[index];
    path.setAttribute('stroke', isSelected ? (live ? '#36c88c' : '#d49c47') : 'transparent');
    const moving = live && isSelected && (motion === 1 || motion === 4);
    path.setAttribute('class', moving ? 'bmcu-route-moving' : '');
    path.setAttribute('stroke-dasharray', moving ? '6 12' : 'none');
    const animation = moving ? `${motion === 4 ? 'bmcu-route-reverse' : 'bmcu-route'} 0.9s linear infinite` : '';
    if (path.getAttribute('data-animation') !== animation) {
      path.setAttribute('data-animation', animation);
      path.style.animation = animation;
    }
    const material = tray?.tray_type ? `, ${tray.tray_type}` : '';
    const description = `Slot ${index + 1}: ${isSelected ? (live ? 'selected, ' : 'last selected, ') : ''}filament ${present == null ? 'unknown' : present ? 'present' : 'empty'}${material}${color ? `, ${color}` : ''}${live ? '' : ', last reported'}`;
    slot.setAttribute('aria-label', description);
    slot.title = description;
    slot.dataset.selected = isSelected && live ? 'true' : 'false';
    style(slot, 'min-w-0 text-center');
  });
  const pull = selected != null && link.pullPercent != null && Number.isFinite(link.pullPercent) && link.pullPercent >= 0 && link.pullPercent <= 100 ? link.pullPercent : null;
  const position = pull == null ? 'Unavailable' : pull < 49 ? 'Pulled' : pull > 51 ? 'Pressed' : 'Near neutral';
  const x = pull == null ? 100 : 10 + pull * 1.8;
  view.bufferMarker.setAttribute('x', String(x - 4));
  const step = Math.max(0, x - 16) / 8;
  const spring = Array.from({ length: 7 }, (_, i) => `L${(10 + step * (i + 1)).toFixed(1)} ${i % 2 ? 22 : 14}`).join(' ');
  view.bufferSpring.setAttribute('d', `M10 18 ${spring} L${Math.max(10, x - 4).toFixed(1)} 18`);
  view.bufferSpring.setAttribute('opacity', pull == null ? '0' : live ? '1' : '0.45');
  view.bufferMarker.setAttribute('opacity', pull == null ? '0' : live ? '1' : '0.45');
  view.buffer.setAttribute('aria-label', pull == null ? 'Buffer position unavailable' : `Buffer position ${pull}%, ${position}; 50% is neutral${live ? '' : ', last reported'}`);
  view.buffer.title = view.buffer.getAttribute('aria-label') || '';
  view.buffer.style.opacity = live ? '1' : '0.6';

  const rawPressure = link.pressure;
  const idle = rawPressure === 0xffff || rawPressure === 0xff74;
  const jam = rawPressure === 0xf06f;
  const protocolCode = rawPressure != null && [0x4700, 0x2b00, 0x1e34, 0xf9c6].includes(rawPressure);
  const expected = pull == null ? null : Math.floor(Math.max(0, pull - 50) * 65535 / 50);
  // The wire field also carries phase codes; only draw a relative level when
  // this report agrees with the selected buffer position's encoded response.
  const measured = rawPressure != null && expected != null && !idle && !jam && !protocolCode && Math.abs(rawPressure - expected) <= 1;
  const level = measured && rawPressure != null ? Math.round(rawPressure * 100 / 65535) : null;
  const response = jam ? 'Jam reported' : idle || rawPressure == null || selected == null ? 'Unavailable' : level != null ? `${level}%` : 'Controller code';
  text(view.pressureState, jam ? '!' : level == null ? '—' : '');
  style(view.pressureState, `w-3 shrink-0 text-center font-medium ${jam ? 'text-red-400' : 'text-bambu-gray'}`);
  view.pressureTrack.style.opacity = level == null ? '0.35' : '1';
  view.pressureFill.style.width = `${level ?? 0}%`;
  view.pressure.setAttribute('aria-label', `Pressure response: ${response}${live ? '' : ', last reported'}`);
  view.pressure.title = level != null ? 'Relative response encoded from buffer position; not a force measurement' : jam ? 'BMCU reported the jam code' : 'The pressure field is unavailable or contains a controller code';
  view.pressure.style.opacity = live ? '1' : '0.6';
  text(view.diagnostics, `Motion ${link.motion?.join(', ') ?? '—'} · Pull ${pull == null ? '—' : `${pull}%`} · Pressure ${rawPressure == null ? '—' : `0x${rawPressure.toString(16).toUpperCase().padStart(4, '0')}`}`);
  view.warning.hidden = live;
  text(view.warning, outdated ? 'Loader updates are delayed.' : !known ? 'No loader state has ever been received for this link.' : 'Link offline or waiting for a fresh STATUS report.');
  text(view.filament, link.activeMask == null ? 'filament —' : `filament 0x${link.activeMask.toString(16).padStart(2, '0')}`);
  text(view.faults, link.faultCount == null ? 'CRC / frame errors: unknown' : `CRC / frame errors: ${link.faultCount} (total)`);
  style(view.faults, '');
}

export function SlotStateGrid({ links, elapsedSeconds = 0, outdated = false, deviceId, liveConnected = false, filamentUnits }: {
  links: BMCULinkSnapshot[];
  elapsedSeconds?: number;
  outdated?: boolean;
  deviceId?: string;
  liveConnected?: boolean;
  filamentUnits?: AMSUnit[];
}) {
  const container = useRef<HTMLDivElement>(null);
  const nodes = useRef(new Map<number, LoaderNodes>());
  const live = useRef<{ deviceId: string; links: BMCULinkSnapshot[]; at: number } | null>(null);
  const isOutdated = useRef(outdated);
  const units = useRef(filamentUnits);

  useLayoutEffect(() => { units.current = filamentUnits; }, [filamentUnits]);

  const renderLinks = useCallback((values: BMCULinkSnapshot[], elapsed: number, stale: boolean) => {
    const root = container.current;
    if (!root) return;
    const ordered = [...values].sort((a, b) => a.linkId.localeCompare(b.linkId, undefined, { numeric: true, sensitivity: 'base' }));
    const keep = new Set(ordered.map((link) => link.linkIndex));
    nodes.current.forEach((view, key) => { if (!keep.has(key)) { view.root.remove(); nodes.current.delete(key); } });
    ordered.forEach((link, index) => {
      let view = nodes.current.get(link.linkIndex);
      if (!view) { view = buildLoader(); nodes.current.set(link.linkIndex, view); }
      if (root.children[index] !== view.root) root.insertBefore(view.root, root.children[index] ?? null);
      const amsIndex = link.amsIndex;
      // Never guess from the link name/order; duplicate firmware AMS IDs are ambiguous.
      const unique = amsIndex != null && values.filter((item) => item.amsIndex === amsIndex).length === 1;
      updateLoader(view, link, elapsed, stale, unique ? units.current?.find((item) => item.id === amsIndex) : undefined);
    });
  }, []);

  useLayoutEffect(() => {
    isOutdated.current = outdated;
    const snapshot = live.current;
    if (liveConnected && !outdated && snapshot && snapshot.deviceId === deviceId) {
      renderLinks(snapshot.links, (Date.now() - snapshot.at) / 1000, false);
    } else {
      renderLinks(links, elapsedSeconds, outdated);
    }
  }, [links, elapsedSeconds, outdated, deviceId, liveConnected, filamentUnits, renderLinks]);

  useLayoutEffect(() => {
    live.current = null;
    if (!deviceId) return;
    return subscribeLoader(deviceId, (detail) => {
      live.current = { deviceId, links: detail.links, at: Date.now() };
      renderLinks(detail.links, 0, isOutdated.current);
    });
  }, [deviceId, renderLinks]);

  return <Card><CardHeader><h2 className="font-semibold text-white">Loader state</h2><p className="mt-1 text-xs text-bambu-gray">Filament paths and buffer response</p></CardHeader><CardContent><div ref={container} className="grid gap-3 sm:grid-cols-2" /></CardContent></Card>;
}
