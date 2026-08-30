/**
 * The topology's derivation.
 *
 * Each node has to be traceable to something the backend published. These tests exist to
 * stop the graph from quietly acquiring a node that "looks right" — the failure mode a
 * relationship view invites, and the one this product must not have.
 */
import { describe, expect, it } from 'vitest';
import { buildTopology } from './topology-model';
import type { AgentStatus, DockerContainer, Host, ManagementPath, NetworkInterface } from '@/api/types';

const HOST = {
  host_id: 'host_1', hostname: 'demo-host', architecture: 'aarch64',
  os_pretty_name: 'Ubuntu 24.04.4 LTS', kernel_release: '6.8.0-1060-raspi',
  identity_gaps: [], freshness: 'current',
} as unknown as Host;

const AGENT = { reachable: true, source: 'live', agent: { agent_version: '0.1.0' } } as unknown as AgentStatus;

const UNRESOLVED_PATH = {
  state: 'unresolved', object_id: null, object_name: null,
  reason: 'transport_peer_local', missing_evidence: ['session.peer', 'route.observe'],
  transport: { peer_address: '127.0.0.1', usable: false, reason: 'transport_peer_local' },
  evidence: null,
} as unknown as ManagementPath;

function dockerBridge(networkId: string, label: string, name: string): NetworkInterface {
  return {
    object_id: `obj_${name}`, name, interface_kind: 'bridge',
    management: { state: 'observed', reason: 'observe_only' },
    ownership: {
      state: 'attributed', reason: 'externally_configured',
      created_by: {
        relation: 'created_by',
        owner: { provider: 'docker', instance: networkId, label, version: null },
        confidence: 'confirmed', reason: 'docker_ipam_gateway_on_link', evidence_sources: [],
      },
      configured_by: null, evidence_gaps: [],
      adoption: { eligible: false, reason: 'externally_configured' },
    },
    health: { state: 'healthy', reason: 'up' },
    link: { mtu: 1500, operstate: 'up' },
    addresses: [],
  } as unknown as NetworkInterface;
}

function container(name: string, networkId: string, state = 'running'): DockerContainer {
  return {
    object_id: `obj_${name}`, name,
    runtime: { state },
    networks: [{ name: 'ignored-name', network_id: networkId, ip_address: '172.20.0.2', aliases: [] }],
    ports: [],
  } as unknown as DockerContainer;
}

const base = { host: HOST, agent: AGENT, path: UNRESOLVED_PATH };

describe('upstream', () => {
  it('says nothing is established when there is no route evidence', () => {
    const nodes = buildTopology({ ...base, interfaces: [], containers: [] });
    const gateway = nodes.find((n) => n.id === 'gateway');
    expect(gateway?.mark.tone).toBe('unknown');
    expect(gateway?.name).toBe('not established');
  });

  it('shows the gateway only when the backend published route evidence', () => {
    const nodes = buildTopology({
      ...base,
      path: {
        ...UNRESOLVED_PATH,
        state: 'confirmed',
        object_name: 'eth0',
        evidence: { route: { status: 'resolved', gateway: '192.168.1.1', destination: '0.0.0.0', destination_prefix_length: 0, protocol: 'static', scope: 'global' } },
      } as unknown as ManagementPath,
      interfaces: [],
      containers: [],
    });
    const gateway = nodes.find((n) => n.id === 'gateway');
    expect(gateway?.name).toBe('192.168.1.1');
    expect(gateway?.mark.tone).toBe('good');
  });
});

describe('path', () => {
  it('renders an unresolved management path as unknown with its reason', () => {
    const nodes = buildTopology({ ...base, interfaces: [], containers: [] });
    const path = nodes.find((n) => n.id === 'path');
    expect(path?.mark.tone).toBe('unknown');
    expect(path?.name).toBe('unresolved');
    expect(path?.note).toContain('transport_peer_local');
  });
});

describe('attached — joins are on identifiers, never on names', () => {
  it('links containers to a docker network through the id the backend attributed', () => {
    const networkId = 'c0ffee00000100000000000000000000000000000000000000000000000000a1';
    const nodes = buildTopology({
      ...base,
      interfaces: [dockerBridge(networkId, 'localplane_default', 'br-c0ffee000001')],
      containers: [container('a', networkId), container('b', networkId, 'exited')],
    });
    const net = nodes.find((n) => n.id === `net:${networkId}`);
    expect(net?.name).toBe('localplane_default');
    expect(net?.detail).toBe('br-c0ffee000001');
    expect(net?.note).toContain('2 containers');
    expect(net?.note).toContain('1 running');
  });

  it('does not attach a container whose network id differs, however similar the name', () => {
    const bridgeNetwork = 'aaaaaaaaaaaa1111111111111111111111111111111111111111111111111111';
    const otherNetwork = 'bbbbbbbbbbbb2222222222222222222222222222222222222222222222222222';
    const nodes = buildTopology({
      ...base,
      interfaces: [dockerBridge(bridgeNetwork, 'monitoring_default', 'br-aaaaaaaaaaaa')],
      containers: [container('grafana', otherNetwork)],
    });
    const net = nodes.find((n) => n.id === `net:${bridgeNetwork}`);
    expect(net?.note).toContain('no container is attached');
  });

  it('ignores a bridge the backend did not attribute to docker', () => {
    const unattributed = {
      ...dockerBridge('x', 'y', 'br-manual'),
      ownership: {
        state: 'unknown', reason: 'evidence_unavailable',
        created_by: null, configured_by: null, evidence_gaps: [],
        adoption: { eligible: false, reason: 'evidence_unavailable' },
      },
    } as unknown as NetworkInterface;
    const nodes = buildTopology({ ...base, interfaces: [unattributed], containers: [] });
    expect(nodes.some((n) => n.id.startsWith('net:'))).toBe(false);
  });

  it('derives a subnet from a global address and its prefix, and ignores link-local', () => {
    const iface = {
      object_id: 'obj_eth0', name: 'eth0', interface_kind: 'ethernet',
      management: { state: 'observed', reason: 'observe_only' },
      ownership: { state: 'attributed', reason: 'x', created_by: null, configured_by: null, evidence_gaps: [], adoption: { eligible: false, reason: 'x' } },
      health: { state: 'healthy', reason: 'up' },
      link: {},
      addresses: [
        { family: 'inet', address: '192.168.1.42', prefix_length: 24, scope: 'global' },
        { family: 'inet', address: '169.254.3.4', prefix_length: 16, scope: 'link' },
      ],
    } as unknown as NetworkInterface;
    const nodes = buildTopology({ ...base, interfaces: [iface], containers: [] });
    expect(nodes.some((n) => n.name === '192.168.1.0/24')).toBe(true);
    expect(nodes.some((n) => n.name.startsWith('169.254'))).toBe(false);
  });

  it('counts only published ports as exposed', () => {
    const withPorts = {
      ...container('web', 'net1'),
      ports: [
        { container_port: 80, protocol: 'tcp', host_ip: '0.0.0.0', host_port: 8080, published: true },
        { container_port: 9000, protocol: 'tcp', host_ip: null, host_port: null, published: false },
      ],
    } as unknown as DockerContainer;
    const nodes = buildTopology({ ...base, interfaces: [], containers: [withPorts] });
    const exposed = nodes.find((n) => n.id === 'exposed');
    expect(exposed?.detail).toBe('1 published');
    expect(exposed?.mark.tone).toBe('attention');
  });

  it('says nothing about what can reach an exposed port', () => {
    const withPorts = {
      ...container('web', 'net1'),
      ports: [{ container_port: 80, protocol: 'tcp', host_ip: '0.0.0.0', host_port: 8080, published: true }],
    } as unknown as DockerContainer;
    const nodes = buildTopology({ ...base, interfaces: [], containers: [withPorts] });
    const exposed = nodes.find((n) => n.id === 'exposed');
    expect(exposed?.mark.description).toMatch(/not established/i);
  });
});

describe('an unread estate', () => {
  it('is stated as unknown rather than rendered as an empty column', () => {
    const nodes = buildTopology({
      ...base,
      interfaces: [],
      containers: [],
      estateRead: { interfaces: true, containers: false },
    });
    const unread = nodes.find((n) => n.id === 'estate-unread');
    expect(unread?.mark.tone).toBe('unknown');
    expect(unread?.note).toMatch(/unknown, not absent/i);
  });

  it('adds no such node when both reads succeeded', () => {
    const nodes = buildTopology({ ...base, interfaces: [], containers: [] });
    expect(nodes.some((n) => n.id === 'estate-unread')).toBe(false);
  });
});

describe('nodes the design draws that no contract supports', () => {
  it('invents no internet, carrier, tailnet or relay node', () => {
    const nodes = buildTopology({
      ...base,
      interfaces: [dockerBridge('n1', 'app_default', 'br-n1')],
      containers: [container('a', 'n1')],
    });
    const names = nodes.map((n) => `${n.name} ${n.kind}`.toLowerCase()).join(' | ');
    for (const invented of ['internet', 'isp', 'carrier', 'tailnet', 'overlay', 'relay', 'derp']) {
      expect(names).not.toContain(invented);
    }
  });
});

describe('the Attached column stays readable', () => {
  it('does not repeat a docker bridge as both a network and a subnet', () => {
    const networkId = 'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888';
    const bridge = {
      ...dockerBridge(networkId, 'app_default', 'br-aaaa1111bbbb'),
      addresses: [{ family: 'inet', address: '172.22.0.1', prefix_length: 16, scope: 'global' }],
    } as unknown as NetworkInterface;

    const nodes = buildTopology({ ...base, interfaces: [bridge], containers: [] });

    expect(nodes.filter((n) => n.id.startsWith('subnet:'))).toHaveLength(0);
    // The subnet is still stated — on the node that already owns the fact.
    expect(nodes.find((n) => n.id === `net:${networkId}`)?.detail).toContain('172.22.0.0/16');
  });

  it('still shows a subnet reached through an interface that is not a docker bridge', () => {
    const iface = {
      object_id: 'obj_eth0', name: 'eth0', interface_kind: 'ethernet',
      management: { state: 'observed', reason: 'observe_only' },
      ownership: { state: 'attributed', reason: 'x', created_by: null, configured_by: null, evidence_gaps: [], adoption: { eligible: false, reason: 'x' } },
      health: { state: 'healthy', reason: 'up' },
      link: {},
      addresses: [{ family: 'inet', address: '192.0.2.5', prefix_length: 24, scope: 'global' }],
    } as unknown as NetworkInterface;
    const nodes = buildTopology({ ...base, interfaces: [iface], containers: [] });
    expect(nodes.some((n) => n.name === '192.0.2.0/24')).toBe(true);
  });

  it('summarises the least busy networks rather than dropping or drawing them all', () => {
    const interfaces = Array.from({ length: 7 }, (_, i) =>
      dockerBridge(`net${i}`.padEnd(64, '0'), `stack${i}_default`, `br-net${i}`),
    );
    // Give the last one a container so ordering is by attachment, not by input order.
    const containers = [container('only', `net6`.padEnd(64, '0'))];
    const nodes = buildTopology({ ...base, interfaces, containers });

    // Three drawn plus one node stating how many were folded: the column stays a diagram
    // and the count stays true.
    const drawn = nodes.filter((n) => n.id.startsWith('net:'));
    expect(drawn).toHaveLength(3);
    expect(drawn[0]?.name).toBe('stack6_default');

    const more = nodes.find((n) => n.id === 'attached:more');
    expect(more?.name).toBe('4 more');
    expect(more?.to).toBe('/network');
  });
});
