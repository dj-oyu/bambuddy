import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Loader2, Info, Clock, AlertTriangle } from 'lucide-react';
import {
  bmcuLinkApi,
  type BMCULinkDevice,
  type BMCULinkEnums,
  type BMCULinkEvent,
} from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { formatRelativeTime } from '../utils/date';

const EVENTS_PAGE_SIZE = 50;

const LINK_STATE_STYLES: Record<BMCULinkDevice['link_state'], { dot: string; badge: string }> = {
  online: { dot: 'bg-green-400', badge: 'bg-green-500/15 text-green-400 border border-green-500/40' },
  stale: { dot: 'bg-yellow-400', badge: 'bg-yellow-500/15 text-yellow-400 border border-yellow-500/40' },
  offline: { dot: 'bg-gray-500', badge: 'bg-gray-500/15 text-gray-400 border border-gray-500/40' },
};

/**
 * Resolve a numeric enum id against an enum table from GET /bmcu-link/enums.
 * Unknown ids render as "unknown(<n>)" so a registry-version mismatch is
 * visible instead of silently wrong.
 */
function resolveEnum(enums: BMCULinkEnums | undefined, table: string, id: number): string {
  const map = enums?.[table];
  if (map && typeof map === 'object') {
    const name = (map as Record<string, string>)[String(id)];
    if (name !== undefined) return name;
  }
  return `unknown(${id})`;
}

/** Render a last_status value; numeric fields with a matching enum table get names. */
function renderStatusValue(enums: BMCULinkEnums | undefined, key: string, value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    const map = enums?.[key];
    if (map && typeof map === 'object') return resolveEnum(enums, key, value);
    return String(value);
  }
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

interface DeviceEventsProps {
  device: BMCULinkDevice;
  enums: BMCULinkEnums | undefined;
}

function DeviceEvents({ device, enums }: DeviceEventsProps) {
  const { t } = useTranslation();
  const [kindFilter, setKindFilter] = useState('');
  const [page, setPage] = useState(0);

  const { data: events, isLoading } = useQuery({
    queryKey: ['bmcu-link-events', device.device_id, kindFilter, page],
    queryFn: () =>
      bmcuLinkApi.getEvents(device.device_id, {
        kind: kindFilter || undefined,
        limit: EVENTS_PAGE_SIZE,
        offset: page * EVENTS_PAGE_SIZE,
      }),
    refetchInterval: 15000,
  });

  const kindTable = enums?.kind;
  const kindOptions =
    kindTable && typeof kindTable === 'object' ? Object.values(kindTable as Record<string, string>) : [];

  return (
    <div className="pt-2 border-t border-bambu-dark-tertiary space-y-2">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h4 className="text-xs font-medium text-white">{t('settings.bmcuLink.recentEvents')}</h4>
        <div className="flex items-center gap-2">
          <select
            value={kindFilter}
            onChange={(e) => {
              setKindFilter(e.target.value);
              setPage(0);
            }}
            className="text-xs bg-bambu-dark-tertiary text-white rounded px-2 py-1 border border-bambu-dark-tertiary"
            aria-label={t('settings.bmcuLink.kindFilter')}
          >
            <option value="">{t('settings.bmcuLink.allKinds')}</option>
            {kindOptions.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </select>
        </div>
      </div>

      {isLoading ? (
        <div className="py-4 flex justify-center">
          <Loader2 className="w-4 h-4 animate-spin text-bambu-green" />
        </div>
      ) : !events || events.length === 0 ? (
        <p className="text-xs text-bambu-gray py-2">{t('settings.bmcuLink.noEvents')}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-bambu-gray border-b border-bambu-dark-tertiary">
                <th className="py-1 pr-3 font-medium">{t('settings.bmcuLink.eventTime')}</th>
                <th className="py-1 pr-3 font-medium">{t('settings.bmcuLink.eventKind')}</th>
                <th className="py-1 pr-3 font-medium">{t('settings.bmcuLink.eventTransaction')}</th>
                <th className="py-1 font-medium">{t('settings.bmcuLink.eventData')}</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event: BMCULinkEvent) => (
                <tr key={event.id} className="border-b border-bambu-dark-tertiary/50 align-top">
                  <td className="py-1 pr-3 text-bambu-gray whitespace-nowrap">
                    {formatRelativeTime(event.server_received_at)}
                  </td>
                  <td className="py-1 pr-3 text-white whitespace-nowrap">
                    {event.kind || resolveEnum(enums, 'kind', event.kind_id)}
                  </td>
                  <td className="py-1 pr-3 text-bambu-gray font-mono">
                    {event.transaction_id ?? '—'}
                  </td>
                  <td className="py-1 text-bambu-gray font-mono break-all">
                    {JSON.stringify(event.data)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex items-center justify-end gap-2">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setPage((p) => Math.max(0, p - 1))}
          disabled={page === 0}
        >
          {t('settings.bmcuLink.prevPage')}
        </Button>
        <span className="text-xs text-bambu-gray">{page + 1}</span>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setPage((p) => p + 1)}
          disabled={!events || events.length < EVENTS_PAGE_SIZE}
        >
          {t('settings.bmcuLink.nextPage')}
        </Button>
      </div>
    </div>
  );
}

interface DeviceCardProps {
  device: BMCULinkDevice;
  enums: BMCULinkEnums | undefined;
}

function DeviceCard({ device, enums }: DeviceCardProps) {
  const { t } = useTranslation();
  // Live status pushed over WebSocket (bmcu_link_status → 'bmcu-link-status'
  // CustomEvent). Overrides the persisted last_status without a refetch.
  const [liveStatus, setLiveStatus] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as
        | { device_id?: string; status?: Record<string, unknown> }
        | undefined;
      if (detail?.device_id === device.device_id && detail.status) {
        setLiveStatus(detail.status);
      }
    };
    window.addEventListener('bmcu-link-status', handler);
    return () => window.removeEventListener('bmcu-link-status', handler);
  }, [device.device_id]);

  const status = liveStatus ?? device.last_status;
  const linkStyle = LINK_STATE_STYLES[device.link_state] ?? LINK_STATE_STYLES.offline;
  const isBenchStub = device.mode === 'bench_stub';

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-base font-semibold text-white truncate">{device.name}</h3>
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${linkStyle.badge}`}
              >
                <span className={`w-2 h-2 rounded-full ${linkStyle.dot}`} />
                {t(`settings.bmcuLink.linkState.${device.link_state}`)}
              </span>
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
                  isBenchStub
                    ? 'bg-yellow-500/15 text-yellow-400 border border-yellow-500/40'
                    : 'bg-blue-500/15 text-blue-400 border border-blue-500/40'
                }`}
              >
                {isBenchStub && <AlertTriangle className="w-3 h-3" />}
                {t(`settings.bmcuLink.mode.${device.mode}`)}
              </span>
            </div>
            <p className="text-xs text-bambu-gray font-mono mt-1 truncate">{device.device_id}</p>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
          <div>
            <div className="text-bambu-gray">{t('settings.bmcuLink.firmware')}</div>
            <div className="text-white">{device.firmware || '—'}</div>
          </div>
          <div>
            <div className="text-bambu-gray">{t('settings.bmcuLink.protocol')}</div>
            <div className="text-white">
              {device.protocol_min === device.protocol_max
                ? device.protocol_min
                : `${device.protocol_min}–${device.protocol_max}`}
            </div>
          </div>
          <div>
            <div className="text-bambu-gray flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {t('settings.bmcuLink.lastSeen')}
            </div>
            <div className="text-white">
              {device.last_seen_at ? formatRelativeTime(device.last_seen_at) : t('settings.bmcuLink.never')}
            </div>
          </div>
          <div>
            <div className="text-bambu-gray">{t('settings.bmcuLink.envelopes')}</div>
            <div className="text-white">
              {device.envelope_count}
              {device.dropped_count > 0 && (
                <span className="text-red-400"> ({t('settings.bmcuLink.dropped', { count: device.dropped_count })})</span>
              )}
            </div>
          </div>
        </div>

        {status && (
          <div className="pt-2 border-t border-bambu-dark-tertiary">
            <h4 className="text-xs font-medium text-white mb-2">{t('settings.bmcuLink.lastStatus')}</h4>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
              {Object.entries(status).map(([key, value]) => (
                <div key={key}>
                  <div className="text-bambu-gray">{key}</div>
                  <div className="text-white break-all">{renderStatusValue(enums, key, value)}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        <DeviceEvents device={device} enums={enums} />
      </CardContent>
    </Card>
  );
}

export function BMCULinkSettings() {
  const { t } = useTranslation();

  const { data, isLoading } = useQuery({
    queryKey: ['bmcu-link-devices'],
    queryFn: () => bmcuLinkApi.getDevices(),
    refetchInterval: 15000,
  });

  const { data: enums } = useQuery({
    queryKey: ['bmcu-link-enums'],
    queryFn: () => bmcuLinkApi.getEnums(),
    staleTime: 5 * 60 * 1000,
    enabled: data?.enabled === true,
  });

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-8 flex justify-center">
          <Loader2 className="w-6 h-6 animate-spin text-bambu-green" />
        </CardContent>
      </Card>
    );
  }

  if (data && data.enabled === false) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-bambu-gray text-sm">
          {t('settings.bmcuLink.disabled')}
        </CardContent>
      </Card>
    );
  }

  const devices = data?.devices ?? [];

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="py-3 px-4">
          <div className="flex items-start gap-2 text-xs">
            <Info className="w-4 h-4 text-blue-400 flex-shrink-0 mt-0.5" />
            <div className="text-bambu-gray">
              <p className="text-white font-medium mb-1">{t('settings.bmcuLink.infoTitle')}</p>
              <p>{t('settings.bmcuLink.infoBody')}</p>
            </div>
          </div>
        </CardContent>
      </Card>

      {devices.length === 0 ? (
        <Card>
          <CardContent className="py-8 text-center text-bambu-gray text-sm">
            {t('settings.bmcuLink.empty')}
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {devices.map((device) => (
            <DeviceCard key={device.device_id} device={device} enums={enums} />
          ))}
        </div>
      )}
    </div>
  );
}
