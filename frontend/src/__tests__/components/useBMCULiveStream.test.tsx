import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useBMCULiveStream } from '../../hooks/useBMCULiveStream';
import { subscribeLoader } from '../../components/bmcu/liveUpdates';
import fixture from '../fixtures/bmcuMonitorApi.json';

vi.mock('../../api/client', () => ({ getAuthToken: () => null }));

describe('BMCU live stream', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it('coalesces a burst of snapshots into one cache update per view', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const setData = vi.spyOn(client, 'setQueryData');
    const deviceId = fixture.detail.deviceId;
    const stream = { deviceId, health: 'online', sourceAddress: '127.0.0.1', queueDepth: 0, counters: {} };
    let source!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(controller) { source = controller; } });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, body }));
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result, unmount } = renderHook(() => useBMCULiveStream(deviceId), { wrapper });
    const payload = JSON.stringify({ detail: fixture.detail, stream });
    source.enqueue(new TextEncoder().encode(Array(20).fill(`event: snapshot\ndata: ${payload}\n\n`).join('')));
    await waitFor(() => expect(result.current).toBe(true));
    await waitFor(() => expect(client.getQueryData(['bmcu-monitor', deviceId])).toBeDefined());
    expect(setData).toHaveBeenCalledTimes(2);
    expect(client.getQueryData(['bmcu-monitor-stream', deviceId])).toEqual(stream);
    unmount();
    source.close();
  });

  it('keeps polling fallback available when the live endpoint is unavailable', async () => {
    const client = new QueryClient();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, body: null }));
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result, unmount } = renderHook(() => useBMCULiveStream('missing'), { wrapper });
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(result.current).toBe(false);
    unmount();
  });

  it('keeps the last displayed loader values when the stream ends before the next cache refresh', async () => {
    const client = new QueryClient();
    const deviceId = fixture.detail.deviceId;
    const stream = { deviceId, health: 'online', sourceAddress: '127.0.0.1', queueDepth: 0, counters: {} };
    let source!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(controller) { source = controller; } });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, body }));
    const seen = vi.fn();
    const unsubscribe = subscribeLoader(deviceId, seen);
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { unmount } = renderHook(() => useBMCULiveStream(deviceId), { wrapper });
    const send = (detail: typeof fixture.detail) => source.enqueue(new TextEncoder().encode(`event: snapshot\ndata: ${JSON.stringify({ detail, stream })}\n\n`));
    send(fixture.detail);
    await waitFor(() => expect(seen).toHaveBeenCalledTimes(1));
    const updated = { ...fixture.detail, links: fixture.detail.links.map((link) => ({ ...link, currentSlot: 2 })) };
    send(updated);
    await waitFor(() => expect(seen).toHaveBeenCalledTimes(2));
    source.close();
    await waitFor(() => expect(client.getQueryData<typeof updated>(['bmcu-monitor', deviceId])?.links[0].currentSlot).toBe(2));
    unsubscribe();
    unmount();
  });

  it('does not apply a queued frame after the tab becomes hidden', async () => {
    const client = new QueryClient();
    const deviceId = fixture.detail.deviceId;
    let visible = true;
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visible ? 'visible' : 'hidden');
    let renderFrame: FrameRequestCallback | undefined;
    vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => { renderFrame = callback; return 1; }));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
    let source!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(controller) { source = controller; } });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, body }));
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result, unmount } = renderHook(() => useBMCULiveStream(deviceId), { wrapper });
    const stream = { deviceId, health: 'online', sourceAddress: '127.0.0.1', queueDepth: 0, counters: {} };
    source.enqueue(new TextEncoder().encode(`event: snapshot\ndata: ${JSON.stringify({ detail: fixture.detail, stream })}\n\n`));
    await waitFor(() => expect(result.current).toBe(true));
    visible = false;
    act(() => { document.dispatchEvent(new Event('visibilitychange')); renderFrame?.(0); });
    expect(client.getQueryData(['bmcu-monitor', deviceId])).toBeUndefined();
    expect(cancelAnimationFrame).toHaveBeenCalled();
    unmount();
    source.close();
  });

  it('preserves the last displayed state and its timestamp when the tab becomes hidden', async () => {
    const client = new QueryClient();
    const deviceId = fixture.detail.deviceId;
    let visible = true;
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visible ? 'visible' : 'hidden');
    let source!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(controller) { source = controller; } });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, body }));
    const seen = vi.fn();
    const unsubscribe = subscribeLoader(deviceId, seen);
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { unmount } = renderHook(() => useBMCULiveStream(deviceId), { wrapper });
    const stream = { deviceId, health: 'online', sourceAddress: '127.0.0.1', queueDepth: 0, counters: {} };
    const send = (detail: typeof fixture.detail) => source.enqueue(new TextEncoder().encode(`event: snapshot\ndata: ${JSON.stringify({ detail, stream })}\n\n`));
    send(fixture.detail);
    await waitFor(() => expect(seen).toHaveBeenCalledTimes(1));
    const updated = { ...fixture.detail, links: fixture.detail.links.map((link) => ({ ...link, currentSlot: 2 })) };
    send(updated);
    await waitFor(() => expect(seen).toHaveBeenCalledTimes(2));
    const displayedAt = Date.now();
    visible = false;
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(client.getQueryData<typeof updated>(['bmcu-monitor', deviceId])?.links[0].currentSlot).toBe(2);
    expect(client.getQueryState(['bmcu-monitor', deviceId])?.dataUpdatedAt).toBeLessThanOrEqual(displayedAt);
    unsubscribe();
    unmount();
    source.close();
  });
});
