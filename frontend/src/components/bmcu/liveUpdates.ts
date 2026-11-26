import type { BMCUMonitorDetail } from '../../api/bmcuMonitors';

type Listener = (detail: BMCUMonitorDetail) => void;
const listeners = new Map<string, Set<Listener>>();

/** Page-local fanout; the high-rate path does not enter React state. */
export function subscribeLoader(deviceId: string, listener: Listener): () => void {
  const current = listeners.get(deviceId) ?? new Set<Listener>();
  current.add(listener);
  listeners.set(deviceId, current);
  return () => {
    current.delete(listener);
    if (!current.size) listeners.delete(deviceId);
  };
}

export function publishLoader(detail: BMCUMonitorDetail): void {
  listeners.get(detail.deviceId)?.forEach((listener) => listener(detail));
}
