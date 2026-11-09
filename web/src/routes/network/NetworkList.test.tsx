import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { NetworkList } from './NetworkList';
import { BACKEND, urlOf } from '@/test/backend';
import type { NetworkInterfaceList } from '@/api/types';
import { refreshAll } from '@/hooks/useResource';

afterEach(() => vi.unstubAllGlobals());

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason?: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function listWith(interfaces: unknown[], sweepId = 'swp_initial'): NetworkInterfaceList {
  const base = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
  return {
    ...base,
    count: interfaces.length,
    interfaces: interfaces as NetworkInterfaceList['interfaces'],
    last_sweep: {
      ...(base.last_sweep as NonNullable<NetworkInterfaceList['last_sweep']>),
      sweep_id: sweepId,
      capability: 'network.observe',
      status: 'ok',
      issues: [],
      object_count: interfaces.length,
    },
  };
}

function renderNetwork(): void {
  render(
    <MemoryRouter initialEntries={['/network']}>
      <Routes><Route path="network" element={<NetworkList />} /></Routes>
    </MemoryRouter>,
  );
}

function interfaceReads(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
  return fetchMock.mock.calls.filter(([input, init]) =>
    urlOf(input) === '/api/v1/network/interfaces' && (init?.method ?? 'GET') === 'GET');
}

function observationPosts(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
  return fetchMock.mock.calls.filter(([input, init]) =>
    urlOf(input) === '/api/v1/network/observations/refresh' && init?.method === 'POST');
}

const BACKEND_NETWORK = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;

function interfaceTable() {
  return within(screen.getByRole('table', { name: 'Network interfaces' }));
}

describe('explicit network observation', () => {
  it.each(['pending', 'failed'] as const)('reports no retained records with a cold %s first GET and recovers with GET only', async (initial) => {
    const first = deferred<Response>();
    const post = deferred<Response>();
    let reads = 0;
    let recover = false;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/network/observations/refresh') return post.promise;
      if (path === '/api/v1/network/interfaces') {
        reads += 1;
        if (reads === 1 && initial === 'pending') return first.promise;
        return Promise.resolve(recover ? response(listWith([BACKEND_NETWORK.interfaces[0]]))
          : response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503));
      }
      return Promise.resolve(response(BACKEND[path] ?? {}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();
    if (initial === 'failed') await screen.findByText('Stored interface records could not be re-read');
    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(screen.getByText(/No stored records are currently visible/)).toBeInTheDocument();
    expect(screen.queryByText(/prior stored records remain visible|last visible records remain on screen/i)).not.toBeInTheDocument();
    await act(async () => post.resolve(response({ error: { code: 'unavailable', message: 'failed', detail: {} } }, 503)));
    await screen.findByText('Network observation request failed');
    await screen.findByText('Stored interface records could not be re-read');
    expect(screen.queryByText(/prior stored records remain visible|last visible records remain on screen/i)).not.toBeInTheDocument();
    recover = true;
    fireEvent.click(screen.getByRole('button', { name: 'Re-read stored records' }));
    await waitFor(() => expect(interfaceTable().getByRole('link', { name: 'enx020000000012' })).toBeInTheDocument());
    expect(observationPosts(fetchMock)).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') !== 'GET')).toHaveLength(1);
  });

  it('exposes returned provider failure when the stored reread fails and retries only GET', async () => {
    let reads = 0;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/network/observations/refresh') return Promise.resolve(response({
        sweep_id: 'swp_provider', status: 'partial', object_count: 1, observation_count: 1, issues: [],
        providers: [{ provider: 'networkmanager', source: 'networkmanager.devices', status: 'failed', reason: 'dbus_timeout', version: '1' }],
      }));
      if (path === '/api/v1/network/interfaces') {
        reads += 1;
        return Promise.resolve(reads === 2
          ? response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503)
          : response(listWith([BACKEND_NETWORK.interfaces[0]], 'swp_provider')));
      }
      return Promise.resolve(response(BACKEND[path] ?? {}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();
    await screen.findByRole('table', { name: 'Network interfaces' });
    fireEvent.click(screen.getByRole('button', { name: 'Observe network' }));
    await screen.findByText('Network responded, but stored records could not be re-read');
    const details = screen.getByText('Provider results (1)').closest('details') as HTMLElement;
    expect(within(details).getByText('networkmanager')).toBeInTheDocument();
    expect(within(details).getByText('failed')).toBeInTheDocument();
    expect(within(details).getByText(/dbus_timeout/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Re-read stored records' }));
    await screen.findByText('Network observation completed with gaps');
    expect(observationPosts(fetchMock)).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') !== 'GET')).toHaveLength(1);
  });
  it.each([
    ['null issue', { issues: [null] }],
    ['null provider', { issues: [], providers: [null] }],
    ['bad reason', { issues: [], providers: [{ provider: 'network', source: 'netlink', status: 'ok', reason: {} }] }],
    ['bad status', { issues: [], providers: [{ provider: 'network', source: 'netlink', status: null, reason: null }] }],
  ])('keeps stored network data for malformed %s and rereads with GET only', async (_name, fields) => {
    const before = listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before');
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/network/observations/refresh') return Promise.resolve(response({
        host_id: 'host_1', sweep_id: 'swp_new', status: 'ok', object_count: 1, observation_count: 1, missing: [], ...fields,
      }));
      return Promise.resolve(response(path === '/api/v1/network/interfaces' ? before : BACKEND[path] ?? {}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();
    await screen.findByRole('table', { name: 'Network interfaces' });
    fireEvent.click(screen.getByRole('button', { name: 'Observe network' }));
    await screen.findByText('Network observation outcome is uncertain');
    expect(interfaceTable().getByRole('link', { name: 'enx020000000012' })).toBeInTheDocument();
    await waitFor(() => expect(interfaceReads(fetchMock).length).toBeGreaterThan(1));
    expect(observationPosts(fetchMock)).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') !== 'GET')).toHaveLength(1);
  });
  it('shows the observe button before any stored read and issues one POST on click', async () => {
    const before = listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before');
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_observed', status: 'ok', object_count: 1, observation_count: 1, missing: [], issues: [] }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(before));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    const button = await screen.findByRole('button', { name: 'Observe network' });
    fireEvent.click(button);
    fireEvent.click(button);

    await screen.findByText('Network observation completed');
    expect(observationPosts(fetchMock)).toHaveLength(1);
    expect(interfaceReads(fetchMock).length).toBeGreaterThanOrEqual(1);
  });

  it('keeps the observe button accessible on an empty initial list', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_empty', status: 'ok', object_count: 0, observation_count: 0, missing: [], issues: [] }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(listWith([], 'swp_empty')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    const button = await screen.findByRole('button', { name: 'Observe network' });
    fireEvent.click(button);
    await screen.findByText('Network observation completed');
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });

  it('reports provider failure status separately from a lost request', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_failed', status: 'failed', object_count: 0, observation_count: 0, missing: [], issues: [{ source: 'network.observe', code: 'rtnetlink_error', message: 'rtnetlink returned error', detail: {} }] }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(listWith([], 'swp_before')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText('Network observation reported provider failure')).toBeInTheDocument();
    expect(screen.getByText(/rtnetlink returned error/)).toBeInTheDocument();
  });

  it('uses console-wide re-read as GET-only and never starts a provider observation', async () => {
    const before = listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(before)));
    renderNetwork();

    await screen.findByRole('button', { name: 'Observe network' });
    act(() => refreshAll());
    await waitFor(() => expect(interfaceTable().getByRole('link', { name: 'enx020000000012' })).toBeInTheDocument());
    expect(observationPosts(vi.mocked(fetch))).toHaveLength(0);
  });

  it('keeps the observe button accessible when the initial GET fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503)),
    );
    renderNetwork();
    expect(await screen.findByRole('button', { name: 'Observe network' })).toBeInTheDocument();
  });

  it('retains prior records and their age when the POST fails', async () => {
    const before = listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before');
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ error: { code: 'agent_unavailable', message: 'Agent unavailable', detail: {} } }, 503));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(before));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    await screen.findByRole('table', { name: 'Network interfaces' });
    expect(interfaceTable().getByRole('link', { name: 'enx020000000012' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText(/Network observation request failed/)).toBeInTheDocument();
    expect(interfaceTable().getByRole('link', { name: 'enx020000000012' })).toBeInTheDocument();
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });

  it('reports a partial provider response without claiming completion', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_partial', status: 'partial', object_count: 1, observation_count: 1, missing: [], issues: [{ source: 'network.observe', code: 'rtnetlink_error', message: 'rtnetlink returned error', detail: {} }], agent_instance_id: 'agent_1' }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(listWith([BACKEND_NETWORK.interfaces[0]], 'swp_partial')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText('Network observation completed with gaps')).toBeInTheDocument();
    expect(screen.getByText(/rtnetlink returned error/)).toBeInTheDocument();
  });

  it('reports a recognised provider failure separately from a lost request', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_failed', status: 'failed', object_count: 0, observation_count: 0, missing: [], issues: [{ source: 'network.observe', code: 'rtnetlink_error', message: 'rtnetlink returned error', detail: {} }], agent_instance_id: 'agent_1' }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(listWith([], 'swp_before')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText('Network observation reported provider failure')).toBeInTheDocument();
    expect(screen.getByText(/rtnetlink returned error/)).toBeInTheDocument();
  });

  it('treats a transport failure as uncertain and keeps prior records', async () => {
    const before = listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before');
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.reject(new Error('network disconnected'));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(before));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    await screen.findByRole('table', { name: 'Network interfaces' });
    expect(interfaceTable().getByRole('link', { name: 'enx020000000012' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText('Network observation outcome is uncertain')).toBeInTheDocument();
    expect(interfaceTable().getByRole('link', { name: 'enx020000000012' })).toBeInTheDocument();
  });

  it('treats a malformed response as uncertain', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ acknowledged: true }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText('Network observation outcome is uncertain')).toBeInTheDocument();
  });

  it('recovers from a successful POST with a failed stored read by re-reading stored records only', async () => {
    const fresh = listWith([{ ...BACKEND_NETWORK.interfaces[0], name: 'fresh-if' }], 'swp_fresh');
    let getCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_fresh', status: 'ok', object_count: 1, observation_count: 1, missing: [], issues: [], agent_instance_id: 'agent_1' }));
      }
      if (path === '/api/v1/network/interfaces') {
        getCall += 1;
        if (getCall === 2) {
          return Promise.resolve(response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503));
        }
        return Promise.resolve(response(getCall === 1 ? listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before') : fresh));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText(/Network responded, but stored records could not be re-read/)).toBeInTheDocument();
    expect(observationPosts(fetchMock)).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Re-read stored records' }));
    await waitFor(() => expect(interfaceTable().getByRole('link', { name: 'fresh-if' })).toBeInTheDocument());
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });

  it('recovers from a global GET after a successful POST with a failed stored read', async () => {
    const fresh = listWith([{ ...BACKEND_NETWORK.interfaces[0], name: 'global-if' }], 'swp_fresh');
    let getCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_fresh', status: 'ok', object_count: 1, observation_count: 1, missing: [], issues: [], agent_instance_id: 'agent_1' }));
      }
      if (path === '/api/v1/network/interfaces') {
        getCall += 1;
        if (getCall === 2) {
          return Promise.resolve(response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503));
        }
        return Promise.resolve(response(getCall === 1 ? listWith([BACKEND_NETWORK.interfaces[0]], 'swp_before') : fresh));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText(/Network responded, but stored records could not be re-read/)).toBeInTheDocument();

    act(() => refreshAll());
    await waitFor(() => expect(interfaceTable().getByRole('link', { name: 'global-if' })).toBeInTheDocument());
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });

  it('keeps a newer global stored GET when the earlier POST fails late', async () => {
    const initial = listWith([{ ...BACKEND_NETWORK.interfaces[0], name: 'initial-if' }], 'swp_initial');
    const global = listWith([{ ...BACKEND_NETWORK.interfaces[0], name: 'global-if' }], 'swp_global');
    const post = deferred<Response>();
    let listCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return post.promise;
      }
      if (path === '/api/v1/network/interfaces') {
        listCall += 1;
        if (listCall === 1) return Promise.resolve(response(initial));
        return Promise.resolve(response(global));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    await screen.findByRole('table', { name: 'Network interfaces' });
    expect(interfaceTable().getByRole('link', { name: 'initial-if' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Observe network' }));

    act(() => refreshAll());
    await waitFor(() => expect(interfaceTable().getByRole('link', { name: 'global-if' })).toBeInTheDocument());

    await act(async () =>
      post.resolve(response({ error: { code: 'agent_unavailable', message: 'Agent unavailable', detail: {} } }, 503))
    );

    expect(await screen.findByText(/Network observation request failed/)).toBeInTheDocument();
    expect(interfaceTable().getByRole('link', { name: 'global-if' })).toBeInTheDocument();
    expect(interfaceTable().queryByRole('link', { name: 'initial-if' })).not.toBeInTheDocument();
  });

  it('reports a sweep mismatch explicitly', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_returned', status: 'ok', object_count: 1, observation_count: 1, missing: [], issues: [], agent_instance_id: 'agent_1' }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(listWith([BACKEND_NETWORK.interfaces[0]], 'swp_stored')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText('Network observation completed')).toBeInTheDocument();
    expect(screen.getByText(/latest sweep differs from this returned response/i)).toBeInTheDocument();
  });

  it('keeps a newer global GET when an earlier retry fails late, without an unhandled rejection', async () => {
    const reads = [deferred<Response>(), deferred<Response>(), deferred<Response>()];
    let listCall = 0;
    const unhandled = vi.fn();
    window.addEventListener('unhandledrejection', unhandled);
    const fetchMock = vi.fn<typeof fetch>((input) => {
      if (urlOf(input) === '/api/v1/network/interfaces') {
        listCall += 1;
        return reads[listCall - 1]!.promise;
      }
      return Promise.resolve(response(BACKEND[urlOf(input)]));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();
    await act(async () => reads[0]!.resolve(response(listWith([{ ...BACKEND_NETWORK.interfaces[0], name: 'initial-if' }], 'swp_initial'))));
    await screen.findByRole('table', { name: 'Network interfaces' });
    expect(interfaceTable().getByRole('link', { name: 'initial-if' })).toBeInTheDocument();

    act(() => refreshAll());
    await waitFor(() => expect(interfaceReads(fetchMock)).toHaveLength(2));
    act(() => refreshAll());
    await waitFor(() => expect(interfaceReads(fetchMock)).toHaveLength(3));
    await act(async () => reads[2]!.resolve(response(listWith([{ ...BACKEND_NETWORK.interfaces[0], name: 'newer-if' }], 'swp_newer'))));
    await waitFor(() => expect(interfaceTable().getByRole('link', { name: 'newer-if' })).toBeInTheDocument());
    await act(async () => reads[1]!.reject(new Error('late stale read failure')));
    expect(interfaceTable().getByRole('link', { name: 'newer-if' })).toBeInTheDocument();
    expect(observationPosts(fetchMock)).toHaveLength(0);
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener('unhandledrejection', unhandled);
  });

  it('shows provider results when the network observation succeeded but an ownership provider failed', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/network/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({
          host_id: 'host_1',
          sweep_id: 'swp_provider_mixed',
          status: 'ok',
          object_count: 1,
          observation_count: 1,
          missing: [],
          issues: [{ source: 'networkmanager.devices', code: 'dbus_timeout', message: 'NetworkManager did not answer', detail: {} }],
          providers: [
            { provider: 'kernel', source: 'kernel.interface', status: 'ok', reason: null, version: '1' },
            { provider: 'networkmanager', source: 'networkmanager.devices', status: 'failed', reason: 'dbus_timeout', version: '1' },
          ],
        }));
      }
      if (path === '/api/v1/network/interfaces') {
        return Promise.resolve(response(listWith([BACKEND_NETWORK.interfaces[0]], 'swp_provider_mixed')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderNetwork();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe network' }));
    expect(await screen.findByText(/Network observation completed/)).toBeInTheDocument();
    expect(screen.getByText(/NetworkManager did not answer/)).toBeInTheDocument();
    expect(screen.getByText(/2 ownership providers consulted/)).toBeInTheDocument();
    const providerDetails = screen.getByText(/Provider results/).closest('details') as HTMLElement;
    expect(within(providerDetails).getByText('networkmanager')).toBeInTheDocument();
    expect(within(providerDetails).getByText('failed')).toBeInTheDocument();
  });
});
