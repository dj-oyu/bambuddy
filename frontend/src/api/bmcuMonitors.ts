import { getAuthToken } from './client';

const API_ROOT = '/api/v1/bmcu-monitors';
/** Device-level connectivity: a BMB1 session is open, or it is not. */
export type MonitorHealth = 'online' | 'offline';
/** How much is known about one link's loader view.
 *  `no_data` — nothing was ever decoded, so every loader field below is null.
 *  `stale`   — something was, and the bridge has said nothing since. */
export type LinkState = 'online' | 'stale' | 'no_data';
export type TimelineSeverity = 'info' | 'warning' | 'error' | 'critical';
export type TimelineSource = 'bmcu' | 'bambuddy' | 'transport' | 'pico';

export interface BMCUMonitorSummary {
  deviceId: string;
  displayName: string;
  firmware: string;
  health: MonitorHealth;
  lastSeenAt: string | null;
  bootId: string | null;
  linkCount: number;
  onlineLinks: number;
  /** Zero-padded 20-digit decimal string. A u64 — never parse it into a number. */
  ackSequence: string;
  replayPending: number;
  /** Always null: the aggregation is not implemented, and 0 would claim the
   *  device reports nothing wrong. */
  anomalyCount: number | null;
}

export interface BMCULinkSnapshot {
  linkIndex: number;
  linkId: string;
  state: LinkState;
  /** Zero-based selected channel; null means none selected (state !== 'no_data')
   *  or unknown (state === 'no_data'). */
  currentSlot: number | null;
  /** Filament-presence bitmask (the microswitch), not the hardware channel mask.
   *  0 is four empty channels; null is no reading. */
  activeMask: number | null;
  /** Per-channel motion enum, four entries. Was a stringified tuple before #3. */
  motion: number[] | null;
  /** Pull of the selected channel, percent, 50 neutral; null when none selected. */
  pullPercent: number | null;
  pressure: number | null;
  /** BMCU crc_error + frame_error, cumulative. Read it as a delta, not a total. */
  faultCount: number | null;
  /** Age of the loader values above; null when no STATUS was ever decoded. */
  statusAgeS: number | null;
  lastSeenAt: string | null;
}

export interface BMCUMonitorDetail extends BMCUMonitorSummary {
  firstSeenAt: string | null;
  links: BMCULinkSnapshot[];
}

export interface BMCUTimelinePoint {
  id: string;
  at: string;
  linkIndex: number | null;
  slot: number | null;
  pullPercent: number | null;
  pressure: number | null;
  motion: number | null;
  kind: string;
  label: string;
  severity: TimelineSeverity;
  source: TimelineSource;
  anomaly: boolean;
  missingData: boolean;
}

export interface BMCUMetricPoint {
  at: string;
  heapFreeBytes: number | null;
  temperatureC: number | null;
  loopDelayUs: number | null;
  uartBacklog: number | null;
  uartErrors: number | null;
  wifiRssiDbm: number | null;
  replayPending: number | null;
  journalBytes: number | null;
  ackAgeMs: number | null;
  loopGapAvgUs?: number | null;
  loopGapP95Us?: number | null;
  loopGapP99Us?: number | null;
  transportEncodeAvgUs?: number | null;
  transportSendAvgUs?: number | null;
  transportSendMaxUs?: number | null;
  gcLastUs?: number | null;
  gcMaxUs?: number | null;
  gcCount?: number | null;
  uartDrainBytes?: number | null;
  uartOverflowTotal?: number | null;
  uartBacklogMax?: number | null;
  uartServiceDelayUs?: number | null;
  uartCrcErrorsTotal?: number | null;
  uartSequenceGapsTotal?: number | null;
}

export interface BMCUTimelineResponse {
  points: BMCUTimelinePoint[];
  from: string;
  to: string;
  downsampled: boolean;
}

type QueryValue = string | number | null | undefined;

async function monitorRequest<T>(path = '', query?: Record<string, QueryValue>): Promise<T> {
  const search = new URLSearchParams();
  Object.entries(query ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  });
  const token = getAuthToken();
  const response = await fetch(`${API_ROOT}${path}${search.size ? `?${search}` : ''}`, {
    cache: 'no-store',
    credentials: 'include',
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const bmcuMonitorsApi = {
  list: () => monitorRequest<BMCUMonitorSummary[]>(),
  get: (deviceId: string) =>
    monitorRequest<BMCUMonitorDetail>(`/${encodeURIComponent(deviceId)}`),
  timeline: (deviceId: string, query: { from: string; to: string; resolution?: string; link?: number }) =>
    monitorRequest<BMCUTimelineResponse>(`/${encodeURIComponent(deviceId)}/timeline`, query),
  metrics: (deviceId: string, query: { from: string; to: string; resolution?: string }) =>
    monitorRequest<BMCUMetricPoint[]>(`/${encodeURIComponent(deviceId)}/metrics`, query),
};
