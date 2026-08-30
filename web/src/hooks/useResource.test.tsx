/**
 * Request sharing.
 *
 * Several components legitimately read the same resource. What must not happen is several
 * requests for it — and what must also not happen is one component's unmount cancelling
 * another's read.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { useCallback } from 'react';
import { useResource } from './useResource';
import { endpoints } from '@/api/endpoints';
import { stubBackend, urlOf } from '@/test/backend';

afterEach(() => vi.unstubAllGlobals());

function Reader({ label }: { label: string }): JSX.Element {
  const { resource } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );
  return (
    <div>
      {label}:{resource.status === 'success' ? String(resource.data.count) : resource.status}
    </div>
  );
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

    const calls = vi
      .mocked(fetch)
      .mock.calls.filter(([input]) => urlOf(input).includes('/network/interfaces'));
    expect(calls).toHaveLength(1);
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
    const calls = vi
      .mocked(fetch)
      .mock.calls.filter(([input]) => urlOf(input).includes('/network/interfaces'));
    expect(calls).toHaveLength(2);
  });
});
