import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import type { Run, SystemdUnit, SystemdUnitList } from '@/api/types';
import { GlobalSearch } from '@/components/layout/GlobalSearch';
import { refreshAll } from '@/hooks/useResource';
import { serviceIntentUrl } from '@/domain/serviceIntent';
import { BACKEND, urlOf } from '@/test/backend';
import fixture from '@/test/fixtures/systemd-run.json';
import { UnitDetail } from './UnitDetail';

const base = BACKEND['/api/v1/systemd/units/obj_unit'] as SystemdUnit;
const list = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
const target = 'unit:/雪 space?x=1&y+#%25';
const service: SystemdUnit = { ...base, object_id: target, canonical_id: 'duplicate.service' };
const other: SystemdUnit = { ...base, object_id: 'other', canonical_id: service.canonical_id };
const units: SystemdUnitList = { ...list, count: 4, units: [other, service,
  { ...base, object_id: 'socket', unit_type: 'socket' },
  { ...base, object_id: 'unloaded', load_state: 'not-found' },
] };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const detailPath = (id: string) => `/api/v1/systemd/units/${encodeURIComponent(id)}`;

function backend(detail: SystemdUnit = service) {
  const table: Record<string, unknown> = { ...BACKEND,
    '/api/v1/systemd/units': units, [detailPath(target)]: detail, [detailPath('other')]: other,
  };
  const mock = vi.fn<typeof fetch>(async (input, init) => {
    const path = urlOf(input);
    if (init?.method === 'POST') return response({ ...fixture, run_id: 'new-run', object_id: target } satisfies Partial<Run>, 201);
    return response(table[path] ?? {}, table[path] ? 200 : 404);
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function Navigation(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  return <>
    <button onClick={() => navigate(-1)}>Back</button>
    <Link to={serviceIntentUrl('other', 'systemd.service.stop')}>Other intent</Link>
    <Link to={serviceIntentUrl(target, 'systemd.service.start')}>Same target, new intent</Link>
    <output data-testid="location">{location.pathname}{location.search}</output>
  </>;
}

function mount(path = '/') {
  return render(<MemoryRouter initialEntries={[path]}>
    <GlobalSearch /><Navigation />
    <Routes><Route path="/system/:objectId" element={<UnitDetail />} /><Route path="*" element={null} /></Routes>
  </MemoryRouter>);
}

function writes(): Parameters<typeof fetch>[] {
  return vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method && init.method !== 'GET');
}

async function search(query: string) {
  await userEvent.click(screen.getByRole('button', { name: /Search objects/ }));
  fireEvent.change(screen.getByRole('combobox'), { target: { value: query } });
}

afterEach(() => vi.unstubAllGlobals());

describe('typed service entry', () => {
  it.each(['start', 'stop', 'restart'] as const)('binds %s to an exact special ID, not a duplicate name; only explicit Plan writes once', async (verb) => {
    backend();
    mount();
    await search(`> ${verb}`);
    const option = await screen.findByRole('option', { name: `Stage ${verb} duplicate.service, ${target}` });
    expect(screen.getAllByRole('option')).toHaveLength(2);
    await userEvent.click(option);
    const plan = await screen.findByRole('button', { name: `Plan ${verb}` });
    expect(screen.getByRole('tab', { name: 'Lifecycle' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('location')).toHaveTextContent(serviceIntentUrl(target, `systemd.service.${verb}`));
    expect(screen.getAllByRole('button', { name: /^Plan / })).toEqual([plan]);
    expect(writes()).toEqual([]);
    fireEvent.click(plan); fireEvent.click(plan);
    await screen.findByRole('link', { name: /Open Composer/ });
    expect(writes()).toHaveLength(1);
    const [url, init] = writes()[0]!;
    expect(url).toBe('/api/v1/runs');
    expect(JSON.parse(init!.body as string)).toEqual({ operation: { type: `systemd.service.${verb}`, object_id: target } });
  });

  it('supports action prefixes and exact ID filtering, excludes unsupported actions and keeps ordinary search', async () => {
    backend(); mount(); await search('> res other');
    expect(await screen.findByRole('option', { name: 'Stage restart duplicate.service, other' })).toBeInTheDocument();
    expect(screen.getAllByRole('option')).toHaveLength(1);
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '> reload' } });
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    expect(screen.getByText(/No loaded service actions match/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '> restart.service' } });
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'duplicate.service' } });
    expect(screen.getAllByRole('option')).toHaveLength(2);
    expect(writes()).toEqual([]);
  });

  it('uses action plus target for keyboard identity and ignores IME confirmation/dismissal', async () => {
    backend(); mount(); await search('>');
    await screen.findByRole('option', { name: 'Stage start duplicate.service, other' });
    const input = screen.getByRole('combobox');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(screen.getByRole('option', { selected: true })).toHaveAccessibleName('Stage stop duplicate.service, other');
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(input, { key: 'Escape', keyCode: 229 });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(writes()).toEqual([]);
    fireEvent.keyDown(input, { key: 'Enter' });
    await screen.findByRole('button', { name: 'Plan stop' });
    expect(screen.getByTestId('location')).toHaveTextContent(serviceIntentUrl('other', 'systemd.service.stop'));
    expect(writes()).toEqual([]);
  });

  it('does not auto-plan on direct links, remount/reload, Back, or tab navigation', async () => {
    backend();
    const path = serviceIntentUrl(target, 'systemd.service.restart');
    const view = mount(path);
    await screen.findByRole('button', { name: 'Plan restart' });
    await userEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    await userEvent.click(screen.getByRole('tab', { name: 'Lifecycle' }));
    await userEvent.click(screen.getByRole('link', { name: 'Other intent' }));
    await screen.findByRole('button', { name: 'Plan stop' });
    await userEvent.click(screen.getByRole('button', { name: 'Back' }));
    await screen.findByRole('button', { name: 'Plan restart' });
    view.unmount(); mount(path);
    await screen.findByRole('button', { name: 'Plan restart' });
    expect(writes()).toEqual([]);
  });

  it.each([
    'operation=systemd.service.reload&target=other',
    'operation=shell.session&target=other',
    'operation=systemd.service.restart',
    'target=other',
    'operation=&target=other',
    'operation=systemd.service.restart&target=wrong',
    'operation=systemd.service.restart&operation=systemd.service.stop&target=other',
    'operation=systemd.service.restart&target=other&target=other',
    'operation=%E0%A4%A&target=other',
  ])('rejects malformed or unsupported intent: %s', async (query) => {
    backend(); mount(`/system/other?tab=lifecycle&${query}`);
    expect(await screen.findByRole('alert')).toHaveTextContent('service action link is invalid');
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
    expect(writes()).toEqual([]);
  });

  it.each([
    { unit_type: 'socket' }, { load_state: 'not-found' }, { object_id: 'wrong' },
    { observation: { ...base.observation!, freshness: 'stale' } },
    { observation: { ...base.observation!, freshness: 'never_observed' } },
    { observation: null }, { observed_in_latest_sweep: false }, { observed_in_latest_sweep: null },
  ] satisfies Partial<SystemdUnit>[])('revalidates fetched detail instead of trusting a search result: %j', async (override) => {
    backend({ ...service, ...override }); mount(); await search('> restart');
    await userEvent.click(await screen.findByRole('option', { name: `Stage restart duplicate.service, ${target}` }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
    expect(writes()).toEqual([]);
  });

  it('withholds a staged plan while detail refreshes and after refreshed evidence becomes stale', async () => {
    const mock = backend(); mount(serviceIntentUrl(target, 'systemd.service.restart'));
    await screen.findByRole('button', { name: 'Plan restart' });
    const original = mock.getMockImplementation()!;
    let finish!: (value: Response) => void;
    mock.mockImplementation((input, init) => urlOf(input) === detailPath(target)
      ? new Promise<Response>((resolve) => { finish = resolve; }) : original(input, init));
    act(() => refreshAll());
    await screen.findByText(/Current service evidence is unavailable/);
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
    await act(async () => finish(response({ ...service, observation: { ...base.observation!, freshness: 'stale' } })));
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
    expect(writes()).toEqual([]);
  });

  it('ignores an aborted old detail response after navigation to a different intent', async () => {
    const mock = backend(); const original = mock.getMockImplementation()!;
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    mock.mockImplementation((input, init) => {
      if (urlOf(input) !== detailPath(target)) return original(input, init);
      signal = init?.signal;
      return new Promise<Response>((resolve) => { finish = resolve; });
    });
    mount(serviceIntentUrl(target, 'systemd.service.restart'));
    expect(screen.getByText('Reading unit…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Other intent' }));
    await screen.findByRole('button', { name: 'Plan stop' });
    expect(signal?.aborted).toBe(true);
    await act(async () => finish(response(service)));
    expect(screen.queryByRole('button', { name: 'Plan restart' })).not.toBeInTheDocument();
    expect(writes()).toEqual([]);
  });

  it('shows a failed detail read, not a plan; retry only rereads', async () => {
    const mock = backend(); const original = mock.getMockImplementation()!;
    mock.mockImplementation((input, init) => urlOf(input) === detailPath(target)
      ? Promise.resolve(response({}, 503)) : original(input, init));
    mount(serviceIntentUrl(target, 'systemd.service.restart'));
    await userEvent.click(await screen.findByRole('button', { name: /try again/i }));
    await screen.findByRole('button', { name: /try again/i });
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
    expect(writes()).toEqual([]);
  });

  it('rereads and rejects stale evidence for a new intent on the same target', async () => {
    const mock = backend(); mount(serviceIntentUrl(target, 'systemd.service.restart'));
    await screen.findByRole('button', { name: 'Plan restart' });
    const original = mock.getMockImplementation()!;
    mock.mockImplementation((input, init) => urlOf(input) === detailPath(target)
      ? Promise.resolve(response({ ...service, load_state: 'not-found' })) : original(input, init));
    await userEvent.click(screen.getByRole('link', { name: 'Same target, new intent' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('no longer targets a loaded service');
    expect(mock.mock.calls.filter(([input]) => urlOf(input) === detailPath(target))).toHaveLength(2);
    expect(screen.queryByRole('button', { name: /^Plan / })).not.toBeInTheDocument();
    expect(writes()).toEqual([]);
  });

  it('retains uncertain planning response guidance and never repeats the request', async () => {
    const mock = backend(); const original = mock.getMockImplementation()!;
    let fail!: (cause: Error) => void;
    mock.mockImplementation((input, init) => init?.method === 'POST'
      ? new Promise<Response>((_resolve, reject) => { fail = reject; }) : original(input, init));
    mount(serviceIntentUrl(target, 'systemd.service.restart'));
    const plan = await screen.findByRole('button', { name: 'Plan restart' });
    fireEvent.click(plan); fireEvent.click(plan);
    expect(plan).toBeDisabled();
    await act(async () => fail(new TypeError('response lost')));
    expect(await screen.findByRole('alert')).toHaveTextContent('a Run may already have been created');
    fireEvent.click(plan);
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(plan).toBeDisabled();
  });
});
