import { parseUTCDate } from '../../utils/date';

const JST_TIME_ZONE = 'Asia/Tokyo';

export function bmcuTimestamp(value: string): number {
  return parseUTCDate(value)?.getTime() ?? 0;
}

export function formatBMCUAge(seconds: number): string {
  const elapsed = Math.max(0, Math.floor(seconds));
  if (elapsed < 60) return `${elapsed}s ago`;
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`;
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)}h ago`;
  return `${Math.floor(elapsed / 86400)}d ago`;
}

export function formatBMCUTime(value: string | number, locales?: string | string[]): string {
  const date = typeof value === 'number' ? new Date(value) : parseUTCDate(value);
  return date?.toLocaleTimeString(locales, {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: JST_TIME_ZONE,
  }) ?? '—';
}

export function formatBMCUDateTime(value: string | number, locales?: string | string[]): string {
  const date = typeof value === 'number' ? new Date(value) : parseUTCDate(value);
  return date?.toLocaleString(locales, {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZone: JST_TIME_ZONE,
  }) ?? '—';
}
