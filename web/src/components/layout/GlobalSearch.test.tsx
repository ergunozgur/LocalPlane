import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { GlobalSearch } from './GlobalSearch';
import { BACKEND } from '@/test/backend';
import type { NetworkInterfaceList, SystemdUnitList, DockerContainerList } from '@/api/types';

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
}));

vi.mock('@/api/endpoints', () => ({ endpoints: mocks }));

function LocationProbe(): JSX.Element {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

function renderSearch(): void {
  render(
    <MemoryRouter>
      <GlobalSearch />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function successfulLists(): void {
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

describe('global object search', () => {
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
    renderSearch();
    await userEvent.click(screen.getByRole('button', { name: /search objects/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('None of the observed domains could be read');
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

  it('uses only GET list requests at the real API boundary through search and navigation', async () => {
    const { endpoints: actual } = await vi.importActual<typeof import('@/api/endpoints')>('@/api/endpoints');
    mocks.interfaces.mockImplementation(actual.interfaces);
    mocks.systemdUnits.mockImplementation(actual.systemdUnits);
    mocks.containers.mockImplementation(actual.containers);
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({
      interfaces: [{ object_id: 'obj/if', name: 'eth0' }], units: [], containers: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));
    await screen.findByText('eth0');
    await user.type(screen.getByRole('combobox'), 'eth');
    await user.keyboard('{Enter}');
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls.map(([url]) => new URL(url instanceof Request ? url.url : url, 'http://test.invalid').pathname).sort()).toEqual([
      '/api/v1/docker/containers', '/api/v1/network/interfaces', '/api/v1/systemd/units',
    ]);
    for (const [, options] of fetch.mock.calls) expect(options?.method).toBe('GET');
  });

  it('distinguishes an empty estate from a query with no match', async () => {
    mocks.interfaces.mockResolvedValue({ interfaces: [] });
    mocks.systemdUnits.mockResolvedValue({ units: [] });
    mocks.containers.mockResolvedValue({ containers: [] });
    const user = userEvent.setup();
    renderSearch();
    await user.click(screen.getByRole('button', { name: /search objects/i }));

    expect(await screen.findByText('No objects were returned by the available domains.')).toBeInTheDocument();
    await user.type(screen.getByRole('combobox'), 'missing');
    expect(screen.getByText('No observed objects match “missing”.')).toBeInTheDocument();
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

    expect(screen.getByText(/Showing the first 40 matches/)).toBeInTheDocument();
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
    const close = screen.getAllByRole('button', { name: /close object search/i })[1]!;

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
    expect(screen.getByText('Reading observed interfaces, units and containers…')).toBeInTheDocument();
    pending.resolve({ units: [] });
    await waitFor(() => expect(screen.queryByText('Reading observed interfaces, units and containers…')).not.toBeInTheDocument());
  });

  it('ignores a cancelled response after close and reopen', async () => {
    const stale = deferred<{ interfaces: Array<{ object_id: string; name: string }> }>();
    const current = deferred<{ interfaces: Array<{ object_id: string; name: string }> }>();
    mocks.interfaces.mockReturnValueOnce(stale.promise).mockReturnValueOnce(current.promise);
    mocks.systemdUnits.mockResolvedValue({ units: [] });
    mocks.containers.mockResolvedValue({ containers: [] });
    const user = userEvent.setup();
    renderSearch();
    const trigger = screen.getByRole('button', { name: /search objects/i });
    await user.click(trigger);
    await user.keyboard('{Escape}');
    await user.click(trigger);

    stale.resolve({ interfaces: [{ object_id: 'stale', name: 'stale-interface' }] });
    expect(screen.queryByText('stale-interface')).not.toBeInTheDocument();
    current.resolve({ interfaces: [{ object_id: 'current', name: 'current-interface' }] });
    expect(await screen.findByText('current-interface')).toBeInTheDocument();
  });
});
