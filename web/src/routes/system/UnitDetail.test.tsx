import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { UnitDetail } from './UnitDetail';
import { RunDetail } from '../operations/RunDetail';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import type { Run, SystemdUnit, SystemdUnitList, SystemdRelationship } from '@/api/types';
import runFixture from '@/test/fixtures/systemd-run.json';

afterEach(() => vi.unstubAllGlobals());

const base = BACKEND['/api/v1/systemd/units/obj_unit'] as SystemdUnit;
const list = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
const sourceId = 'source:with space#?';
const relation: SystemdRelationship = {
  kind: 'After', group: 'ordering', target_unit: base.canonical_id,
  canonical_target: null, target_object_id: base.object_id,
  resolution: 'resolved', estate_state: 'current', source: 'systemd.dbus',
};
const detail: SystemdUnit = {
  ...base,
  relationships: [{ ...relation, kind: 'Wants', target_unit: 'outbound.service', target_object_id: 'outbound:with space' }],
  incoming_references: {
    host_id: 'host_1', last_inventory_sweep: { ...list.last_sweep!, status: 'partial' },
    inventory_freshness: 'stale', excluded_ambiguous_count: 1,
    references: [{
      source_object_id: sourceId, source_canonical_id: 'source.service',
      source_observed_in_latest_sweep: true,
      source_observation: { ...base.observation!, freshness: 'stale', fidelity: 'partial', gaps: ['read failed'] },
      relationship: relation,
    }],
  },
};

function mount(path = '/system/obj_unit?tab=relationships') {
  return render(<MemoryRouter initialEntries={[path]}>
    <Link to="/system/next?tab=relationships">Next unit</Link>
    <Routes><Route path="system/:objectId" element={<UnitDetail />} /></Routes>
  </MemoryRouter>);
}

describe('incoming systemd references', () => {
  it('preserves direction, shows snapshot limitations and filters both directions', async () => {
    stubBackend({ '/api/v1/systemd/units/obj_unit': detail });
    mount();
    const incoming = await screen.findByRole('table', { name: 'Incoming unit references' });
    expect(within(incoming).getByText('After')).toBeInTheDocument();
    expect(within(incoming).queryByText('Before')).not.toBeInTheDocument();
    expect(screen.getByText(/not a complete dependency graph/)).toBeInTheDocument();
    expect(screen.getByText(/conflicting alias ownership/)).toBeInTheDocument();
    expect(screen.getByText('read failed')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'outbound.service' })).toHaveAttribute('href', '/system/outbound%3Awith%20space');
    const filter = screen.getByRole('searchbox', { name: 'Filter relationships' });
    fireEvent.change(filter, { target: { value: 'After' } });
    expect(screen.getByRole('link', { name: 'source.service' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'outbound.service' })).not.toBeInTheDocument();
    fireEvent.change(filter, { target: { value: 'missing' } });
    expect(screen.getByText('No incoming reference matches')).toBeInTheDocument();
    expect(screen.getByText(/No matches does not prove no dependents/)).toBeInTheDocument();
  });

  it('activates encoded source routes, preserves the tab and resets local filter', async () => {
    stubBackend({
      '/api/v1/systemd/units/obj_unit': detail,
      [`/api/v1/systemd/units/${encodeURIComponent(sourceId)}`]: { ...base, object_id: sourceId, canonical_id: 'source.service' },
    });
    mount();
    const link = await screen.findByRole('link', { name: 'source.service' });
    expect(link).toHaveAttribute('href', `/system/${encodeURIComponent(sourceId)}?tab=relationships`);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'source' } });
    link.focus();
    await userEvent.keyboard('{Enter}');
    await screen.findByText('Incoming references unavailable');
    expect(screen.getByRole('tab', { name: /Relationships/ })).toHaveAttribute('aria-selected', 'true');
    expect(vi.mocked(fetch).mock.calls.some(([input]) => urlOf(input) === `/api/v1/systemd/units/${encodeURIComponent(sourceId)}`)).toBe(true);
    expect(screen.getByRole('searchbox')).toHaveValue('');
  });

  it.each(['failed', 'partial', 'ok', null])('does not claim no dependents for an empty %s inventory', async (status) => {
    stubBackend({ '/api/v1/systemd/units/obj_unit': {
      ...detail, incoming_references: {
        ...detail.incoming_references!, references: [],
        last_inventory_sweep: status ? { ...list.last_sweep!, status } : null,
      },
    } });
    mount();
    await screen.findByText('No stored incoming references');
    expect(screen.getByText(/Dependents outside this evidence remain unknown/)).toBeInTheDocument();
  });

  it('shows loading and errors instead of empty reference claims', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => urlOf(input).endsWith('/obj_unit')
      ? new Promise<Response>((resolve) => { finish = resolve; })
      : Promise.resolve(new Response(JSON.stringify(list)))));
    mount();
    expect(screen.getByText('Reading unit…')).toBeInTheDocument();
    expect(screen.queryByText('No stored incoming references')).not.toBeInTheDocument();
    await act(async () => finish(new Response('{}', { status: 500 })));
    await screen.findByRole('button', { name: /try again/i });
    expect(screen.queryByText('No stored incoming references')).not.toBeInTheDocument();
  });

  it('ignores an obsolete response after navigation', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.endsWith('/obj_unit')) return new Promise<Response>((resolve) => { finish = resolve; });
      const body = url.endsWith('/next') ? { ...base, object_id: 'next', canonical_id: 'next.service' } : BACKEND[url];
      return Promise.resolve(new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 }));
    }));
    mount();
    await userEvent.click(screen.getByRole('link', { name: 'Next unit' }));
    await screen.findByText('Incoming references unavailable');
    await act(async () => finish(new Response(JSON.stringify(detail))));
    await waitFor(() => expect(screen.queryByRole('link', { name: 'source.service' })).not.toBeInTheDocument());
  });
});

describe('service execution diagnosis', () => {
  it('requires a deliberate journal read, links to the same service Lifecycle tab, and never creates a Run', async () => {
    const journal = {
      object_id: 'obj_unit', canonical_id: base.canonical_id, status: 'ok', retrieved_at: '2026-11-01T00:00:00Z',
      since: '2026-11-01T00:00:00Z', boot_id: 'boot-test', truncated: false, coverage: 'complete', gaps: [],
      entries: [{ timestamp: null, priority: '6', message: 'nonsecret fixture', boot_id: 'boot-test', invocation_id: null }],
    };
    stubBackend({ '/api/v1/systemd/units/obj_unit': detail, '/api/v1/systemd/units': list,
      '/api/v1/systemd/units/obj_unit/journal': journal });
    mount('/system/obj_unit');
    await screen.findByRole('heading', { name: 'Execution diagnosis' });
    expect(vi.mocked(fetch).mock.calls.some(([input]) => urlOf(input).endsWith('/journal'))).toBe(false);
    await userEvent.click(screen.getByRole('tab', { name: 'Journal' }));
    expect(await screen.findByRole('link', { name: /Lifecycle for/ })).toHaveAttribute('href', '/system/obj_unit?tab=lifecycle');
    expect(vi.mocked(fetch).mock.calls.some(([input]) => urlOf(input).endsWith('/journal'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByText('nonsecret fixture')).toBeInTheDocument();
    const journalCall = vi.mocked(fetch).mock.calls.find(([input]) => urlOf(input).endsWith('/journal'));
    expect(journalCall?.[1]?.method).toBe('POST');
    expect(journalCall?.[1]?.body).toBe(JSON.stringify({ acknowledge_sensitive: true }));
    expect(vi.mocked(fetch).mock.calls.some(([input, init]) => urlOf(input).includes('/runs') && init?.method === 'POST')).toBe(false);
    await userEvent.click(screen.getByRole('link', { name: /Lifecycle for/ }));
    expect(await screen.findByRole('tab', { name: 'Lifecycle' })).toHaveAttribute('aria-selected', 'true');
    expect(vi.mocked(fetch).mock.calls.some(([input, init]) => urlOf(input).includes('/runs') && init?.method === 'POST')).toBe(false);
  });

  it('keeps one incident journey on the canonical service and durable Run boundary', async () => {
    const journal = {
      object_id: 'obj_unit', canonical_id: base.canonical_id, status: 'ok', retrieved_at: '2026-11-01T00:00:00Z',
      since: '2026-11-01T00:00:00Z', boot_id: 'boot-test', truncated: false, coverage: 'complete', gaps: [],
      entries: [{ timestamp: null, priority: '3', message: 'nonsecret incident fixture', boot_id: 'boot-test', invocation_id: null }],
    };
    const run = structuredClone(runFixture) as unknown as Run;
    run.run_id = 'run_incident';
    run.object_id = base.object_id;
    run.object_name = base.canonical_id;
    run.preview.what = { ...run.preview.what, object_id: base.object_id, object_name: base.canonical_id };
    run.state = 'preview';
    run.preview.how = { ...run.preview.how, availability: 'available', eligibility: 'eligible', blockers: [], note: 'Eligible service action.' };
    run.preview.protection = { ...run.preview.protection, status: 'clear', unresolved: [], missing_evidence: [] };
    run.preview.validity = { state: 'current', as_of: '2026-11-01T00:00:00Z', reasons: [] };
    run.preview.confirmation = { ...run.preview.confirmation, required: true, method: 'acknowledge', satisfiable: true, satisfied: false, unsatisfiable_reason: null };
    let current = run;
    const requests: Array<{ path: string; init: RequestInit | undefined }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = urlOf(input); requests.push({ path, init });
      if (path === '/api/v1/systemd/units/obj_unit') return new Response(JSON.stringify(detail));
      if (path === '/api/v1/systemd/units') return new Response(JSON.stringify(list));
      if (path === '/api/v1/systemd/units/obj_unit/journal') return new Response(JSON.stringify(journal));
      if (path === '/api/v1/runs' && init?.method === 'POST') return new Response(JSON.stringify(current), { status: 201 });
      if (path === '/api/v1/runs/run_incident/confirm') {
        current = {
          ...current,
          preview: { ...current.preview, confirmation: { ...current.preview.confirmation, satisfied: true } },
          confirmation: {
            confirmation_id: 'confirmation', preview_id: current.preview.preview_id, preview_digest: current.preview.preview_digest,
            required_method: 'acknowledge', method: 'acknowledge', typed_statement: null, policy: '', source: 'authenticated_request',
            satisfied_at: '2026-11-01T00:00:01Z', consumed: false, consumed_at: null, consumed_by_attempt_id: null,
          },
        };
        return new Response(JSON.stringify(current));
      }
      if (path === '/api/v1/runs/run_incident/apply') {
        current = {
          ...current, state: 'recovery_required', host_effect: 'written', change_id: 'change_incident',
          change: {
            result: 'recovery_required', mutation: { outcome: 'written', reason: 'provider completed dispatch' },
            verification: { outcome: 'mismatch' }, recovery: { reason: 'verification mismatch; no automatic retry or rollback is available' },
          } as Run['change'],
        };
        return new Response(JSON.stringify(current));
      }
      if (path === '/api/v1/runs/run_incident') return new Response(JSON.stringify(current));
      return new Response(JSON.stringify({ error: { code: 'not_found', message: 'not found', detail: {} } }), { status: 404 });
    }));
    render(<MemoryRouter initialEntries={['/system/obj_unit?tab=journal']}><Routes>
      <Route path="system/:objectId" element={<UnitDetail />} />
      <Route path="operations/runs/:runId" element={<RunDetail />} />
      <Route path="operations" element={<><p>Operations list</p><Link to="/operations/runs/run_incident">Reopen retained Run</Link></>} />
    </Routes></MemoryRouter>);

    // A journal request has no side effect on planning and keeps the canonical Lifecycle handoff.
    await screen.findByRole('button', { name: /acknowledge and read/i });
    expect(requests.filter(({ path }) => path === '/api/v1/systemd/units/obj_unit/journal')).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    await screen.findByText('nonsecret incident fixture');
    const lifecycle = screen.getByRole('link', { name: `Lifecycle for ${base.canonical_id}` });
    expect(lifecycle).toHaveAttribute('href', '/system/obj_unit?tab=lifecycle');
    await userEvent.click(lifecycle);
    const plan = await screen.findByRole('button', { name: 'Plan restart' });
    await userEvent.click(plan);
    expect(requests.filter(({ path, init }) => path === '/api/v1/runs' && init?.method === 'POST')).toHaveLength(1);
    const planned = requests.find(({ path, init }) => path === '/api/v1/runs' && init?.method === 'POST')!;
    expect(JSON.parse(planned.init?.body as string)).toEqual({ operation: { type: 'systemd.service.restart', object_id: base.object_id } });

    await userEvent.click(await screen.findByRole('link', { name: /Open Composer/ }));
    await userEvent.click(await screen.findByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', { name: 'Confirm preview' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply approved preview' })).toBeEnabled());
    const confirmation = requests.find(({ path }) => path.endsWith('/confirm'))!;
    expect(JSON.parse(confirmation.init?.body as string)).toEqual({ preview_id: run.preview.preview_id, expected_preview_digest: run.preview.preview_digest, acknowledge: true });
    await userEvent.click(screen.getByRole('button', { name: 'Apply approved preview' }));
    expect(await screen.findByText(/Recorded state: recovery_required/)).toBeInTheDocument();
    expect(screen.getByLabelText('Recorded outcome')).toHaveTextContent('Change result: recovery_required · Mutation: written · Verification: mismatch');
    expect(screen.getByLabelText('Recorded outcome')).toHaveTextContent('no automatic retry or rollback is available');
    expect(requests.filter(({ path }) => path.endsWith('/apply'))).toHaveLength(1);

    await userEvent.click(screen.getByRole('link', { name: 'Close Composer' }));
    await userEvent.click(await screen.findByRole('link', { name: 'Reopen retained Run' }));
    expect(await screen.findByText(/Recorded state: recovery_required/)).toBeInTheDocument();
    expect(screen.getByLabelText('Recorded outcome')).toHaveTextContent('no automatic retry or rollback is available');
    expect(requests.filter(({ path, init }) => path === '/api/v1/runs' && init?.method === 'POST')).toHaveLength(1);
    expect(requests.filter(({ path }) => path.endsWith('/apply'))).toHaveLength(1);
    expect(requests.every(({ path }) => path.startsWith('/api/v1/'))).toBe(true);
  });

  it('keeps a prior failure distinct from current active, health, and freshness evidence', async () => {
    stubBackend({ '/api/v1/systemd/units/obj_unit': {
      ...detail,
      service: {
        type: 'simple', main_pid: 123456789012345, control_pid: null, exec_main_pid: 123456789012345,
        result: 'exit-code', exec_main_code: 1, exec_main_status: 23, restart: 'on-failure',
        restart_usec: 0, n_restarts: 2, remain_after_exit: null, guess_main_pid: null,
        exec_main_start_timestamp_monotonic: 123456789, watchdog_usec: null,
        watchdog_timestamp_monotonic: null, watchdog_signal: null, watchdog_pid: null,
      },
    } });
    mount('/system/obj_unit');
    expect(await screen.findByRole('heading', { name: 'Execution diagnosis' })).toBeInTheDocument();
    expect(screen.getByText('exit-code')).toBeInTheDocument();
    expect(screen.getByText('exited (status 23)')).toBeInTheDocument();
    expect(screen.getByText('2 automatic restarts reported')).toBeInTheDocument();
    expect(screen.getByText('0 µs')).toBeInTheDocument();
    expect(screen.getAllByText('active').length).toBeGreaterThan(0);
    expect(screen.getByText('healthy')).toBeInTheDocument();
    expect(screen.getByText('current')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: 'Service facts' }));
    expect(screen.getAllByText('123456789012345').length).toBeGreaterThan(0);
    const rawFacts = screen.getByText('Raw manager facts');
    rawFacts.focus();
    const keydown = vi.fn();
    rawFacts.addEventListener('keydown', keydown);
    await userEvent.keyboard('{Enter}');
    expect(rawFacts).toHaveFocus();
    expect(keydown).toHaveBeenCalledWith(expect.objectContaining({ key: 'Enter' }));
    // jsdom dispatches Enter on summary but does not implement its native disclosure default.
    await userEvent.click(rawFacts);
    expect(rawFacts.closest('details')).toHaveAttribute('open');
    expect(screen.getByText('exec_main_code')).toBeInTheDocument();
    expect(screen.getByText('23')).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
  });

  it('keeps the service route readable when the manager omits optional facts', async () => {
    stubBackend({ '/api/v1/systemd/units/obj_unit': {
      ...detail,
      service: { type: null } as NonNullable<SystemdUnit['service']>,
    } });
    mount('/system/obj_unit');
    expect(await screen.findByRole('heading', { name: 'Execution diagnosis' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'the manager did not report a service result' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'Service facts' }));
    expect(screen.getByRole('tab', { name: 'Service facts' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
  });

  it('does not add diagnosis to non-service units', async () => {
    stubBackend({ '/api/v1/systemd/units/obj_unit': { ...detail, unit_type: 'socket', service: null } });
    mount('/system/obj_unit');
    await screen.findByText('State');
    expect(screen.queryByRole('heading', { name: 'Execution diagnosis' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Service facts' })).not.toBeInTheDocument();
  });
});
