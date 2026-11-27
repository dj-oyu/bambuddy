import { useCallback, useLayoutEffect, useRef } from 'react';
import type { BMCULinkSnapshot } from '../../api/bmcuMonitors';
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
  slotStates: HTMLSpanElement[];
  filamentIndicators: SVGCircleElement[];
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

// Spools are schematic. The sensor indicator alone represents filament presence.
function buildSpool() {
  const icon = svgNode('svg', { viewBox: '0 0 74 54', class: 'mx-auto h-auto w-full max-w-[78px]', 'aria-hidden': 'true', fill: 'none' });
  const ellipse = (cx: number, cy: number, rx: number, ry: number, fill: string, stroke = 'none') =>
    svgNode('ellipse', { cx: String(cx), cy: String(cy), rx: String(rx), ry: String(ry), fill, stroke });
  const body = svgNode('g', { transform: 'rotate(-12 37 28)' });
  icon.append(ellipse(38, 48, 25, 3, '#101518'), body);
  body.append(ellipse(47, 27, 13, 21, '#354249', '#89969b'),
    svgNode('path', { d: 'M26 18 L47 18 A6 9 0 0 1 47 36 H26 Z', fill: '#38464b' }));
  const winding = svgNode('g', { color: '#687b7c' });
  for (const cx of [43, 40, 37, 34, 31]) {
    winding.append(ellipse(cx, 27, 10, 16, 'currentColor', '#233e39'));
  }
  const rim = ellipse(27, 27, 13, 21, '#263339', '#abb9bd');
  const wheel = svgNode('g', { transform: 'translate(27 27) scale(.58 1)' });
  const spokes = svgNode('g', {});
  for (const angle of [0, 120, 240]) {
    spokes.append(svgNode('path', { d: 'M-2 -7 L-4 -16 Q0 -18 4 -16 L2 -7 Z', transform: `rotate(${angle})`, fill: '#87999f' }));
  }
  wheel.append(spokes, ellipse(0, 0, 5.5, 5.5, '#526167', '#9aa8ac'), ellipse(0, 0, 2, 2, '#111a1e'));
  body.append(winding, rim, wheel);
  const indicator = svgNode('circle', { cx: '60', cy: '44', r: '3.5', stroke: '#a5b5b6', 'stroke-width': '1' });
  icon.append(indicator);
  return { icon, indicator };
}

function buildLoader(): LoaderNodes {
  const root = node('div', 'rounded-xl border border-bambu-dark-tertiary bg-bambu-dark/40 p-4 sm:p-5');
  const heading = node('div', 'flex flex-wrap items-center justify-between gap-2');
  const name = node('span', 'font-medium text-white');
  const badge = node('span');
  heading.append(name, badge);
  const deck = node('div', 'mt-3 overflow-hidden rounded-xl border border-bambu-dark-tertiary bg-gradient-to-b from-[#30393d] via-[#20282b] to-[#161c1e] px-2 pb-2 pt-2 shadow-inner sm:px-3');
  const slots = node('div');
  const slotStates: HTMLSpanElement[] = [];
  const filamentIndicators: SVGCircleElement[] = [];
  const slotNodes = [0, 1, 2, 3].map((slot) => {
    const item = node('div');
    const label = node('div', 'flex items-center justify-between text-[10px] font-medium uppercase tracking-wide', 'Slot');
    const state = node('span', 'block truncate text-center text-[11px]');
    const { icon, indicator } = buildSpool();
    filamentIndicators.push(indicator);
    item.append(label, node('div', 'font-mono text-lg font-semibold leading-tight tabular-nums', String(slot + 1)), icon, state);
    slotStates.push(state);
    slots.append(item);
    return item;
  });
  const routing = svgNode('svg', { viewBox: '0 0 400 46', preserveAspectRatio: 'none', class: 'block h-10 w-full', 'aria-hidden': 'true' });
  const css = svgNode('style', {});
  css.textContent = '@keyframes bmcu-route{to{stroke-dashoffset:-18}} @keyframes bmcu-route-reverse{to{stroke-dashoffset:18}} @media (prefers-reduced-motion:reduce){.bmcu-route-moving{animation:none!important}}';
  routing.append(css);
  const flowPaths = [50, 150, 250, 350].map((x) => {
    const d = `M${x} 0 V6 C${x} 27 200 10 200 34 V45`;
    routing.append(svgNode('path', { d, fill: 'none', stroke: '#586467', 'stroke-width': '4', 'vector-effect': 'non-scaling-stroke' }));
    return svgNode('path', { d, fill: 'none', stroke: 'transparent', 'stroke-width': '3', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' });
  });
  routing.append(...flowPaths);
  const flowLabel = node('div', 'mx-auto w-fit max-w-full rounded border border-[#566166] bg-[#252e31] px-3 py-1 text-center text-[11px] font-medium text-bambu-gray-light shadow-sm');
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
  return { root, name, badge, slots, slotNodes, slotStates, filamentIndicators, flowPaths, flowLabel, buffer, bufferMarker, bufferSpring, pressure, pressureState, pressureFill, pressureTrack, diagnostics, warning, filament, faults };
}

function updateLoader(view: LoaderNodes, link: BMCULinkSnapshot, elapsedSeconds: number, outdated: boolean) {
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
    const indicator = view.filamentIndicators[index];
    indicator.setAttribute('fill', present ? (live ? '#7de1ba' : '#bdac8e') : '#21292b');
    indicator.setAttribute('stroke-dasharray', present == null ? '2 2' : 'none');
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
    text(view.slotStates[index], present == null ? 'Unknown' : present ? 'Present' : 'Empty');
    slot.setAttribute('aria-label', `Slot ${index + 1}: ${isSelected ? (live ? 'selected, ' : 'last selected, ') : ''}filament ${present == null ? 'unknown' : present ? 'present' : 'empty'}${live ? '' : ', last reported'}`);
    style(slot, `min-w-0 rounded-md border px-1.5 pb-1.5 pt-2 text-left shadow-[inset_0_1px_0_#ffffff12,0_2px_2px_#0004] ${live && isSelected ? 'border-bambu-green bg-bambu-green/15 text-white' : isSelected ? 'border-amber-500/40 bg-amber-500/5 text-bambu-gray-light' : 'border-[#465256] bg-[#252d30] text-bambu-gray-light'}`);
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
  style(view.pressureState, `font-medium ${jam ? 'text-red-400' : 'text-white'}`);
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

export function SlotStateGrid({ links, elapsedSeconds = 0, outdated = false, deviceId, liveConnected = false }: {
  links: BMCULinkSnapshot[];
  elapsedSeconds?: number;
  outdated?: boolean;
  deviceId?: string;
  liveConnected?: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const nodes = useRef(new Map<number, LoaderNodes>());
  const live = useRef<{ deviceId: string; links: BMCULinkSnapshot[]; at: number } | null>(null);
  const isOutdated = useRef(outdated);

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
      updateLoader(view, link, elapsed, stale);
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
  }, [links, elapsedSeconds, outdated, deviceId, liveConnected, renderLinks]);

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
