import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { NetworkInterface } from '@/api/types';
import { BACKEND, stubBackend } from '@/test/backend';
import { InterfaceDetail } from './InterfaceDetail';

afterEach(() => vi.unstubAllGlobals());

const base = BACKEND['/api/v1/network/interfaces/obj_if'] as NetworkInterface;

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
