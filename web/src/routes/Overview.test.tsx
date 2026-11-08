import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { Overview } from './Overview';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import { refreshAll } from '@/hooks/useResource';
import type {
  NetworkInterfaceList,
  SystemdUnitList,
  FindingList,
  RecoveryHolds,
  Sweep,
} from '@/api/types';

afterEach(() => vi.unstubAllGlobals());

const EMPTY_FINDINGS: FindingList = {
  ...(BACKEND['/api/v1/findings'] as FindingList),
  count: 0,
  findings: [],
};

const EMPTY_HOLDS: RecoveryHolds = {
  host_id: 'host_1',
  total_count: 0,
  count: 0,
  limit: 50,
  truncated: false,
  changes: [],
};

const OK_SWEEP: Sweep = {
  ...((BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList).last_sweep as Sweep),
  status: 'ok',
};

const PARTIAL_SWEEP: Sweep = { ...OK_SWEEP, status: 'partial' };
const FAILED_SWEEP: Sweep = { ...OK_SWEEP, status: 'failed' };

function renderOverview(): void {
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<Overview />} />
      </Routes>
    </MemoryRouter>,
  );
}

function hasText(regex: RegExp): () => HTMLElement | null {
  return () => {
    const elements = screen.queryAllByText((_content, element) =>
      element != null && regex.test(element.textContent ?? ''));
    return elements.length > 0 ? elements[0]! : null;
  };
}

function neverResolvingFetch(): void {
  vi.stubGlobal('fetch', vi.fn<typeof fetch>(() => new Promise(() => {})));
}

describe('Overview attention', () => {
  it('shows a quiet claim limited to what was actually checked', async () => {
    stubBackend({
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();

    await screen.findByText('No attention in these checks');
    expect(hasText(/3 stored object records/)()).not.toBeNull();
    expect(hasText(/Network latest inventory ok \(1 latest-sighted\)/)()).not.toBeNull();
    expect(hasText(/no open findings/)()).not.toBeNull();
    expect(hasText(/no failed units in latest inventory/)()).not.toBeNull();
    expect(hasText(/0 unresolved holds/)()).not.toBeNull();
  });

  it('shows drifted managed interfaces as attention', async () => {
    const interfaces = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    const drifted = {
      ...interfaces,
      interfaces: interfaces.interfaces.map((item) => ({
        ...item,
        management: { state: 'managed', reason: 'adopted' },
        reconciliation: 'drifted',
      })),
    };
    stubBackend({
      '/api/v1/network/interfaces': drifted,
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();

    const rail = await screen.findByLabelText('Attention');
    await waitFor(() => expect(rail).toHaveAttribute('data-state', 'drift'));
    expect(screen.getByRole('link', { name: 'enx020000000012 drifted' })).toBeInTheDocument();
  });

  it('shows latest-sighted failed units and links retained historical failures', async () => {
    const units = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
    const failingUnit = {
      ...units.units[0],
      object_id: 'obj_fail',
      canonical_id: 'fail.service',
      active_state: 'failed',
      observed_in_latest_sweep: true,
    };
    const retainedUnit = {
      ...units.units[0],
      object_id: 'obj_retained',
      canonical_id: 'retained.service',
      active_state: 'failed',
      observed_in_latest_sweep: false,
    };
    stubBackend({
      '/api/v1/systemd/units': { ...units, count: 2, units: [failingUnit, retainedUnit] },
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();

    const rail = await screen.findByLabelText('Attention');
    await waitFor(() => expect(rail).toHaveAttribute('data-state', 'serviceFailure'));
    expect(screen.getByRole('link', { name: 'fail.service last recorded failed · current' })).toBeInTheDocument();
    expect(hasText(/1 failed unit from an earlier inventory remains/)()).not.toBeNull();
    const retained = within(rail).getByRole('link', { name: 'retained.service' });
    expect(retained).toHaveAttribute('href', '/system/obj_retained');
    expect(retained.className).toContain('retainedLink');
  });

  it('shows recovery holds with the authoritative total and truncation note', async () => {
    stubBackend({
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': {
        ...EMPTY_HOLDS,
        total_count: 42,
        count: 1,
        truncated: true,
        changes: [
          {
            change_id: 'chg_1', run_id: 'run_1', object_id: 'obj_if', object_name: 'enx020000000012',
            operation: 'network.interface.reconcile_mtu', change_kind: 'field', field: 'mtu',
            before_value: 1500, desired_value: 1400, action: null, expected_state: null,
            created_at: '2026-08-27T21:50:00Z', finished_at: '2026-08-27T21:50:30Z',
            host_effect: 'write_unknown', mutation_outcome: 'write_unknown',
            verification_outcome: 'observation_unavailable', rollback_outcome: null,
            result: 'recovery_required', recovery_required: true,
            recovery_reason: 'write_unknown_unverified', recovery_state: 'unresolved',
          },
        ],
      },
    });
    renderOverview();

    const rail = await screen.findByLabelText('Attention');
    await waitFor(() => expect(rail).toHaveAttribute('data-state', 'hold'));
    expect(hasText(/42 unresolved holds total \(truncated\)/)()).not.toBeNull();
    const holdHeading = within(rail).getByText('holds').parentElement!;
    expect(within(holdHeading).getByText('42')).toBeInTheDocument();
  });

  it('reports incomplete assessment when a supporting read fails', async () => {
    stubBackend({
      '/api/v1/findings': new Response(
        JSON.stringify({ error: { code: 'not_found', message: 'missing', detail: {} } }),
        { status: 404 },
      ),
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();

    await screen.findByText('Attention could not be fully assessed');
    expect(hasText(/Could not read findings/i)()).not.toBeNull();
  });

  it('does not present a saturated Findings page as an unbounded total', async () => {
    const interfaces = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    const template = (BACKEND['/api/v1/findings'] as FindingList).findings[0]!;
    stubBackend({
      '/api/v1/network/interfaces': { ...interfaces, last_sweep: PARTIAL_SWEEP },
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
      '/api/v1/findings': {
        ...EMPTY_FINDINGS,
        count: 50,
        findings: Array.from({ length: 50 }, (_, i) => ({ ...template, finding_id: `finding-${i}` })),
      },
    });
    renderOverview();
    await waitFor(() => expect(hasText(/at least 50 open findings \(first 50 shown\)/)()).not.toBeNull());
  });

  it('keeps incomplete assessment visible alongside real attention', async () => {
    const units = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
    const failingUnit = { ...units.units[0], active_state: 'failed', observed_in_latest_sweep: true };
    stubBackend({
      '/api/v1/systemd/units': { ...units, units: [failingUnit] },
      '/api/v1/findings': new Response(
        JSON.stringify({ error: { code: 'not_found', message: 'missing', detail: {} } }),
        { status: 404 },
      ),
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();

    await screen.findByText('Assessment incomplete.');
    expect(hasText(/Could not read findings/i)()).not.toBeNull();
    expect(screen.getByRole('link', { name: 'ModemManager.service last recorded failed · current' })).toBeInTheDocument();
  });

  it('requests open findings with a bounded limit', async () => {
    stubBackend({
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();
    await screen.findByText('No attention in these checks');

    const calls = vi.mocked(fetch).mock.calls.filter(([input]) => urlOf(input) === '/api/v1/findings');
    expect(calls).toHaveLength(1);
    const [input] = calls[0]!;
    const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    expect(raw).toContain('status=open');
    expect(raw).toContain('limit=50');
  });

  it('does not claim all-clear while inventory sources are still loading', async () => {
    neverResolvingFetch();
    renderOverview();

    const rail = await screen.findByLabelText('Attention');
    expect(rail).toHaveAttribute('data-state', 'unknown');
    expect(screen.queryByText('No attention in these checks')).not.toBeInTheDocument();
    expect(screen.getByText('Attention could not be fully assessed')).toBeInTheDocument();
    expect(hasText(/Network reading inventory/)()).not.toBeNull();
  });

  it.each([
    { name: 'partial', sweep: PARTIAL_SWEEP, label: /Network latest inventory partial/ },
    { name: 'failed', sweep: FAILED_SWEEP, label: /Network latest inventory failed/ },
  ] as const)('stays incomplete when the latest $name sweep is not ok', async ({ sweep, label }) => {
    const interfaces = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    stubBackend({
      '/api/v1/network/interfaces': { ...interfaces, last_sweep: sweep },
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();

    await screen.findByText('Attention could not be fully assessed');
    expect(hasText(label)()).not.toBeNull();
  });

  it('stays incomplete when the latest inventory is stale', async () => {
    const interfaces = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    const template = interfaces.interfaces[0]!;
    const staleInterface = {
      ...template,
      observed_in_latest_sweep: true,
      observation: { ...template.observation, freshness: 'stale' as const },
    };
    stubBackend({
      '/api/v1/network/interfaces': { ...interfaces, last_sweep: { ...OK_SWEEP, freshness: 'stale', age_seconds: 900 }, interfaces: [staleInterface] },
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    renderOverview();

    await screen.findByText('Attention could not be fully assessed');
    expect(hasText(/Network latest inventory stale/)()).not.toBeNull();
  });

  it('withdraws the quiet claim until a delayed hold reread settles', async () => {
    stubBackend({
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': EMPTY_HOLDS,
    });
    const ordinaryFetch = fetch;
    let releaseHolds!: (response: Response) => void;
    let holdReads = 0;
    vi.stubGlobal('fetch', vi.fn<typeof fetch>((input, init) => {
      if (urlOf(input) === '/api/v1/changes/recovery-holds' && ++holdReads > 1) {
        return new Promise<Response>((resolve) => { releaseHolds = resolve; });
      }
      return ordinaryFetch(input, init);
    }));
    renderOverview();
    await screen.findByText('No attention in these checks');
    act(() => refreshAll());
    await waitFor(() => expect(holdReads).toBe(2));
    expect(screen.queryByText('No attention in these checks')).not.toBeInTheDocument();
    expect(hasText(/holds reading/)()).not.toBeNull();
    await act(async () => releaseHolds(new Response(JSON.stringify(EMPTY_HOLDS))));
    await screen.findByText('No attention in these checks');
  });

  it('keeps a known hold visible while another source is unread', async () => {
    const hold: RecoveryHolds = {
      ...EMPTY_HOLDS,
      total_count: 1,
      count: 1,
      changes: [
        {
          change_id: 'chg_1', run_id: 'run_1', object_id: 'obj_if', object_name: 'enx020000000012',
          operation: 'network.interface.reconcile_mtu', change_kind: 'field', field: 'mtu',
          before_value: 1500, desired_value: 1400, action: null, expected_state: null,
          created_at: '2026-08-27T21:50:00Z', finished_at: '2026-08-27T21:50:30Z',
          host_effect: 'write_unknown', mutation_outcome: 'write_unknown',
          verification_outcome: 'observation_unavailable', rollback_outcome: null,
          result: 'recovery_required', recovery_required: true,
          recovery_reason: 'write_unknown_unverified', recovery_state: 'unresolved',
        },
      ],
    };
    stubBackend({
      '/api/v1/findings': EMPTY_FINDINGS,
      '/api/v1/changes/recovery-holds': hold,
    });
    // Leave units unread by never resolving them.
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>((input) => {
        const path = urlOf(input);
        if (path === '/api/v1/systemd/units') return new Promise(() => {});
        const body = { ...BACKEND, '/api/v1/findings': EMPTY_FINDINGS, '/api/v1/changes/recovery-holds': hold }[path];
        if (body !== undefined) return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
        return Promise.resolve(new Response(JSON.stringify({ error: { code: 'not_found', message: 'missing', detail: {} } }), { status: 404 }));
      }),
    );
    renderOverview();

    const rail = await screen.findByLabelText('Attention');
    await waitFor(() => expect(rail).toHaveAttribute('data-state', 'hold'));
    expect(screen.getByRole('link', { name: 'enx020000000012 network.interface.reconcile_mtu' })).toBeInTheDocument();
    expect(screen.getByText('Assessment incomplete.')).toBeInTheDocument();
  });
});
