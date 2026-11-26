import { Radio } from 'lucide-react';
import { memo } from 'react';
import type { BMCUStreamDiagnostics } from '../../api/bmcuMonitors';
import { Card, CardContent } from '../Card';
import { BMCUHealthBadge } from './MonitorCard';

const ingestCounters = [
  { key: 'datagrams_received', label: 'Datagrams received' },
  { key: 'records_persisted', label: 'Records stored' },
  { key: 'gaps_detected', label: 'Missing sequences detected' },
  { key: 'ingest_overflow', label: 'Queue overflow drops' },
] as const;

export const StreamDiagnostics = memo(function StreamDiagnostics({
  data,
  outdated,
  error,
}: {
  data: BMCUStreamDiagnostics | null | undefined;
  outdated: boolean;
  error: Error | null;
}) {
  const hasCounters = data?.counters.datagrams_received != null;

  return (
    <Card className="h-full">
      <CardContent className="space-y-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-semibold text-white">
            <Radio className="h-4 w-4 text-bambu-gray" />
            UDP stream
          </h2>
          {data && <BMCUHealthBadge health={data.health} outdated={outdated} />}
        </div>
        {error && <p role="status" className="text-sm text-amber-400">UDP diagnostics could not be refreshed.</p>}
        {!error && data === undefined && <p className="text-sm text-bambu-gray">Loading UDP diagnostics…</p>}
        {!error && data === null && <p className="text-sm text-bambu-gray">UDP receiver disabled.</p>}
        {data && (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="text-bambu-gray">Source <span className="ml-1 font-mono text-white">{data.sourceAddress || '—'}</span></span>
              <span className="text-bambu-gray">Queue depth <span className="ml-1 font-mono text-white">{data.queueDepth.toLocaleString()}</span></span>
            </div>
            <div className="border-t border-bambu-dark-tertiary pt-3">
              <p className="mb-2 text-xs text-bambu-gray">Totals since receiver startup</p>
              <dl className="grid grid-cols-2 gap-2">
                {ingestCounters.map(({ key, label }) => (
                  <div key={key} className="rounded-md bg-bambu-dark/55 px-2.5 py-2">
                    <dt className="text-xs text-bambu-gray">{label}</dt>
                    <dd className="mt-1 font-mono text-sm font-semibold tabular-nums text-white">
                      {hasCounters ? (data.counters[key] ?? 0).toLocaleString() : '—'}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
});
