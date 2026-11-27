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
  age: HTMLParagraphElement;
  selection: HTMLParagraphElement;
  slots: HTMLDivElement;
  slotNodes: HTMLDivElement[];
  slotStates: HTMLSpanElement[];
  spoolIcons: SVGSVGElement[];
  flowPaths: SVGPathElement[];
  flowLabel: HTMLDivElement;
  values: HTMLDivElement;
  motion: HTMLSpanElement;
  pull: HTMLSpanElement;
  pressure: HTMLSpanElement;
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

function buildLoader(): LoaderNodes {
  const root = node('div', 'rounded-xl border border-bambu-dark-tertiary bg-bambu-dark/40 p-4 sm:p-5');
  const heading = node('div', 'flex flex-wrap items-center justify-between gap-2');
  const name = node('span', 'font-medium text-white');
  const badge = node('span');
  heading.append(name, badge);
  const age = node('p', 'mt-2 text-xs text-bambu-gray');
  const selection = node('p', 'mt-4 text-sm font-medium text-bambu-gray-light');
  const slots = node('div');
  const slotStates: HTMLSpanElement[] = [];
  const spoolIcons: SVGSVGElement[] = [];
  const slotNodes = [0, 1, 2, 3].map((slot) => {
    const item = node('div');
    const label = node('div', 'flex items-center justify-between text-[10px] font-medium uppercase tracking-wide', 'Slot');
    const state = node('span', 'mt-2 block text-[11px]');
    const icon = svgNode('svg', { viewBox: '0 0 40 40', class: 'mt-2 h-9 w-9', 'aria-hidden': 'true', fill: 'none' });
    icon.append(svgNode('circle', { cx: '20', cy: '20', r: '16', stroke: 'currentColor', 'stroke-width': '2' }),
      svgNode('circle', { cx: '20', cy: '20', r: '9', stroke: 'currentColor', 'stroke-width': '2', opacity: '.65' }),
      svgNode('circle', { cx: '20', cy: '20', r: '3', fill: 'currentColor' }));
    spoolIcons.push(icon);
    item.append(label, icon, node('div', 'mt-2 text-lg font-semibold tabular-nums', String(slot + 1)), state);
    slotStates.push(state);
    slots.append(item);
    return item;
  });
  const routing = svgNode('svg', { viewBox: '0 0 400 42', preserveAspectRatio: 'none', class: 'h-9 w-full', 'aria-hidden': 'true' });
  const css = svgNode('style', {});
  css.textContent = '@keyframes bmcu-route{to{stroke-dashoffset:-18}} @keyframes bmcu-route-reverse{to{stroke-dashoffset:18}} @media (prefers-reduced-motion:reduce){.bmcu-route-moving{animation:none!important}}';
  routing.append(css);
  const flowPaths = [50, 150, 250, 350].map((x) => {
    const d = `M${x} 0 V12 H200 V40`;
    routing.append(svgNode('path', { d, fill: 'none', stroke: '#4b5563', 'stroke-width': '2', 'vector-effect': 'non-scaling-stroke' }));
    return svgNode('path', { d, fill: 'none', stroke: 'transparent', 'stroke-width': '3', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' });
  });
  routing.append(...flowPaths);
  const flowLabel = node('div', 'text-center text-[10px] font-medium uppercase tracking-widest text-bambu-gray', 'BMCU');
  const values = node('div');
  const addValue = (label: string) => {
    const block = node('div');
    const value = node('span', 'text-white');
    block.append(node('span', 'block text-bambu-gray', label), value);
    values.append(block);
    return value;
  };
  const motion = addValue('Motion');
  const pull = addValue('Pull');
  const pressure = addValue('Pressure');
  const warning = node('div', 'mt-3 rounded bg-amber-500/10 px-2 py-1.5 text-xs text-amber-300');
  const footer = node('div', 'mt-3 flex items-center justify-between text-xs text-bambu-gray');
  const filament = node('span');
  const faults = node('span');
  footer.append(filament, faults);
  root.append(heading, age, selection, slots, routing, flowLabel, values, warning, footer);
  return { root, name, badge, age, selection, slots, slotNodes, slotStates, spoolIcons, flowPaths, flowLabel, values, motion, pull, pressure, warning, filament, faults };
}

function updateLoader(view: LoaderNodes, link: BMCULinkSnapshot, elapsedSeconds: number, outdated: boolean) {
  const ageSeconds = link.statusAgeS == null ? null : Math.max(0, link.statusAgeS + elapsedSeconds);
  const live = !outdated && link.state === 'online' && ageSeconds != null && ageSeconds < 15;
  const known = link.state !== 'no_data';
  text(view.name, link.linkId);
  text(view.badge, live ? 'Live' : known ? 'Stale' : 'no data');
  style(view.badge, `inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs ${live ? 'bg-emerald-500/10 text-emerald-400' : known ? 'bg-amber-500/10 text-amber-400' : 'bg-bambu-dark-tertiary text-bambu-gray-light'}`);
  view.age.hidden = ageSeconds == null;
  text(view.age, ageSeconds == null ? '' : `Last STATUS ${formatBMCUAge(ageSeconds)}`);
  const selected = Number.isInteger(link.currentSlot) && link.currentSlot != null && link.currentSlot >= 0 && link.currentSlot < 4 ? link.currentSlot : null;
  text(view.selection, !known ? 'Selected slot: unknown' : link.currentSlot == null ? (live ? 'No slot selected' : 'Last reported: no slot selected') : selected == null ? 'Selected slot: unknown' : `${live ? 'Selected slot' : 'Last selected slot'}: ${selected + 1}`);
  const currentMotion = selected == null ? undefined : link.motion?.[selected];
  text(view.flowLabel, live && currentMotion === 1 ? 'BMCU · load requested' : live && currentMotion === 4 ? 'BMCU · unload requested' : live && currentMotion === 2 ? 'BMCU · in use' : 'BMCU');
  style(view.slots, `mt-3 grid grid-cols-4 gap-2 ${live ? '' : 'opacity-70'}`);
  view.slotNodes.forEach((slot, index) => {
    const isSelected = selected === index;
    const present = link.activeMask == null ? null : (link.activeMask & (1 << index)) !== 0;
    const motion = link.motion?.[index];
    view.spoolIcons[index].setAttribute('style', `color:${present == null ? '#64748b' : present ? '#6ee7b7' : '#64748b'};opacity:${present === false ? '0.45' : '1'}`);
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
    style(slot, `min-w-0 rounded-lg border px-2 py-3 text-left ${live && isSelected ? 'border-bambu-green bg-bambu-green/15 text-white' : isSelected ? 'border-amber-500/40 bg-amber-500/5 text-bambu-gray-light' : 'border-bambu-dark-tertiary bg-bambu-dark/30 text-bambu-gray-light'}`);
  });
  style(view.values, `mt-4 grid grid-cols-3 gap-2 text-xs ${live ? '' : 'opacity-50'}`);
  text(view.motion, link.motion == null ? '—' : link.motion.join(', '));
  text(view.pull, link.pullPercent == null ? '—' : `${link.pullPercent}%`);
  text(view.pressure, link.pressure == null ? '—' : String(link.pressure));
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

  return <Card><CardHeader><h2 className="font-semibold text-white">Loader state</h2><p className="mt-1 text-xs text-bambu-gray">Selected slot and filament presence reported by each BMCU</p></CardHeader><CardContent><div ref={container} className="grid gap-3 sm:grid-cols-2" /></CardContent></Card>;
}
