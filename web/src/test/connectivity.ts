/** Controlled generated-contract fixtures, never live observations. */
import type { ConnectivityEvidence, ConnectivitySnapshot, DnsResult, RouteResult, TcpResult } from '@/api/types';

export const CONNECTIVITY_EVIDENCE: ConnectivityEvidence = {
  source: 'fixture.provider', observed_at: '2026-11-09T12:00:00Z', freshness: 'current',
  completeness: 'complete', status: 'observed', gaps: [], vantage: 'fixture network namespace', provider_version: 'fixture-1', host_id: 'fixture-host', agent_instance_id: 'fixture-agent',
};
const e = CONNECTIVITY_EVIDENCE;
export const CONNECTIVITY: ConnectivitySnapshot = {
  object_id: 'obj_if',
  profile: { evidence: { ...e, source: 'fixture.nm.profile' }, value: {
    provider: 'NetworkManager', uuid: 'profile-a', name: 'LAN', state: 'unsaved',
    device_identity: '/device/2', active_connection_identity: '/active/7',
    addresses: [{ address: '192.0.2.30', prefix: 24 }], dns_servers: ['192.0.2.53'], search_domains: ['example.internal'], routes: [],
  } },
  applied: { evidence: { ...e, source: 'fixture.nm.applied' }, value: {
    provider: 'NetworkManager', version: '7', addresses: [{ address: '192.0.2.20', prefix: 24 }], dns_servers: [], search_domains: [], routes: [],
  } },
  kernel: { evidence: e, value: { addresses: [{ address: '192.0.2.20', prefix: 24 }] } },
  routes: { evidence: e, value: [{ family: 'ipv6', destination: '::/0', table: '100', route_type: 'unicast', protocol: 'static', scope: 'global', metric: null,
    nexthops: [{ gateway: '2001:db8::1', interface: 'eth1', ifindex: 3, object_id: 'obj_egress', weight: 1 }] }] },
  rules: { evidence: e, value: [{ family: 'ipv4', priority: 100, source: '192.0.2.0/24', destination: null, table: '100', action: 'lookup' }] },
  resolver: { evidence: { ...e, completeness: 'partial', freshness: 'stale', gaps: ['optional resolver property absent'] }, value: {
    global_servers: ['192.0.2.54'], global_domains: ['~.'], global_routing_domains: [{ name: '.', route_only: true }], links: [{ interface: 'eth0', servers: ['192.0.2.53'], domains: ['~example.internal'], routing_domains: [{ name: 'example.internal', route_only: true }], current_server: null, dns_default_route: false }],
  } },
  client_resolver: { evidence: e, value: { client_nameservers: ['127.0.0.53'], client_mode: 'stub' } },
  policy: { status: 'valid', digest: 'a'.repeat(64), dns: ['nas.example.internal.'], tcp: [{ address: '2001:db8::20', port: 443 }], version: 2, control_plane_complete: true, address_count: 1, tcp_status: 'ready' },
};
export const ROUTE: RouteResult = { object_id: 'obj_if', destination: '192.0.2.1', status: 'selected', source: '192.0.2.20', gateway: '192.0.2.254', egress_interface: 'eth1', egress_ifindex: 3, egress_object_id: 'obj_egress', query_context: 'Unmarked fixture lookup; no source or interface binding.', table: '100', route_type: 'unicast', metric: null, observed_at: '2026-11-09T12:01:00Z', detail: 'Fixture kernel selection; no packets.' };
export const DNS: DnsResult = { kind: 'dns', name: 'nas.example.internal.', record_type: 'A', outcome: 'resolved', vantage: 'fixture agent', started_at: '2026-11-09T12:01:00Z', ended_at: '2026-11-09T12:01:01Z', answers: ['192.0.2.20', '192.0.2.21'], truncated: true, detail: 'Fixture answers, not live.' };
export const TCP: TcpResult = { kind: 'tcp', address: '2001:db8::20', port: 443, outcome: 'refused', vantage: 'fixture agent', started_at: '2026-11-09T12:01:00Z', ended_at: '2026-11-09T12:01:01Z', local: { address: '2001:db8::2', port: 12345 }, remote: { address: '2001:db8::20', port: 443 }, detail: 'Fixture refusal; rejector unknown.' };

/** Automatic IPv4 contributions coexist with explicit IPv6 configuration. */
export const FAMILY_CONNECTIVITY: ConnectivitySnapshot = {
  ...CONNECTIVITY,
  profile: { evidence: { ...e, source: 'fixture.nm.profile', provider_version: '1.46.0', freshness: 'stale' }, value: {
    ...CONNECTIVITY.profile.value!, version: 'profile-generation-9', device_ifindex: 2,
    addresses: [{ address: 'fd00::20', prefix: 64 }], dns_servers: [], routes: [],
    ip_settings: [
      { family: 'ipv4', method: 'auto', gateway: null, route_metric: -1, dns_priority: 100, ignore_auto_dns: false, dns_servers: [] },
      { family: 'ipv6', method: 'manual', gateway: 'fd00::1', route_metric: 600, dns_priority: -50, ignore_auto_dns: true, dns_servers: ['fd00::53'] },
    ],
  } },
  applied: { evidence: { ...e, source: 'fixture.nm.applied' }, value: {
    ...CONNECTIVITY.applied.value!, version: '8',
    ip_settings: [
      { family: 'ipv4', method: 'auto', gateway: null, route_metric: -1, dns_priority: 200, ignore_auto_dns: false, dns_servers: [] },
      { family: 'ipv6', method: 'manual', gateway: 'fd00::1', route_metric: null, dns_priority: null, ignore_auto_dns: null, dns_servers: ['fd00::54'] },
    ],
  } },
  kernel: { evidence: e, value: { addresses: [{ address: '192.168.50.20', prefix: 24 }, { address: 'fd00::20', prefix: 64 }] } },
  routes: { evidence: e, value: [
    { family: 'ipv4', destination: '0.0.0.0/0', table: '254', metric: 100, route_type: 'unicast', protocol: 'dhcp', nexthops: [{ gateway: '192.168.50.1', interface: 'eth0', ifindex: 2, object_id: 'obj_if' }] },
    { family: 'ipv6', destination: '::/0', table: '200', metric: null, route_type: 'unicast', protocol: 'static', nexthops: [{ gateway: 'fd00::1', interface: 'eth1', ifindex: 3, object_id: null }] },
  ] },
  rules: { evidence: e, value: [
    { family: 'ipv4', priority: 100, source: '192.168.50.0/24', destination: '10.0.0.0/8', table: '100', action: 'lookup', fwmark: '0x0', fwmask: '0xffffffff', incoming_interface: 'eth0', outgoing_interface: 'eth1', uid_range: '1000-1999', suppress_prefix_length: 0 },
    { family: 'ipv6', priority: 100, source: 'fd00::/64', destination: '2001:db8::/32', table: '200', action: 'lookup', fwmark: '0x10', fwmask: '0xff', incoming_interface: null, outgoing_interface: 'eth2', uid_range: null, suppress_prefix_length: 64 },
  ] },
  resolver: { evidence: { ...e, source: 'fixture.resolve1', freshness: 'stale', completeness: 'partial', gaps: ['current-server scope not exposed'] }, value: {
    global_servers: ['192.168.50.54'], current_server: '192.168.50.1', current_server_ifindex: null,
    global_routing_domains: [{ name: '.', route_only: true }],
    links: [
      { interface: 'eth0', servers: ['192.168.50.1'], current_server: null, routing_domains: [{ name: 'ops.example.internal', route_only: true }], dns_default_route: true },
      { interface: 'eth1', servers: ['fd00::53'], current_server: 'fd00::53', routing_domains: [{ name: 'lab.example.internal', route_only: false }], dns_default_route: false },
    ],
  } },
};
