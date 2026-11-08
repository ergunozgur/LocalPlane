import { describe, expect, it } from 'vitest';
import type { DockerContainer, ManagementPath, NetworkInterface } from '@/api/types';
import { deriveRelationships, type ListEnvelope } from './relationships';

const path = { state: 'unresolved', object_id: null } as unknown as ManagementPath;

function observation(freshness: string, observedAt = '2026-08-27T21:47:01Z') {
  return {
    observation_id: 'obs_1',
    sweep_id: 'swp_1',
    observed_at: observedAt,
    received_at: observedAt,
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

function bridge(
  networkId: string,
  name = 'br-docker',
  latest = true,
  freshness = 'current',
): NetworkInterface {
  return {
    kind: 'network.interface',
    object_id: `if-${name}`,
    name,
    interface_kind: 'bridge',
    observed_in_latest_sweep: latest,
    observation: observation(freshness),
    ownership: {
      // `reason` is not decoration here: it is interpolated into the evidence sentence the
      // operator reads, so a fixture that omits it lets `evidence=undefined` render green.
      created_by: {
        relation: 'created_by',
        owner: { provider: 'docker', instance: networkId, label: 'app_default', version: null },
        confidence: 'corroborated',
        reason: 'docker_ipam_gateway_on_link',
        evidence_sources: ['docker.networks'],
      },
    },
    link: { master: null },
  } as unknown as NetworkInterface;
}

function container(
  objectId: string,
  networkId: string | null,
  name = objectId,
  latest = true,
  freshness = 'current',
): DockerContainer {
  return {
    kind: 'docker.container',
    object_id: objectId,
    name,
    observed_in_latest_sweep: latest,
    observation: observation(freshness),
    networks: [{ name: 'app_default', network_id: networkId }],
  } as unknown as DockerContainer;
}

function envelope(status: string | null, sweepFreshness: string | null = 'current'): ListEnvelope | null {
  return status === null
    ? null
    : {
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
          freshness: (sweepFreshness ?? 'never_observed') as 'current' | 'stale' | 'never_observed',
          age_seconds: 0,
        },
      };
}

describe('deriveRelationships', () => {
  it('joins a Docker bridge to containers by owner.instance and networks[].network_id', () => {
    const networkId = 'network-a';
    const subject = bridge(networkId);
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [container('ct-a', networkId)],
      managementPath: path,
    });

    expect(rows.map((row) => row.target.objectId)).toContain('ct-a');
    expect(rows.find((row) => row.target.objectId === 'ct-a')?.evidence).toContain('network_id');
    // The one user-visible string assembled from provider data must never print `undefined`.
    for (const row of rows) expect(row.evidence).not.toContain('undefined');
  });

  it('joins a container to its attributed host bridge by network id', () => {
    const subject = container('ct-a', 'network-a');
    const rows = deriveRelationships({
      subject,
      interfaces: [bridge('network-a')],
      containers: [subject],
      managementPath: path,
    });

    expect(rows.find((row) => row.type === 'is carried by host bridge')?.target.objectId).toBe('if-br-docker');
  });

  it('uses the published master field and marks a missing master unresolved', () => {
    const subject = {
      ...bridge('network-a', 'eth0'),
      link: { master: 'br0' },
    } as unknown as NetworkInterface;
    const rows = deriveRelationships({ subject, interfaces: [subject], containers: [], managementPath: path });

    expect(rows.find((row) => row.type === 'member of interface')?.target.unresolved).toBe(true);
    expect(rows.find((row) => row.type === 'member of interface')?.evidence).toContain('link.master');
  });

  it('opens a resolved master interface by the published master value', () => {
    const subject = {
      ...bridge('network-a', 'veth0'),
      link: { master: 'br0' },
    } as unknown as NetworkInterface;
    const master = bridge('network-a', 'br0');
    const row = deriveRelationships({
      subject,
      interfaces: [subject, master],
      containers: [],
      managementPath: path,
    }).find((candidate) => candidate.type === 'member of interface');

    expect(row?.target.objectId).toBe(master.object_id);
    expect(row?.target.href).toBe(`/network/${master.object_id}`);
  });

  it('publishes a guarded relationship when management_path.object_id resolves to the subject', () => {
    const subject = bridge('network-a');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [],
      managementPath: { state: 'confirmed', object_id: subject.object_id } as unknown as ManagementPath,
    });

    expect(rows.find((row) => row.guarded)?.type).toBe('carries management path');
  });

  it('returns no rows when no published relationship exists', () => {
    const subject = {
      ...container('ct-a', null),
      networks: [],
    } as unknown as DockerContainer;
    expect(deriveRelationships({ subject, interfaces: [], containers: [subject], managementPath: path })).toEqual([]);
  });

  it('does not join networks or containers by names', () => {
    const subject = container('ct-a', 'network-a', 'same-name');
    const other = container('ct-b', 'network-b', 'same-name');
    const rows = deriveRelationships({
      subject,
      interfaces: [bridge('network-b')],
      containers: [subject, other],
      managementPath: path,
    });

    expect(rows.some((row) => row.target.objectId === 'ct-b')).toBe(false);
    expect(rows.find((row) => row.type === 'is carried by host bridge')).toBeUndefined();
  });

  it('refuses to name an object when an identifier matches more than one', () => {
    const networkId = 'network-dup';
    const subject = container('ct-a', networkId);
    const rows = deriveRelationships({
      subject,
      interfaces: [bridge(networkId, 'br-one'), bridge(networkId, 'br-two')],
      containers: [subject],
      managementPath: path,
    });
    const carried = rows.find((row) => row.type === 'is carried by host bridge');
    expect(carried?.target.unresolved).toBe(true);
    expect(carried?.target.objectId).toBeNull();
    expect(carried?.evidence).toContain('2 interfaces');
  });

  it('does not join a master name that matches two interfaces', () => {
    const subject = {
      kind: 'network.interface',
      object_id: 'if-veth',
      name: 'veth0',
      interface_kind: 'veth',
      ownership: { created_by: null },
      link: { master: 'br-dup' },
    } as unknown as NetworkInterface;
    const rows = deriveRelationships({
      subject,
      interfaces: [bridge('n1', 'br-dup'), bridge('n2', 'br-dup')],
      containers: [],
      managementPath: path,
    });
    const member = rows.find((row) => row.type === 'member of interface');
    expect(member?.target.unresolved).toBe(true);
    expect(member?.evidence).toContain('does not identify one');
  });

  it('reports subject and counterpart observation freshness', () => {
    const subject = bridge('network-a', 'br-docker', true, 'current');
    const retainedContainer = container('ct-a', 'network-a', 'ct-a', false, 'stale');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [retainedContainer],
      managementPath: path,
      interfaceEnvelope: envelope('ok'),
      containerEnvelope: envelope('ok'),
    });
    const row = rows.find((r) => r.type === 'contains container');
    expect(row?.subjectFreshness).toBe('current');
    expect(row?.target.freshness).toBe('stale');
    expect(row?.target.observedInLatestSweep).toBe(false);
  });

  it('reports the inventory sweep status for unresolved counterparts', () => {
    const subject = container('ct-a', null, 'ct-a', true, 'current');
    const rows = deriveRelationships({
      subject,
      interfaces: [],
      containers: [subject],
      managementPath: path,
      containerEnvelope: envelope('partial'),
    });
    const row = rows.find((r) => r.type === 'connected to Docker network');
    expect(row?.target.unresolved).toBe(true);
    expect(row?.target.sweepStatus).toBe('partial');
  });

  it('reports a failed sweep status on authoritative joins', () => {
    const subject = bridge('network-a', 'br-docker', true, 'current');
    const sibling = container('ct-a', 'network-a');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [sibling],
      managementPath: path,
      interfaceEnvelope: envelope('failed'),
      containerEnvelope: envelope('failed'),
    });
    const row = rows.find((r) => r.type === 'contains container');
    expect(row?.subjectSweepStatus).toBe('failed');
    expect(row?.target.sweepStatus).toBe('failed');
  });

  it('carries subject freshness on the management-path relationship', () => {
    const subject = bridge('network-a');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [],
      managementPath: { state: 'confirmed', object_id: subject.object_id } as unknown as ManagementPath,
      interfaceEnvelope: envelope('ok'),
    });
    const row = rows.find((r) => r.guarded);
    expect(row?.subjectFreshness).toBe('current');
  });

  it('exposes both row freshness and sweep status for the counterpart', () => {
    const subject = bridge('network-a', 'br-docker', true, 'current');
    const currentContainer = container('ct-a', 'network-a', 'ct-a', true, 'current');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [currentContainer],
      managementPath: path,
      interfaceEnvelope: envelope('ok'),
      containerEnvelope: envelope('partial'),
    });
    const row = rows.find((r) => r.type === 'contains container');
    expect(row?.target.freshness).toBe('current');
    expect(row?.target.sweepStatus).toBe('partial');
    expect(row?.target.sweepFreshness).toBe('current');
  });

  it('carries subject membership, row freshness, and sweep status together', () => {
    const subject = { ...bridge('network-a', 'br-docker', false, 'stale'), observed_in_latest_sweep: false } as unknown as NetworkInterface;
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [container('ct-a', 'network-a')],
      managementPath: path,
      interfaceEnvelope: envelope('ok'),
      containerEnvelope: envelope('ok'),
    });
    const row = rows.find((r) => r.type === 'contains container');
    expect(row?.subjectObservedInLatestSweep).toBe(false);
    expect(row?.subjectFreshness).toBe('stale');
    expect(row?.subjectSweepStatus).toBe('ok');
    expect(row?.subjectSweepFreshness).toBe('current');
  });

  it('carries authoritative envelope freshness separately from row freshness', () => {
    const subject = bridge('network-a', 'br-docker', true, 'current');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [container('ct-a', 'network-a', 'ct-a', true, 'current')],
      managementPath: path,
      interfaceEnvelope: envelope('ok', 'stale'),
      containerEnvelope: envelope('ok', 'stale'),
    });
    const row = rows.find((r) => r.type === 'contains container');
    expect(row?.subjectFreshness).toBe('current');
    expect(row?.subjectSweepFreshness).toBe('stale');
    expect(row?.target.freshness).toBe('current');
    expect(row?.target.sweepFreshness).toBe('stale');
  });

  it('marks missing envelopes as not read rather than silently ok', () => {
    const subject = bridge('network-a');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [container('ct-a', 'network-a')],
      managementPath: path,
    });
    const row = rows.find((r) => r.type === 'contains container');
    expect(row?.subjectSweepStatus).toBeNull();
    expect(row?.subjectSweepFreshness).toBeNull();
    expect(row?.target.sweepStatus).toBeNull();
    expect(row?.target.sweepFreshness).toBeNull();
  });

  it('marks Docker network targets as not independently observed', () => {
    const subject = container('ct-a', 'network-a');
    const rows = deriveRelationships({
      subject,
      interfaces: [],
      containers: [subject],
      managementPath: path,
      containerEnvelope: envelope('ok'),
    });
    const row = rows.find((r) => r.type === 'connected to Docker network');
    expect(row?.target.freshness).toBeNull();
    expect(row?.target.observedInLatestSweep).toBeNull();
    expect(row?.target.sweepStatus).toBe('ok');
  });

  it('carries both endpoint qualifiers on every relationship', () => {
    const subject = { ...bridge('network-a', 'br-docker', false, 'stale'), observed_in_latest_sweep: false } as unknown as NetworkInterface;
    const sibling = container('ct-a', 'network-a', 'ct-a', true, 'current');
    const rows = deriveRelationships({
      subject,
      interfaces: [subject],
      containers: [sibling],
      managementPath: path,
      interfaceEnvelope: envelope('partial', 'stale'),
      containerEnvelope: envelope('ok', 'current'),
    });
    const row = rows.find((r) => r.type === 'contains container');
    expect(row?.subjectObservedInLatestSweep).toBe(false);
    expect(row?.subjectFreshness).toBe('stale');
    expect(row?.subjectSweepStatus).toBe('partial');
    expect(row?.subjectSweepFreshness).toBe('stale');
    expect(row?.target.observedInLatestSweep).toBe(true);
    expect(row?.target.freshness).toBe('current');
    expect(row?.target.sweepStatus).toBe('ok');
    expect(row?.target.sweepFreshness).toBe('current');
  });
});
