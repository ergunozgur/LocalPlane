import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ApiError } from '@/api/client';
import { ViewerProvider } from '@/identity/viewer';
import { RELATIONSHIP_BRIDGE, stubBackend } from '@/test/backend';
import { WorkloadDetail } from '@/routes/workloads/WorkloadDetail';
import type { DockerContainer, DockerContainerList, ManagementPath, NetworkInterface, NetworkInterfaceList } from '@/api/types';
import type { Resource } from '@/hooks/useResource';
import { ObjectRelationships } from './ObjectRelationships';

const path = { state: 'unresolved', object_id: null } as unknown as ManagementPath;

afterEach(() => vi.unstubAllGlobals());

function resource<T>(data: T): Resource<T> {
  return { status: 'success', data, fetchedAt: new Date('2026-09-06T00:00:00Z'), refreshing: false };
}

function bridge(): NetworkInterface {
  return {
    kind: 'network.interface',
    object_id: 'if-bridge',
    name: 'br-app',
    interface_kind: 'bridge',
    // A complete created_by claim, because `reason` is interpolated into the evidence cell the
    // operator reads: an incomplete fixture renders the literal `evidence=undefined` and passes.
    ownership: {
      created_by: {
        relation: 'created_by',
        owner: { provider: 'docker', instance: 'network-a', label: 'app_default', version: null },
        confidence: 'corroborated',
        reason: 'docker_ipam_gateway_on_link',
        evidence_sources: ['docker.networks'],
      },
    },
    link: { master: null },
  } as unknown as NetworkInterface;
}

function container(objectId: string, networks = [{ name: 'app_default', network_id: 'network-a' }]): DockerContainer {
  return { kind: 'docker.container', object_id: objectId, name: objectId, networks } as unknown as DockerContainer;
}

function observation(freshness: string) {
  return {
    observation_id: 'obs_1',
    sweep_id: 'swp_1',
    observed_at: '2026-08-27T21:47:01Z',
    received_at: '2026-08-27T21:47:01Z',
    freshness,
    age_seconds: 3,
    provider: 'test',
    provider_version: '1',
    method: 'test',
    capability: 'test.observe',
    fidelity: 'complete',
    gaps: [],
  };
}

function bridgeWith(freshness: string): NetworkInterface {
  return { ...bridge(), observed_in_latest_sweep: true, observation: observation(freshness) } as unknown as NetworkInterface;
}

function containerWith(objectId: string, freshness: string, latest = true): DockerContainer {
  return { ...container(objectId), observed_in_latest_sweep: latest, observation: observation(freshness) } as unknown as DockerContainer;
}

function listEnvelope(status: string, sweepFreshness: string): { last_sweep: Record<string, unknown> } {
  return {
    last_sweep: {
      sweep_id: 'swp_1',
      capability: 'test.observe',
      scope: 'inventory',
      provider: 'test',
      provider_version: '1',
      status,
      started_at: '2026-08-27T21:47:00Z',
      completed_at: '2026-08-27T21:47:01Z',
      received_at: '2026-08-27T21:47:01Z',
      object_count: 1,
      missing: [],
      issues: [],
      agent_instance_id: 'agent_1',
      freshness: sweepFreshness,
      age_seconds: 0,
    },
  };
}

describe('ObjectRelationships', () => {
  it('renders evidence-backed targets and links to existing detail routes', () => {
    const subject = container('ct-a');
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ interfaces: [bridge()], count: 1 } as NetworkInterfaceList)}
          containers={resource<DockerContainerList>({ containers: [subject, container('ct-b')], count: 2 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: 'br-app' })).toHaveAttribute('href', '/network/if-bridge');
    expect(screen.getByRole('link', { name: 'ct-b' })).toHaveAttribute('href', '/workloads/ct-b');
    expect(screen.getAllByText(/network_id=network-a/).length).toBeGreaterThan(0);
  });

  it('keeps interface-derived relationships visible while the container stream is unread', () => {
    const subject = bridge();
    const unread = {
      status: 'failed',
      error: new ApiError({ kind: 'unreachable', message: 'socket closed', path: '/docker/containers' }),
      retry: () => undefined,
    } as Resource<DockerContainerList>;
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ interfaces: [subject], count: 1 } as NetworkInterfaceList)}
          containers={unread}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText('app_default')).toBeInTheDocument();
    expect(screen.getByText(/unread: containers/)).toBeInTheDocument();
    expect(screen.getByText(/unreachable/)).toBeInTheDocument();
    // The evidence cell is assembled from provider fields, so it is the one place a missing
    // value would reach the operator as the word "undefined".
    expect(document.body.textContent).not.toContain('undefined');
  });

  it('states an empty relationship result rather than implying unread data is empty', () => {
    const subject = container('ct-a', []);
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({
             host_id: 'host_1',
             last_sweep: null,
             interfaces: [],
             count: 0,
           })}
          containers={resource<DockerContainerList>({ containers: [subject], count: 1 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText('No relationship is established')).toBeInTheDocument();
  });

  it('keeps loading distinct from an empty relationship result', () => {
    const subject = bridge();
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ interfaces: [subject], count: 1 } as NetworkInterfaceList)}
          containers={{ status: 'loading' }}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(/loading: containers/)).toBeInTheDocument();
    expect(screen.queryByText('No relationship is established')).not.toBeInTheDocument();
  });

  it('shows subject membership, row freshness, and sweep status together', () => {
    const subject = bridgeWith('current');
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ ...listEnvelope('ok', 'current'), interfaces: [subject], count: 1 } as NetworkInterfaceList)}
          containers={resource<DockerContainerList>({ ...listEnvelope('ok', 'current'), containers: [containerWith('ct-a', 'current')], count: 1 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    const subjectCoverage = coverage(rowContaining('contains container'), 'subject');
    expect(within(subjectCoverage).getByText('seen in latest inventory')).toBeInTheDocument();
    expect(within(subjectCoverage).getByText(/sweep ok · current/)).toBeInTheDocument();
  });

  it('shows counterpart row freshness and sweep status even when the row is current', () => {
    const subject = bridgeWith('current');
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ ...listEnvelope('ok', 'current'), interfaces: [subject], count: 1 } as NetworkInterfaceList)}
          containers={resource<DockerContainerList>({ ...listEnvelope('partial', 'current'), containers: [containerWith('ct-a', 'current')], count: 1 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    const counterpartCoverage = coverage(rowContaining('contains container'), 'counterpart');
    expect(within(counterpartCoverage).getByText('current')).toBeInTheDocument();
    expect(within(counterpartCoverage).getByText(/sweep partial · current/)).toBeInTheDocument();
  });

  it('shows retained subject membership and stale row freshness without hiding sweep status', () => {
    const subject = { ...bridgeWith('stale'), observed_in_latest_sweep: false } as unknown as NetworkInterface;
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ ...listEnvelope('ok', 'current'), interfaces: [subject], count: 1 } as NetworkInterfaceList)}
          containers={resource<DockerContainerList>({ ...listEnvelope('ok', 'current'), containers: [containerWith('ct-a', 'current')], count: 1 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    const subjectCoverage = coverage(rowContaining('contains container'), 'subject');
    expect(within(subjectCoverage).getByText('not seen in latest inventory')).toBeInTheDocument();
    expect(within(subjectCoverage).getByText('stale')).toBeInTheDocument();
    expect(within(subjectCoverage).getByText(/sweep ok · current/)).toBeInTheDocument();
  });

  it('carries authoritative envelope freshness separately from row freshness', () => {
    const subject = bridgeWith('current');
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ ...listEnvelope('ok', 'stale'), interfaces: [subject], count: 1 } as NetworkInterfaceList)}
          containers={resource<DockerContainerList>({ ...listEnvelope('ok', 'stale'), containers: [containerWith('ct-a', 'current')], count: 1 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    const row = rowContaining('contains container');
    const subjectCoverage = coverage(row, 'subject');
    const counterpartCoverage = coverage(row, 'counterpart');
    expect(within(subjectCoverage).getByText('current')).toBeInTheDocument();
    expect(within(subjectCoverage).getByText(/sweep ok · stale/)).toBeInTheDocument();
    expect(within(counterpartCoverage).getByText('current')).toBeInTheDocument();
    expect(within(counterpartCoverage).getByText(/sweep ok · stale/)).toBeInTheDocument();
  });

  it('says inventory not read when the envelope is absent', () => {
    const subject = bridgeWith('current');
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ interfaces: [subject], count: 1 } as NetworkInterfaceList)}
          containers={resource<DockerContainerList>({ containers: [containerWith('ct-a', 'current')], count: 1 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    const row = rowContaining('contains container');
    expect(within(coverage(row, 'subject')).getByText('inventory not read')).toBeInTheDocument();
    expect(within(coverage(row, 'counterpart')).getByText('inventory not read')).toBeInTheDocument();
  });

  it('marks a Docker network target as not independently observed', () => {
    const subject = container('ct-a');
    render(
      <MemoryRouter>
        <ObjectRelationships
          subject={subject}
          interfaces={resource<NetworkInterfaceList>({ host_id: 'host_1', last_sweep: null, interfaces: [], count: 0 })}
          containers={resource<DockerContainerList>({ ...listEnvelope('ok', 'current'), containers: [subject], count: 1 } as DockerContainerList)}
          managementPath={resource(path)}
        />
      </MemoryRouter>,
    );

    const counterpartCoverage = coverage(rowContaining('connected to Docker network'), 'counterpart');
    expect(within(counterpartCoverage).getByText('not independently observed')).toBeInTheDocument();
    expect(within(counterpartCoverage).getByText(/source inventory sweep ok · current/)).toBeInTheDocument();
  });
});

function rowContaining(text: string): HTMLElement {
  return screen.getByText(text).closest('tr') as HTMLElement;
}

function coverage(row: HTMLElement, endpoint: 'subject' | 'counterpart'): HTMLElement {
  return within(row).getByTestId(`${endpoint}-coverage`);
}

describe('detail workspace integration', () => {
  it('renders the relationships tab on a workload detail route', async () => {
    stubBackend({
      '/api/v1/network/interfaces': {
        host_id: 'host_1',
        last_sweep: null,
        count: 1,
        interfaces: [RELATIONSHIP_BRIDGE],
      },
    });
    render(
      <ViewerProvider>
        <MemoryRouter initialEntries={['/workloads/obj_ct?tab=relationships']}>
          <Routes>
            <Route path="/workloads/:objectId" element={<WorkloadDetail />} />
          </Routes>
        </MemoryRouter>
      </ViewerProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Relationships' })).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'br-app' })).toHaveAttribute('href', '/network/if-bridge');
  });
});
