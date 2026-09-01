/**
 * The Operations surface.
 *
 * Two things are load-bearing here and neither is visual. Filtering happens on the backend,
 * so a narrowed list is the backend's answer rather than a slice this console took — which
 * means the chip counts stop being knowable the moment a filter is on, and must disappear
 * rather than go stale. And there is no Risk column: risk is assessed per plan, and
 * producing it for a list of a hundred would mean a preview request per row.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { App } from '@/App';
import { ViewerProvider } from '@/identity/viewer';
import { PreferencesProvider } from '@/preferences/preferences';
import { AuthenticationProvider } from '@/auth/AuthProvider';
import { stubBackend } from '@/test/backend';

afterEach(() => vi.unstubAllGlobals());

function renderAt(path: string): void {
  render(
    <ViewerProvider>
      <PreferencesProvider>
        <AuthenticationProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </AuthenticationProvider>
      </PreferencesProvider>
    </ViewerProvider>,
  );
}

describe('filter chips', () => {
  it('shows every result value and its count without opening anything', async () => {
    stubBackend();
    renderAt('/operations');

    const bar = await screen.findByRole('group', { name: 'Change result' });
    for (const label of ['All', 'in flight', 'succeeded', 'failed', 'rolled back', 'recovery required']) {
      expect(within(bar).getByRole('button', { name: new RegExp(label) })).toBeInTheDocument();
    }
    // The fixture holds one change, in `recovery_required`.
    expect(within(bar).getByRole('button', { name: /recovery required\s*1/ })).toBeInTheDocument();
  });

  it('drops the counts once a filter is on, rather than showing stale ones', async () => {
    stubBackend();
    renderAt('/operations');

    const bar = await screen.findByRole('group', { name: 'Change result' });
    fireEvent.click(within(bar).getByRole('button', { name: /^failed/ }));

    expect(await screen.findByText(/counts are hidden while a filter is on/)).toBeInTheDocument();
    expect(within(bar).getByRole('button', { name: 'All' })).toHaveTextContent(/^All$/);
  });
});

describe('columns', () => {
  it('names the operation’s own domain, taken from its typed name', async () => {
    stubBackend();
    renderAt('/operations');
    // `network.interface.reconcile_mtu` is in the `network` domain because the backend
    // named it that way — nothing is inferred from the target object.
    expect((await screen.findAllByText('network')).length).toBeGreaterThan(0);
  });

  it('does not infer the current session into summary rows without attribution', async () => {
    stubBackend();
    renderAt('/operations');
    expect((await screen.findAllByText('not recorded')).length).toBeGreaterThan(0);
  });

  it('has no Risk column, because it would cost one preview request per row', async () => {
    stubBackend();
    renderAt('/operations');
    await screen.findAllByRole('columnheader', { name: 'Operation' });
    expect(screen.queryByRole('columnheader', { name: /risk/i })).not.toBeInTheDocument();
  });
});
