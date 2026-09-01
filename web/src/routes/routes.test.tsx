/**
 * Route-level rendering, against a real backend payload.
 *
 * The Run detail fixture is the response a live backend gave for
 * `POST /runs {systemd.service.restart}` — not a hand-written approximation — so this test
 * fails if the page stops rendering the shape the backend actually sends.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { urlOf } from '@/test/backend';
import { RunDetail } from './operations/RunDetail';
import { SystemList } from './system/SystemList';
import { ViewerProvider } from '@/identity/viewer';
import runFixture from '@/test/fixtures/systemd-run.json';

function mockJson(body: unknown, status = 200): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { 'content-type': 'application/json' },
        }),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderRun(): void {
  render(
    <ViewerProvider>
      <MemoryRouter initialEntries={['/operations/runs/run_x']}>
        <Routes>
          <Route path="/operations/runs/:runId" element={<RunDetail />} />
        </Routes>
      </MemoryRouter>
    </ViewerProvider>,
  );
}

describe('Run detail, from a real backend response', () => {
  it('renders the plan and reports the executor as not implemented', async () => {
    mockJson(runFixture);
    renderRun();

    await screen.findByText('systemd.service.restart');
    expect(screen.getAllByText('not_implemented').length).toBeGreaterThan(0);
    expect(screen.getByText('not implemented')).toBeInTheDocument();
  });

  it('offers no control to execute the plan', async () => {
    mockJson(runFixture);
    renderRun();

    await screen.findByText('systemd.service.restart');
    for (const label of [/^apply$/i, /^confirm$/i, /^execute$/i, /^run$/i, /^restart$/i]) {
      expect(screen.queryByRole('button', { name: label })).not.toBeInTheDocument();
    }
    expect(screen.getByText(/produces nothing across it/i)).toBeInTheDocument();
  });

  it('shows the capability as declared while execution stays unavailable', async () => {
    mockJson(runFixture);
    renderRun();

    await screen.findByText('systemd.service.lifecycle');
    const row = screen.getByText('Declared by agent').closest('div') as HTMLElement;
    expect(within(row).getByText('yes')).toBeInTheDocument();
    expect(screen.getByText(/this stored plan predates the executor/i)).toBeInTheDocument();
  });

  it('lists every blocker rather than the first', async () => {
    mockJson(runFixture);
    renderRun();

    // The token appears twice by design: as a blocker, and as the confirmation's reason for
    // being unsatisfiable. Both are true and both are shown.
    expect((await screen.findAllByText('execution_not_implemented')).length).toBeGreaterThan(1);
    expect(screen.getByText('protection_unresolved:management_path')).toBeInTheDocument();
    expect(screen.getByText('protection_unresolved:localplane_agent')).toBeInTheDocument();
  });

  it('renders unresolved protection as unknown, not as clear', async () => {
    mockJson(runFixture);
    renderRun();

    await screen.findByText(/Protection/);
    expect(screen.queryByText('clear')).not.toBeInTheDocument();
    expect(screen.getAllByText('unknown').length).toBeGreaterThan(0);
  });

  it('reports the confirmation as unsatisfiable with the backend’s reason', async () => {
    mockJson(runFixture);
    renderRun();

    const row = (await screen.findByText('Satisfiable')).closest('div') as HTMLElement;
    expect(within(row).getByText('no')).toBeInTheDocument();
    expect(within(row).getByText('execution_not_implemented')).toBeInTheDocument();
  });

  it('does not infer the current session into a run without persisted attribution', async () => {
    mockJson(runFixture);
    renderRun();
    expect(await screen.findByText('not recorded')).toBeInTheDocument();
  });

  it('surfaces a backend failure without inventing data', async () => {
    mockJson({ error: { code: 'run_not_found', message: 'no such run', detail: {} } }, 404);
    renderRun();

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByText('run_not_found')).toBeInTheDocument();
    expect(screen.queryByText('systemd.service.restart')).not.toBeInTheDocument();
  });
});

const UNIT_LIST = {
  host_id: 'host_x',
  capability: {
    capability: 'systemd.units.observe',
    version: 1,
    status: 'available',
    mutating: false,
    summary: '',
    reason: null,
    detail: {},
    discovered_at: '2026-08-27T21:46:40Z',
  },
  last_sweep: {
    sweep_id: 'swp_1',
    capability: 'systemd.units.observe',
    scope: 'inventory',
    provider: 'systemd',
    provider_version: '255',
    status: 'ok',
    started_at: '2026-08-27T21:47:00Z',
    completed_at: '2026-08-27T21:47:01Z',
    received_at: '2026-08-27T21:47:01Z',
    object_count: 2,
    missing: [],
    issues: [],
    agent_instance_id: 'agent_1',
  },
  count: 2,
  units: [
    {
      object_id: 'obj_a',
      kind: 'systemd.unit',
      canonical_id: 'ModemManager.service',
      names: null,
      description: 'Modem Manager',
      unit_type: 'service',
      identity: { basis: 'unit_id', value: 'ModemManager.service', confidence: 'high' },
      management: { state: 'observed', reason: 'observe_only' },
      health: { state: 'healthy', reason: 'ok' },
      observation: null,
      observed_in_latest_sweep: true,
      load_state: 'loaded',
      active_state: 'active',
      sub_state: 'running',
      unit_file_state: 'enabled',
      unit_file_preset: null,
      can_start: true, can_stop: true, can_reload: false,
      refuse_manual_start: false, refuse_manual_stop: false, need_daemon_reload: false,
      fragment_path: null, source_path: null, drop_in_paths: null,
      transient: false, template: null, current_job: null, invocation_id: null,
      timestamps: {}, relationships: [],
      first_seen_at: '2026-08-27T21:46:55Z', last_seen_at: '2026-08-27T21:47:11Z',
    },
    {
      object_id: 'obj_b',
      kind: 'systemd.unit',
      canonical_id: 'cups.socket',
      names: null,
      description: 'CUPS Scheduler',
      unit_type: 'socket',
      identity: { basis: 'unit_id', value: 'cups.socket', confidence: 'high' },
      management: { state: 'observed', reason: 'observe_only' },
      health: { state: 'inactive', reason: 'inactive_dead' },
      observation: null,
      observed_in_latest_sweep: true,
      load_state: 'loaded',
      active_state: 'inactive',
      sub_state: 'dead',
      unit_file_state: 'disabled',
      unit_file_preset: null,
      can_start: true, can_stop: true, can_reload: false,
      refuse_manual_start: false, refuse_manual_stop: false, need_daemon_reload: false,
      fragment_path: null, source_path: null, drop_in_paths: null,
      transient: false, template: null, current_job: null, invocation_id: null,
      timestamps: {}, relationships: [],
      first_seen_at: '2026-08-27T21:46:55Z', last_seen_at: '2026-08-27T21:47:11Z',
    },
  ],
};

describe('System list', () => {
  it('lists units and links each to its detail page', async () => {
    mockJson(UNIT_LIST);
    render(
      <MemoryRouter>
        <SystemList />
      </MemoryRouter>,
    );

    const link = await screen.findByRole('link', { name: 'ModemManager.service' });
    expect(link).toHaveAttribute('href', '/system/obj_a');
  });

  it('filters by search without asking the backend again', async () => {
    mockJson(UNIT_LIST);
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SystemList />
      </MemoryRouter>,
    );

    await screen.findByRole('link', { name: 'ModemManager.service' });
    await user.type(screen.getByRole('searchbox'), 'cups');

    await waitFor(() =>
      expect(screen.queryByRole('link', { name: 'ModemManager.service' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('link', { name: 'cups.socket' })).toBeInTheDocument();
    // One *unit list* read, however many keystrokes. The page also reads the host once for
    // its breadcrumb, which is a different endpoint and not what this asserts.
    const unitReads = vi
      .mocked(fetch)
      .mock.calls.filter(([input]) => urlOf(input).includes('/systemd/units'));
    expect(unitReads).toHaveLength(1);
  });

  it('explains an empty filter result as a filter result, not as an empty estate', async () => {
    mockJson(UNIT_LIST);
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SystemList />
      </MemoryRouter>,
    );

    await screen.findByRole('link', { name: 'ModemManager.service' });
    await user.type(screen.getByRole('searchbox'), 'zzzz-no-such-unit');

    expect(await screen.findByText(/No unit matches/i)).toBeInTheDocument();
  });
});
