import { render, screen } from '@testing-library/react';
import { AddressingSummary } from './AddressingSummary';
import type { NetworkInterfaceList } from '@/api/types';
import { BACKEND } from '@/test/backend';

const observedInterface = (BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList)
  .interfaces[0]!;

function renderSummary(addresses: (typeof observedInterface.addresses)[], count = addresses.length): void {
  render(
    <AddressingSummary
      interfaces={addresses.map((value, index) => ({
        ...observedInterface,
        object_id: `interface-${index}`,
        name: `interface-${index}`,
        addresses: value,
      }))}
      observedAt={new Date('2026-01-01T00:00:00Z')}
    />,
  );
  expect(screen.getByText(String(count), { selector: 'span' })).toBeInTheDocument();
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

    expect(screen.getByText('No addresses')).toBeInTheDocument();
    expect(screen.getByText('No observed interface reports an address.')).toBeInTheDocument();
    expect(screen.queryByText(/no address source available/i)).not.toBeInTheDocument();
  });

  it('retains the known-empty wording when there are no interfaces', () => {
    renderSummary([], 0);

    expect(screen.getByText('No addresses')).toBeInTheDocument();
    expect(screen.getByText('No observed interface reports an address.')).toBeInTheDocument();
    expect(screen.queryByText(/no address source available/i)).not.toBeInTheDocument();
  });

  it('keeps reported rows and their unknown-interface disclosure together', () => {
    renderSummary([observedInterface.addresses, null], 1);

    expect(screen.getByText('192.168.1.42/24')).toBeInTheDocument();
    expect(screen.getByText(/1 interface had no address source available/i)).toBeInTheDocument();
    expect(screen.queryByText('No address records reported')).not.toBeInTheDocument();
  });
});
