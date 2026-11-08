import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { ChangeDetail } from '@/routes/operations/ChangeDetail';
import type { Change } from '@/api/types';
import { BACKEND } from '@/test/backend';
import runFixture from '@/test/fixtures/systemd-run.json';
import { refreshAll } from '@/hooks/useResource';
import { AuthenticationProvider } from '@/auth/AuthProvider';
import userEvent from '@testing-library/user-event';

let sequence = 0;
function held(): Change {
  const change = structuredClone(BACKEND['/api/v1/changes/chg_1']) as Change;
  change.change_id = `recovery_${++sequence}`;
  change.operation = 'systemd.service.restart'; change.change_kind = 'action'; change.action = 'restart';
  change.object_name = 'example.service';
  change.recovery = { ...change.recovery, revision: 7,
    confirmation_requirement: { method: 'typed', object_name: change.object_name, policy: 'typed service policy' },
    available_actions: ['assess', 'confirm_retry', 'retry', 'resolve'],
  };
  return change;
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function setup(change: Change, action?: (kind: string, options: RequestInit) => Promise<Response>) {
  let current = change;
  const fetch = vi.fn(async (path: string, _options: RequestInit) => {
    if (path.includes('/recovery/')) return action ? action(path.split('/').at(-1)!, _options) : response(current);
    if (path.endsWith('/recovery-holds')) return response({ total_count: current.recovery.object_write_locked ? 3 : 2, changes: [] });
    if (path.includes('/runs/')) return response(runFixture);
    return response(current);
  });
  vi.stubGlobal('fetch', fetch);
  return { fetch, setCurrent: (next: Change) => { current = next; } };
}
function show(change: Change) {
  return render(<MemoryRouter initialEntries={[`/operations/changes/${change.change_id}`]}><Routes><Route path="/operations/changes/:changeId" element={<ChangeDetail />} /></Routes></MemoryRouter>);
}
const button = (name: string) => screen.getByRole('button', { name });
async function ready() { await screen.findByRole('region', { name: 'Recovery console' }); await waitFor(() => expect(button('Assess current service state')).toBeEnabled()); }
function acknowledgeGrant(change: Change) {
  fireEvent.click(screen.getByRole('checkbox', { name: 'I authorize one recovery retry of this exact original action.' }));
  fireEvent.change(screen.getByLabelText(`Type ${change.object_name} to authorize retry`), { target: { value: change.object_name } });
}
function consumedRetry(change: Change): Change['recovery']['attempts'][number] {
  return { attempt_id: 'retry_consumed', sequence: 1, kind: 'retry',
    started_at: '2026-11-08T10:00:00Z', finished_at: '2026-11-08T10:00:01Z',
    outcome: 'not_proven', refusal_code: null, releases_hold: false, management_path: 'clear',
    protection_evidence_id: 'protection_retry', evidence: change.verification,
    mutation: { ...change.mutation, attempt_id: 'mutation_retry', outcome: 'written', dispatch_began_at: '2026-11-08T10:00:00Z', settled_at: '2026-11-08T10:00:01Z' },
    host_effect: 'written', verification: { ...change.verification, outcome: 'mismatch' },
    confirmation_id: 'grant_first', operator_statement: null, note: null };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); });

it('grants only after exact typed acknowledgment, then retries separately with exact revision and grant', async () => {
  const change = held();
  const grant = { confirmation_id: 'grant_exact', required_method: 'typed', method: 'typed', policy: 'typed service policy', source: 'authenticated_request', satisfied_at: '2026-11-08T10:00:00Z' };
  const granted = { ...change, recovery: { ...change.recovery, revision: 8, authority: grant } };
  const c = setup(change, async (kind) => { if (kind === 'confirm') { c.setCurrent(granted); return response(granted); } return response(granted); });
  show(change); await ready();
  expect(button('Grant one recovery retry')).toBeDisabled(); expect(button('Retry original action once')).toBeDisabled();
  fireEvent.change(screen.getByLabelText(`Type ${change.object_name} to authorize retry`), { target: { value: 'wrong' } });
  fireEvent.click(screen.getByRole('checkbox', { name: /I authorize/ }));
  expect(button('Grant one recovery retry')).toBeDisabled();
  fireEvent.change(screen.getByLabelText(`Type ${change.object_name} to authorize retry`), { target: { value: change.object_name } });
  fireEvent.click(button('Grant one recovery retry'));
  await waitFor(() => expect(button('Retry original action once')).toBeEnabled());
  expect(c.fetch.mock.calls.filter(([path]) => path.endsWith('/retry'))).toHaveLength(0);
  const confirm = c.fetch.mock.calls.find(([path]) => path.endsWith('/confirm'))!;
  expect(confirm[1].headers).toMatchObject({ 'If-Recovery-Revision': '7' });
  expect(JSON.parse(confirm[1].body as string)).toEqual({ acknowledge: true, acknowledge_object: change.object_name, expected_recovery_reason: change.recovery.reason });
  fireEvent.click(button('Retry original action once')); fireEvent.click(button('Retry original action once'));
  await waitFor(() => expect(c.fetch.mock.calls.filter(([path]) => path.endsWith('/retry'))).toHaveLength(1));
  const retry = c.fetch.mock.calls.find(([path]) => path.endsWith('/retry'))!;
  expect(retry[1].headers).toMatchObject({ 'If-Recovery-Revision': '8', 'If-Recovery-Confirmation': 'grant_exact' });
  expect(retry[1].body).toBeUndefined();
});

it('assessment with an outstanding grant never confirms or retries and rereads authoritative hold counts', async () => {
  const change = held();
  change.recovery.authority = { confirmation_id: 'other_tab', required_method: 'typed', method: 'typed', policy: 'typed service policy', source: 'authenticated_request', satisfied_at: '2026-11-08T10:00:00Z' };
  const resolved = { ...change, recovery: { ...change.recovery, revision: 8, state: 'resolved', object_write_locked: false, released_by: 'assess' } };
  const c = setup(change, async () => { c.setCurrent(resolved); return response(resolved); });
  show(change); await ready(); fireEvent.click(button('Assess current service state'));
  await screen.findByText(/Authoritative unresolved holds on this host: 2/);
  expect(c.fetch.mock.calls.filter(([, options]) => options.method === 'POST').map(([path]) => path.split('/').at(-1))).toEqual(['assess']);
  const assess = c.fetch.mock.calls.find(([path]) => path.endsWith('/assess'))!;
  expect(assess[1].body).toBeUndefined(); expect(assess[1].headers).toMatchObject({ 'If-Recovery-Revision': '7' });
  expect(screen.getByText('Released / not held')).toBeInTheDocument();
  expect(within(screen.getByRole('region', { name: 'Recovery console' })).getByText('recovery_required')).toBeInTheDocument();
});

it('manual release requires separate typed name and acknowledgment and preserves original uncertainty', async () => {
  const change = held(); const c = setup(change); show(change); await ready();
  acknowledgeGrant(change); expect(button('Release object hold manually')).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: /release does not verify/ }));
  fireEvent.change(screen.getByLabelText(`Type ${change.object_name} to release hold`), { target: { value: change.object_name } });
  fireEvent.change(screen.getByLabelText(/Recorded note/), { target: { value: 'Handled independently; original write still unknown.' } });
  fireEvent.click(button('Release object hold manually'));
  await waitFor(() => expect(c.fetch.mock.calls.some(([path]) => path.endsWith('/resolve'))).toBe(true));
  const call = c.fetch.mock.calls.find(([path]) => path.endsWith('/resolve'))!;
  expect(JSON.parse(call[1].body as string)).toEqual({ acknowledge: true, acknowledge_object: change.object_name, expected_recovery_reason: change.recovery.reason, note: 'Handled independently; original write still unknown.' });
  expect(call[1].headers).toMatchObject({ 'If-Recovery-Revision': '7' });
  expect(c.fetch.mock.calls.filter(([path]) => path.endsWith('/confirm') || path.endsWith('/retry'))).toHaveLength(0);
});

it.each(['assess', 'confirm', 'retry', 'resolve'])('lost %s response never replays writes on refresh, shell reread or remount', async (kind) => {
  const change = held();
  if (kind === 'retry') change.recovery.authority = { confirmation_id: 'grant', required_method: 'typed', method: 'typed', policy: 'typed service policy', source: 'authenticated_request', satisfied_at: '' };
  const c = setup(change, async () => { throw new TypeError('response lost'); });
  const view = show(change); await ready();
  if (kind === 'assess') fireEvent.click(button('Assess current service state'));
  if (kind === 'confirm') { acknowledgeGrant(change); fireEvent.click(button('Grant one recovery retry')); }
  if (kind === 'retry') fireEvent.click(button('Retry original action once'));
  if (kind === 'resolve') {
    fireEvent.click(screen.getByRole('checkbox', { name: /release does not verify/ }));
    fireEvent.change(screen.getByLabelText(`Type ${change.object_name} to release hold`), { target: { value: change.object_name } });
    fireEvent.click(button('Release object hold manually'));
  }
  await screen.findByRole('alert');
  await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
  fireEvent.click(button('Refresh recorded status')); act(() => refreshAll());
  await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
  view.unmount(); show(change); await screen.findByRole('region', { name: 'Recovery console' });
  expect(c.fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  if (kind === 'confirm') expect(button('Grant one recovery retry')).toBeDisabled();
});

it('GET refresh preserves form inputs and focus, and displays backend stale-tab refusal without replay', async () => {
  const change = held(); const c = setup(change, async () => response({ error: { code: 'recovery_revision_stale', message: 'Refresh before acting', detail: {} } }, 409));
  show(change); await ready(); acknowledgeGrant(change);
  const input = screen.getByLabelText(`Type ${change.object_name} to authorize retry`); input.focus();
  act(() => refreshAll()); await waitFor(() => expect(button('Assess current service state')).toBeEnabled());
  expect(input).toHaveValue(change.object_name); expect(input).toHaveFocus();
  fireEvent.click(button('Grant one recovery retry'));
  expect(await screen.findByRole('alert')).toHaveTextContent('recovery_revision_stale');
  expect(c.fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
});

it.each(['revision', 'confirmation', 'future-method'])('fails closed on missing or unsupported %s metadata', async (kind) => {
  const change = held();
  // Deliberately incomplete wire fixtures, not an alternate production type contract.
  if (kind === 'revision') Reflect.deleteProperty(change.recovery, 'revision');
  if (kind === 'confirmation') Reflect.deleteProperty(change.recovery, 'confirmation_requirement');
  if (kind === 'future-method') change.recovery.confirmation_requirement.method = 'future';
  const c = setup(change); show(change); await screen.findByRole('region', { name: 'Recovery console' });
  expect(button('Grant one recovery retry')).toBeDisabled(); expect(button('Retry original action once')).toBeDisabled();
  expect(c.fetch.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
});

it('shows chronological evidence, dispatch, refusal, release note and original outcome independently', async () => {
  const change = held();
  change.recovery.attempts = [{ attempt_id: 'assessment', sequence: 1, kind: 'assess', started_at: '2026-11-08T10:00:00Z', finished_at: '2026-11-08T10:00:01Z', outcome: 'not_proven', refusal_code: 'executor_completion_unknown', releases_hold: false, management_path: 'unknown', protection_evidence_id: 'protection_a', evidence: { ...change.verification, reason: 'original_restart_generation_unproven', observation_id: 'observation_a' }, mutation: null, host_effect: 'none', verification: null, confirmation_id: null, operator_statement: null, note: null },
    { attempt_id: 'manual_release', sequence: 2, kind: 'resolve', started_at: '2026-11-08T10:01:00Z', finished_at: '2026-11-08T10:01:01Z', outcome: 'resolved', refusal_code: null, releases_hold: true, management_path: 'unknown', protection_evidence_id: null, evidence: change.verification, mutation: null, host_effect: 'none', verification: null, confirmation_id: null, operator_statement: change.object_name, note: 'Independent intervention; original remains uncertain.' }];
  change.recovery.object_write_locked = false; change.recovery.state = 'resolved';
  setup(change); show(change);
  const panel = await screen.findByRole('region', { name: 'Recovery console' });
  expect(within(panel).getByText('recovery_required')).toBeInTheDocument();
  expect(within(panel).getByText('Released / not held')).toBeInTheDocument();
  const attempts = within(panel).getAllByRole('listitem');
  const history = attempts.filter((item) => item.textContent?.includes('Host effect:'));
  expect(history).toHaveLength(2);
  expect(history[0]).toHaveTextContent('executor_completion_unknown');
  expect(history[0]).toHaveTextContent('original_restart_generation_unproven');
  expect(history[0]).toHaveTextContent('observation_a');
  expect(history[1]).toHaveTextContent('Independent intervention; original remains uncertain.');
});

it.each(['confirm', 'assess', 'retry', 'resolve'])('pre-handler auth refusal of recovery %s permits only a new deliberate action after same-ID reattachment', async (kind) => {
  const change = held(); let lost = false; let refused = false; let current = change;
  if (kind === 'retry') change.recovery.authority = { confirmation_id: 'grant_before_auth', required_method: 'typed', method: 'typed', policy: 'typed service policy', source: 'authenticated_request', satisfied_at: '' };
  const fetch = vi.fn(async (path: string, _options: RequestInit) => {
    if (path === '/api/v1/session') {
      if (lost) current = { ...change, recovery: { ...change.recovery, revision: 9, authority: change.recovery.authority ? { ...change.recovery.authority, confirmation_id: 'grant_after_auth' } : null } };
      lost = false; return response({ authenticated: true, expires_at: '2026-11-08T20:00:00Z' });
    }
    if (path.endsWith(`/recovery/${kind}`)) {
      if (!refused) { refused = true; lost = true; return response({ error: { code: 'session_invalid', message: 'Sign in again', detail: {} } }, 401); }
      return response(current);
    }
    if (lost) return response({ error: { code: 'authentication_required', message: 'Sign in again', detail: {} } }, 401);
    if (path.endsWith('/recovery-holds')) return response({ total_count: 3, changes: [] });
    if (path.includes('/runs/')) return response(runFixture);
    return response(current);
  });
  vi.stubGlobal('fetch', fetch);
  render(<MemoryRouter initialEntries={[`/operations/changes/${change.change_id}`]}><AuthenticationProvider><Routes><Route path="/operations/changes/:changeId" element={<ChangeDetail />} /></Routes></AuthenticationProvider></MemoryRouter>);
  const send = () => {
    if (kind === 'confirm') { acknowledgeGrant(change); fireEvent.click(button('Grant one recovery retry')); }
    if (kind === 'assess') fireEvent.click(button('Assess current service state'));
    if (kind === 'retry') fireEvent.click(button('Retry original action once'));
    if (kind === 'resolve') {
      fireEvent.click(screen.getByRole('checkbox', { name: /release does not verify/ }));
      fireEvent.change(screen.getByLabelText(`Type ${change.object_name} to release hold`), { target: { value: change.object_name } });
      fireEvent.click(button('Release object hold manually'));
    }
  };
  await ready(); send();
  await screen.findByRole('button', { name: 'Sign in' });
  fireEvent.change(screen.getByLabelText('Master credential'), { target: { value: 'fixture-only-credential' } });
  fireEvent.click(button('Sign in'));
  await screen.findByRole('region', { name: 'Recovery console' });
  await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([path, options]) => path !== '/api/v1/session' && options.method !== 'GET')).toHaveLength(1);
  expect(screen.getByRole('link', { name: /Original Run/ })).toHaveAttribute('href', `/operations/runs/${change.run_id}`);
  send();
  await waitFor(() => expect(fetch.mock.calls.filter(([path, options]) => path !== '/api/v1/session' && options.method !== 'GET')).toHaveLength(2));
  const post = fetch.mock.calls.filter(([path]) => path.endsWith(`/recovery/${kind}`)).at(-1)!;
  expect(post[1].headers).toMatchObject({ 'If-Recovery-Revision': '9', ...(kind === 'retry' ? { 'If-Recovery-Confirmation': 'grant_after_auth' } : {}) });
});

it('supports labeled native DOM controls and keyboard activation without auto-spending a grant', async () => {
  const change = held(); const c = setup(change); const user = userEvent.setup();
  show(change); await ready();
  await user.click(screen.getByRole('checkbox', { name: /I authorize/ }));
  await user.type(screen.getByLabelText(`Type ${change.object_name} to authorize retry`), change.object_name);
  const grantButton = button('Grant one recovery retry');
  expect(grantButton).toHaveAttribute('type', 'button');
  grantButton.focus(); await user.keyboard('{Enter}');
  await waitFor(() => expect(c.fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1));
  expect(c.fetch.mock.calls.some(([path]) => path.endsWith('/retry'))).toBe(false);
});

it.each(['success', 'lost', 'stale'])('consumed unproven retry permits a separately reviewed grant: %s response', async (outcome) => {
  const change = held();
  const authority = { confirmation_id: 'grant_first', required_method: 'typed', method: 'typed', policy: 'typed service policy', source: 'authenticated_request', satisfied_at: '2026-11-08T10:00:00Z' };
  const granted = { ...change, recovery: { ...change.recovery, revision: 8, authority } };
  const failed = { ...change, recovery: { ...change.recovery, revision: 10, authority: null, attempts: [consumedRetry(change)] } };
  let confirms = 0;
  const c = setup(change, async (kind) => {
    if (kind === 'retry') { c.setCurrent(failed); return response(failed); }
    confirms += 1;
    if (confirms === 1) { c.setCurrent(granted); return response(granted); }
    if (outcome === 'lost') throw new TypeError('second grant response lost');
    if (outcome === 'stale') return response({ error: { code: 'recovery_revision_stale', message: 'Another tab advanced the revision', detail: {} } }, 409);
    const next = { ...failed, recovery: { ...failed.recovery, revision: 12, authority: { ...authority, confirmation_id: 'grant_second' } } };
    c.setCurrent(next); return response(next);
  });
  show(change); await ready();
  // Manual-release input must survive the grant/consumption boundary untouched.
  fireEvent.change(screen.getByLabelText(/Recorded note/), { target: { value: 'Retain this unrelated release note' } });
  acknowledgeGrant(change); fireEvent.click(button('Grant one recovery retry'));
  await waitFor(() => expect(button('Retry original action once')).toBeEnabled());
  fireEvent.click(button('Retry original action once'));
  await screen.findByText(/Durable retry history records consumption/);
  await waitFor(() => expect(button('Assess current service state')).toBeEnabled());
  expect(confirms).toBe(1);
  expect(screen.getByRole('checkbox', { name: /I authorize/ })).not.toBeChecked();
  expect(screen.getByLabelText(`Type ${change.object_name} to authorize retry`)).toHaveValue('');
  expect(screen.getByLabelText(/Recorded note/)).toHaveValue('Retain this unrelated release note');
  expect(button('Grant one recovery retry')).toBeDisabled();
  acknowledgeGrant(change);
  // An assessment-only revision change preserves this review but is not the boundary.
  c.setCurrent({ ...failed, recovery: { ...failed.recovery, revision: 11 } });
  fireEvent.click(button('Refresh recorded status'));
  await waitFor(() => expect(button('Grant one recovery retry')).toBeEnabled());
  expect(confirms).toBe(1);
  fireEvent.click(button('Grant one recovery retry'));
  await waitFor(() => expect(confirms).toBe(2));
  const confirmCalls = c.fetch.mock.calls.filter(([path]) => path.endsWith('/confirm'));
  expect(confirmCalls[1]![1].headers).toMatchObject({ 'If-Recovery-Revision': '11' });
  expect(JSON.parse(confirmCalls[1]![1].body as string)).toMatchObject({ acknowledge: true, acknowledge_object: change.object_name });
  expect(c.fetch.mock.calls.filter(([path]) => path.endsWith('/retry'))).toHaveLength(1);
  if (outcome === 'success') await waitFor(() => expect(button('Retry original action once')).toBeEnabled());
  else {
    expect(await screen.findByRole('alert')).toHaveTextContent(outcome === 'lost' ? 'second grant response lost' : 'recovery_revision_stale');
    await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
    c.setCurrent({ ...failed, recovery: { ...failed.recovery, revision: 12 } });
    fireEvent.click(button('Refresh recorded status'));
    await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
    expect(button('Grant one recovery retry')).toBeDisabled();
    fireEvent.click(button('Grant one recovery retry'));
    expect(confirms).toBe(2);
  }
});

it.each(['assessment', 'missing-dispatch', 'in-flight'])('lost confirm cannot renew on revision or incomplete consumption proof: %s', async (kind) => {
  const change = held(); const c = setup(change, async () => { throw new TypeError('lost confirmation'); });
  show(change); await ready(); acknowledgeGrant(change); fireEvent.click(button('Grant one recovery retry'));
  await screen.findByRole('alert');
  const attempt = consumedRetry(change);
  if (kind === 'assessment') { attempt.kind = 'assess'; attempt.confirmation_id = null; attempt.mutation = null; }
  if (kind === 'missing-dispatch') attempt.mutation!.dispatch_began_at = null;
  if (kind === 'in-flight') { attempt.outcome = 'in_flight'; attempt.finished_at = null; }
  c.setCurrent({ ...change, recovery: { ...change.recovery, revision: 12, attempts: [attempt] } });
  await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
  fireEvent.click(button('Refresh recorded status'));
  await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
  expect(button('Grant one recovery retry')).toBeDisabled();
  expect(c.fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
});

it.each([
  { kind: 'confirm', code: 'recovery_revision_mismatch', rotate: false },
  { kind: 'assess', code: 'recovery_revision_mismatch', rotate: false },
  { kind: 'resolve', code: 'recovery_revision_mismatch', rotate: false },
  { kind: 'retry', code: 'recovery_revision_mismatch', rotate: false },
  { kind: 'retry', code: 'recovery_confirmation_mismatch', rotate: true },
] as const)('proven pre-dispatch $kind / $code refreshes stale-tab metadata without replay', async ({ kind, code, rotate }) => {
  const change = held();
  if (kind === 'retry') change.recovery.authority = { confirmation_id: 'reviewed_grant', required_method: 'typed', method: 'typed', policy: 'typed service policy', source: 'authenticated_request', satisfied_at: '' };
  const fresh = { ...change, recovery: { ...change.recovery, revision: 9, authority: change.recovery.authority && rotate ? { ...change.recovery.authority, confirmation_id: 'current_grant' } : change.recovery.authority } };
  let posts = 0;
  const c = setup(change, async () => {
    posts += 1;
    if (posts === 1) {
      c.setCurrent(fresh);
      return response({ error: { code, message: 'Refresh stale tab metadata', detail: {} } }, 409);
    }
    return response(fresh);
  });
  show(change); await ready();
  if (kind === 'confirm') acknowledgeGrant(change);
  if (kind === 'resolve') {
    fireEvent.click(screen.getByRole('checkbox', { name: /release does not verify/ }));
    fireEvent.change(screen.getByLabelText(`Type ${change.object_name} to release hold`), { target: { value: change.object_name } });
  }
  const label = { confirm: 'Grant one recovery retry', assess: 'Assess current service state', retry: 'Retry original action once', resolve: 'Release object hold manually' }[kind];
  fireEvent.click(button(label));
  await waitFor(() => expect(screen.getByText(/Recorded revision: 9/)).toBeInTheDocument());
  await waitFor(() => expect(button(label)).toBeEnabled());
  expect(posts).toBe(1);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.click(button(label));
  await waitFor(() => expect(posts).toBe(2));
  const calls = c.fetch.mock.calls.filter(([path]) => path.endsWith(`/recovery/${kind}`));
  expect(calls[0]![1].headers).toMatchObject({ 'If-Recovery-Revision': '7' });
  expect(calls[1]![1].headers).toMatchObject({ 'If-Recovery-Revision': '9', ...(kind === 'retry' ? { 'If-Recovery-Confirmation': rotate ? 'current_grant' : 'reviewed_grant' } : {}) });
  expect(c.fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(2);
});

it.each([
  { status: 409, code: 'generic_conflict', malformed: false },
  { status: 409, code: 'provider_write_unknown', malformed: false },
  { status: 500, code: 'recovery_revision_mismatch', malformed: false },
  { status: 409, code: 'recovery_revision_mismatch', malformed: true },
])('unproven $status / $code / malformed=$malformed keeps the grant intent after refresh', async ({ status, code, malformed }) => {
  const change = held();
  const c = setup(change, async () => malformed ? new Response('unreadable refusal', { status }) : response({ error: { code, message: 'Delivery not proven', detail: {} } }, status));
  show(change); await ready(); acknowledgeGrant(change); fireEvent.click(button('Grant one recovery retry'));
  await screen.findByRole('alert');
  c.setCurrent({ ...change, recovery: { ...change.recovery, revision: 9 } });
  await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
  fireEvent.click(button('Refresh recorded status'));
  await waitFor(() => expect(button('Refresh recorded status')).toBeEnabled());
  expect(button('Grant one recovery retry')).toBeDisabled();
  expect(c.fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
});
