import { getAuthToken } from './client';

const API_ROOT = '/api/v1/bmcu-monitors';
export type MonitorHealth = 'online' | 'stale' | 'offline' | 'incompatible' | 'unknown';
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
  ackSequence: string;
  replayPending: number;
  anomalyCount: number;
}

export interface BMCULinkSnapshot {
  linkIndex: number;
  linkId: string;
  state: MonitorHealth | 'resyncing';
  currentSlot: number | null;
  activeMask: number;
  motion: string | null;
  pullPercent: number | null;
  pressure: number | null;
  faultCount: number;
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
  motion: string | null;
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
