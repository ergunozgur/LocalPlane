import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { WorkloadDetail, ResourceUsagePanel } from './WorkloadDetail';
import { BACKEND, urlOf } from '@/test/backend';
import { refreshAll } from '@/hooks/useResource';

afterEach(() => vi.unstubAllGlobals());

function statsResponse(id: string): Response {
  return new Response(
    JSON.stringify({
      object_id: id,
      sampled_at: '2026-08-27T21:47:01Z',
      cpu_percent: 12.34,
      memory_usage_bytes: '1048576',
      memory_limit_bytes: '2097152',
      memory_percent: 50,
      network_rx_bytes: '1024',
      network_tx_bytes: '2048',
      block_read_bytes: '512',
      block_write_bytes: '256',
      pids: 12,
      pids_limit: 100,
      online_cpus: 4,
      gaps: [],
    }),
    { status: 200 },
  );
}

function statsCalls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .filter((call) => {
      const [input, init] = call as [RequestInfo | URL, RequestInit | undefined];
      const path = urlOf(input);
      return path.startsWith('/api/v1/docker/containers/') && path.endsWith('/stats') && init?.method === 'POST';
    })
    .map((call) => urlOf((call as [RequestInfo | URL])[0]));
}

function renderPanel(id: string): ReturnType<typeof render> {
  return render(<ResourceUsagePanel objectId={id} />);
}

function renderAt(path: string, fetchMock: typeof fetch): void {
  vi.stubGlobal('fetch', fetchMock);
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/workloads/:objectId/*" element={<WorkloadDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('WorkloadDetail resource usage', () => {
  it('does not sample usage on the Resources tab until the operator asks', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers/obj_ct/stats' && init?.method === 'POST') {
        return Promise.resolve(statsResponse('obj_ct'));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
      return Promise.resolve(new Response(JSON.stringify({ error: { code: 'not_found', message: 'missing', detail: {} } }), { status: 404 }));
    });
    renderAt('/workloads/obj_ct?tab=resources', fetchMock);

    expect(await screen.findByRole('heading', { name: 'Resource usage' })).toBeInTheDocument();
    expect(statsCalls(fetchMock)).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: /Sample usage/i }));
    expect(await screen.findByText(/12.34%/)).toBeInTheDocument();
    expect(statsCalls(fetchMock)).toHaveLength(1);
  });

  it('does not resample usage on global refreshAll', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers/obj_ct/stats' && init?.method === 'POST') {
        return Promise.resolve(statsResponse('obj_ct'));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
      return Promise.resolve(new Response(JSON.stringify({ error: { code: 'not_found', message: 'missing', detail: {} } }), { status: 404 }));
    });
    renderAt('/workloads/obj_ct?tab=resources', fetchMock);

    await screen.findByRole('heading', { name: 'Resource usage' });
    fireEvent.click(screen.getByRole('button', { name: /Sample usage/i }));
    await screen.findByText(/12.34%/);
    expect(statsCalls(fetchMock)).toHaveLength(1);

    act(() => refreshAll());
    await waitFor(() => expect(statsCalls(fetchMock)).toHaveLength(1));
  });

  it('does not carry an existing sample across object identity', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers/obj_a/stats' && init?.method === 'POST') {
        return Promise.resolve(statsResponse('obj_a'));
      }
      if (path === '/api/v1/docker/containers/obj_b/stats' && init?.method === 'POST') {
        return Promise.resolve(statsResponse('obj_b'));
      }
      return Promise.resolve(new Response(JSON.stringify({}), { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { rerender } = renderPanel('obj_a');

    fireEvent.click(screen.getByRole('button', { name: /Sample usage/i }));
    expect(await screen.findByText(/12.34%/)).toBeInTheDocument();
    expect(statsCalls(fetchMock)).toHaveLength(1);

    rerender(<ResourceUsagePanel key="obj_b" objectId="obj_b" />);
    expect(screen.queryByText(/12.34%/)).not.toBeInTheDocument();
    expect(statsCalls(fetchMock)).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /Sample usage/i }));
    expect(await screen.findByText(/12.34%/)).toBeInTheDocument();
    expect(statsCalls(fetchMock)).toHaveLength(2);
  });
});
