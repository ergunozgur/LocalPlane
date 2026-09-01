/**
 * A stand-in backend for route tests.
 *
 * Every body here is shaped like the live responses this host returns — the same fields, the
 * same nulls, the same typed codes. Nothing models a state the backend could not produce:
 * the point of these tests is that the UI reads the real contract correctly, and a fixture
 * that invented a state would prove the opposite of what it claims.
 */
import { vi } from 'vitest';

const SWEEP = {
  sweep_id: 'swp_1', capability: 'network.observe', scope: 'inventory',
  provider: 'linux_network', provider_version: '1', status: 'ok',
  started_at: '2026-08-27T21:47:00Z', completed_at: '2026-08-27T21:47:01Z',
  received_at: '2026-08-27T21:47:01Z', object_count: 1, missing: [], issues: [],
  agent_instance_id: 'agent_1',
};

const OBSERVATION = {
  observation_id: 'obs_1', sweep_id: 'swp_1', observed_at: '2026-08-27T21:47:01Z',
  received_at: '2026-08-27T21:47:01Z', freshness: 'current', age_seconds: 3,
  provider: 'linux_network', provider_version: '1', method: 'rtnetlink',
  capability: 'network.observe', fidelity: 'complete', gaps: [],
};

const INTERFACE = {
  object_id: 'obj_if', kind: 'network.interface', name: 'enx020000000012',
  interface_kind: 'ethernet',
  identity: { basis: 'mac_address', value: '02:00:00:00:00:12', confidence: 'high' },
  management: { state: 'observed', reason: 'observe_only' },
  reconciliation: null, intent: null,
  ownership: {
    state: 'attributed', reason: 'externally_configured',
    created_by: { relation: 'created_by',
      owner: { provider: 'kernel', instance: 'usb/1-1.1:1.0', label: null, version: null },
      confidence: 'confirmed', reason: 'kernel_device_backed', evidence_sources: ['kernel.interface'] },
    configured_by: { relation: 'configured_by',
      owner: { provider: 'networkmanager', instance: 'dddd6252', label: 'service-lan', version: '1.46.0' },
      confidence: 'confirmed', reason: 'networkmanager_active_profile', evidence_sources: ['networkmanager.devices'] },
    evidence_gaps: [],
    adoption: { eligible: false, reason: 'externally_configured', blocked_by: null, evidence_gaps: [] },
  },
  health: { state: 'healthy', reason: 'carrier_up' },
  observation: OBSERVATION, observed_in_latest_sweep: true,
  link: { ifindex: 4, mtu: 1500, mac_address: '02:00:00:00:00:12', mac_is_permanent: true,
    admin_up: true, operstate: 'up', carrier: true, speed_mbps: 1000, duplex: 'full',
    link_kind: null, arphrd_type: 1, is_physical: true, device_path: null, master: null,
    carrier_changes: 3 },
  addresses: [{ family: 'inet', address: '192.168.1.42', prefix_length: 24, scope: 'global',
    dynamic: true, valid_lifetime_s: 3600, preferred_lifetime_s: 1800 }],
  statistics: { rx_bytes: 91234567, tx_bytes: 4567890, rx_packets: 12345, tx_packets: 6789,
    rx_errors: 0, tx_errors: 0, rx_dropped: 2, tx_dropped: 0 },
  first_seen_at: '2026-08-27T21:46:55Z', last_seen_at: '2026-08-27T21:47:11Z',
};

const CONTAINER = {
  object_id: 'obj_ct', kind: 'docker.container', name: 'grafana',
  container_id: 'abc123def4567890', short_id: 'abc123def456',
  identity: { basis: 'container_id', value: 'abc123def4567890', confidence: 'high' },
  management: { state: 'observed', reason: 'observe_only' },
  ownership: { state: 'attributed', reason: 'externally_created', created_by: null,
    configured_by: null, evidence_gaps: [],
    adoption: { eligible: false, reason: 'externally_created', blocked_by: null, evidence_gaps: [] } },
  health: { state: 'inactive', reason: 'exited' },
  observation: { ...OBSERVATION, capability: 'docker.containers.observe', provider: 'docker' },
  observed_in_latest_sweep: true,
  image: { reference: 'grafana/grafana-oss:latest', image_id: 'sha256:deadbeef' },
  created_at: '2026-08-20T10:00:00Z',
  runtime: { state: 'exited', running: false, paused: false, restarting: false, exit_code: 0,
    error: null, oom_killed: false, pid: null, started_at: '2026-08-20T10:00:05Z',
    finished_at: '2026-08-25T08:00:00Z', restart_count: 0 },
  container_health: { checked: false, status: null, failing_streak: null },
  restart_policy: { name: 'unless-stopped', maximum_retry_count: 0 },
  network_mode: 'bridge',
  networks: [{ name: 'bridge', network_id: 'net1', ip_address: '172.17.0.2', ipv6_address: null,
    gateway: '172.17.0.1', mac_address: null, aliases: [] }],
  ports: [{ container_port: 3000, protocol: 'tcp', host_ip: '0.0.0.0', host_port: 3000, published: true }],
  mounts: [{ type: 'volume', name: 'grafana-data', source: '/var/lib/docker/volumes/grafana-data/_data',
    destination: '/var/lib/grafana', driver: 'local', mode: 'z', read_write: true, propagation: '' }],
  labels: {
    'com.docker.compose.project': 'monitoring',
    'com.docker.compose.service': 'grafana',
    'com.docker.compose.container-number': '1',
    'com.docker.compose.project.config_files': '/srv/compose/monitoring/docker-compose.yml',
    'com.docker.compose.project.working_dir': '/srv/compose/monitoring',
    'com.docker.compose.config-hash': 'abc123',
  },
  labels_dropped: 3,
  log_driver: 'json-file', platform: 'linux',
  first_seen_at: '2026-08-27T21:46:55Z', last_seen_at: '2026-08-27T21:47:11Z',
};

const UNIT = {
  object_id: 'obj_unit', kind: 'systemd.unit', canonical_id: 'ModemManager.service',
  names: ['ModemManager.service'], description: 'Modem Manager', unit_type: 'service',
  identity: { basis: 'unit_id', value: 'ModemManager.service', confidence: 'high' },
  management: { state: 'observed', reason: 'observe_only' },
  health: { state: 'healthy', reason: 'active_running' },
  observation: { ...OBSERVATION, capability: 'systemd.units.observe', provider: 'systemd' },
  observed_in_latest_sweep: true,
  load_state: 'loaded', active_state: 'active', sub_state: 'running',
  unit_file_state: 'enabled', unit_file_preset: 'enabled',
  can_start: true, can_stop: true, can_reload: false,
  refuse_manual_start: false, refuse_manual_stop: false, need_daemon_reload: false,
  fragment_path: '/usr/lib/systemd/system/ModemManager.service', source_path: null,
  drop_in_paths: null, transient: false, template: null, current_job: null, invocation_id: 'inv_1',
  timestamps: { ActiveEnterTimestamp: 1756330000000000, InactiveEnterTimestamp: 0 },
  relationships: [
    { kind: 'After', group: 'ordering', target_unit: 'dbus.socket', canonical_target: 'dbus.socket',
      target_object_id: 'obj_dbus', resolution: 'resolved', estate_state: 'current', source: 'manager' },
    { kind: 'Wants', group: 'requirement', target_unit: 'network.target', canonical_target: null,
      target_object_id: null, resolution: 'referenced', estate_state: 'not_observed', source: 'manager' },
  ],
  service: { type: 'dbus', main_pid: 900, restart: 'on-failure' },
  first_seen_at: '2026-08-27T21:46:55Z', last_seen_at: '2026-08-27T21:47:11Z',
};

/** A change that wrote, could not prove it, and is holding a lock. The interesting case. */
const CHANGE = {
  change_id: 'chg_1', run_id: 'run_1', preview_id: 'pv_1', checkpoint_id: 'ckpt_1',
  host_id: 'host_1', object_id: 'obj_if', object_name: 'enx020000000012',
  operation: 'network.interface.reconcile_mtu', change_kind: 'field', field: 'mtu',
  before_value: 1500, desired_value: 1400, action: null, observed_state: null, expected_state: null,
  created_at: '2026-08-27T21:50:00Z', host_effect: 'write_unknown', host_mutated: false,
  mutation: { outcome: 'write_unknown', reason: 'dispatch_began_no_response', provider: 'helper',
    method: 'set_mtu', attempt_id: 'att_1', dispatch_began_at: '2026-08-27T21:50:01Z',
    settled_at: null, detail: {} },
  verification: { outcome: 'observation_unavailable', observation_id: null, observed_value: null,
    expected_value: 1400, observed_state: null, expected_state: null, reason: 'agent_unreachable' },
  rollback: { required: false, attempt_id: null, dispatch_began_at: null, outcome: null,
    reason: 'no_rollback_attempted', restores_value: 1500,
    verification: { outcome: 'not_attempted', observation_id: null, observed_value: null,
      expected_value: null, observed_state: null, expected_state: null, reason: null },
    detail: {} },
  recovery: { required: true, state: 'unresolved', reason: 'write_unknown_unverified',
    known: { before_value: 1500, desired_value: 1400 },
    unknown: ['whether_the_write_reached_the_kernel'], object_write_locked: true,
    released_at: null, released_by: null, released_by_attempt_id: null, last_observed: {},
    attempts: [], authority: null, available_actions: ['retry', 'resolve'] },
  result: 'recovery_required', finished_at: '2026-08-27T21:50:30Z',
  events: [{ sequence: 1, event: 'change_opened', state_from: null, state_to: 'in_flight',
    occurred_at: '2026-08-27T21:50:01Z', change_id: 'chg_1', detail: {} }],
};

/** A drift finding, shaped as the backend publishes one, with typed evidence. */
const DRIFT_FINDING = {
  finding_id: 'find_1',
  finding_key: 'drift:obj_if:mtu',
  host_id: 'host_1',
  object_id: 'obj_if',
  object_name: 'enx020000000012',
  finding_type: 'drift',
  subject: 'mtu',
  status: 'open',
  summary: 'enx020000000012 mtu differs from its intent',
  evidence: {
    intent_id: 'int_1',
    field: 'mtu',
    intended: { type: 'integer', value: 1400 },
    observed: { type: 'integer', value: 1500 },
    comparison: 'differs',
    reason: 'observed_differs_from_intended',
    observation: 'obs_1',
    sweep: 'swp_1',
  },
  first_seen_at: '2026-08-20T10:00:00Z',
  last_seen_at: '2026-08-28T08:00:00Z',
  updated_at: '2026-08-28T08:00:00Z',
  resolved_at: null,
  resolution: null,
};

export const BACKEND: Record<string, unknown> = {
  '/api/v1/session': {
    authenticated: true, mechanism: 'session', expires_at: '2026-08-28T09:47:11Z',
  },
  '/api/v1/status': { status: 'ok', version: '0.1.0', database: { path: '/x', schema_versions: [12] } },
  '/api/v1/host': {
    host_id: 'host_1', identity_basis: 'machine_id', identity_confidence: 'high',
    hostname: 'demo-host', configured_hostname: 'demo-host', boot_id: 'b1',
    os_id: 'ubuntu', os_version_id: '24.04', os_pretty_name: 'Ubuntu 24.04.4 LTS',
    kernel_name: 'Linux', kernel_release: '6.8.0-1060-raspi', architecture: 'aarch64',
    identity_gaps: [], first_seen_at: '2026-08-27T21:46:55Z', last_seen_at: '2026-08-27T21:47:11Z',
    freshness: 'current', age_seconds: 0.2,
  },
  '/api/v1/agent': {
    reachable: true, source: 'live', as_of: '2026-08-27T21:47:11Z',
    agent: { agent_instance_id: 'agent_1', agent_version: '0.1.0', protocol_version: '1',
      transport: 'unix', process_isolated: true, privilege: 'unprivileged', effective_uid: 1000,
      pid: 1, started_at: '2026-08-27T21:46:40Z', last_contact_at: '2026-08-27T21:47:11Z' },
    socket: '/run/localplane/agent.sock',
  },
  '/api/v1/agent/capabilities': {
    reachable: true, source: 'live', as_of: '2026-08-27T21:47:11Z', agent_instance_id: 'agent_1',
    capabilities: [
      { capability: 'systemd.service.lifecycle', version: 1, status: 'available', mutating: true,
        summary: '', reason: null, detail: {}, discovered_at: '2026-08-27T21:46:40Z' },
      { capability: 'network.interface.set_mtu', version: 1, status: 'unavailable', mutating: true,
        summary: '', reason: 'helper_unavailable', detail: {}, discovered_at: '2026-08-27T21:46:40Z' },
    ],
  },
  '/api/v1/management-path': {
    host_id: 'host_1', state: 'unresolved', object_id: null, object_name: null,
    reason: 'transport_peer_local', missing_evidence: ['session.peer', 'route.observe'],
    transport: { peer_address: '127.0.0.1', peer_family: 'inet', local_endpoint_address: '127.0.0.1',
      local_endpoint_family: 'inet', usable: false, reason: 'transport_peer_local' },
    evidence: null, evidence_ttl_seconds: 60, as_of: '2026-08-27T21:47:11Z',
  },
  '/api/v1/observations/sweeps': { host_id: 'host_1', count: 1, sweeps: [SWEEP] },
  '/api/v1/network/interfaces': { host_id: 'host_1', last_sweep: SWEEP, count: 1, interfaces: [INTERFACE] },
  '/api/v1/network/interfaces/obj_if': INTERFACE,
  '/api/v1/network/interfaces/obj_if/protection': {
    object_id: 'obj_if', object_name: 'enx020000000012', status: 'unknown', reasons: [],
    unresolved: ['management_path'], management_path: 'unknown', reason: 'transport_peer_local',
    missing_evidence: ['session.peer', 'route.observe'],
    assessed: [{ reason: 'management_path', status: 'unknown', detail: 'transport_peer_local',
      evidence_id: null, observed_at: null }],
    implemented_reasons: ['management_path'],
    note: 'Protection is a different axis from ownership. `clear` is scoped to the reasons this build implements and is not a word for `safe`.',
    as_of: '2026-08-27T21:48:06Z',
  },
  '/api/v1/network/interfaces/obj_if/provenance': {
    object_id: 'obj_if', name: 'enx020000000012',
    management: { state: 'observed', reason: 'observe_only' },
    state: 'attributed', reason: 'externally_configured',
    claims: [{ relation: 'configured_by',
      owner: { provider: 'networkmanager', instance: 'dddd6252', label: 'service-lan', version: '1.46.0' },
      confidence: 'confirmed', reason: 'networkmanager_active_profile', evidence: [] }],
    sources: [
      { source: 'kernel.interface', provider: 'kernel', status: 'ok', outcome: 'kernel_device_backed',
        gap: false, observed_at: '2026-08-27T21:47:01Z' },
      { source: 'tailscale.status', provider: 'tailscale', status: 'absent', outcome: 'not_a_tunnel_link',
        gap: false, observed_at: null },
    ],
    adoption: { eligible: false, reason: 'externally_configured', evidence_gaps: [] },
    observation: null, as_of: '2026-08-27T21:48:06Z',
  },
  '/api/v1/network/interfaces/obj_if/evidence': {
    object_id: 'obj_if', observation_id: 'obs_1', observed_at: '2026-08-27T21:47:01Z',
    evidence: { sysfs_path: '/sys/class/net/enx020000000012', sysfs: { mtu: '1500' } },
  },
  '/api/v1/docker/containers': {
    host_id: 'host_1',
    // Its own sweep: the docker estate is read by a different provider than the network one.
    last_sweep: {
      ...SWEEP,
      sweep_id: 'swp_docker',
      capability: 'docker.containers.observe',
      provider: 'docker',
      provider_version: '29.1.3',
    },
    count: 1,
    containers: [CONTAINER],
  },
  '/api/v1/docker/containers/obj_ct': CONTAINER,
  '/api/v1/systemd/units': { host_id: 'host_1', capability: null, last_sweep: SWEEP, count: 1, units: [UNIT] },
  '/api/v1/systemd/units/obj_unit': UNIT,
  '/api/v1/findings': {
    host_id: 'host_1',
    status: 'open',
    count: 1,
    findings: [DRIFT_FINDING],
  },
  '/api/v1/findings/find_1': DRIFT_FINDING,
  '/api/v1/runs': { host_id: 'host_1', state: null, count: 0, runs: [] },
  '/api/v1/changes': { host_id: 'host_1', count: 1, changes: [
    { change_id: 'chg_1', run_id: 'run_1', object_id: 'obj_if', object_name: 'enx020000000012',
      operation: 'network.interface.reconcile_mtu', change_kind: 'field', field: 'mtu',
      before_value: 1500, desired_value: 1400, action: null, expected_state: null,
      created_at: '2026-08-27T21:50:00Z', finished_at: '2026-08-27T21:50:30Z',
      host_effect: 'write_unknown', mutation_outcome: 'write_unknown',
      verification_outcome: 'observation_unavailable', rollback_outcome: null,
      result: 'recovery_required', recovery_required: true,
      recovery_reason: 'write_unknown_unverified', recovery_state: 'unresolved' },
  ] },
  '/api/v1/changes/chg_1': CHANGE,
};

/**
 * The path a `fetch` call targets.
 *
 * `fetch` accepts a string, a `URL` or a `Request`, and only the first stringifies usefully.
 * The query string is dropped: routing here is by path, and the client's parameters are
 * asserted separately in the client's own tests.
 */
export function urlOf(input: RequestInfo | URL): string {
  const raw =
    typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return raw.split('?')[0] ?? '';
}

/** Install the stand-in. Any path not listed answers 404, as the backend would. */
export function stubBackend(overrides: Record<string, unknown> = {}): void {
  const table = { ...BACKEND, ...overrides };
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>((input) => {
      const url = urlOf(input);
      const body = table[url];
      if (body === undefined) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ error: { code: 'object_not_found', message: 'no such object', detail: {} } }),
            { status: 404 },
          ),
        );
      }
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
    }),
  );
}
