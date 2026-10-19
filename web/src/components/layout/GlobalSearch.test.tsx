import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router-dom';
import { GlobalSearch } from './GlobalSearch';
import { BACKEND } from '@/test/backend';
import type { NetworkInterfaceList, SystemdUnitList, DockerContainerList, RunList, ChangeList, FindingList } from '@/api/types';

const runs: RunList = {
  host_id: 'host_1', state: 'all', count: 1, runs: [{
    run_id: 'record/id?#', host_id: 'host_1', object_id: 'obj_run', object_name: 'worker.service',
    operation: 'systemd.service.restart', state: 'preview', created_at: '2026-08-27T21:50:00Z',
    cancelled_at: null, finished_at: null, host_effect: 'none', change_created: false, change_id: null,
    preview: {
      preview_id: 'preview_1', preview_digest: 'digest', published_at: '2026-08-27T21:50:00Z',
      kind: 'action', action: 'restart', observed_state: 'active', expected_state: 'active',
      risk_tier: 'low', confirmation_required: true, execution_availability: 'implemented',
      execution_eligibility: 'eligible', blockers: [], validity: { state: 'current', reasons: [], as_of: '2026-08-27T21:50:00Z' },
    },
  }],
};
const changes: ChangeList = {
  host_id: 'host_1', count: 1, changes: [{
    change_id: 'record/id?#', run_id: 'run_1', object_id: 'obj_change', object_name: 'uplink',
    operation: 'network.interface.reconcile_mtu', change_kind: 'field', field: 'mtu',
    before_value: 1500, desired_value: 1400, action: null, expected_state: null,
    created_at: '2026-08-27T21:50:00Z', finished_at: '2026-08-27T21:50:30Z',
    host_effect: 'write_unknown', mutation_outcome: 'write_unknown',
    verification_outcome: 'observation_unavailable', rollback_outcome: null,
    result: 'recovery_required', recovery_required: true,
    recovery_reason: 'write_unknown_unverified', recovery_state: 'resolved',
  }],
};
const findings: FindingList = {
  host_id: 'host_1', status: 'all', count: 1, findings: [{
    finding_id: 'record/id?#', finding_key: 'drift:obj_finding:mtu', host_id: 'host_1',
    object_id: 'obj_finding', object_name: 'lan', finding_type: 'drift', subject: 'mtu',
    status: 'resolved', summary: 'MTU differs from intent',
    evidence: {
      intent_id: 'evidence-only-marker', field: 'mtu', intended: { value_type: 'integer', value: 1400 },
      observed: { value_type: 'integer', value: 1500 }, comparison: 'differs',
      reason: 'observed_differs_from_intended', observation: 'obs_1', sweep: 'sweep_1',
    },
    first_seen_at: '2026-08-20T10:00:00Z', last_seen_at: '2026-08-28T08:00:00Z',
    updated_at: '2026-08-28T09:00:00Z', resolved_at: '2026-08-28T09:00:00Z',
    resolution: 'observed_matches_intent', resolved_by_observation_id: 'obs_2',
  }],
};

const networkTemplate = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
const unitTemplate = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
const emptyContainers: DockerContainerList = {
  ...(BACKEND['/api/v1/docker/containers'] as DockerContainerList), count: 0, containers: [],
};

function networkList(count = 1): NetworkInterfaceList {
  return {
    ...networkTemplate, count,
    interfaces: Array.from({ length: count }, (_, index) => ({
      ...networkTemplate.interfaces[0]!, object_id: index === 0 ? 'shared/id?#' : `network/${index}`,
      name: `network-${index}`,
    })),
  };
}

function systemList(): SystemdUnitList {
  return {
    ...unitTemplate, count: 2,
    units: ['system/a', 'shared/id?#'].map((id, index) => ({
      ...unitTemplate.units[0]!, object_id: id, canonical_id: `system-${index}.service`,
    })),
  };
}

function expectSelection(name: string): HTMLElement {
  const option = screen.getByRole('option', { name });
  expect(screen.getAllByRole('option', { selected: true })).toEqual([option]);
  expect(screen.getByRole('combobox')).toHaveAttribute('aria-activedescendant', option.id);
  expect(document.getElementById(option.id)).toBe(option);
  return option;
}

const systemB = 'Open System systemd unit system-1.service, shared/id?#';

const mocks = vi.hoisted(() => ({
  interfaces: vi.fn(),
  systemdUnits: vi.fn(),
  containers: vi.fn(),
  runs: vi.fn(),
  changes: vi.fn(),
  findings: vi.fn(),
}));

vi.mock('@/api/endpoints', () => ({ endpoints: mocks }));

function LocationProbe(): JSX.Element {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

function RecordRoute(): JSX.Element {
  const params = useParams();
  return <span data-testid="record-route">{Object.values(params).join('')}</span>;
}

function renderSearch(): void {
  render(
    <MemoryRouter>
      <GlobalSearch />
      <LocationProbe />
      <Routes>
        <Route path="/operations/runs/:runId" element={<RecordRoute />} />
        <Route path="/operations/changes/:changeId" element={<RecordRoute />} />
        <Route path="/operations/findings/:findingId" element={<RecordRoute />} />
        <Route path="*" element={null} />
      </Routes>
    </MemoryRouter>,
  );
}

function successfulLists(): void {
  mocks.runs.mockResolvedValue({ ...runs, count: 0, runs: [] } satisfies RunList);
  mocks.changes.mockResolvedValue({ ...changes, count: 0, changes: [] } satisfies ChangeList);
  mocks.findings.mockResolvedValue({ ...findings, count: 0, findings: [] } satisfies FindingList);
  mocks.interfaces.mockResolvedValue({
    interfaces: [{ object_id: 'obj/if', name: 'eth0' }],
  });
  mocks.systemdUnits.mockResolvedValue({
    units: [{ object_id: 'obj_unit', canonical_id: 'sshd.service' }],
  });
  mocks.containers.mockResolvedValue({
    containers: [{ object_id: 'obj_ct', name: 'grafana' }],
  });
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  successfulLists();
});

afterEach(() => vi.restoreAllMocks());

describe('service action search reads', () => {
  async function openActions(query = '>') {
    renderSearch();
    await userEvent.click(screen.getByRole('button', { name: /Search objects/ }));
    fireEvent.change(screen.getByRole('combobox'), { target: { value: query } });
  }

  it('keeps action identity when unrelated sources arrive or fail', async () => {
    const network = deferred<NetworkInterfaceList>();
    const records = deferred<RunList>();
    mocks.interfaces.mockReturnValue(network.promise);
    mocks.runs.mockReturnValue(records.promise);
    mocks.systemdUnits.mockResolvedValue(systemList());
    await openActions();
    await screen.findByRole('option', { name: 'Stage start system-0.service, system/a' });
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' });
    expectSelection('Stage stop system-0.service, system/a');
    network.resolve(networkList(45)); records.reject(new Error('unavailable'));
    await waitFor(() => expectSelection('Stage stop system-0.service, system/a'));
    expect(screen.queryByText(/Partial coverage/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Loading service/)).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' });
    expect(screen.getByTestId('location')).toHaveTextContent('/system/system%2Fa');
  });

  it('reports the service source failure even when object/record sources succeeded', async () => {
    const pending = deferred<SystemdUnitList>();
    mocks.systemdUnits.mockReturnValue(pending.promise);
    await openActions('> restart');
    expect(screen.getByText('Loading service targets…')).toBeInTheDocument();
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    pending.reject(new Error('unavailable'));
    expect(await screen.findByRole('alert')).toHaveTextContent('System units could not be read');
    expect(screen.queryByText(/No loaded service actions match/)).not.toBeInTheDocument();
  });

  it('distinguishes no loaded services from query misses', async () => {
    mocks.systemdUnits.mockResolvedValue({ ...unitTemplate, count: 0, units: [] } satisfies SystemdUnitList);
    await openActions();
    expect(await screen.findByText(/No loaded services were returned/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('ignores an aborted old inventory after reopen, bounds action results and refines by ID', async () => {
    const old = deferred<SystemdUnitList>();
    const many: SystemdUnitList = { ...unitTemplate, count: 15, units: Array.from({ length: 15 }, (_, index) => ({
      ...unitTemplate.units[0]!, object_id: `target-${index}`, canonical_id: 'duplicate.service',
    })) };
    mocks.systemdUnits.mockReturnValueOnce(old.promise).mockResolvedValue(many);
    await openActions();
    const signal = mocks.systemdUnits.mock.calls[0]?.[0].signal as AbortSignal;
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' });
    expect(signal.aborted).toBe(true);
    expect(screen.getByRole('button', { name: /Search objects/ })).toHaveFocus();
    await userEvent.click(screen.getByRole('button', { name: /Search objects/ }));
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '>' } });
    await screen.findByText(/Showing the first 40 loaded matches/);
    expect(screen.getAllByRole('option')).toHaveLength(40);
    old.resolve(systemList());
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '> restart target-14' } });
    await screen.findByRole('option', { name: 'Stage restart duplicate.service, target-14' });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(mocks.systemdUnits).toHaveBeenCalledTimes(2);
  });
});

describe('global object search', () => {
  it.each([
    ['runs', runs, ['record/id?#', 'obj_run', 'worker.service', 'systemd.service.restart', 'preview']],
    ['changes', changes, ['record/id?#', 'obj_change', 'uplink', 'network.interface.reconcile_mtu', 'recovery_required']],
    ['findings', findings, ['record/id?#', 'obj_finding', 'lan', 'differs from intent', 'drift', 'mtu', 'resolved']],
  ] as const)('matches public %s fields and activates its exact encoded detail route', async (source, list, fields) => {
    mocks[source].mockResolvedValue(list);
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(4));
    const input = screen.getByRole('combobox');
    for (const field of fields) {
      fireEvent.change(input, { target: { value: `  ${field.toUpperCase()}  ` } });
      expect(screen.getAllByRole('option')).toHaveLength(1);
      expect(screen.getByRole('option')).toHaveTextContent('record/id?#');
    }
    fireEvent.change(input, { target: { value: 'evidence-only-marker' } });
    expect(screen.queryAllByRole('option')).toHaveLength(0);
    fireEvent.change(input, { target: { value: 'record/id?#' } });
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location').textContent).toBe(`/operations/${source}/record%2Fid%3F%23`);
    expect(screen.getByTestId('record-route')).toHaveTextContent('record/id?#');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('requests bounded all-state history and keeps recurring Finding episodes distinct', async () => {
    mocks.runs.mockResolvedValue(runs);
    mocks.changes.mockResolvedValue(changes);
    mocks.findings.mockResolvedValue({ ...findings, count: 2, findings: [findings.findings[0]!, {
      ...findings.findings[0]!, finding_id: 'recurrence', status: 'open', resolved_at: null,
      resolution: null, resolved_by_observation_id: null,
    }] } satisfies FindingList);
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(7));
    const options = { signal: expect.any(AbortSignal) };
    expect(mocks.runs).toHaveBeenCalledWith({ state: 'all', limit: 50 }, options);
    expect(mocks.changes).toHaveBeenCalledWith({ limit: 100 }, options);
    expect(mocks.findings).toHaveBeenCalledWith({ status: 'all', limit: 100 }, options);
    expect(screen.getByText(/Recent records only: up to 50 Runs, 100 Changes and 100 Findings/)).toHaveTextContent('returned counts are not history totals');
    await user.type(screen.getByRole('combobox'), 'obj_finding');
    expect(screen.getAllByRole('option')).toHaveLength(2);
    expect(screen.getByRole('option', { name: /, resolved$/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /, open$/ })).toBeInTheDocument();
    await user.keyboard('{ArrowDown}{Enter}');
    expect(screen.getByTestId('record-route')).toHaveTextContent('recurrence');
    for (const source of [mocks.runs, mocks.changes, mocks.findings]) expect(source).toHaveBeenCalledTimes(1);
  });

  it('keeps a chosen record through mixed pending and failed sources, without substituting the same ID', async () => {
    const pendingNetwork = deferred<NetworkInterfaceList>();
    const pendingRuns = deferred<RunList>();
    mocks.interfaces.mockReturnValue(pendingNetwork.promise);
    mocks.systemdUnits.mockRejectedValue(new Error('offline'));
    mocks.runs.mockReturnValue(pendingRuns.promise);
    mocks.changes.mockResolvedValue(changes);
    mocks.findings.mockRejectedValue(new Error('offline'));
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByRole('option', { name: /Open Changes/ });
    await user.keyboard('{ArrowUp}');
    const chosen = screen.getByRole('option', { selected: true });
    expect(chosen).toHaveAccessibleName(/Open Changes/);
    expect(screen.getByRole('status')).toHaveTextContent('System, Findings');
    expect(screen.getByText('Reading objects and recent records…')).toBeInTheDocument();
    pendingRuns.resolve(runs);
    pendingNetwork.resolve(networkList());
    await screen.findByRole('option', { name: /Open Runs/ });
    await waitFor(() => expect(screen.queryByText('Reading objects and recent records…')).not.toBeInTheDocument());
    expect(screen.getByRole('option', { selected: true })).toBe(chosen);
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location').textContent).toBe('/operations/changes/record%2Fid%3F%23');
  });

  it('keeps record coverage distinct from the display cap and refines a cap-displaced record', async () => {
    const pending = deferred<NetworkInterfaceList>();
    mocks.interfaces.mockReturnValue(pending.promise);
    mocks.runs.mockResolvedValue({ ...runs, count: 50, runs: Array.from({ length: 50 }, (_, i) => ({
      ...runs.runs[0]!, run_id: `run_${i}`,
    })) } satisfies RunList);
    mocks.changes.mockResolvedValue(changes);
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByRole('option', { name: /run_0,/ });
    await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}');
    expect(screen.getByRole('option', { selected: true })).toHaveAccessibleName(/run_1,/);
    pending.resolve(networkList(40));
    await screen.findByText('network-39');
    expect(screen.queryAllByRole('option', { selected: true })).toHaveLength(0);
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location').textContent).toBe('/');
    expect(screen.getByText(/Showing the first 40 loaded matches/)).toBeInTheDocument();
    expect(screen.getByText(/Recent records only/)).toBeInTheDocument();
    await user.type(screen.getByRole('combobox'), 'record/id?#');
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.queryByText(/Showing the first 40 loaded matches/)).not.toBeInTheDocument();
    expect(screen.getByText(/Older records may be excluded/)).toBeInTheDocument();
    expect(mocks.runs).toHaveBeenCalledTimes(1);
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location').textContent).toBe('/operations/changes/record%2Fid%3F%23');
  });

  it.each([false, true])('retains explicit System identity after Network prepends (continued arrows: %s)', async (continuedArrows) => {
    const pending = deferred<NetworkInterfaceList>();
    mocks.interfaces.mockReturnValue(pending.promise);
    mocks.systemdUnits.mockResolvedValue(systemList());
    mocks.containers.mockResolvedValue(emptyContainers);
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByText('system-1.service');
    await user.keyboard('{ArrowDown}');
    const chosen = expectSelection(systemB);
    const scroll = vi.fn();
    chosen.scrollIntoView = scroll;

    pending.resolve(networkList());
    await screen.findByText('network-0');
    expect.soft(screen.getByRole('option', { selected: true })).toBe(chosen);
    expect.soft(document.getElementById(screen.getByRole('combobox').getAttribute('aria-activedescendant')!)).toBe(chosen);
    expect(chosen.className).toMatch(/active/);
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });
    if (continuedArrows) {
      await user.keyboard('{ArrowUp}');
      expectSelection('Open System systemd unit system-0.service, system/a');
      await user.keyboard('{ArrowDown}');
      expectSelection(systemB);
    }
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/system\/shared%2Fid%3F%23$/);
  });

  it.each(['ArrowDown', 'ArrowUp'])('clears a cap-displaced choice and reselects with %s', async (arrow) => {
    const pending = deferred<NetworkInterfaceList>();
    mocks.interfaces.mockReturnValue(pending.promise);
    mocks.systemdUnits.mockResolvedValue(systemList());
    mocks.containers.mockResolvedValue(emptyContainers);
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByText('system-1.service');
    await user.keyboard('{ArrowDown}');
    expectSelection(systemB);
    pending.resolve(networkList(40));
    await screen.findByText('network-39');
    expect(screen.getAllByRole('option')).toHaveLength(40);
    expect(screen.queryByText('system-1.service')).not.toBeInTheDocument();
    expect(screen.queryAllByRole('option', { selected: true })).toHaveLength(0);
    expect(screen.getByRole('combobox')).not.toHaveAttribute('aria-activedescendant');
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.keyboard(`{${arrow}}`);
    const index = arrow === 'ArrowDown' ? 0 : 39;
    const id = index === 0 ? 'shared/id?#' : 'network/39';
    expectSelection(`Open Network interface network-${index}, ${id}`);
    // Ordinary wrapping continues from the newly chosen visible identity.
    await user.keyboard(arrow === 'ArrowDown' ? '{ArrowUp}{ArrowDown}' : '{ArrowDown}{ArrowUp}');
    expectSelection(`Open Network interface network-${index}, ${id}`);
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location').textContent).toBe(`/network/${encodeURIComponent(id)}`);
  });

  it('follows default-first arrivals and resets explicit selection on query changes and reopen', async () => {
    const pending = deferred<NetworkInterfaceList>();
    mocks.interfaces.mockReturnValueOnce(pending.promise).mockResolvedValue(networkList());
    mocks.systemdUnits.mockResolvedValue(systemList());
    mocks.containers.mockResolvedValue(emptyContainers);
    const user = userEvent.setup();
    renderSearch();
    const trigger = screen.getByRole('button', { name: /search objects/i });
    await user.click(trigger);
    await screen.findByText('system-1.service');
    expectSelection('Open System systemd unit system-0.service, system/a');
    pending.resolve(networkList());
    await screen.findByText('network-0');
    const first = 'Open Network interface network-0, shared/id?#';
    expectSelection(first);
    await user.keyboard('{ArrowUp}');
    expectSelection(systemB);
    const input = screen.getByRole('combobox');
    await user.type(input, 'system');
    expectSelection('Open System systemd unit system-0.service, system/a');
    await user.type(input, '-missing');
    expect(screen.queryAllByRole('option')).toHaveLength(0);
    expect(input).not.toHaveAttribute('aria-activedescendant');
    await user.keyboard('{ArrowDown}{ArrowUp}{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/);
    await user.clear(input);
    expectSelection(first);
    expect(mocks.interfaces).toHaveBeenCalledTimes(1);
    await user.keyboard('{ArrowUp}{Escape}');
    await user.click(trigger);
    await screen.findByText('network-0');
    expectSelection(first);
    expect(screen.getByRole('combobox')).toHaveValue('');
    expect(mocks.interfaces).toHaveBeenCalledTimes(2);
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/network\/shared%2Fid%3F%23$/);
  });

  it('retains an explicit choice when a pending earlier domain fails', async () => {
    const pending = deferred<NetworkInterfaceList>();
    mocks.interfaces.mockReturnValue(pending.promise);
    mocks.systemdUnits.mockResolvedValue(systemList());
    mocks.containers.mockResolvedValue(emptyContainers);
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByText('system-1.service');
    await user.keyboard('{ArrowDown}');
    expectSelection(systemB);
    pending.reject(new Error('unavailable'));
    expect(await screen.findByRole('status')).toHaveTextContent('Network');
    expectSelection(systemB);
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/system\/shared%2Fid%3F%23$/);
  });

  it('fetches once on open, searches across domains, and navigates with an encoded ID', async () => {
    const user = userEvent.setup();
    renderSearch();

    expect(mocks.interfaces).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    expect(await screen.findByText('eth0')).toBeInTheDocument();
    expect(screen.getByText('sshd.service')).toBeInTheDocument();
    expect(screen.getByText('grafana')).toBeInTheDocument();
    expect(mocks.interfaces).toHaveBeenCalledTimes(1);
    expect(mocks.systemdUnits).toHaveBeenCalledTimes(1);
    expect(mocks.containers).toHaveBeenCalledTimes(1);

    const input = screen.getByRole('combobox');
    await user.type(input, 'obj/if');
    expect(screen.getByRole('option')).toHaveTextContent('eth0');
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent('/network/obj%2Fif');
  });

  it('supports the shortcut, arrow selection, and focus restoration on Escape', async () => {
    const user = userEvent.setup();
    renderSearch();
    const trigger = screen.getByRole('button', { name: /search objects/i });

    fireEvent.keyDown(document, { key: 'k', ctrlKey: true });
    const input = await screen.findByRole('combobox');
    expect(input).toHaveFocus();
    await user.type(input, 'e');
    await user.keyboard('{ArrowDown}{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent('/system/obj_unit');

    await user.click(trigger);
    const reopened = await screen.findByRole('combobox');
    fireEvent.keyDown(reopened, { key: 'Escape' });
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('keeps successful domains visible when one list fails and never issues writes', async () => {
    mocks.systemdUnits.mockRejectedValue(new Error('unavailable'));
    renderSearch();
    await userEvent.click(screen.getByRole('button', { name: /search objects/i }));

    expect(await screen.findByRole('status')).toHaveTextContent('System');
    expect(screen.getByText('eth0')).toBeInTheDocument();
    expect(screen.getByText('grafana')).toBeInTheDocument();
    expect(screen.queryByText('sshd.service')).not.toBeInTheDocument();
    expect(mocks.interfaces.mock.calls[0]?.[0]).toEqual(expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('reports a full read failure distinctly', async () => {
    mocks.interfaces.mockRejectedValue(new Error('offline'));
    mocks.systemdUnits.mockRejectedValue(new Error('offline'));
    mocks.containers.mockRejectedValue(new Error('offline'));
    mocks.runs.mockRejectedValue(new Error('offline'));
    mocks.changes.mockRejectedValue(new Error('offline'));
    mocks.findings.mockRejectedValue(new Error('offline'));
    renderSearch();
    await userEvent.click(screen.getByRole('button', { name: /search objects/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('None of the sources could be read');
  });

  it('does not navigate when Enter confirms an IME composition', async () => {
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByText('eth0');
    const input = screen.getByRole('combobox');
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/);
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent('/network/obj%2Fif');
  });

  it('does not let incidental pointer hover retarget keyboard selection', async () => {
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByText('sshd.service');
    const options = screen.getAllByRole('option');
    fireEvent.mouseEnter(options[1]!);
    expect(options[0]).toHaveAttribute('aria-selected', 'true');
    expect(options[1]).toHaveAttribute('aria-selected', 'false');
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('location')).toHaveTextContent('/network/obj%2Fif');
  });

  it.each(['eth0', 'preview', 'recovery_required', 'resolved'])('uses only six bounded GET list requests through search and navigation for %s', async (query) => {
    const { endpoints: actual } = await vi.importActual<typeof import('@/api/endpoints')>('@/api/endpoints');
    mocks.interfaces.mockImplementation(actual.interfaces);
    mocks.systemdUnits.mockImplementation(actual.systemdUnits);
    mocks.containers.mockImplementation(actual.containers);
    mocks.runs.mockImplementation(actual.runs);
    mocks.changes.mockImplementation(actual.changes);
    mocks.findings.mockImplementation(actual.findings);
    const responses: Record<string, unknown> = {
      '/api/v1/network/interfaces': { interfaces: [{ object_id: 'obj/if', name: 'eth0' }] },
      '/api/v1/systemd/units': { units: [] }, '/api/v1/docker/containers': { containers: [] },
      '/api/v1/runs': runs, '/api/v1/changes': changes, '/api/v1/findings': findings,
    };
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => new Response(JSON.stringify(
      responses[new URL(url instanceof Request ? url.url : url, 'http://test.invalid').pathname],
    ), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByText('eth0');
    await user.type(screen.getByRole('combobox'), query);
    await user.keyboard('{Enter}');
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(fetch.mock.calls.map(([url]) => new URL(url instanceof Request ? url.url : url, 'http://test.invalid').pathname).sort()).toEqual([
      '/api/v1/changes', '/api/v1/docker/containers', '/api/v1/findings', '/api/v1/network/interfaces', '/api/v1/runs', '/api/v1/systemd/units',
    ]);
    for (const [, options] of fetch.mock.calls) expect(options?.method).toBe('GET');
    expect(fetch.mock.calls.map(([url]) => url instanceof Request ? url.url : url.toString())).toEqual(expect.arrayContaining([
      '/api/v1/runs?state=all&limit=50', '/api/v1/changes?limit=100', '/api/v1/findings?status=all&limit=100',
    ]));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('distinguishes an empty estate from a query with no match', async () => {
    mocks.interfaces.mockResolvedValue({ interfaces: [] });
    mocks.systemdUnits.mockResolvedValue({ units: [] });
    mocks.containers.mockResolvedValue({ containers: [] });
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));

    expect(await screen.findByText('No objects or recent records were returned by the available sources.')).toBeInTheDocument();
    await user.type(screen.getByRole('combobox'), 'missing');
    expect(screen.getByText('No loaded objects or records match “missing”.')).toBeInTheDocument();
  });

  it('bounds displayed results and reports truncation without refetching while typing', async () => {
    mocks.interfaces.mockResolvedValue({
      interfaces: Array.from({ length: 45 }, (_, index) => ({
        object_id: `obj_if_${index}`,
        name: `eth${index}`,
      })),
    });
    mocks.systemdUnits.mockResolvedValue({ units: [] });
    mocks.containers.mockResolvedValue({ containers: [] });
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(40));

    expect(screen.getByText(/Showing the first 40 loaded matches/)).toBeInTheDocument();
    await user.type(screen.getByRole('combobox'), 'eth4');
    expect(mocks.interfaces).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole('option')).toHaveLength(6);
  });

  it('traps focus inside the compact palette and restores the trigger after dismissal', async () => {
    const user = userEvent.setup();
    renderSearch();
    const trigger = screen.getByRole('button', { name: /search objects/i });
    await user.click(trigger);
    const input = await screen.findByRole('combobox');
    const close = screen.getAllByRole('button', { name: /close object and record search/i })[1]!;

    expect(input).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab();
    expect(input).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('keeps resolved domains visible while another source is pending', async () => {
    const pending = deferred<{ units: never[] }>();
    mocks.interfaces.mockResolvedValue({ interfaces: [{ object_id: 'obj/if', name: 'eth0' }] });
    mocks.systemdUnits.mockReturnValue(pending.promise);
    mocks.containers.mockResolvedValue({ containers: [] });
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));

    expect(await screen.findByText('eth0')).toBeInTheDocument();
    expect(screen.getByText('Reading objects and recent records…')).toBeInTheDocument();
    pending.resolve({ units: [] });
    await waitFor(() => expect(screen.queryByText('Reading objects and recent records…')).not.toBeInTheDocument());
  });

  it('ignores a cancelled response after close and reopen', async () => {
    const stale = deferred<{ interfaces: Array<{ object_id: string; name: string }> }>();
    const current = deferred<{ interfaces: Array<{ object_id: string; name: string }> }>();
    mocks.interfaces.mockReturnValueOnce(stale.promise).mockReturnValueOnce(current.promise);
    mocks.systemdUnits.mockResolvedValue({ units: [] });
    mocks.containers.mockResolvedValue({ containers: [] });
    const staleFindings = deferred<FindingList>();
    mocks.findings.mockReturnValueOnce(staleFindings.promise).mockResolvedValue({ ...findings, findings: [], count: 0 });
    const user = userEvent.setup();
    renderSearch();
    const trigger = screen.getByRole('button', { name: /search objects/i });
    await user.click(trigger);
    await user.keyboard('{Escape}');
    expect(mocks.findings.mock.calls[0]?.[1].signal.aborted).toBe(true);
    await user.click(trigger);

    stale.resolve({ interfaces: [{ object_id: 'stale', name: 'stale-interface' }] });
    expect(screen.queryByText('stale-interface')).not.toBeInTheDocument();
    current.resolve({ interfaces: [{ object_id: 'current', name: 'current-interface' }] });
    expect(await screen.findByText('current-interface')).toBeInTheDocument();
    staleFindings.resolve(findings);
    await waitFor(() => expect(screen.queryByText('Reading objects and recent records…')).not.toBeInTheDocument());
    expect(screen.queryByRole('option', { name: /Open Findings/ })).not.toBeInTheDocument();
  });
});
