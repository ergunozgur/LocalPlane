import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { RunDetail } from '@/routes/operations/RunDetail';
import { ServicePlan } from './ServicePlan';
import { AuthenticationProvider } from '@/auth/AuthProvider';
import { refreshAll } from '@/hooks/useResource';
import { act } from '@testing-library/react';
import type { Run, SystemdUnit } from '@/api/types';
import fixture from '@/test/fixtures/systemd-run.json';
import { BACKEND } from '@/test/backend';

let sequence = 0;
function planned(): Run {
  const run = structuredClone(fixture) as unknown as Run;
  run.run_id = `composer_${++sequence}`;
  run.preview.how = { ...run.preview.how, availability: 'available', eligibility: 'eligible', blockers: [], note: 'Eligible ordinary service action.' };
  run.preview.protection = { ...run.preview.protection, status: 'clear', unresolved: [], missing_evidence: [] };
  run.preview.validity = { state: 'current', as_of: '', reasons: [] };
  run.preview.confirmation = { ...run.preview.confirmation, method: 'acknowledge', required: true, satisfiable: true, satisfied: false, unsatisfiable_reason: null };
  return run;
}
function confirmed(run: Run): Run {
  return { ...run, preview: { ...run.preview, confirmation: { ...run.preview.confirmation, satisfied: true } }, confirmation: {
    confirmation_id: 'confirmation', preview_id: run.preview.preview_id, preview_digest: run.preview.preview_digest,
    required_method: run.preview.confirmation.method, method: run.preview.confirmation.method, typed_statement: null,
    policy: '', source: 'authenticated_request', satisfied_at: '', consumed: false, consumed_at: null, consumed_by_attempt_id: null,
  } };
}
function show(run: Run) {
  return render(<MemoryRouter initialEntries={[`/operations/runs/${run.run_id}`]}><Routes>
    <Route path="/operations/runs/:runId" element={<RunDetail />} />
    <Route path="/operations" element={<p>Operations list</p>} />
  </Routes></MemoryRouter>);
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

it.each(['start', 'stop', 'restart'] as const)('plans %s with only typed operation and object identity', async (verb) => {
  const run = planned();
  const fetch = vi.fn().mockResolvedValue(response(run, 201)); vi.stubGlobal('fetch', fetch);
  const unit = BACKEND['/api/v1/systemd/units/obj_unit'] as SystemdUnit;
  render(<MemoryRouter><ServicePlan unit={unit} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: `Plan ${verb}` }));
  await screen.findByRole('link', { name: /Open Composer/ });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/runs');
  expect(JSON.parse(fetch.mock.calls[0]?.[1].body as string)).toEqual({ operation: { type: `systemd.service.${verb}`, object_id: unit.object_id } });
});

it('offers no plan on an unloaded unit or non-service', () => {
  const unit = BACKEND['/api/v1/systemd/units/obj_unit'] as SystemdUnit;
  const view = render(<MemoryRouter><ServicePlan unit={{ ...unit, unit_type: 'socket' }} /></MemoryRouter>);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  view.rerender(<MemoryRouter><ServicePlan unit={{ ...unit, load_state: 'not-found' }} /></MemoryRouter>);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

it('requires acknowledgement, binds exact preview, then applies once without a body', async () => {
  const run = planned(); let current = run;
  const fetch = vi.fn(async (path: string, _options: RequestInit) => {
    if (path.endsWith('/confirm')) current = confirmed(run);
    if (path.endsWith('/apply')) current = { ...current, state: 'succeeded', host_effect: 'written' };
    return response(current);
  }); vi.stubGlobal('fetch', fetch); show(run);
  const confirm = await screen.findByRole('button', { name: 'Confirm preview' });
  expect(confirm).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Apply approved preview' })).toBeDisabled();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox')); confirm.focus(); fireEvent.click(confirm);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Apply approved preview' })).toBeEnabled());
  expect(screen.getByRole('button', { name: 'Apply approved preview' })).toHaveFocus();
  const confirmCall = fetch.mock.calls.find(([path]) => path.endsWith('/confirm'))!;
  expect(JSON.parse(confirmCall[1].body as string)).toEqual({ preview_id: run.preview.preview_id, expected_preview_digest: run.preview.preview_digest, acknowledge: true });
  fireEvent.click(screen.getByRole('button', { name: 'Apply approved preview' }));
  await screen.findByText(/Recorded state: succeeded/);
  const applies = fetch.mock.calls.filter(([path]) => path.endsWith('/apply'));
  expect(applies).toHaveLength(1); expect(applies[0]?.[1].body).toBeUndefined();
  expect(screen.queryByRole('button', { name: 'Cancel before write' })).not.toBeInTheDocument();
});

describe('ordinary apply refuses unsafe or unsupported previews', () => {
  it.each(['guarded', 'blocked', 'not_implemented', 'unsatisfiable', 'stale', 'unknown-protection', 'unsupported-method', 'wrong-preview', 'wrong-digest', 'consumed'])('%s', async (kind) => {
    const run = confirmed(planned());
    if (kind === 'guarded' || kind === 'blocked') run.preview.how.eligibility = kind;
    if (kind === 'not_implemented') run.preview.how.availability = kind;
    if (kind === 'unsatisfiable') run.preview.confirmation.satisfiable = false;
    if (kind === 'stale') run.preview.validity.state = 'stale';
    if (kind === 'unknown-protection') run.preview.protection.status = 'unknown';
    if (kind === 'unsupported-method') run.preview.confirmation.method = 'future';
    if (kind === 'wrong-preview') run.confirmation!.preview_id = 'other';
    if (kind === 'wrong-digest') run.confirmation!.preview_digest = 'other';
    if (kind === 'consumed') run.confirmation!.consumed = true;
    const fetch = vi.fn().mockResolvedValue(response(run)); vi.stubGlobal('fetch', fetch); show(run);
    const apply = await screen.findByRole('button', { name: 'Apply approved preview' });
    expect(apply).toBeDisabled(); fireEvent.click(apply); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it.each(['docker.container.start', 'docker.container.stop', 'docker.container.restart', 'network.interface.reconcile_mtu', 'systemd.service.self-impact-override', 'shell.session', 'command'])('has no %s mutation controls', async (operation) => {
    const run = confirmed(planned()); run.operation = operation;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(run))); show(run);
    await screen.findByRole('region', { name: 'Change Composer' });
    expect(screen.queryByRole('button', { name: /apply|confirm|cancel/i })).not.toBeInTheDocument();
  });
});

it('requires the exact published name only for typed confirmation', async () => {
  const run = planned(); run.preview.confirmation.method = 'typed';
  const fetch = vi.fn().mockResolvedValue(response(run)); vi.stubGlobal('fetch', fetch); show(run);
  const input = await screen.findByRole('textbox'); fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.change(input, { target: { value: 'wrong' } }); expect(screen.getByRole('button', { name: 'Confirm preview' })).toBeDisabled();
  fireEvent.change(input, { target: { value: run.preview.what.object_name } }); fireEvent.click(screen.getByRole('button', { name: 'Confirm preview' }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(JSON.parse(fetch.mock.calls[1]?.[1].body as string).acknowledge_object).toBe(run.preview.what.object_name);
});

it('resets local acknowledgement when a refreshed preview is replaced', async () => {
  const run = planned(); let current = run;
  vi.stubGlobal('fetch', vi.fn(async () => response(current))); show(run);
  fireEvent.click(await screen.findByRole('checkbox'));
  current = { ...run, preview: { ...run.preview, preview_id: 'replacement' } };
  fireEvent.click(screen.getByRole('button', { name: 'Refresh Run' }));
  await waitFor(() => expect(screen.getByRole('checkbox')).not.toBeChecked());
  expect(screen.getByRole('button', { name: 'Confirm preview' })).toBeDisabled();
});

it.each(['lost', 'stale', 'origin', 'provider'])('shows %s apply failure and only rereads the same Run', async (kind) => {
  const run = confirmed(planned());
  const codes = { stale: 'preview_stale', origin: 'origin_invalid', provider: 'provider_unavailable' };
  const fetch = vi.fn(async (path: string) => {
    if (path.endsWith('/apply')) {
      if (kind === 'lost') throw new TypeError('connection lost');
      return response({ error: { code: codes[kind as keyof typeof codes], message: 'Request refused', detail: {} } }, kind === 'origin' ? 403 : kind === 'stale' ? 409 : 503);
    }
    return response(run);
  }); vi.stubGlobal('fetch', fetch);
  const view = show(run); fireEvent.click(await screen.findByRole('button', { name: 'Apply approved preview' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(kind === 'lost' ? 'outcome are unknown' : codes[kind as keyof typeof codes]);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh Run' }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: 'Apply approved preview' })).toBeDisabled();
  view.unmount(); show(run);
  expect(await screen.findByRole('button', { name: 'Apply approved preview' })).toBeDisabled();
  expect(fetch.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(1);
});

it('closing during a pending apply does not cancel or repeat it', async () => {
  const run = confirmed(planned()); let finish!: (value: Response) => void;
  const fetch = vi.fn((path: string) => path.endsWith('/apply') ? new Promise<Response>((resolve) => { finish = resolve; }) : Promise.resolve(response(run)));
  vi.stubGlobal('fetch', fetch); show(run);
  fireEvent.click(await screen.findByRole('button', { name: 'Apply approved preview' }));
  expect(screen.getByRole('button', { name: 'Apply approved preview' })).toBeDisabled();
  fireEvent.click(screen.getByRole('link', { name: 'Close Composer' }));
  await screen.findByText('Operations list'); finish(response({ ...run, state: 'succeeded' }));
  expect(fetch.mock.calls.filter(([path]) => path.endsWith('/cancel'))).toHaveLength(0);
  expect(fetch.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(1);
});

it('cancels only before write and shows the retained cancelled Run', async () => {
  const run = planned(); const fetch = vi.fn(async (path: string) => response(path.endsWith('/cancel') ? { ...run, state: 'cancelled' } : run));
  vi.stubGlobal('fetch', fetch); show(run);
  fireEvent.click(await screen.findByRole('button', { name: 'Cancel before write' }));
  await screen.findByText(/Recorded state: cancelled/);
  expect(screen.queryByRole('button', { name: 'Cancel before write' })).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(0);
});

it('retains an ambiguous planning failure without silently creating another Run', async () => {
  const fetch = vi.fn().mockRejectedValue(new TypeError('response lost')); vi.stubGlobal('fetch', fetch);
  render(<MemoryRouter><ServicePlan unit={BACKEND['/api/v1/systemd/units/obj_unit'] as SystemdUnit} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: 'Plan restart' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('a Run may already have been created');
  expect(screen.getByRole('button', { name: 'Plan restart' })).toBeDisabled();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('refuses apply before dispatch when the attempt marker cannot be stored', async () => {
  const run = confirmed(planned()); const fetch = vi.fn().mockResolvedValue(response(run)); vi.stubGlobal('fetch', fetch);
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage denied'); });
  show(run);
  fireEvent.click(await screen.findByRole('button', { name: 'Apply approved preview' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('No apply request was sent');
  expect(fetch).toHaveBeenCalledTimes(1); write.mockRestore();
});

it('treats a body lost after response headers as delivery unknown', async () => {
  const run = confirmed(planned());
  const fetch = vi.fn(async (path: string) => path.endsWith('/apply')
    ? { ok: true, status: 200, text: () => Promise.reject(new Error('body lost')) }
    : response(run));
  vi.stubGlobal('fetch', fetch); show(run);
  fireEvent.click(await screen.findByRole('button', { name: 'Apply approved preview' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('request delivery is unknown');
  expect(screen.getByRole('button', { name: 'Apply approved preview' })).toBeDisabled();
});

it('renders an embedded uncertain mutation and failed verification without success claims', async () => {
  const run = confirmed(planned());
  run.state = 'recovery_required'; run.host_effect = 'write_unknown'; run.change_id = 'change';
  run.change = { result: 'recovery_required', mutation: { outcome: 'write_unknown', reason: 'provider_response_lost' }, verification: { outcome: 'observation_unavailable' }, recovery: { reason: 'end_state_unproven' } } as Run['change'];
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(run))); show(run);
  expect(await screen.findByLabelText('Recorded outcome')).toHaveTextContent('Mutation: write_unknown · Verification: observation_unavailable');
  expect(screen.queryByRole('button', { name: /apply|cancel|confirm/i })).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Inspect Change outcome and verification' })).toHaveAttribute('href', '/operations/changes/change');
});


it('does not mount service planning controls before authentication', async () => {
  const fetch = vi.fn().mockResolvedValue(response({ error: { code: 'authentication_required', message: 'Sign in', detail: {} } }, 401));
  vi.stubGlobal('fetch', fetch);
  render(<AuthenticationProvider><MemoryRouter><ServicePlan unit={BACKEND['/api/v1/systemd/units/obj_unit'] as SystemdUnit} /></MemoryRouter></AuthenticationProvider>);
  await screen.findByRole('button', { name: 'Sign in' });
  expect(screen.queryByRole('button', { name: /Plan/ })).not.toBeInTheDocument();
  expect(fetch.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
});


it('rereads the Run through the shell refresh signal without issuing a write', async () => {
  const run = planned(); let current = run;
  const fetch = vi.fn(async () => response(current)); vi.stubGlobal('fetch', fetch); show(run);
  await screen.findByRole('button', { name: 'Confirm preview' });
  current = { ...run, state: 'cancelled' };
  act(() => refreshAll());
  await screen.findByText(/Recorded state: cancelled/);
  expect(fetch.mock.calls).toHaveLength(2);
});


it('shows every execution blocker even when the backend reports more than twelve', async () => {
  const run = planned(); run.preview.how.eligibility = 'blocked';
  run.preview.how.blockers = Array.from({ length: 15 }, (_, n) => `execution_blocker_${n}`);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(run))); show(run);
  await screen.findByText('execution_blocker_14');
  for (const code of run.preview.how.blockers) expect(screen.getByText(code)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply approved preview' })).toBeDisabled();
});
