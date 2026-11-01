import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { WorkloadList } from './WorkloadList';
import { BACKEND, urlOf } from '@/test/backend';
import type { DockerContainer, DockerContainerList } from '@/api/types';
import { refreshAll } from '@/hooks/useResource';

afterEach(() => vi.unstubAllGlobals());

const before = BACKEND['/api/v1/docker/containers'] as DockerContainerList;
const original = before.containers[0] as DockerContainer;

function listWith(containers: DockerContainer[], sweepId = 'swp_refreshed'): DockerContainerList {
  return {
    ...before,
    count: containers.length,
    containers,
    last_sweep: {
      ...before.last_sweep!,
      sweep_id: sweepId,
      capability: 'docker.containers.observe',
      status: 'ok',
      issues: [],
      completed_at: '2026-11-01T10:00:00Z',
      object_count: containers.length,
    },
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (cause: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((accept, refuse) => { resolve = accept; reject = refuse; });
  return { promise, resolve, reject };
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function renderWorkloads(): void {
  render(
    <MemoryRouter initialEntries={['/workloads']}>
      <Routes><Route path="workloads" element={<WorkloadList />} /></Routes>
    </MemoryRouter>,
  );
}

function installBackend({
  after,
  result = { host_id: 'host_1', sweep_id: 'swp_refreshed', status: 'ok', container_count: 1, issues: [], provider_version: '29.1.3' },
  storedReadFailure = false,
}: {
  after: DockerContainerList;
  result?: Record<string, unknown>;
  storedReadFailure?: boolean;
}): ReturnType<typeof vi.fn<typeof fetch>> {
  let refreshed = false;
  let failOnce = storedReadFailure;
  const fetchMock = vi.fn<typeof fetch>((input, init) => {
    const path = urlOf(input);
    const method = init?.method ?? 'GET';
    if (path === '/api/v1/docker/containers/observations/refresh') {
      refreshed = true;
      return Promise.resolve(response(result));
    }
    if (path === '/api/v1/docker/containers') {
      if (refreshed && failOnce) {
        failOnce = false;
        return Promise.resolve(response({ error: { code: 'store_busy', message: 'stored list unavailable', detail: {} } }, 503));
      }
      return Promise.resolve(response(refreshed ? after : before));
    }
    const body = BACKEND[path];
    if (body !== undefined) return Promise.resolve(response(body));
    return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function inventoryCalls(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
  return fetchMock.mock.calls.filter(([input]) => urlOf(input) === '/api/v1/docker/containers/observations/refresh');
}

function containerReads(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
  return fetchMock.mock.calls.filter(([input, init]) =>
    urlOf(input) === '/api/v1/docker/containers' && (init?.method ?? 'GET') === 'GET');
}

function expectOnlyInventoryPost(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>): void {
  const writes = fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') !== 'GET');
  expect(writes).toHaveLength(1);
  expect(urlOf(writes[0]?.[0] as RequestInfo)).toBe('/api/v1/docker/containers/observations/refresh');
  expect(writes[0]?.[1]?.method).toBe('POST');
  expect(fetchMock.mock.calls.map(([input]) => urlOf(input))).not.toEqual(expect.arrayContaining([
    '/api/v1/docker/containers/obj_ct/logs',
    '/api/v1/docker/containers/obj_ct/stats',
  ]));
}

describe('explicit Docker inventory refresh', () => {
  it('uses one exact POST, guards rapid activation, then uses a pure stored-record GET', async () => {
    const updated: DockerContainer = {
      ...original,
      name: 'newly-observed',
      observed_in_latest_sweep: true,
      runtime: { ...original.runtime, state: 'running', running: true },
    };
    const fetchMock = installBackend({ after: listWith([updated]) });
    renderWorkloads();

    const button = await screen.findByRole('button', { name: 'Refresh Docker inventory' });
    fireEvent.click(button);
    fireEvent.click(button);

    await screen.findByText('Docker inventory completed');
    expect(screen.getByText('newly-observed')).toBeInTheDocument();
    expect(inventoryCalls(fetchMock)).toHaveLength(1);
    expect(inventoryCalls(fetchMock)[0]?.[1]?.method).toBe('POST');

    const postCalls = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(postCalls).toHaveLength(1);
    expect(postCalls[0] && urlOf(postCalls[0][0])).toBe('/api/v1/docker/containers/observations/refresh');
    expectOnlyInventoryPost(fetchMock);
  });

  it('retains old records when the successful observation cannot be followed by a stored GET, then retries GET only', async () => {
    const oldRecord: DockerContainer = { ...original, name: 'retained-before-read-failure', observed_in_latest_sweep: false };
    const freshRecord: DockerContainer = { ...original, name: 'fresh-after-reread', observed_in_latest_sweep: true };
    const fetchMock = installBackend({
      after: listWith([freshRecord]),
      storedReadFailure: true,
      result: {
        host_id: 'host_1', sweep_id: 'swp_refreshed', status: 'partial', container_count: 1,
        issues: [{ source: 'docker.containers', code: 'inspect_failed', message: 'one inspection failed', detail: {} }],
        provider_version: '29.1.3',
      },
    });
    // Both initial list readers share this response; preserve an unmistakable prior row.
    const originalImplementation = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input, init) => {
      if (urlOf(input) === '/api/v1/docker/containers' && inventoryCalls(fetchMock).length === 0) {
        return Promise.resolve(response(listWith([oldRecord], 'swp_before')));
      }
      return originalImplementation(input, init);
    });
    renderWorkloads();

    const button = await screen.findByRole('button', { name: 'Refresh Docker inventory' });
    await waitFor(() => expect(screen.getByText('retained-before-read-failure')).toBeInTheDocument());
    fireEvent.click(button);

    await screen.findByText('Docker responded, but stored records could not be re-read');
    expect(screen.getByText('retained-before-read-failure')).toBeInTheDocument();
    expect(inventoryCalls(fetchMock)).toHaveLength(1);
    expect(screen.getByText('Reported issues (1)')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Re-read stored records' }));
    await screen.findByText('fresh-after-reread');
    expect(inventoryCalls(fetchMock)).toHaveLength(1);
  });

  it.each([
    ['complete zero-container', { host_id: 'host_1', sweep_id: 'swp_refreshed', status: 'ok', container_count: 0, issues: [], provider_version: '29.1.3' }, 'Docker inventory completed'],
    ['partial', { host_id: 'host_1', sweep_id: 'swp_refreshed', status: 'partial', container_count: 0, issues: [{ source: 'docker.containers', code: 'inspect_failed', message: 'one inspection failed', detail: {} }], provider_version: '29.1.3' }, 'Docker inventory completed with gaps'],
    ['unknown status with issues', { host_id: 'host_1', sweep_id: 'swp_refreshed', status: 'provider_new_status', container_count: 0, issues: [{ source: 'docker.containers', code: 'new_status_note', message: 'provider token is newer', detail: {} }], provider_version: '29.1.3' }, 'Docker inventory returned unrecognised status: provider_new_status'],
  ])('presents %s response truthfully without treating HTTP success as complete', async (_name, result, expected) => {
    installBackend({ after: listWith([], 'swp_refreshed'), result });
    renderWorkloads();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh Docker inventory' }));
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  it('keeps stored partial-zero sweep facts separate from fetched time and preserves a returned-sweep mismatch', async () => {
    const result = {
      host_id: 'host_1', sweep_id: 'swp_returned', status: 'partial', container_count: 0,
      issues: [{ source: 'docker.containers', code: 'inspect_failed', message: 'container inspection failed', detail: {} }],
      provider_version: '29.1.3',
    };
    const partial = listWith([], 'swp_stored_other');
    const lastSweep = partial.last_sweep!;
    const after: DockerContainerList = {
      ...partial,
      last_sweep: { ...lastSweep, status: 'partial', object_count: result.container_count, issues: result.issues },
    };
    installBackend({ after, result });
    renderWorkloads();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh Docker inventory' }));
    await screen.findByText('Stored Docker inventory');
    expect(screen.getAllByText('partial', { selector: 'code' })).not.toHaveLength(0);
    expect(screen.getAllByText(/docker 29.1.3/)).not.toHaveLength(0);
    expect(screen.getAllByText(/1 issue/)).not.toHaveLength(0);
    expect(await screen.findByText(/latest sweep differs from this returned response/i)).toBeInTheDocument();
    expect(screen.getByText('Docker inventory completed with gaps')).toBeInTheDocument();
  });

  it('keeps prior records and calls the missing response uncertain rather than empty', async () => {
    const oldRecord: DockerContainer = { ...original, name: 'retained-through-uncertain-response' };
    const fetchMock = installBackend({ after: listWith([]) });
    const originalImplementation = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input, init) => {
      if (urlOf(input) === '/api/v1/docker/containers/observations/refresh') {
        return Promise.reject(new Error('network disconnected'));
      }
      if (urlOf(input) === '/api/v1/docker/containers') return Promise.resolve(response(listWith([oldRecord], 'swp_before')));
      return originalImplementation(input, init);
    });
    renderWorkloads();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh Docker inventory' }));
    expect(await screen.findByText('Docker inventory outcome is uncertain')).toBeInTheDocument();
    expect(screen.getByText('retained-through-uncertain-response')).toBeInTheDocument();
    expect(screen.getByText(/may or may not have been observed/i)).toBeInTheDocument();
  });

  it('labels latest-sighted, retained and unknown records independently', async () => {
    installBackend({
      after: listWith([]),
    });
    // This state is visible before an explicit refresh and must not call Docker by itself.
    const seen = { ...original, name: 'seen', observed_in_latest_sweep: true, runtime: { ...original.runtime, state: 'running' } };
    const retained = { ...original, object_id: 'obj_retained', name: 'retained', observed_in_latest_sweep: false, runtime: { ...original.runtime, state: 'running' } };
    const unknown = { ...original, object_id: 'obj_unknown', name: 'unknown', observed_in_latest_sweep: null, runtime: { ...original.runtime, state: 'exited' } };
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input) => {
      if (urlOf(input) === '/api/v1/docker/containers') return Promise.resolve(response(listWith([seen, retained, unknown], 'swp_before')));
      const body = BACKEND[urlOf(input)];
      return Promise.resolve(response(body));
    });
    renderWorkloads();
    await screen.findByText('seen');
    expect(screen.getAllByText('seen in latest inventory')).not.toHaveLength(0);
    expect(screen.getAllByText('not seen in latest inventory')).not.toHaveLength(0);
    expect(screen.getAllByText('membership unknown')).not.toHaveLength(0);
    expect(screen.getByText((_content, element) => element?.textContent === '1 reported running among 1 latest-sighted')).toBeInTheDocument();
    expect(inventoryCalls(fetchMock)).toHaveLength(0);
  });

  it.each([
    ['created', 'created'],
    ['an unrecognised runtime token', 'provider_new_state'],
    ['an unknown runtime', null],
  ])('does not synthesize an aggregate exited mark for %s latest-sighted runtime', async (_label, state) => {
    const record = {
      ...original,
      observed_in_latest_sweep: true,
      runtime: { ...original.runtime, state },
    } as DockerContainer;
    installBackend({ after: listWith([record]) });
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input) => {
      if (urlOf(input) === '/api/v1/docker/containers') return Promise.resolve(response(listWith([record], 'swp_state')));
      return Promise.resolve(response(BACKEND[urlOf(input)]));
    });
    renderWorkloads();

    await screen.findByText((_content, element) => element?.textContent === '0 reported running among 1 latest-sighted');
    expect(screen.queryByLabelText(/exited — stopped/i)).not.toBeInTheDocument();
    expect(inventoryCalls(fetchMock)).toHaveLength(0);
  });

  it('retains a newer global stored GET when the earlier POST fails late', async () => {
    const initial = deferred<Response>();
    const globalRead = deferred<Response>();
    const post = deferred<Response>();
    let listCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers') return [initial, globalRead][listCall++]!.promise;
      if (path === '/api/v1/docker/containers/observations/refresh') return post.promise;
      return Promise.resolve(response(BACKEND[path]));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderWorkloads();

    await act(async () => initial.resolve(response(listWith([{ ...original, name: 'snapshot-a' }], 'swp_a'))));
    await screen.findByText('snapshot-a');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Docker inventory' }));
    act(() => refreshAll());
    await waitFor(() => expect(containerReads(fetchMock)).toHaveLength(2));
    await act(async () => globalRead.resolve(response(listWith([{ ...original, name: 'newer-global-b' }], 'swp_b'))));
    expect(await screen.findByText('newer-global-b')).toBeInTheDocument();

    await act(async () => post.resolve(response({ error: { code: 'docker_unavailable', message: 'Docker is unavailable', detail: {} } }, 503)));
    expect(await screen.findByText('Docker inventory request failed')).toBeInTheDocument();
    expect(screen.getByText('newer-global-b')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh Docker inventory' })).not.toBeDisabled();
    expect(fetchMock.mock.calls
      .filter(([input]) => ['/api/v1/docker/containers', '/api/v1/docker/containers/observations/refresh'].includes(urlOf(input)))
      .map(([input, init]) => `${init?.method ?? 'GET'} ${urlOf(input)}`))
      .toEqual([
        'GET /api/v1/docker/containers',
        'POST /api/v1/docker/containers/observations/refresh',
        'GET /api/v1/docker/containers',
      ]);
    expectOnlyInventoryPost(fetchMock);
  });

  it.each([
    ['partial', 'partial', 'Docker inventory completed with gaps'],
    ['unknown', 'provider_new_status', 'Docker inventory returned unrecognised status: provider_new_status'],
  ])('settles a %s acknowledged reread failure from a later global GET without another POST', async (_kind, status, title) => {
    let listCall = 0;
    const result = {
      host_id: 'host_1', sweep_id: 'swp_acknowledged', status, container_count: 1,
      issues: [{ source: 'docker.containers', code: 'inspect_failed', message: 'one inspection failed', detail: {} }],
      provider_version: '29.1.3',
    };
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers') {
        listCall += 1;
        if (listCall === 1) return Promise.resolve(response(listWith([{ ...original, name: 'before-ack' }], 'swp_before')));
        if (listCall === 2) return Promise.resolve(response({ error: { code: 'store_busy', message: 'stored list unavailable', detail: {} } }, 503));
        return Promise.resolve(response(listWith([{ ...original, name: 'global-recovery' }], 'swp_acknowledged')));
      }
      if (path === '/api/v1/docker/containers/observations/refresh') return Promise.resolve(response(result));
      return Promise.resolve(response(BACKEND[path]));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderWorkloads();

    fireEvent.click(await screen.findByRole('button', { name: 'Refresh Docker inventory' }));
    await screen.findByText('Docker responded, but stored records could not be re-read');
    act(() => refreshAll());
    expect(await screen.findByText('global-recovery')).toBeInTheDocument();
    expect(screen.getByText(title)).toBeInTheDocument();
    expect(screen.queryByText('Docker responded, but stored records could not be re-read')).not.toBeInTheDocument();
    expect(screen.getAllByText(status, { selector: 'code' })).not.toHaveLength(0);
    expect(inventoryCalls(fetchMock)).toHaveLength(1);
    expect(containerReads(fetchMock)).toHaveLength(3);
    expectOnlyInventoryPost(fetchMock);
  });

  it('updates the Workloads scope count from the stored list re-read', async () => {
    const secondGroup: DockerContainer = {
      ...original,
      object_id: 'obj_second_group',
      name: 'second-group',
      labels: {},
      observed_in_latest_sweep: true,
    };
    const fetchMock = installBackend({
      after: listWith([original, secondGroup]),
      result: { host_id: 'host_1', sweep_id: 'swp_refreshed', status: 'ok', container_count: 2, issues: [], provider_version: '29.1.3' },
    });
    renderWorkloads();

    expect(await screen.findByRole('link', { name: /Container groups\s*1/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Docker inventory' }));
    expect((await screen.findAllByText('second-group')).length).toBeGreaterThan(0);
    expect(screen.getByRole('link', { name: /Container groups\s*2/ })).toBeInTheDocument();
    expect(containerReads(fetchMock)).toHaveLength(2);
    expectOnlyInventoryPost(fetchMock);
  });

  it('uses a console-wide re-read as GET-only while an explicit POST is pending', async () => {
    const initial = deferred<Response>();
    const globalRead = deferred<Response>();
    const observed = deferred<Response>();
    const reread = deferred<Response>();
    let listCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers') {
        listCall += 1;
        return [initial, globalRead, reread][listCall - 1]!.promise;
      }
      if (path === '/api/v1/docker/containers/observations/refresh') return observed.promise;
      return Promise.resolve(response(BACKEND[path]));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderWorkloads();

    await act(async () => initial.resolve(response(listWith([original], 'swp_before'))));
    const button = await screen.findByRole('button', { name: 'Refresh Docker inventory' });
    act(() => refreshAll());
    await waitFor(() => expect(containerReads(fetchMock)).toHaveLength(2));
    expect(inventoryCalls(fetchMock)).toHaveLength(0);

    fireEvent.click(button);
    expect(inventoryCalls(fetchMock)).toHaveLength(1);
    await act(async () => observed.resolve(response({ host_id: 'host_1', sweep_id: 'swp_refreshed', status: 'ok', container_count: 1, issues: [], provider_version: '29.1.3' })));
    await waitFor(() => expect(containerReads(fetchMock)).toHaveLength(3));
    await act(async () => {
      // A browser may settle the aborted global GET; it must not replace the later POST flow.
      globalRead.resolve(response(listWith([{ ...original, name: 'late-global-row' }], 'swp_global')));
      reread.resolve(response(listWith([{ ...original, name: 'post-row' }])));
    });
    expect(await screen.findByText('post-row')).toBeInTheDocument();
    expect(screen.queryByText('late-global-row')).not.toBeInTheDocument();
    expectOnlyInventoryPost(fetchMock);
  });

  it('keeps a newer global GET when an earlier retry fails late, without an unhandled rejection', async () => {
    const reads = [deferred<Response>(), deferred<Response>(), deferred<Response>()];
    let listCall = 0;
    const unhandled = vi.fn();
    window.addEventListener('unhandledrejection', unhandled);
    const fetchMock = vi.fn<typeof fetch>((input) => {
      if (urlOf(input) === '/api/v1/docker/containers') {
        listCall += 1;
        return reads[listCall - 1]!.promise;
      }
      return Promise.resolve(response(BACKEND[urlOf(input)]));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderWorkloads();
    await act(async () => reads[0]!.resolve(response(listWith([{ ...original, name: 'initial-row' }], 'swp_initial'))));
    await screen.findByText('initial-row');

    act(() => refreshAll());
    await waitFor(() => expect(containerReads(fetchMock)).toHaveLength(2));
    act(() => refreshAll());
    await waitFor(() => expect(containerReads(fetchMock)).toHaveLength(3));
    await act(async () => reads[2]!.resolve(response(listWith([{ ...original, name: 'newer-row' }], 'swp_newer'))));
    expect(await screen.findByText('newer-row')).toBeInTheDocument();
    await act(async () => reads[1]!.reject(new Error('late stale read failure')));
    expect(screen.getByText('newer-row')).toBeInTheDocument();
    expect(inventoryCalls(fetchMock)).toHaveLength(0);
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener('unhandledrejection', unhandled);
  });

  it('ignores a late initial GET after unmount without an unhandled rejection', async () => {
    const initial = deferred<Response>();
    const unhandled = vi.fn();
    window.addEventListener('unhandledrejection', unhandled);
    vi.stubGlobal('fetch', vi.fn<typeof fetch>((input) =>
      urlOf(input) === '/api/v1/docker/containers'
        ? initial.promise
        : Promise.resolve(response(BACKEND[urlOf(input)]))));
    const view = render(
      <MemoryRouter initialEntries={['/workloads']}>
        <Routes><Route path="workloads" element={<WorkloadList />} /></Routes>
      </MemoryRouter>,
    );
    view.unmount();
    await act(async () => initial.reject(new Error('late initial response failure')));
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener('unhandledrejection', unhandled);
  });

  it('shows a known unavailable backend response and a malformed response without claiming no refresh occurred', async () => {
    const responses = [
      response({ error: { code: 'docker_unavailable', message: 'Docker socket is unavailable', detail: { reason: 'daemon_down' } } }, 503),
      response({ acknowledged: true }),
    ];
    let responseIndex = 0;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers/observations/refresh') return Promise.resolve(responses[responseIndex++]!);
      if (path === '/api/v1/docker/containers') return Promise.resolve(response(listWith([original], 'swp_before')));
      return Promise.resolve(response(BACKEND[path]));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderWorkloads();
    const button = await screen.findByRole('button', { name: 'Refresh Docker inventory' });
    fireEvent.click(button);
    expect(await screen.findByText('Docker inventory request failed')).toBeInTheDocument();
    expect(screen.getByText(/Docker socket is unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/daemon_down/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry inventory' }));
    expect(await screen.findByText('Docker inventory outcome is uncertain')).toBeInTheDocument();
    expect(screen.queryByText('Docker inventory was not refreshed')).not.toBeInTheDocument();
    expect(inventoryCalls(fetchMock)).toHaveLength(2);
  });
});
