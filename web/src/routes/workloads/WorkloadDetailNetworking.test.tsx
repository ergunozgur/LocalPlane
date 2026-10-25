import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { WorkloadDetail } from './WorkloadDetail';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import type { DockerContainer } from '@/api/types';

afterEach(() => vi.unstubAllGlobals());

function renderNetworking(container: DockerContainer): void {
  stubBackend({ '/api/v1/docker/containers/obj_ct': container });
  render(
    <MemoryRouter initialEntries={['/workloads/obj_ct?tab=networking']}>
      <Routes><Route path="workloads/:objectId" element={<WorkloadDetail />} /></Routes>
    </MemoryRouter>,
  );
}

function expectNetworkingReadsOnly(): void {
  const calls = vi.mocked(fetch).mock.calls;
  const expectedPaths = new Set([
    '/api/v1/docker/containers/obj_ct',
    '/api/v1/network/interfaces',
    '/api/v1/docker/containers',
    '/api/v1/management-path',
    '/api/v1/findings',
    '/api/v1/changes',
    '/api/v1/runs',
  ]);

  expect(calls).not.toHaveLength(0);
  expect(new Set(calls.map(([input]) => urlOf(input)))).toEqual(expectedPaths);
  expect(calls.every(([, init]) => init?.method === 'GET')).toBe(true);
}

describe('container Networking tab', () => {
  it('separates mixed published and exposed-only rows without inventing an endpoint', async () => {
    const base = BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer;
    const container: DockerContainer = {
      ...base,
      observation: { ...base.observation!, freshness: 'stale' },
      ports: [
        { container_port: 8080, protocol: 'tcp', host_ip: '0.0.0.0', host_port: 8080, published: true },
        { container_port: 8080, protocol: 'tcp', host_ip: '::1', host_port: 8080, published: true },
        { container_port: 9000, protocol: 'tcp', host_ip: null, host_port: null, published: false },
        { container_port: 443, protocol: 'tcp', host_ip: '::', host_port: null, published: true },
      ],
      networks: [{
        name: 'project_default', network_id: 'network-very-long-identity', ip_address: '172.18.0.4',
        ipv6_address: '2001:db8::4', gateway: '172.18.0.1', mac_address: '02:42:ac:12:00:04',
        aliases: ['grafana', 'grafana-long-alias'],
      }],
    };
    renderNetworking(container);

    await screen.findByRole('tabpanel', { name: 'Networking' });
    expect(screen.getByText('0.0.0.0:8080')).toBeInTheDocument();
    expect(screen.getByText('[::1]:8080')).toBeInTheDocument();
    expect(screen.getByText('Exposed only')).toBeInTheDocument();
    expect(screen.getByText('Published; [::] · host port not reported')).toBeInTheDocument();
    expect(screen.getByText('All IPv4 addresses')).toBeInTheDocument();
    expect(screen.getByText('Loopback address')).toBeInTheDocument();
    expect(screen.getByText('All IPv6 addresses')).toBeInTheDocument();
    expect(screen.queryByText('not published')).not.toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();

    const user = userEvent.setup();
    const details = screen.getByText('Attachment facts').closest('details');
    const summary = within(details as HTMLElement).getByText('Attachment facts').closest('summary') as HTMLElement;
    summary.focus();
    expect(summary).toHaveFocus();
    expect(summary.tagName).toBe('SUMMARY');
    await user.click(summary);
    expect(details).toHaveAttribute('open');
    expect(screen.getByText('network-very-long-identity')).toBeInTheDocument();
    expect(screen.getByText('grafana, grafana-long-alias')).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: 'Overview' }));
    await user.click(screen.getByRole('tab', { name: /^Networking/ }));
    expectNetworkingReadsOnly();
  });

  it.each([
    ['host', 'Shares the host network namespace', 'Docker reported no attachment rows for host networking'],
    ['none', 'Docker reports no network attachment', 'Docker reported no attachment rows for this mode'],
    ['container:shared-id', 'Shares another container network namespace', 'Docker reported no attachment rows for this shared network namespace'],
  ])('keeps %s empty records contextual', async (networkMode, context, empty) => {
    const container: DockerContainer = {
      ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer),
      network_mode: networkMode,
      networks: [],
      ports: [],
    };
    renderNetworking(container);

    await screen.findByRole('tabpanel', { name: 'Networking' });
    expect(screen.getByText(new RegExp(context))).toBeInTheDocument();
    expect(screen.getByText(new RegExp(empty))).toBeInTheDocument();
    expect(screen.getByText('No port records')).toBeInTheDocument();
    expect(screen.getByText('This does not establish no listeners or connectivity.')).toBeInTheDocument();
  });

  it('keeps an unknown mode and observation explicitly unreported', async () => {
    const container: DockerContainer = {
      ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer),
      network_mode: null,
      networks: [],
      ports: [],
      observation: null,
    };
    renderNetworking(container);

    await screen.findByRole('tabpanel', { name: 'Networking' });
    expect(screen.getByText('Network mode was not reported.')).toBeInTheDocument();
    expect(screen.getByText('Never observed')).toBeInTheDocument();
  });
});
