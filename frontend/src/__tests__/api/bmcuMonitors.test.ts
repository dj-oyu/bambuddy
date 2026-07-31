import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { bmcuMonitorsApi } from '../../api/bmcuMonitors';
import { server } from '../mocks/server';
import fixture from '../fixtures/bmcuMonitorApi.json';

describe('BMCU Monitor API contract', () => {
  it('consumes the backend camelCase list and detail unchanged', async () => {
    server.use(
      http.get('*/api/v1/bmcu-monitors', () => HttpResponse.json(fixture.list)),
      http.get('*/api/v1/bmcu-monitors/pico-01', () => HttpResponse.json(fixture.detail)),
    );
    await expect(bmcuMonitorsApi.list()).resolves.toEqual(fixture.list);
    const detail = await bmcuMonitorsApi.get('pico-01');
    expect(detail.links[0]).toMatchObject({ currentSlot: 1, pullPercent: 42, pressure: 17 });
  });

  it('consumes the timeline wrapper with unique IDs and nullable slots', async () => {
    server.use(http.get('*/api/v1/bmcu-monitors/pico-01/timeline', () => HttpResponse.json(fixture.timeline)));
    const timeline = await bmcuMonitorsApi.timeline('pico-01', { from: fixture.timeline.from, to: fixture.timeline.to });
    expect(new Set(timeline.points.map((point) => point.id)).size).toBe(timeline.points.length);
    expect(timeline.points[0]).toMatchObject({ slot: 1, anomaly: false });
    expect(timeline.points[1]).toMatchObject({ slot: null, anomaly: true, severity: 'critical' });
  });

  it('consumes decoded metric fields without decoding TLVs in the browser', async () => {
    server.use(http.get('*/api/v1/bmcu-monitors/pico-01/metrics', () => HttpResponse.json(fixture.metrics)));
    const metrics = await bmcuMonitorsApi.metrics('pico-01', { from: fixture.timeline.from, to: fixture.timeline.to });
    expect(metrics[0]).toMatchObject({ temperatureC: 42.125, loopGapP99Us: 900, transportSendMaxUs: 2000, uartOverflowTotal: 3 });
  });
});
