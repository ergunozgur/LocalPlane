import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { WorkloadDetail } from './WorkloadDetail';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import type { DockerContainer } from '@/api/types';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function renderMounts(container: DockerContainer): void {
  stubBackend({ '/api/v1/docker/containers/obj_ct': container });
  render(
    <MemoryRouter initialEntries={['/workloads/obj_ct?tab=mounts']}>
      <Routes><Route path="workloads/:objectId" element={<WorkloadDetail />} /></Routes>
    </MemoryRouter>,
  );
}

function expectMountReadsOnly(): void {
  const expectedPaths = new Set([
    '/api/v1/docker/containers/obj_ct',
    '/api/v1/network/interfaces',
    '/api/v1/docker/containers',
    '/api/v1/management-path',
    '/api/v1/findings',
    '/api/v1/changes',
    '/api/v1/runs',
  ]);
  const calls = vi.mocked(fetch).mock.calls;

  expect(calls).not.toHaveLength(0);
  expect(new Set(calls.map(([input]) => urlOf(input)))).toEqual(expectedPaths);
  expect(calls.every(([, init]) => init?.method === 'GET')).toBe(true);
}

describe('container Mounts tab', () => {
  it('keeps volume names, daemon sources, reported access, and exact conflicting facts distinct', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const container: DockerContainer = {
      ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer),
      observation: {
        ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer).observation!,
        freshness: 'stale',
      },
      mounts: [
        { type: 'volume', name: 'named-data', source: '/var/lib/docker/volumes/named-data/_data', destination: '/data', driver: 'local', mode: 'ro', read_write: true, propagation: '' },
        { type: 'volume', name: 'named-data', source: '/var/lib/docker/volumes/named-data/_data', destination: '/data', driver: 'other', mode: 'ro', read_write: false, propagation: 'rshared' },
        { type: 'volume', name: null, source: '/var/lib/docker/volumes/anonymous/_data', destination: '/anonymous', driver: 'local', mode: '', read_write: false, propagation: null },
        { type: 'bind', name: null, source: '/very/long/host/path/that/must/remain/reachable/without/becoming/a/storage/join', destination: '/data', driver: null, mode: 'z', read_write: null, propagation: 'rprivate' },
        { type: 'tmpfs', name: null, source: '', destination: '/run/cache', driver: null, mode: '', read_write: false, propagation: '' },
        { type: 'mystery', name: '', source: null, destination: null, driver: '', mode: null, read_write: null, propagation: '' },
      ],
    };
    renderMounts(container);

    await screen.findByRole('tabpanel', { name: 'Mounts' });
    expect(screen.getAllByText('named-data')).toHaveLength(4);
    expect(screen.getAllByText('/var/lib/docker/volumes/named-data/_data')).toHaveLength(4);
    expect(screen.getAllByText('/very/long/host/path/that/must/remain/reachable/without/becoming/a/storage/join')).toHaveLength(2);
    expect(screen.getAllByText('rw (reported)')).toHaveLength(1);
    expect(screen.getAllByText('ro (reported)')).toHaveLength(3);
    expect(screen.getByText('stale')).toBeInTheDocument();

    const user = userEvent.setup();
    const details = screen.getByText('Exact mount facts').closest('details') as HTMLElement;
    const summary = within(details).getByText('Exact mount facts').closest('summary') as HTMLElement;
    summary.focus();
    expect(summary).toHaveFocus();
    expect(summary.tagName).toBe('SUMMARY');
    await user.click(summary);
    expect(details).toHaveAttribute('open');
    expect(screen.getByText('true')).toBeInTheDocument();
    expect(screen.getAllByText('false')).toHaveLength(3);
    expect(screen.getByText('other')).toBeInTheDocument();
    expect(screen.getByText('rprivate')).toBeInTheDocument();
    expect(screen.getByText('rshared')).toBeInTheDocument();
    expect(screen.getAllByText('ro')).toHaveLength(2);
    expect(consoleError.mock.calls.flat().join(' ')).not.toContain('Encountered two children with the same key');

    await user.click(screen.getByRole('tab', { name: 'Overview' }));
    await user.click(screen.getByRole('tab', { name: /^Mounts/ }));
    expectMountReadsOnly();
  });

  it('keeps empty records and missing observation truthful', async () => {
    const container: DockerContainer = {
      ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer),
      mounts: [],
      observation: null,
    };
    renderMounts(container);

    await screen.findByRole('tabpanel', { name: 'Mounts' });
    expect(screen.getByText('No mount records')).toBeInTheDocument();
    expect(screen.getByText(/does not establish that the container has no storage/i)).toBeInTheDocument();
    expect(screen.getByText('Never observed')).toBeInTheDocument();
  });

  it('keeps an unrecognised observation freshness unknown without another request', async () => {
    const container: DockerContainer = {
      ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer),
      observation: {
        ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer).observation!,
        freshness: 'unrecognised_freshness' as never,
      },
    };
    renderMounts(container);

    await screen.findByRole('tabpanel', { name: 'Mounts' });
    const freshness = screen.getByText('unrecognised_freshness').closest('[title]') as HTMLElement;
    expect(freshness).toHaveTextContent('not known: unrecognised_freshness');
    expect(freshness).toHaveAttribute('title', expect.stringContaining('does not recognise'));
    expectMountReadsOnly();
  });
});
