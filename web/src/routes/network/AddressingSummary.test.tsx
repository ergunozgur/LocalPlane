import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AddressingSummary } from './AddressingSummary';
import type { Address, NetworkInterfaceList } from '@/api/types';
import { BACKEND } from '@/test/backend';

const observedInterface = (BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList)
  .interfaces[0]!;

function address(overrides: Partial<Address>): Address {
  const base = observedInterface.addresses![0]!;
  return { ...base, ...overrides };
}

function renderSummary(addresses: (typeof observedInterface.addresses)[], count = addresses.length): void {
  render(
    <MemoryRouter><AddressingSummary
      interfaces={addresses.map((value, index) => ({
        ...observedInterface,
        object_id: `interface-${index}`,
        name: `interface-${index}`,
        addresses: value,
      }))}
      observedAt={new Date('2026-01-01T00:00:00Z')}
    /></MemoryRouter>,
  );
  expect(screen.getByText(`${count} stored addresses`)).toBeInTheDocument();
}

describe('AddressingSummary empty and partial evidence', () => {
  it('does not describe all-unread address evidence as absence', () => {
    renderSummary([null, null], 0);

    expect(screen.getByText('No address records reported')).toBeInTheDocument();
    expect(screen.getByText('Address sources were unavailable for some interfaces; their addresses are unknown, not absent.')).toBeInTheDocument();
    expect(screen.getByText('2 interfaces had no address source available — their addresses are unknown, not absent')).toBeInTheDocument();
  });

  it('keeps known-empty interfaces distinct from unknown interfaces', () => {
    renderSummary([[], null], 0);

    expect(screen.getByText('No address records reported')).toBeInTheDocument();
    expect(screen.getByText('Address sources were unavailable for some interfaces; their addresses are unknown, not absent.')).toBeInTheDocument();
    expect(screen.getByText('1 interface had no address source available — their addresses are unknown, not absent')).toBeInTheDocument();
  });

  it('retains the known-empty wording when every interface reports no addresses', () => {
    renderSummary([[]], 0);

    expect(screen.getByText('No stored addresses')).toBeInTheDocument();
    expect(screen.getByText('No stored interface record reports an address. This does not establish current absence.')).toBeInTheDocument();
    expect(screen.queryByText(/no address source available/i)).not.toBeInTheDocument();
  });

  it('retains the known-empty wording when there are no interfaces', () => {
    renderSummary([], 0);

    expect(screen.getByText('No stored addresses')).toBeInTheDocument();
    expect(screen.getByText('No stored interface record reports an address. This does not establish current absence.')).toBeInTheDocument();
    expect(screen.queryByText(/no address source available/i)).not.toBeInTheDocument();
  });

  it('keeps reported rows and their unknown-interface disclosure together', () => {
    renderSummary([observedInterface.addresses, null], 1);

    expect(screen.getByText('192.168.1.42/24')).toBeInTheDocument();
    expect(screen.getByText(/1 interface had no address source available/i)).toBeInTheDocument();
    expect(screen.queryByText('No address records reported')).not.toBeInTheDocument();
  });
});

describe('AddressingSummary lifetime states', () => {
  it.each([
    { label: 'infinite', state: 'infinite' as const, seconds: null, expected: 'forever' },
    { label: 'finite zero', state: 'finite' as const, seconds: 0, expected: '0s' },
    { label: 'finite', state: 'finite' as const, seconds: 3600, expected: '1h 0m' },
    { label: 'unknown', state: 'unknown' as const, seconds: null, expected: 'unknown', count: 1 },
  ])('renders valid $label lifetime without truthiness loss', ({ state, seconds, expected, count = 1 }) => {
    renderSummary([[address({ valid_lifetime_state: state, valid_lifetime_s: seconds })]]);
    expect(screen.getAllByText(expected)).toHaveLength(count);
  });

  it.each([
    { label: 'infinite', state: 'infinite' as const, seconds: null, expected: 'forever' },
    { label: 'finite zero', state: 'finite' as const, seconds: 0, expected: '0s' },
    { label: 'finite', state: 'finite' as const, seconds: 1800, expected: '30m 0s' },
    { label: 'unknown', state: 'unknown' as const, seconds: null, expected: 'unknown', count: 1 },
  ])('renders preferred $label lifetime without truthiness loss', ({ state, seconds, expected, count = 1 }) => {
    renderSummary([[address({ preferred_lifetime_state: state, preferred_lifetime_s: seconds })]]);
    expect(screen.getAllByText(expected)).toHaveLength(count);
  });

  it('treats malformed lifetime state and missing seconds as unknown', () => {
    renderSummary([
      [address({ valid_lifetime_state: 'provider_new_state' as unknown as 'unknown', valid_lifetime_s: 3600 })],
      [address({ preferred_lifetime_state: null as unknown as 'unknown', preferred_lifetime_s: null })],
    ]);
    const unknowns = screen.getAllByText('unknown');
    expect(unknowns.length).toBeGreaterThanOrEqual(2);
  });

  it('keeps a contradictory infinite state unknown rather than asserting forever', () => {
    renderSummary([[address({ valid_lifetime_state: 'infinite', valid_lifetime_s: 3600 })]]);
    expect(screen.getByText('unknown')).toBeInTheDocument();
    expect(screen.queryByText('forever')).not.toBeInTheDocument();
  });
});

it('qualifies retained address evidence and links to the exact interface', () => {
  render(<MemoryRouter><AddressingSummary interfaces={[{
    ...observedInterface,
    object_id: 'interface/retained',
    name: 'retained0',
    observed_in_latest_sweep: false,
    observation: { ...observedInterface.observation!, freshness: 'stale' },
  }]} observedAt={new Date('2026-01-01T00:00:00Z')} /></MemoryRouter>);
  expect(screen.getByText('not seen in latest inventory')).toBeInTheDocument();
  expect(screen.getByText('stale')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'retained0' })).toHaveAttribute('href', '/network/interface%2Fretained');
  expect(screen.getByText(/not a live countdown/)).toBeInTheDocument();
});
