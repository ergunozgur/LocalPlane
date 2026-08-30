/**
 * Error normalisation at the request boundary.
 *
 * The property under test is the one the whole console depends on: a transport failure and a
 * domain state must never become the same thing.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request } from './client';

function respond(body: unknown, init: { status?: number; text?: string } = {}): void {
  const status = init.status ?? 200;
  const text = init.text ?? JSON.stringify(body);
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(text, { status, headers: { 'content-type': 'application/json' } })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('successful reads', () => {
  it('returns the parsed body', async () => {
    respond({ status: 'ok', version: '0.1.0' });
    await expect(request<{ status: string }>('/status')).resolves.toEqual({
      status: 'ok',
      version: '0.1.0',
    });
  });

  it('treats a 200 carrying an unknown domain state as a success, not an error', async () => {
    // This is the distinction the client exists to preserve.
    respond({ status: 'unknown', reason: 'transport_peer_local' });
    const body = await request<{ status: string }>('/network/interfaces/x/protection');
    expect(body.status).toBe('unknown');
  });

  it('builds a query string, omitting undefined parameters', async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response('{}', { status: 200 })),
    );
    vi.stubGlobal('fetch', fetchMock);
    await request('/runs', { query: { limit: 10, state: undefined } });
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/runs?limit=10');
  });
});

describe('backend errors', () => {
  it('reads the structured envelope and keeps the stable code', async () => {
    respond({ error: { code: 'object_not_found', message: 'no such object', detail: { id: 'x' } } }, { status: 404 });
    const error = await request('/network/interfaces/x').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const api = error as ApiError;
    expect(api.kind).toBe('backend');
    expect(api.code).toBe('object_not_found');
    expect(api.status).toBe(404);
    expect(api.notFound).toBe(true);
    expect(api.detail).toEqual({ id: 'x' });
  });

  it('synthesises a code when the body is not an envelope', async () => {
    respond(null, { status: 500, text: 'internal server error' });
    const api = (await request('/status').catch((e: unknown) => e)) as ApiError;
    expect(api.kind).toBe('backend');
    expect(api.code).toBe('http_500');
    expect(api.retryable).toBe(true);
  });

  it('does not mark a client error retryable', async () => {
    respond({ error: { code: 'execution_not_implemented', message: 'no executor' } }, { status: 409 });
    const api = (await request('/runs/x/apply', { method: 'POST' }).catch((e: unknown) => e)) as ApiError;
    expect(api.retryable).toBe(false);
    expect(api.code).toBe('execution_not_implemented');
  });
});

describe('transport failures', () => {
  it('reports an unreachable backend distinctly', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    const api = (await request('/status').catch((e: unknown) => e)) as ApiError;
    expect(api.kind).toBe('unreachable');
    expect(api.retryable).toBe(true);
    expect(api.status).toBeNull();
  });

  it('reports a caller cancellation as cancelled, not as a failure to show', async () => {
    const controller = new AbortController();
    controller.abort();
    vi.stubGlobal('fetch', vi.fn(async () => { throw new DOMException('aborted', 'AbortError'); }));
    const api = (await request('/status', { signal: controller.signal }).catch((e: unknown) => e)) as ApiError;
    expect(api.kind).toBe('cancelled');
  });

  it('reports an unreadable body as malformed rather than as empty data', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>gateway</html>', { status: 200 })));
    const api = (await request('/status').catch((e: unknown) => e)) as ApiError;
    expect(api.kind).toBe('malformed');
  });
});
