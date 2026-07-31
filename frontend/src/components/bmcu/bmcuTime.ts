import { parseUTCDate } from '../../utils/date';

const JST_TIME_ZONE = 'Asia/Tokyo';

export function bmcuTimestamp(value: string): number {
  return parseUTCDate(value)?.getTime() ?? 0;
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
