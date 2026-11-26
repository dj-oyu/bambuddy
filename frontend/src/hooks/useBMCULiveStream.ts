import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { getAuthToken } from '../api/client';
import { publishLoader } from '../components/bmcu/liveUpdates';
import type { BMCUMonitorDetail, BMCUStreamDiagnostics } from '../api/bmcuMonitors';

type Snapshot = { detail: BMCUMonitorDetail; stream: BMCUStreamDiagnostics };
const isDocumentHidden = () => document.visibilityState === 'hidden';

/** Stream heartbeat health and committed loader values while visible. The server
 * coalesces bursts; the browser coalesces rendering and reconnects with a
 * bounded delay. Polling remains available while the stream is disconnected. */
export function useBMCULiveStream(deviceId: string | undefined): boolean {
  const queryClient = useQueryClient();
  const [connectedId, setConnectedId] = useState<string | null>(null);

  useEffect(() => {
    if (!deviceId) return;
    let stopped = false;
    let controller: AbortController | null = null;
    let retry: number | undefined;
    let animation: number | undefined;
    let latest: { snapshot: Snapshot; receivedAt: number } | undefined;
    let pendingStream: BMCUStreamDiagnostics | undefined;
    let lastPublishedDetail: BMCUMonitorDetail | undefined;
    let lastPublishedAt = 0;
    let streamTimer: number | undefined;
    let lastDetailAt = 0;
    let lastStreamAt = 0;
    let lastMaterial = '';
    let lastStreamHealth: BMCUStreamDiagnostics['health'] | undefined;
    let retryDelay = 3000;

    const discardPending = () => {
      latest = undefined;
      pendingStream = undefined;
      if (animation !== undefined) cancelAnimationFrame(animation);
      if (streamTimer !== undefined) window.clearTimeout(streamTimer);
      animation = undefined;
      streamTimer = undefined;
    };

    const preserveDisplayed = () => {
      if (lastPublishedDetail && (queryClient.getQueryState(['bmcu-monitor', deviceId])?.dataUpdatedAt ?? 0) <= lastPublishedAt) {
        queryClient.setQueryData(['bmcu-monitor', deviceId], lastPublishedDetail, { updatedAt: lastPublishedAt });
      }
      lastPublishedDetail = undefined;
    };

    const flushStream = () => {
      streamTimer = undefined;
      if (!pendingStream || stopped) return;
      queryClient.setQueryData(['bmcu-monitor-stream', deviceId], pendingStream);
      lastStreamHealth = pendingStream.health;
      lastStreamAt = Date.now();
      pendingStream = undefined;
    };

    const flush = () => {
      animation = undefined;
      const pending = latest;
      latest = undefined;
      if (!pending || stopped) return;
      const now = Date.now();
      const delaySeconds = Math.max(0, now - pending.receivedAt) / 1000;
      const snapshot: Snapshot = {
        ...pending.snapshot,
        detail: {
          ...pending.snapshot.detail,
          links: pending.snapshot.detail.links.map((link) => ({
            ...link, statusAgeS: link.statusAgeS == null ? null : link.statusAgeS + delaySeconds,
          })),
        },
      };
      publishLoader(snapshot.detail);
      lastPublishedDetail = snapshot.detail;
      lastPublishedAt = now;
      // Receipt timestamps change for every STATUS. Compare actual state first
      // so high-rate unchanged samples do not force unnecessary React work.
      const material = JSON.stringify({
        health: snapshot.detail.health,
        boot: snapshot.detail.bootId,
        firmware: snapshot.detail.firmware,
        online: snapshot.detail.onlineLinks,
        links: snapshot.detail.links.map((link) => [
          link.linkIndex, link.linkId, link.state,
        ]),
      });
      if (material !== lastMaterial || now - lastDetailAt >= 1000) {
        queryClient.setQueryData(['bmcu-monitor', deviceId], snapshot.detail);
        lastMaterial = material;
        lastDetailAt = now;
      }
      pendingStream = snapshot.stream;
      if (snapshot.stream.health !== lastStreamHealth || now - lastStreamAt >= 1000) {
        if (streamTimer !== undefined) window.clearTimeout(streamTimer);
        flushStream();
      } else if (streamTimer === undefined) {
        streamTimer = window.setTimeout(flushStream, 1000 - (now - lastStreamAt));
      }
    };

    const connect = async () => {
      if (stopped || controller || document.visibilityState === 'hidden') return;
      const activeController = new AbortController();
      controller = activeController;
      lastPublishedDetail = undefined;
      const token = getAuthToken();
      let opened = false;
      let lastBytesAt = Date.now();
      const watchdog = window.setInterval(() => {
        if (Date.now() - lastBytesAt > 20000) activeController.abort();
      }, 5000);
      try {
        const response = await fetch(`/api/v1/bmcu-monitors/${encodeURIComponent(deviceId)}/live`, {
          signal: activeController.signal,
          cache: 'no-store',
          credentials: 'include',
          headers: { Accept: 'text/event-stream', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        });
        if (!response.ok || !response.body) throw new Error(`Live stream HTTP ${response.status}`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done || stopped || controller !== activeController) break;
          lastBytesAt = Date.now();
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
          if (buffer.length > 262144) throw new Error('Live stream event too large');
          let end: number;
          while ((end = buffer.indexOf('\n\n')) >= 0) {
            const event = buffer.slice(0, end);
            buffer = buffer.slice(end + 2);
            const line = event.split('\n').find((part) => part.startsWith('data: '));
            if (!line) continue;
            const snapshot = JSON.parse(line.slice(6)) as Snapshot;
            if (snapshot.detail?.deviceId !== deviceId || snapshot.stream?.deviceId !== deviceId || !Array.isArray(snapshot.detail.links)) continue;
            if (!opened) {
              opened = true;
              retryDelay = 3000;
              void queryClient.cancelQueries({ queryKey: ['bmcu-monitor', deviceId], exact: true });
              void queryClient.cancelQueries({ queryKey: ['bmcu-monitor-stream', deviceId], exact: true });
              setConnectedId(deviceId);
            }
            latest = { snapshot, receivedAt: Date.now() };
            if (animation === undefined) animation = requestAnimationFrame(flush);
          }
        }
      } catch {
        // Fallback queries provide current state while the stream reconnects.
      } finally {
        window.clearInterval(watchdog);
        const isCurrent = controller === activeController;
        if (isCurrent) controller = null;
        if (!stopped && isCurrent) {
          if (!isDocumentHidden()) {
            preserveDisplayed();
            flushStream();
          }
          discardPending();
          setConnectedId(null);
          if (!isDocumentHidden()) {
            retry = window.setTimeout(connect, opened ? 250 : retryDelay);
            if (!opened) retryDelay = Math.min(retryDelay * 2, 30000);
          }
        }
      }
    };

    const visibility = () => {
      if (document.visibilityState === 'hidden') {
        preserveDisplayed();
        controller?.abort();
        controller = null;
        discardPending();
        if (retry !== undefined) window.clearTimeout(retry);
        setConnectedId(null);
      } else {
        if (retry !== undefined) window.clearTimeout(retry);
        void connect();
      }
    };
    document.addEventListener('visibilitychange', visibility);
    void connect();
    return () => {
      stopped = true;
      controller?.abort();
      if (retry !== undefined) window.clearTimeout(retry);
      discardPending();
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [deviceId, queryClient]);

  return connectedId === deviceId && !!deviceId;
}
