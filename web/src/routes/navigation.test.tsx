/**
 * Every route renders, and the detail flows carry the facts that matter.
 *
 * A crash on a detail page is the failure mode a component-only suite misses, so each route
 * is mounted through the real app shell and the real router.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { App } from '@/App';
import { ViewerProvider } from '@/identity/viewer';
import { AuthenticationProvider } from '@/auth/AuthProvider';
import { PreferencesProvider } from '@/preferences/preferences';
import { BACKEND, stubBackend } from '@/test/backend';

const INTERFACE_FIXTURE = BACKEND['/api/v1/network/interfaces/obj_if'] as Record<string, unknown>;

afterEach(() => vi.unstubAllGlobals());

/** Reports the router's address, which `MemoryRouter` deliberately keeps off `window`. */
function AddressProbe(): JSX.Element {
  const location = useLocation();
  return <span data-testid="address">{`${location.pathname}${location.search}`}</span>;
}

function renderAt(path: string): void {
  render(
    <PreferencesProvider>
      <MemoryRouter initialEntries={[path]}>
        <AuthenticationProvider>
          <ViewerProvider>
            <App />
            <AddressProbe />
          </ViewerProvider>
        </AuthenticationProvider>
      </MemoryRouter>
    </PreferencesProvider>,
  );
}

/**
 * Open one tab of the object workspace.
 *
 * Detail content lives behind object tabs now, so a test that wants the Provenance panel has
 * to do what an operator does. The tab is a real `role="tab"`, which is what makes this a
 * one-liner rather than a class-name query.
 */
async function openTab(name: string): Promise<void> {
  fireEvent.click(await screen.findByRole('tab', { name: new RegExp(name, 'i') }));
}

describe('every route renders', () => {
  it.each([
    ['/', 'Attached'],
    ['/network', 'Network'],
    ['/network/obj_if', 'enx020000000012'],
    ['/workloads', 'Workloads'],
    ['/workloads/obj_ct', 'grafana'],
    ['/system', 'System'],
    ['/system/obj_unit', 'ModemManager.service'],
    ['/operations', 'Operations'],
    ['/operations/changes/chg_1', 'network.interface.reconcile_mtu'],
    ['/settings', 'Settings'],
  ])('renders %s', async (path, expected) => {
    stubBackend();
    renderAt(path);
    expect((await screen.findAllByText(expected)).length).toBeGreaterThan(0);
  });

  it('shows a not-found surface for an unknown address', async () => {
    stubBackend();
    renderAt('/nope');
    expect(await screen.findByText('No such page')).toBeInTheDocument();
  });
});

describe('the shell', () => {
  it('offers the five domains and marks the active one', async () => {
    stubBackend();
    renderAt('/network');

    const nav = await screen.findByRole('navigation', { name: 'Primary' });

    // A domain that is one surface is a link; a domain with sub-surfaces is a menu trigger,
    // and the chevron on the second kind is the only warning an operator gets that pressing
    // it opens something rather than going somewhere.
    for (const label of ['Overview', 'Network']) {
      expect(within(nav).getByRole('link', { name: new RegExp(label) })).toBeInTheDocument();
    }
    for (const label of ['Workloads', 'System', 'Operations']) {
      expect(within(nav).getByRole('button', { name: new RegExp(label) })).toHaveAttribute(
        'aria-haspopup',
        'menu',
      );
    }
    expect(within(nav).getByRole('link', { name: /Network/ })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('opens a domain menu and lists its sub-surfaces as menu items', async () => {
    stubBackend();
    renderAt('/network');

    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    const trigger = within(nav).getByRole('button', { name: /Workloads/ });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(await screen.findByRole('menuitem', { name: /Container groups/ })).toHaveAttribute(
      'href',
      '/workloads',
    );
    expect(screen.getByRole('menuitem', { name: /Runtime/ })).toHaveAttribute(
      'href',
      '/workloads/runtime',
    );
  });

  it('names the host in the app bar on every surface', async () => {
    stubBackend();
    renderAt('/operations');
    // The host sits in the bar itself: every number on every screen is a claim about it.
    const bar = await screen.findByRole('banner');
    expect(await within(bar).findByText('demo-host')).toBeInTheDocument();
    expect(within(bar).getByText('127.0.0.1')).toBeInTheDocument();
  });

  it('marks the host as not currently observable when the agent is silent', async () => {
    stubBackend({
      '/api/v1/agent': {
        reachable: false, source: 'recorded', as_of: '2026-08-27T21:47:11Z',
        error: { code: 'agent_unavailable', message: 'not running', detail: {} },
        agent: null, socket: '/run/localplane/agent.sock',
      },
    });
    renderAt('/');
    const bar = await screen.findByRole('banner');
    expect(await within(bar).findByRole('img', { name: /agent unreachable/i })).toBeInTheDocument();
    expect((await screen.findAllByText('agent unreachable')).length).toBeGreaterThan(0);
  });
});

describe('interface detail', () => {
  it('shows protection as unknown with the evidence that would settle it', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    await openTab('Protection');

    expect(screen.getAllByText('unknown').length).toBeGreaterThan(0);
    expect(screen.queryByText('clear')).not.toBeInTheDocument();
    expect(screen.getByText('session.peer')).toBeInTheDocument();
  });

  it('shows who configures the interface, from the provenance claim', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    await openTab('Provenance');
    expect((await screen.findAllByText('networkmanager')).length).toBeGreaterThan(0);
    expect(screen.getByText('service-lan')).toBeInTheDocument();
  });

  it('marks a source that answered and one that is simply not installed', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    await openTab('Provenance');
    await screen.findByText('kernel.interface');
    expect(screen.getByText('tailscale.status')).toBeInTheDocument();
    expect(screen.getByText('not present')).toBeInTheDocument();
  });

  it('reports an observed object as not tracked rather than in sync', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    // The head carries the reconciliation state, so this needs no tab at all — which is the
    // point: a safety word must be readable before an operator opens anything.
    expect((await screen.findAllByText('not tracked')).length).toBeGreaterThan(0);
    expect(screen.queryByText('in sync')).not.toBeInTheDocument();
  });

  it('keeps raw evidence behind a disclosure rather than as the main view', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    await openTab('Evidence');
    const summary = (await screen.findAllByText('Raw evidence')).find(
      (node) => node.closest('details') !== null,
    );
    expect(summary?.closest('details')).not.toHaveAttribute('open');
  });

  it('assembles the tab strip from what the interface is', async () => {
    stubBackend();
    renderAt('/network/obj_if');

    await screen.findByRole('tab', { name: /Overview/ });
    for (const label of [
      'Addressing',
      'Intent and drift',
      'Protection',
      'Provenance',
      'Traffic',
      'Evidence',
    ]) {
      expect(screen.getByRole('tab', { name: new RegExp(label, 'i') })).toBeInTheDocument();
    }
  });

  it('drops the Traffic tab when the source supplied no counters', async () => {
    // Assembled away rather than rendered empty: an interface with no counters has no
    // Traffic tab at all, and nothing on the page implies the counters were zero.
    stubBackend({
      '/api/v1/network/interfaces/obj_if': { ...INTERFACE_FIXTURE, statistics: null },
    });
    renderAt('/network/obj_if');

    await screen.findByRole('tab', { name: /Overview/ });
    expect(screen.queryByRole('tab', { name: /Traffic/ })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Addressing/ })).toBeInTheDocument();
  });

  it('puts the open tab in the address, so a tab can be sent to somebody', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    await openTab('Provenance');
    expect(screen.getByTestId('address')).toHaveTextContent('/network/obj_if?tab=provenance');

    // The first tab is the absence of the parameter, not `?tab=overview` — a default that
    // writes itself into the address turns every visit into a distinct URL.
    await openTab('Overview');
    expect(screen.getByTestId('address')).toHaveTextContent('/network/obj_if');
    expect(screen.getByTestId('address').textContent).not.toContain('tab=');
  });
});

describe('the machine record rail', () => {
  it('rides alongside every tab of the object workspace', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    await screen.findByText('Machine record');
    await openTab('Provenance');
    expect(screen.getByText('Machine record')).toBeInTheDocument();
    expect(
      screen.getByText('every entry here was written by an observation, not by a person'),
    ).toBeInTheDocument();
  });

  it('lists object-scoped changes and runs on one timeline', async () => {
    stubBackend();
    renderAt('/network/obj_if');
    // The fixture's change targets this object; the rail is the object's own history.
    expect(
      (await screen.findAllByText('network.interface.reconcile_mtu')).length,
    ).toBeGreaterThan(0);
  });

  it('says which stream it could not read instead of showing a shorter history', async () => {
    stubBackend({ '/api/v1/changes': undefined });
    renderAt('/network/obj_if');
    expect(await screen.findByText(/changes stream could not be read/i)).toBeInTheDocument();
    expect(screen.getByText(/what is missing is unknown/i)).toBeInTheDocument();
  });
});

describe('change detail — the five outcomes stay five', () => {
  it('shows write_unknown as its own answer, not as failure or as not written', async () => {
    stubBackend();
    renderAt('/operations/changes/chg_1');

    await screen.findAllByText('Outcome');
    expect(screen.getAllByText('write unknown').length).toBeGreaterThan(0);
    expect(screen.queryByText('not written')).not.toBeInTheDocument();
    expect(screen.queryByText('written')).not.toBeInTheDocument();
  });

  it('explains that reading the value back would not settle it', async () => {
    stubBackend();
    renderAt('/operations/changes/chg_1');
    expect(await screen.findByText(/different question from whether this write happened/i)).toBeInTheDocument();
  });

  it('keeps verification separate from the write', async () => {
    stubBackend();
    renderAt('/operations/changes/chg_1');
    await screen.findAllByText('Outcome');
    expect(screen.getAllByText('observation unavailable').length).toBeGreaterThan(0);
    expect(screen.queryByText('verified')).not.toBeInTheDocument();
  });

  it('presents recovery_required as a truthful ending holding a lock', async () => {
    stubBackend();
    renderAt('/operations/changes/chg_1');
    await screen.findByText('Recovery');
    expect(screen.getAllByText('recovery required').length).toBeGreaterThan(0);
    const locked = screen.getByText('Object write locked').closest('div') as HTMLElement;
    expect(within(locked).getByText('yes')).toBeInTheDocument();
  });

  it('lists recovery actions as record without offering them as controls', async () => {
    stubBackend();
    renderAt('/operations/changes/chg_1');
    await screen.findByText('retry');
    expect(screen.getByText('resolve')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^retry$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^resolve$/i })).not.toBeInTheDocument();
  });
});

describe('workload detail', () => {
  it('states that lifecycle control is deferred rather than unsupported', async () => {
    stubBackend();
    renderAt('/workloads/obj_ct');
    await openTab('Lifecycle');
    expect(screen.getByText(/deferred deliberately, not missing by oversight/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^start$/i })).not.toBeInTheDocument();
  });

  it('reports a container with no health check as having none, not as unhealthy', async () => {
    stubBackend();
    renderAt('/workloads/obj_ct');
    expect((await screen.findAllByText('no health check')).length).toBeGreaterThan(0);
    expect(screen.queryByText('unhealthy')).not.toBeInTheDocument();
  });
});

describe('unit detail', () => {
  it('links a resolved relationship and leaves a referenced one unlinked', async () => {
    stubBackend();
    renderAt('/system/obj_unit');
    await openTab('Relationships');

    const resolved = await screen.findByRole('link', { name: 'dbus.socket' });
    expect(resolved).toHaveAttribute('href', '/system/obj_dbus');
    expect(screen.queryByRole('link', { name: 'network.target' })).not.toBeInTheDocument();
    expect(screen.getByText('network.target')).toBeInTheDocument();
  });

  it('renders a zero systemd timestamp as never rather than as the epoch', async () => {
    stubBackend();
    renderAt('/system/obj_unit');
    await openTab('Timestamps');
    const row = (await screen.findByText('InactiveEnterTimestamp')).closest('div') as HTMLElement;
    expect(within(row).getByRole('img', { name: /never happened/i })).toBeInTheDocument();
  });
});

describe('appearance', () => {
  it('offers only the themes that exist, and applies the choice', async () => {
    stubBackend();
    const user = userEvent.setup();
    renderAt('/settings');

    // Appearance sits behind the account menu, as swatch buttons rather than a list of
    // names — the choice being made is a visual one.
    await user.click((await screen.findAllByRole('button', { name: 'Account and appearance' }))[0]!);

    const graphite = screen.getByRole('button', { name: /graphite/i });
    expect(screen.getByRole('button', { name: /^localplane default$/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    // Exactly the two implemented themes — no "system theme", no greyed-out coming-soon
    // entry. Scoped to the pressed-state buttons so the System *navigation* group, which is
    // a different thing entirely, does not match.
    const themeButtons = screen
      .getAllByRole('button')
      .filter((button) => button.hasAttribute('aria-pressed'));
    expect(themeButtons).toHaveLength(2);

    await user.click(graphite);
    expect(document.documentElement.getAttribute('data-theme')).toBe('graphite');
  });

  it('attributes the session without inventing a user', async () => {
    stubBackend();
    renderAt('/settings');
    expect((await screen.findAllByText('authenticated_request')).length).toBeGreaterThan(0);
    expect(screen.getByText('none')).toBeInTheDocument();
  });
});
