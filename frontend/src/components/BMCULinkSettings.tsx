import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Loader2, Info, Clock, AlertTriangle, Copy, Check, Cable } from 'lucide-react';
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

function CopyableUrl({ label, url }: { label: string; url: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="text-bambu-gray w-16 flex-shrink-0">{label}</span>
      <code className="text-white font-mono text-xs bg-bambu-dark-tertiary rounded px-2 py-1 truncate flex-1">
        {url}
      </code>
      <button
        type="button"
        className="text-bambu-gray hover:text-white flex-shrink-0"
        aria-label={`copy ${label}`}
        onClick={() => {
          navigator.clipboard.writeText(url).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        }}
      >
        {copied ? <Check className="w-4 h-4 text-green-400" /> : <Copy className="w-4 h-4" />}
      </button>
    </div>
  );
}

/** Endpoint URLs the Pi/Pico bridge must be pointed at, built server-side
 * from LAN addresses (the browser's own URL may be a Tailscale address the
 * bridge cannot reach). */
function ConnectionInfoPanel() {
  const { t } = useTranslation();
  const { data } = useQuery({
    queryKey: ['bmcu-link-connection-info'],
    queryFn: () => bmcuLinkApi.getConnectionInfo(),
    staleTime: 5 * 60 * 1000,
  });

  if (!data) return null;

  return (
    <Card>
      <CardContent className="py-3 px-4 space-y-2">
        <div className="flex items-center gap-2 text-xs">
          <Cable className="w-4 h-4 text-bambu-green flex-shrink-0" />
          <p className="text-white font-medium">{t('settings.bmcuLink.connectionTitle')}</p>
        </div>
        <p className="text-xs text-bambu-gray">{t('settings.bmcuLink.connectionBody')}</p>
        {data.endpoints.length === 0 ? (
          <p className="text-xs text-yellow-400">{t('settings.bmcuLink.connectionNoLan')}</p>
        ) : (
          <div className="space-y-2 text-xs">
            {data.endpoints.map((ep) => (
              <div key={ep.ip} className="space-y-1">
                <CopyableUrl label="WebSocket" url={ep.ws_url} />
                <CopyableUrl label="HTTP" url={ep.ingest_url} />
              </div>
            ))}
          </div>
        )}
        <p className="text-xs text-bambu-gray">
          {data.auth_enabled
            ? t('settings.bmcuLink.connectionAuthOn')
            : t('settings.bmcuLink.connectionAuthOff')}
        </p>
      </CardContent>
    </Card>
  );
}

/** last_status arrives as a JSON string from the REST API but as an object
 * over the WS push; normalize both (a bare string previously fell into
 * Object.entries and rendered one character per cell). */
function parseStatus(status: unknown): Record<string, unknown> | null {
  if (typeof status === 'string') {
    try {
      const parsed = JSON.parse(status);
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch {
      return null;
    }
  }
  if (status && typeof status === 'object') return status as Record<string, unknown>;
  return null;
}

function bit(mask: unknown, i: number): boolean | null {
  return typeof mask === 'number' ? (mask & (1 << i)) !== 0 : null;
}

const ERROR_COUNTER_KEYS = new Set(['crc_error', 'frame_error', 'rx_drop', 'tx_drop', 'control_error']);
// Fields folded into the per-channel grid; everything else renders as a tile.
const CHANNEL_KEYS = new Set(['inserted_mask', 'online_mask', 'pull_pct', 'motion', 'current_slot']);

function PresenceDot({ on }: { on: boolean | null }) {
  if (on === null) return <span className="text-bambu-gray">—</span>;
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full ${on ? 'bg-green-400' : 'bg-gray-600'}`}
      aria-label={on ? 'yes' : 'no'}
    />
  );
}

function StatusGrid({ status, enums }: { status: unknown; enums: BMCULinkEnums | undefined }) {
  const { t } = useTranslation();
  const parsed = parseStatus(status);
  if (!parsed) {
    return (
      <pre className="text-xs text-bambu-gray font-mono whitespace-pre-wrap break-all">
        {typeof status === 'string' ? status : JSON.stringify(status)}
      </pre>
    );
  }
  // Envelope shape is {hw_tick64, data:{...}}; flat objects are used as-is.
  const inner = parseStatus(parsed.data);
  const data: Record<string, unknown> = inner ?? parsed;
  const topLevelExtras = inner
    ? Object.entries(parsed).filter(([k]) => k !== 'data')
    : [];

  const pullPct = Array.isArray(data.pull_pct) ? (data.pull_pct as unknown[]) : null;
  const motion = Array.isArray(data.motion) ? (data.motion as unknown[]) : null;
  const channelCount = Math.max(pullPct?.length ?? 0, motion?.length ?? 0, 4);
  const currentSlot = typeof data.current_slot === 'number' ? data.current_slot : null;
  const channels = Array.from({ length: channelCount }, (_, i) => i);
  const hasChannelData =
    pullPct !== null || motion !== null || typeof data.inserted_mask === 'number' || typeof data.online_mask === 'number';

  const tiles = [
    ...Object.entries(data).filter(([k]) => !CHANNEL_KEYS.has(k)),
    ...topLevelExtras,
  ];

  return (
    <div className="space-y-3">
      {hasChannelData && (
        <div className="overflow-x-auto">
          <table className="text-xs border-collapse">
            <thead>
              <tr>
                <th className="py-1 px-2 text-left font-medium text-bambu-gray border border-bambu-dark-tertiary">
                  {t('settings.bmcuLink.statusChannel')}
                </th>
                {channels.map((i) => (
                  <th
                    key={i}
                    className={`py-1 px-3 text-center font-medium border border-bambu-dark-tertiary ${
                      i === currentSlot ? 'bg-bambu-green/20 text-bambu-green' : 'text-white'
                    }`}
                  >
                    {i + 1}
                    {i === currentSlot && (
                      <span className="block text-[10px] font-normal">{t('settings.bmcuLink.statusCurrent')}</span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="py-1 px-2 text-bambu-gray border border-bambu-dark-tertiary whitespace-nowrap">
                  {t('settings.bmcuLink.statusInserted')}
                </td>
                {channels.map((i) => (
                  <td key={i} className="py-1 px-3 text-center border border-bambu-dark-tertiary">
                    <PresenceDot on={bit(data.inserted_mask, i)} />
                  </td>
                ))}
              </tr>
              <tr>
                <td className="py-1 px-2 text-bambu-gray border border-bambu-dark-tertiary whitespace-nowrap">
                  {t('settings.bmcuLink.statusOnline')}
                </td>
                {channels.map((i) => (
                  <td key={i} className="py-1 px-3 text-center border border-bambu-dark-tertiary">
                    <PresenceDot on={bit(data.online_mask, i)} />
                  </td>
                ))}
              </tr>
              {pullPct && (
                <tr>
                  <td className="py-1 px-2 text-bambu-gray border border-bambu-dark-tertiary whitespace-nowrap">
                    {t('settings.bmcuLink.statusPull')}
                  </td>
                  {channels.map((i) => {
                    const v = typeof pullPct[i] === 'number' ? (pullPct[i] as number) : null;
                    return (
                      <td key={i} className="py-1 px-3 text-center border border-bambu-dark-tertiary">
                        {v === null ? (
                          <span className="text-bambu-gray">—</span>
                        ) : (
                          <div className="min-w-[3rem]">
                            <div className="text-white">{v}%</div>
                            <div className="h-1 bg-bambu-dark-tertiary rounded mt-0.5">
                              <div
                                className="h-1 bg-bambu-green rounded"
                                style={{ width: `${Math.max(0, Math.min(100, v))}%` }}
                              />
                            </div>
                          </div>
                        )}
                      </td>
                    );
                  })}
                </tr>
              )}
              {motion && (
                <tr>
                  <td className="py-1 px-2 text-bambu-gray border border-bambu-dark-tertiary whitespace-nowrap">
                    {t('settings.bmcuLink.statusMotion')}
                  </td>
                  {channels.map((i) => (
                    <td key={i} className="py-1 px-3 text-center border border-bambu-dark-tertiary text-white">
                      {typeof motion[i] === 'number'
                        ? renderStatusValue(enums, 'motion', motion[i])
                        : '—'}
                    </td>
                  ))}
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {tiles.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
          {tiles.map(([key, value]) => {
            const isErrorCounter = ERROR_COUNTER_KEYS.has(key) && typeof value === 'number' && value > 0;
            return (
              <div key={key} className="bg-bambu-dark-tertiary/50 rounded px-2 py-1">
                <div className="text-bambu-gray">{key}</div>
                <div className={`break-all ${isErrorCounter ? 'text-red-400' : 'text-white'}`}>
                  {renderStatusValue(enums, key, value)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
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
            <StatusGrid status={status} enums={enums} />
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

      <ConnectionInfoPanel />

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
