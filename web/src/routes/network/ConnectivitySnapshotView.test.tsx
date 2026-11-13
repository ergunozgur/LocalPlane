import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import type { ConnectivitySnapshot } from '@/api/types';
import { FAMILY_CONNECTIVITY } from '@/test/connectivity';
import { ConnectivitySnapshotView } from './ConnectivitySnapshotView';

afterEach(() => vi.restoreAllMocks());
const readAt = new Date('2026-11-10T12:00:00Z');
function view(snapshot = FAMILY_CONNECTIVITY) {
  return <MemoryRouter><ConnectivitySnapshotView snapshot={snapshot} readAt={readAt} /></MemoryRouter>;
}
function row(region: HTMLElement, label: string) {
  return within(region).getByText(label).parentElement!;
}
async function openFamily(region: HTMLElement, family: 'IPv4' | 'IPv6', method: string) {
  await userEvent.click(within(region).getByText(`${family} configuration · ${method}`));
}

it('keeps automatic empty IPv4 configuration distinct from observed DNS, addresses and metrics', async () => {
  render(view());
  const profile = screen.getByRole('region', { name: 'Profile configuration' });
  const applied = screen.getByRole('region', { name: 'Applied configuration' });
  await openFamily(profile, 'IPv4', 'auto');
  await openFamily(applied, 'IPv4', 'auto');
  const v4 = screen.getByRole('region', { name: 'Profile IPv4 settings' });
  const appliedV4 = screen.getByRole('region', { name: 'Applied IPv4 settings' });
  expect(row(v4, 'Family DNS contribution')).toHaveTextContent('None reported');
  expect(row(v4, 'Ignore automatic DNS')).toHaveTextContent('no');
  expect(row(v4, 'Configured route metric')).toHaveTextContent('configured -1; effective metric unknown');
  expect(row(v4, 'Configured route metric')).not.toHaveTextContent(/\b0\b/);
  expect(row(v4, 'DNS priority')).toHaveTextContent('100');
  expect(row(appliedV4, 'DNS priority')).toHaveTextContent('200');
  expect(within(v4).getByText(/Automatic configuration can supply runtime addresses, routes and DNS/)).toBeInTheDocument();
  expect(within(profile).getByText(/empty configured DNS is not global DNS absence/)).toBeInTheDocument();
  const kernel = screen.getByRole('region', { name: 'Observed kernel facts' });
  expect(kernel).toHaveTextContent('192.168.50.20/24');
  const routes = screen.getByRole('table', { name: 'Installed routes' });
  expect(routes).toHaveTextContent('100');
  expect(screen.getByRole('table', { name: 'Per-link resolver' })).toHaveTextContent('192.168.50.1');
  expect(screen.queryByText(/^drifted$/i)).not.toBeInTheDocument();
});

it('shows independent manual IPv6 settings, signed DNS priority and unknown applied metrics without inventing defaults', async () => {
  render(view());
  await openFamily(screen.getByRole('region', { name: 'Profile configuration' }), 'IPv6', 'manual');
  await openFamily(screen.getByRole('region', { name: 'Applied configuration' }), 'IPv6', 'manual');
  const v6 = screen.getByRole('region', { name: 'Profile IPv6 settings' });
  const appliedV6 = screen.getByRole('region', { name: 'Applied IPv6 settings' });
  expect(row(v6, 'Gateway')).toHaveTextContent('fd00::1');
  expect(row(v6, 'Configured route metric')).toHaveTextContent('600');
  expect(row(v6, 'DNS priority')).toHaveTextContent('-50');
  expect(row(v6, 'Ignore automatic DNS')).toHaveTextContent('yes');
  expect(row(v6, 'Family DNS contribution')).toHaveTextContent('fd00::53');
  expect(row(appliedV6, 'Family DNS contribution')).toHaveTextContent('fd00::54');
  for (const label of ['Configured route metric', 'DNS priority', 'Ignore automatic DNS']) {
    expect(within(row(appliedV6, label)).getByRole('img', { name: 'not known' })).toBeInTheDocument();
    expect(row(appliedV6, label)).not.toHaveTextContent('0');
  }
});

it('keeps authoritative provider, UUID, device, host, agent, version and dated source context', () => {
  render(view());
  const profile = screen.getByRole('region', { name: 'Profile configuration' });
  expect(row(profile, 'Provider')).toHaveTextContent('NetworkManager');
  expect(row(profile, 'Profile UUID')).toHaveTextContent('profile-a');
  expect(row(profile, 'Device ifindex')).toHaveTextContent('2');
  expect(row(profile, 'Device identity')).toHaveTextContent('/device/2');
  expect(row(profile, 'Provider version')).toHaveTextContent('1.46.0');
  expect(row(profile, 'Profile version')).toHaveTextContent('profile-generation-9');
  expect(row(profile, 'Host identity')).toHaveTextContent('fixture-host');
  expect(row(profile, 'Agent instance')).toHaveTextContent('fixture-agent');
  expect(row(profile, 'Observed at')).toHaveTextContent('2026-11-09T12:00:00Z');
  expect(row(profile, 'Freshness')).toHaveTextContent('stale');
});

it('keeps extended selectors discoverable per rule, including zero, masks, interfaces and UID ranges', async () => {
  render(view());
  const table = screen.getByRole('table', { name: 'Routing rules' });
  const rows = within(table).getAllByRole('row').slice(1);
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent('192.168.50.0/24');
  expect(rows[0]).toHaveTextContent('10.0.0.0/8');
  expect(rows[1]).toHaveTextContent('fd00::/64');
  expect(rows[1]).toHaveTextContent('2001:db8::/32');
  for (const rule of rows) {
    const summary = within(rule).getByText('Rule selectors');
    expect(summary.closest('details')).not.toHaveAttribute('open');
    await userEvent.click(summary);
    expect(summary.closest('details')).toHaveAttribute('open');
  }
  expect(row(rows[0]!, 'Firewall mark')).toHaveTextContent('0x0');
  expect(row(rows[0]!, 'Firewall mask')).toHaveTextContent('0xffffffff');
  expect(row(rows[0]!, 'Incoming interface')).toHaveTextContent('eth0');
  expect(row(rows[0]!, 'Outgoing interface')).toHaveTextContent('eth1');
  expect(row(rows[0]!, 'UID range')).toHaveTextContent('1000-1999');
  expect(row(rows[0]!, 'Suppress prefix length')).toHaveTextContent('0');
  expect(row(rows[1]!, 'Suppress prefix length')).toHaveTextContent('64');
  expect(within(row(rows[1]!, 'Incoming interface')).getByRole('img', { name: 'not known' })).toBeInTheDocument();
});

it('does not promote a resolver current server with unknown scope to global or answering-server evidence', () => {
  render(view());
  const resolver = screen.getByRole('region', { name: 'Resolver evidence' });
  expect(row(resolver, 'Resolver-selected current server')).toHaveTextContent('192.168.50.1');
  expect(row(resolver, 'Global DNS servers')).toHaveTextContent('192.168.50.54');
  expect(row(resolver, 'Global DNS servers')).not.toHaveTextContent('192.168.50.1');
  expect(within(row(resolver, 'Current-server scope ifindex')).getByRole('img', { name: 'not known' })).toBeInTheDocument();
  expect(resolver).toHaveTextContent('current-server scope not exposed');
  expect(resolver).toHaveTextContent('stale');
  expect(resolver).toHaveTextContent('server that answered a diagnostic');
  const links = within(resolver).getByRole('table', { name: 'Per-link resolver' });
  expect(links).toHaveTextContent('ops.example.internal (route-only: yes)');
  expect(links).toHaveTextContent('lab.example.internal (route-only: no)');
  expect(links).toHaveTextContent('eth0');
  expect(links).toHaveTextContent('eth1');
});

it.each([0, 3])('preserves an explicitly supplied current-server scope ifindex %i', (scope) => {
  render(view({ ...FAMILY_CONNECTIVITY, resolver: { ...FAMILY_CONNECTIVITY.resolver, value: { ...FAMILY_CONNECTIVITY.resolver.value, current_server_ifindex: scope } } }));
  const resolver = screen.getByRole('region', { name: 'Resolver evidence' });
  expect(row(resolver, 'Current-server scope ifindex')).toHaveTextContent(String(scope));
  expect(within(row(resolver, 'Current-server scope ifindex')).queryByRole('img')).not.toBeInTheDocument();
});

it('leaves missing installed metrics unknown and never guesses identity links from names', () => {
  render(view());
  const routes = screen.getByRole('table', { name: 'Installed routes' });
  const ipv6 = within(routes).getAllByRole('row')[2]!;
  const metric = within(ipv6).getAllByRole('cell')[4]!;
  expect(within(metric).getByRole('img', { name: 'not known' })).toBeInTheDocument();
  expect(metric).not.toHaveTextContent('0');
  expect(within(ipv6).queryByRole('link')).not.toBeInTheDocument();
  expect(within(routes).getByRole('link', { name: 'eth0' })).toHaveAttribute('href', '/network/obj_if?tab=connectivity');
});

it('handles missing method/family data as unknown, not as disabled configuration', async () => {
  const snapshot: ConnectivitySnapshot = { ...FAMILY_CONNECTIVITY,
    profile: { ...FAMILY_CONNECTIVITY.profile, value: { ...FAMILY_CONNECTIVITY.profile.value!, ip_settings: [{ family: 'ipv4', method: null }] } },
    applied: { ...FAMILY_CONNECTIVITY.applied, value: { ...FAMILY_CONNECTIVITY.applied.value!, ip_settings: null } },
  };
  render(view(snapshot));
  await openFamily(screen.getByRole('region', { name: 'Profile configuration' }), 'IPv4', 'method unknown');
  const v4 = screen.getByRole('region', { name: 'Profile IPv4 settings' });
  expect(within(row(v4, 'Method')).getByRole('img', { name: 'not known' })).toBeInTheDocument();
  expect(screen.getByText('IP-family settings not observed; configuration methods remain unknown.')).toBeInTheDocument();
});

it('reading, disclosing, rerendering and policy changes in the snapshot presentation make no network requests', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch');
  const { rerender, unmount } = render(view());
  await openFamily(screen.getByRole('region', { name: 'Profile configuration' }), 'IPv4', 'auto');
  rerender(view({ ...FAMILY_CONNECTIVITY, policy: { status: 'missing', digest: null, dns: [], tcp: [], tcp_status: 'policy_unavailable' } }));
  unmount();
  expect(fetch).not.toHaveBeenCalled();
});
