import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ContainerLogPanel } from './ContainerLogPanel';
import { refreshAll } from '@/hooks/useResource';
import { urlOf } from '@/test/backend';

afterEach(() => vi.unstubAllGlobals());

function logsResponse(message: string): Response {
  return new Response(
    JSON.stringify({
      object_id: 'obj_ct',
      line_count: 1,
      requested_lines: 50,
      read_at: '2026-08-27T21:47:01Z',
      truncated: false,
      line_limit: 100,
      byte_limit: 262144,
      lines: [{ timestamp: '2026-08-27T21:47:00Z', stream: 'stdout', message }],
    }),
    { status: 200 },
  );
}

function logCalls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .filter((call) => {
      const [input, init] = call as [RequestInfo | URL, RequestInit | undefined];
      const path = urlOf(input);
      return path.startsWith('/api/v1/docker/containers/') && path.endsWith('/logs') && init?.method === 'POST';
    })
    .map((call) => urlOf((call as [RequestInfo | URL])[0]));
}

describe('ContainerLogPanel', () => {
  it('does not read logs until the operator opens the panel', () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({})));
    vi.stubGlobal('fetch', fetchMock);
    render(<ContainerLogPanel objectId="obj_ct" />);

    expect(screen.getByRole('button', { name: /Read recent logs/i })).toBeInTheDocument();
    expect(logCalls(fetchMock)).toHaveLength(0);
  });

  it('reads logs once when opened and does not re-read on global refreshAll', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers/obj_ct/logs' && init?.method === 'POST') {
        return Promise.resolve(logsResponse('hello'));
      }
      return Promise.resolve(new Response(JSON.stringify({}), { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<ContainerLogPanel objectId="obj_ct" />);

    fireEvent.click(screen.getByRole('button', { name: /Read recent logs/i }));
    expect(await screen.findByText('hello')).toBeInTheDocument();
    expect(logCalls(fetchMock)).toHaveLength(1);

    act(() => refreshAll());
    await waitFor(() => expect(logCalls(fetchMock)).toHaveLength(1));
  });

  it('does not carry open logs or tail across object identity', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      if (path === '/api/v1/docker/containers/obj_a/logs' && init?.method === 'POST') {
        return Promise.resolve(logsResponse('hello-A'));
      }
      if (path === '/api/v1/docker/containers/obj_b/logs' && init?.method === 'POST') {
        return Promise.resolve(logsResponse('hello-B'));
      }
      return Promise.resolve(new Response(JSON.stringify({}), { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { rerender } = render(<ContainerLogPanel key="obj_a" objectId="obj_a" />);

    fireEvent.click(screen.getByRole('button', { name: /Read recent logs/i }));
    expect(await screen.findByText('hello-A')).toBeInTheDocument();
    expect(logCalls(fetchMock)).toHaveLength(1);

    rerender(<ContainerLogPanel key="obj_b" objectId="obj_b" />);
    expect(screen.queryByText('hello-A')).not.toBeInTheDocument();
    expect(screen.queryByText('hello-B')).not.toBeInTheDocument();
    expect(logCalls(fetchMock)).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /Read recent logs/i }));
    expect(await screen.findByText('hello-B')).toBeInTheDocument();
    expect(logCalls(fetchMock)).toHaveLength(2);
  });
});
