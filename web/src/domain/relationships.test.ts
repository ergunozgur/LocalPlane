import { describe, expect, it } from 'vitest';
import type { DockerContainer, ManagementPath, NetworkInterface } from '@/api/types';
import { deriveRelationships } from './relationships';

const path = { state: 'unresolved', object_id: null } as unknown as ManagementPath;

function bridge(networkId: string, name = 'br-docker'): NetworkInterface {
  return {
    kind: 'network.interface',
    object_id: `if-${name}`,
    name,
    interface_kind: 'bridge',
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

function container(objectId: string, networkId: string | null, name = objectId): DockerContainer {
  return {
    kind: 'docker.container',
    object_id: objectId,
    name,
    networks: [{ name: 'app_default', network_id: networkId }],
  } as unknown as DockerContainer;
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
});
