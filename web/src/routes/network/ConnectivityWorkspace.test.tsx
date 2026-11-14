import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConnectivityPolicy, ManagementPath, NetworkInterface, ObjectProtection, RouteResult } from '@/api/types';
import { endpoints } from '@/api/endpoints';
import { BACKEND, stubBackend } from '@/test/backend';
import { CONNECTIVITY, DNS, ROUTE, TCP } from '@/test/connectivity';
import { ConnectivityWorkspace } from './ConnectivityWorkspace';
import { useConnectivity } from './useConnectivity';
import { NetworkList } from './NetworkList';

const iface = BACKEND['/api/v1/network/interfaces/obj_if'] as NetworkInterface;
const path = BACKEND['/api/v1/management-path'] as ManagementPath;
const guard = BACKEND['/api/v1/network/interfaces/obj_if/protection'] as ObjectProtection;
function Harness({ current = true, management = path }: { current?: boolean; management?: ManagementPath }): JSX.Element {
  const controller = useConnectivity('obj_if');
  return <MemoryRouter><Routes><Route path="/" element={<ConnectivityWorkspace controller={controller} iface={{ ...iface, observed_in_latest_sweep: current }}
    managementPath={{ status: 'success', data: management, fetchedAt: new Date(), refreshing: false }}
    protection={{ status: 'success', data: guard, fetchedAt: new Date(), refreshing: false }}
  />} /><Route path="/network" element={<NetworkList />} /></Routes></MemoryRouter>;
}

beforeEach(() => {
  stubBackend();
  vi.spyOn(endpoints, 'connectivity').mockResolvedValue(CONNECTIVITY);
  vi.spyOn(endpoints, 'observeConnectivity').mockResolvedValue(CONNECTIVITY);
  vi.spyOn(endpoints, 'connectivityRoute').mockResolvedValue(ROUTE);
  vi.spyOn(endpoints, 'connectivityDiagnostic').mockResolvedValue(DNS);
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function ready(current = true) {
  const view = render(<Harness current={current} />);
  await screen.findByRole('table', { name: 'Installed routes' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Re-read stored evidence' })).toBeEnabled());
  return view;
}

describe('generated snapshot presentation', () => {
  it('shows profile/applied/kernel differences, policy routes/rules and plural resolver facts', async () => {
    await ready();
    expect(within(screen.getByRole('region', { name: 'Profile configuration' })).getByText('192.0.2.30/24')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Applied configuration' })).getByText('192.0.2.20/24')).toBeInTheDocument();
    const routes = screen.getByRole('table', { name: 'Installed routes' });
    expect(within(routes).getByText('::/0')).toBeInTheDocument();
    expect(within(routes).getByText('2001:db8::1')).toBeInTheDocument();
    expect(within(routes).getByText('eth1')).toBeInTheDocument();
    expect(within(routes).getByRole('img', { name: 'not known' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Routing rules' })).toHaveTextContent('192.0.2.0/24');
    expect(screen.getByRole('table', { name: 'Per-link resolver' })).toHaveTextContent('~example.internal');
    expect(screen.getByRole('table', { name: 'Per-link resolver' })).toHaveTextContent('example.internal (route-only: yes)');
    expect(within(routes).getByRole('link', { name: 'eth1' })).toHaveAttribute('href', '/network/obj_egress?tab=connectivity');
    expect(screen.getByText('127.0.0.53')).toBeInTheDocument();
    expect(screen.getByText('optional resolver property absent')).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
    expect(screen.getByText(/Current server (does not prove which server answered|is not proof of the server that answered a diagnostic)/)).toBeInTheDocument();
    expect(screen.getByText(/Management-path and protection statements above come from their independent assessments/)).toBeInTheDocument();
    expect(screen.getByText(/unresolved ·/)).toBeInTheDocument();
  });

  it('keeps kernel evidence when NM is missing and routing evidence unknown', async () => {
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY,
      profile: { evidence: { ...CONNECTIVITY.profile.evidence, status: 'unavailable', completeness: 'unknown', gaps: ['NetworkManager unavailable'] }, value: null },
      routes: { evidence: { ...CONNECTIVITY.routes.evidence, status: 'not_observed' }, value: null },
    });
    render(<Harness />);
    await screen.findByText('NetworkManager unavailable');
    expect(screen.getByText('Route entries unknown; this is not an empty route inventory.')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Observed kernel facts' })).getByText('192.0.2.20/24')).toBeInTheDocument();
  });

  it('separates the current assessment time from old proof time/TTL and retains stored return-route facts', async () => {
    const management: ManagementPath = { ...path, state: 'unresolved', reason: 'stored_proof_stale',
      as_of: '2026-11-10T12:00:00Z', evidence_ttl_seconds: 60,
      transport: { peer_address: '198.51.100.10', peer_family: 'inet', local_endpoint_address: '192.0.2.20', local_endpoint_family: 'inet', usable: true, reason: null },
      evidence: { observation_id: 'proof-old', host_id: 'fixture-host', observed_at: '2026-11-09T09:00:00Z', agent_instance_id: 'fixture-agent', capability: 'fixture.path', provider: 'fixture.route', provider_version: '1', method: 'fixture.netlink', transport_peer_address: '198.51.100.9', local_endpoint_address: '192.0.2.19', family: 'inet',
        route: { status: 'resolved', reason: null, family: 'inet', destination: '198.51.100.9', destination_prefix_length: 32, preferred_source: '192.0.2.19', gateway: '192.0.2.254', oif_index: 4, table: 100, route_type: 'unicast', scope: 'global', protocol: 'static', priority: 100, error: null },
      },
    };
    render(<Harness management={management} />);
    await screen.findByText('unresolved · stored_proof_stale');
    expect(screen.queryByText('Management evidence as of')).not.toBeInTheDocument();
    expect(within(screen.getByText('Assessment as of').parentElement!).getByText('2026-11-10T12:00:00Z')).toBeInTheDocument();
    expect(within(screen.getByText('Proof observed at').parentElement!).getByText('2026-11-09T09:00:00Z')).toBeInTheDocument();
    expect(within(screen.getByText('Proof TTL (seconds)').parentElement!).getByText('60')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Stored management return-route proof'));
    expect(within(screen.getByText('Stored return-route source address').parentElement!).getByText('192.0.2.19')).toBeInTheDocument();
    expect(within(screen.getByText('Stored return-route egress ifindex').parentElement!).getByText('4')).toBeInTheDocument();
    expect(within(screen.getByText('Stored return-route table').parentElement!).getByText('100')).toBeInTheDocument();
    expect(within(screen.getByText('Stored return-route gateway').parentElement!).getByText('192.0.2.254')).toBeInTheDocument();
  });

  it('does not discard known current transport facts when stored proof is absent or confirm a path from them', async () => {
    render(<Harness management={{ ...path, evidence: null, transport: { ...path.transport, peer_address: '198.51.100.10', local_endpoint_address: '192.0.2.20', usable: true } }} />);
    await screen.findByRole('table', { name: 'Installed routes' });
    expect(within(screen.getByText('Current accepted transport peer').parentElement!).getByText('198.51.100.10')).toBeInTheDocument();
    expect(within(screen.getByText('Current accepted local endpoint').parentElement!).getByText('192.0.2.20')).toBeInTheDocument();
    expect(screen.getByText(/No stored return-route proof is present/)).toBeInTheDocument();
    expect(screen.getByText(/unresolved ·/)).toBeInTheDocument();
  });

  it('describes empty configured contributions without inferring global DNS absence or automatic drift', async () => {
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, profile: { ...CONNECTIVITY.profile, value: { provider: 'NetworkManager', state: 'unknown', addresses: [], routes: [], dns_servers: [] } } });
    await ready();
    expect(within(screen.getByRole('region', { name: 'Profile configuration' })).getByText(/empty configured DNS is not global DNS absence/)).toBeInTheDocument();
    expect(screen.getByText('127.0.0.53')).toBeInTheDocument();
    expect(screen.queryByText(/^drifted$/)).not.toBeInTheDocument();
  });
});

describe('policy and deliberate forms', () => {
  it.each(['disabled', 'missing_declaration', 'incomplete_declaration', 'policy_unavailable'] as const)('disables TCP for %s while preserving independent DNS authorization', async (tcp_status) => {
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy: {
      ...CONNECTIVITY.policy, tcp_status, version: tcp_status === 'missing_declaration' ? 1 : 2,
      control_plane_complete: tcp_status === 'incomplete_declaration' ? false : true,
    } });
    await ready();
    await userEvent.selectOptions(screen.getByLabelText('Configured TCP endpoint'), '[2001:db8::20]:443');
    await userEvent.selectOptions(screen.getByLabelText('Authorized DNS name'), DNS.name);
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Resolve name' })).toBeEnabled();
    expect(screen.getByText('Backend TCP policy status').parentElement).toHaveTextContent(tcp_status);
    await userEvent.click(screen.getByRole('button', { name: 'Resolve name' }));
    expect(await screen.findByText('DNS lookup · resolved')).toBeInTheDocument();
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
    expect(vi.mocked(endpoints.connectivityDiagnostic).mock.calls[0]?.[1]).toMatchObject({ kind: 'dns' });
  });

  it('shows count unknown without inventing zero or deny addresses, and withholds TCP on incomplete metadata', async () => {
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, address_count: null } });
    await ready();
    const count = screen.getByText('Declared control-plane address count').parentElement!;
    expect(within(count).getByRole('img', { name: 'not known' })).toBeInTheDocument();
    expect(count).not.toHaveTextContent('0');
    expect(screen.getByText(/Declared control-plane address count is unknown or invalid/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Configured TCP endpoint'), '[2001:db8::20]:443');
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeDisabled();
    expect(endpoints.connectivityDiagnostic).not.toHaveBeenCalled();
  });

  it('shows complete administrator attestation and count as backend policy eligibility, not execution or management proof', async () => {
    await ready();
    expect(screen.getByText('Loaded policy version').parentElement).toHaveTextContent('2');
    expect(screen.getByText('Control-plane declaration').parentElement).toHaveTextContent('Complete — administrator attestation');
    expect(screen.getByText('Declared control-plane address count').parentElement).toHaveTextContent('1');
    expect(screen.getByText(/Backend TCP policy eligibility is ready/)).toHaveTextContent('does not prove current agent agreement');
    expect(screen.getByText(/Configured TCP allow entries may still be rejected/)).toHaveTextContent('fresh agent policy/current local, broadcast and namespace checks occur on each request');
    expect(screen.getByText('Configured TCP allow entries').parentElement).toHaveTextContent('[2001:db8::20]:443');
    expect(screen.getByText(/unresolved ·/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Configured TCP endpoint'), '[2001:db8::20]:443');
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeEnabled();
    expect(endpoints.connectivityDiagnostic).not.toHaveBeenCalled();
  });

  it('retains the configured pair and backend denial detail, rereads policy without a probe, and requires a separate deliberate retry', async () => {
    await ready();
    vi.mocked(endpoints.connectivityDiagnostic).mockResolvedValue({ ...TCP, outcome: 'denied', started_at: null, ended_at: null, local: null, remote: null, detail: 'The declared address-wide control-plane deny set excludes this configured endpoint.' });
    await userEvent.selectOptions(screen.getByLabelText('Configured TCP endpoint'), '[2001:db8::20]:443');
    await userEvent.click(screen.getByRole('button', { name: 'Check TCP endpoint' }));
    await screen.findByText('TCP handshake · denied');
    expect(screen.getByText('The declared address-wide control-plane deny set excludes this configured endpoint.')).toBeInTheDocument();
    expect(screen.getByLabelText('Configured TCP endpoint')).toHaveValue('[2001:db8::20]:443');
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeDisabled();
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, digest: 'b'.repeat(64) } });
    await userEvent.click(screen.getByRole('button', { name: 'Re-read stored evidence' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeEnabled());
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
    expect(screen.getByText('TCP handshake · denied')).toBeInTheDocument();
    expect(screen.getByLabelText('Configured TCP endpoint')).toHaveValue('[2001:db8::20]:443');
    vi.mocked(endpoints.connectivityDiagnostic).mockResolvedValue(TCP);
    await userEvent.click(screen.getByRole('button', { name: 'Check TCP endpoint' }));
    await screen.findByText('TCP handshake · refused');
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(2);
    expect(vi.mocked(endpoints.connectivityDiagnostic).mock.calls[1]?.[1]).toMatchObject({ kind: 'tcp', policy_digest: 'b'.repeat(64) });
  });

  it.each(['missing', 'valid', 'invalid', 'unsafe', 'unreadable'] as const)('handles %s policy without fabricating authorization', async (status) => {
    const policy: ConnectivityPolicy = { status, digest: status === 'valid' ? 'a'.repeat(64) : null, dns: [], tcp: [], tcp_status: status === 'valid' ? 'disabled' : 'policy_unavailable' };
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy });
    await ready();
    if (status === 'missing' || status === 'valid') expect(screen.getByText('No targets configured for active diagnostics')).toBeInTheDocument();
    else {
      expect(screen.queryByText('No targets configured for active diagnostics')).not.toBeInTheDocument();
      expect(screen.getByText(new RegExp(`Policy status: ${status}`))).toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: 'Resolve name' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Numeric route destination'), '2001:db8::20');
    expect(screen.getByRole('button', { name: 'Look up route' })).toBeEnabled();
    expect(endpoints.connectivityDiagnostic).not.toHaveBeenCalled();
  });

  it('keeps the empty DNS and TCP authorities independent in both directions', async () => {
    const view = await ready();
    await userEvent.selectOptions(screen.getByLabelText('Authorized DNS name'), DNS.name);
    expect(screen.getByRole('button', { name: 'Resolve name' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeDisabled();
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, dns: [] } });
    await userEvent.click(screen.getByRole('button', { name: 'Re-read stored evidence' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Resolve name' })).toBeDisabled());
    await userEvent.selectOptions(screen.getByLabelText('Configured TCP endpoint'), '[2001:db8::20]:443');
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeEnabled();
    view.unmount();
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, tcp: [] } });
    await ready();
    await userEvent.selectOptions(screen.getByLabelText('Authorized DNS name'), DNS.name);
    expect(screen.getByRole('button', { name: 'Resolve name' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Check TCP endpoint' })).toBeDisabled();
  });

  it('submits A/AAAA explicitly, shows all bounded answers and never populates TCP from DNS', async () => {
    await ready();
    await userEvent.selectOptions(screen.getByLabelText('Authorized DNS name'), DNS.name);
    await userEvent.selectOptions(screen.getByLabelText('DNS record type'), 'AAAA');
    vi.mocked(endpoints.connectivityDiagnostic).mockResolvedValue({ ...DNS, record_type: 'AAAA', answers: ['2001:db8::20', '2001:db8::21'] });
    await userEvent.click(screen.getByRole('button', { name: 'Resolve name' }));
    expect(await screen.findByText('2001:db8::20, 2001:db8::21')).toBeInTheDocument();
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledWith('obj_if', { kind: 'dns', name: DNS.name, record_type: 'AAAA', policy_digest: CONNECTIVITY.policy.digest });
    expect(screen.getByLabelText('Configured TCP endpoint')).toHaveValue('');
    expect(endpoints.connectivityRoute).not.toHaveBeenCalled();
    expect(within(screen.getByRole('region', { name: 'Destination result' })).getByText('yes')).toBeInTheDocument();
  });

  it('submits the exact IPv6 pair and shows the actual socket tuple without service health claims', async () => {
    await ready();
    vi.mocked(endpoints.connectivityDiagnostic).mockResolvedValue(TCP);
    await userEvent.selectOptions(screen.getByLabelText('Configured TCP endpoint'), '[2001:db8::20]:443');
    expect(screen.getByText('Selected pair:').parentElement).toHaveTextContent('[2001:db8::20]:443');
    await userEvent.click(screen.getByRole('button', { name: 'Check TCP endpoint' }));
    expect(await screen.findByText('TCP handshake · refused')).toBeInTheDocument();
    expect(screen.getByText('[2001:db8::2]:12345')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Destination result' })).getAllByText('[2001:db8::20]:443')).toHaveLength(2);
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
  });

  it('disables duplicates and retains the submitted target while inputs change during pending', async () => {
    await ready();
    let resolve!: (value: RouteResult) => void;
    vi.mocked(endpoints.connectivityRoute).mockReturnValue(new Promise((done) => { resolve = done; }));
    const input = screen.getByLabelText('Numeric route destination');
    await userEvent.type(input, '192.0.2.1');
    await userEvent.dblClick(screen.getByRole('button', { name: 'Look up route' }));
    expect(endpoints.connectivityRoute).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Observe connectivity' })).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, '192.0.2.2');
    expect(screen.getByText('Waiting for result · 192.0.2.1')).toBeInTheDocument();
    await act(async () => { resolve(ROUTE); });
    expect(await screen.findByText('Route lookup · selected')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Destination result' })).getByRole('link', { name: 'eth1' })).toHaveAttribute('href', '/network/obj_egress?tab=connectivity');
    expect(input).toHaveValue('192.0.2.2');
    const result = screen.getByRole('region', { name: 'Destination result' });
    expect(within(result).getByText(/Earlier result/)).toHaveTextContent('submitted target, not necessarily the current selection');
    expect(within(result).getByText('192.0.2.1')).toBeInTheDocument();
  });

  it('keeps an earlier DNS target and record type explicit after the selection changes', async () => {
    await ready();
    await userEvent.selectOptions(screen.getByLabelText('Authorized DNS name'), DNS.name);
    await userEvent.click(screen.getByRole('button', { name: 'Resolve name' }));
    await screen.findByText('DNS lookup · resolved');
    await userEvent.selectOptions(screen.getByLabelText('DNS record type'), 'AAAA');
    const result = screen.getByRole('region', { name: 'Destination result' });
    expect(within(result).getByText(/Earlier result/)).toBeInTheDocument();
    expect(within(result).getByText(`${DNS.name} (A)`)).toBeInTheDocument();
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
  });

  it('retains inputs and last result through a failed subsequent action and stored read', async () => {
    await ready();
    const input = screen.getByLabelText('Numeric route destination');
    await userEvent.type(input, '192.0.2.1');
    await userEvent.click(screen.getByRole('button', { name: 'Look up route' }));
    await screen.findByText('Route lookup · selected');
    vi.mocked(endpoints.connectivityRoute).mockRejectedValue(new Error('fixture route error'));
    await userEvent.click(screen.getByRole('button', { name: 'Look up route' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('fixture route error');
    expect(screen.getByText('Route lookup · selected')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Destination result' })).getByText(/Latest request failed/)).toHaveTextContent('fixture route error');
    expect(screen.getByText(/Earlier result · last completed destination action · 2026-11-09T12:01:00Z/)).toBeInTheDocument();
    expect(input).toHaveValue('192.0.2.1');
    vi.mocked(endpoints.connectivity).mockRejectedValue(new Error('fixture stored error'));
    await userEvent.click(screen.getByRole('button', { name: 'Re-read stored evidence' }));
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(2));
    expect(screen.getByRole('table', { name: 'Installed routes' })).toBeInTheDocument();
  });

  it('keeps a removed policy target visible but disabled until the operator selects a newly authorized target', async () => {
    await ready();
    await userEvent.selectOptions(screen.getByLabelText('Authorized DNS name'), DNS.name);
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, digest: 'b'.repeat(64), dns: ['new.example.internal.'] } });
    await userEvent.click(screen.getByRole('button', { name: 'Re-read stored evidence' }));
    await screen.findByRole('option', { name: /no longer authorized/ });
    expect(screen.getByLabelText('Authorized DNS name')).toHaveValue(DNS.name);
    expect(screen.getByRole('button', { name: 'Resolve name' })).toBeDisabled();
    expect(endpoints.connectivityDiagnostic).not.toHaveBeenCalled();
  });

  it('keeps passive route lookup independent and reaches the correct explicit inventory remedy for stale active-action eligibility', async () => {
    await ready(false);
    await userEvent.type(screen.getByLabelText('Numeric route destination'), '192.0.2.1');
    await userEvent.selectOptions(screen.getByLabelText('Authorized DNS name'), DNS.name);
    expect(screen.getByText(/Selected interface membership or freshness is not current/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Look up route' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Resolve name' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Observe connectivity' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Look up route' }));
    expect(endpoints.connectivityRoute).toHaveBeenCalledWith('obj_if', { destination: '192.0.2.1' });
    expect(await screen.findByText('Route lookup · selected')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Destination result' })).getByRole('link', { name: 'eth1' })).toHaveAttribute('href', '/network/obj_egress?tab=connectivity');
    const refresh = vi.spyOn(endpoints, 'refreshNetwork');
    await userEvent.click(screen.getByRole('link', { name: 'Open Network inventory to observe interfaces' }));
    expect(await screen.findByRole('button', { name: 'Observe network' })).toBeEnabled();
    expect(refresh).not.toHaveBeenCalled();
    expect(endpoints.connectivityDiagnostic).not.toHaveBeenCalled();
  });

  it('announces a new pending target beside retained previous results instead of presenting old success as the new outcome', async () => {
    await ready();
    const input = screen.getByLabelText('Numeric route destination');
    await userEvent.type(input, '192.0.2.1');
    await userEvent.click(screen.getByRole('button', { name: 'Look up route' }));
    await screen.findByText('Route lookup · selected');
    let resolve!: (value: RouteResult) => void;
    vi.mocked(endpoints.connectivityRoute).mockReturnValue(new Promise((done) => { resolve = done; }));
    await userEvent.clear(input);
    await userEvent.type(input, '192.0.2.2');
    await userEvent.click(screen.getByRole('button', { name: 'Look up route' }));
    const result = screen.getByRole('region', { name: 'Destination result' });
    expect(within(result).getByRole('status')).toHaveTextContent('New action pending · route · 192.0.2.2');
    expect(within(result).getByRole('status')).toHaveTextContent('previous evidence, not this attempt');
    expect(within(result).getByText(/Earlier result · last completed destination action · 2026-11-09T12:01:00Z/)).toBeInTheDocument();
    expect(within(result).getByText('192.0.2.1')).toBeInTheDocument();
    await act(async () => { resolve({ ...ROUTE, destination: '192.0.2.2', status: 'unavailable', observed_at: '2026-11-09T12:02:00Z' }); });
    expect(await screen.findByText('Route lookup · unavailable')).toBeInTheDocument();
    expect(within(result).queryByText(/New action pending/)).not.toBeInTheDocument();
  });
});
