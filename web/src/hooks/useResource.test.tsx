/**
 * Request sharing and read-kind scoping.
 *
 * Several components legitimately read the same resource. What must not happen is several
 * requests for it — and what must also not happen is one component's unmount cancelling
 * another's read.
 *
 * Read kinds keep the console-wide re-read from touching live probes and consent-gated reads.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useCallback } from 'react';
import { refreshAll, useResource, type ReadKind } from './useResource';
import { endpoints } from '@/api/endpoints';
import { stubBackend, urlOf } from '@/test/backend';

afterEach(() => vi.unstubAllGlobals());

function Reader({ label, kind }: { label: string; kind?: ReadKind }): JSX.Element {
  const { resource } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
    kind ? { kind } : undefined,
  );
  return (
    <div>
      {label}:{resource.status === 'success' ? String(resource.data.count) : resource.status}
    </div>
  );
}

function interfaceCalls(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
  return fetchMock.mock.calls.filter(([input]) => urlOf(input).includes('/network/interfaces'));
}

describe('in-flight sharing', () => {
  it('issues one request when three components read the same key', async () => {
    stubBackend();
    render(
      <>
        <Reader label="a" />
        <Reader label="b" />
        <Reader label="c" />
      </>,
    );

    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
    expect(screen.getByText('b:1')).toBeInTheDocument();
    expect(screen.getByText('c:1')).toBeInTheDocument();

    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(1);
  });

  it('does not cancel a shared read when one subscriber unmounts', async () => {
    stubBackend();
    const { rerender } = render(
      <>
        <Reader label="a" />
        <Reader label="b" />
      </>,
    );
    // Drop one subscriber while the request is still in flight.
    rerender(
      <>
        <Reader label="a" />
      </>,
    );
    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
  });

  it('fetches again for a later mount rather than serving a cached body', async () => {
    stubBackend();
    const { unmount } = render(<Reader label="a" />);
    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
    unmount();

    render(<Reader label="b" />);
    await waitFor(() => expect(screen.getByText('b:1')).toBeInTheDocument());

    // Sharing is not caching: a fresh mount asks the backend again, because a body held in
    // memory would be a claim about the host that nobody made.
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(2);
  });
});

describe('read kind scoping', () => {
  it('stored reads fetch once on mount and once per console-wide re-read', async () => {
    stubBackend();
    render(<Reader label="a" />);
    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(1);

    act(() => refreshAll());
    await waitFor(() => expect(screen.queryByText('a:1')).not.toBeNull());
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(2);
  });

  it('global re-read does not restart a live read or add another request', async () => {
    stubBackend();
    render(<Reader label="a" kind="live" />);
    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(1);

    act(() => refreshAll());
    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(1);
  });

});

function ConsentReader({ label }: { label: string }): JSX.Element {
  const { resource, refresh } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
    { kind: 'consent' },
  );
  return (
    <div>
      <span>{label}:{resource.status === 'success' ? String(resource.data.count) : resource.status}</span>
      <button type="button" onClick={refresh}>Refresh</button>
    </div>
  );
}

describe('consent reads', () => {
  it('stay idle until manually refreshed, then ignore global re-read', async () => {
    stubBackend();
    render(<ConsentReader label="a" />);
    expect(screen.getByText('a:idle')).toBeInTheDocument();
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(1);

    act(() => refreshAll());
    await waitFor(() => expect(screen.getByText('a:1')).toBeInTheDocument());
    expect(interfaceCalls(vi.mocked(fetch))).toHaveLength(1);
  });
});
