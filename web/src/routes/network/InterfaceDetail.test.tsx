import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { NetworkInterface } from '@/api/types';
import { BACKEND, stubBackend } from '@/test/backend';
import { InterfaceDetail } from './InterfaceDetail';
import { endpoints } from '@/api/endpoints';
import { CONNECTIVITY, ROUTE } from '@/test/connectivity';
import type { RouteResult } from '@/api/types';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const base = BACKEND['/api/v1/network/interfaces/obj_if'] as NetworkInterface;

it('keeps a pending explicit action and its input/result across tabs without cancelling or resubmitting', async () => {
  stubBackend();
  vi.spyOn(endpoints, 'connectivity').mockResolvedValue(CONNECTIVITY);
  let resolve!: (value: RouteResult) => void;
  const route = vi.spyOn(endpoints, 'connectivityRoute').mockReturnValue(new Promise((done) => { resolve = done; }));
  render(<MemoryRouter initialEntries={['/network/obj_if?tab=connectivity']}>
    <Routes><Route path="/network/:objectId" element={<InterfaceDetail />} /></Routes>
  </MemoryRouter>);
  const input = await screen.findByLabelText('Numeric route destination');
  await userEvent.type(input, '192.0.2.1');
  await userEvent.click(screen.getByRole('button', { name: 'Look up route' }));
  await userEvent.click(screen.getByRole('tab', { name: /Addressing/ }));
  expect(await screen.findByRole('table', { name: 'Addresses' })).toBeInTheDocument();
  await act(async () => { resolve(ROUTE); });
  await userEvent.click(screen.getByRole('tab', { name: 'Connectivity' }));
  expect(screen.getByLabelText('Numeric route destination')).toHaveValue('192.0.2.1');
  expect(screen.getByText('Route lookup · selected')).toBeInTheDocument();
  expect(route).toHaveBeenCalledTimes(1);
  expect(endpoints.connectivity).toHaveBeenCalledTimes(1);
});

it('opens Connectivity by address, retains the workspace and sends only stored connectivity GETs', async () => {
  stubBackend();
  const fetch = vi.mocked(globalThis.fetch);
  render(
    <MemoryRouter initialEntries={['/network/obj_if?tab=connectivity']}>
      <Routes><Route path="/network/:objectId" element={<InterfaceDetail />} /></Routes>
    </MemoryRouter>,
  );
  const tab = await screen.findByRole('tab', { name: 'Connectivity' });
  expect(tab).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByRole('tabpanel', { name: 'Connectivity' })).toBeInTheDocument();
  expect(screen.getByText('Machine record')).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /Addressing/ })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('tab', { name: /Addressing/ }));
  expect(await screen.findByRole('table', { name: 'Addresses' })).toBeInTheDocument();
  await userEvent.click(tab);
  expect(screen.getByText(/No action has been requested/)).toBeInTheDocument();
  let connectivityReads = 0;
  for (const [url, options] of fetch.mock.calls) {
    const path = typeof url === 'string' ? url : url instanceof URL ? url.href : url.url;
    if (path.endsWith('/connectivity')) connectivityReads++;
    expect(options?.method ?? 'GET').toBe('GET');
  }
  expect(connectivityReads).toBe(1);
});

describe('interface detail address lifetimes', () => {
  it.each([
    { state: undefined, seconds: null, expected: 'unknown' },
    { state: 'unknown', seconds: null, expected: 'unknown' },
    { state: 'finite', seconds: 0, expected: '0s' },
    { state: 'finite', seconds: 3600, expected: '1h 0m' },
    { state: 'infinite', seconds: null, expected: 'forever' },
    { state: 'infinite', seconds: 0, expected: 'unknown' },
    { state: 'finite', seconds: -1, expected: 'unknown' },
  ] as const)('renders both $state/$seconds lifetimes as $expected', async ({ state, seconds, expected }) => {
    const address = {
      ...base.addresses![0]!,
      valid_lifetime_state: state,
      preferred_lifetime_state: state,
      valid_lifetime_s: seconds,
      preferred_lifetime_s: seconds,
    };
    stubBackend({ '/api/v1/network/interfaces/obj_if': { ...base, addresses: [address] } });
    render(
      <MemoryRouter initialEntries={['/network/obj_if?tab=addressing']}>
        <Routes><Route path="/network/:objectId" element={<InterfaceDetail />} /></Routes>
      </MemoryRouter>,
    );
    const table = await screen.findByRole('table', { name: 'Addresses' });
    const rows = within(table).getAllByRole('row');
    const cells = within(rows[1]!).getAllByRole('cell');
    expect(cells[4]).toHaveTextContent(expected);
    expect(cells[5]).toHaveTextContent(expected);
    if (expected !== 'forever') expect(within(table).queryByText('forever')).not.toBeInTheDocument();
  });
});

it('keeps retained membership and stale evidence visible on the addressing tab', async () => {
  stubBackend({ '/api/v1/network/interfaces/obj_if': {
    ...base,
    observed_in_latest_sweep: false,
    observation: { ...base.observation!, freshness: 'stale' },
  } });
  render(
    <MemoryRouter initialEntries={['/network/obj_if?tab=addressing']}>
      <Routes><Route path="/network/:objectId" element={<InterfaceDetail />} /></Routes>
    </MemoryRouter>,
  );
  await screen.findByRole('table', { name: 'Addresses' });
  expect(screen.getByText('not seen in latest inventory')).toBeInTheDocument();
  expect(screen.getByText('stale')).toBeInTheDocument();
  expect(screen.getByText(/No intended state is retained/)).toBeInTheDocument();
});
