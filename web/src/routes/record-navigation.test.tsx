/**
 * Real-app navigation for encoded operational-record identities.
 *
 * These tests activate Search and Machine Record links through the actual App shell
 * and router, not through surrogate routes. They assert both the correct detail
 * screen/identity and the exact encoded API request path.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { App } from '@/App';
import { ViewerProvider } from '@/identity/viewer';
import { AuthenticationProvider } from '@/auth/AuthProvider';
import { PreferencesProvider } from '@/preferences/preferences';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import { runUrl, changeUrl, findingUrl } from '@/domain/recordLink';
import fixture from '@/test/fixtures/systemd-run.json';
import type { Change, Finding, Run } from '@/api/types';

const SPECIAL_ID = 'record/id ?#%';

afterEach(() => vi.unstubAllGlobals());

function AddressProbe(): JSX.Element {
  const location = useLocation();
  return <span data-testid="address">{`${location.pathname}${location.search}`}</span>;
}

function renderAt(path: string): void {
  render(
    <PreferencesProvider>
      <MemoryRouter initialEntries={[path]}>
        <AuthenticationProvider>
          <ViewerProvider>
            <App />
            <AddressProbe />
          </ViewerProvider>
        </AuthenticationProvider>
      </MemoryRouter>
    </PreferencesProvider>,
  );
}

function specialRun(): Run {
  const run = structuredClone(fixture) as unknown as Run;
  run.run_id = SPECIAL_ID;
  run.object_id = 'obj_unit';
  run.object_name = 'ModemManager.service';
  run.operation = 'systemd.service.restart';
  return run;
}

function specialFinding(): Finding {
  const finding = structuredClone(BACKEND['/api/v1/findings/find_1']) as Finding;
  finding.finding_id = SPECIAL_ID;
  finding.summary = 'Special finding';
  return finding;
}

function specialChange(): Change {
  const change = structuredClone(BACKEND['/api/v1/changes/chg_1']) as Change;
  change.change_id = SPECIAL_ID;
  change.run_id = 'run_1';
  change.recovery = { ...change.recovery, object_write_locked: false };
  return change;
}

function encodedRecordPath(kind: 'runs' | 'changes' | 'findings', id: string): string {
  const prefix = kind === 'runs' ? '/api/v1/runs' : kind === 'changes' ? '/api/v1/changes' : '/api/v1/findings';
  return `${prefix}/${encodeURIComponent(id)}`;
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function mockFetch(): ReturnType<typeof vi.fn> {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

describe('encoded record navigation through the real app', () => {
  it('follows a Machine Record finding link with reserved characters and requests the exact encoded detail path', async () => {
    const finding = specialFinding();
    stubBackend({
      '/api/v1/findings': { host_id: 'host_1', status: 'all', count: 1, findings: [finding] },
      [encodedRecordPath('findings', SPECIAL_ID)]: finding,
    });
    const fetch = mockFetch();
    renderAt('/network/obj_if');

    await screen.findByText('Machine record', {}, { timeout: 3000 });
    const link = await screen.findByRole('link', { name: finding.summary }, { timeout: 3000 });
    expect(link).toHaveAttribute('href', findingUrl(SPECIAL_ID));
    fireEvent.click(link);

    expect(await screen.findByTestId('address')).toHaveTextContent(`/operations/findings/${encodeURIComponent(SPECIAL_ID)}`);
    expect(await screen.findByRole('heading', { name: finding.summary })).toBeInTheDocument();
    expect(fetch.mock.calls.some(([url]) => url === encodedRecordPath('findings', SPECIAL_ID))).toBe(true);
    expect(fetch.mock.calls.some(([url]) => urlOf(url) === '/api/v1/findings')).toBe(true);
  });

  it('follows a Machine Record run link with reserved characters and requests the exact encoded detail path', async () => {
    const run = specialRun();
    stubBackend({
      '/api/v1/runs': { host_id: 'host_1', count: 1, runs: [run] },
      [encodedRecordPath('runs', SPECIAL_ID)]: run,
    });
    const fetch = mockFetch();
    renderAt('/network/obj_if');

    await screen.findByText('Machine record', {}, { timeout: 3000 });
    const link = await screen.findByRole('link', { name: run.operation }, { timeout: 3000 });
    expect(link).toHaveAttribute('href', runUrl(SPECIAL_ID));
    fireEvent.click(link);

    expect(await screen.findByTestId('address')).toHaveTextContent(`/operations/runs/${encodeURIComponent(SPECIAL_ID)}`);
    expect(await screen.findByRole('region', { name: 'Change Composer' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: run.operation })).toBeInTheDocument();
    expect(fetch.mock.calls.some(([url]) => url === encodedRecordPath('runs', SPECIAL_ID))).toBe(true);
    expect(fetch.mock.calls.some(([url]) => urlOf(url) === '/api/v1/runs')).toBe(true);
  });

  it('follows a Machine Record change link with reserved characters and requests the exact encoded detail path', async () => {
    const change = specialChange();
    stubBackend({
      '/api/v1/changes': { host_id: 'host_1', count: 1, changes: [change] },
      [encodedRecordPath('changes', SPECIAL_ID)]: change,
    });
    const fetch = mockFetch();
    renderAt('/network/obj_if');

    await screen.findByText('Machine record', {}, { timeout: 3000 });
    const link = await screen.findByRole('link', { name: new RegExp(change.operation) }, { timeout: 3000 });
    expect(link).toHaveAttribute('href', changeUrl(SPECIAL_ID));
    fireEvent.click(link);

    expect(await screen.findByTestId('address')).toHaveTextContent(`/operations/changes/${encodeURIComponent(SPECIAL_ID)}`);
    expect(await screen.findByRole('heading', { name: change.operation })).toBeInTheDocument();
    expect(fetch.mock.calls.some(([url]) => url === encodedRecordPath('changes', SPECIAL_ID))).toBe(true);
    expect(fetch.mock.calls.some(([url]) => urlOf(url) === '/api/v1/changes')).toBe(true);
  });

  it.each([
    ['runs', specialRun, '/api/v1/runs?state=all&limit=50'],
    ['changes', specialChange, '/api/v1/changes?limit=100'],
    ['findings', specialFinding, '/api/v1/findings?status=all&limit=100'],
  ] as const)('follows a Search %s result with reserved characters and requests the exact encoded detail path', async (kind, makeRecord, listUrl) => {
    const record = makeRecord();
    const detailPath = encodedRecordPath(kind, SPECIAL_ID);
    const listKey = kind === 'runs' ? '/api/v1/runs' : kind === 'changes' ? '/api/v1/changes' : '/api/v1/findings';
    stubBackend({
      [listKey]: { host_id: 'host_1', count: 1, [kind]: [record] },
      [detailPath]: record,
    });
    const fetch = mockFetch();
    const user = userEvent.setup();
    renderAt('/');

    const trigger = await screen.findByRole('button', { name: /Search objects/ }, { timeout: 3000 });
    await user.click(trigger);
    await user.type(screen.getByRole('combobox'), SPECIAL_ID);
    await screen.findByRole('option', { name: new RegExp(escapeRegex(SPECIAL_ID)) }, { timeout: 3000 });
    await user.keyboard('{Enter}');

    expect(await screen.findByTestId('address')).toHaveTextContent(`/operations/${kind}/${encodeURIComponent(SPECIAL_ID)}`);

    if (kind === 'runs') {
      expect(await screen.findByRole('region', { name: 'Change Composer' })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: (record as Run).operation })).toBeInTheDocument();
    } else if (kind === 'changes') {
      expect(await screen.findByRole('heading', { name: (record as Change).operation })).toBeInTheDocument();
    } else {
      expect(await screen.findByRole('heading', { name: (record as Finding).summary })).toBeInTheDocument();
    }

    expect(fetch.mock.calls.some(([url]) => url === detailPath)).toBe(true);
    expect(fetch.mock.calls.some(([url]) => url === listUrl)).toBe(true);
    // No double-encoded or decoded path leaked to the wire.
    expect(fetch.mock.calls.some(([url]) => typeof url === 'string' && url.includes('/record/id') && !url.includes(encodeURIComponent(SPECIAL_ID)))).toBe(false);
  }, 15000);
});
