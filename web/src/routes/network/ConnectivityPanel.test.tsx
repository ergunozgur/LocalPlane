import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import {
  ConnectivityEvidencePlate, ConnectivityPanel, ConnectivityPolicy, ConnectivityResult,
  type ConnectivityActionEvidence, type ConnectivityEvidence,
} from './ConnectivityPanel';

// Controlled presentation fixtures, not claimed API payloads or live observations.
const evidence: ConnectivityEvidence = {
  state: 'complete', source: 'fixture.kernel', observedAt: '2026-11-09T12:00:00Z',
  age: '20 seconds', freshness: 'stale', detail: 'Stored fixture observation.', gaps: [],
  facts: [{ label: 'Address', value: '192.0.2.20/24' }],
};
const action: ConnectivityActionEvidence = {
  kind: 'route', target: '192.0.2.1', outcome: 'selected', detail: 'Kernel selection only; no packets sent.',
  source: 'fixture.netlink', vantage: 'fixture host', startedAt: '2026-11-09T12:01:00Z',
  completedAt: '2026-11-09T12:01:01Z', egress: { name: 'eth1', objectId: 'if_egress' },
  facts: [{ label: 'Source address', value: '192.0.2.20' }, { label: 'Metric', value: null }],
};

describe('connectivity comparison evidence', () => {
  it('keeps independent source times, identities and disagreement without inventing drift', () => {
    render(<MemoryRouter><ConnectivityPanel selectedInterface="eth0" comparison={{
      profile: { ...evidence, source: 'fixture.profile', observedAt: '2026-11-09T11:00:00Z', age: '1 hour',
        facts: [{ label: 'Profile UUID', value: 'profile-a' }, { label: 'Address', value: '192.0.2.30/24' }] },
      applied: { ...evidence, source: 'fixture.applied', facts: [{ label: 'Applied version', value: '7' }] },
      kernel: evidence, resolver: { ...evidence, state: 'partial', source: 'fixture.resolver',
        gaps: ['upstream server not observed'], facts: [{ label: 'Client stub', value: '127.0.0.53' }] },
      routes: evidence, rules: evidence,
    }} /></MemoryRouter>);
    const profile = screen.getByRole('region', { name: 'Profile configuration' });
    expect(within(profile).getByText('profile-a')).toBeInTheDocument();
    expect(within(profile).getByText('2026-11-09T11:00:00Z')).toBeInTheDocument();
    expect(within(profile).getByText('1 hour')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Applied configuration' })).getByText('7')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Observed kernel facts' })).getByText('192.0.2.20/24')).toBeInTheDocument();
    expect(screen.getByText('127.0.0.53')).toBeInTheDocument();
    expect(screen.getByText('upstream server not observed')).toBeInTheDocument();
    expect(screen.getByText(/A stub is not an upstream server/)).toBeInTheDocument();
    expect(screen.queryByText(/^drifted$/i)).not.toBeInTheDocument();
  });

  it.each([
    ['waiting', 'Waiting for evidence'], ['not_observed', 'Not observed'],
    ['unavailable', 'Provider unavailable'], ['error', 'Read failed'],
    ['partial', 'Partial observation'], ['changed', 'Changed during read'],
  ] as const)('does not call %s evidence empty', (state, label) => {
    render(<ConnectivityEvidencePlate title="Test section" evidence={{ ...evidence, state,
      detail: 'Provider did not establish complete evidence.', facts: [], gaps: ['association unknown'] }} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText('association unknown')).toBeInTheDocument();
    expect(screen.queryByText('No entries reported')).not.toBeInTheDocument();
  });

  it('distinguishes a complete empty section from missing evidence', () => {
    render(<ConnectivityEvidencePlate title="Test section" evidence={{ ...evidence, facts: [] }} />);
    expect(screen.getByText('No entries reported')).toBeInTheDocument();
    expect(screen.getByText(/not current absence/)).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
  });
});

describe('diagnostic policy presentation', () => {
  it('keeps prerequisites and topology recovery visible while making policy metadata discoverable', async () => {
    render(<ConnectivityPolicy policy={{ state: 'ready', dnsNames: ['host.example'], tcpEndpoints: ['192.0.2.20:443'] }} />);
    expect(screen.getByText(/DNS name and TCP IP:port authorizations are independent/)).toBeVisible();
    expect(screen.getByText(/administrator-attested v2 control-plane declaration/)).toBeVisible();
    expect(screen.getByText(/Before changing management topology/)).toBeVisible();
    expect(screen.getByText(/After the topology change/)).toBeVisible();
    const disclosure = screen.getByText('Policy metadata and checks');
    await userEvent.click(disclosure);
    expect(screen.getByText('Loaded policy version')).toBeVisible();
    expect(screen.getByText(/Configured TCP allow entries may still be rejected/)).toBeVisible();
  });

  it('discloses administrator topology attestation and the actual startup-only TCP disable/re-enable procedure without inventing metadata', () => {
    render(<ConnectivityPolicy policy={{ state: 'ready', dnsNames: [], tcpEndpoints: [] }} />);
    expect(screen.getByText(/administrator-attested v2 control-plane declaration/)).toHaveTextContent('not automatic discovery');
    expect(screen.getByText(/LocalPlane cannot detect every undeclared topology change/)).toHaveTextContent('use DNS-only with TCP disabled');
    expect(screen.getByText(/Before changing management topology/)).toHaveTextContent('tcp: []');
    expect(screen.getByText(/Before changing management topology/)).toHaveTextContent('restart BOTH backend and agent');
    expect(screen.getByText(/Before changing management topology/)).toHaveTextContent('wait for any actual running diagnostic attempt to finish');
    expect(screen.getByText(/Before changing management topology/)).toHaveTextContent('saving the file alone does not disable currently loaded TCP targets');
    expect(screen.getByText(/After the topology change/)).toHaveTextContent('matching validated policy before TCP can be re-enabled');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
  it('uses the exact no-targets wording only for a known empty policy', () => {
    render(<ConnectivityPolicy policy={{ state: 'ready', dnsNames: [], tcpEndpoints: [] }} />);
    expect(screen.getByText('No targets configured for active diagnostics')).toBeInTheDocument();
    expect(screen.getByText(/Passive route lookup is separate/)).toBeInTheDocument();
  });

  it.each(['waiting', 'blocked'] as const)('does not disguise %s policy as valid empty', (state) => {
    render(<ConnectivityPolicy policy={{ state, detail: 'Policy unavailable or not read.' }} />);
    expect(screen.queryByText('No targets configured for active diagnostics')).not.toBeInTheDocument();
    expect(screen.getByText(/Policy unavailable or not read/)).toBeInTheDocument();
  });

  it.each([
    { dnsNames: ['nas.example.internal.'], tcpEndpoints: [] },
    { dnsNames: [], tcpEndpoints: ['192.0.2.20:443'] },
  ])('shows DNS and TCP authorization independently: %j', (targets) => {
    render(<ConnectivityPolicy policy={{ state: 'ready', ...targets }} />);
    expect(screen.getByText('None configured')).toBeInTheDocument();
    expect(screen.queryByText('No targets configured for active diagnostics')).not.toBeInTheDocument();
    expect(screen.getByText(/Resolving a name does not authorize a connection/)).toBeInTheDocument();
  });
});

describe('destination result presentation', () => {
  it('keeps the selected interface separate from the authoritative egress', () => {
    render(<MemoryRouter><ConnectivityResult selectedInterface="eth0" value={{ state: 'complete', result: action }} /></MemoryRouter>);
    expect(screen.getByText('eth0')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'eth1' })).toHaveAttribute('href', '/network/if_egress?tab=connectivity');
    expect(screen.getByText('fixture host')).toBeInTheDocument();
    expect(screen.getByText('2026-11-09T12:01:01Z')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'not known' })).toBeInTheDocument();
    expect(screen.getByText(/Neither confirms the management path/)).toBeInTheDocument();
  });

  it('does not infer egress from the workspace or success from missing evidence', () => {
    render(<MemoryRouter><ConnectivityResult selectedInterface="eth0" value={{ state: 'complete', result: {
      ...action, outcome: 'unavailable', egress: null, vantage: null, facts: [],
    } }} /></MemoryRouter>);
    expect(screen.getByText('Route lookup · unavailable')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'not known' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'No egress evidence was supplied' })).toBeInTheDocument();
  });

  it.each([
    ['dns', 'resolved', 'DNS lookup · resolved'],
    ['tcp', 'refused', 'TCP handshake · refused'],
    ['tcp', 'timeout', 'TCP handshake · timeout'],
    ['route', 'prohibit', 'Route lookup · prohibit'],
  ] as const)('preserves %s %s without a health verdict', (kind, outcome, text) => {
    render(<MemoryRouter><ConnectivityResult selectedInterface="eth0" value={{ state: 'complete', result: {
      ...action, kind, outcome,
    } }} /></MemoryRouter>);
    expect(screen.getByRole('status')).toHaveTextContent(text);
    expect(screen.queryByText(/^healthy$/i)).not.toBeInTheDocument();
  });

  it('distinguishes waiting, request error and idle without an unsupported cancel control', () => {
    const { rerender } = render(<MemoryRouter><ConnectivityResult selectedInterface="eth0" value={{ state: 'pending', target: '192.0.2.1' }} /></MemoryRouter>);
    expect(screen.getByRole('status')).toHaveTextContent('Waiting for result · 192.0.2.1');
    expect(screen.getByText(/Leaving this view does not cancel execution/)).toBeInTheDocument();
    rerender(<MemoryRouter><ConnectivityResult selectedInterface="eth0" value={{ state: 'error', detail: 'policy mismatch' }} /></MemoryRouter>);
    expect(screen.getByRole('status')).toHaveTextContent('Request failed · policy mismatch');
    rerender(<MemoryRouter><ConnectivityResult selectedInterface="eth0" value={{ state: 'idle' }} /></MemoryRouter>);
    expect(screen.getByRole('status')).toHaveTextContent('No action has been requested');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('has no network effects on mounting, rerendering or unmounting the shell', () => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    try {
      const { rerender, unmount } = render(<MemoryRouter><ConnectivityPanel selectedInterface="eth0" /></MemoryRouter>);
      expect(screen.getAllByRole('button')).toHaveLength(3);
      for (const button of screen.getAllByRole('button')) expect(button).toBeDisabled();
      expect(screen.getByText(/Connectivity reads and controls are not connected yet/)).toBeInTheDocument();
      expect(screen.queryByText('No targets configured for active diagnostics')).not.toBeInTheDocument();
      rerender(<MemoryRouter><ConnectivityPanel selectedInterface="eth1" /></MemoryRouter>);
      unmount();
      expect(fetch).not.toHaveBeenCalled();
    } finally { fetch.mockRestore(); }
  });
});
