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
  slots: HTMLDivElement;
  slotNodes: HTMLDivElement[];
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

function buildLoader(): LoaderNodes {
  const root = node('div', 'rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/40 p-4');
  const heading = node('div', 'flex flex-wrap items-center justify-between gap-2');
  const name = node('span', 'font-medium text-white');
  const badge = node('span');
  heading.append(name, badge);
  const age = node('p', 'mt-2 text-xs text-bambu-gray');
  const slots = node('div');
  const slotNodes = [0, 1, 2, 3].map((slot) => {
    const item = node('div');
    item.append(node('div', 'text-[10px] uppercase', 'Slot'), node('div', 'font-semibold', String(slot + 1)));
    slots.append(item);
    return item;
  });
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
  root.append(heading, age, slots, values, warning, footer);
  return { root, name, badge, age, slots, slotNodes, values, motion, pull, pressure, warning, filament, faults };
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
  style(view.slots, `mt-4 grid grid-cols-4 gap-2 ${live ? '' : 'opacity-50'}`);
  view.slotNodes.forEach((slot, index) => {
    style(slot, `rounded-md border px-2 py-3 text-center ${live && link.currentSlot === index ? 'border-bambu-green bg-bambu-green/15 text-white' : link.currentSlot === index ? 'border-amber-500/40 text-bambu-gray-light' : 'border-bambu-dark-tertiary text-bambu-gray'}`);
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

  return <Card><CardHeader><h2 className="font-semibold text-white">Loader state</h2></CardHeader><CardContent><div ref={container} className="grid gap-3 sm:grid-cols-2" /></CardContent></Card>;
}
