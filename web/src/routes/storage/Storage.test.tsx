import { afterEach, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { StorageMount, StorageObservation } from '@/api/types';
import { storageResponse } from '@/api/storage';
import { refreshAll } from '@/hooks/useResource';
import { Storage } from './Storage';
import { availablePercent, displayPath, size } from '@/domain/storage';

const appShellStyles = readFileSync('src/components/layout/AppShell.module.css', 'utf8');
const storageStyles = readFileSync('src/routes/storage/Storage.module.css', 'utf8');

afterEach(() => vi.unstubAllGlobals());

function fixture(id = 'a'.repeat(32)): StorageObservation {
  const row: StorageMount = {
    mount_id: 1, parent_id: 0, mountpoint: '/byte-pressure', root: '/', filesystem: 'ext4', device: '8:1', read_only: false,
    status: 'observed', reason: 'Pinned descriptor evidence.',
    bytes: { total: '10240', used: '9216', free: '1024', available: '0', reserved: '1024' },
    inodes: { total: '1000', used: '100', free: '900', available: '900', reserved: '0' },
    inode_reason: 'Observed with fstatvfs.', backing_name: 'example1', backing_reason: 'Kernel major:minor join.',
  };
  return { observation_id: id, observed_at: '2026-10-19T12:00:00+00:00', namespace: 'mnt:[42]', status: 'partial', reason: 'Synthetic partial coverage.',
    mounts: [row, { ...row, mount_id: 2, mountpoint: '/inode-pressure', filesystem: 'xfs', bytes: row.inodes, inodes: row.bytes },
      { ...row, mount_id: 3, mountpoint: '/remote', filesystem: 'nfs', status: 'unsupported', bytes: null, inodes: null, backing_name: null }],
    mount_limit: 128, metadata_limit_bytes: 1048576, response_limit_bytes: 524288, collection_limit_ms: 3000, work_limit_ms: 500 };
}

const response = (value: unknown, status = 200): Response => new Response(JSON.stringify(value), { status });

it('separates byte/inode exhaustion, exact reserved evidence, backing and bounds', async () => {
  const fetch = vi.fn().mockResolvedValue(response(fixture()));
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  render(<Storage />);
  expect(await screen.findByText('No usable bytes')).toBeInTheDocument();
  expect(screen.getByText('No usable inodes')).toBeInTheDocument();
  expect(screen.getByText('unsupported')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '/byte-pressure' }));
  const detail = screen.getByRole('region', { name: 'Selected mount evidence' });
  expect(within(detail).getByText('example1')).toBeInTheDocument();
  expect(within(detail).getAllByText('reserved')).toHaveLength(2);
  expect(within(detail).getByText(/Used = total/)).toBeInTheDocument();
  await user.click(screen.getByText('Provenance and collection limits'));
  expect(screen.getByText(/At most 128 mount rows/)).toHaveTextContent('500 ms per mount work');
  expect(fetch.mock.calls[0]?.[1]).toMatchObject({ method: 'POST', credentials: 'same-origin' });
});

it('sorts by available percentage, filters locally and supports keyboard selection', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(fixture())));
  const user = userEvent.setup();
  render(<Storage />);
  await screen.findByRole('button', { name: '/byte-pressure' });
  await user.selectOptions(screen.getByRole('combobox', { name: 'Order by' }), 'inodes');
  expect(screen.getAllByRole('row')[1]).toHaveTextContent('/inode-pressure');
  const mount = screen.getByRole('button', { name: '/inode-pressure' });
  mount.focus();
  await user.keyboard('{Enter}');
  expect(mount).toHaveAttribute('aria-pressed', 'true');
  await user.type(screen.getByRole('textbox', { name: 'Filter mounts' }), 'remote');
  expect(screen.queryByRole('button', { name: '/byte-pressure' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '/remote' })).toBeInTheDocument();
});

it('keeps capacity first, separates unresolved ext4 from virtual mounts, and reveals virtual search matches', async () => {
  const data = fixture();
  data.mounts.push(
    { ...data.mounts[0]!, mount_id: 4, mountpoint: '/run', filesystem: 'tmpfs', status: 'unsupported', bytes: null, inodes: null, backing_name: null, reason: 'Virtual filesystem.' },
    { ...data.mounts[0]!, mount_id: 5, mountpoint: '/data', filesystem: 'ext4', status: 'unavailable', bytes: null, inodes: null, backing_name: null, reason: 'Counter read failed.' },
    { ...data.mounts[0]!, mount_id: 6, mountpoint: '/bind', root: '/byte-pressure', backing_name: 'example1', reason: 'Bind mount evidence.' },
  );
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(data)));
  const user = userEvent.setup();
  render(<Storage />);
  expect(await screen.findByText('capacity and inode pressure first')).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Total' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Used' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Available' })).toBeInTheDocument();
  expect(screen.getByText('Other unsupported or unresolved mounts')).toBeInTheDocument();
  expect(screen.getByText('Counter read failed.')).toBeInTheDocument();
  expect(screen.getByText(/Immediate backing is supplied per observed mount/)).toBeInTheDocument();
  const tables = document.querySelectorAll('table');
  expect(tables).toHaveLength(3);
  tables.forEach((table) => expect(table.className).toContain('mountTable'));
  const virtual = screen.getByText(/System and virtual mounts/).closest('details')!;
  expect(virtual).not.toHaveAttribute('open');
  await user.type(screen.getByRole('textbox', { name: 'Filter mounts' }), 'tmpfs');
  expect(virtual).toHaveAttribute('open');
  expect(screen.getByRole('button', { name: '/run' })).toBeInTheDocument();
  await user.clear(screen.getByRole('textbox', { name: 'Filter mounts' }));
  await user.click(screen.getByText(/System and virtual mounts/));
  await user.click(screen.getByRole('button', { name: '/run' }));
  expect(screen.getByRole('region', { name: 'Selected mount evidence' })).toHaveTextContent('/run');
  await user.click(screen.getByRole('button', { name: '/bind' }));
  expect(screen.getByRole('region', { name: 'Selected mount evidence' })).toHaveTextContent('example1');
});

it('keeps every mount group horizontally scrollable with a usable mount column at narrow widths', () => {
  expect(storageStyles).toContain('.tableWrap { overflow-x: auto; }');
  expect(storageStyles).toContain('.mountTable { min-width: 760px; }');
  expect(storageStyles).toContain('.mountTable .mountCell { width: 13rem; min-width: 13rem; }');
});

it('keeps zero totals and unsupported evidence explicit in their respective groups', async () => {
  const data = fixture();
  data.mounts[0]!.bytes = { total: '0', used: '0', free: '0', available: '0', reserved: '0' };
  data.mounts[2] = { ...data.mounts[2]!, filesystem: 'ext4', status: 'unsupported', reason: 'Unsupported ext4 sample.' };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(data)));
  render(<Storage />);
  expect(await screen.findByText('Total is zero; ratio unknown')).toBeInTheDocument();
  expect(screen.getByText('Unsupported ext4 sample.')).toBeInTheDocument();
  expect(screen.getByText('Other unsupported or unresolved mounts')).toBeInTheDocument();
});

it('keeps the serif upright/italic wordmark at narrow widths', () => {
  expect(appShellStyles).toContain('font-family: var(--wordmark)');
  expect(appShellStyles).toContain('.brand i { font-style: italic');
  expect(appShellStyles).not.toContain('@media (max-width: 520px)');
});

it('hides stale details during refresh and resets selection when mount ID is reused', async () => {
  let resolve!: (value: Response) => void;
  const fetch = vi.fn().mockResolvedValueOnce(response(fixture())).mockImplementationOnce(() => new Promise<Response>((r) => { resolve = r; }));
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  render(<Storage />);
  await user.click(await screen.findByRole('button', { name: '/byte-pressure' }));
  await user.click(screen.getByRole('button', { name: 'Refresh filesystems' }));
  expect(screen.getByText(/Refreshing — previous observation/)).toBeInTheDocument();
  expect(screen.queryByRole('region', { name: 'Selected mount evidence' })).not.toBeInTheDocument();
  const next = fixture('b'.repeat(32));
  next.mounts[0]!.mountpoint = '/replacement';
  await act(async () => resolve(response(next)));
  expect(await screen.findByRole('button', { name: '/replacement' })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.queryByRole('region', { name: 'Selected mount evidence' })).not.toBeInTheDocument();
});

it('ignores an older asynchronous response after a new explicit refresh', async () => {
  const resolvers: ((value: Response) => void)[] = [];
  vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise<Response>((resolve) => resolvers.push(resolve))));
  render(<Storage />);
  expect(screen.getByText('Reading mounted filesystem evidence…')).toBeInTheDocument();
  act(() => refreshAll());
  await waitFor(() => expect(resolvers).toHaveLength(2));
  const newer = fixture('c'.repeat(32));
  newer.mounts[0]!.mountpoint = '/newer';
  await act(async () => resolvers[1]!(response(newer)));
  await screen.findByRole('button', { name: '/newer' });
  await act(async () => resolvers[0]!(response(fixture())));
  expect(screen.queryByRole('button', { name: '/byte-pressure' })).not.toBeInTheDocument();
});

it('makes refresh failure explicit rather than retaining current-looking evidence', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(fixture())).mockResolvedValueOnce(response({ error: { code: 'agent_unavailable', message: 'Agent unavailable', detail: {} } }, 503)));
  const user = userEvent.setup();
  render(<Storage />);
  await screen.findByRole('button', { name: '/byte-pressure' });
  await user.click(screen.getByRole('button', { name: 'Refresh filesystems' }));
  expect(await screen.findByText('Agent unavailable')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '/byte-pressure' })).not.toBeInTheDocument();
});

it.each(['busy', 'unavailable', 'partial'] as const)('shows %s and empty coverage truthfully', async (status) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...fixture(), status, mounts: [], namespace: status === 'partial' ? 'mnt:[42]' : null })));
  render(<Storage />);
  expect(await screen.findByText(`${status} observation`)).toBeInTheDocument();
  expect(screen.getByText(/No mount rows were returned/)).toBeInTheDocument();
});

it('rejects malformed evidence and invalid counters before rendering', async () => {
  const bad = fixture();
  bad.mounts[0]!.bytes!.available = 'NaN';
  expect(() => storageResponse(bad)).toThrow('unreadable');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(bad)));
  render(<Storage />);
  expect(await screen.findByText('The backend returned unreadable filesystem evidence.')).toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

it('preserves large counters and avoids division by zero', () => {
  expect(displayPath('/é\nnext\\literal')).toBe('/é\\nnext\\\\literal');
  expect(size('1152921504606846976')).toBe('1 EiB');
  expect(availablePercent({ total: '0', used: '0', free: '0', available: '0', reserved: '0' })).toBeNull();
  expect(availablePercent(null)).toBeNull();
  const duplicated = fixture();
  duplicated.mounts[1]!.mount_id = 1;
  expect(() => storageResponse(duplicated)).toThrow('unreadable');
});
