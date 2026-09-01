/**
 * The Overview, rendered whole.
 *
 * Every widget in the default template goes through the real grid and the real endpoints,
 * so a crash in any of them fails here rather than in a browser. The responses are shaped
 * like the live ones this host returns.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Overview } from '@/routes/Overview';
import { ViewerProvider } from '@/identity/viewer';
import { urlOf } from '@/test/backend';
import type { NetworkInterface } from '@/api/types';

const HOST = {
  host_id: 'host_d68ecb94',
  identity_basis: 'machine_id',
  identity_confidence: 'high',
  hostname: 'demo-host',
  configured_hostname: 'demo-host',
  boot_id: '07eb7d39',
  os_id: 'ubuntu',
  os_version_id: '24.04',
  os_pretty_name: 'Ubuntu 24.04.4 LTS',
  kernel_name: 'Linux',
  kernel_release: '6.8.0-1060-raspi',
  architecture: 'aarch64',
  identity_gaps: [],
  first_seen_at: '2026-08-27T21:46:55Z',
  last_seen_at: '2026-08-27T21:47:11Z',
  freshness: 'current',
  age_seconds: 0.19,
};

const AGENT = {
  reachable: true,
  source: 'live',
  as_of: '2026-08-27T21:47:11Z',
  agent: {
    agent_instance_id: 'agent_936af1be',
    agent_version: '0.1.0',
    protocol_version: '1',
    transport: 'unix',
    process_isolated: true,
    privilege: 'unprivileged',
    effective_uid: 1000,
    pid: 1234,
    started_at: '2026-08-27T21:46:40Z',
    last_contact_at: '2026-08-27T21:47:11Z',
  },
  socket: '/run/localplane/agent.sock',
};

/** Measured on this host: loopback leaves the management path genuinely unresolved. */
const MANAGEMENT_PATH = {
  host_id: 'host_d68ecb94',
  state: 'unresolved',
  object_id: null,
  object_name: null,
  reason: 'transport_peer_local',
  missing_evidence: ['session.peer', 'route.observe'],
  transport: {
    peer_address: '127.0.0.1',
    peer_family: 'inet',
    local_endpoint_address: '127.0.0.1',
    local_endpoint_family: 'inet',
    usable: false,
    reason: 'transport_peer_local',
  },
  evidence: null,
  evidence_ttl_seconds: 60,
  as_of: '2026-08-27T21:47:11Z',
};

const SWEEP = {
  sweep_id: 'swp_1',
  capability: 'network.observe',
  scope: 'inventory',
  provider: 'linux_network',
  provider_version: '1',
  status: 'ok',
  started_at: '2026-08-27T21:47:00Z',
  completed_at: '2026-08-27T21:47:01Z',
  received_at: '2026-08-27T21:47:01Z',
  object_count: 1,
  missing: [],
  issues: [],
  agent_instance_id: 'agent_936af1be',
};

const ROUTES: Record<string, unknown> = {
  '/api/v1/host': HOST,
  '/api/v1/agent': AGENT,
  '/api/v1/management-path': MANAGEMENT_PATH,
  '/api/v1/agent/capabilities': {
    reachable: true,
    source: 'live',
    as_of: '2026-08-27T21:47:11Z',
    agent_instance_id: 'agent_936af1be',
    capabilities: [
      {
        capability: 'systemd.service.lifecycle',
        version: 1,
        status: 'available',
        mutating: true,
        summary: '',
        reason: null,
        detail: {},
        discovered_at: '2026-08-27T21:46:40Z',
      },
    ],
  },
  '/api/v1/observations/sweeps': { host_id: 'host_d68ecb94', count: 1, sweeps: [SWEEP] },
  '/api/v1/network/interfaces': {
    host_id: 'host_d68ecb94',
    last_sweep: SWEEP,
    count: 1,
    interfaces: [
      {
        object_id: 'obj_if',
        kind: 'network.interface',
        name: 'eth0',
        interface_kind: 'ethernet',
        identity: { basis: 'mac', value: '02:00:00', confidence: 'high' },
        management: { state: 'observed', reason: 'observe_only' },
        reconciliation: null,
        ownership: {
          state: 'attributed',
          reason: 'externally_configured',
          created_by: null,
          configured_by: {
            relation: 'configured_by',
            owner: { provider: 'networkmanager', instance: 'x', label: 'lan', version: '1.46' },
            confidence: 'confirmed',
            reason: 'networkmanager_active_profile',
            evidence_sources: [],
          },
          evidence_gaps: [],
          adoption: { eligible: false, reason: 'externally_configured' },
        },
        health: { state: 'healthy', reason: 'up' },
        observation: null,
        observed_in_latest_sweep: true,
        link: { mtu: 1500, operstate: 'up', ifindex: 2, mac_address: null, mac_is_permanent: null,
                admin_up: true, carrier: true, speed_mbps: 1000, duplex: 'full', link_kind: null,
                arphrd_type: null, is_physical: true, device_path: null, master: null, carrier_changes: 1 },
        addresses: [{ family: 'inet', address: '192.168.1.10', prefix_length: 24, scope: 'global',
                      dynamic: true, valid_lifetime_s: null, preferred_lifetime_s: null }],
        statistics: null,
        first_seen_at: '2026-08-27T21:46:55Z',
        last_seen_at: '2026-08-27T21:47:11Z',
      },
    ],
  },
  '/api/v1/docker/containers': {
    host_id: 'host_d68ecb94', last_sweep: SWEEP, count: 0, containers: [],
  },
  '/api/v1/systemd/units': {
    host_id: 'host_d68ecb94', capability: null, last_sweep: SWEEP, count: 0, units: [],
  },
  '/api/v1/findings': { host_id: 'host_d68ecb94', status: 'open', count: 0, findings: [] },
  '/api/v1/runs': { host_id: 'host_d68ecb94', state: null, count: 0, runs: [] },
  '/api/v1/changes': { host_id: 'host_d68ecb94', count: 0, changes: [] },
};

/** jsdom has no ResizeObserver; without one the plane never measures and draws one column. */
function installResizeObserver(width: number): void {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(): void {
        this.callback(
          [{ contentRect: { width } } as unknown as ResizeObserverEntry],
          this,
        );
      }
      unobserve(): void {}
      disconnect(): void {}
    },
  );
}

function stubBackend(): void {
  installResizeObserver(1400);
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>((input) => {
      const url = urlOf(input);
      const body = ROUTES[url];
      if (body === undefined) {
        return Promise.resolve(
          new Response(JSON.stringify({ error: { code: 'http_404', message: 'no route', detail: {} } }), {
            status: 404,
          }),
        );
      }
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
    }),
  );
}

function managedInterface(
  objectId: string,
  reconciliation: Exclude<NetworkInterface['reconciliation'], undefined>,
): NetworkInterface {
  const list = ROUTES['/api/v1/network/interfaces'] as {
    interfaces: readonly NetworkInterface[];
  };
  const base = list.interfaces[0];
  if (!base) throw new Error('overview fixture must contain a network interface');
  return {
    ...base,
    object_id: objectId,
    name: objectId,
    management: { state: 'managed', reason: 'operator_adopted' },
    reconciliation,
  };
}

function stubBackendWithInterfaces(
  interfaces: readonly NetworkInterface[],
  openFindings: readonly Record<string, unknown>[] = [],
): void {
  installResizeObserver(1400);
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>((input) => {
      const url = urlOf(input);
      let body = ROUTES[url];
      if (url === '/api/v1/network/interfaces') {
        body = { ...(body as Record<string, unknown>), count: interfaces.length, interfaces };
      } else if (url === '/api/v1/findings') {
        body = {
          ...(body as Record<string, unknown>),
          count: openFindings.length,
          findings: openFindings,
        };
      }
      if (body === undefined) {
        return Promise.resolve(
          new Response(JSON.stringify({ error: { code: 'http_404', message: 'no route', detail: {} } }), {
            status: 404,
          }),
        );
      }
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

function renderOverview(): void {
  render(
    <ViewerProvider>
      <MemoryRouter>
        <Overview />
      </MemoryRouter>
    </ViewerProvider>,
  );
}

describe('Overview', () => {
  it('renders every widget in the default template without crashing', async () => {
    stubBackend();
    renderOverview();

    await screen.findAllByText('demo-host');
    // The lead plate is titled with the host itself, and is identified
    // here by the relationship plane it carries.
    for (const column of ['Upstream', 'Path', 'Host', 'Attached']) {
      expect(screen.getByText(column)).toBeInTheDocument();
    }
    for (const title of ['Observation', 'Workloads', 'Network', 'Services', 'Recent runs', 'Recent changes']) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
  });

  it('reports the management path as unresolved rather than guessing', async () => {
    stubBackend();
    renderOverview();

    await screen.findAllByText('demo-host');
    // Stated twice by design: as the path node in the plane, and as the pill beneath it.
    expect(screen.getAllByText('unresolved').length).toBeGreaterThan(0);
    expect(screen.getByText(/cannot tell which interface carries this connection/i)).toBeInTheDocument();
    expect(screen.getAllByText('transport_peer_local').length).toBeGreaterThan(0);
  });

  it('shows the running and configured hostnames separately when they differ', async () => {
    installResizeObserver(1400);
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>((input) => {
        const url = urlOf(input);
        const body =
          url === '/api/v1/host'
            ? { ...HOST, configured_hostname: 'renamed-in-etc-hostname' }
            : (ROUTES[url] ?? {});
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
      }),
    );
    renderOverview();

    expect((await screen.findAllByText('demo-host')).length).toBeGreaterThan(0);
    expect(screen.getByText('renamed-in-etc-hostname')).toBeInTheDocument();
  });

  it('names the evidence that would settle the management path', async () => {
    stubBackend();
    renderOverview();

    await screen.findByText('session.peer');
    expect(screen.getByText('route.observe')).toBeInTheDocument();
  });

  it('distinguishes "nothing is managed" from "no drift"', async () => {
    stubBackend();
    renderOverview();

    // The only interface is `observed`, so drift is not merely absent — it is inapplicable,
    // and the rail's quiet line has to say so rather than reporting a clean estate.
    expect(await screen.findByText(/Nothing needs attention/i)).toBeInTheDocument();
    expect(screen.getByText(/none managed, so nothing can drift/i)).toBeInTheDocument();
  });

  it('does not report all in sync while a managed reconciliation is unsettled', async () => {
    // `applying` is a change in flight; it is not an in-sync result and must not be
    // presented as one.
    stubBackendWithInterfaces([managedInterface('obj_managed', 'applying')]);
    renderOverview();

    expect(await screen.findByText(/Attention could not be fully assessed/i)).toBeInTheDocument();
    expect(
      screen.getByText(/1 managed object has an unknown or in-progress reconciliation result/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/all in sync/i)).not.toBeInTheDocument();
  });

  it('reports all in sync only when every managed reconciliation is settled', async () => {
    stubBackendWithInterfaces([managedInterface('obj_managed', 'in_sync')]);
    renderOverview();

    expect(await screen.findByText(/Nothing needs attention/i)).toBeInTheDocument();
    expect(screen.getByText(/1 managed, all in sync/i)).toBeInTheDocument();
  });

  it('keeps drift and findings visible while reconciliation assessment is incomplete', async () => {
    stubBackendWithInterfaces(
      [
        managedInterface('obj_drifted', 'drifted'),
        managedInterface('obj_applying', 'applying'),
      ],
      [
        {
          finding_id: 'finding_exposure',
          object_name: 'sshd.service',
          finding_type: 'open_exposure',
        },
      ],
    );
    renderOverview();

    expect(await screen.findByRole('link', { name: /obj_drifted drifted/i })).toBeInTheDocument();
    expect(screen.getByText('sshd.service')).toBeInTheDocument();
    expect(screen.getByText(/Assessment incomplete/i)).toBeInTheDocument();
    expect(
      screen.getByText(/1 managed object has an unknown or in-progress reconciliation result/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/all in sync/i)).not.toBeInTheDocument();
  });

  it('states an unread estate rather than showing an empty Attached column', async () => {
    installResizeObserver(1400);
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>((input) => {
        const url = urlOf(input);
        if (url === '/api/v1/docker/containers') {
          return Promise.resolve(
            new Response(
              JSON.stringify({ error: { code: 'agent_unavailable', message: 'down', detail: {} } }),
              { status: 503 },
            ),
          );
        }
        return Promise.resolve(new Response(JSON.stringify(ROUTES[url] ?? {}), { status: 200 }));
      }),
    );
    renderOverview();

    expect(await screen.findByText('not read')).toBeInTheDocument();
    expect(screen.getByText(/unknown, not absent/i)).toBeInTheDocument();
  });

  it('explains an empty container list by its sweep', async () => {
    stubBackend();
    renderOverview();
    expect(await screen.findByText(/daemon answered and reported none/i)).toBeInTheDocument();
  });

  it('shows a failed read as an error on that widget alone', async () => {
    installResizeObserver(1400);
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>((input) => {
        const url = urlOf(input);
        if (url === '/api/v1/docker/containers') {
          return Promise.resolve(
            new Response(
              JSON.stringify({ error: { code: 'agent_unavailable', message: 'agent down', detail: {} } }),
              { status: 503 },
            ),
          );
        }
        const body = ROUTES[url] ?? {};
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
      }),
    );
    renderOverview();

    // The failing widget reports it; the rest of the page still renders, and — the point of
    // this test — the machine panel keeps its identity rather than being blanked by an
    // unreadable Docker socket.
    await waitFor(() => expect(screen.getAllByText('agent_unavailable').length).toBeGreaterThan(0));
    expect(screen.getAllByText('demo-host').length).toBeGreaterThan(0);
    expect(screen.getByText('Attached')).toBeInTheDocument();
  });
});
